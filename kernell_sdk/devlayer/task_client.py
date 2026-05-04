"""
Kernell OS — Task Client
═════════════════════════
Submits coding tasks to the Kernell distributed execution fabric
and tracks their lifecycle from PENDING → COMMITTED → VERIFIED → FINALIZED.

This is the bridge between the developer's intent and the network's
economic consensus. Every task submission:
  1. Gets structured context from ContextRouter
  2. Gets submitted to the marketplace with escrow
  3. Gets assigned to agents via the deterministic scheduler
  4. Returns a verified ExecutionReceipt with diffs
"""
import uuid
import time
import json
import hashlib
import logging
from pathlib import Path
from dataclasses import dataclass, field, asdict
from typing import List, Dict, Optional, Any
from enum import Enum

logger = logging.getLogger("kernell.devlayer.task_client")


class TaskStatus(Enum):
    DRAFT = "draft"
    SUBMITTED = "submitted"
    ASSIGNED = "assigned"
    EXECUTING = "executing"
    VERIFIED = "verified"
    FINALIZED = "finalized"
    FAILED = "failed"
    REJECTED = "rejected"


@dataclass
class FileDiff:
    """A single file change produced by an agent."""
    path: str
    action: str  # "create", "modify", "delete"
    original_content: Optional[str] = None
    new_content: Optional[str] = None
    diff_lines: List[str] = field(default_factory=list)

    def render_diff(self) -> str:
        """Render a human-readable diff."""
        lines = []
        lines.append(f"{'━' * 60}")
        action_icons = {"create": "🆕", "modify": "📝", "delete": "🗑️"}
        lines.append(f"  {action_icons.get(self.action, '❓')} {self.action.upper()}: {self.path}")
        lines.append(f"{'━' * 60}")

        if self.diff_lines:
            for dl in self.diff_lines:
                if dl.startswith("+"):
                    lines.append(f"  \033[32m{dl}\033[0m")  # green
                elif dl.startswith("-"):
                    lines.append(f"  \033[31m{dl}\033[0m")  # red
                else:
                    lines.append(f"  {dl}")
        elif self.new_content and self.action == "create":
            for i, line in enumerate(self.new_content.split("\n")[:20]):
                lines.append(f"  \033[32m+ {line}\033[0m")
            total = len(self.new_content.split("\n"))
            if total > 20:
                lines.append(f"  ... ({total - 20} more lines)")

        return "\n".join(lines)


@dataclass
class ExecutionReceipt:
    """Cryptographically verifiable proof of agent execution."""
    receipt_id: str
    task_id: str
    agent_id: str
    agent_reputation: float
    execution_time_ms: int
    diffs: List[FileDiff]
    output_hash: str
    canary_passed: bool
    signature: str
    timestamp: float
    status: str = "pending_review"

    def summary(self) -> str:
        """One-line summary for the developer."""
        files_changed = len(self.diffs)
        rep = f"{self.agent_reputation:.0f}"
        canary = "✅" if self.canary_passed else "❌"
        return (
            f"Agent {self.agent_id} | "
            f"Rep: {rep} | "
            f"{files_changed} file(s) | "
            f"{self.execution_time_ms}ms | "
            f"Canary: {canary}"
        )


@dataclass
class DevTask:
    """A developer's coding task submitted to the network."""
    task_id: str
    description: str
    context_files: List[Dict]
    status: TaskStatus = TaskStatus.DRAFT
    created_at: float = field(default_factory=time.time)
    receipts: List[ExecutionReceipt] = field(default_factory=list)
    escrow_id: Optional[str] = None
    assigned_agent: Optional[str] = None
    value_kern: float = 10.0

    def to_submission(self) -> dict:
        """Package task for network submission."""
        return {
            "task_id": self.task_id,
            "description": self.description,
            "context": [
                {"path": c["path"], "language": c.get("language", "unknown")}
                for c in self.context_files
            ],
            "context_hash": hashlib.sha256(
                json.dumps(self.context_files, sort_keys=True).encode()
            ).hexdigest(),
            "value": self.value_kern,
            "created_at": self.created_at,
            "required_mode": "isolated",
        }


class TaskClient:
    """
    Manages the lifecycle of developer tasks on the Kernell network.
    
    This is what makes Kernell fundamentally different from Cursor:
    - Tasks go through economic consensus, not just API calls
    - Results come back with cryptographic receipts
    - Multiple agents can compete on the same task
    - The developer sees reputation + proof before accepting
    """

    def __init__(self, project_root: str, network_url: str = "localhost:50051"):
        self.project_root = Path(project_root).resolve()
        self.network_url = network_url
        self.tasks: Dict[str, DevTask] = {}
        self._history_path = self.project_root / ".kernell" / "task_history.jsonl"

    def create_task(
        self,
        description: str,
        context_files: List[Dict],
        value_kern: float = 10.0,
    ) -> DevTask:
        """Create a new coding task from developer intent + context."""
        task_id = f"devtask_{uuid.uuid4().hex[:12]}"

        task = DevTask(
            task_id=task_id,
            description=description,
            context_files=context_files,
            value_kern=value_kern,
        )
        self.tasks[task_id] = task

        logger.info(f"Task created: {task_id} ({len(context_files)} context files)")
        return task

    def submit_task(self, task: DevTask) -> str:
        """Submit task to the Kernell network for execution."""
        task.status = TaskStatus.SUBMITTED
        submission = task.to_submission()

        # In production: this calls the gRPC bridge to submit to the P2P network
        # For now: simulate the marketplace flow
        logger.info(f"Submitting task {task.task_id} to network (value: {task.value_kern} KERN)")

        # Simulate escrow lock
        task.escrow_id = f"escrow_{uuid.uuid4().hex[:8]}"
        logger.info(f"Escrow locked: {task.escrow_id}")

        # Simulate assignment (in production: deterministic scheduler via P2P consensus)
        task.status = TaskStatus.ASSIGNED
        task.assigned_agent = f"agent_{uuid.uuid4().hex[:8]}"
        logger.info(f"Assigned to: {task.assigned_agent}")

        # Simulate execution
        task.status = TaskStatus.EXECUTING

        # Simulate receipt (in production: comes back via GossipSub EVENT_CONFIRM)
        receipt = self._simulate_execution(task)
        task.receipts.append(receipt)
        task.status = TaskStatus.VERIFIED

        self._persist_task(task)
        return task.task_id

    def get_task(self, task_id: str) -> Optional[DevTask]:
        """Get task by ID."""
        return self.tasks.get(task_id)

    def get_pending_reviews(self) -> List[DevTask]:
        """Get all tasks waiting for developer review."""
        return [
            t for t in self.tasks.values()
            if t.status == TaskStatus.VERIFIED and t.receipts
        ]

    def accept_receipt(self, task: DevTask, receipt: ExecutionReceipt) -> bool:
        """Developer accepts the agent's work — triggers payment release."""
        if receipt.status != "pending_review":
            logger.warning(f"Receipt {receipt.receipt_id} not in pending_review state")
            return False

        receipt.status = "accepted"
        task.status = TaskStatus.FINALIZED

        # In production: this triggers escrow release via the EscrowEngine
        logger.info(
            f"✅ Receipt ACCEPTED for task {task.task_id}. "
            f"Releasing escrow {task.escrow_id} → {task.assigned_agent}"
        )

        # Apply the diffs to local filesystem
        applied = self._apply_diffs(receipt.diffs)
        logger.info(f"Applied {applied} file change(s) to local project")

        self._persist_task(task)
        return True

    def reject_receipt(self, task: DevTask, receipt: ExecutionReceipt, reason: str = "") -> bool:
        """Developer rejects the agent's work — triggers reputation penalty."""
        receipt.status = "rejected"
        task.status = TaskStatus.REJECTED

        # In production: this triggers dispute resolution
        logger.info(
            f"❌ Receipt REJECTED for task {task.task_id}. "
            f"Reason: {reason or 'not specified'}. "
            f"Escrow {task.escrow_id} returned. Agent penalized."
        )

        self._persist_task(task)
        return True

    def _simulate_execution(self, task: DevTask) -> ExecutionReceipt:
        """Simulate agent execution for MVP testing."""
        # In production this comes from the P2P network
        diffs = []

        # Generate a plausible diff based on the task
        if task.context_files:
            target = task.context_files[0]
            diffs.append(FileDiff(
                path=target["path"],
                action="modify",
                diff_lines=[
                    f"  # Context: {target['path']}",
                    f"- # TODO: {task.description[:50]}",
                    f"+ # DONE: {task.description[:50]}",
                    f"+ # Implemented by Kernell Agent",
                ],
            ))

        output_hash = hashlib.sha256(
            json.dumps(task.to_submission(), sort_keys=True).encode()
        ).hexdigest()

        return ExecutionReceipt(
            receipt_id=f"receipt_{uuid.uuid4().hex[:12]}",
            task_id=task.task_id,
            agent_id=task.assigned_agent or "unknown",
            agent_reputation=85.0 + (hash(task.task_id) % 15),
            execution_time_ms=int(120 + (hash(task.task_id) % 500)),
            diffs=diffs,
            output_hash=output_hash,
            canary_passed=True,
            signature="simulated_ed25519_signature",
            timestamp=time.time(),
        )

    def _apply_diffs(self, diffs: List[FileDiff]) -> int:
        """Apply file diffs to the local project."""
        applied = 0
        for diff in diffs:
            full_path = self.project_root / diff.path
            try:
                if diff.action == "create" and diff.new_content:
                    full_path.parent.mkdir(parents=True, exist_ok=True)
                    full_path.write_text(diff.new_content)
                    applied += 1
                elif diff.action == "modify" and diff.new_content:
                    full_path.write_text(diff.new_content)
                    applied += 1
                elif diff.action == "delete":
                    if full_path.exists():
                        full_path.unlink()
                        applied += 1
            except Exception as e:
                logger.error(f"Failed to apply diff to {diff.path}: {e}")
        return applied

    def _persist_task(self, task: DevTask):
        """Append task state to history log."""
        try:
            self._history_path.parent.mkdir(parents=True, exist_ok=True)
            with open(self._history_path, "a") as f:
                record = {
                    "task_id": task.task_id,
                    "status": task.status.value,
                    "description": task.description[:100],
                    "agent": task.assigned_agent,
                    "timestamp": time.time(),
                }
                f.write(json.dumps(record) + "\n")
        except Exception as e:
            logger.warning(f"Failed to persist task: {e}")
