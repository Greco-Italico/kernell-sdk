"""
Kernell OS SDK — Closed Learning Loop (Portable)
══════════════════════════════════════════════════
SDK-portable version of the Closed Learning Loop engine.
Designed to work standalone (filesystem) or with Redis.

Unlike the core/learning version (which is Kernell OS-specific),
this module can be used by any project that installs kernell-os SDK.

Usage:
    from kernell_sdk.learning.loop import LearningLoop, TaskTrace

    loop = LearningLoop(skills_dir="./my_skills")

    trace = TaskTrace(
        task_id="task_001",
        description="Integrate payment API with Stripe",
        steps=[
            {"tool": "read_file", "input": "stripe.py", "success": True},
            {"tool": "write_file", "input": "payment_handler.py", "success": True},
            {"tool": "run_tests", "input": "test_payments.py", "success": True},
            {"tool": "deploy", "input": "staging", "success": True},
            {"tool": "verify", "input": "health_check", "success": True},
        ],
        outcome="success",
    )
    result = loop.process_trace(trace)
    # → {"learned": True, "skill_id": "learned_integrate_payment_api_...", ...}
"""
import hashlib
import json
import logging
import re
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional

logger = logging.getLogger("kernell.sdk.learning")

LEARNED_PREFIX = "learned_"
MIN_STEPS = 5

# ── Pattern categories ──────────────────────────────────────

CATEGORIES = {
    "api_integration": ["fetch", "api", "endpoint", "request", "response", "rest", "post", "get"],
    "file_operations": ["create", "write", "read", "edit", "modify", "file", "directory"],
    "debugging": ["fix", "bug", "error", "debug", "trace", "exception", "crash"],
    "refactoring": ["refactor", "clean", "optimize", "improve", "restructure"],
    "testing": ["test", "spec", "assert", "coverage", "verify", "validate"],
    "deployment": ["deploy", "build", "compile", "bundle", "serve", "docker"],
    "security": ["auth", "token", "encrypt", "permission", "vault", "key"],
    "database": ["redis", "sql", "query", "migrate", "schema", "index"],
    "frontend": ["component", "render", "style", "css", "jsx", "react", "dom"],
    "infrastructure": ["config", "env", "setup", "install", "service", "systemd"],
}


# ═══════════════════════════════════════════════════════════
# TaskTrace
# ═══════════════════════════════════════════════════════════

class TaskTrace:
    """A recorded trace of a completed task."""

    def __init__(self, task_id: str, description: str, steps: List[Dict],
                 outcome: str = "success", total_time_ms: int = 0,
                 model_used: str = "unknown", tags: Optional[List[str]] = None):
        self.task_id = task_id
        self.description = description
        self.steps = steps
        self.outcome = outcome
        self.total_time_ms = total_time_ms
        self.model_used = model_used
        self.tags = tags or []
        self.timestamp = time.time()

    @property
    def step_count(self) -> int:
        return len(self.steps)

    @property
    def tools_used(self) -> List[str]:
        return list(dict.fromkeys(s.get("tool", "unknown") for s in self.steps))

    @property
    def success_rate(self) -> float:
        if not self.steps:
            return 0.0
        return sum(1 for s in self.steps if s.get("success", True)) / len(self.steps)

    def to_dict(self) -> dict:
        return {
            "task_id": self.task_id, "description": self.description,
            "steps": self.steps, "outcome": self.outcome,
            "step_count": self.step_count, "tools_used": self.tools_used,
            "success_rate": self.success_rate, "total_time_ms": self.total_time_ms,
            "model_used": self.model_used, "tags": self.tags,
            "timestamp": self.timestamp,
        }


# ═══════════════════════════════════════════════════════════
# LearningLoop
# ═══════════════════════════════════════════════════════════

class LearningLoop:
    """
    Closed Learning Loop — SDK-portable version.

    After complex tasks (5+ tool calls), analyzes the execution,
    extracts reusable patterns, and generates Markdown skill files.

    Args:
        skills_dir: Path to skills directory (default: ./.agent/skills/)
        redis_client: Optional Redis connection for indexing/stats
        min_steps: Minimum steps to trigger learning (default: 5)
    """

    def __init__(self, skills_dir: str = "./.agent/skills",
                 redis_client=None, min_steps: int = MIN_STEPS):
        self.skills_dir = Path(skills_dir).resolve()
        self.redis = redis_client
        self.min_steps = min_steps

    def process_trace(self, trace: TaskTrace) -> Dict:
        """Process a completed task trace. Returns learning result."""
        if trace.step_count < self.min_steps:
            return {"learned": False, "reason": f"Too few steps ({trace.step_count} < {self.min_steps})"}
        if trace.outcome != "success":
            self._persist_trace(trace)
            return {"learned": False, "reason": f"Outcome: {trace.outcome}"}
        if trace.success_rate < 0.6:
            self._persist_trace(trace)
            return {"learned": False, "reason": f"Low success: {trace.success_rate:.0%}"}

        workflow = self._extract_workflow(trace)
        categories = workflow["categories"]
        skill_name = self._generate_name(trace.description, categories)

        # Check for existing similar skill
        existing = self.find_skills(trace.description)
        if existing and existing[0].get("score", 0) > 8:
            self._refine_skill(existing[0], trace)
            self._persist_trace(trace)
            return {"learned": True, "skill_id": existing[0]["skill_id"],
                    "action": "refined", "reason": "Refined existing skill"}

        # Generate new skill
        content = self._generate_skill(trace, workflow)
        skill_dir = self.skills_dir / skill_name
        skill_dir.mkdir(parents=True, exist_ok=True)
        (skill_dir / "SKILL.md").write_text(content, encoding="utf-8")

        self._index_skill(skill_name, trace, workflow)
        self._persist_trace(trace)
        self._update_stats("skills_created")

        logger.info("🧠 Learned skill '%s' from '%s'", skill_name, trace.task_id)
        return {"learned": True, "skill_id": skill_name, "action": "created",
                "categories": categories, "path": str(skill_dir / "SKILL.md")}

    def find_skills(self, description: str) -> List[Dict]:
        """Find learned skills relevant to a task description."""
        desc_lower = description.lower()
        desc_words = set(desc_lower.split())
        matches = []

        for skill_dir in self.skills_dir.iterdir():
            if not skill_dir.is_dir() or not skill_dir.name.startswith(LEARNED_PREFIX):
                continue
            skill_md = skill_dir / "SKILL.md"
            if not skill_md.exists():
                continue
            try:
                content = skill_md.read_text(encoding="utf-8").lower()
                cat_match = re.search(r'categories:\s*\[([^\]]+)\]', content)
                categories = [c.strip() for c in cat_match.group(1).split(",")] if cat_match else []
                score = sum(1 for w in desc_words if len(w) > 3 and w in content)
                score += sum(3 for c in categories if any(w in desc_lower for w in c.split("_")))
                if score > 2:
                    matches.append({"skill_id": skill_dir.name, "categories": categories,
                                    "score": score, "path": str(skill_md)})
            except Exception:
                continue

        matches.sort(key=lambda m: -m["score"])
        return matches[:5]

    def get_stats(self) -> Dict:
        """Get learning statistics."""
        stats = {"skills_created": 0, "skills_refined": 0, "traces_processed": 0}
        if self.redis:
            raw = self.redis.hgetall("kernell:learning:stats") or {}
            for k, v in raw.items():
                key = k if isinstance(k, str) else k.decode()
                stats[key] = int(v if isinstance(v, str) else v.decode())
        stats["learned_skills_on_disk"] = sum(
            1 for d in self.skills_dir.iterdir()
            if d.is_dir() and d.name.startswith(LEARNED_PREFIX))
        return stats

    def list_learned(self) -> List[Dict]:
        """List all auto-generated skills."""
        skills = []
        for d in sorted(self.skills_dir.iterdir()):
            if not d.is_dir() or not d.name.startswith(LEARNED_PREFIX):
                continue
            md = d / "SKILL.md"
            if not md.exists():
                continue
            try:
                content = md.read_text(encoding="utf-8")
                meta = {}
                fm = re.search(r'^---\n(.*?)\n---', content, re.DOTALL)
                if fm:
                    for line in fm.group(1).split("\n"):
                        if ": " in line:
                            k, v = line.split(": ", 1)
                            meta[k.strip()] = v.strip()
                meta["skill_id"] = d.name
                meta["path"] = str(md)
                skills.append(meta)
            except Exception:
                continue
        return skills

    # ── Private ──────────────────────────────────────────

    def _categorize(self, description: str, tools: List[str]) -> List[str]:
        text = f"{description} {' '.join(tools)}".lower()
        matches = []
        for cat, kws in CATEGORIES.items():
            if sum(1 for kw in kws if kw in text) >= 2:
                matches.append(cat)
        return matches or ["general"]

    def _extract_workflow(self, trace: TaskTrace) -> Dict:
        categories = self._categorize(trace.description, trace.tools_used)
        critical = [{"order": i+1, "tool": s.get("tool", "?"),
                      "purpose": s.get("input_summary", s.get("input", ""))[:100],
                      "output": s.get("output_summary", "")[:200]}
                     for i, s in enumerate(trace.steps) if s.get("success", True)]
        tool_counts = {}
        for s in trace.steps:
            t = s.get("tool", "?")
            tool_counts[t] = tool_counts.get(t, 0) + 1

        errors = []
        for i, s in enumerate(trace.steps):
            if not s.get("success", True) and i+1 < len(trace.steps) and trace.steps[i+1].get("success", True):
                errors.append({"failed": s.get("tool"), "recovery": trace.steps[i+1].get("tool"),
                               "action": trace.steps[i+1].get("input_summary", "")[:100]})

        return {"categories": categories, "critical_path": critical,
                "tools_sequence": trace.tools_used,
                "retried": {t: c for t, c in tool_counts.items() if c > 1},
                "error_recoveries": errors, "success_rate": trace.success_rate}

    def _generate_name(self, desc: str, cats: List[str]) -> str:
        words = re.sub(r'[^a-z0-9\s]', '', desc.lower()).split()
        stop = {"the", "a", "an", "to", "for", "in", "on", "at", "is", "it", "of", "and", "or", "with"}
        meaningful = [w for w in words if w not in stop and len(w) > 2][:4]
        base = "_".join(meaningful) if meaningful else (cats[0] if cats else "general")
        h = hashlib.sha256(desc.encode()).hexdigest()[:6]
        return f"{LEARNED_PREFIX}{base}_{h}"

    def _generate_skill(self, trace: TaskTrace, wf: Dict) -> str:
        cats = wf["categories"]
        name = " ".join(c.replace("_", " ").title() for c in cats[:2]) + " Workflow"
        date = datetime.now().strftime("%Y-%m-%d")

        when = "\n".join(f"- Tasks involving **{c.replace('_', ' ')}**" for c in cats)
        when += "\n" + "\n".join(f"- Uses `{t}`" for t in wf["tools_sequence"][:5])

        path_md = "\n".join(
            f"{s['order']}. **`{s['tool']}`** — {s['purpose']}"
            + (f"\n   - _{s['output'][:80]}_" if s.get("output") else "")
            for s in wf["critical_path"][:10]) or "_(empty)_"

        tools_md = "\n".join(f"- `{t}`" for t in wf["tools_sequence"])

        err_md = ""
        if wf["error_recoveries"]:
            err_md = "### Error Recovery\n" + "\n".join(
                f"- `{e['failed']}` fails → `{e['recovery']}`: _{e['action']}_"
                for e in wf["error_recoveries"])

        obs = [f"- {trace.total_time_ms}ms, {trace.step_count} steps, {wf['success_rate']:.0%} success",
               f"- Model: `{trace.model_used}`"]
        for t, c in wf.get("retried", {}).items():
            obs.append(f"- `{t}` ×{c}")

        return f"""---
name: {name}
description: Learned workflow for {', '.join(cats)} tasks
version: 1.0
created: {date}
source: closed_learning_loop
learned_from: {trace.task_id}
categories: [{', '.join(cats)}]
confidence: {min(95, int(wf['success_rate'] * 100))}
uses: 0
---

# 🧠 {name}

> Auto-generated by Kernell OS SDK Closed Learning Loop.
> From task `{trace.task_id}` on {date}.

## When To Use
{when}

## Workflow ({len(wf['critical_path'])} steps, {wf['success_rate']:.0%} success)
{path_md}

### Tools
{tools_md}

{err_md}

## Observations
{chr(10).join(obs)}

## History
| Date | Change |
|------|--------|
| {date} | Created from `{trace.task_id}` |
"""

    def _refine_skill(self, meta: Dict, trace: TaskTrace):
        p = meta.get("path")
        if not p or not Path(p).exists():
            return
        try:
            content = Path(p).read_text(encoding="utf-8")
            content = re.sub(r'uses: (\d+)', lambda m: f'uses: {int(m.group(1))+1}', content)
            date = datetime.now().strftime("%Y-%m-%d")
            content = content.rstrip() + f"\n| {date} | Refined from `{trace.task_id}` ({trace.success_rate:.0%}) |\n"
            Path(p).write_text(content, encoding="utf-8")
            self._update_stats("skills_refined")
        except Exception as e:
            logger.warning("Refine failed: %s", e)

    def _index_skill(self, skill_id: str, trace: TaskTrace, wf: Dict):
        if not self.redis:
            return
        self.redis.hset("kernell:learning:skills_index", skill_id, json.dumps({
            "skill_id": skill_id, "description": trace.description,
            "categories": wf["categories"], "tools": wf["tools_sequence"],
            "success_rate": wf["success_rate"], "created_at": time.time(),
            "path": str(self.skills_dir / skill_id / "SKILL.md"),
        }))

    def _persist_trace(self, trace: TaskTrace):
        if not self.redis:
            return
        self.redis.lpush("kernell:learning:task_traces", json.dumps(trace.to_dict()))
        self.redis.ltrim("kernell:learning:task_traces", 0, 199)
        self._update_stats("traces_processed")

    def _update_stats(self, key: str, delta: int = 1):
        if self.redis:
            self.redis.hincrby("kernell:learning:stats", key, delta)
