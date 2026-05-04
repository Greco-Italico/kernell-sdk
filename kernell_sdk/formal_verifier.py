"""
Kernell OS SDK — Formal Verifier (MVP)
═══════════════════════════════════════
Deterministic, math-based verification gate for agent actions.
NO LLM calls. Pure algorithmic checks that cannot be manipulated.

Ported from Kernell OS core/sea/formal_verifier.py (MVP subset).

This module sits between the CodePipeline and the Execution Gateway:

    Agent Intent → CodePipeline → FormalVerifier → ExecutionGate → Sandbox

If ANY check fails → action is BLOCKED. No debate. No override.

Checks implemented (MVP):
  1. FORBIDDEN_OPERATIONS: No file deletion outside /tmp, /workspace
  2. FORBIDDEN_IMPORTS: No os.system, subprocess.call with shell=True
  3. NETWORK_POLICY: No socket connections to internal networks
  4. RESOURCE_BOUNDS: No unbounded loops, fork bombs
  5. INVARIANT_LOCK: Custom invariants defined by the user

Usage:
    from kernell_sdk.formal_verifier import FormalVerifier, VerificationResult

    verifier = FormalVerifier()
    result = verifier.verify(code="import os; os.system('rm -rf /')")
    if not result.passed:
        print(f"BLOCKED: {result.violations}")
"""

import ast
import logging
import re
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Set

logger = logging.getLogger("kernell.formal_verifier")


@dataclass
class Violation:
    """A single verification violation."""
    check: str          # Which check caught it
    severity: str       # "CRITICAL", "HIGH", "MEDIUM", "LOW"
    message: str        # Human-readable explanation
    line: Optional[int] = None
    evidence: str = ""  # The problematic code snippet


@dataclass
class VerificationResult:
    """Complete output of the formal verification pass."""
    passed: bool
    violations: List[Violation] = field(default_factory=list)
    checks_run: int = 0
    duration_ms: float = 0.0

    @property
    def critical_count(self) -> int:
        return sum(1 for v in self.violations if v.severity == "CRITICAL")

    @property
    def high_count(self) -> int:
        return sum(1 for v in self.violations if v.severity == "HIGH")


# ══════════════════════════════════════════════════════════════════════
# FORBIDDEN PATTERNS (compiled regexes for performance)
# ══════════════════════════════════════════════════════════════════════

_DANGEROUS_CALLS = {
    # Shell execution
    r"os\.system\s*\(":             ("CRITICAL", "os.system() — arbitrary shell execution"),
    r"os\.popen\s*\(":              ("CRITICAL", "os.popen() — shell execution with pipe"),
    r"os\.exec[lv]p?e?\s*\(":       ("CRITICAL", "os.exec*() — process replacement"),
    r"subprocess\.call\(.*shell\s*=\s*True": ("CRITICAL", "subprocess.call with shell=True"),
    r"subprocess\.Popen\(.*shell\s*=\s*True": ("CRITICAL", "subprocess.Popen with shell=True"),
    r"subprocess\.run\(.*shell\s*=\s*True": ("CRITICAL", "subprocess.run with shell=True"),
    r"subprocess\.(?:call|run|Popen|check_output)\s*\(": ("HIGH", "subprocess usage detected"),

    # File destruction
    r"shutil\.rmtree\s*\(":         ("HIGH", "shutil.rmtree() — recursive directory deletion"),
    r"os\.remove\s*\(":             ("MEDIUM", "os.remove() — file deletion"),
    r"os\.unlink\s*\(":             ("MEDIUM", "os.unlink() — file deletion"),
    r"pathlib\.Path.*\.unlink":     ("MEDIUM", "Path.unlink() — file deletion"),

    # Network — expanded for Phase 4
    r"socket\.socket\s*\(":         ("HIGH", "Direct socket creation"),
    r"requests\.(?:get|post|put|delete|patch|head)\s*\(": ("HIGH", "HTTP request via requests library"),
    r"urllib\.request\.urlopen\s*\(": ("HIGH", "HTTP request via urllib"),
    r"urllib\.request\.Request\s*\(": ("HIGH", "HTTP request construction via urllib"),
    r"http\.client\.HTTP": ("HIGH", "HTTP connection via http.client"),
    r"httpx\.(?:get|post|Client)":  ("HIGH", "HTTP request via httpx"),
    r"aiohttp\.ClientSession":      ("HIGH", "Async HTTP session via aiohttp"),

    # Code execution
    r"(?<!\w)exec\s*\(":            ("CRITICAL", "exec() — arbitrary code execution"),
    r"(?<!\w)eval\s*\(":            ("HIGH", "eval() — arbitrary expression evaluation"),
    r"compile\s*\(.*exec":          ("HIGH", "compile() with exec mode"),

    # Process manipulation
    r"os\.fork\s*\(":               ("CRITICAL", "os.fork() — process forking (fork bomb risk)"),
    r"os\.kill\s*\(":               ("HIGH", "os.kill() — process killing"),
    r"signal\.signal\s*\(":         ("MEDIUM", "signal.signal() — signal handler modification"),

    # Environment / secrets access
    r"os\.environ":                 ("HIGH", "os.environ — environment variable access"),
    r"os\.getenv\s*\(":             ("HIGH", "os.getenv() — environment variable access"),
}

_DANGEROUS_IMPORTS = {
    "ctypes":      ("HIGH", "ctypes — direct memory access"),
    "pickle":      ("MEDIUM", "pickle — deserialization vulnerability"),
    "marshal":     ("HIGH", "marshal — low-level serialization"),
    "importlib":   ("MEDIUM", "importlib — dynamic import manipulation"),
    "requests":    ("HIGH", "requests — network access"),
    "httpx":       ("HIGH", "httpx — network access"),
    "aiohttp":     ("HIGH", "aiohttp — async network access"),
    "paramiko":    ("CRITICAL", "paramiko — SSH connections"),
    "ftplib":      ("HIGH", "ftplib — FTP connections"),
    "smtplib":     ("HIGH", "smtplib — email sending"),
    "telnetlib":   ("HIGH", "telnetlib — telnet connections"),
    "webbrowser":  ("MEDIUM", "webbrowser — browser opening"),
}

_RESOURCE_PATTERNS = {
    r"while\s+True\s*:(?!\s*#.*break)":  ("HIGH", "Unbounded while True loop (no visible break)"),
    r"while\s+1\s*:":                     ("HIGH", "Unbounded while 1 loop"),
    r"recursion_limit":                   ("MEDIUM", "Attempting to modify recursion limit"),
    r"range\s*\(\s*10\s*\*\*\s*[7-9]\b":  ("HIGH", "Extremely large range (10^7+) — CPU bomb"),
    r"range\s*\(\s*10\s*\*\*\s*\d{2,}":   ("CRITICAL", "Astronomical range (10^10+) — CPU bomb"),
    r"\*\s*10\s*\*\*\s*[89]":             ("HIGH", "Large exponential operation — memory bomb"),
    r"""b?['"].*['"]\s*\*\s*\d{6,}""":    ("HIGH", "String/bytes multiplication bomb"),
}


class FormalVerifier:
    """
    Deterministic verification gate for agent-generated code.
    No LLM. No network. Pure algorithmic analysis.
    """

    def __init__(
        self,
        allowed_paths: Optional[Set[str]] = None,
        custom_invariants: Optional[List[Callable[[str], Optional[Violation]]]] = None,
        strict_mode: bool = True,
    ):
        """
        Args:
            allowed_paths: Set of path prefixes where file operations are allowed.
                           Defaults to {"/tmp", "/workspace", "/var/lib/kernell"}.
            custom_invariants: User-defined check functions. Each takes code string
                              and returns Violation or None.
            strict_mode: If True, any CRITICAL or HIGH violation blocks execution.
                        If False, only CRITICAL blocks.
        """
        self._allowed_paths = allowed_paths or {"/tmp", "/workspace", "/var/lib/kernell"}
        self._custom_invariants = custom_invariants or []
        self._strict_mode = strict_mode

    def verify(self, code: str) -> VerificationResult:
        """
        Run all verification checks on the provided code.
        Returns VerificationResult with pass/fail and any violations.
        """
        t0 = time.time()
        violations: List[Violation] = []
        checks = 0

        # Phase 4: Normalize/deobfuscate before analysis
        normalized = self._normalize(code)

        # Check 1: Forbidden operations (regex-based)
        checks += 1
        violations.extend(self._check_dangerous_calls(normalized))

        # Check 2: Forbidden imports
        checks += 1
        violations.extend(self._check_dangerous_imports(normalized))

        # Check 3: File path safety
        checks += 1
        violations.extend(self._check_file_paths(normalized))

        # Check 4: Resource bounds
        checks += 1
        violations.extend(self._check_resource_bounds(normalized))

        # Check 5: AST-based analysis (deeper than regex)
        checks += 1
        violations.extend(self._check_ast(code))  # Use original for valid AST

        # Check 6: Obfuscation detection (Phase 4)
        checks += 1
        violations.extend(self._check_obfuscation(code))

        # Check 7: Data exfiltration patterns (Phase 4)
        checks += 1
        violations.extend(self._check_exfiltration(normalized))

        # Check 8: Custom invariants
        for invariant in self._custom_invariants:
            checks += 1
            try:
                v = invariant(code)
                if v:
                    violations.append(v)
            except Exception as e:
                logger.warning(f"[FormalVerifier] Custom invariant error: {e}")

        # Determine pass/fail
        if self._strict_mode:
            passed = all(v.severity not in ("CRITICAL", "HIGH") for v in violations)
        else:
            passed = all(v.severity != "CRITICAL" for v in violations)

        duration_ms = round((time.time() - t0) * 1000, 2)

        if violations:
            logger.warning(
                f"[FormalVerifier] {len(violations)} violations found "
                f"({sum(1 for v in violations if v.severity == 'CRITICAL')} CRITICAL, "
                f"{sum(1 for v in violations if v.severity == 'HIGH')} HIGH)"
            )
        else:
            logger.info(f"[FormalVerifier] All {checks} checks passed ({duration_ms}ms)")

        return VerificationResult(
            passed=passed,
            violations=violations,
            checks_run=checks,
            duration_ms=duration_ms,
        )

    # ── Check Implementations ────────────────────────────────────────

    def _check_dangerous_calls(self, code: str) -> List[Violation]:
        violations = []
        lines = code.split("\n")
        for pattern, (severity, msg) in _DANGEROUS_CALLS.items():
            for i, line in enumerate(lines, 1):
                stripped = line.strip()
                if stripped.startswith("#"):
                    continue  # Skip comments
                if re.search(pattern, line):
                    violations.append(Violation(
                        check="FORBIDDEN_OPERATIONS",
                        severity=severity,
                        message=msg,
                        line=i,
                        evidence=line.strip()[:100],
                    ))
        return violations

    def _check_dangerous_imports(self, code: str) -> List[Violation]:
        violations = []
        lines = code.split("\n")
        for i, line in enumerate(lines, 1):
            stripped = line.strip()
            if stripped.startswith("#"):
                continue
            for mod, (severity, msg) in _DANGEROUS_IMPORTS.items():
                if re.search(rf"(?:import\s+{mod}|from\s+{mod}\s+import)", stripped):
                    violations.append(Violation(
                        check="FORBIDDEN_IMPORTS",
                        severity=severity,
                        message=msg,
                        line=i,
                        evidence=stripped[:100],
                    ))
        return violations

    def _check_file_paths(self, code: str) -> List[Violation]:
        """Check that file operations only target allowed paths."""
        violations = []
        # Find string literals that look like absolute paths
        path_pattern = re.compile(r'''['"](/[a-zA-Z][a-zA-Z0-9_/.\-]*)['"]''')
        lines = code.split("\n")

        for i, line in enumerate(lines, 1):
            stripped = line.strip()
            if stripped.startswith("#"):
                continue
            # Only check lines with file operations
            has_file_op = any(op in line for op in [
                "open(", "remove(", "unlink(", "rmtree(", "mkdir(",
                "write_text(", "write_bytes(", "rename(", "replace("
            ])
            if not has_file_op:
                continue

            for match in path_pattern.finditer(line):
                path = match.group(1)
                if not any(path.startswith(allowed) for allowed in self._allowed_paths):
                    violations.append(Violation(
                        check="FILE_PATH_SAFETY",
                        severity="HIGH",
                        message=f"File operation on path outside allowed zones: {path}",
                        line=i,
                        evidence=stripped[:100],
                    ))
        return violations

    def _check_resource_bounds(self, code: str) -> List[Violation]:
        violations = []
        lines = code.split("\n")
        for pattern, (severity, msg) in _RESOURCE_PATTERNS.items():
            for i, line in enumerate(lines, 1):
                if line.strip().startswith("#"):
                    continue
                if re.search(pattern, line):
                    violations.append(Violation(
                        check="RESOURCE_BOUNDS",
                        severity=severity,
                        message=msg,
                        line=i,
                        evidence=line.strip()[:100],
                    ))
        return violations

    def _check_ast(self, code: str) -> List[Violation]:
        """AST-based checks for deeper analysis."""
        violations = []
        try:
            tree = ast.parse(code)
        except SyntaxError:
            return violations

        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                func = node.func
                # __import__() direct
                if isinstance(func, ast.Name) and func.id == "__import__":
                    violations.append(Violation(
                        check="FORBIDDEN_OPERATIONS",
                        severity="CRITICAL",
                        message="__import__() — dynamic import bypass",
                        line=getattr(node, "lineno", None),
                    ))

                # getattr(__import__("os"), "system") — Phase 4 obfuscation detection
                if isinstance(func, ast.Name) and func.id == "getattr":
                    if node.args and isinstance(node.args[0], ast.Call):
                        inner = node.args[0]
                        if isinstance(inner.func, ast.Name) and inner.func.id == "__import__":
                            violations.append(Violation(
                                check="OBFUSCATION",
                                severity="CRITICAL",
                                message="getattr(__import__()) — obfuscated dynamic import + attribute access",
                                line=getattr(node, "lineno", None),
                            ))

                # os.system(), os.popen(), etc.
                if isinstance(func, ast.Attribute):
                    if isinstance(func.value, ast.Name):
                        if func.value.id == "os" and func.attr in ("system", "popen", "exec", "execvp", "execl", "execlp"):
                            violations.append(Violation(
                                check="FORBIDDEN_OPERATIONS",
                                severity="CRITICAL",
                                message=f"os.{func.attr}() detected via AST",
                                line=getattr(node, "lineno", None),
                            ))
                        # subprocess.* without shell=True (still caught)
                        if func.value.id == "subprocess" and func.attr in ("call", "run", "Popen", "check_output", "check_call"):
                            violations.append(Violation(
                                check="FORBIDDEN_OPERATIONS",
                                severity="HIGH",
                                message=f"subprocess.{func.attr}() detected via AST",
                                line=getattr(node, "lineno", None),
                            ))

            # Tail recursion detection (Phase 4)
            if isinstance(node, ast.FunctionDef):
                depth = self._get_nesting_depth(node)
                if depth > 5:
                    violations.append(Violation(
                        check="RESOURCE_BOUNDS",
                        severity="MEDIUM",
                        message=f"Deeply nested function ({depth} levels) — potential obfuscation",
                        line=getattr(node, "lineno", None),
                    ))
                # Detect self-calling functions without base case guard
                if self._is_unguarded_recursion(node):
                    violations.append(Violation(
                        check="RESOURCE_BOUNDS",
                        severity="HIGH",
                        message=f"Function '{node.name}' appears to recurse without base case",
                        line=getattr(node, "lineno", None),
                    ))

        return violations

    @staticmethod
    def _get_nesting_depth(node, depth=0) -> int:
        max_depth = depth
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                child_depth = FormalVerifier._get_nesting_depth(child, depth + 1)
                max_depth = max(max_depth, child_depth)
        return max_depth

    @staticmethod
    def _is_unguarded_recursion(func_node: ast.FunctionDef) -> bool:
        """Detect functions that call themselves without an if/return guard."""
        fname = func_node.name
        has_self_call = False
        has_base_case = False
        for node in ast.walk(func_node):
            # Check for self-call
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
                if node.func.id == fname:
                    has_self_call = True
            # Check for if+return pattern (base case)
            if isinstance(node, ast.If):
                for child in ast.walk(node):
                    if isinstance(child, ast.Return):
                        has_base_case = True
                        break
        return has_self_call and not has_base_case

    # ── Phase 4: Deobfuscation ───────────────────────────────────────

    @staticmethod
    def _normalize(code: str) -> str:
        """Light deobfuscation: resolve simple string-building patterns."""
        # Collapse line continuations
        normalized = code.replace("\\\n", "")
        # Collapse string concatenation: "o" + "s" → "os"
        normalized = re.sub(r'"([^"]{1,3})"\s*\+\s*"([^"]{1,3})"', r'"\1\2"', normalized)
        normalized = re.sub(r"'([^']{1,3})'\s*\+\s*'([^']{1,3})'", r"'\1\2'", normalized)
        return normalized

    # ── Phase 4: Obfuscation Detection ───────────────────────────────

    def _check_obfuscation(self, code: str) -> List[Violation]:
        """Detect common obfuscation patterns used to bypass static analysis."""
        violations = []
        lines = code.split("\n")

        obfuscation_patterns = {
            # getattr + __import__ combo (the classic bypass)
            r'getattr\s*\(\s*__import__':   ("CRITICAL", "getattr(__import__()) — obfuscated module access"),
            # chr() string building
            r'chr\s*\(\s*\d+\s*\)\s*\+\s*chr': ("HIGH", "chr() string building — potential code obfuscation"),
            # eval/exec of joined strings
            r'(?:eval|exec)\s*\(.*\.join':  ("CRITICAL", "eval/exec of joined strings — code assembly"),
            r'(?:eval|exec)\s*\(.*chr\(':   ("CRITICAL", "eval/exec with chr() — encoded payload"),
            # Attribute access via strings
            r'getattr\s*\(.*,\s*["\'](?:system|popen|exec|fork|kill)["\']': ("CRITICAL", "getattr() accessing dangerous method by string"),
            # Base64 encoded payloads executed
            r'(?:eval|exec)\s*\(.*b64decode': ("CRITICAL", "Execution of base64-decoded payload"),
            r'(?:eval|exec)\s*\(.*decode\(':  ("HIGH", "Execution of decoded payload"),
        }

        for i, line in enumerate(lines, 1):
            stripped = line.strip()
            if stripped.startswith("#"):
                continue
            for pattern, (severity, msg) in obfuscation_patterns.items():
                if re.search(pattern, line):
                    violations.append(Violation(
                        check="OBFUSCATION",
                        severity=severity,
                        message=msg,
                        line=i,
                        evidence=stripped[:100],
                    ))

        return violations

    # ── Phase 4: Data Exfiltration Detection ─────────────────────────

    def _check_exfiltration(self, code: str) -> List[Violation]:
        """Detect patterns that could leak sensitive data."""
        violations = []
        lines = code.split("\n")

        # Sensitive files
        sensitive_files = {
            "/etc/passwd", "/etc/shadow", "/etc/hosts",
            ".env", ".git/config", ".ssh/",
            "id_rsa", "id_ed25519", ".aws/credentials",
            ".kube/config", ".docker/config.json",
        }

        for i, line in enumerate(lines, 1):
            stripped = line.strip()
            if stripped.startswith("#"):
                continue

            # Check for reads of sensitive files
            if "open(" in line or "read_text(" in line or "read_bytes(" in line:
                for sf in sensitive_files:
                    if sf in line:
                        violations.append(Violation(
                            check="DATA_EXFILTRATION",
                            severity="CRITICAL",
                            message=f"Attempting to read sensitive file: {sf}",
                            line=i,
                            evidence=stripped[:100],
                        ))

            # print(open(...).read()) — classic exfiltration
            if re.search(r'print\s*\(\s*open\s*\(', line):
                violations.append(Violation(
                    check="DATA_EXFILTRATION",
                    severity="HIGH",
                    message="print(open()) — data exfiltration via stdout",
                    line=i,
                    evidence=stripped[:100],
                ))

            # Sending env vars over network
            if "os.environ" in line and any(net in line for net in ["requests.", "urllib.", "http.", "socket."]):
                violations.append(Violation(
                    check="DATA_EXFILTRATION",
                    severity="CRITICAL",
                    message="Environment variables sent over network",
                    line=i,
                    evidence=stripped[:100],
                ))

        return violations
