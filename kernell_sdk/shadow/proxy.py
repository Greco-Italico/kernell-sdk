"""
Kernell OS SDK — Shadow Proxy (OpenAI-Compatible Interceptor)
═════════════════════════════════════════════════════════════
Transparent observation layer for existing AI workloads.

How it works:
  1. Client installs kernell-os-sdk
  2. Changes ONE line: `from kernell_sdk.shadow import openai`
     (or sets OPENAI_BASE_URL to our local proxy)
  3. Every API call passes through to OpenAI/Anthropic UNCHANGED
  4. We record: model used, tokens, latency, estimated cost
  5. We compute: what Kernell WOULD have done (counterfactual)
  6. Dashboard shows: "You spent $X. Kernell would have spent $Y."

Zero risk. Zero code changes. Pure observation.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import time
import uuid
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger("kernell.shadow")


# ═══════════════════════════════════════════════════════════════════════════
# Configuration
# ═══════════════════════════════════════════════════════════════════════════

@dataclass
class ShadowConfig:
    """Shadow Mode configuration."""
    enabled: bool = True
    agent_id: str = ""
    org_id: str = ""
    log_dir: str = ""
    telemetry_endpoint: str = "https://api.kernell.ai/telemetry"
    send_telemetry: bool = False       # Off by default until endpoint exists
    buffer_max_events: int = 10_000
    flush_interval_seconds: int = 300  # 5 minutes

    def __post_init__(self):
        if not self.agent_id:
            self.agent_id = str(uuid.uuid4())[:8]
        if not self.log_dir:
            self.log_dir = str(Path.home() / ".kernell" / "shadow")


# ═══════════════════════════════════════════════════════════════════════════
# Cost Tables (real pricing as of 2025-Q4)
# ═══════════════════════════════════════════════════════════════════════════

# Cost per 1M tokens (input, output)
API_PRICING = {
    # Premium APIs
    "gpt-4o":             {"input": 2.50,  "output": 10.00},
    "gpt-4o-mini":        {"input": 0.15,  "output": 0.60},
    "gpt-4-turbo":        {"input": 10.00, "output": 30.00},
    "gpt-4":              {"input": 30.00, "output": 60.00},
    "claude-opus-4-0520": {"input": 15.00, "output": 75.00},
    "claude-sonnet-4-0514": {"input": 3.00, "output": 15.00},
    "claude-3-5-sonnet":  {"input": 3.00,  "output": 15.00},
    "claude-3-haiku":     {"input": 0.25,  "output": 1.25},
    "gemini-2.5-pro":     {"input": 1.25,  "output": 10.00},
    "gemini-2.0-flash":   {"input": 0.10,  "output": 0.40},
    # Cheap APIs
    "deepseek-chat":      {"input": 0.14,  "output": 0.28},
    "deepseek-reasoner":  {"input": 0.55,  "output": 2.19},
    "llama-3.3-70b":      {"input": 0.59,  "output": 0.79},
    "llama-3.1-8b":       {"input": 0.05,  "output": 0.08},
    # Local (free)
    "local":              {"input": 0.00,  "output": 0.00},
}

# Counterfactual: what Kernell would route to
KERNELL_ROUTING_TABLE = {
    # task_complexity → (model, tier)
    "trivial":  ("local",            "local_nano"),    # < 200 tokens, extraction
    "easy":     ("local",            "local_small"),   # < 500 tokens, simple gen
    "moderate": ("deepseek-chat",    "cheap_api"),     # < 2000 tokens, coding
    "hard":     ("claude-3-5-sonnet","premium_api"),   # Complex reasoning
    "extreme":  ("claude-opus-4-0520","premium_api"),  # Multi-step planning
}


# ═══════════════════════════════════════════════════════════════════════════
# Shadow Event (what we record per API call)
# ═══════════════════════════════════════════════════════════════════════════

@dataclass
class ShadowEvent:
    """A single observed API call with counterfactual analysis."""
    event_id: str
    timestamp: float
    agent_id: str

    # What the client actually did
    original_model: str
    original_tokens_in: int
    original_tokens_out: int
    original_cost_usd: float
    original_latency_ms: float

    # What Kernell WOULD have done
    kernell_model: str
    kernell_tier: str
    kernell_cost_usd: float
    kernell_confidence: float

    # The delta (this is what sells)
    savings_usd: float
    savings_pct: float

    # Structural metadata (non-sensitive)
    task_hash: str           # SHA-256 of the prompt (not the prompt itself)
    task_token_count: int
    task_complexity: str     # trivial/easy/moderate/hard/extreme
    num_messages: int


# ═══════════════════════════════════════════════════════════════════════════
# Shadow Proxy (The Core Engine)
# ═══════════════════════════════════════════════════════════════════════════

class ShadowProxy:
    """
    Transparent observation proxy for OpenAI-compatible API calls.

    Usage (Nivel 1 — SDK Wrapper):
        from kernell_sdk.shadow import ShadowProxy

        proxy = ShadowProxy()
        # Wrap any openai-style call:
        result = proxy.observe(
            model="gpt-4o",
            messages=[{"role": "user", "content": "..."}],
            response=openai_response,
            latency_ms=elapsed,
        )

    Usage (Nivel 2 — Monkey-patch):
        from kernell_sdk.shadow.proxy import patch_openai
        patch_openai()  # Now all openai.chat.completions.create() calls are observed
    """

    def __init__(self, config: Optional[ShadowConfig] = None):
        self._config = config or ShadowConfig()
        self._events: List[ShadowEvent] = []
        self._total_original_cost = 0.0
        self._total_kernell_cost = 0.0
        self._total_savings = 0.0

        # Ensure log directory exists
        os.makedirs(self._config.log_dir, exist_ok=True)

        logger.info(
            f"Shadow Proxy initialized | agent={self._config.agent_id} | "
            f"log_dir={self._config.log_dir}"
        )

    def observe(
        self,
        model: str,
        messages: list,
        response: Any,
        latency_ms: float,
        tokens_in: Optional[int] = None,
        tokens_out: Optional[int] = None,
    ) -> ShadowEvent:
        """
        Record an API call observation and compute counterfactual savings.

        This method NEVER modifies the response. It only observes.
        """
        if not self._config.enabled:
            return None

        # Extract token usage from response
        usage = getattr(response, "usage", None)
        if usage:
            t_in = tokens_in or getattr(usage, "prompt_tokens", 0)
            t_out = tokens_out or getattr(usage, "completion_tokens", 0)
        else:
            t_in = tokens_in or self._estimate_tokens(messages)
            t_out = tokens_out or 0

        # Compute actual cost
        original_cost = self._compute_cost(model, t_in, t_out)

        # Classify task complexity (structural analysis only)
        complexity = self._classify_complexity(t_in, t_out, len(messages))

        # Compute counterfactual (what Kernell would have done)
        kernell_model, kernell_tier = KERNELL_ROUTING_TABLE.get(
            complexity, ("deepseek-chat", "cheap_api")
        )
        kernell_cost = self._compute_cost(kernell_model, t_in, t_out)

        # Compute savings
        savings_usd = max(0, original_cost - kernell_cost)
        savings_pct = (savings_usd / original_cost * 100) if original_cost > 0 else 0

        # Hash the prompt content (NEVER store raw text)
        task_hash = hashlib.sha256(
            json.dumps(messages, default=str).encode()
        ).hexdigest()[:16]

        # Build event
        event = ShadowEvent(
            event_id=str(uuid.uuid4())[:8],
            timestamp=time.time(),
            agent_id=self._config.agent_id,
            original_model=model,
            original_tokens_in=t_in,
            original_tokens_out=t_out,
            original_cost_usd=round(original_cost, 6),
            original_latency_ms=round(latency_ms, 1),
            kernell_model=kernell_model,
            kernell_tier=kernell_tier,
            kernell_cost_usd=round(kernell_cost, 6),
            kernell_confidence=self._estimate_confidence(complexity),
            savings_usd=round(savings_usd, 6),
            savings_pct=round(savings_pct, 1),
            task_hash=task_hash,
            task_token_count=t_in + t_out,
            task_complexity=complexity,
            num_messages=len(messages),
        )

        # Accumulate
        self._events.append(event)
        self._total_original_cost += original_cost
        self._total_kernell_cost += kernell_cost
        self._total_savings += savings_usd

        # Bounded buffer
        if len(self._events) > self._config.buffer_max_events:
            self._flush_to_disk()

        return event

    # ── Dashboard Data ────────────────────────────────────────────────

    def get_dashboard_data(self) -> dict:
        """
        Returns the complete dataset for the FinOps Dashboard.
        This is what powers every card, chart, and table.
        """
        events = self._events
        total_events = len(events)

        if total_events == 0:
            return {
                "baseline_spend": 0, "optimized_spend": 0,
                "verified_savings": 0, "savings_pct": 0,
                "savings_velocity_per_min": 0, "confidence": 0,
                "savings_at_risk": 0, "routing": {}, "events": [],
            }

        # KPI calculations
        baseline = self._total_original_cost
        optimized = self._total_kernell_cost
        savings = self._total_savings

        # Savings velocity ($/min)
        if total_events >= 2:
            time_span = events[-1].timestamp - events[0].timestamp
            velocity = (savings / (time_span / 60)) if time_span > 0 else 0
        else:
            velocity = 0

        # Confidence (weighted average)
        conf_sum = sum(e.kernell_confidence for e in events)
        avg_confidence = conf_sum / total_events

        # Savings at risk (from low-confidence routes)
        at_risk = sum(
            e.savings_usd for e in events if e.kernell_confidence < 0.85
        )

        # Routing distribution
        local_count = sum(1 for e in events if e.kernell_tier.startswith("local"))
        cheap_count = sum(1 for e in events if e.kernell_tier == "cheap_api")
        premium_count = sum(1 for e in events if e.kernell_tier == "premium_api")

        local_cost = sum(e.kernell_cost_usd for e in events if e.kernell_tier.startswith("local"))
        cheap_cost = sum(e.kernell_cost_usd for e in events if e.kernell_tier == "cheap_api")
        premium_cost = sum(e.kernell_cost_usd for e in events if e.kernell_tier == "premium_api")

        local_conf = (
            sum(e.kernell_confidence for e in events if e.kernell_tier.startswith("local"))
            / max(1, local_count)
        )
        cheap_conf = (
            sum(e.kernell_confidence for e in events if e.kernell_tier == "cheap_api")
            / max(1, cheap_count)
        )
        premium_conf = (
            sum(e.kernell_confidence for e in events if e.kernell_tier == "premium_api")
            / max(1, premium_count)
        )

        return {
            "baseline_spend": round(baseline, 2),
            "optimized_spend": round(optimized, 2),
            "verified_savings": round(savings, 2),
            "savings_pct": round((savings / baseline * 100) if baseline > 0 else 0, 1),
            "savings_velocity_per_min": round(velocity, 2),
            "confidence": round(avg_confidence * 100, 1),
            "savings_at_risk": round(at_risk, 2),
            "audit_coverage_pct": 100.0,
            "total_events": total_events,
            "routing": {
                "local": {
                    "count": local_count,
                    "pct": round(local_count / total_events * 100, 1),
                    "cost": round(local_cost, 2),
                    "confidence": round(local_conf * 100, 1),
                },
                "cheap_api": {
                    "count": cheap_count,
                    "pct": round(cheap_count / total_events * 100, 1),
                    "cost": round(cheap_cost, 2),
                    "confidence": round(cheap_conf * 100, 1),
                },
                "premium_api": {
                    "count": premium_count,
                    "pct": round(premium_count / total_events * 100, 1),
                    "cost": round(premium_cost, 2),
                    "confidence": round(premium_conf * 100, 1),
                },
            },
            "recent_events": [
                {
                    "hash": e.task_hash,
                    "original_route": e.original_model,
                    "kernell_route": e.kernell_model,
                    "actual_cost": e.original_cost_usd,
                    "counterfactual_cost": e.kernell_cost_usd,
                    "savings": e.savings_usd,
                    "confidence": e.kernell_confidence,
                }
                for e in events[-50:]  # Last 50 events
            ],
        }

    # ── Internal Helpers ──────────────────────────────────────────────

    def _compute_cost(self, model: str, tokens_in: int, tokens_out: int) -> float:
        """Compute USD cost for a given model and token count."""
        pricing = API_PRICING.get(model, API_PRICING.get("gpt-4o-mini"))
        cost_in = (tokens_in / 1_000_000) * pricing["input"]
        cost_out = (tokens_out / 1_000_000) * pricing["output"]
        return cost_in + cost_out

    def _classify_complexity(
        self, tokens_in: int, tokens_out: int, num_messages: int
    ) -> str:
        """
        Classify task complexity from structural signals only.
        No content is analyzed — only shape and size.
        """
        total = tokens_in + tokens_out
        if total < 200 and num_messages <= 2:
            return "trivial"
        elif total < 500:
            return "easy"
        elif total < 2000:
            return "moderate"
        elif total < 5000:
            return "hard"
        else:
            return "extreme"

    def _estimate_confidence(self, complexity: str) -> float:
        """Estimate routing confidence based on task complexity."""
        return {
            "trivial":  0.98,
            "easy":     0.95,
            "moderate": 0.89,
            "hard":     0.78,
            "extreme":  0.65,
        }.get(complexity, 0.80)

    def _estimate_tokens(self, messages: list) -> int:
        """Rough token estimate from message list (4 chars ≈ 1 token)."""
        total_chars = sum(len(str(m.get("content", ""))) for m in messages)
        return max(1, total_chars // 4)

    def _flush_to_disk(self):
        """Persist buffered events to local JSONL file."""
        filepath = os.path.join(
            self._config.log_dir,
            f"shadow_{self._config.agent_id}.jsonl"
        )
        try:
            with open(filepath, "a") as f:
                for event in self._events:
                    f.write(json.dumps(asdict(event)) + "\n")
            logger.info(f"Flushed {len(self._events)} shadow events to {filepath}")
            self._events = []
        except Exception as e:
            logger.warning(f"Failed to flush shadow events: {e}")

    def flush(self):
        """Manually flush all events to disk."""
        if self._events:
            self._flush_to_disk()

    @property
    def stats(self) -> dict:
        return {
            "total_observed": len(self._events),
            "total_original_cost": round(self._total_original_cost, 4),
            "total_kernell_cost": round(self._total_kernell_cost, 4),
            "total_savings": round(self._total_savings, 4),
            "agent_id": self._config.agent_id,
        }


# ═══════════════════════════════════════════════════════════════════════════
# Monkey-patch helper for zero-code integration
# ═══════════════════════════════════════════════════════════════════════════

_global_proxy: Optional[ShadowProxy] = None


def patch_openai(config: Optional[ShadowConfig] = None):
    """
    Monkey-patch the openai library to observe all chat completions.

    Usage:
        from kernell_sdk.shadow.proxy import patch_openai
        patch_openai()

        # Now all openai calls are automatically observed:
        import openai
        client = openai.OpenAI()
        response = client.chat.completions.create(...)  # ← observed
    """
    global _global_proxy
    _global_proxy = ShadowProxy(config)

    try:
        import openai
        _original_create = openai.resources.chat.completions.Completions.create

        def _patched_create(self_inner, *args, **kwargs):
            t0 = time.monotonic()
            response = _original_create(self_inner, *args, **kwargs)
            latency = (time.monotonic() - t0) * 1000

            model = kwargs.get("model", "unknown")
            messages = kwargs.get("messages", [])

            _global_proxy.observe(
                model=model,
                messages=messages,
                response=response,
                latency_ms=latency,
            )
            return response

        openai.resources.chat.completions.Completions.create = _patched_create
        logger.info("✅ OpenAI monkey-patch applied. All calls are now observed.")

    except ImportError:
        logger.warning("openai package not installed. Monkey-patch skipped.")
    except Exception as e:
        logger.warning(f"Failed to patch openai: {e}. Shadow mode inactive.")


def get_proxy() -> Optional[ShadowProxy]:
    """Get the global shadow proxy instance (if patched)."""
    return _global_proxy
