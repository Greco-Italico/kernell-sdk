"""
Kernell OS — Preview Engine
═════════════════════════════
Renders execution results for developer review in the terminal.
Provides rich visual diffs, agent reputation info, and accept/reject flow.

This is what replaces Cursor's Cmd+K inline diff experience,
but with cryptographic proof and agent reputation transparency.
"""
import logging
from typing import List

logger = logging.getLogger("kernell.devlayer.preview")


class PreviewEngine:
    """
    Renders execution receipts with visual diffs for developer review.
    
    Unlike Cursor (which shows you a suggestion from an anonymous model),
    Kernell shows you:
    - WHO executed it (agent ID + reputation score)
    - HOW it was verified (canary result + output hash)
    - WHAT changed (full diff with color coding)
    - WHY you can trust it (cryptographic receipt + consensus status)
    """

    HEADER = """
╔══════════════════════════════════════════════════════════════╗
║                 ⬡  KERNELL EXECUTION PREVIEW                ║
╚══════════════════════════════════════════════════════════════╝"""

    SEPARATOR = "─" * 62

    @staticmethod
    def render_receipt(receipt, task_description: str = "") -> str:
        """Render a complete execution receipt for terminal display."""
        lines = [PreviewEngine.HEADER]

        # Task info
        if task_description:
            lines.append(f"\n  📋 Task: {task_description[:80]}")

        lines.append(f"\n  {PreviewEngine.SEPARATOR}")

        # Agent info
        rep_bar = PreviewEngine._reputation_bar(receipt.agent_reputation)
        canary = "\033[32m✅ PASSED\033[0m" if receipt.canary_passed else "\033[31m❌ FAILED\033[0m"

        lines.append(f"  🤖 Agent:      {receipt.agent_id}")
        lines.append(f"  ⭐ Reputation: {rep_bar} ({receipt.agent_reputation:.0f}/100)")
        lines.append(f"  ⏱️  Execution:  {receipt.execution_time_ms}ms")
        lines.append(f"  🔬 Canary:     {canary}")
        lines.append(f"  🔐 Receipt:    {receipt.receipt_id}")
        lines.append(f"  #️⃣  Hash:       {receipt.output_hash[:16]}...")

        lines.append(f"\n  {PreviewEngine.SEPARATOR}")
        lines.append(f"  📂 Changes ({len(receipt.diffs)} file(s)):")
        lines.append(f"  {PreviewEngine.SEPARATOR}")

        # Diffs
        for diff in receipt.diffs:
            lines.append(diff.render_diff())

        lines.append(f"\n  {PreviewEngine.SEPARATOR}")

        # Action prompt
        lines.append("")
        lines.append("  \033[1m[A]\033[0m Accept  │  \033[1m[R]\033[0m Reject  │  \033[1m[D]\033[0m Details  │  \033[1m[S]\033[0m Skip")
        lines.append("")

        return "\n".join(lines)

    @staticmethod
    def render_task_list(tasks) -> str:
        """Render a list of pending tasks for review."""
        if not tasks:
            return "\n  📭 No pending reviews.\n"

        lines = ["\n  ⬡ Pending Reviews:"]
        lines.append(f"  {'─' * 50}")

        for i, task in enumerate(tasks):
            status_icon = {
                "verified": "🟢",
                "submitted": "🟡",
                "executing": "🔵",
                "failed": "🔴",
            }.get(task.status.value, "⚪")

            desc = task.description[:50]
            receipt_count = len(task.receipts)

            lines.append(
                f"  {status_icon} [{i+1}] {desc}"
                f" ({receipt_count} receipt{'s' if receipt_count != 1 else ''})"
            )

        lines.append(f"  {'─' * 50}")
        lines.append(f"  Total: {len(tasks)} task(s) awaiting review\n")
        return "\n".join(lines)

    @staticmethod
    def render_index_summary(graph_summary: dict) -> str:
        """Render codebase index summary."""
        lines = ["\n  ⬡ Codebase Index:"]
        lines.append(f"  {'─' * 50}")
        lines.append(f"  📁 Root:    {graph_summary['root']}")
        lines.append(f"  📄 Files:   {graph_summary['total_files']}")
        lines.append(f"  🔤 Tokens:  ~{graph_summary['estimated_tokens']:,}")

        langs = graph_summary.get("languages", {})
        if langs:
            top_langs = sorted(langs.items(), key=lambda x: x[1], reverse=True)[:5]
            lang_str = ", ".join(f"{l} ({c})" for l, c in top_langs)
            lines.append(f"  🌐 Languages: {lang_str}")

        lines.append(f"  {'─' * 50}\n")
        return "\n".join(lines)

    @staticmethod
    def _reputation_bar(score: float, width: int = 20) -> str:
        """Visual reputation bar."""
        filled = int(score / 100 * width)
        empty = width - filled

        if score >= 80:
            color = "\033[32m"  # green
        elif score >= 50:
            color = "\033[33m"  # yellow
        else:
            color = "\033[31m"  # red

        return f"{color}{'█' * filled}{'░' * empty}\033[0m"
