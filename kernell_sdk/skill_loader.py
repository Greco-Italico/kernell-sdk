"""
Kernell OS SDK — Skill Loader (SKILL.md Frontmatter Parser)
════════════════════════════════════════════════════════════
Loads skill definitions from SKILL.md files with YAML frontmatter.
Compatible with the Kernell OS skill format.
"""
import re, logging
from pathlib import Path
from dataclasses import dataclass, field
from typing import List, Optional, Dict, Any

logger = logging.getLogger("kernell.skills")

try:
    import yaml
    HAS_YAML = True
except ImportError:
    HAS_YAML = False

@dataclass
class SkillConfig:
    name: str
    description: str = ""
    content: str = ""
    source_path: str = ""
    allowed_tools: List[str] = field(default_factory=list)
    paths: List[str] = field(default_factory=list)
    when_to_use: str = ""
    context: str = "inline"
    privilege_level: int = 3

class SkillLoader:
    """Loads SKILL.md files with YAML frontmatter."""

    @staticmethod
    def parse_frontmatter(text: str) -> tuple:
        match = re.match(r'^---\s*\n(.*?)\n---\s*\n(.*)', text, re.DOTALL)
        if not match:
            return {}, text
        if not HAS_YAML:
            return {}, text
        try:
            meta = yaml.safe_load(match.group(1)) or {}
            return meta, match.group(2)
        except Exception:
            return {}, text

    @staticmethod
    def load_skill(path: Path) -> Optional[SkillConfig]:
        if not path.exists():
            return None
        text = path.read_text(encoding="utf-8")
        meta, body = SkillLoader.parse_frontmatter(text)
        return SkillConfig(
            name=meta.get("name", path.parent.name),
            description=meta.get("description", ""),
            content=body.strip(),
            source_path=str(path),
            allowed_tools=meta.get("allowed-tools", []),
            paths=meta.get("paths", []),
            when_to_use=meta.get("when_to_use", ""),
            context=meta.get("context", "inline"),
            privilege_level=meta.get("privilege_level", 3),
        )

    @staticmethod
    def load_skills_dir(directory: Path) -> List[SkillConfig]:
        skills = []
        for skill_md in directory.rglob("SKILL.md"):
            s = SkillLoader.load_skill(skill_md)
            if s:
                skills.append(s)
                logger.info(f"Loaded skill: {s.name} from {skill_md}")
        return skills
