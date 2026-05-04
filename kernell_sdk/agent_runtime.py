"""
Kernell OS SDK — Agent Runtime (Phase 5)
═════════════════════════════════════════
Autonomous agent loop with planning, memory, tools, and execution.

This is the bridge between "infrastructure that runs code" and
"system that pursues goals autonomously" — the key gap vs Computer Use.

Architecture:
    Agent
      ├── Planner          (decides what to do next)
      ├── MemoryStore       (persistent state across steps)
      ├── ToolRegistry      (callable capabilities)
      ├── AgentLoop         (think → act → observe → refine)
      └── Executor          (CodePipeline → FormalVerifier → ExecutionGate)

Usage:
    from kernell_sdk.agent_runtime import (
        Agent, MemoryStore, ToolRegistry, Tool, AgentConfig
    )

    memory = MemoryStore()
    tools = ToolRegistry()
    tools.register(Tool("calculator", lambda expr: str(eval(expr)), "Evaluate math"))

    agent = Agent(
        llm_registry=my_registry,
        memory=memory,
        tools=tools,
    )

    result = agent.run("Calculate the compound interest on $1000 at 5% for 10 years")
    print(result.final_answer)
    print(result.steps_taken)
"""

import hashlib
import json
import logging
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Dict, List, Optional, Set, Union
import uuid

from kernell_sdk.agent_persistence import (
    CheckpointManager, AgentStateSnapshot, TaskStatus
)
from kernell_sdk.agent_validation import ToolValidator
from kernell_sdk.agent_world_model import WorldModelState, WorldModelUpdater
from kernell_sdk.agent_reliability import ReliabilityEngine, FailurePolicy
from kernell_sdk.interaction_router import InteractionRouter
from kernell_sdk.observability.event_bus import GLOBAL_EVENT_BUS

# Phase 9: Sully Compute Allocation + Swarm Orchestration
from kernell_sdk.sully.types import TaskFeatures, Tier
from kernell_sdk.sully.engine import SullyEngine
from kernell_sdk.sully.market import ModelMarketRegistry, GroqMarketProvider, OpenRouterMarketProvider, LocalMarketProvider
from kernell_sdk.sully.training_pipeline import TrainingPipeline
from kernell_sdk.swarm.orchestrator import SwarmOrchestrator
from kernell_sdk.swarm.decomposer import TaskDecomposer
from kernell_sdk.swarm.consensus import ConsensusEngine

logger = logging.getLogger("kernell.agent")


# ══════════════════════════════════════════════════════════════════════
# MEMORY
# ══════════════════════════════════════════════════════════════════════

@dataclass
class MemoryItem:
    """A single memory entry with metadata."""
    key: str
    value: Any
    timestamp: float
    step: int = 0
    source: str = ""  # "tool", "code", "observation", "user"


class MemoryStore:
    """
    Persistent agent memory. Survives across steps and tasks.
    MVP: in-memory dict. Pluggable to Redis/SQLite/vector DB.
    """

    def __init__(self):
        self._store: Dict[str, MemoryItem] = {}
        self._history: List[MemoryItem] = []  # Append-only timeline

    def remember(self, key: str, value: Any, source: str = "", step: int = 0):
        """Store a key-value pair in memory."""
        item = MemoryItem(
            key=key, value=value, timestamp=time.time(),
            step=step, source=source,
        )
        self._store[key] = item
        self._history.append(item)

    def recall(self, key: str, default: Any = None) -> Any:
        """Retrieve a value from memory."""
        item = self._store.get(key)
        return item.value if item else default

    def search(self, prefix: str) -> Dict[str, Any]:
        """Find all memories matching a key prefix."""
        return {k: v.value for k, v in self._store.items() if k.startswith(prefix)}

    def dump(self) -> Dict[str, Any]:
        """Get all current memory as a flat dict."""
        return {k: v.value for k, v in self._store.items()}

    def timeline(self, last_n: int = 10) -> List[Dict]:
        """Get the last N memory operations."""
        return [
            {"key": m.key, "value": str(m.value)[:200], "source": m.source, "step": m.step}
            for m in self._history[-last_n:]
        ]

    def clear(self):
        """Reset all memory."""
        self._store.clear()
        self._history.clear()

    def __len__(self):
        return len(self._store)

    def __contains__(self, key: str):
        return key in self._store


# ══════════════════════════════════════════════════════════════════════
# TOOL REGISTRY
# ══════════════════════════════════════════════════════════════════════

@dataclass
class Tool:
    """A callable tool available to the agent."""
    name: str
    func: Callable
    description: str
    parameters: Optional[Dict[str, str]] = None  # param_name → description

    def __call__(self, **kwargs):
        return self.func(**kwargs)


class ToolRegistry:
    """Registry of tools the agent can use."""

    def __init__(self):
        self._tools: Dict[str, Tool] = {}

    def register(self, tool: Tool):
        """Register a tool."""
        self._tools[tool.name] = tool
        logger.info(f"[ToolRegistry] Registered: {tool.name}")

    def get(self, name: str) -> Optional[Tool]:
        """Get a tool by name."""
        return self._tools.get(name)

    def list_tools(self) -> Dict[str, str]:
        """Get tool names and descriptions for the planner."""
        return {name: t.description for name, t in self._tools.items()}

    def list_tools_detailed(self) -> str:
        """Formatted tool descriptions for LLM prompts."""
        parts = []
        for name, tool in self._tools.items():
            params = ""
            if tool.parameters:
                params = ", ".join(f"{k}: {v}" for k, v in tool.parameters.items())
                params = f" (params: {params})"
            parts.append(f"  - {name}: {tool.description}{params}")
        return "\n".join(parts) if parts else "  (no tools registered)"

    def __len__(self):
        return len(self._tools)

    def __contains__(self, name: str):
        return name in self._tools


# ══════════════════════════════════════════════════════════════════════
# STEP / ACTION TYPES
# ══════════════════════════════════════════════════════════════════════

class ActionType(str, Enum):
    THINK = "think"       # Pure reasoning, no side effects
    CODE = "code"         # Generate and execute code
    TOOL = "tool"         # Call a registered tool directly
    UI_INTERACT = "ui_interact" # Abstract UI interaction (routed dynamically)
    ANSWER = "answer"     # Final answer to the user
    MEMORY = "memory"     # Store/recall from memory


@dataclass
class StepPlan:
    """A single planned action."""
    action: ActionType
    description: str = ""
    tool_name: str = ""
    tool_args: Dict[str, Any] = field(default_factory=dict)
    code_task: str = ""
    answer: str = ""
    memory_key: str = ""
    memory_value: Any = None
    expected_outcome: str = ""
    # Phase 7c: Abstract UI intent
    intent: str = ""
    target: str = ""
    text: str = ""


@dataclass
class StepResult:
    """Result of executing a single step."""
    step_idx: int
    action: ActionType
    success: bool
    output: str = ""
    error: str = ""
    duration_ms: float = 0.0
    is_valid: bool = True


# ══════════════════════════════════════════════════════════════════════
# AGENT CONFIG
# ══════════════════════════════════════════════════════════════════════

@dataclass
class AgentConfig:
    """Configuration for the agent loop."""
    max_steps: int = 10
    max_think_tokens: int = 2048
    max_code_tokens: int = 4096
    planning_temperature: float = 0.3
    planning_role: str = "reasoning"
    enable_code_execution: bool = True
    enable_tools: bool = True
    # Phase 9: Sully + Swarm configuration
    enable_swarm: bool = True       # enable autonomous DAG execution
    default_budget: float = 0.50    # $ budget per task
    max_concurrency: int = 10       # max parallel agents in swarm
    complexity_threshold: float = 0.4  # tasks above this use swarm


# ══════════════════════════════════════════════════════════════════════
# AGENT RESULT
# ══════════════════════════════════════════════════════════════════════

@dataclass
class AgentResult:
    """Complete result of an agent run."""
    goal: str
    final_answer: str = ""
    success: bool = False
    steps_taken: int = 0
    step_results: List[StepResult] = field(default_factory=list)
    total_tokens: int = 0
    total_duration_ms: float = 0.0
    memory_snapshot: Dict[str, Any] = field(default_factory=dict)
    # Phase 9: Economic metadata
    total_cost: float = 0.0
    tier_used: str = ""
    execution_mode: str = ""       # "single_shot" or "swarm"
    subtasks_completed: int = 0
    consensus_method: str = ""


# ══════════════════════════════════════════════════════════════════════
# PLANNER PROMPT
# ══════════════════════════════════════════════════════════════════════

PLANNER_SYSTEM = """You are an autonomous agent planner. Given a goal, current memory, available tools, and previous step results, decide the NEXT SINGLE action to take.

You MUST respond with valid JSON only. No markdown, no explanation outside JSON.

Action types:
- "think": Reason about the problem (no side effects). Use this to analyze before acting.
- "ui_interact": Interact with a UI element abstractly. The system will route this to DOM or OS Vision automatically.
- "code": Generate Python code to execute. Specify the task description.
- "tool": Call a registered tool by name with arguments (for non-UI tools).
- "answer": Provide the final answer. Use this when the goal is achieved.
- "memory": Store a value for later use.

Response format:
{
  "action": "think|ui_interact|code|tool|answer|memory",
  "reasoning": "why this action",
  "description": "what this step does",
  "intent": "click|type|read (if action=ui_interact)",
  "target": "visual label or DOM element description (if action=ui_interact)",
  "text": "text to type (if action=ui_interact and intent=type)",
  "tool_name": "name (if action=tool)",
  "tool_args": {"key": "value"} (if action=tool),
  "code_task": "task description (if action=code)",
  "answer": "final answer text (if action=answer)",
  "memory_key": "key (if action=memory)",
  "memory_value": "value (if action=memory)",
  "expected_outcome": "what should visually/technically happen as a result (for tool/code)"
}

Rules:
- Take ONE action at a time
- Use "think" first if the problem needs analysis
- Use "answer" when you have enough information to respond
- If you're stuck after multiple attempts, use "answer" with what you have
- NEVER loop indefinitely — converge to an answer"""


# ══════════════════════════════════════════════════════════════════════
# AGENT
# ══════════════════════════════════════════════════════════════════════

class Agent:
    """
    Autonomous agent with plan-act-observe loop.
    Integrates all SDK components: LLM Registry, CodePipeline,
    FormalVerifier, ExecutionGate, EconomicEngine.
    """

    def __init__(
        self,
        llm_registry,
        memory: Optional[MemoryStore] = None,
        tools: Optional[ToolRegistry] = None,
        config: Optional[AgentConfig] = None,
        code_pipeline=None,
        verifier=None,
        execution_gate=None,
        checkpoint_manager: Optional[CheckpointManager] = None,
        interaction_router: Optional[InteractionRouter] = None,
    ):
        self._registry = llm_registry
        self.memory = memory or MemoryStore()
        self.tools = tools or ToolRegistry()
        self.config = config or AgentConfig()
        self._pipeline = code_pipeline
        self._verifier = verifier
        self._gate = execution_gate
        self._checkpoint_manager = checkpoint_manager
        self._validator = ToolValidator(llm_registry)
        self._world_updater = WorldModelUpdater(llm_registry)
        self.world_model = WorldModelState()
        self._reliability = ReliabilityEngine()
        self._router = interaction_router or InteractionRouter()
        
        # Phase 9: Sully + Swarm Orchestration
        self._market = ModelMarketRegistry()
        self._sully = SullyEngine(market=self._market, mode="heuristic")
        self._orchestrator = SwarmOrchestrator(
            sully=self._sully,
            market=self._market,
            llm_registry=llm_registry,
            decomposer=TaskDecomposer(mode="llm", llm_registry=llm_registry),
            consensus=ConsensusEngine(llm_registry=llm_registry),
            max_concurrency=self.config.max_concurrency,
        )

    # ── Phase 9: Complexity Gate ──────────────────────────────────────

    def _assess_complexity(self, goal: str) -> float:
        """
        Quick heuristic to estimate task complexity.
        Determines whether to use single-shot or swarm execution.
        
        Returns 0.0 (trivial) to 1.0 (very complex).
        """
        score = 0.0
        goal_lower = goal.lower()
        
        # Length-based complexity
        if len(goal) > 200:
            score += 0.2
        if len(goal) > 500:
            score += 0.1
        
        # Multi-step indicators
        multi_step_keywords = [
            "and then", "after that", "step 1", "step 2",
            "first", "second", "third", "finally",
            "compare", "analyze", "research",
            "multiple", "several", "all",
        ]
        for kw in multi_step_keywords:
            if kw in goal_lower:
                score += 0.1
        
        # High-quality indicators
        quality_keywords = ["verify", "validate", "ensure", "critical", "accurate"]
        for kw in quality_keywords:
            if kw in goal_lower:
                score += 0.05
        
        # Code generation
        code_keywords = ["write code", "implement", "build", "create api", "develop"]
        for kw in code_keywords:
            if kw in goal_lower:
                score += 0.15
        
        return min(score, 1.0)

    def _infer_task_type(self, goal: str) -> str:
        """Infer task type from goal text for decomposition template selection."""
        goal_lower = goal.lower()
        
        if any(kw in goal_lower for kw in ["scrape", "extract", "crawl", "parse"]):
            return "web_scraping"
        if any(kw in goal_lower for kw in ["code", "implement", "function", "api", "build", "develop"]):
            return "code_gen"
        if any(kw in goal_lower for kw in ["research", "compare", "analyze", "investigate"]):
            return "research"
        if any(kw in goal_lower for kw in ["classify", "categorize", "label", "detect"]):
            return "classification"
        if any(kw in goal_lower for kw in ["data", "extract", "json", "csv", "table"]):
            return "data_extraction"
        if any(kw in goal_lower for kw in ["click", "navigate", "login", "fill", "submit"]):
            return "web_automation"
        
        return "research"  # safe default

    def run(self, goal: str, session_id: Optional[str] = None, budget: Optional[float] = None) -> AgentResult:
        """
        Execute the agent loop for a given goal.
        
        Phase 9 Complexity Gate:
          - Simple tasks (complexity < threshold) → direct single-shot execution
          - Complex tasks → Sully + Swarm DAG pipeline
        
        If session_id is provided and a checkpoint exists, the agent will resume from it.
        """
        t0 = time.time()
        
        # Phase 9: Complexity Gate — decide execution mode
        complexity = self._assess_complexity(goal)
        use_swarm = (
            self.config.enable_swarm
            and complexity >= self.config.complexity_threshold
        )
        
        if use_swarm:
            GLOBAL_EVENT_BUS.emit("agent_mode_selected", session_id or "unknown", {
                "mode": "swarm",
                "complexity": complexity,
                "threshold": self.config.complexity_threshold,
            })
            return self._run_swarm(
                goal, budget=budget or self.config.default_budget, t0=t0
            )
        
        GLOBAL_EVENT_BUS.emit("agent_mode_selected", session_id or "unknown", {
            "mode": "single_shot",
            "complexity": complexity,
            "threshold": self.config.complexity_threshold,
        })
        return self._run_single_shot(goal, session_id, t0)

    # ── Phase 9: Swarm Execution Path ────────────────────────────────

    def _run_swarm(self, goal: str, budget: float, t0: float) -> AgentResult:
        """
        Execute task through the full Sully + Swarm + Consensus pipeline.
        Used for complex, multi-step, or high-quality tasks.
        """
        import asyncio

        task_type = self._infer_task_type(goal)
        complexity = self._assess_complexity(goal)

        features = TaskFeatures(
            task_type=task_type,
            ui_complexity=complexity,
            estimated_tokens=self.config.max_think_tokens,
            estimated_output_tokens=self.config.max_code_tokens,
            quality_requirement=min(0.6 + complexity * 0.3, 0.95),
            expected_output_type="code" if task_type == "code_gen" else "text",
        )

        logger.info(
            f"[Agent] SWARM mode: type={task_type}, complexity={complexity:.2f}, "
            f"budget=${budget:.2f}"
        )

        try:
            loop = asyncio.get_event_loop()
            if loop.is_running():
                # Already in async context — use nest_asyncio pattern
                import concurrent.futures
                with concurrent.futures.ThreadPoolExecutor() as pool:
                    orch_result = pool.submit(
                        asyncio.run,
                        self._orchestrator.execute(goal, features, budget)
                    ).result()
            else:
                orch_result = asyncio.run(
                    self._orchestrator.execute(goal, features, budget)
                )
        except RuntimeError:
            # No event loop exists
            orch_result = asyncio.run(
                self._orchestrator.execute(goal, features, budget)
            )

        elapsed = round((time.time() - t0) * 1000, 1)

        result = AgentResult(
            goal=goal,
            final_answer=orch_result.output,
            success=orch_result.success,
            steps_taken=orch_result.subtasks_total,
            total_duration_ms=elapsed,
            memory_snapshot=self.memory.dump(),
            # Phase 9: Economic metadata
            total_cost=orch_result.total_cost,
            tier_used=f"swarm_{orch_result.waves_executed}w",
            execution_mode="swarm",
            subtasks_completed=orch_result.subtasks_succeeded,
            consensus_method=orch_result.consensus_method,
        )

        logger.info(
            f"[Agent] SWARM complete: {orch_result.subtasks_succeeded}/{orch_result.subtasks_total} "
            f"subtasks, {orch_result.waves_executed} waves, ${orch_result.total_cost:.4f}, "
            f"{elapsed}ms, consensus={orch_result.consensus_method}"
        )

        GLOBAL_EVENT_BUS.emit("agent_completed", orch_result.trace_id, {
            "mode": "swarm",
            "success": result.success,
            "subtasks": f"{orch_result.subtasks_succeeded}/{orch_result.subtasks_total}",
            "cost": orch_result.total_cost,
            "consensus": orch_result.consensus_method,
            "latency_ms": elapsed,
        })

        return result

    # ── Single-Shot Execution Path (original loop) ───────────────────

    def _run_single_shot(self, goal: str, session_id: Optional[str], t0: float) -> AgentResult:
        """
        Execute task through the original plan-act-observe loop.
        Used for simple, single-step tasks where swarm overhead is unnecessary.
        """
        # ── Phase 5.5: Task Recovery ──────────────────────────────
        session_id = session_id or str(uuid.uuid4())
        start_step = 0
        step_history: List[Dict] = []
        
        if self._checkpoint_manager:
            checkpoint = self._checkpoint_manager.load_checkpoint(session_id)
            if checkpoint:
                logger.info(f"[Agent] Resuming session {session_id} from step {checkpoint.current_step}")
                goal = checkpoint.goal  # Ensure goal matches
                start_step = checkpoint.current_step
                step_history = checkpoint.history
                
                # Restore memory
                self.memory.clear()
                for k, v in checkpoint.memory_dump.items():
                    self.memory.remember(k, v, source="checkpoint")
                    
                # Phase 5.7: Restore World Model
                if checkpoint.world_model:
                    self.world_model = WorldModelState.from_dict(checkpoint.world_model)
                    
                if checkpoint.status in (TaskStatus.COMPLETED, TaskStatus.FAILED):
                    logger.warning(f"[Agent] Session {session_id} is already {checkpoint.status.value}")
                    return AgentResult(
                        goal=goal, success=(checkpoint.status == TaskStatus.COMPLETED),
                        steps_taken=start_step, memory_snapshot=self.memory.dump(),
                        execution_mode="single_shot",
                    )

        result = AgentResult(goal=goal, execution_mode="single_shot")
        logger.info(f"[Agent] Starting/Resuming: {goal[:100]} (session: {session_id})")
        GLOBAL_EVENT_BUS.emit("agent_started", session_id, {"goal": goal, "session_id": session_id})

        for step_idx in range(start_step, self.config.max_steps):
            # ── Plan next action ─────────────────────────────────
            plan = self._plan_next(goal, step_history)

            if plan is None:
                result.final_answer = "Planning failed — could not determine next action"
                self._save_checkpoint(session_id, goal, step_idx, step_history, TaskStatus.FAILED)
                GLOBAL_EVENT_BUS.emit("agent_error", session_id, {"error": "Planning failed"})
                break
                
            GLOBAL_EVENT_BUS.emit("step_started", session_id, {
                "step": step_idx,
                "intent": getattr(plan, "intent", ""),
                "action": plan.action.value,
                "description": plan.description
            })

            # ── Phase 6.5: Evaluate Plan Viability ───────────────
            action_key = plan.tool_name or plan.code_task or plan.action.value
            if not self._reliability.evaluate_plan(action_key):
                step_result = StepResult(
                    step_idx=step_idx, action=plan.action, success=False, is_valid=False,
                    error=f"[SYSTEM] Tool '{action_key}' is blocked due to consecutive failures. You MUST change your strategy or tool."
                )
            else:
                # ── Execute the action ───────────────────────────────
                step_result = self._execute_step(step_idx, plan)
                
            result.step_results.append(step_result)
            result.steps_taken = step_idx + 1

            # ── Phase 6.5: Record Success/Failure & Protect State 
            is_successful = step_result.success and step_result.is_valid
            
            if is_successful:
                self._reliability.record_success(action_key)
            else:
                directive = self._reliability.record_failure(action_key)
                if directive == "abort":
                    result.final_answer = f"Agent aborted due to repeated failures. Last error: {step_result.error or step_result.output}"
                    self._save_checkpoint(session_id, goal, step_idx + 1, step_history, TaskStatus.FAILED)
                    GLOBAL_EVENT_BUS.emit("agent_error", session_id, {"error": "Max failures reached."})
                    break

            # Record in history for next planning iteration
            step_history.append({
                "step": step_idx,
                "action": plan.action.value,
                "description": plan.description,
                "output": step_result.output[:500] if is_successful else step_result.error or step_result.output[:500],
                "success": step_result.success,
                "is_valid": step_result.is_valid,
            })
            
            GLOBAL_EVENT_BUS.emit("step_completed", session_id, {
                "step": step_idx,
                "success": step_result.success,
                "is_valid": step_result.is_valid,
            })

            # ── Check for completion ─────────────────────────────
            if plan.action == ActionType.ANSWER:
                result.final_answer = plan.answer or step_result.output
                result.success = True
                self._save_checkpoint(session_id, goal, step_idx + 1, step_history, TaskStatus.COMPLETED)
                break
                
            # ── Phase 5.7: Update World Model ────────────────────
            # Phase 6.5: ONLY update world model if the action was successful and valid!
            if is_successful and plan.action in (ActionType.TOOL, ActionType.CODE):
                self.world_model = self._world_updater.update_beliefs(
                    self.world_model,
                    action=f"{plan.action.value} -> {plan.tool_name or plan.code_task}",
                    observation=step_result.output
                )
                GLOBAL_EVENT_BUS.emit("world_model_updated", session_id, self.world_model.to_dict())

            # ── Safety: detect spinning ──────────────────────────
            if len(step_history) >= 3:
                last_actions = [h["action"] for h in step_history[-3:]]
                if len(set(last_actions)) == 1 and last_actions[0] == "think":
                    logger.warning("[Agent] Detected thinking loop — forcing answer")
                    result.final_answer = self._force_answer(goal, step_history)
                    result.success = True
                    self._save_checkpoint(session_id, goal, step_idx + 1, step_history, TaskStatus.COMPLETED)
                    break
                    
            # ── Phase 5.5: Save state after step ─────────────────
            self._save_checkpoint(session_id, goal, step_idx + 1, step_history, TaskStatus.RUNNING)

        if not result.final_answer and step_history:
            result.final_answer = self._force_answer(goal, step_history)
            result.success = True
            self._save_checkpoint(session_id, goal, self.config.max_steps, step_history, TaskStatus.COMPLETED)

        result.total_duration_ms = round((time.time() - t0) * 1000, 1)
        result.memory_snapshot = self.memory.dump()

        logger.info(
            f"[Agent] Complete: {result.steps_taken} steps, "
            f"{result.total_duration_ms}ms, success={result.success}"
        )
        return result

    # ── Planning ─────────────────────────────────────────────────────

    def _plan_next(self, goal: str, history: List[Dict]) -> Optional[StepPlan]:
        """Ask the LLM to plan the next action."""
        context_parts = [f"GOAL: {goal}"]

        # Phase 5.7: World Model Context
        if self.world_model:
            context_parts.append(f"WORLD MODEL:\n{json.dumps(self.world_model.to_dict(), default=str)[:1500]}")

        # Memory context
        mem = self.memory.dump()
        if mem:
            context_parts.append(f"CURRENT MEMORY:\n{json.dumps(mem, default=str)[:1000]}")

        # Tools context
        if self.tools and len(self.tools) > 0:
            context_parts.append(f"AVAILABLE TOOLS:\n{self.tools.list_tools_detailed()}")
        else:
            context_parts.append("AVAILABLE TOOLS: none")

        # Code execution capability
        if self.config.enable_code_execution:
            context_parts.append("CODE EXECUTION: enabled (Python sandbox)")

        # Previous steps
        if history:
            hist_str = json.dumps(history[-5:], default=str)[:2000]  # Last 5 steps
            context_parts.append(f"PREVIOUS STEPS:\n{hist_str}")

        prompt = "\n\n".join(context_parts)

        resp = self._registry.complete(
            messages=[{"role": "user", "content": prompt}],
            system_prompt=PLANNER_SYSTEM,
            role=self.config.planning_role,
            max_tokens=self.config.max_think_tokens,
            temperature=self.config.planning_temperature,
        )

        if not resp:
            return None

        return self._parse_plan(resp.content)

    def _parse_plan(self, content: str) -> Optional[StepPlan]:
        """Parse the LLM's JSON response into a StepPlan."""
        # Try to extract JSON from the response
        text = content.strip()

        # Handle markdown code blocks
        if "```json" in text:
            text = text.split("```json")[1].split("```")[0].strip()
        elif "```" in text:
            text = text.split("```")[1].split("```")[0].strip()

        # Find JSON object
        start = text.find("{")
        end = text.rfind("}") + 1
        if start >= 0 and end > start:
            text = text[start:end]

        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            # Fallback: treat entire response as a think step
            return StepPlan(action=ActionType.THINK, description=content[:500])

        action_str = data.get("action", "think")
        try:
            action = ActionType(action_str)
        except ValueError:
            action = ActionType.THINK

        return StepPlan(
            action=action,
            description=data.get("description", data.get("reasoning", "")),
            tool_name=data.get("tool_name", ""),
            tool_args=data.get("tool_args", {}),
            code_task=data.get("code_task", ""),
            answer=data.get("answer", ""),
            memory_key=data.get("memory_key", ""),
            memory_value=data.get("memory_value"),
            expected_outcome=data.get("expected_outcome", ""),
            intent=data.get("intent", ""),
            target=data.get("target", ""),
            text=data.get("text", ""),
        )

    # ── Step Execution ───────────────────────────────────────────────

    def _execute_step(self, step_idx: int, plan: StepPlan) -> StepResult:
        """Execute a single planned step."""
        t0 = time.time()

        if plan.action == ActionType.THINK:
            return StepResult(
                step_idx=step_idx, action=plan.action, success=True,
                output=plan.description,
                duration_ms=round((time.time() - t0) * 1000, 1),
            )

        elif plan.action == ActionType.TOOL:
            return self._execute_tool(step_idx, plan)
            
        elif plan.action == ActionType.UI_INTERACT:
            return self._execute_ui_interact(step_idx, plan)

        elif plan.action == ActionType.CODE:
            return self._execute_code(step_idx, plan)

        elif plan.action == ActionType.MEMORY:
            self.memory.remember(
                plan.memory_key, plan.memory_value,
                source="agent", step=step_idx,
            )
            return StepResult(
                step_idx=step_idx, action=plan.action, success=True,
                output=f"Stored '{plan.memory_key}'",
                duration_ms=round((time.time() - t0) * 1000, 1),
            )

        elif plan.action == ActionType.ANSWER:
            return StepResult(
                step_idx=step_idx, action=plan.action, success=True,
                output=plan.answer,
                duration_ms=round((time.time() - t0) * 1000, 1),
            )

        return StepResult(
            step_idx=step_idx, action=plan.action, success=False,
            error=f"Unknown action: {plan.action}",
        )

    def _execute_ui_interact(self, step_idx: int, plan: StepPlan) -> StepResult:
        """Phase 7c: Route abstract UI interaction to concrete tool."""
        t0 = time.time()
        
        route = self._router.route(plan.intent, plan.target, plan.text, self.world_model)
        
        if route.confidence < 0.6:
            return StepResult(
                step_idx=step_idx, action=ActionType.UI_INTERACT, success=False, is_valid=False,
                error=f"Cannot confidently route interaction for '{plan.target}'. Confidence: {route.confidence}. Reason: {route.reasoning}. Please replan or explore the screen.",
                duration_ms=round((time.time() - t0) * 1000, 1),
            )
            
        # Modify the plan to match the routed tool, then delegate to _execute_tool
        plan.tool_name = route.tool_name
        plan.tool_args = route.args
        
        res = self._execute_tool(step_idx, plan)
        # Override the action type back to UI_INTERACT for logging
        res.action = ActionType.UI_INTERACT
        res.output = f"[Routed via {route.tool_name} (conf: {route.confidence})]\n" + res.output
        return res

    def _execute_tool(self, step_idx: int, plan: StepPlan) -> StepResult:
        """Execute a tool call."""
        t0 = time.time()
        tool = self.tools.get(plan.tool_name)

        if not tool:
            return StepResult(
                step_idx=step_idx, action=ActionType.TOOL, success=False,
                error=f"Tool not found: {plan.tool_name}",
                duration_ms=round((time.time() - t0) * 1000, 1),
            )

        try:
            output = tool(**plan.tool_args)
            output_str = str(output)[:5000]
            is_valid = True
            
            # Phase 5.6: Validate expectation
            if plan.expected_outcome:
                val_result = self._validator.validate(plan.expected_outcome, output_str)
                if not val_result.is_valid:
                    is_valid = False
                    output_str += f"\n\n[VALIDATION FAILED] The expectation was NOT met: {val_result.reason}"
            
            self.memory.remember(
                f"tool:{plan.tool_name}:result",
                output_str, source="tool", step=step_idx,
            )
            return StepResult(
                step_idx=step_idx, action=ActionType.TOOL, success=True,
                is_valid=is_valid,
                output=output_str,
                duration_ms=round((time.time() - t0) * 1000, 1),
            )
        except Exception as e:
            return StepResult(
                step_idx=step_idx, action=ActionType.TOOL, success=False,
                error=f"{type(e).__name__}: {e}",
                duration_ms=round((time.time() - t0) * 1000, 1),
            )

    def _execute_code(self, step_idx: int, plan: StepPlan) -> StepResult:
        """Execute code via the full pipeline: CodePipeline → Verifier → Gate."""
        t0 = time.time()

        if not self.config.enable_code_execution:
            return StepResult(
                step_idx=step_idx, action=ActionType.CODE, success=False,
                error="Code execution disabled by config",
            )

        # If we have the full pipeline, use it
        if self._pipeline:
            pipe_result = self._pipeline.run(task=plan.code_task)
            if not pipe_result.success:
                return StepResult(
                    step_idx=step_idx, action=ActionType.CODE, success=False,
                    error="CodePipeline failed",
                    duration_ms=round((time.time() - t0) * 1000, 1),
                )
            code = pipe_result.final_code
        else:
            # Fallback: ask LLM directly for code
            resp = self._registry.complete(
                messages=[{"role": "user", "content": f"Write Python code for: {plan.code_task}\nOutput ONLY the code, no explanations."}],
                system_prompt="You are a Python programmer. Output only executable Python code.",
                role="implementer",
                max_tokens=self.config.max_code_tokens,
                temperature=0.2,
            )
            if not resp:
                return StepResult(
                    step_idx=step_idx, action=ActionType.CODE, success=False,
                    error="LLM failed to generate code",
                )
            code = resp.content
            # Strip markdown code blocks
            if "```python" in code:
                code = code.split("```python")[1].split("```")[0].strip()
            elif "```" in code:
                code = code.split("```")[1].split("```")[0].strip()

        # Verify if verifier available
        if self._verifier:
            vr = self._verifier.verify(code)
            if not vr.passed:
                return StepResult(
                    step_idx=step_idx, action=ActionType.CODE, success=False,
                    error=f"Verification blocked: {len(vr.violations)} violations",
                    duration_ms=round((time.time() - t0) * 1000, 1),
                )

        # Execute via gate or direct
        if self._gate:
            exec_result = self._gate.execute(code)
            output = exec_result.stdout if exec_result.success else (exec_result.error or "")
            self.memory.remember(
                f"code:step{step_idx}:result",
                output, source="code", step=step_idx,
            )
            return StepResult(
                step_idx=step_idx, action=ActionType.CODE,
                success=exec_result.success,
                output=output[:5000],
                error=exec_result.error or "",
                duration_ms=round((time.time() - t0) * 1000, 1),
            )
        else:
            # No gate — return code as output (dry run)
            self.memory.remember(
                f"code:step{step_idx}:generated",
                code[:2000], source="code", step=step_idx,
            )
            return StepResult(
                step_idx=step_idx, action=ActionType.CODE, success=True,
                output=f"[Code generated, no execution gate]\n{code[:2000]}",
                duration_ms=round((time.time() - t0) * 1000, 1),
            )

    # ── Forced Answer ────────────────────────────────────────────────

    def _force_answer(self, goal: str, history: List[Dict]) -> str:
        """Force a final answer when the agent is stuck or at max steps."""
        context = (
            f"GOAL: {goal}\n\n"
            f"MEMORY: {json.dumps(self.memory.dump(), default=str)[:1500]}\n\n"
            f"STEPS TAKEN: {json.dumps(history[-5:], default=str)[:1500]}\n\n"
            "Based on everything above, provide your BEST final answer now."
        )
        resp = self._registry.complete(
            messages=[{"role": "user", "content": context}],
            system_prompt="Synthesize all available information into a clear, complete answer.",
            role="default",
            max_tokens=2048,
        )
        return resp.content if resp else "Unable to produce answer"

    # ── Persistence ──────────────────────────────────────────────────

    def _save_checkpoint(self, session_id: str, goal: str, step: int, history: List[Dict], status: TaskStatus):
        """Save the agent's current state if checkpoint manager is enabled."""
        if not self._checkpoint_manager:
            return
            
        state = AgentStateSnapshot(
            session_id=session_id,
            goal=goal,
            status=status,
            current_step=step,
            memory_dump=self.memory.dump(),
            history=history,
            created_at=time.time(),
            updated_at=time.time(),
            world_model=self.world_model.to_dict() if self.world_model else None
        )
        self._checkpoint_manager.save_checkpoint(state)
