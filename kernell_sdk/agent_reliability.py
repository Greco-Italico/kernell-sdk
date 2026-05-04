"""
Kernell OS SDK — Action Reliability Layer (Phase 6.5)
═════════════════════════════════════════════════════
Deterministic fallback, retry, and rollback policies for agent actions.
Prevents infinite LLM loops and orchestrates error recovery.

Capabilities:
  - FailurePolicy Engine: explicit rules for retries, backoffs, and limits.
  - Action State Tracking: monitors consecutive failures of the same tool.
  - Rollback Engine: protects World Model and Memory from contamination.
"""

import logging
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from kernell_sdk.observability.event_bus import GLOBAL_EVENT_BUS

logger = logging.getLogger("kernell.agent.reliability")

@dataclass
class FailurePolicy:
    """Explicit rules for handling agent execution failures."""
    max_retries_per_action: int = 3
    max_total_failures: int = 10
    retry_backoff: List[int] = field(default_factory=lambda: [1, 2, 5])
    on_validation_fail: str = "replan"  # replan | abort
    on_tool_crash: str = "retry"        # retry | replan | abort
    on_stuck_loop: str = "abort"        # force_replan | abort


class ReliabilityEngine:
    """
    Monitors execution history and enforces the FailurePolicy.
    Decides whether to allow an action, force a retry, or abort.
    """
    def __init__(self, policy: Optional[FailurePolicy] = None):
        self.policy = policy or FailurePolicy()
        self.action_failure_counts: Dict[str, int] = {}
        self.total_failures = 0

    def record_success(self, tool_name: str):
        """Clear failure counts for a tool on success."""
        if tool_name in self.action_failure_counts:
            self.action_failure_counts[tool_name] = 0

    def record_failure(self, tool_name: str) -> str:
        """
        Record a failure and return the deterministic directive:
        'retry', 'replan', or 'abort'
        """
        self.total_failures += 1
        current_fails = self.action_failure_counts.get(tool_name, 0) + 1
        self.action_failure_counts[tool_name] = current_fails

        if self.total_failures >= self.policy.max_total_failures:
            logger.error(f"[ARL] Max total failures reached ({self.total_failures}). Aborting.")
            GLOBAL_EVENT_BUS.emit("arl_block", "current", {"reason": "max_total_failures", "tool": tool_name})
            return "abort"

        if current_fails > self.policy.max_retries_per_action:
            logger.warning(f"[ARL] Tool '{tool_name}' failed {current_fails} times. Action stuck loop triggered.")
            GLOBAL_EVENT_BUS.emit("arl_block", "current", {"reason": "consecutive_failures", "tool": tool_name})
            return self.policy.on_stuck_loop

        # Apply backoff
        backoff_idx = min(current_fails - 1, len(self.policy.retry_backoff) - 1)
        sleep_time = self.policy.retry_backoff[backoff_idx]
        if sleep_time > 0:
            logger.info(f"[ARL] Backing off for {sleep_time}s before next step.")
            time.sleep(sleep_time)

        return self.policy.on_validation_fail

    def evaluate_plan(self, tool_name: str) -> bool:
        """
        Check if the LLM is trying to use a tool that is currently blacklisted
        due to max retries being exceeded.
        """
        if not tool_name:
            return True
        fails = self.action_failure_counts.get(tool_name, 0)
        return fails <= self.policy.max_retries_per_action

    def reset(self):
        self.action_failure_counts.clear()
        self.total_failures = 0
