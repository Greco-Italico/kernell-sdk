"""
Kernell OS SDK — Tool Result Persister
═══════════════════════════════════════
When a tool output exceeds a threshold, it's saved to disk and replaced
with a compact preview. Saves 40-60% tokens per turn.
Migrated from Kernell OS core/tool_result_persister.py.
"""
import hashlib, time, logging
from pathlib import Path
from typing import Optional
from .token_estimator import estimate_tokens

logger = logging.getLogger("kernell.persister")

PREVIEW_SIZE = 2000
DEFAULT_THRESHOLD = 50_000  # chars

class ToolResultPersister:
    def __init__(self, agent_name: str, persist_dir: str = "~/.kernell/tool_results"):
        self.agent_name = agent_name
        self.base_dir = Path(persist_dir).expanduser() / agent_name
        self.base_dir.mkdir(parents=True, exist_ok=True)
        self.stats = {"persisted": 0, "tokens_saved": 0}

    def maybe_persist(self, content: str, tool_name: str = "unknown",
                      tool_id: str = "", threshold: int = DEFAULT_THRESHOLD) -> str:
        if len(content) <= threshold:
            return content
        # Save full output to disk
        content_hash = hashlib.sha256(content.encode()).hexdigest()[:12]
        filename = f"{tool_name}_{content_hash}_{int(time.time())}.txt"
        path = self.base_dir / filename
        path.write_text(content, encoding="utf-8")

        tokens_before = estimate_tokens(content)
        preview = content[:PREVIEW_SIZE]
        tokens_after = estimate_tokens(preview)
        saved = tokens_before - tokens_after

        self.stats["persisted"] += 1
        self.stats["tokens_saved"] += saved
        logger.info(f"Persisted {tool_name} output ({len(content)} chars → {PREVIEW_SIZE} preview). Saved ~{saved} tokens.")

        return (
            f"[Tool output persisted to disk — {len(content)} chars]\n"
            f"[Preview ({PREVIEW_SIZE} chars):]\n{preview}\n"
            f"[Full output: {path}]"
        )
