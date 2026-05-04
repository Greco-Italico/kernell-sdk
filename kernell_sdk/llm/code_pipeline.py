"""
Kernell OS SDK — Code Pipeline (4-Phase Multi-Model)
═════════════════════════════════════════════════════
Produces Opus-quality code using cheaper models through structured
multi-phase reasoning. Ported from Kernell OS core/code/code_pipeline.py.

Pipeline:
  1. ARCHITECT (reasoning model): Analyze problem, design decisions, edge cases.
     Does NOT write code. Only thinks.
  2. IMPLEMENTER (capable model): Write code based on architect's analysis.
  3. CRITIC (fast model): Find bugs, edge cases, SOLID violations, hardcoded values.
  4. REFINER (reasoning model): Fix issues found by critic. Final version.

Why this works:
  Single-shot models produce code that "looks right" but has subtle bugs.
  This pipeline makes the implicit reasoning steps explicit, catching
  errors at each phase before they compound.

Usage:
    from kernell_sdk.llm.code_pipeline import CodePipeline

    pipeline = CodePipeline(registry=my_llm_registry)
    result = pipeline.run(
        task="Create a rate limiter with sliding window",
        context="Python 3.12, no external deps",
    )
    print(result.final_code)
    print(result.architect_reasoning)
    print(result.critic_feedback)
"""

import logging
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

logger = logging.getLogger("kernell.code_pipeline")


@dataclass
class PipelineResult:
    """Complete output of the 4-phase code pipeline."""
    # Final output
    final_code: str = ""
    success: bool = False

    # Phase outputs
    architect_reasoning: str = ""
    implementer_code: str = ""
    critic_feedback: str = ""
    refiner_changes: str = ""

    # Metadata
    total_tokens: int = 0
    total_latency_ms: float = 0.0
    phases_completed: int = 0
    models_used: List[str] = field(default_factory=list)
    errors: List[str] = field(default_factory=list)


# ── Phase Prompts ────────────────────────────────────────────────────

ARCHITECT_SYSTEM = """You are a senior software architect. Your job is to THINK, not to code.

Given a programming task, produce:
1. DESIGN DECISIONS: Key architectural choices and why.
2. EDGE CASES: What could go wrong? What inputs are tricky?
3. DATA STRUCTURES: What types/classes are needed?
4. ALGORITHM SKETCH: High-level flow (pseudocode acceptable).
5. RISKS: Security concerns, performance pitfalls, maintainability issues.

DO NOT write any implementation code. Only analysis and design.
Be thorough. The implementer will rely entirely on your analysis."""

IMPLEMENTER_SYSTEM = """You are an expert programmer. Write production-quality code.

You will receive:
- A task description
- An architect's analysis with design decisions and edge cases

Write the COMPLETE implementation based on the architect's design.
Rules:
- Production quality. No TODOs, no placeholders.
- Handle all edge cases identified by the architect.
- Follow the data structures and algorithm the architect specified.
- Include type hints, docstrings, and error handling.
- Output ONLY the code, no explanations."""

CRITIC_SYSTEM = """You are a ruthless code reviewer. Find EVERY problem.

Analyze the provided code for:
1. BUGS: Logic errors, off-by-one, null handling, race conditions.
2. EDGE CASES: Unhandled inputs, boundary conditions.
3. SECURITY: Injection, overflow, unsafe operations.
4. PERFORMANCE: O(n²) when O(n) is possible, unnecessary allocations.
5. STYLE: Non-idiomatic patterns, naming issues, missing docs.
6. SOLID: Violations of Single Responsibility, Open-Closed, etc.

For each issue:
- Quote the problematic line
- Explain why it's wrong
- Suggest the fix

If the code is perfect (rare), say "NO ISSUES FOUND" and explain why."""

REFINER_SYSTEM = """You are a senior developer performing the final revision.

You will receive:
- The original code
- A critic's feedback with specific issues

Apply ALL valid fixes from the critic's feedback.
If a critique is wrong, ignore it but note why.

Output the COMPLETE corrected code. Not a diff — the full file.
After the code, add a brief "CHANGES MADE" section listing what you fixed."""


class CodePipeline:
    """
    4-phase multi-model code generation pipeline.
    Produces significantly higher quality than single-shot generation.
    """

    def __init__(self, registry, max_tokens_per_phase: int = 4096):
        """
        Args:
            registry: LLMProviderRegistry instance
            max_tokens_per_phase: Max tokens per LLM call
        """
        self._registry = registry
        self._max_tokens = max_tokens_per_phase

    def run(
        self,
        task: str,
        context: str = "",
        skip_refiner: bool = False,
    ) -> PipelineResult:
        """
        Execute the full pipeline: Architect → Implementer → Critic → Refiner.

        Args:
            task: What to build (natural language description)
            context: Additional context (language, constraints, existing code)
            skip_refiner: If True, skip phase 4 (faster, slightly less refined)

        Returns:
            PipelineResult with the final code and all intermediate outputs.
        """
        result = PipelineResult()
        t0 = time.time()

        # ── Phase 1: ARCHITECT ───────────────────────────────────────
        logger.info("[CodePipeline] Phase 1: ARCHITECT")
        architect_prompt = f"TASK:\n{task}"
        if context:
            architect_prompt += f"\n\nCONTEXT:\n{context}"

        arch_resp = self._registry.complete(
            messages=[{"role": "user", "content": architect_prompt}],
            system_prompt=ARCHITECT_SYSTEM,
            role="architect",
            max_tokens=self._max_tokens,
            temperature=0.3,
        )

        if not arch_resp:
            result.errors.append("Phase 1 (Architect) failed: all providers down")
            return result

        result.architect_reasoning = arch_resp.content
        if arch_resp.reasoning_trace:
            result.architect_reasoning = arch_resp.reasoning_trace + "\n---\n" + arch_resp.content
        result.total_tokens += arch_resp.tokens_used
        result.models_used.append(arch_resp.provider_used)
        result.phases_completed = 1
        logger.info(f"[CodePipeline] Phase 1 OK ({arch_resp.tokens_used} tokens, {arch_resp.provider_used})")

        # ── Phase 2: IMPLEMENTER ─────────────────────────────────────
        logger.info("[CodePipeline] Phase 2: IMPLEMENTER")
        impl_prompt = (
            f"TASK:\n{task}\n\n"
            f"ARCHITECT'S ANALYSIS:\n{result.architect_reasoning}"
        )
        if context:
            impl_prompt += f"\n\nCONTEXT:\n{context}"

        impl_resp = self._registry.complete(
            messages=[{"role": "user", "content": impl_prompt}],
            system_prompt=IMPLEMENTER_SYSTEM,
            role="implementer",
            max_tokens=self._max_tokens,
            temperature=0.2,
        )

        if not impl_resp:
            result.errors.append("Phase 2 (Implementer) failed: all providers down")
            return result

        result.implementer_code = impl_resp.content
        result.total_tokens += impl_resp.tokens_used
        result.models_used.append(impl_resp.provider_used)
        result.phases_completed = 2
        logger.info(f"[CodePipeline] Phase 2 OK ({impl_resp.tokens_used} tokens, {impl_resp.provider_used})")

        # ── Phase 3: CRITIC ──────────────────────────────────────────
        logger.info("[CodePipeline] Phase 3: CRITIC")
        critic_prompt = (
            f"ORIGINAL TASK:\n{task}\n\n"
            f"CODE TO REVIEW:\n```\n{result.implementer_code}\n```"
        )

        critic_resp = self._registry.complete(
            messages=[{"role": "user", "content": critic_prompt}],
            system_prompt=CRITIC_SYSTEM,
            role="critic",
            max_tokens=self._max_tokens,
            temperature=0.3,
        )

        if not critic_resp:
            # Critic failed but we still have implementer code
            result.final_code = result.implementer_code
            result.success = True
            result.errors.append("Phase 3 (Critic) failed — using implementer output")
            return result

        result.critic_feedback = critic_resp.content
        result.total_tokens += critic_resp.tokens_used
        result.models_used.append(critic_resp.provider_used)
        result.phases_completed = 3
        logger.info(f"[CodePipeline] Phase 3 OK ({critic_resp.tokens_used} tokens, {critic_resp.provider_used})")

        # Check if critic found no issues
        if "NO ISSUES FOUND" in result.critic_feedback.upper():
            result.final_code = result.implementer_code
            result.success = True
            result.total_latency_ms = round((time.time() - t0) * 1000, 1)
            logger.info("[CodePipeline] Critic found no issues — using implementer output directly")
            return result

        if skip_refiner:
            result.final_code = result.implementer_code
            result.success = True
            result.total_latency_ms = round((time.time() - t0) * 1000, 1)
            return result

        # ── Phase 4: REFINER ─────────────────────────────────────────
        logger.info("[CodePipeline] Phase 4: REFINER")
        refiner_prompt = (
            f"ORIGINAL CODE:\n```\n{result.implementer_code}\n```\n\n"
            f"CRITIC'S FEEDBACK:\n{result.critic_feedback}"
        )

        refiner_resp = self._registry.complete(
            messages=[{"role": "user", "content": refiner_prompt}],
            system_prompt=REFINER_SYSTEM,
            role="refiner",
            max_tokens=self._max_tokens,
            temperature=0.2,
        )

        if not refiner_resp:
            # Refiner failed, use implementer code
            result.final_code = result.implementer_code
            result.success = True
            result.errors.append("Phase 4 (Refiner) failed — using implementer output")
        else:
            result.refiner_changes = refiner_resp.content
            result.final_code = refiner_resp.content
            result.total_tokens += refiner_resp.tokens_used
            result.models_used.append(refiner_resp.provider_used)
            result.phases_completed = 4
            result.success = True
            logger.info(f"[CodePipeline] Phase 4 OK ({refiner_resp.tokens_used} tokens, {refiner_resp.provider_used})")

        result.total_latency_ms = round((time.time() - t0) * 1000, 1)
        logger.info(
            f"[CodePipeline] COMPLETE — {result.phases_completed} phases, "
            f"{result.total_tokens} total tokens, {result.total_latency_ms}ms"
        )
        return result
