"""
Kernell OS SDK — SLO Monitor
═════════════════════════════
Tracks uptime, error rate, and latency for each agent.
"""
import time, math, logging
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Callable
from enum import Enum

logger = logging.getLogger("kernell.health")

class HealthStatus(str, Enum):
    HEALTHY = "HEALTHY"
    DEGRADED = "DEGRADED"
    CRITICAL = "CRITICAL"

@dataclass
class SLOScore:
    agent: str
    status: HealthStatus
    uptime_pct: float
    error_rate_pct: float
    latency_p95_ms: float
    total_events: int
    error_events: int
    violations: List[str]

class SLOMonitor:
    def __init__(self, agent_name: str, targets: Optional[Dict[str, float]] = None,
                 window_seconds: int = 3600, on_violation: Optional[Callable] = None):
        self.agent_name = agent_name
        self.targets = {"uptime_pct": 99.0, "error_rate_max": 2.0, "latency_p95_ms": 15000, **(targets or {})}
        self.window_seconds = window_seconds
        self.on_violation = on_violation
        self._window_start = time.time()
        self._total = 0
        self._errors = 0
        self._latencies: List[float] = []

    def _rotate(self):
        if time.time() - self._window_start >= self.window_seconds:
            self._window_start = time.time()
            self._total = self._errors = 0
            self._latencies.clear()

    def record_success(self, latency_ms: float = 0.0):
        self._rotate()
        self._total += 1
        if latency_ms > 0: self._latencies.append(latency_ms)

    def record_error(self, latency_ms: float = 0.0, reason: str = ""):
        self._rotate()
        self._total += 1
        self._errors += 1
        if latency_ms > 0: self._latencies.append(latency_ms)

    def score(self) -> SLOScore:
        self._rotate()
        violations = []
        uptime = ((self._total - self._errors) / max(self._total, 1)) * 100
        err_rate = (self._errors / max(self._total, 1)) * 100
        p95 = sorted(self._latencies)[int(len(self._latencies) * 0.95)] if self._latencies else 0.0
        if uptime < self.targets["uptime_pct"]: violations.append(f"Uptime {uptime:.1f}%")
        if err_rate > self.targets["error_rate_max"]: violations.append(f"Error rate {err_rate:.1f}%")
        if p95 > self.targets["latency_p95_ms"]: violations.append(f"P95 {p95:.0f}ms")
        status = HealthStatus.HEALTHY if not violations else (HealthStatus.DEGRADED if len(violations) == 1 else HealthStatus.CRITICAL)
        return SLOScore(self.agent_name, status, round(uptime, 2), round(err_rate, 2), round(p95, 2), self._total, self._errors, violations)
