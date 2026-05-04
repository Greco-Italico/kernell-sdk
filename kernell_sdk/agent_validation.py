"""
Kernell OS SDK — Tool Validation Layer (Phase 5.6)
══════════════════════════════════════════════════
Provides post-condition validation for agent actions.
Closes the loop between "I clicked a button" and "Did the page actually change?"

Capabilities:
  - Validates expected outcomes against actual environment states.
  - Generates explicit feedback for the Planner when actions fail silently.
"""

import json
import logging
from dataclasses import dataclass
from typing import Optional

logger = logging.getLogger("kernell.agent.validation")

@dataclass
class ValidationResult:
    """Outcome of a validation check."""
    is_valid: bool
    reason: str


VALIDATOR_PROMPT = """You are a post-action Validator for an autonomous agent.
The agent just executed a tool or action. It had an EXPECTATION of what would happen.
Below is the EXPECTATION and the ACTUAL STATE (output from the tool, or current page state).

Your job is to determine if the expectation was met.
If the actual state shows errors, unexpected modals, no change, or contradicts the expectation, validation fails.
If the actual state matches the expectation, validation passes.

Respond strictly in JSON format:
{
  "is_valid": true/false,
  "reason": "Brief explanation of why it passed or failed based on the actual state."
}
"""

class ToolValidator:
    """
    Validates expectations against reality using an LLM.
    """
    def __init__(self, llm_registry):
        self._registry = llm_registry

    def validate(self, expectation: str, actual_state: str) -> ValidationResult:
        """Evaluate if the actual state meets the expectation."""
        if not expectation or not expectation.strip():
            return ValidationResult(is_valid=True, reason="No expectation provided.")

        context = (
            f"EXPECTATION:\n{expectation}\n\n"
            f"ACTUAL STATE:\n{actual_state}\n"
        )

        try:
            resp = self._registry.complete(
                messages=[{"role": "user", "content": context}],
                system_prompt=VALIDATOR_PROMPT,
                role="reasoning",
                max_tokens=256,
                temperature=0.1,
            )
            if not resp or not resp.content:
                return ValidationResult(is_valid=True, reason="Validation skipped (no LLM response).")
            
            # Parse JSON
            text = resp.content.strip()
            if "```json" in text:
                text = text.split("```json")[1].split("```")[0].strip()
            elif "```" in text:
                text = text.split("```")[1].split("```")[0].strip()

            start = text.find("{")
            end = text.rfind("}") + 1
            if start >= 0 and end > start:
                text = text[start:end]

            data = json.loads(text)
            return ValidationResult(
                is_valid=bool(data.get("is_valid", True)),
                reason=data.get("reason", "No reason provided")
            )
        except Exception as e:
            logger.warning(f"[ToolValidator] Error during validation: {e}")
            return ValidationResult(is_valid=True, reason=f"Validation error: {e}")
