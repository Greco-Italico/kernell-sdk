"""
Kernell OS SDK — Task Model
════════════════════════════
The atomic unit of work in the agentic operating system.
Every user goal is decomposed into a DAG of Tasks by the Planner agent.
"""
from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional, List


class TaskType(str, Enum):
    """What kind of cognitive work this task requires."""
    PLAN = "plan"          # Decompose a goal into subtasks
    CODE = "code"          # Generate or edit code
    REASON = "reason"      # Complex logic, architecture decisions
    VERIFY = "verify"      # Validate outputs, run tests
    EXECUTE = "execute"    # Run a command in sandbox
    SEARCH = "search"      # RAG lookup or web search
    SUMMARIZE = "summarize"  # Compress information


class Complexity(str, Enum):
    """How hard this task is — drives model selection and cost."""
    LOW = "low"            # Local model, fast, free
    MEDIUM = "medium"      # Local or cheap cloud
    HIGH = "high"          # Cloud model, slower, paid
    CRITICAL = "critical"  # Multi-model consensus required


class TaskStatus(str, Enum):
    """Lifecycle state of a task."""
    PENDING = "pending"
    QUEUED = "queued"
    RUNNING = "running"
    AWAITING_APPROVAL = "awaiting_approval"  # Blocked on Intent Firewall
    DONE = "done"
    FAILED = "failed"
    CANCELLED = "cancelled"


@dataclass
class Task:
    """
    The atomic unit of work in Kernell OS.

    Every user goal is decomposed into a DAG of Tasks.
    Each Task has a cognitive type, complexity estimate,
    assigned agent, economic stake (escrow), and result.
    """
    task_id: str = field(default_factory=lambda: f"task-{uuid.uuid4().hex[:12]}")
    description: str = ""
    task_type: TaskType = TaskType.CODE
    complexity: Complexity = Complexity.MEDIUM
    parent_task_id: Optional[str] = None
    depends_on: List[str] = field(default_factory=list)
    assigned_agent: Optional[str] = None
    assigned_model: Optional[str] = None
    status: TaskStatus = TaskStatus.PENDING
    escrow_id: Optional[str] = None
    budget_kern: float = 0.0
    cost_estimate_usd: float = 0.0
    cost_actual_usd: float = 0.0
    prompt_tokens_used: int = 0
    completion_tokens_used: int = 0
    result: Optional[str] = None
    error: Optional[str] = None
    created_at: float = field(default_factory=time.time)
    started_at: Optional[float] = None
    completed_at: Optional[float] = None
    metadata: dict = field(default_factory=dict)

    @property
    def duration_seconds(self) -> Optional[float]:
        if self.started_at and self.completed_at:
            return self.completed_at - self.started_at
        return None

    @property
    def is_terminal(self) -> bool:
        return self.status in (TaskStatus.DONE, TaskStatus.FAILED, TaskStatus.CANCELLED)

    @property
    def is_ready(self) -> bool:
        """A task is ready when all its dependencies are DONE."""
        # Note: actual dependency resolution happens in ExecutionGraph
        return self.status == TaskStatus.PENDING and not self.depends_on

    def start(self) -> None:
        self.status = TaskStatus.RUNNING
        self.started_at = time.time()

    def complete(self, result: str, cost_usd: float = 0.0,
                 prompt_tokens: int = 0, completion_tokens: int = 0) -> None:
        self.status = TaskStatus.DONE
        self.result = result
        self.cost_actual_usd = cost_usd
        self.prompt_tokens_used = prompt_tokens
        self.completion_tokens_used = completion_tokens
        self.completed_at = time.time()

    def fail(self, error: str) -> None:
        self.status = TaskStatus.FAILED
        self.error = error
        self.completed_at = time.time()

    def to_event(self) -> dict:
        """Serialize for WebSocket broadcast."""
        return {
            "task_id": self.task_id,
            "description": self.description[:120],
            "task_type": self.task_type.value,
            "complexity": self.complexity.value,
            "status": self.status.value,
            "assigned_agent": self.assigned_agent,
            "assigned_model": self.assigned_model,
            "cost_usd": round(self.cost_actual_usd, 6),
            "budget_kern": self.budget_kern,
            "duration_s": self.duration_seconds,
        }
