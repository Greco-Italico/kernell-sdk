"""
Kernell OS SDK — Cognitive Router v2 (Policy-Based Engine)
═══════════════════════════════════════════════════════════
This is the core decision engine for the autonomous economy.
It is NOT a reactive heuristic script. It is a predictable, auditable
Policy Engine that scores models based on:
  1. Task Profile (precision, risk, type)
  2. Context State (RAG score, past failures)
  3. System Policy (max cost, strict locality)

This ensures scaling without LLM hallucinations inside the routing layer.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Optional, List, Dict, Tuple

from .task import Task, TaskType, Complexity
from .agent_role import CognitiveAgent

logger = logging.getLogger("kernell.cognitive.router")


@dataclass
class ModelConfig:
    """Configuration for a registered LLM model."""
    model_id: str
    provider: str
    model_name: str
    cost_per_1k_input: float = 0.0
    cost_per_1k_output: float = 0.0
    max_context: int = 32768
    tags: List[str] = field(default_factory=list)
    is_local: bool = False
    
    # Capability Scores (0.0 to 1.0)
    precision_score: float = 0.8
    reasoning_score: float = 0.5
    code_score: float = 0.5
    
    @property
    def cost_score(self) -> float:
        """Lower is cheaper."""
        return self.cost_per_1k_input + self.cost_per_1k_output


@dataclass
class TaskProfile:
    """Explicit characteristics of the work to be done."""
    task_type: TaskType
    complexity: Complexity
    required_precision: float = 0.5
    risk_level: str = "medium"  # low, medium, high, critical
    latency_sensitivity: str = "low" # low, high

    @classmethod
    def from_task(cls, task: Task) -> TaskProfile:
        precision = {
            Complexity.LOW: 0.5,
            Complexity.MEDIUM: 0.7,
            Complexity.HIGH: 0.9,
            Complexity.CRITICAL: 1.0
        }.get(task.complexity, 0.5)
        
        return cls(
            task_type=task.task_type,
            complexity=task.complexity,
            required_precision=precision,
            risk_level="high" if task.complexity == Complexity.CRITICAL else "medium"
        )


@dataclass
class ContextState:
    """Dynamic state passed to the router for context-aware decisions."""
    rag_match_score: float = 0.0
    graph_confidence: float = 0.0  # From SemanticMemoryGraph Path Intelligence
    graph_relevance: float = 0.0   # Contextual relevance for current task
    past_failures: int = 0
    similar_tasks_solved: int = 0

    @property
    def novelty_score(self) -> float:
        return 1.0 - self.graph_relevance


@dataclass
class SystemPolicy:
    """Global constraints for the router."""
    max_cost_usd: float = 0.05
    prefer_local_models: bool = True
    allow_high_risk_models: bool = False
    enforce_budget: bool = True


@dataclass
class RouterDecision:
    """The auditable result of a routing decision."""
    task_id: str
    selected_model: str
    reason: str
    cost_estimate_usd: float
    strategy_used: str             # "rag_reuse", "direct", "consensus"
    confidence: float
    alternatives_considered: List[str] = field(default_factory=list)
    timestamp: float = field(default_factory=time.time)
    tokens_saved: int = 0

    def to_event(self) -> dict:
        return {
            "task_id": self.task_id,
            "selected_model": self.selected_model,
            "reason": self.reason,
            "cost_estimate": round(self.cost_estimate_usd, 6),
            "strategy": self.strategy_used,
            "confidence": round(self.confidence, 2),
            "tokens_saved": self.tokens_saved
        }


class CognitiveRouter:
    """
    Policy-Based Engine for Model Orchestration.
    Calculates explicit scores for models based on cost, precision, risk, and RAG context.
    """

    def __init__(
        self,
        models: Dict[str, ModelConfig],
        strategy: str = "policy-based",
        policy: Optional[SystemPolicy] = None,
        # Legacy compat params
        cascade_enabled: bool = True,
        consensus_on_critical: bool = True,
        max_cost_per_task_usd: float = 0.50,
    ):
        self._models = models
        self.policy = policy or SystemPolicy(max_cost_usd=max_cost_per_task_usd)
        self._decision_log: List[RouterDecision] = []

    def route(self, task: Task, agent: Optional[CognitiveAgent] = None, context: Optional[ContextState] = None) -> RouterDecision:
        """Core routing algorithm."""
        context = context or ContextState()
        profile = TaskProfile.from_task(task)

        # 1. Immediate RAG resolution (Dual Confidence Bypass)
        final_confidence = context.graph_confidence * context.graph_relevance
        if final_confidence > 0.85:
            # We have a highly confident AND highly relevant architectural path.
            # Pick the cheapest/fastest model just to write the glue code.
            cheap_models = [m for m in self._models.values() if m.is_local or m.cost_score == 0]
            selected = cheap_models[0] if cheap_models else list(self._models.values())[0]
            
            decision = RouterDecision(
                task_id=task.task_id,
                selected_model=selected.model_id,
                reason=f"High Graph Confidence ({context.graph_confidence:.2f})",
                cost_estimate_usd=0.0,
                strategy_used="path_reuse",
                confidence=context.graph_confidence,
                tokens_saved=1500 # Estimated RAG savings
            )
            self._log_decision(decision)
            return decision

        # 2. Filter Models
        candidates = self._filter_models(profile, agent)
        if not candidates:
            candidates = list(self._models.values()) # Fallback

        # 3. Score Models
        scored_models = self._score_models(candidates, profile, context)

        # 4. Select Best
        best_model, confidence = self._select_best(scored_models)

        # Consensus Override for Critical Risk
        if profile.risk_level == "critical" or profile.complexity == Complexity.CRITICAL:
            return self._route_consensus(task, sorted([m for m, _ in scored_models], key=lambda x: x.cost_score, reverse=True))

        # Standard Decision
        decision = RouterDecision(
            task_id=task.task_id,
            selected_model=best_model.model_id,
            reason=f"Policy score: {confidence:.2f}",
            cost_estimate_usd=self._estimate_cost(best_model, profile),
            strategy_used="policy_direct",
            confidence=confidence,
            alternatives_considered=[m.model_id for m, _ in scored_models if m.model_id != best_model.model_id]
        )
        self._log_decision(decision)
        return decision

    def _filter_models(self, profile: TaskProfile, agent: Optional[CognitiveAgent]) -> List[ModelConfig]:
        """Apply hard constraints to eliminate invalid models."""
        candidates = []
        for m in self._models.values():
            # 1. Budget constraint
            if self.policy.enforce_budget and self._estimate_cost(m, profile) > self.policy.max_cost_usd:
                continue
            # 2. Precision constraint
            if m.precision_score < profile.required_precision and profile.complexity in (Complexity.HIGH, Complexity.CRITICAL):
                continue
            candidates.append(m)
            
        # Agent explicitly requested a specific model (respect if it passes filters or if list is empty)
        if agent and agent.model_id and agent.model_id in self._models:
            agent_model = self._models[agent.model_id]
            if agent_model not in candidates:
                candidates.append(agent_model)
                
        return candidates

    def _score_models(self, models: List[ModelConfig], profile: TaskProfile, context: ContextState) -> List[Tuple[ModelConfig, float]]:
        """Calculate quantitative policy score for each model."""
        results = []
        
        # Normalize costs for scoring
        max_cost = max([m.cost_score for m in models] + [0.0001])
        
        for m in models:
            score = 0.0
            
            # 1. Cost (Cheaper is better, +0.0 to +1.0)
            cost_factor = 1.0 - (m.cost_score / max_cost)
            score += cost_factor * 2.0
            
            # 2. Locality Bonus
            if self.policy.prefer_local_models and m.is_local:
                score += 1.5
                
            # 3. Precision Match (Is it capable enough?)
            if profile.task_type in (TaskType.PLAN, TaskType.REASON):
                score += m.reasoning_score * 2.0
            elif profile.task_type == TaskType.CODE:
                score += m.code_score * 2.0
            else:
                score += m.precision_score
                
            # 4. Context Penalties & Novelty Boosts
            if context.past_failures > 0 and m.is_local:
                # Exponential penalty to force cascade up
                score -= (context.past_failures ** 2) * 2.0
                
            # If the context is highly novel, boost models with high reasoning scores
            if context.novelty_score > 0.6:
                score += m.reasoning_score * (context.novelty_score * 2.0)
                
            results.append((m, score))
            
        return results

    def _select_best(self, scored: List[Tuple[ModelConfig, float]]) -> Tuple[ModelConfig, float]:
        """Return the model with the highest policy score."""
        best = max(scored, key=lambda x: x[1])
        # Normalize confidence to 0-1 range heuristically
        confidence = min(1.0, max(0.1, best[1] / 6.0))
        return best[0], confidence

    def _route_consensus(self, task: Task, sorted_capable_models: List[ModelConfig]) -> RouterDecision:
        """Critical tasks require multiple models."""
        consensus_models = sorted_capable_models[:2] if len(sorted_capable_models) >= 2 else sorted_capable_models
        primary = consensus_models[0]

        decision = RouterDecision(
            task_id=task.task_id,
            selected_model=primary.model_id,
            reason=f"consensus_required ({len(consensus_models)} models)",
            cost_estimate_usd=sum(self._estimate_cost(m, TaskProfile.from_task(task)) for m in consensus_models),
            strategy_used="consensus",
            confidence=0.99,
            alternatives_considered=[m.model_id for m in consensus_models[1:]]
        )
        self._log_decision(decision)
        return decision

    def _estimate_cost(self, model: ModelConfig, profile: TaskProfile) -> float:
        """Estimate USD cost based on task complexity."""
        est_tokens = {
            Complexity.LOW: 500,
            Complexity.MEDIUM: 1000,
            Complexity.HIGH: 2000,
            Complexity.CRITICAL: 4000,
        }.get(profile.complexity, 1000)

        input_cost = (est_tokens / 1000) * model.cost_per_1k_input
        output_cost = (est_tokens / 1000) * model.cost_per_1k_output
        return input_cost + output_cost

    def _log_decision(self, decision: RouterDecision):
        self._decision_log.append(decision)
        logger.info(f"Router v2: {decision.task_id} → {decision.selected_model} [{decision.strategy_used}] (conf: {decision.confidence:.2f})")

    def get_decision_log(self) -> List[dict]:
        return [d.to_event() for d in self._decision_log]
