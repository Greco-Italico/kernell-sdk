"""
Kernell OS SDK — Policy-Lite Client (edge runtime)
══════════════════════════════════════════════════
Local policy model wrapper that predicts execution strategy:
route, confidence, risk, decomposition need, and expected budget/latency.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Optional, Protocol

from .types import PolicyDecision, PolicyRoute, RiskLevel

logger = logging.getLogger("kernell.router.policy_lite")


POLICY_SYSTEM_PROMPT = """\
You are a routing policy model for an LLM orchestration engine.

Before deciding, you MUST think step-by-step in a <thought> block about the task's complexity, cost, and risk.
After your thought block, return ONLY JSON with this schema:
{
  "route": "local|cheap|premium|hybrid",
  "confidence": 0.0-1.0,
  "needs_decomposition": true|false,
  "risk": "low|medium|high",
  "expected_cost_usd": number,
  "expected_latency_s": number,
  "max_budget_usd": number
}
"""


class LLMBackend(Protocol):
    def generate(self, prompt: str, system: str = "") -> str: ...


@dataclass
class PolicyLiteConfig:
    enabled: bool = True
    min_confidence: float = 0.55
    high_risk_force_hybrid: bool = True
    max_expected_cost_error: float = 0.02
    default_policy_version: str = "lite-v0"


class PolicyLiteClient:
    """Local client that produces PolicyDecision from a lightweight model."""

    def __init__(self, model: LLMBackend, config: Optional[PolicyLiteConfig] = None):
        self._model = model
        self._config = config or PolicyLiteConfig()

    def decide(self, task: str) -> PolicyDecision:
        """Infer a policy decision with safe parsing and fallback."""
        if not self._config.enabled:
            return self._fallback_decision()

        try:
            raw = self._model.generate(
                prompt=f"Task:\n{task}\n\nPredict best route.",
                system=POLICY_SYSTEM_PROMPT,
            )
            decision = self._parse(raw)
        except Exception as exc:
            logger.warning(f"Policy-Lite inference failed: {exc}")
            decision = self._fallback_decision()

        budget_overrun = (
            decision.max_budget_usd > 0.0
            and (decision.expected_cost_usd - decision.max_budget_usd) > self._config.max_expected_cost_error
        )
        if (
            decision.confidence < self._config.min_confidence
            or (self._config.high_risk_force_hybrid and decision.risk == RiskLevel.HIGH)
            or budget_overrun
        ):
            # Force safer path when confidence/risk/economic uncertainty is concerning.
            decision.route = PolicyRoute.HYBRID
            decision.needs_decomposition = True
        return decision

    def _parse(self, raw: str) -> PolicyDecision:
        start = raw.find("{")
        end = raw.rfind("}")
        if start == -1 or end == -1:
            return self._fallback_decision()

        try:
            data = json.loads(raw[start : end + 1])
            route = PolicyRoute(data.get("route", "hybrid"))
            risk = RiskLevel(data.get("risk", "medium"))
            return PolicyDecision(
                route=route,
                confidence=float(data.get("confidence", 0.5)),
                needs_decomposition=bool(data.get("needs_decomposition", route == PolicyRoute.HYBRID)),
                risk=risk,
                expected_cost_usd=float(data.get("expected_cost_usd", 0.0)),
                expected_latency_s=float(data.get("expected_latency_s", 0.0)),
                max_budget_usd=float(data.get("max_budget_usd", 0.0)),
                policy_version=self._config.default_policy_version,
            )
        except (ValueError, TypeError, KeyError):
            return self._fallback_decision()

    def _fallback_decision(self) -> PolicyDecision:
        return PolicyDecision(
            route=PolicyRoute.HYBRID,
            confidence=0.5,
            needs_decomposition=True,
            risk=RiskLevel.MEDIUM,
            expected_cost_usd=0.0,
            expected_latency_s=0.0,
            max_budget_usd=0.0,
            policy_version=self._config.default_policy_version,
        )
