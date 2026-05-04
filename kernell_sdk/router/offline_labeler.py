#!/usr/bin/env python3
"""
Kernell OS SDK — Offline Labeler (Phase A2)
════════════════════════════════════════════
Computes `optimal_label` from observed execution outcomes.

Takes raw telemetry events (with actual route, cost, latency, success)
and produces supervised labels for policy model training.

Objective function (configurable):
  score = quality_weight * verified_success
        - cost_weight * usd
        - latency_weight * seconds

Usage:
  python3 -m kernell_sdk.router.offline_labeler \
    --input telemetry_events.jsonl \
    --output labeled_policy_v2.jsonl
"""
from __future__ import annotations

import json
import logging
import math
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Dict, List, Optional

logger = logging.getLogger("kernell.router.labeler")

ROUTE_COSTS = {
    "local": 0.0,
    "local_nano": 0.0, "local_small": 0.0, "local_medium": 0.0, "local_large": 0.0,
    "cheap": 0.005, "cheap_api": 0.005,
    "premium": 0.10, "premium_api": 0.10,
}


@dataclass
class LabelConfig:
    """Weights for the objective function."""
    quality_weight: float = 1.0      # Reward for success
    cost_weight: float = 10.0        # Penalty per $1 spent
    latency_weight: float = 0.1      # Penalty per second
    escalation_penalty: float = 0.3  # Penalty per fallback step
    min_confidence: float = 0.5      # Floor for ambiguous labels
    verifier_min_score: float = 0.75 # Below this, avoid aggressive downgrades


@dataclass
class LabeledExample:
    """A labeled training example for the policy model."""
    # Input features
    task_hash: str
    task_token_count: int
    task_domain: str
    hardware_tier: str
    has_gpu: bool

    # Observed execution
    predicted_route: str
    actual_route: str
    was_escalated: bool
    escalation_chain: List[str]
    success: bool
    cost_usd: float
    latency_s: float
    tokens_total: int

    # Computed optimal label (ground truth for training)
    optimal_route: str
    should_use_premium: bool
    penalty: float                    # How much the actual route cost vs optimal
    label_confidence: float           # How confident we are in this label
    label_reason: str                 # Why this label was assigned
    error_type: str = ""              # underestimation | overestimation | none
    error_severity: float = 0.0       # economic impact estimate


class OfflineLabeler:
    """
    Computes optimal route labels from observed telemetry.

    Logic:
    1. If task succeeded at local → optimal = local (free wins)
    2. If task succeeded at cheap without escalation → optimal = cheap
    3. If task was escalated local→cheap → optimal = cheap (local wasted time)
    4. If task was escalated cheap→premium → optimal = premium (cheap wasted $)
    5. If task was over-provisioned (premium for trivial) → optimal = cheapest that works
    """

    def __init__(self, config: Optional[LabelConfig] = None):
        self.config = config or LabelConfig()
        self._stats = {"total": 0, "labeled": 0, "ambiguous": 0, "skipped": 0}

    def label_event(self, event: dict) -> Optional[LabeledExample]:
        """Label a single telemetry event with optimal route."""
        self._stats["total"] += 1

        success = event.get("success", False)
        actual_tier = event.get("actual_tier_used", event.get("final_route_used", ""))
        was_escalated = event.get("was_escalated", False)
        chain = event.get("escalation_chain", [])
        cost = event.get("cost_usd", 0.0)
        latency = event.get("latency_ms", 0.0) / 1000.0
        tokens = event.get("tokens_in", 0) + event.get("tokens_out", 0)
        verified = bool(event.get("verified", event.get("verifier_accepted", True)))
        verifier_score = float(event.get("self_verifier_score", event.get("verifier_confidence", 1.0)))

        # Normalize tier names to route names
        actual_route = self._tier_to_route(actual_tier)
        predicted_route = self._tier_to_route(
            event.get("policy_route_predicted", event.get("predicted_tier", ""))
        )

        if not actual_route:
            self._stats["skipped"] += 1
            return None

        # Compute optimal route and penalty
        optimal, reason, penalty, confidence = self._compute_optimal(
            success, actual_route, was_escalated, chain, cost, latency, tokens,
            verified=verified, verifier_score=verifier_score
        )

        # Gray-zone verifier confidence: allow, but down-weight training signal.
        if verifier_score < 0.85:
            confidence *= 0.7

        if confidence < self.config.min_confidence:
            self._stats["ambiguous"] += 1
            return None

        self._stats["labeled"] += 1

        return LabeledExample(
            task_hash=event.get("task_hash", ""),
            task_token_count=event.get("task_token_count", 0),
            task_domain=event.get("task_domain", "general"),
            hardware_tier=event.get("hardware_tier", ""),
            has_gpu=event.get("has_gpu", False),
            predicted_route=predicted_route,
            actual_route=actual_route,
            was_escalated=was_escalated,
            escalation_chain=chain,
            success=success,
            cost_usd=cost,
            latency_s=latency,
            tokens_total=tokens,
            optimal_route=optimal,
            should_use_premium=(optimal == "premium"),
            penalty=round(penalty, 6),
            label_confidence=round(confidence, 3),
            label_reason=reason,
            error_type=self._derive_error_type(predicted_route, actual_route, optimal, was_escalated),
            error_severity=round(self._derive_error_severity(penalty, cost, latency), 6),
        )

    def label_batch(self, events: List[dict]) -> List[LabeledExample]:
        """Label a batch of events. Skips unlabelable ones."""
        results = []
        for ev in events:
            labeled = self.label_event(ev)
            if labeled:
                results.append(labeled)
        return results

    def export_jsonl(self, examples: List[LabeledExample], output_path: Path) -> int:
        """Export labeled examples as JSONL for training pipeline."""
        output_path.parent.mkdir(parents=True, exist_ok=True)
        with open(output_path, "w") as f:
            for ex in examples:
                f.write(json.dumps(asdict(ex)) + "\n")
        return len(examples)

    def get_stats(self) -> dict:
        return dict(self._stats)

    def get_balance_report(self, examples: List[LabeledExample]) -> dict:
        """Check class balance for training quality."""
        route_counts: Dict[str, int] = {}
        risk_counts: Dict[str, int] = {}
        for ex in examples:
            route_counts[ex.optimal_route] = route_counts.get(ex.optimal_route, 0) + 1
        total = len(examples) or 1
        return {
            "total": len(examples),
            "route_distribution": {k: f"{v/total*100:.1f}%" for k, v in route_counts.items()},
            "route_counts": route_counts,
            "min_class_size": min(route_counts.values()) if route_counts else 0,
            "max_class_size": max(route_counts.values()) if route_counts else 0,
            "is_balanced": (min(route_counts.values(), default=0) > len(examples) * 0.1),
        }

    # ── Private ──────────────────────────────────────────────────

    def _compute_optimal(self, success, actual, escalated, chain, cost, latency, tokens,
                         verified: bool = True, verifier_score: float = 1.0):
        """Core labeling logic: what SHOULD the route have been?"""
        cfg = self.config

        # Case 1: Succeeded at local without escalation → local is optimal
        if success and actual == "local" and not escalated:
            return "local", "succeeded_at_local", 0.0, 0.95

        # Case 2: Succeeded at cheap without escalation → cheap is optimal
        if success and actual == "cheap" and not escalated:
            return "cheap", "succeeded_at_cheap", 0.0, 0.90

        # Case 3: Succeeded at premium without escalation → check if was necessary
        if success and actual == "premium" and not escalated:
            # If cost was very low for premium, it was probably easy → should have been cheap
            if (
                cost < 0.02
                and tokens < 500
                and verified
                and verifier_score >= cfg.verifier_min_score
            ):
                penalty = cost - ROUTE_COSTS.get("cheap", 0.005)
                return "cheap", "premium_overkill_low_tokens", penalty, 0.75
            # Otherwise premium was warranted
            return "premium", "premium_warranted", 0.0, 0.85

        # Case 4: Escalated from local → cheap (local failed)
        if escalated and len(chain) >= 2:
            first = self._tier_to_route(chain[0])
            last = self._tier_to_route(chain[-1])

            if first == "local" and last == "cheap":
                # Should have gone cheap directly — local wasted time
                penalty = latency * cfg.latency_weight
                return "cheap", "local_failed_cheap_succeeded", penalty, 0.85

            if first == "local" and last == "premium":
                # Should have skipped to premium (non-linear routing)
                penalty = cost * 0.5 + latency * cfg.latency_weight
                return "premium", "local_failed_to_premium", penalty, 0.80

            if first == "cheap" and last == "premium":
                # Cheap wasted $ before premium succeeded
                cheap_cost = ROUTE_COSTS.get("cheap", 0.005)
                penalty = cheap_cost + cfg.escalation_penalty
                return "premium", "cheap_failed_to_premium", penalty, 0.85

        # Case 5: Failed entirely
        if not success or not verified:
            return "premium", "all_failed_needs_premium", cost, 0.60

        # Fallback: use actual route with low confidence
        return actual, "ambiguous_actual_used", 0.0, 0.50

    @staticmethod
    def _derive_error_type(predicted: str, actual: str, optimal: str, was_escalated: bool) -> str:
        if was_escalated or predicted == "local" and optimal == "premium":
            return "underestimation"
        if predicted == "premium" and optimal in ("local", "cheap"):
            return "overestimation"
        if actual != optimal:
            return "misroute"
        return "none"

    @staticmethod
    def _derive_error_severity(penalty: float, cost: float, latency: float) -> float:
        # Weight direct waste first, then residual execution cost, then latency impact.
        return max(0.0, penalty) + (max(0.0, cost) * 0.5) + (max(0.0, latency) * 0.01)

    @staticmethod
    def _tier_to_route(tier: str) -> str:
        """Normalize tier names to route names."""
        if not tier:
            return ""
        t = tier.lower()
        if t.startswith("local"):
            return "local"
        if t in ("cheap", "cheap_api"):
            return "cheap"
        if t in ("premium", "premium_api"):
            return "premium"
        if t == "hybrid":
            return "hybrid"
        return t


if __name__ == "__main__":
    import sys

    print("🏷️  Offline Labeler — Self-test")
    print("=" * 50)

    labeler = OfflineLabeler()

    # Simulate various execution scenarios
    test_events = [
        {"task_hash": "a1", "task_token_count": 20, "task_domain": "data",
         "actual_tier_used": "local_nano", "was_escalated": False,
         "success": True, "cost_usd": 0.0, "latency_ms": 50, "tokens_in": 10, "tokens_out": 15},

        {"task_hash": "b2", "task_token_count": 80, "task_domain": "code",
         "actual_tier_used": "cheap_api", "was_escalated": False,
         "success": True, "cost_usd": 0.004, "latency_ms": 1200, "tokens_in": 300, "tokens_out": 200},

        {"task_hash": "c3", "task_token_count": 120, "task_domain": "code",
         "actual_tier_used": "premium_api", "was_escalated": True,
         "escalation_chain": ["local_small", "cheap_api", "premium_api"],
         "success": True, "cost_usd": 0.12, "latency_ms": 5000, "tokens_in": 800, "tokens_out": 600},

        {"task_hash": "d4", "task_token_count": 30, "task_domain": "data",
         "actual_tier_used": "premium_api", "was_escalated": False,
         "success": True, "cost_usd": 0.01, "latency_ms": 800, "tokens_in": 50, "tokens_out": 100},

        {"task_hash": "e5", "task_token_count": 200, "task_domain": "reasoning",
         "actual_tier_used": "premium_api", "was_escalated": False,
         "success": True, "cost_usd": 0.35, "latency_ms": 8000, "tokens_in": 2000, "tokens_out": 1500},
    ]

    results = labeler.label_batch(test_events)

    for ex in results:
        status = "✅" if ex.penalty == 0 else f"⚠️  penalty=${ex.penalty:.4f}"
        print(f"  {ex.task_hash}: {ex.actual_route:8s} → optimal={ex.optimal_route:8s} "
              f"conf={ex.label_confidence:.2f}  {status}  ({ex.label_reason})")

    print(f"\n📊 Stats: {labeler.get_stats()}")
    print(f"📊 Balance: {labeler.get_balance_report(results)}")
