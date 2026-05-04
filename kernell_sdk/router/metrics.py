"""
Kernell OS SDK — Router Metrics & Observability
══════════════════════════════════════════════════
Production-grade instrumentation for the Intelligent Router.

This is NOT a passive logger — it's the data layer that:
  1. Tracks every routing decision with full provenance
  2. Computes cost_per_successful_task (the business metric)
  3. Measures misclassification_rate (feeds fine-tuning decisions)
  4. Exports Prometheus-compatible metrics
  5. Provides the data backbone for the SDK dashboard

Without this, fine-tuning is shooting blind.
"""
from __future__ import annotations

import time
import threading
import logging
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from .types import ModelTier, DifficultyLevel, ExecutionResult, TaskDomain

logger = logging.getLogger("kernell.router.metrics")


# ── Cost table per provider (USD per 1M tokens) ─────────────────────────
# These are approximate and should be updated via config
API_COST_TABLE: Dict[str, Dict[str, float]] = {
    # tier: {input: $/M, output: $/M}
    "local_nano":    {"input": 0.0,    "output": 0.0},
    "local_small":   {"input": 0.0,    "output": 0.0},
    "local_medium":  {"input": 0.0,    "output": 0.0},
    "local_large":   {"input": 0.0,    "output": 0.0},
    "cheap_api":     {"input": 0.14,   "output": 0.28},   # DeepSeek V3 baseline
    "premium_api":   {"input": 15.0,   "output": 75.0},   # Claude Opus baseline
}


@dataclass
class SubtaskEvent:
    """A single tracked routing event with full provenance."""
    timestamp: float
    task_hash: str
    subtask_id: str
    description_preview: str        # First 80 chars
    predicted_difficulty: int
    predicted_tier: str
    actual_tier: str
    domain: str
    was_escalated: bool
    was_cached: bool
    was_verified: bool
    verifier_confidence: float
    success: bool
    tokens_in: int
    tokens_out: int
    cost_usd: float
    latency_ms: float
    model_used: str


@dataclass
class WindowCounter:
    """Sliding window counter for rate metrics."""
    count: int = 0
    cost: float = 0.0
    tokens: int = 0
    window_start: float = field(default_factory=time.time)
    window_seconds: float = 3600.0  # 1 hour default

    def add(self, cost: float = 0.0, tokens: int = 0):
        self._rotate()
        self.count += 1
        self.cost += cost
        self.tokens += tokens

    def _rotate(self):
        now = time.time()
        if now - self.window_start >= self.window_seconds:
            self.count = 0
            self.cost = 0.0
            self.tokens = 0
            self.window_start = now


class RouterMetricsCollector:
    """
    Central metrics collector for the Intelligent Router.
    
    Tracks every routing decision and computes aggregate metrics
    that drive business decisions and fine-tuning.
    
    Thread-safe for concurrent agent workloads.
    """

    def __init__(self, cost_table: Optional[Dict] = None):
        self._lock = threading.Lock()
        self._cost_table = cost_table or API_COST_TABLE

        # ── Event log (bounded) ──────────────────────────────────────
        self._events: List[SubtaskEvent] = []
        self._max_events = 10000

        # ── Aggregate counters ───────────────────────────────────────
        self._total_subtasks = 0
        self._successful_subtasks = 0
        self._failed_subtasks = 0
        self._cache_hits = 0
        self._escalations = 0
        self._verifier_rejections = 0

        # ── Per-tier counters ────────────────────────────────────────
        self._tier_counts: Dict[str, int] = defaultdict(int)
        self._tier_costs: Dict[str, float] = defaultdict(float)
        self._tier_tokens: Dict[str, int] = defaultdict(int)

        # ── Per-domain counters ──────────────────────────────────────
        self._domain_counts: Dict[str, int] = defaultdict(int)

        # ── Cost tracking ────────────────────────────────────────────
        self._total_cost_usd = 0.0
        self._premium_only_estimate = 0.0  # What it WOULD have cost all-premium

        # ── Latency tracking ─────────────────────────────────────────
        self._latencies: List[float] = []

        # ── Sliding windows ──────────────────────────────────────────
        self._hourly = WindowCounter(window_seconds=3600)
        self._daily = WindowCounter(window_seconds=86400)

        # ── Misclassification tracking (for fine-tuning) ─────────────
        self._misclassifications = 0

    def record_event(
        self,
        result: ExecutionResult,
        task_hash: str = "",
        description: str = "",
        predicted_difficulty: int = 3,
        predicted_tier: str = "",
        domain: str = "general",
        was_verified: bool = False,
        verifier_confidence: float = 0.0,
    ) -> None:
        """Record a completed subtask execution."""
        with self._lock:
            # Calculate actual cost
            cost = self._calculate_cost(
                result.tier_used.value,
                result.tokens_in,
                result.tokens_out,
            )

            # Estimate what premium would have cost
            premium_cost = self._calculate_cost(
                "premium_api",
                result.tokens_in,
                result.tokens_out,
            )

            # Was this a misclassification?
            was_escalated = result.escalated_from is not None
            if was_escalated:
                self._misclassifications += 1
                self._escalations += 1

            # Build event
            event = SubtaskEvent(
                timestamp=time.time(),
                task_hash=task_hash,
                subtask_id=result.subtask_id,
                description_preview=description[:80],
                predicted_difficulty=predicted_difficulty,
                predicted_tier=predicted_tier or result.tier_used.value,
                actual_tier=result.tier_used.value,
                domain=domain,
                was_escalated=was_escalated,
                was_cached=result.was_cached,
                was_verified=was_verified,
                verifier_confidence=verifier_confidence,
                success=result.success,
                tokens_in=result.tokens_in,
                tokens_out=result.tokens_out,
                cost_usd=cost,
                latency_ms=result.latency_ms,
                model_used=result.model_used,
            )

            # Update aggregates
            self._total_subtasks += 1
            if result.success:
                self._successful_subtasks += 1
            else:
                self._failed_subtasks += 1

            if result.was_cached:
                self._cache_hits += 1

            self._tier_counts[result.tier_used.value] += 1
            self._tier_costs[result.tier_used.value] += cost
            self._tier_tokens[result.tier_used.value] += result.tokens_in + result.tokens_out
            self._domain_counts[domain] += 1

            self._total_cost_usd += cost
            self._premium_only_estimate += premium_cost

            if result.latency_ms > 0:
                self._latencies.append(result.latency_ms)

            # Sliding windows
            self._hourly.add(cost, result.tokens_in + result.tokens_out)
            self._daily.add(cost, result.tokens_in + result.tokens_out)

            # Bounded event log
            self._events.append(event)
            if len(self._events) > self._max_events:
                self._events = self._events[-self._max_events:]

    def _calculate_cost(self, tier: str, tokens_in: int, tokens_out: int) -> float:
        """Calculate USD cost for a completion."""
        rates = self._cost_table.get(tier, {"input": 0.0, "output": 0.0})
        return (tokens_in / 1_000_000 * rates["input"]) + (tokens_out / 1_000_000 * rates["output"])

    # ── Dashboard-ready metrics ──────────────────────────────────────────

    def get_dashboard_metrics(self) -> dict:
        """Return all metrics formatted for the SDK dashboard."""
        with self._lock:
            return {
                "cost_overview": self._cost_overview(),
                "tier_distribution": self._tier_distribution(),
                "efficiency": self._efficiency_metrics(),
                "classifier_health": self._classifier_health(),
                "hourly_window": self._window_snapshot(self._hourly),
                "daily_window": self._window_snapshot(self._daily),
                "top_domains": dict(sorted(
                    self._domain_counts.items(),
                    key=lambda x: x[1], reverse=True,
                )[:10]),
            }

    def _cost_overview(self) -> dict:
        return {
            "total_cost_usd": round(self._total_cost_usd, 4),
            "premium_only_estimate_usd": round(self._premium_only_estimate, 4),
            "savings_usd": round(self._premium_only_estimate - self._total_cost_usd, 4),
            "savings_percent": (
                round((1 - self._total_cost_usd / self._premium_only_estimate) * 100, 1)
                if self._premium_only_estimate > 0 else 100.0
            ),
            "cost_per_successful_task": (
                round(self._total_cost_usd / self._successful_subtasks, 6)
                if self._successful_subtasks > 0 else 0.0
            ),
            "cost_by_tier": {k: round(v, 4) for k, v in self._tier_costs.items()},
        }

    def _tier_distribution(self) -> dict:
        total = max(self._total_subtasks, 1)
        return {
            "total_subtasks": self._total_subtasks,
            "by_tier": {k: v for k, v in self._tier_counts.items()},
            "by_tier_percent": {k: round(v / total * 100, 1) for k, v in self._tier_counts.items()},
            "local_resolution_rate": round(
                sum(v for k, v in self._tier_counts.items() if k.startswith("local_"))
                / total * 100, 1
            ),
            "cache_hit_rate": round(self._cache_hits / total * 100, 1),
        }

    def _efficiency_metrics(self) -> dict:
        return {
            "cache_hits": self._cache_hits,
            "escalations": self._escalations,
            "verifier_rejections": self._verifier_rejections,
            "avg_latency_ms": (
                round(sum(self._latencies) / len(self._latencies), 1)
                if self._latencies else 0.0
            ),
            "p95_latency_ms": (
                round(sorted(self._latencies)[int(len(self._latencies) * 0.95)], 1)
                if len(self._latencies) >= 20 else 0.0
            ),
            "total_tokens": sum(self._tier_tokens.values()),
            "tokens_by_tier": dict(self._tier_tokens),
        }

    def _classifier_health(self) -> dict:
        total = max(self._total_subtasks, 1)
        return {
            "misclassification_rate": round(self._misclassifications / total * 100, 2),
            "total_misclassifications": self._misclassifications,
            "ready_for_finetuning": self._misclassifications >= 50,
            "recommendation": (
                "Sufficient misclassification data for fine-tuning"
                if self._misclassifications >= 50
                else f"Need {50 - self._misclassifications} more samples before fine-tuning"
            ),
        }

    def _window_snapshot(self, window: WindowCounter) -> dict:
        window._rotate()
        return {
            "requests": window.count,
            "cost_usd": round(window.cost, 4),
            "tokens": window.tokens,
        }

    # ── Prometheus-compatible export ─────────────────────────────────────

    def export_prometheus(self) -> str:
        """
        Export metrics in Prometheus text exposition format.
        
        Scrape this at /metrics endpoint for Grafana dashboards.
        """
        lines = []

        def gauge(name, value, help_text="", labels=None):
            if help_text:
                lines.append(f"# HELP kernell_{name} {help_text}")
                lines.append(f"# TYPE kernell_{name} gauge")
            label_str = ""
            if labels:
                label_str = "{" + ",".join(f'{k}="{v}"' for k, v in labels.items()) + "}"
            lines.append(f"kernell_{name}{label_str} {value}")

        with self._lock:
            gauge("router_total_subtasks", self._total_subtasks, "Total subtasks processed")
            gauge("router_successful_subtasks", self._successful_subtasks, "Successful subtasks")
            gauge("router_failed_subtasks", self._failed_subtasks, "Failed subtasks")
            gauge("router_cache_hits", self._cache_hits, "Semantic cache hits")
            gauge("router_escalations", self._escalations, "Tier escalations")
            gauge("router_total_cost_usd", round(self._total_cost_usd, 6), "Total cost in USD")
            gauge("router_savings_usd", round(self._premium_only_estimate - self._total_cost_usd, 6), "USD saved vs all-premium")
            gauge("router_misclassifications", self._misclassifications, "Classifier errors")

            for tier, count in self._tier_counts.items():
                gauge("router_tier_requests", count, labels={"tier": tier})
            for tier, cost in self._tier_costs.items():
                gauge("router_tier_cost_usd", round(cost, 6), labels={"tier": tier})

        return "\n".join(lines) + "\n"

    # ── Fine-tuning readiness ────────────────────────────────────────────

    def get_misclassified_events(self) -> List[SubtaskEvent]:
        """Return events where the classifier got it wrong — raw training signal."""
        with self._lock:
            return [e for e in self._events if e.was_escalated]

    def export_training_candidates(self) -> List[dict]:
        """Export misclassified events in fine-tuning dataset format."""
        candidates = self.get_misclassified_events()
        return [
            {
                "task": e.description_preview,
                "predicted_difficulty": e.predicted_difficulty,
                "actual_outcome": "escalated" if e.was_escalated else "success",
                "final_tier": e.actual_tier,
                "tokens_used": e.tokens_in + e.tokens_out,
                "was_misclassified": True,
                "domain": e.domain,
            }
            for e in candidates
        ]
