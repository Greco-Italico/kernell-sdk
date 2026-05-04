"""
Kernell OS — Context Router
════════════════════════════
Indexes the local codebase and intelligently selects which files/functions
are relevant for a given task. This is the equivalent of Cursor's "codebase
awareness" but designed for distributed multi-agent execution.

Key difference from Cursor:
  - Cursor sends context to ONE model.
  - Kernell sends structured context to N specialized agents via the marketplace.
  - Each agent gets only the context relevant to its sub-task.
"""
import os
import hashlib
import json
import fnmatch
import logging
from pathlib import Path
from dataclasses import dataclass, field, asdict
from typing import List, Dict, Optional, Set

logger = logging.getLogger("kernell.devlayer.context")

# File extensions we index (code files only)
CODE_EXTENSIONS = {
    ".py", ".js", ".ts", ".tsx", ".jsx", ".go", ".rs", ".java",
    ".c", ".cpp", ".h", ".hpp", ".rb", ".php", ".swift", ".kt",
    ".scala", ".sh", ".bash", ".yaml", ".yml", ".toml", ".json",
    ".sql", ".html", ".css", ".scss", ".md",
}

# Default ignore patterns
DEFAULT_IGNORE = {
    "__pycache__", ".git", ".venv", "node_modules", ".hypothesis",
    ".pytest_cache", "dist", "build", ".egg-info", ".tox",
    "venv", "env", ".mypy_cache", ".ruff_cache",
}

MAX_FILE_SIZE = 100_000  # 100KB max per file for indexing


@dataclass
class FileNode:
    """A single indexed file in the codebase graph."""
    path: str              # Relative path from project root
    language: str          # Detected language
    size_bytes: int
    content_hash: str      # SHA-256 of file content
    imports: List[str] = field(default_factory=list)
    exports: List[str] = field(default_factory=list)
    symbols: List[str] = field(default_factory=list)  # functions, classes
    last_modified: float = 0.0


@dataclass
class CodebaseGraph:
    """The full indexed representation of a project."""
    root: str
    files: Dict[str, FileNode] = field(default_factory=dict)
    dependency_edges: List[tuple] = field(default_factory=list)
    total_tokens_estimate: int = 0

    def summary(self) -> dict:
        langs = {}
        for f in self.files.values():
            langs[f.language] = langs.get(f.language, 0) + 1
        return {
            "root": self.root,
            "total_files": len(self.files),
            "languages": langs,
            "estimated_tokens": self.total_tokens_estimate,
        }


class ContextRouter:
    """
    Indexes a codebase and selects relevant context for tasks.
    
    This replaces Cursor's internal indexing with a system designed
    for distributed multi-agent execution:
    
    1. Index: Scan project, extract symbols, build dependency graph
    2. Route: Given a task description, select relevant files
    3. Chunk: Split selected files into agent-appropriate context windows
    """

    def __init__(self, project_root: str, ignore_patterns: Optional[Set[str]] = None):
        self.project_root = Path(project_root).resolve()
        self.ignore_patterns = ignore_patterns or DEFAULT_IGNORE
        self.graph: Optional[CodebaseGraph] = None
        self._index_cache_path = self.project_root / ".kernell" / "index.json"

    def index(self, force: bool = False) -> CodebaseGraph:
        """Scan the project and build a codebase graph."""
        if not force and self._index_cache_path.exists():
            try:
                cached = self._load_cache()
                if cached:
                    self.graph = cached
                    logger.info(f"Loaded cached index: {len(cached.files)} files")
                    return cached
            except Exception:
                pass

        logger.info(f"Indexing project: {self.project_root}")
        graph = CodebaseGraph(root=str(self.project_root))
        total_tokens = 0

        for filepath in self._walk_files():
            rel_path = str(filepath.relative_to(self.project_root))
            try:
                stat = filepath.stat()
                if stat.st_size > MAX_FILE_SIZE:
                    continue

                content = filepath.read_text(encoding="utf-8", errors="ignore")
                content_hash = hashlib.sha256(content.encode()).hexdigest()

                lang = self._detect_language(filepath)
                symbols = self._extract_symbols(content, lang)
                imports = self._extract_imports(content, lang)

                node = FileNode(
                    path=rel_path,
                    language=lang,
                    size_bytes=stat.st_size,
                    content_hash=content_hash,
                    imports=imports,
                    symbols=symbols,
                    last_modified=stat.st_mtime,
                )
                graph.files[rel_path] = node

                # Rough token estimate: ~4 chars per token
                total_tokens += len(content) // 4

            except Exception as e:
                logger.debug(f"Skipping {rel_path}: {e}")
                continue

        # Build dependency edges from imports
        graph.dependency_edges = self._build_edges(graph)
        graph.total_tokens_estimate = total_tokens
        self.graph = graph

        # Cache to disk
        self._save_cache(graph)
        logger.info(f"Indexed {len(graph.files)} files, ~{total_tokens} tokens")
        return graph

    def select_context(self, task_description: str, max_files: int = 15) -> List[Dict]:
        """
        Given a task description, select the most relevant files.
        
        Strategy:
        1. Keyword match against file paths and symbols
        2. Follow dependency edges for connected files
        3. Rank by relevance score
        4. Return top N with content
        """
        if not self.graph:
            self.index()

        scores = {}
        task_lower = task_description.lower()
        task_words = set(task_lower.split())

        for rel_path, node in self.graph.files.items():
            score = 0.0

            # Path relevance (file name matches task keywords)
            path_parts = set(rel_path.lower().replace("/", " ").replace("_", " ").replace(".", " ").split())
            path_overlap = len(task_words & path_parts)
            score += path_overlap * 10.0

            # Symbol relevance (function/class names match)
            for sym in node.symbols:
                sym_lower = sym.lower()
                for word in task_words:
                    if word in sym_lower or sym_lower in word:
                        score += 5.0

            # Language bonus (if task mentions a language)
            if node.language in task_lower:
                score += 3.0

            # Recency bonus (recently modified files are more relevant)
            score += min(node.last_modified / 1e10, 2.0)

            if score > 0:
                scores[rel_path] = score

        # Sort by score, take top N
        ranked = sorted(scores.items(), key=lambda x: x[1], reverse=True)[:max_files]

        # Expand with dependencies (add files imported by selected files)
        selected_paths = {p for p, _ in ranked}
        for rel_path, _ in ranked[:5]:  # Only expand top 5
            node = self.graph.files[rel_path]
            for imp in node.imports:
                for candidate, cnode in self.graph.files.items():
                    if candidate not in selected_paths and imp in candidate:
                        selected_paths.add(candidate)
                        if len(selected_paths) >= max_files:
                            break

        # Build context payload
        context = []
        for rel_path in list(selected_paths)[:max_files]:
            node = self.graph.files[rel_path]
            try:
                full_path = self.project_root / rel_path
                content = full_path.read_text(encoding="utf-8", errors="ignore")
                context.append({
                    "path": rel_path,
                    "language": node.language,
                    "symbols": node.symbols,
                    "content": content,
                    "relevance_score": scores.get(rel_path, 0),
                })
            except Exception:
                continue

        context.sort(key=lambda x: x["relevance_score"], reverse=True)
        logger.info(f"Selected {len(context)} files for task context")
        return context

    def get_file_content(self, rel_path: str) -> Optional[str]:
        """Get content of a specific file."""
        full_path = self.project_root / rel_path
        if full_path.exists():
            return full_path.read_text(encoding="utf-8", errors="ignore")
        return None

    # ── Private Methods ──────────────────────────────────────────────

    def _walk_files(self):
        """Walk project tree respecting ignore patterns."""
        for root, dirs, files in os.walk(self.project_root):
            # Filter out ignored directories in-place
            dirs[:] = [d for d in dirs if d not in self.ignore_patterns
                       and not d.startswith(".")]

            for fname in files:
                fpath = Path(root) / fname
                if fpath.suffix in CODE_EXTENSIONS:
                    yield fpath

    def _detect_language(self, filepath: Path) -> str:
        """Detect programming language from file extension."""
        ext_map = {
            ".py": "python", ".js": "javascript", ".ts": "typescript",
            ".tsx": "typescript", ".jsx": "javascript", ".go": "go",
            ".rs": "rust", ".java": "java", ".rb": "ruby",
            ".php": "php", ".c": "c", ".cpp": "cpp", ".h": "c",
            ".swift": "swift", ".kt": "kotlin", ".scala": "scala",
            ".sh": "bash", ".yaml": "yaml", ".yml": "yaml",
            ".toml": "toml", ".json": "json", ".sql": "sql",
            ".html": "html", ".css": "css", ".md": "markdown",
        }
        return ext_map.get(filepath.suffix, "unknown")

    def _extract_symbols(self, content: str, language: str) -> List[str]:
        """Extract function/class names from code content."""
        symbols = []
        if language == "python":
            for line in content.split("\n"):
                stripped = line.strip()
                if stripped.startswith("def "):
                    name = stripped[4:].split("(")[0].strip()
                    if name and not name.startswith("_"):
                        symbols.append(name)
                elif stripped.startswith("class "):
                    name = stripped[6:].split("(")[0].split(":")[0].strip()
                    if name:
                        symbols.append(name)
        elif language in ("javascript", "typescript"):
            for line in content.split("\n"):
                stripped = line.strip()
                if "function " in stripped:
                    parts = stripped.split("function ")[1].split("(")[0].strip()
                    if parts:
                        symbols.append(parts)
                elif stripped.startswith("export "):
                    parts = stripped.replace("export ", "").split(" ")
                    if len(parts) >= 2:
                        symbols.append(parts[1].split("(")[0].split(":")[0])
        elif language == "go":
            for line in content.split("\n"):
                stripped = line.strip()
                if stripped.startswith("func "):
                    name = stripped[5:].split("(")[0].strip()
                    if name:
                        symbols.append(name)
                elif stripped.startswith("type "):
                    parts = stripped.split()
                    if len(parts) >= 2:
                        symbols.append(parts[1])
        return symbols

    def _extract_imports(self, content: str, language: str) -> List[str]:
        """Extract import paths from code."""
        imports = []
        if language == "python":
            for line in content.split("\n"):
                stripped = line.strip()
                if stripped.startswith("from ") and " import " in stripped:
                    module = stripped.split("from ")[1].split(" import")[0].strip()
                    imports.append(module)
                elif stripped.startswith("import "):
                    module = stripped[7:].split(" as ")[0].split(",")[0].strip()
                    imports.append(module)
        elif language in ("javascript", "typescript"):
            for line in content.split("\n"):
                if "from " in line and ("import" in line or "require" in line):
                    parts = line.split("from ")
                    if len(parts) > 1:
                        path = parts[-1].strip().strip("';\"")
                        imports.append(path)
        return imports

    def _build_edges(self, graph: CodebaseGraph) -> List[tuple]:
        """Build dependency edges between files based on imports."""
        edges = []
        for source_path, node in graph.files.items():
            for imp in node.imports:
                for target_path in graph.files:
                    # Fuzzy match: import path matches file path
                    imp_normalized = imp.replace(".", "/")
                    if imp_normalized in target_path:
                        edges.append((source_path, target_path))
        return edges

    def _save_cache(self, graph: CodebaseGraph):
        """Persist index to disk for fast reload."""
        try:
            self._index_cache_path.parent.mkdir(parents=True, exist_ok=True)
            data = {
                "root": graph.root,
                "total_tokens_estimate": graph.total_tokens_estimate,
                "files": {k: asdict(v) for k, v in graph.files.items()},
                "edges": graph.dependency_edges,
            }
            self._index_cache_path.write_text(json.dumps(data, indent=2))
        except Exception as e:
            logger.warning(f"Failed to save index cache: {e}")

    def _load_cache(self) -> Optional[CodebaseGraph]:
        """Load cached index from disk."""
        data = json.loads(self._index_cache_path.read_text())
        graph = CodebaseGraph(
            root=data["root"],
            total_tokens_estimate=data.get("total_tokens_estimate", 0),
        )
        for rel_path, fdata in data.get("files", {}).items():
            graph.files[rel_path] = FileNode(**fdata)
        graph.dependency_edges = [tuple(e) for e in data.get("edges", [])]
        return graph
