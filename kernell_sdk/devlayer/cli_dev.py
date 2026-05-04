"""
Kernell OS — DevLayer CLI
══════════════════════════
Developer-facing CLI commands for the Kernell distributed coding fabric.

Commands:
  kernell dev index   - Index the current codebase for context routing
  kernell dev ask     - Submit a coding task to the Kernell network
  kernell dev review  - Review pending execution receipts (accept/reject)
  kernell dev status  - Show current task pipeline status
  kernell dev history - Show task execution history

Usage:
  $ kernell dev index
  $ kernell dev ask "refactor the auth module to use JWT tokens"
  $ kernell dev review
"""
import os
import sys
import logging
from pathlib import Path

logger = logging.getLogger("kernell.devlayer.cli")


def _get_project_root() -> str:
    """Find the project root (directory with kernell.yaml or .git)."""
    cwd = Path.cwd()
    for marker in ["kernell.yaml", ".git", "pyproject.toml", "package.json"]:
        if (cwd / marker).exists():
            return str(cwd)
    return str(cwd)


def cmd_dev(args):
    """Router for dev subcommands."""
    sub = getattr(args, "dev_command", None)
    if not sub:
        print("\n  ⬡ Kernell DevLayer — Available Commands:")
        print("  ─────────────────────────────────────────")
        print("  kernell dev index    Index the codebase")
        print("  kernell dev ask      Submit a coding task")
        print("  kernell dev review   Review pending results")
        print("  kernell dev status   Pipeline status")
        print("  kernell dev history  Execution history")
        print()
        return

    handlers = {
        "index": _cmd_index,
        "ask": _cmd_ask,
        "review": _cmd_review,
        "status": _cmd_status,
        "history": _cmd_history,
    }

    handler = handlers.get(sub)
    if handler:
        handler(args)
    else:
        print(f"Unknown dev command: {sub}")


def _cmd_index(args):
    """Index the current codebase."""
    from kernell_sdk.devlayer.context_router import ContextRouter
    from kernell_sdk.devlayer.preview import PreviewEngine

    project_root = _get_project_root()
    router = ContextRouter(project_root)

    print("\n  ⬡ Indexing codebase...")
    graph = router.index(force=getattr(args, "force", False))

    summary = graph.summary()
    print(PreviewEngine.render_index_summary(summary))
    print("  ✅ Index cached at .kernell/index.json")
    print(f"  Ready to route context to {summary['total_files']} files\n")


def _cmd_ask(args):
    """Submit a coding task to the Kernell network."""
    from kernell_sdk.devlayer.context_router import ContextRouter
    from kernell_sdk.devlayer.task_client import TaskClient
    from kernell_sdk.devlayer.preview import PreviewEngine

    description = getattr(args, "description", None)
    if not description:
        print("  ❌ Usage: kernell dev ask \"<task description>\"")
        return

    project_root = _get_project_root()

    # Step 1: Index and select context
    print("\n  ⬡ Analyzing codebase for relevant context...")
    router = ContextRouter(project_root)
    router.index()
    context = router.select_context(description, max_files=10)

    print(f"  📂 Selected {len(context)} relevant file(s):")
    for c in context[:5]:
        score = c.get("relevance_score", 0)
        print(f"     • {c['path']} (relevance: {score:.1f})")
    if len(context) > 5:
        print(f"     ... and {len(context) - 5} more")

    # Step 2: Create and submit task
    print(f"\n  📡 Submitting to Kernell network...")
    client = TaskClient(project_root)
    task = client.create_task(description, context)
    client.submit_task(task)

    # Step 3: Show results
    if task.receipts:
        receipt = task.receipts[0]
        preview = PreviewEngine.render_receipt(receipt, description)
        print(preview)

        # Interactive accept/reject
        try:
            choice = input("  Your choice: ").strip().lower()
            if choice == "a":
                client.accept_receipt(task, receipt)
                print("\n  ✅ Changes accepted and applied to your project.\n")
            elif choice == "r":
                reason = input("  Reason (optional): ").strip()
                client.reject_receipt(task, receipt, reason)
                print("\n  ❌ Changes rejected. Agent penalized.\n")
            else:
                print("\n  ⏭️  Skipped. Use 'kernell dev review' to review later.\n")
        except (EOFError, KeyboardInterrupt):
            print("\n  ⏭️  Skipped.\n")
    else:
        print("  ⚠️  No receipts received. Task may still be executing.")
        print(f"  Track with: kernell dev status --task {task.task_id}\n")


def _cmd_review(args):
    """Review pending execution receipts."""
    from kernell_sdk.devlayer.task_client import TaskClient
    from kernell_sdk.devlayer.preview import PreviewEngine

    project_root = _get_project_root()
    client = TaskClient(project_root)

    pending = client.get_pending_reviews()
    print(PreviewEngine.render_task_list(pending))

    if not pending:
        return

    for task in pending:
        for receipt in task.receipts:
            if receipt.status != "pending_review":
                continue

            preview = PreviewEngine.render_receipt(receipt, task.description)
            print(preview)

            try:
                choice = input("  Your choice: ").strip().lower()
                if choice == "a":
                    client.accept_receipt(task, receipt)
                    print("  ✅ Accepted.\n")
                elif choice == "r":
                    reason = input("  Reason: ").strip()
                    client.reject_receipt(task, receipt, reason)
                    print("  ❌ Rejected.\n")
                else:
                    print("  ⏭️  Skipped.\n")
            except (EOFError, KeyboardInterrupt):
                print("\n  Done reviewing.\n")
                return


def _cmd_status(args):
    """Show current task pipeline status."""
    from kernell_sdk.devlayer.task_client import TaskClient

    project_root = _get_project_root()
    client = TaskClient(project_root)

    total = len(client.tasks)
    if total == 0:
        print("\n  📭 No active tasks. Use 'kernell dev ask' to submit one.\n")
        return

    print(f"\n  ⬡ Task Pipeline ({total} tasks):")
    print(f"  {'─' * 50}")

    for task in client.tasks.values():
        status_icons = {
            "draft": "⚪", "submitted": "🟡", "assigned": "🔵",
            "executing": "🔄", "verified": "🟢", "finalized": "✅",
            "failed": "🔴", "rejected": "❌",
        }
        icon = status_icons.get(task.status.value, "❓")
        print(f"  {icon} {task.task_id[:20]}  {task.status.value:12}  {task.description[:40]}")

    print(f"  {'─' * 50}\n")


def _cmd_history(args):
    """Show task execution history from persistent log."""
    import json

    project_root = _get_project_root()
    history_path = Path(project_root) / ".kernell" / "task_history.jsonl"

    if not history_path.exists():
        print("\n  📭 No task history yet.\n")
        return

    print("\n  ⬡ Task History:")
    print(f"  {'─' * 60}")

    lines = history_path.read_text().strip().split("\n")
    # Show last 20 entries
    for line in lines[-20:]:
        try:
            record = json.loads(line)
            status = record.get("status", "?")
            task_id = record.get("task_id", "?")[:20]
            desc = record.get("description", "")[:35]
            agent = record.get("agent", "?")

            icon = {"finalized": "✅", "rejected": "❌", "verified": "🟢"}.get(status, "⚪")
            print(f"  {icon} {task_id}  {status:12}  {desc}")
        except json.JSONDecodeError:
            continue

    print(f"  {'─' * 60}")
    print(f"  Total entries: {len(lines)}\n")
