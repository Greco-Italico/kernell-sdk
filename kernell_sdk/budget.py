"""
Kernell OS SDK — Token Budget Guard
════════════════════════════════════
Controls token consumption per agent.
Prevents runaway loops from burning through API credits.

Usage:
    from kernell_sdk.budget import TokenBudget

    budget = TokenBudget(hourly_limit=50_000, daily_limit=200_000)

    if budget.can_spend(estimated_tokens=2000):
        result = llm.call(prompt)
        budget.record(result.usage.total_tokens)
    else:
        print("Budget exceeded — switch to cheaper model")
"""
import time
import logging
import threading
from typing import Optional, Dict
from dataclasses import dataclass, field

logger = logging.getLogger("kernell.budget")


@dataclass
class BudgetSnapshot:
    """Current state of the token budget."""
    hourly_used: int = 0
    daily_used: int = 0
    total_used: int = 0
    hourly_limit: int = 0
    daily_limit: int = 0
    hourly_remaining: int = 0
    daily_remaining: int = 0
    is_throttled: bool = False
    throttle_reason: str = ""


class TokenBudget:
    """
    Rate-limits token consumption per hour and per day.
    Inspired by Kernell OS core/token_budget_guard.py.
    """

    def __init__(
        self,
        agent_name: str = "default",
        hourly_limit: int = 50_000,
        daily_limit: int = 200_000,
    ):
        self.agent_name = agent_name
        self.hourly_limit = hourly_limit
        self.daily_limit = daily_limit

        self._hourly_used: int = 0
        self._daily_used: int = 0
        self._total_used: int = 0

        self._hour_start: float = time.time()
        self._day_start: float = time.time()

        self._consecutive_overages: int = 0
        self._lock = threading.Lock()  # Thread-safety for concurrent agents

    def _rotate_windows(self):
        """Reset counters when the time window expires."""
        now = time.time()
        if now - self._hour_start >= 3600:
            logger.debug(f"[{self.agent_name}] Hourly window reset. Used: {self._hourly_used}")
            self._hourly_used = 0
            self._hour_start = now
        if now - self._day_start >= 86400:
            logger.debug(f"[{self.agent_name}] Daily window reset. Used: {self._daily_used}")
            self._daily_used = 0
            self._day_start = now

    def can_spend(self, estimated_tokens: int = 0) -> bool:
        """Check if the agent can spend the estimated number of tokens."""
        with self._lock:
            self._rotate_windows()

            if self._hourly_used + estimated_tokens > self.hourly_limit:
                self._consecutive_overages += 1
                logger.warning(
                    f"[{self.agent_name}] HOURLY budget would exceed: "
                    f"{self._hourly_used + estimated_tokens}/{self.hourly_limit}"
                )
                return False

            if self._daily_used + estimated_tokens > self.daily_limit:
                self._consecutive_overages += 1
                logger.warning(
                    f"[{self.agent_name}] DAILY budget would exceed: "
                    f"{self._daily_used + estimated_tokens}/{self.daily_limit}"
                )
                return False

            self._consecutive_overages = 0
            return True

    def record(self, tokens_used: int):
        """Record actual tokens consumed after an LLM call."""
        with self._lock:
            self._rotate_windows()
            self._hourly_used += tokens_used
            self._daily_used += tokens_used
            self._total_used += tokens_used
            logger.debug(
                f"[{self.agent_name}] Recorded {tokens_used} tokens. "
                f"Hourly: {self._hourly_used}/{self.hourly_limit} | "
                f"Daily: {self._daily_used}/{self.daily_limit}"
            )

    def snapshot(self) -> BudgetSnapshot:
        """Get a point-in-time snapshot of the budget state."""
        self._rotate_windows()
        throttled = self._hourly_used >= self.hourly_limit or self._daily_used >= self.daily_limit
        reason = ""
        if self._hourly_used >= self.hourly_limit:
            reason = "hourly_limit_reached"
        elif self._daily_used >= self.daily_limit:
            reason = "daily_limit_reached"

        return BudgetSnapshot(
            hourly_used=self._hourly_used,
            daily_used=self._daily_used,
            total_used=self._total_used,
            hourly_limit=self.hourly_limit,
            daily_limit=self.daily_limit,
            hourly_remaining=max(0, self.hourly_limit - self._hourly_used),
            daily_remaining=max(0, self.daily_limit - self._daily_used),
            is_throttled=throttled,
            throttle_reason=reason,
        )

    def suggest_model_tier(self, estimated_tokens: int = 2000) -> str:
        """Suggest which model tier to use based on remaining budget."""
        snap = self.snapshot()
        hourly_pct = snap.hourly_used / max(snap.hourly_limit, 1)
        daily_pct = snap.daily_used / max(snap.daily_limit, 1)
        max_pct = max(hourly_pct, daily_pct)

        if max_pct < 0.5:
            return "premium"   # Claude Opus / GPT-4o
        elif max_pct < 0.8:
            return "standard"  # Claude Sonnet / GPT-4o-mini
        elif max_pct < 0.95:
            return "economy"   # Haiku / Llama-3-8B local
        else:
            return "blocked"   # No budget left
