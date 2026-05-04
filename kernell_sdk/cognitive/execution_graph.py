"""
Kernell OS SDK — Execution Graph (DAG Orchestrator)
════════════════════════════════════════════════════
Manages a Directed Acyclic Graph of Tasks with dependencies.
This is the beating heart of the agentic OS: it decides what
runs next, opens escrows, and drives the entire lifecycle.

Visualized in the Dashboard as the "Agent Graph" (Panel A).
"""
from __future__ import annotations

import asyncio
import logging
import time
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Set

from .task import Task, TaskStatus, Complexity
from .agent_role import CognitiveAgent, AgentRole
from .cognitive_router import CognitiveRouter, RouterDecision, ContextState
from .semantic_memory_graph import SemanticMemoryGraph

logger = logging.getLogger("kernell.cognitive.graph")


@dataclass
class GraphResult:
    """Final result of executing an entire graph."""
    success: bool
    total_tasks: int
    completed: int
    failed: int
    total_cost_usd: float
    total_kern_spent: float
    duration_seconds: float
    task_results: Dict[str, str] = field(default_factory=dict)
    errors: Dict[str, str] = field(default_factory=dict)


class ExecutionGraph:
    """
    DAG-based task orchestrator.

    Lifecycle:
      1. Planner decomposes user goal into Tasks (add_task)
      2. Router assigns models to each Task
      3. Tasks execute in dependency order (with Firewall approval)
      4. Escrows open/close as tasks complete
      5. Results aggregate into GraphResult
    """

    def __init__(
        self,
        router: CognitiveRouter,
        agents: Dict[str, CognitiveAgent],
        memory_graph: Optional[SemanticMemoryGraph] = None,
        on_task_event: Optional[Callable] = None,
        on_escrow_event: Optional[Callable] = None,
        on_firewall_event: Optional[Callable] = None,
        on_router_event: Optional[Callable] = None,
    ):
        self._router = router
        self._agents = agents
        self._memory_graph = memory_graph
        self._tasks: Dict[str, Task] = {}
        self._adjacency: Dict[str, List[str]] = defaultdict(list)  # task -> depends_on
        self._reverse: Dict[str, List[str]] = defaultdict(list)    # task -> dependents

        # WebSocket event callbacks (Dashboard Control Plane)
        self._on_task = on_task_event
        self._on_escrow = on_escrow_event
        self._on_firewall = on_firewall_event
        self._on_router = on_router_event

        self._started_at: Optional[float] = None
        self._completed_at: Optional[float] = None

    def add_task(self, task: Task, depends_on: Optional[List[str]] = None) -> None:
        """Add a task to the graph with optional dependencies."""
        self._tasks[task.task_id] = task
        if depends_on:
            task.depends_on = depends_on
            for dep_id in depends_on:
                self._adjacency[task.task_id].append(dep_id)
                self._reverse[dep_id].append(task.task_id)

    def get_ready_tasks(self) -> List[Task]:
        """Return tasks whose dependencies are all DONE and status is PENDING."""
        ready = []
        for task in self._tasks.values():
            if task.status != TaskStatus.PENDING or task.assigned_agent is not None:
                continue
            deps_met = all(
                self._tasks[dep_id].status == TaskStatus.DONE
                for dep_id in task.depends_on
                if dep_id in self._tasks
            )
            if deps_met:
                ready.append(task)
        return ready

    def get_task(self, task_id: str) -> Optional[Task]:
        return self._tasks.get(task_id)

    @property
    def all_tasks(self) -> List[Task]:
        return list(self._tasks.values())

    @property
    def is_complete(self) -> bool:
        return all(t.is_terminal for t in self._tasks.values())

    @property
    def has_failures(self) -> bool:
        return any(t.status == TaskStatus.FAILED for t in self._tasks.values())

    def _find_agent_for_task(self, task: Task) -> Optional[CognitiveAgent]:
        """Find an idle agent whose role can handle this task type."""
        for agent in self._agents.values():
            if agent.can_handle(task) and agent.active_task is None:
                return agent
        return None

    async def execute(
        self,
        task_executor: Callable,
        max_concurrent: int = 5,
    ) -> GraphResult:
        """
        Execute all tasks in dependency order.

        Args:
            task_executor: async function(task, agent, model_decision) -> result_str
            max_concurrent: max parallel tasks
        """
        self._started_at = time.time()
        logger.info(f"ExecutionGraph starting: {len(self._tasks)} tasks")

        semaphore = asyncio.Semaphore(max_concurrent)
        pending_futures: Set[asyncio.Task] = set()

        while not self.is_complete:
            ready = self.get_ready_tasks()

            if not ready and not pending_futures:
                # Deadlock or all failed
                logger.warning("No ready tasks and no pending futures — breaking")
                break

            for task in ready:
                agent = self._find_agent_for_task(task)
                if not agent:
                    logger.debug(f"No idle agent for {task.task_id}, will retry next cycle")
                    continue

                context = ContextState()
                if self._memory_graph:
                    graph_result = self._memory_graph.query(task.description)
                    context = graph_result.to_context_state()

                # Route the task
                decision = self._router.route(task, agent=agent, context=context)
                task.assigned_model = decision.selected_model
                agent.assign(task)

                if self._on_router:
                    self._on_router(decision.to_event())

                # Launch async execution
                fut = asyncio.create_task(
                    self._execute_one(task, agent, decision, task_executor, semaphore)
                )
                pending_futures.add(fut)
                fut.add_done_callback(pending_futures.discard)

            # Wait for at least one task to complete before checking again
            if pending_futures:
                done, _ = await asyncio.wait(pending_futures, return_when=asyncio.FIRST_COMPLETED)
                for d in done:
                    pending_futures.discard(d)
            else:
                await asyncio.sleep(0.1)

        # Wait for any remaining
        if pending_futures:
            await asyncio.gather(*pending_futures, return_exceptions=True)

        self._completed_at = time.time()
        return self._build_result()

    async def _execute_one(
        self,
        task: Task,
        agent: CognitiveAgent,
        decision: RouterDecision,
        executor: Callable,
        semaphore: asyncio.Semaphore,
    ) -> None:
        """Execute a single task with semaphore control."""
        async with semaphore:
            task.start()
            if self._on_task:
                self._on_task(task.to_event())

            try:
                result = await executor(task, agent, decision)
                cost = decision.cost_estimate_usd
                task.complete(
                    result=result,
                    cost_usd=cost,
                    prompt_tokens=task.prompt_tokens_used,
                    completion_tokens=task.completion_tokens_used,
                )
                agent.record_completion(cost, task.prompt_tokens_used + task.completion_tokens_used)
                logger.info(f"✅ Task {task.task_id} completed (${cost:.4f})")

            except Exception as e:
                task.fail(str(e))
                agent.record_failure()
                logger.error(f"❌ Task {task.task_id} failed: {e}")

            if self._on_task:
                self._on_task(task.to_event())

    def _build_result(self) -> GraphResult:
        completed = [t for t in self._tasks.values() if t.status == TaskStatus.DONE]
        failed = [t for t in self._tasks.values() if t.status == TaskStatus.FAILED]
        duration = (self._completed_at or time.time()) - (self._started_at or time.time())

        return GraphResult(
            success=len(failed) == 0,
            total_tasks=len(self._tasks),
            completed=len(completed),
            failed=len(failed),
            total_cost_usd=sum(t.cost_actual_usd for t in self._tasks.values()),
            total_kern_spent=sum(t.budget_kern for t in completed),
            duration_seconds=round(duration, 2),
            task_results={t.task_id: (t.result or "") for t in completed},
            errors={t.task_id: (t.error or "") for t in failed},
        )

    def to_graph_event(self) -> dict:
        """Full graph state for Dashboard initial render."""
        nodes = []
        edges = []
        for task in self._tasks.values():
            nodes.append(task.to_event())
            for dep_id in task.depends_on:
                edges.append({"source": dep_id, "target": task.task_id})
        return {"nodes": nodes, "edges": edges}
