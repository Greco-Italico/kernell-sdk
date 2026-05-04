"""
Kernell OS SDK — Cost Estimator (Pre-execution Simulation)
═══════════════════════════════════════════════════════════
Estimates cost BEFORE executing a task.

This reduces user anxiety ("how much will this cost?") and
enables budget-aware routing decisions.

Usage:
    estimator = CostEstimator(registry, metrics)
    preview = estimator.estimate("Build a REST API with JWT")
    # → {"estimated_cost": 0.07, "likely_layers": ["local", "cheap_api"], ...}
"""
from __future__ import annotations

import logging
from typing import Dict, List, Optional, Protocol

from .types import ModelTier, DifficultyLevel
from .metrics import API_COST_TABLE

logger = logging.getLogger("kernell.router.estimator")


# Average tokens per difficulty level (empirical estimates)
TOKENS_BY_DIFFICULTY = {
    DifficultyLevel.TRIVIAL: {"in": 200, "out": 100},
    DifficultyLevel.EASY:    {"in": 500, "out": 300},
    DifficultyLevel.MEDIUM:  {"in": 1000, "out": 600},
    DifficultyLevel.HARD:    {"in": 2000, "out": 1200},
    DifficultyLevel.EXPERT:  {"in": 4000, "out": 2500},
}

# Estimated probability of escalation per difficulty level
ESCALATION_PROBABILITY = {
    DifficultyLevel.TRIVIAL: 0.02,
    DifficultyLevel.EASY:    0.08,
    DifficultyLevel.MEDIUM:  0.15,
    DifficultyLevel.HARD:    0.35,
    DifficultyLevel.EXPERT:  0.90,
}


class LLMBackend(Protocol):
    def generate(self, prompt: str, system: str = "") -> str: ...


class CostEstimator:
    """
    Estimates cost and layer distribution before executing a task.
    
    Uses the decomposer to break down the task, then applies
    cost tables and escalation probabilities to produce a preview.
    """

    def __init__(
        self,
        decomposer=None,
        cost_table: Optional[Dict] = None,
        historical_escalation_rate: float = 0.15,
    ):
        self._decomposer = decomposer
        self._cost_table = cost_table or API_COST_TABLE
        self._historical_rate = historical_escalation_rate

    def estimate(self, task: str) -> dict:
        """
        Estimate cost and layer distribution for a task.
        
        If decomposer is available, uses real decomposition.
        Otherwise, uses heuristic estimation.
        """
        if self._decomposer:
            return self._estimate_with_decomposer(task)
        return self._estimate_heuristic(task)

    def _estimate_with_decomposer(self, task: str) -> dict:
        """Use the real decomposer for accurate estimation."""
        subtasks = self._decomposer.decompose(task)

        total_cost = 0.0
        premium_cost = 0.0
        layers_used = set()
        tier_breakdown = {}

        for sub in subtasks:
            tokens = TOKENS_BY_DIFFICULTY.get(sub.difficulty, {"in": 1000, "out": 600})
            esc_prob = ESCALATION_PROBABILITY.get(sub.difficulty, 0.15)

            # Base cost at predicted tier
            base_cost = self._tier_cost(sub.target_tier.value, tokens["in"], tokens["out"])

            # Expected escalation cost
            esc_cost = 0.0
            if sub.escalate_if_fail:
                next_tier = self._next_tier(sub.target_tier)
                if next_tier:
                    esc_cost = self._tier_cost(next_tier, tokens["in"], tokens["out"]) * esc_prob

            subtask_cost = base_cost + esc_cost
            total_cost += subtask_cost

            # Premium baseline
            premium_cost += self._tier_cost("premium_api", tokens["in"], tokens["out"])

            layers_used.add(sub.target_tier.value)
            tier_breakdown[sub.target_tier.value] = tier_breakdown.get(sub.target_tier.value, 0) + 1

        premium_probability = sum(
            ESCALATION_PROBABILITY.get(s.difficulty, 0.15)
            for s in subtasks
            if s.difficulty >= DifficultyLevel.HARD
        ) / max(len(subtasks), 1)

        return {
            "estimated_cost_usd": round(total_cost, 4),
            "premium_only_cost_usd": round(premium_cost, 4),
            "estimated_savings_usd": round(premium_cost - total_cost, 4),
            "estimated_savings_percent": (
                round((1 - total_cost / premium_cost) * 100, 1)
                if premium_cost > 0 else 100.0
            ),
            "num_subtasks": len(subtasks),
            "likely_layers": sorted(layers_used),
            "tier_breakdown": tier_breakdown,
            "premium_probability": round(premium_probability, 2),
            "confidence": "high" if len(subtasks) > 1 else "medium",
        }

    def _estimate_heuristic(self, task: str) -> dict:
        """Fallback estimation without decomposer."""
        words = len(task.split())
        if words < 20:
            est_subtasks = 2
            avg_difficulty = DifficultyLevel.EASY
        elif words < 100:
            est_subtasks = 5
            avg_difficulty = DifficultyLevel.MEDIUM
        else:
            est_subtasks = 10
            avg_difficulty = DifficultyLevel.HARD

        tokens = TOKENS_BY_DIFFICULTY[avg_difficulty]
        local_cost = 0.0  # Local is free
        total_tokens_est = (tokens["in"] + tokens["out"]) * est_subtasks
        premium_cost = self._tier_cost("premium_api", tokens["in"] * est_subtasks, tokens["out"] * est_subtasks)
        esc_cost = premium_cost * self._historical_rate

        return {
            "estimated_cost_usd": round(esc_cost, 4),
            "premium_only_cost_usd": round(premium_cost, 4),
            "estimated_savings_usd": round(premium_cost - esc_cost, 4),
            "estimated_savings_percent": round((1 - esc_cost / max(premium_cost, 0.001)) * 100, 1),
            "num_subtasks": est_subtasks,
            "likely_layers": ["local_medium"],
            "tier_breakdown": {"local_medium": est_subtasks},
            "premium_probability": round(self._historical_rate, 2),
            "confidence": "low",
        }

    def _tier_cost(self, tier: str, tokens_in: int, tokens_out: int) -> float:
        rates = self._cost_table.get(tier, {"input": 0.0, "output": 0.0})
        return (tokens_in / 1_000_000 * rates["input"]) + (tokens_out / 1_000_000 * rates["output"])

    def _next_tier(self, current: ModelTier) -> Optional[str]:
        escalation_chain = {
            ModelTier.LOCAL_NANO: "local_small",
            ModelTier.LOCAL_SMALL: "local_medium",
            ModelTier.LOCAL_MEDIUM: "local_large",
            ModelTier.LOCAL_LARGE: "cheap_api",
            ModelTier.CHEAP_API: "premium_api",
            ModelTier.PREMIUM_API: None,
        }
        return escalation_chain.get(current)
