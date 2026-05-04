"""
Kernell OS SDK — Self-Verification Layer
══════════════════════════════════════════
Validates model outputs BEFORE escalating to a more expensive tier.

Inspired by AutoMix (NeurIPS 2024): the model attempts, self-verifies,
and only escalates if confidence is below threshold.

This prevents unnecessary premium API calls caused by:
  - Classifier imprecision (subtask was actually easier than rated)
  - Local model succeeding despite low classifier confidence
  - Intermittent quality variance in local models
"""
from __future__ import annotations

import json
import logging
from typing import Protocol, Optional

logger = logging.getLogger("kernell.router.verifier")


VERIFIER_PROMPT = """\
You are a quality verifier. Evaluate if the following output correctly fulfills the task.

Task: {task}

Output to verify:
{output}

Respond ONLY with JSON:
{{"valid": true/false, "confidence": 0.0-1.0, "reason": "brief explanation"}}
"""


class LLMBackend(Protocol):
    """Protocol for any LLM that can generate text."""
    def generate(self, prompt: str, system: str = "") -> str: ...


class SelfVerifier:
    """
    Validates outputs before allowing escalation.
    
    Flow:
      local_model.run(task) → output
      verifier.verify(task, output) → {valid, confidence}
      
      if valid AND confidence > threshold:
          return output (SAVED premium call)
      else:
          escalate to next tier
    """

    def __init__(
        self,
        model: LLMBackend,
        confidence_threshold: float = 0.70,
    ):
        self._model = model
        self._threshold = confidence_threshold
        self._total_checks = 0
        self._prevented_escalations = 0

    def verify(self, task: str, output: str) -> VerificationResult:
        """
        Verify if an output correctly fulfills a task.
        
        Returns:
            VerificationResult with valid flag and confidence score
        """
        self._total_checks += 1

        prompt = VERIFIER_PROMPT.format(task=task, output=output)

        try:
            raw = self._model.generate(prompt)
            result = self._parse_result(raw)
        except Exception as e:
            logger.warning(f"Verifier error: {e}, defaulting to pass-through")
            result = VerificationResult(valid=True, confidence=0.5, reason="verifier_error")

        if result.should_accept(self._threshold):
            self._prevented_escalations += 1

        return result

    def _parse_result(self, raw: str) -> VerificationResult:
        """Parse the verifier's JSON output."""
        raw = raw.strip()

        # Find JSON object
        start = raw.find("{")
        end = raw.rfind("}")
        if start == -1 or end == -1:
            return VerificationResult(valid=True, confidence=0.5, reason="parse_fallback")

        try:
            data = json.loads(raw[start:end + 1])
            return VerificationResult(
                valid=bool(data.get("valid", True)),
                confidence=float(data.get("confidence", 0.5)),
                reason=str(data.get("reason", "")),
            )
        except (json.JSONDecodeError, ValueError):
            return VerificationResult(valid=True, confidence=0.5, reason="json_fallback")

    @property
    def stats(self) -> dict:
        return {
            "total_checks": self._total_checks,
            "prevented_escalations": self._prevented_escalations,
            "prevention_rate": (
                self._prevented_escalations / self._total_checks * 100
                if self._total_checks > 0 else 0.0
            ),
        }


class VerificationResult:
    """Result of a self-verification check."""

    def __init__(self, valid: bool, confidence: float, reason: str = ""):
        self.valid = valid
        self.confidence = confidence
        self.reason = reason

    def should_accept(self, threshold: float) -> bool:
        """Returns True if the output should be accepted without escalation."""
        return self.valid and self.confidence >= threshold

    def __repr__(self) -> str:
        return f"VerificationResult(valid={self.valid}, confidence={self.confidence:.2f})"
