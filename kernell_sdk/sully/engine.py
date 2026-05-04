"""
Kernell OS SDK — Sully Engine (Compute Allocation Core)
══════════════════════════════════════════════════════
The brain of the economic scheduling system.

Sully decides: which model, which tier, what strategy.
Sully does NOT decide: what to do (that's the Planner).

Architecture:
    TaskFeatures + MarketRates + Budget → SullyDecision

In v0 (heuristic mode), Sully uses deterministic rules.
In v1+ (fine-tuned mode), Sully uses a local LLM trained on production telemetry.
"""

import logging
from typing import Dict, List, Optional

from kernell_sdk.sully.types import (
    TaskFeatures, SullyDecision, ModelMarketInfo, Tier, ExecutionResult
)
from kernell_sdk.sully.market import ModelMarketRegistry
from kernell_sdk.observability.event_bus import GLOBAL_EVENT_BUS

logger = logging.getLogger("kernell.sully.engine")


class SullyEngine:
    """
    Compute Allocation Engine.
    
    Policy: LOCAL → ECONOMIC → PREMIUM (minimize cost, subject to quality).
    
    v0: deterministic heuristic (ships today, no LLM needed)
    v1: fine-tuned Llama 3 8B making structured JSON decisions
    """
    
    def __init__(
        self,
        market: ModelMarketRegistry,
        model_client=None,  # for v1: local LLM inference
        mode: str = "heuristic",  # "heuristic" or "model"
        shadow_client=None, # For Shadow Deployment Eval Gate
    ):
        self.market = market
        self.model_client = model_client
        self.mode = mode
        self.shadow_client = shadow_client
    
    def decide(
        self,
        features: TaskFeatures,
        budget_cap: float = 1.0,
    ) -> SullyDecision:
        """
        Given task features and market conditions, decide optimal routing.
        """
        market = self.market.get_market()
        
        if self.mode == "model" and self.model_client:
            decision = self._decide_with_model(features, market, budget_cap)
        else:
            decision = self._decide_heuristic(features, market, budget_cap)
            
        # Shadow Deployment Eval Gate (predict but don't act)
        shadow_decision_payload = None
        if self.shadow_client:
            # We use _decide_with_model but pass the shadow_client
            # For simplicity in this implementation, we simulate it or pass it.
            # In real system: shadow = self._decide_with_model_impl(features, market, budget_cap, self.shadow_client)
            shadow_decision_payload = {
                "tier": Tier.ECONOMIC.value,
                "model": "local/shadow_lora:v3.3",
                "expected_latency": 1500.0,
                "expected_cost": 0.0,
                "confidence": 0.85
            }
        
        # Validate decision against hard constraints
        validated = self._validate_decision(decision, market, features, budget_cap)
        validated.shadow_decision = shadow_decision_payload
        
        GLOBAL_EVENT_BUS.emit("sully_decision", "current", {
            "tier": validated.tier.value,
            "model": validated.model_id,
            "confidence": validated.confidence,
            "expected_cost": validated.expected_cost,
            "strategy": validated.strategy_hint,
            "reasoning": validated.reasoning,
        })
        
        return validated
    
    # ── v0: Heuristic Decision Engine ────────────────────────────────
    
    def _decide_heuristic(
        self,
        features: TaskFeatures,
        market: Dict[str, ModelMarketInfo],
        budget_cap: float,
    ) -> SullyDecision:
        """
        Deterministic scoring-based routing.
        This is the v0 that ships today — no LLM needed.
        """
        tiers = self.market.get_models_by_tier(market)
        
        # Score each model
        scored = []
        for model in market.values():
            score = self._score_model(model, features, budget_cap)
            if score > 0:
                scored.append((score, model))
        
        scored.sort(key=lambda x: x[0], reverse=True)
        
        if not scored:
            # Absolute fallback: cheapest available
            return SullyDecision(
                tier=Tier.LOCAL,
                model_id="local/llama3-8b",
                confidence=0.3,
                expected_cost=0.0,
                expected_latency=250,
                strategy_hint="auto",
                reasoning="No suitable model found, using local fallback"
            )
        
        best_score, best_model = scored[0]
        tier = self._classify_tier(best_model)
        
        # [AUDIT FIX] Full cost = input + output tokens
        expected_cost = (
            (features.estimated_tokens / 1000) * best_model.input_cost_per_1k
            + (features.estimated_output_tokens / 1000) * best_model.output_cost_per_1k
        )
        
        # Strategy hint based on features
        strategy = "auto"
        if not features.dom_available:
            strategy = "vision_first"
        elif features.ui_complexity > 0.7:
            strategy = "hybrid"
        else:
            strategy = "dom_first"
        
        return SullyDecision(
            tier=tier,
            model_id=best_model.model_id,
            confidence=min(best_score / 10, 1.0),
            expected_cost=expected_cost,
            expected_latency=best_model.avg_latency_ms,
            strategy_hint=strategy,
            reasoning=f"Score {best_score:.2f}: cost={best_model.input_cost_per_1k}, quality={best_model.quality_score}, latency={best_model.avg_latency_ms}ms"
        )
    
    def _score_model(
        self,
        model: ModelMarketInfo,
        features: TaskFeatures,
        budget_cap: float,
    ) -> float:
        """
        Multi-variable scoring function.
        Higher score = better fit for this task.
        """
        # Hard filters (instant disqualify)
        if model.rate_limited:
            return 0.0
        if model.availability < 0.8:
            return 0.0
        if model.context_limit < features.estimated_tokens:
            return 0.0
        
        # [AUDIT FIX] Full cost = input + output
        estimated_cost = (
            (features.estimated_tokens / 1000) * model.input_cost_per_1k
            + (features.estimated_output_tokens / 1000) * model.output_cost_per_1k
        )
        if estimated_cost > budget_cap:
            return 0.0
        
        # Dynamic Market Feedback Loop: routing = learned_quality × cost_efficiency × latency_efficiency
        # Quality & Reliability (Learned from telemetry)
        quality = model.quality_score * getattr(model, "reliability_score", 1.0)
        
        # Cost Efficiency (0.1 to 1.0)
        if model.input_cost_per_1k == 0:
            cost_factor = 1.0  # Free is perfect
        else:
            cost_factor = max(0.1, 1.0 - (estimated_cost / max(budget_cap, 0.001)))
            
        # Latency Efficiency (0.1 to 1.0)
        latency_factor = max(0.1, 1.0 - (model.avg_latency_ms / 5000.0))
        
        # Multiplicative score
        score = quality * cost_factor * latency_factor * 10.0
        
        # Penalties/Bonuses
        if features.requires_auth and not model.supports_reasoning:
            score *= 0.5
            
        # Tier preference bonus (LOCAL > ECONOMIC > PREMIUM)
        tier = self._classify_tier(model)
        if tier == Tier.LOCAL:
            score += 1.5
        elif tier == Tier.ECONOMIC:
            score += 0.5
        
        # Task-specific adjustments
        if features.ui_complexity > 0.7 and model.quality_score < 0.7:
            score *= 0.5  # penalize weak models on complex tasks
        
        if features.history_failures > 0:
            # After failures, bias toward higher quality
            score += model.quality_score * features.history_failures * 0.5
        
        # [AUDIT FIX] Quality requirement gate — critical tasks reject weak models
        if features.quality_requirement > 0.8 and model.quality_score < features.quality_requirement:
            score *= 0.2  # heavy penalty, not zero (allow as last resort)
        
        # [AUDIT FIX] Output type awareness — code/critical needs high quality
        if features.expected_output_type in ("code", "critical_action"):
            if model.quality_score < 0.8:
                score *= 0.3
        
        return score
    
    def _classify_tier(self, model: ModelMarketInfo) -> Tier:
        """Classify model into a tier based on provider and cost."""
        if model.provider == "local":
            return Tier.LOCAL
        if model.input_cost_per_1k < 0.001:
            return Tier.ECONOMIC
        return Tier.PREMIUM
    
    # ── v1: Model-Based Decision (future) ────────────────────────────
    
    def _decide_with_model(
        self,
        features: TaskFeatures,
        market: Dict[str, ModelMarketInfo],
        budget_cap: float,
    ) -> SullyDecision:
        """
        Use fine-tuned Sully model for routing decisions.
        Input: task features + market snapshot + budget
        Output: structured JSON decision
        """
        payload = {
            "task": {
                "type": features.task_type,
                "complexity": features.ui_complexity,
                "estimated_tokens": features.estimated_tokens,
                "requires_auth": features.requires_auth,
                "history_failures": features.history_failures,
            },
            "market": {
                m.model_id: {
                    "cost_in": m.input_cost_per_1k,
                    "cost_out": m.output_cost_per_1k,
                    "latency": m.avg_latency_ms,
                    "ctx": m.context_limit,
                    "availability": m.availability,
                    "rate_limited": m.rate_limited,
                    "quality": m.quality_score,
                }
                for m in market.values()
            },
            "constraints": {
                "budget": budget_cap,
            }
        }
        
        try:
            response = self.model_client.infer(payload)
            return SullyDecision(
                tier=Tier(response["tier"]),
                model_id=response["model"],
                confidence=response.get("confidence", 0.5),
                expected_cost=response.get("expected_cost", 0.0),
                expected_latency=response.get("expected_latency", 1000),
                strategy_hint=response.get("strategy", "auto"),
                reasoning=response.get("reasoning", "model-based decision"),
            )
        except Exception as e:
            logger.warning(f"[Sully] Model inference failed, falling back to heuristic: {e}")
            return self._decide_heuristic(features, market, budget_cap)
    
    # ── Guardrail: Deterministic Validation ──────────────────────────
    
    def _validate_decision(
        self,
        decision: SullyDecision,
        market: Dict[str, ModelMarketInfo],
        features: TaskFeatures,
        budget_cap: float,
    ) -> SullyDecision:
        """
        Mathematical guardrail. Overrides Sully if the decision violates
        hard constraints. This is deterministic Python, not the LLM.
        """
        model = market.get(decision.model_id)
        
        if not model:
            logger.warning(f"[Sully] Decision references unknown model {decision.model_id}, overriding")
            return self._decide_heuristic(features, market, budget_cap)
        
        # Check context limit
        if model.context_limit < features.estimated_tokens:
            logger.warning(f"[Sully] Model {decision.model_id} ctx {model.context_limit} < needed {features.estimated_tokens}")
            return self._decide_heuristic(features, market, budget_cap)
        
        # Check budget [AUDIT FIX: includes output cost]
        real_cost = (
            (features.estimated_tokens / 1000) * model.input_cost_per_1k
            + (features.estimated_output_tokens / 1000) * model.output_cost_per_1k
        )
        if real_cost > budget_cap:
            logger.warning(f"[Sully] Model {decision.model_id} cost ${real_cost:.4f} > budget ${budget_cap:.4f}")
            return self._decide_heuristic(features, market, budget_cap)
        
        # Check rate limiting
        if model.rate_limited:
            logger.warning(f"[Sully] Model {decision.model_id} is rate-limited, overriding")
            return self._decide_heuristic(features, market, budget_cap)
        
        # Check availability
        if model.availability < 0.8:
            logger.warning(f"[Sully] Model {decision.model_id} availability {model.availability:.0%}, overriding")
            return self._decide_heuristic(features, market, budget_cap)
        
        # Recalculate cost with real data
        decision.expected_cost = real_cost
        return decision
    
    # ── Escalation Helper ────────────────────────────────────────────
    
    def next_tier(self, current: Tier) -> Optional[Tier]:
        """Return the next escalation tier, or None if at PREMIUM."""
        if current == Tier.LOCAL:
            return Tier.ECONOMIC
        if current == Tier.ECONOMIC:
            return Tier.PREMIUM
        return None
