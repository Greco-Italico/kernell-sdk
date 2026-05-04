"""
Kernell OS SDK — Router Entrypoint (Dual Router + Feature Flags)
═════════════════════════════════════════════════════════════════
Single point of entry for all routing decisions.

Strategy: Wrap BOTH routers (legacy + intelligent) behind a safe
entrypoint with feature flags, shadow mode, and automatic fallback.

Shadow mode runs BOTH routers on every request, returns the legacy
result (zero risk), but logs the diff for dataset building.

This is the bridge from "router exists" to "router is validated in prod".
"""
from __future__ import annotations

import hashlib
import logging
import secrets
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Protocol

logger = logging.getLogger("kernell.router.entrypoint")


@dataclass
class RouterConfig:
    """Feature flags for router activation."""
    enable_intelligent_router: bool = False      # Master switch
    shadow_mode: bool = True                     # Run both, return legacy
    canary_percent: float = 0.0                  # 0.0-1.0, % of traffic to new router
    log_diffs: bool = True                       # Log when routers disagree
    fallback_on_error: bool = True               # Always fallback to legacy on crash
    classifier_endpoint: str = ""                # Remote model endpoint (not in SDK)
    classifier_model_tag: str = "kernell-classifier:latest"  # Ollama tag


class LegacyRouter(Protocol):
    """Protocol for the existing LLMRouter."""
    def complete(self, messages: list, **kwargs) -> Any: ...


class IntelligentRouterBackend(Protocol):
    """Protocol for the new IntelligentRouter."""
    def execute(self, task: str) -> list: ...


@dataclass
class RouterDiff:
    """Record of a shadow-mode comparison."""
    timestamp: float
    task_hash: str
    task_preview: str
    legacy_model: str
    intelligent_tier: str
    intelligent_subtasks: int
    estimated_savings_pct: float


class RouterEntrypoint:
    """
    Unified entry point for all LLM routing.
    
    Activation phases:
      Phase 0: shadow_mode=True  → runs both, returns legacy, logs diffs
      Phase 1: canary_percent=0.1 → 10% traffic to intelligent router
      Phase 2: canary_percent=1.0 → full rollout with fallback
    
    The fine-tuned classifier model is NOT bundled in the SDK.
    It is downloaded at install time from a model registry (Ollama/HF)
    and runs on the user's local hardware or a remote endpoint.
    """

    def __init__(
        self,
        legacy_router: Optional[LegacyRouter] = None,
        intelligent_router: Optional[IntelligentRouterBackend] = None,
        config: Optional[RouterConfig] = None,
        metrics_collector=None,
    ):
        self._legacy = legacy_router
        self._intelligent = intelligent_router
        self._config = config or RouterConfig()
        self._metrics = metrics_collector

        # Shadow mode diff log (bounded)
        self._diffs: List[RouterDiff] = []
        self._max_diffs = 5000

        # Counters
        self._total_requests = 0
        self._legacy_used = 0
        self._intelligent_used = 0
        self._fallbacks = 0

    def route(self, task: str, messages: Optional[list] = None, **kwargs) -> Any:
        """
        Main routing entry point.
        
        Decides which router to use based on feature flags.
        ALWAYS has a safe fallback path.
        """
        self._total_requests += 1

        # ── No intelligent router available → legacy always ──────────
        if not self._intelligent or not self._config.enable_intelligent_router:
            return self._use_legacy(messages or [], **kwargs)

        # ── Shadow Mode: run both, return legacy ─────────────────────
        if self._config.shadow_mode:
            return self._shadow_execute(task, messages or [], **kwargs)

        # ── Canary Mode: probabilistic routing ───────────────────────
        if self._config.canary_percent > 0:
            if secrets.SystemRandom().random() < self._config.canary_percent:
                return self._use_intelligent_safe(task, messages, **kwargs)
            else:
                return self._use_legacy(messages or [], **kwargs)

        # ── Full Mode: intelligent with fallback ─────────────────────
        return self._use_intelligent_safe(task, messages, **kwargs)

    def _use_legacy(self, messages: list, **kwargs) -> Any:
        """Execute via the legacy LLMRouter."""
        self._legacy_used += 1
        if self._legacy:
            return self._legacy.complete(messages, **kwargs)
        return None

    def _use_intelligent_safe(self, task: str, messages: Optional[list], **kwargs) -> Any:
        """Execute via the intelligent router with automatic fallback."""
        try:
            result = self._intelligent.execute(task)
            self._intelligent_used += 1
            return result
        except Exception as e:
            self._fallbacks += 1
            logger.warning(
                f"Intelligent router failed ({type(e).__name__}: {e}), "
                f"falling back to legacy (fallback #{self._fallbacks})"
            )
            if self._config.fallback_on_error and self._legacy and messages:
                return self._use_legacy(messages, **kwargs)
            raise

    def _shadow_execute(self, task: str, messages: list, **kwargs) -> Any:
        """
        Shadow mode: run BOTH routers, return legacy result.
        
        This is zero-risk data collection. The intelligent router
        output is logged but never returned to the user.
        """
        # Always execute legacy (this is the real result)
        legacy_result = self._use_legacy(messages, **kwargs)

        # Run intelligent router in background (fire-and-forget safe)
        try:
            t0 = time.monotonic()
            intelligent_results = self._intelligent.execute(task)
            elapsed = (time.monotonic() - t0) * 1000

            # Build diff
            task_hash = hashlib.sha256(task.encode()).hexdigest()[:12]

            diff = RouterDiff(
                timestamp=time.time(),
                task_hash=task_hash,
                task_preview=task[:120],
                legacy_model=getattr(legacy_result, 'model_used', 'unknown'),
                intelligent_tier=(
                    intelligent_results[0].tier_used.value
                    if intelligent_results else "none"
                ),
                intelligent_subtasks=len(intelligent_results) if intelligent_results else 0,
                estimated_savings_pct=0.0,
            )

            self._diffs.append(diff)
            if len(self._diffs) > self._max_diffs:
                self._diffs = self._diffs[-self._max_diffs:]

            if self._config.log_diffs:
                logger.info(
                    f"Shadow diff: task={task_hash} "
                    f"legacy={diff.legacy_model} "
                    f"intelligent={diff.intelligent_tier} "
                    f"subtasks={diff.intelligent_subtasks} "
                    f"latency={elapsed:.0f}ms"
                )

        except Exception as e:
            logger.debug(f"Shadow intelligent router error (non-blocking): {e}")

        # ALWAYS return legacy result
        return legacy_result

    # ── Dashboard / observability ────────────────────────────────────

    def get_status(self) -> dict:
        """Return entrypoint status for dashboard."""
        return {
            "mode": self._current_mode(),
            "total_requests": self._total_requests,
            "legacy_used": self._legacy_used,
            "intelligent_used": self._intelligent_used,
            "fallbacks": self._fallbacks,
            "shadow_diffs_collected": len(self._diffs),
            "config": {
                "enable_intelligent_router": self._config.enable_intelligent_router,
                "shadow_mode": self._config.shadow_mode,
                "canary_percent": self._config.canary_percent,
                "classifier_model": self._config.classifier_model_tag,
                "classifier_endpoint": self._config.classifier_endpoint or "local",
            },
        }

    def _current_mode(self) -> str:
        if not self._config.enable_intelligent_router:
            return "legacy_only"
        if self._config.shadow_mode:
            return "shadow"
        if 0 < self._config.canary_percent < 1.0:
            return f"canary_{int(self._config.canary_percent * 100)}pct"
        return "intelligent_full"

    def get_shadow_diffs(self) -> List[dict]:
        """Export shadow diffs for analysis and training dataset building."""
        return [
            {
                "timestamp": d.timestamp,
                "task_hash": d.task_hash,
                "task_preview": d.task_preview,
                "legacy_model": d.legacy_model,
                "intelligent_tier": d.intelligent_tier,
                "intelligent_subtasks": d.intelligent_subtasks,
            }
            for d in self._diffs
        ]
