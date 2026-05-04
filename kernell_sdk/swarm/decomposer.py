"""
Kernell OS SDK — Task Decomposer
════════════════════════════════
Intelligent task decomposition for swarm execution.

Converts complex tasks into parallelizable DAGs of subtasks,
each with its own TaskFeatures for independent Sully routing.

Pipeline:
    Complex Task → Decomposer → SubTask DAG → Sully (per node) → Swarm → Consensus

v0: Template-based heuristic decomposition (no LLM needed, ships today)
v1: LLM-assisted decomposition using ECONOMIC tier (future)
"""

import logging
import uuid
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Set

from kernell_sdk.sully.types import TaskFeatures, Tier
from kernell_sdk.observability.event_bus import GLOBAL_EVENT_BUS

logger = logging.getLogger("kernell.swarm.decomposer")


# ══════════════════════════════════════════════════════════════════════
# TYPES
# ══════════════════════════════════════════════════════════════════════

@dataclass
class SubTask:
    """A single node in the task DAG."""
    id: str
    description: str
    prompt: str                     # actual prompt to send to LLM
    features: TaskFeatures
    dependencies: List[str] = field(default_factory=list)  # ids of tasks that must finish first
    priority: int = 0               # higher = more important
    phase: str = "execute"          # "plan", "execute", "validate", "synthesize"


@dataclass
class TaskDAG:
    """Directed Acyclic Graph of subtasks with execution ordering."""
    original_task: str
    subtasks: List[SubTask] = field(default_factory=list)
    
    @property
    def phases(self) -> Dict[str, List[SubTask]]:
        """Group subtasks by phase."""
        groups: Dict[str, List[SubTask]] = {}
        for st in self.subtasks:
            groups.setdefault(st.phase, []).append(st)
        return groups
    
    @property
    def execution_waves(self) -> List[List[SubTask]]:
        """
        Compute parallel execution waves respecting dependencies.
        Wave 0: tasks with no dependencies
        Wave 1: tasks depending only on Wave 0, etc.
        """
        completed: Set[str] = set()
        waves: List[List[SubTask]] = []
        remaining = list(self.subtasks)
        
        while remaining:
            wave = [
                st for st in remaining
                if all(dep in completed for dep in st.dependencies)
            ]
            if not wave:
                # Circular dependency or broken DAG — force remaining into last wave
                logger.warning(f"[Decomposer] Breaking circular dependency: {[st.id for st in remaining]}")
                waves.append(remaining)
                break
            
            waves.append(wave)
            for st in wave:
                completed.add(st.id)
            remaining = [st for st in remaining if st.id not in completed]
        
        return waves


# ══════════════════════════════════════════════════════════════════════
# DECOMPOSITION TEMPLATES (v0 — deterministic, no LLM)
# ══════════════════════════════════════════════════════════════════════

# Maps task_type to a decomposition template
DECOMPOSITION_TEMPLATES: Dict[str, List[Dict]] = {
    "web_scraping": [
        {"phase": "execute", "suffix": "navigate_and_scrape",  "quality": 0.6, "output_type": "text"},
        {"phase": "execute", "suffix": "extract_structured",   "quality": 0.7, "output_type": "json"},
        {"phase": "validate", "suffix": "validate_extraction", "quality": 0.8, "output_type": "json", "depends_on": [-1]},
        {"phase": "synthesize", "suffix": "format_output",     "quality": 0.6, "output_type": "text", "depends_on": [-1]},
    ],
    "code_gen": [
        {"phase": "plan", "suffix": "design_architecture",     "quality": 0.8, "output_type": "text"},
        {"phase": "execute", "suffix": "implement_code",       "quality": 0.9, "output_type": "code", "depends_on": [0]},
        {"phase": "validate", "suffix": "review_and_test",     "quality": 0.9, "output_type": "code", "depends_on": [1]},
        {"phase": "synthesize", "suffix": "integrate_final",   "quality": 0.8, "output_type": "code", "depends_on": [2]},
    ],
    "data_extraction": [
        {"phase": "execute", "suffix": "extract_raw",          "quality": 0.6, "output_type": "json"},
        {"phase": "execute", "suffix": "parse_normalize",      "quality": 0.7, "output_type": "json"},
        {"phase": "validate", "suffix": "validate_schema",     "quality": 0.8, "output_type": "json", "depends_on": [0, 1]},
        {"phase": "synthesize", "suffix": "merge_results",     "quality": 0.7, "output_type": "json", "depends_on": [-1]},
    ],
    "research": [
        {"phase": "execute", "suffix": "search_source_1",      "quality": 0.6, "output_type": "text", "parallel": True},
        {"phase": "execute", "suffix": "search_source_2",      "quality": 0.6, "output_type": "text", "parallel": True},
        {"phase": "execute", "suffix": "search_source_3",      "quality": 0.6, "output_type": "text", "parallel": True},
        {"phase": "synthesize", "suffix": "cross_reference",   "quality": 0.8, "output_type": "text", "depends_on": [0, 1, 2]},
        {"phase": "validate", "suffix": "fact_check",          "quality": 0.9, "output_type": "text", "depends_on": [-1]},
    ],
    "web_automation": [
        {"phase": "plan", "suffix": "identify_steps",          "quality": 0.7, "output_type": "json"},
        {"phase": "execute", "suffix": "execute_interaction",  "quality": 0.8, "output_type": "text", "depends_on": [0]},
        {"phase": "validate", "suffix": "verify_result",       "quality": 0.8, "output_type": "json", "depends_on": [1]},
    ],
    "classification": [
        # Simple tasks don't need decomposition — single subtask
        {"phase": "execute", "suffix": "classify",             "quality": 0.7, "output_type": "json"},
    ],
}

# Fallback template for unknown task types
DEFAULT_TEMPLATE = [
    {"phase": "plan", "suffix": "analyze_task",        "quality": 0.7, "output_type": "text"},
    {"phase": "execute", "suffix": "execute_task",     "quality": 0.7, "output_type": "text", "depends_on": [0]},
    {"phase": "validate", "suffix": "validate_output", "quality": 0.8, "output_type": "text", "depends_on": [1]},
]


# ══════════════════════════════════════════════════════════════════════
# TASK DECOMPOSER
# ══════════════════════════════════════════════════════════════════════

class TaskDecomposer:
    """
    Decomposes complex tasks into executable DAGs.
    
    v0: Uses template-based decomposition (deterministic, zero cost)
    v1: Uses LLM-assisted decomposition (ECONOMIC tier, smart splitting)
    """
    
    def __init__(
        self,
        llm_registry=None,         # for v1: LLM-assisted decomposition
        mode: str = "template",    # "template" or "llm"
        max_subtasks: int = 8,     # safety limit
    ):
        self.llm = llm_registry
        self.mode = mode
        self.max_subtasks = max_subtasks
    
    def decompose(
        self,
        task_description: str,
        base_features: TaskFeatures,
        budget_cap: float = 1.0,
    ) -> TaskDAG:
        """
        Decompose a complex task into a DAG of subtasks.
        """
        if self.mode == "llm" and self.llm:
            dag = self._decompose_with_llm(task_description, base_features, budget_cap)
        else:
            dag = self._decompose_template(task_description, base_features)
        
        # Budget distribution across subtasks
        self._distribute_budget(dag, budget_cap)
        
        # Emit telemetry
        waves = dag.execution_waves
        GLOBAL_EVENT_BUS.emit("task_decomposed", "current", {
            "original_task": task_description[:200],
            "subtask_count": len(dag.subtasks),
            "wave_count": len(waves),
            "phases": list(dag.phases.keys()),
            "parallelizable_count": sum(
                1 for st in dag.subtasks if st.features.parallelizable
            ),
        })
        
        logger.info(
            f"[Decomposer] '{task_description[:60]}...' → "
            f"{len(dag.subtasks)} subtasks in {len(waves)} waves"
        )
        
        return dag
    
    # ── v0: Template-Based Decomposition ─────────────────────────────
    
    def _decompose_template(
        self,
        task_description: str,
        base_features: TaskFeatures,
    ) -> TaskDAG:
        """
        Deterministic decomposition using task_type templates.
        Zero cost, zero latency, ships today.
        """
        template = DECOMPOSITION_TEMPLATES.get(
            base_features.task_type, DEFAULT_TEMPLATE
        )
        
        dag = TaskDAG(original_task=task_description)
        
        for i, step in enumerate(template[:self.max_subtasks]):
            subtask_id = f"st_{uuid.uuid4().hex[:8]}"
            
            # Resolve dependencies
            deps = []
            for dep_idx in step.get("depends_on", []):
                if dep_idx == -1:
                    # Depends on previous subtask
                    if dag.subtasks:
                        deps.append(dag.subtasks[-1].id)
                elif 0 <= dep_idx < len(dag.subtasks):
                    deps.append(dag.subtasks[dep_idx].id)
            
            # Build per-subtask features
            features = TaskFeatures(
                task_type=base_features.task_type,
                ui_complexity=base_features.ui_complexity,
                requires_auth=base_features.requires_auth,
                dom_available=base_features.dom_available,
                estimated_tokens=base_features.estimated_tokens // max(len(template), 1),
                estimated_output_tokens=base_features.estimated_output_tokens // max(len(template), 1),
                parallelizable=len(deps) == 0 and step.get("parallel", False),
                expected_output_type=step.get("output_type", "text"),
                quality_requirement=step.get("quality", base_features.quality_requirement),
            )
            
            subtask = SubTask(
                id=subtask_id,
                description=f"{step['suffix']}",
                prompt=f"[Task: {task_description}]\n[Step: {step['suffix']}]\nExecute this specific step and return the result.",
                features=features,
                dependencies=deps,
                priority=len(template) - i,  # earlier steps = higher priority
                phase=step["phase"],
            )
            
            dag.subtasks.append(subtask)
        
        return dag
    
    # ── v1: LLM-Assisted Decomposition ──────────────────────────────
    
    def _decompose_with_llm(
        self,
        task_description: str,
        base_features: TaskFeatures,
        budget_cap: float,
    ) -> TaskDAG:
        """
        Use an LLM (ECONOMIC tier) to intelligently decompose tasks.
        Falls back to template if LLM fails.
        """
        decompose_prompt = f"""Decompose this task into subtasks for parallel execution.

TASK: {task_description}

Return ONLY valid JSON:
{{
  "subtasks": [
    {{
      "description": "what to do",
      "phase": "plan|execute|validate|synthesize",
      "dependencies": [],
      "quality_required": 0.7,
      "output_type": "text|json|code",
      "parallelizable": true
    }}
  ]
}}

Rules:
- Maximum {self.max_subtasks} subtasks
- Dependencies reference subtask indices (0-based)
- Minimize dependencies to maximize parallelism
- Set quality_required higher for validation/synthesis steps
"""
        
        try:
            import json
            response = self.llm.complete(
                messages=[{"role": "user", "content": decompose_prompt}],
                role="economy",
                max_tokens=1000,
            )
            
            if response and response.content:
                data = json.loads(response.content)
                return self._parse_llm_decomposition(data, task_description, base_features)
        except Exception as e:
            logger.warning(f"[Decomposer] LLM decomposition failed: {e}, falling back to template")
        
        return self._decompose_template(task_description, base_features)
    
    def _parse_llm_decomposition(
        self,
        data: dict,
        task_description: str,
        base_features: TaskFeatures,
    ) -> TaskDAG:
        """Parse LLM JSON output into a TaskDAG."""
        dag = TaskDAG(original_task=task_description)
        
        for i, item in enumerate(data.get("subtasks", [])[:self.max_subtasks]):
            subtask_id = f"st_{uuid.uuid4().hex[:8]}"
            
            # Resolve dependency indices to IDs
            deps = []
            for dep_idx in item.get("dependencies", []):
                if isinstance(dep_idx, int) and 0 <= dep_idx < len(dag.subtasks):
                    deps.append(dag.subtasks[dep_idx].id)
            
            features = TaskFeatures(
                task_type=base_features.task_type,
                ui_complexity=base_features.ui_complexity,
                requires_auth=base_features.requires_auth,
                dom_available=base_features.dom_available,
                estimated_tokens=base_features.estimated_tokens // max(len(data.get("subtasks", [])), 1),
                estimated_output_tokens=base_features.estimated_output_tokens // max(len(data.get("subtasks", [])), 1),
                parallelizable=item.get("parallelizable", False),
                expected_output_type=item.get("output_type", "text"),
                quality_requirement=item.get("quality_required", 0.7),
            )
            
            subtask = SubTask(
                id=subtask_id,
                description=item.get("description", f"subtask_{i}"),
                prompt=f"[Task: {task_description}]\n[Step: {item.get('description', '')}]\nExecute this step.",
                features=features,
                dependencies=deps,
                priority=len(data.get("subtasks", [])) - i,
                phase=item.get("phase", "execute"),
            )
            
            dag.subtasks.append(subtask)
        
        if not dag.subtasks:
            return self._decompose_template(task_description, base_features)
        
        return dag
    
    # ── Budget Distribution ──────────────────────────────────────────
    
    def _distribute_budget(self, dag: TaskDAG, total_budget: float):
        """
        Distribute budget across subtasks based on phase priority.
        Validation and synthesis get higher share.
        """
        phase_weights = {
            "plan": 0.1,
            "execute": 0.3,
            "validate": 0.35,
            "synthesize": 0.25,
        }
        
        # Calculate total weight
        total_weight = sum(
            phase_weights.get(st.phase, 0.25)
            for st in dag.subtasks
        )
        
        if total_weight == 0:
            return
        
        for st in dag.subtasks:
            weight = phase_weights.get(st.phase, 0.25)
            # This is informational — actual budget enforcement is in SwarmBudgetManager
            share = (weight / total_weight) * total_budget
            # Store as metadata hint (not enforced here)
            st.features.estimated_tokens = max(
                st.features.estimated_tokens,
                int(share * 1000 / max(0.001, 1.0))  # rough estimate
            )
