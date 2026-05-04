"""
Kernell OS SDK — Agent Roles (Cognitive Identity)
══════════════════════════════════════════════════
An agent is NOT defined by its model. It is defined by its ROLE.
The model is just the engine; the role is the job description.
"""
from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass, field
from decimal import Decimal
from enum import Enum
from typing import Optional, List

from .task import Task, TaskType, Complexity

logger = logging.getLogger("kernell.cognitive.agent")


class AgentRole(str, Enum):
    """Cognitive role — determines behavior, not model selection."""
    PLANNER = "planner"      # Decomposes goals into task DAGs
    REASONER = "reasoner"    # Solves complex logic, architecture
    CODER = "coder"          # Generates and edits code
    VERIFIER = "verifier"    # Validates outputs, runs tests
    EXECUTOR = "executor"    # Runs commands in sandbox
    CRITIC = "critic"        # Identifies flaws in proposals (debate protocol)


# Which task types each role can handle
ROLE_CAPABILITIES: dict[AgentRole, set[TaskType]] = {
    AgentRole.PLANNER:  {TaskType.PLAN, TaskType.SUMMARIZE},
    AgentRole.REASONER: {TaskType.REASON, TaskType.PLAN},
    AgentRole.CODER:    {TaskType.CODE, TaskType.SEARCH},
    AgentRole.VERIFIER: {TaskType.VERIFY, TaskType.REASON},
    AgentRole.EXECUTOR: {TaskType.EXECUTE},
    AgentRole.CRITIC:   {TaskType.VERIFY, TaskType.REASON},
}


@dataclass
class CognitiveAgent:
    """
    A cognitive agent in the Kernell OS swarm.

    Each agent has:
    - A role (what it does)
    - A model binding (which LLM powers it)
    - A KERN budget (how much it can spend/earn)
    - A task queue (what it's working on)
    """
    agent_id: str = field(default_factory=lambda: f"agent-{uuid.uuid4().hex[:8]}")
    name: str = ""
    role: AgentRole = AgentRole.CODER
    model_id: str = ""             # Reference to a model in kernell.yaml
    budget_kern: Decimal = Decimal("10.0")
    spent_kern: Decimal = Decimal("0")
    earned_kern: Decimal = Decimal("0")
    active_task: Optional[str] = None
    completed_tasks: int = 0
    failed_tasks: int = 0
    total_tokens_used: int = 0
    total_cost_usd: float = 0.0

    @property
    def available_kern(self) -> Decimal:
        return self.budget_kern - self.spent_kern + self.earned_kern

    def can_handle(self, task: Task) -> bool:
        """Check if this agent's role supports the task type."""
        capabilities = ROLE_CAPABILITIES.get(self.role, set())
        return task.task_type in capabilities

    def assign(self, task: Task) -> None:
        """Assign a task to this agent."""
        if not self.can_handle(task):
            raise ValueError(
                f"Agent {self.agent_id} (role={self.role.value}) "
                f"cannot handle task type {task.task_type.value}"
            )
        self.active_task = task.task_id
        task.assigned_agent = self.agent_id
        task.assigned_model = self.model_id
        logger.info(
            f"Agent {self.name or self.agent_id} ({self.role.value}) "
            f"assigned task {task.task_id} using model {self.model_id}"
        )

    def record_completion(self, cost_usd: float, tokens: int, kern_earned: Decimal = Decimal("0")) -> None:
        """Record metrics after completing a task."""
        self.completed_tasks += 1
        self.total_cost_usd += cost_usd
        self.total_tokens_used += tokens
        self.earned_kern += kern_earned
        self.active_task = None

    def record_failure(self) -> None:
        self.failed_tasks += 1
        self.active_task = None

    def to_event(self) -> dict:
        """Serialize for WebSocket broadcast."""
        return {
            "agent_id": self.agent_id,
            "name": self.name or self.agent_id,
            "role": self.role.value,
            "model": self.model_id,
            "state": "busy" if self.active_task else "idle",
            "active_task": self.active_task,
            "balance_kern": float(self.available_kern),
            "completed": self.completed_tasks,
            "failed": self.failed_tasks,
            "cost_usd": round(self.total_cost_usd, 6),
        }
