"""
Kernell OS SDK — Swarm Budget Manager
═════════════════════════════════════
Transactional budget control for concurrent swarm execution.

Pattern: Reserve → Execute → Commit/Release
This prevents cost runaway when N agents run in parallel.
Thread-safe via asyncio.Lock.
"""

import asyncio
import logging
from dataclasses import dataclass

from kernell_sdk.observability.event_bus import GLOBAL_EVENT_BUS

logger = logging.getLogger("kernell.swarm.budget")


@dataclass
class BudgetState:
    """Transactional budget ledger."""
    total_budget: float
    reserved: float = 0.0
    spent: float = 0.0
    
    @property
    def available(self) -> float:
        return self.total_budget - self.reserved - self.spent


class SwarmBudgetManager:
    """
    Controls spend across all concurrent agents in a swarm.
    
    Uses a Reserve/Commit pattern (like database transactions):
    1. reserve(estimated) — lock funds before execution
    2. commit(actual, reserved) — settle the real cost
    3. release(reserved) — unlock if execution was cancelled
    """
    
    SAFETY_FACTOR = 1.3  # Over-reserve by 30% to prevent overruns
    
    def __init__(self, total_budget: float):
        self.state = BudgetState(total_budget=total_budget)
        self._lock = asyncio.Lock()
    
    async def reserve(self, estimated_cost: float) -> bool:
        """Try to reserve funds. Returns False if insufficient budget."""
        safe_cost = estimated_cost * self.SAFETY_FACTOR
        
        async with self._lock:
            if safe_cost > self.state.available:
                logger.warning(
                    f"[Budget] Reservation denied: ${safe_cost:.4f} > available ${self.state.available:.4f}"
                )
                GLOBAL_EVENT_BUS.emit("budget_denied", "current", {
                    "requested": safe_cost,
                    "available": self.state.available,
                })
                return False
            
            self.state.reserved += safe_cost
            logger.debug(f"[Budget] Reserved ${safe_cost:.4f} (available: ${self.state.available:.4f})")
            return True
    
    async def commit(self, actual_cost: float, reserved_cost: float):
        """Settle execution: release reservation, record actual spend."""
        async with self._lock:
            self.state.reserved -= reserved_cost * self.SAFETY_FACTOR
            self.state.spent += actual_cost
            
            GLOBAL_EVENT_BUS.emit("budget_committed", "current", {
                "actual_cost": actual_cost,
                "total_spent": self.state.spent,
                "remaining": self.state.available,
            })
    
    async def release(self, reserved_cost: float):
        """Release reservation without spending (e.g. execution cancelled)."""
        async with self._lock:
            self.state.reserved -= reserved_cost * self.SAFETY_FACTOR
            self.state.reserved = max(0, self.state.reserved)  # safety clamp
    
    def snapshot(self) -> dict:
        """Return current budget state for telemetry/display."""
        return {
            "total": self.state.total_budget,
            "reserved": self.state.reserved,
            "spent": self.state.spent,
            "available": self.state.available,
        }
