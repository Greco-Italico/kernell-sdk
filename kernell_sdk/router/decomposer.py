"""
Kernell OS SDK — Task Decomposer-Classifier
═════════════════════════════════════════════
The heart of the token economy. A single model (fine-tuned Qwen3-1.7B)
performs TWO jobs in ONE inference:

  1. DECOMPOSITION: Breaks a task into the maximum number of atomic steps
  2. CLASSIFICATION: Assigns difficulty (1-5), domain, and target tier

This is the #1 candidate for QLoRA fine-tuning. With ~1500 examples
it outperforms 7B models without fine-tuning on this specific task.

The output is structured JSON consumed directly by the Router.
"""
from __future__ import annotations

import json
import logging
import time
from typing import List, Optional, Protocol

from .types import SubTask, DifficultyLevel, ModelTier, TaskDomain

logger = logging.getLogger("kernell.router.decomposer")


# ── System prompt for the classifier (used both at inference and fine-tuning) ─

DECOMPOSER_SYSTEM_PROMPT = """\
You are a Task Decomposer-Classifier for an AI agent system.

Your job is to break a task into the MAXIMUM number of atomic subtasks possible,
and classify each one by difficulty and domain.

Rules:
- Each subtask must be independently executable by a single AI model
- Assign difficulty 1-5:
  1 = retrieval, formatting, simple classification
  2 = summarization, transformations, Q&A on given context
  3 = multi-step reasoning, functional code generation
  4 = complex analysis, multi-source synthesis, advanced code
  5 = deep abstract reasoning, genuine creativity, frontier-only tasks
- Identify dependencies between steps
- Mark which steps can run in parallel
- Be aggressive in decomposition: more small steps = cheaper execution

Before outputting the JSON array, you MUST think step-by-step in a <thought> block.
Trace your logic, evaluate the difficulty of each step, and map out dependencies.
After your thought block, output ONLY the JSON array.

Example output:
<thought>
1. The user wants to build an API with JWT auth.
2. Step 1: Extract endpoint list. Difficulty 1 (simple extraction), Domain: data.
3. Step 2: Generate route handlers. Difficulty 2 (summarization/generation), Domain: code.
4. Step 3: Implement JWT middleware. Difficulty 3 (multi-step logic), Domain: code. Depends on s1 and s2.
</thought>
[
  {"id": "s1", "description": "Extract endpoint list from spec", "difficulty": 1, "domain": "data", "parallel_ok": true, "depends_on": []},
  {"id": "s2", "description": "Generate route handler skeleton", "difficulty": 2, "domain": "code", "parallel_ok": true, "depends_on": []},
  {"id": "s3", "description": "Implement JWT middleware logic", "difficulty": 3, "domain": "code", "parallel_ok": false, "depends_on": ["s1", "s2"]}
]
"""


class LLMBackend(Protocol):
    """Protocol for any LLM that can generate text."""
    def generate(self, prompt: str, system: str = "") -> str: ...


# ── Difficulty → Tier mapping ────────────────────────────────────────────────

def _difficulty_to_tier(difficulty: int, has_large_local: bool = False) -> ModelTier:
    """Map difficulty level to the cheapest capable tier."""
    if difficulty <= 1:
        return ModelTier.LOCAL_NANO
    elif difficulty <= 2:
        return ModelTier.LOCAL_SMALL
    elif difficulty <= 3:
        return ModelTier.LOCAL_MEDIUM
    elif difficulty == 4:
        return ModelTier.LOCAL_LARGE if has_large_local else ModelTier.CHEAP_API
    else:
        return ModelTier.PREMIUM_API


class TaskDecomposer:
    """
    Decomposes a complex task into atomic subtasks with difficulty classification.
    
    Uses a local model (ideally fine-tuned) for the decomposition itself,
    keeping the meta-reasoning cost at near zero.
    """

    def __init__(
        self,
        model: LLMBackend,
        has_large_local: bool = False,
        confidence_default: float = 0.75,
    ):
        self._model = model
        self._has_large_local = has_large_local
        self._confidence_default = confidence_default
        self._decomposition_count = 0

    def decompose(self, task: str) -> List[SubTask]:
        """
        Decompose a task into atomic subtasks.
        
        Args:
            task: The full task description from the user
            
        Returns:
            List of SubTask objects ready for the Router
        """
        t0 = time.monotonic()

        raw_output = self._model.generate(
            prompt=f"Decompose this task:\n\n{task}",
            system=DECOMPOSER_SYSTEM_PROMPT,
        )

        subtasks = self._parse_output(raw_output, task)
        elapsed = (time.monotonic() - t0) * 1000

        self._decomposition_count += 1
        logger.info(
            f"Decomposed into {len(subtasks)} subtasks in {elapsed:.0f}ms "
            f"(decomposition #{self._decomposition_count})"
        )
        return subtasks

    def _parse_output(self, raw: str, original_task: str) -> List[SubTask]:
        """Parse the classifier's JSON output into SubTask objects."""
        # Extract JSON from potentially noisy output
        raw = raw.strip()
        
        # Find JSON array boundaries
        start = raw.find("[")
        end = raw.rfind("]")
        if start == -1 or end == -1:
            logger.warning("Decomposer output is not valid JSON array, creating single task")
            return [self._fallback_single_task(original_task)]

        try:
            items = json.loads(raw[start:end + 1])
        except json.JSONDecodeError as e:
            logger.warning(f"JSON parse error: {e}, falling back to single task")
            return [self._fallback_single_task(original_task)]

        subtasks = []
        for item in items:
            difficulty_raw = item.get("difficulty", 3)
            difficulty = DifficultyLevel(max(1, min(5, int(difficulty_raw))))

            domain_raw = item.get("domain", "general")
            try:
                domain = TaskDomain(domain_raw)
            except ValueError:
                domain = TaskDomain.GENERAL

            subtasks.append(SubTask(
                id=item.get("id", f"s{len(subtasks) + 1}"),
                description=item.get("description", ""),
                difficulty=difficulty,
                domain=domain,
                target_tier=_difficulty_to_tier(difficulty, self._has_large_local),
                confidence=float(item.get("confidence", self._confidence_default)),
                escalate_if_fail=item.get("escalate_if_fail", True),
                parallel_ok=item.get("parallel_ok", False),
                depends_on=item.get("depends_on", []),
            ))

        return subtasks if subtasks else [self._fallback_single_task(original_task)]

    def _fallback_single_task(self, original_task: str) -> SubTask:
        """When decomposition fails, treat the whole thing as one task."""
        return SubTask(
            id="s1",
            description=original_task,
            difficulty=DifficultyLevel.MEDIUM,
            domain=TaskDomain.GENERAL,
            target_tier=ModelTier.LOCAL_MEDIUM,
            confidence=0.5,
            escalate_if_fail=True,
            parallel_ok=False,
            depends_on=[],
        )


# ── Fine-tuning dataset format ──────────────────────────────────────────────

class DecomposerTrainingCollector:
    """
    Collects implicit feedback for fine-tuning the decomposer.
    
    Two signals arrive naturally without human labeling:
    1. Local model fails → subtask was harder than classified (upgrade label)
    2. Premium solves in <200 tokens → subtask was easier than classified (downgrade label)
    """

    def __init__(self):
        self._examples: List[dict] = []

    def record_escalation(self, original_task: str, subtask: SubTask, new_difficulty: int):
        """Record when a subtask needed escalation (was harder than classified)."""
        self._examples.append({
            "input": original_task,
            "subtask_description": subtask.description,
            "original_difficulty": subtask.difficulty,
            "corrected_difficulty": new_difficulty,
            "signal": "escalation",
            "timestamp": time.time(),
        })

    def record_overestimation(self, original_task: str, subtask: SubTask, premium_tokens: int):
        """Record when premium solved easily (was easier than classified)."""
        if premium_tokens < 200:
            self._examples.append({
                "input": original_task,
                "subtask_description": subtask.description,
                "original_difficulty": subtask.difficulty,
                "corrected_difficulty": max(1, subtask.difficulty - 1),
                "signal": "overestimation",
                "premium_tokens_used": premium_tokens,
                "timestamp": time.time(),
            })

    def export_dataset(self) -> List[dict]:
        """Export collected examples in QLoRA training format."""
        return self._examples

    @property
    def size(self) -> int:
        return len(self._examples)
