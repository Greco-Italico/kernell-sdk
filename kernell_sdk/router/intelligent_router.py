"""
Kernell OS SDK — Intelligent Router (3-Layer Token Economy Engine)
══════════════════════════════════════════════════════════════════
The main orchestrator that ties all components together.

Pipeline per subtask:
  INPUT → Decompose → Cache Check → Local Exec → Verify → [Cheap API] → [Premium API]

Each layer acts as a gate: if the current layer succeeds, we NEVER
touch the more expensive layer. This is how we achieve 85-95% cost
reduction compared to sending everything to premium APIs.

Integrates with:
  - SemanticCache (cognitive/semantic_cache.py) for deduplication
  - RollingSummarizer for context compression
  - SelfVerifier for escalation prevention
  - DecomposerTrainingCollector for automatic fine-tuning feedback
"""
from __future__ import annotations

import logging
import time
from typing import Dict, List, Optional, Protocol

from .types import (
    SubTask, ExecutionResult, RouterStats,
    ModelTier, DifficultyLevel, PolicyRoute, TaskDomain,
)
from .decomposer import TaskDecomposer, DecomposerTrainingCollector
from .summarizer import RollingSummarizer
from .verifier import SelfVerifier
from .telemetry_collector import TelemetryCollector
from .classifier_pro import ClassifierProClient, ProClassification
from .policy_lite import PolicyLiteClient

logger = logging.getLogger("kernell.router.engine")


class LLMBackend(Protocol):
    """Protocol for any LLM backend (local or API)."""
    def generate(self, prompt: str, system: str = "") -> str: ...


class CacheBackend(Protocol):
    """Protocol compatible with cognitive/semantic_cache.py"""
    def query(self, prompt: str, model: str = "") -> Optional[object]: ...
    def store(self, prompt: str, response: str, model_used: str = "", tokens_used: int = 0) -> None: ...


class IntelligentRouter:
    """
    3-Layer Token Economy Engine.
    
    Layer 1 (LOCAL):     Nano/Small/Medium/Large local models via Ollama
    Layer 2 (CHEAP API): DeepSeek, Groq, Gemini Flash — $0.07-0.55/M tokens
    Layer 3 (PREMIUM):   Claude Opus, GPT-5, Gemini Pro — last resort only
    
    Usage:
        router = IntelligentRouter(
            classifier=classifier_model,
            local_models={"local_nano": nano_llm, "local_medium": medium_llm},
            cheap_api=deepseek_client,
            premium_api=claude_client,
            cache=semantic_cache,
        )
        results = router.execute("Build a REST API with JWT auth")
    """

    def __init__(
        self,
        classifier: LLMBackend,
        local_models: Dict[str, LLMBackend],
        cheap_api: Optional[LLMBackend] = None,
        premium_api: Optional[LLMBackend] = None,
        cache: Optional[CacheBackend] = None,
        summarizer_model: Optional[LLMBackend] = None,
        verifier_model: Optional[LLMBackend] = None,
        verify_confidence_threshold: float = 0.70,
        has_large_local: bool = False,
        monthly_budget_usd: Optional[float] = None,
        telemetry: Optional[TelemetryCollector] = None,
        classifier_pro: Optional[ClassifierProClient] = None,
        policy_lite: Optional[PolicyLiteClient] = None,
        hardware_tier: str = "unknown",
        ram_gb: int = 0,
        has_gpu: bool = False,
    ):
        # Core components
        self._decomposer = TaskDecomposer(
            model=classifier,
            has_large_local=has_large_local,
        )
        self._local_models = local_models
        self._cheap_api = cheap_api
        self._premium_api = premium_api
        self._cache = cache

        # Anti-waste layers
        self._summarizer = (
            RollingSummarizer(summarizer_model)
            if summarizer_model else None
        )
        self._verifier = (
            SelfVerifier(verifier_model or classifier, verify_confidence_threshold)
            if (verifier_model or classifier) else None
        )

        # Feedback collection for fine-tuning
        self._training_collector = DecomposerTrainingCollector()

        # Data Flywheel & Cloud API
        self._telemetry = telemetry or TelemetryCollector()
        self._classifier_pro = classifier_pro
        self._policy_lite = policy_lite
        self._hardware_tier = hardware_tier
        self._ram_gb = ram_gb
        self._has_gpu = has_gpu

        # MCTS Reasoning Engine (System 2)
        if self._policy_lite and self._verifier:
            try:
                from .mcts_engine import MCTSEngine
                self._mcts_engine = MCTSEngine(
                    policy_lite=self._policy_lite,
                    decomposer=self._decomposer,
                    verifier=self._verifier,
                    num_simulations=3
                )
            except ImportError:
                self._mcts_engine = None
        else:
            self._mcts_engine = None

        # Budget tracking
        self._budget_usd = monthly_budget_usd
        self._spent_usd = 0.0

        # Statistics
        self._stats = RouterStats()

    def execute(self, task: str) -> List[ExecutionResult]:
        """
        Execute a full task through the 3-layer pipeline.
        
        1. Decompose into atomic subtasks
        2. For each subtask (respecting dependencies):
           a. Check semantic cache
           b. Try local model
           c. Self-verify output
           d. Escalate if needed (cheap API → premium API)
        3. Compress context between steps
        4. Collect training feedback
        
        Returns list of ExecutionResult for each subtask.
        """
        logger.info(f"Router: executing task ({len(task)} chars)")
        t0 = time.monotonic()

        # Step 1: Policy-Lite decision (optional, with MCTS)
        pre_computed_subtasks = None
        if hasattr(self, '_mcts_engine') and self._mcts_engine:
            logger.info("Engaging System 2: MCTS Routing Engine")
            optimal_node = self._mcts_engine.search_optimal_path(task)
            policy_decision = optimal_node.decision
            pre_computed_subtasks = optimal_node.subtasks
        else:
            policy_decision = (
                self._policy_lite.decide(task)
                if self._policy_lite else None
            )

        # Step 1.1: Build execution plan
        fallback_trigger = ""
        if policy_decision and not policy_decision.needs_decomposition:
            tier = self._route_to_tier(policy_decision.route)
            subtasks = [SubTask(
                id="s1",
                description=task,
                difficulty=DifficultyLevel.MEDIUM,
                domain=TaskDomain.GENERAL,
                target_tier=tier,
                confidence=policy_decision.confidence,
                escalate_if_fail=True,
                parallel_ok=False,
                depends_on=[],
            )]
            logger.info(f"Policy-Lite selected direct route={policy_decision.route.value}")
        else:
            # Fallback/default path: decompose task using local decomposer
            if pre_computed_subtasks:
                subtasks = pre_computed_subtasks
                logger.info(f"Using {len(subtasks)} subtasks from MCTS pre-computation")
            else:
                subtasks = self._decomposer.decompose(task)
                logger.info(f"Decomposed into {len(subtasks)} subtasks locally")
            
            if policy_decision and policy_decision.route != PolicyRoute.HYBRID:
                fallback_trigger = "policy_route_override"
                forced_tier = self._route_to_tier(policy_decision.route)
                for st in subtasks:
                    st.target_tier = forced_tier

        # Step 1.5: Escalation Check (Classifier-Pro API)
        if self._classifier_pro:
            avg_conf = sum(s.confidence for s in subtasks) / max(1, len(subtasks))
            max_diff = max((s.difficulty for s in subtasks), default=1)
            
            if self._classifier_pro.should_consult_pro(avg_conf, max_diff):
                logger.info("Escalating routing decision to Classifier-Pro API")
                pro_decision = self._classifier_pro.classify(
                    task=task,
                    local_subtasks=[s.__dict__ for s in subtasks],
                    hardware_tier=self._hardware_tier,
                    ram_gb=self._ram_gb,
                    has_gpu=self._has_gpu,
                )
                
                # Rehydrate subtasks from API decision
                if pro_decision.subtasks:
                    subtasks = [SubTask(**s) for s in pro_decision.subtasks]
                    logger.info(f"Classifier-Pro returned {len(subtasks)} optimized subtasks")

        # Step 2: Execute in dependency order
        results: List[ExecutionResult] = []
        completed_ids: set = set()

        # Reset summarizer for new task chain
        if self._summarizer:
            self._summarizer.reset()

        for subtask in self._topological_sort(subtasks):
            result = self._execute_subtask(subtask, task)
            results.append(result)
            completed_ids.add(subtask.id)

            # Record telemetry for Data Flywheel
            self._telemetry.record_from_result(
                task=task,
                subtask_desc=subtask.description,
                predicted_difficulty=subtask.difficulty,
                predicted_tier=subtask.target_tier.value,
                confidence=subtask.confidence,
                result=result,
                hardware_tier=self._hardware_tier,
                has_gpu=self._has_gpu,
                ram_gb=self._ram_gb,
                policy_decision=policy_decision,
                final_route_used=result.tier_used.value if hasattr(result, "tier_used") else "",
                fallback_trigger=fallback_trigger,
            )

            # Feed result to summarizer
            if self._summarizer and result.success:
                self._summarizer.add_step_output(subtask.id, result.output)

        elapsed = (time.monotonic() - t0) * 1000
        logger.info(
            f"Router: completed {len(results)} subtasks in {elapsed:.0f}ms | "
            f"local={self._stats.local_executions} cheap={self._stats.cheap_api_executions} "
            f"premium={self._stats.premium_api_executions} cached={self._stats.cache_hits}"
        )

        return results

    def _execute_subtask(self, subtask: SubTask, original_task: str) -> ExecutionResult:
        """Execute a single subtask through the layered pipeline."""
        self._stats.total_subtasks += 1

        # ── Gate 0: Semantic Cache ───────────────────────────────────
        if self._cache:
            cached = self._cache.query(subtask.description)
            if cached:
                self._stats.cache_hits += 1
                logger.debug(f"Cache HIT for {subtask.id}")
                response_text = cached.response if hasattr(cached, 'response') else str(cached)
                return ExecutionResult(
                    subtask_id=subtask.id,
                    output=response_text,
                    success=True,
                    model_used="cache",
                    tier_used=subtask.target_tier,
                    was_cached=True,
                )

        # Build context with compression
        context = subtask.description
        if self._summarizer:
            context = self._summarizer.get_context_for_step(subtask.description)

        # ── Gate 1: Local Execution ──────────────────────────────────
        local_model = self._get_local_model(subtask.target_tier)
        if local_model:
            t0 = time.monotonic()
            output = local_model.generate(context)
            latency = (time.monotonic() - t0) * 1000

            # Self-verify before accepting
            if self._verifier:
                check = self._verifier.verify(subtask.description, output)
                if check.should_accept(self._verifier._threshold):
                    self._stats.local_executions += 1
                    self._cache_result(subtask.description, output, "local")
                    return ExecutionResult(
                        subtask_id=subtask.id,
                        output=output,
                        success=True,
                        model_used=subtask.target_tier.value,
                        tier_used=subtask.target_tier,
                        confidence=check.confidence,
                        latency_ms=latency,
                    )
                else:
                    logger.info(
                        f"Verifier rejected local output for {subtask.id} "
                        f"(confidence={check.confidence:.2f}), escalating"
                    )
            else:
                # No verifier — accept local output directly
                self._stats.local_executions += 1
                self._cache_result(subtask.description, output, "local")
                return ExecutionResult(
                    subtask_id=subtask.id,
                    output=output,
                    success=True,
                    model_used=subtask.target_tier.value,
                    tier_used=subtask.target_tier,
                    latency_ms=latency,
                )

        # ── Gate 2: Cheap API ────────────────────────────────────────
        if self._cheap_api and subtask.escalate_if_fail:
            if self._budget_check(estimated_cost=0.001):
                t0 = time.monotonic()
                output = self._cheap_api.generate(context)
                latency = (time.monotonic() - t0) * 1000

                # Verify cheap API output too
                accepted = True
                confidence = 0.8
                if self._verifier:
                    check = self._verifier.verify(subtask.description, output)
                    accepted = check.should_accept(self._verifier._threshold)
                    confidence = check.confidence

                if accepted:
                    self._stats.cheap_api_executions += 1
                    self._stats.escalations += 1
                    self._spent_usd += 0.001
                    self._cache_result(subtask.description, output, "cheap_api")

                    # Training feedback: escalation signal
                    self._training_collector.record_escalation(
                        original_task, subtask,
                        min(5, subtask.difficulty + 1),
                    )

                    return ExecutionResult(
                        subtask_id=subtask.id,
                        output=output,
                        success=True,
                        model_used="cheap_api",
                        tier_used=ModelTier.CHEAP_API,
                        confidence=confidence,
                        latency_ms=latency,
                        escalated_from=subtask.target_tier,
                    )

        # ── Gate 3: Premium API (last resort) ────────────────────────
        if self._premium_api:
            if self._budget_check(estimated_cost=0.01):
                t0 = time.monotonic()
                output = self._premium_api.generate(context)
                latency = (time.monotonic() - t0) * 1000

                self._stats.premium_api_executions += 1
                self._stats.escalations += 1
                self._spent_usd += 0.01
                self._cache_result(subtask.description, output, "premium_api")

                # Training feedback: overestimation check
                tokens_approx = len(output.split()) * 2
                self._training_collector.record_overestimation(
                    original_task, subtask, tokens_approx,
                )

                return ExecutionResult(
                    subtask_id=subtask.id,
                    output=output,
                    success=True,
                    model_used="premium_api",
                    tier_used=ModelTier.PREMIUM_API,
                    latency_ms=latency,
                    escalated_from=subtask.target_tier,
                )

        # All gates failed
        logger.error(f"All execution layers failed for {subtask.id}")
        return ExecutionResult(
            subtask_id=subtask.id,
            output="",
            success=False,
            model_used="none",
            tier_used=subtask.target_tier,
        )

    def _get_local_model(self, tier: ModelTier) -> Optional[LLMBackend]:
        """Find the best available local model for a tier."""
        # Try exact tier match first
        if tier.value in self._local_models:
            return self._local_models[tier.value]

        # Fallback: try any available local model
        tier_priority = [
            ModelTier.LOCAL_LARGE, ModelTier.LOCAL_MEDIUM,
            ModelTier.LOCAL_SMALL, ModelTier.LOCAL_NANO,
        ]
        for t in tier_priority:
            if t.value in self._local_models:
                return self._local_models[t.value]

        return None

    def _cache_result(self, prompt: str, response: str, model: str) -> None:
        """Store result in semantic cache if available."""
        if self._cache:
            self._cache.store(prompt, response, model_used=model)

    @staticmethod
    def _route_to_tier(route: PolicyRoute) -> ModelTier:
        mapping = {
            PolicyRoute.LOCAL: ModelTier.LOCAL_SMALL,
            PolicyRoute.CHEAP: ModelTier.CHEAP_API,
            PolicyRoute.PREMIUM: ModelTier.PREMIUM_API,
            PolicyRoute.HYBRID: ModelTier.LOCAL_MEDIUM,
        }
        return mapping.get(route, ModelTier.LOCAL_MEDIUM)

    def _budget_check(self, estimated_cost: float) -> bool:
        """Check if we have budget remaining."""
        if self._budget_usd is None:
            return True
        return (self._spent_usd + estimated_cost) <= self._budget_usd

    def _topological_sort(self, subtasks: List[SubTask]) -> List[SubTask]:
        """Sort subtasks respecting dependency order."""
        by_id = {s.id: s for s in subtasks}
        visited = set()
        result = []

        def visit(s: SubTask):
            if s.id in visited:
                return
            visited.add(s.id)
            for dep_id in s.depends_on:
                if dep_id in by_id:
                    visit(by_id[dep_id])
            result.append(s)

        for s in subtasks:
            visit(s)

        return result

    @property
    def stats(self) -> RouterStats:
        return self._stats

    @property
    def training_data(self) -> DecomposerTrainingCollector:
        return self._training_collector

    @property
    def spent_usd(self) -> float:
        return self._spent_usd
