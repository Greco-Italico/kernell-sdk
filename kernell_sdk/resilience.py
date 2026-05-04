"""
Kernell OS SDK — Circuit Breaker
═════════════════════════════════
Prevents cascading failures when external services or skills fail repeatedly.
Implements the Netflix Hystrix pattern: CLOSED → OPEN → HALF_OPEN.

Usage:
    from kernell_sdk.resilience import CircuitBreaker

    cb = CircuitBreaker("my_api", failure_threshold=3, recovery_timeout=300)

    if cb.can_execute():
        try:
            result = call_external_api()
            cb.record_success()
        except Exception as e:
            cb.record_failure(str(e))
    else:
        print(f"Circuit OPEN — blocked until {cb.time_until_half_open()}s")
"""
import time
import logging
import threading
from enum import Enum
from typing import Optional, Callable, Any
from dataclasses import dataclass, field

logger = logging.getLogger("kernell.resilience")


class CircuitState(str, Enum):
    CLOSED = "CLOSED"       # Normal operation
    OPEN = "OPEN"           # Blocked — too many failures
    HALF_OPEN = "HALF_OPEN" # Testing recovery


@dataclass
class CircuitStats:
    """Point-in-time stats for a circuit breaker."""
    name: str
    state: str
    consecutive_failures: int
    consecutive_successes: int
    total_failures: int
    total_successes: int
    last_failure_reason: str
    time_until_half_open: float


class CircuitBreaker:
    """
    Circuit Breaker pattern for agent skill execution.
    Inspired by Kernell OS core/circuit_breaker.py.
    """

    def __init__(
        self,
        name: str,
        failure_threshold: int = 3,
        success_threshold: int = 2,
        recovery_timeout: float = 300.0,
        on_open: Optional[Callable[[str], None]] = None,
    ):
        self.name = name
        self.failure_threshold = failure_threshold
        self.success_threshold = success_threshold
        self.recovery_timeout = recovery_timeout
        self.on_open = on_open  # Callback when circuit opens (e.g., send alert)

        self._state: CircuitState = CircuitState.CLOSED
        self._consecutive_failures: int = 0
        self._consecutive_successes: int = 0
        self._total_failures: int = 0
        self._total_successes: int = 0
        self._trips: int = 0  # Total times circuit has opened
        self._last_failure_time: float = 0.0
        self._last_failure_reason: str = ""
        self._opened_at: float = 0.0
        self._lock = threading.Lock()

    def _maybe_open_to_half_open_unlocked(self) -> None:
        """Caller must hold ``_lock``. OPEN → HALF_OPEN after recovery timeout."""
        if self._state == CircuitState.OPEN and time.time() - self._opened_at >= self.recovery_timeout:
            self._state = CircuitState.HALF_OPEN
            logger.info(f"[{self.name}] Circuit transitioned to HALF_OPEN (testing recovery)")

    @property
    def state(self) -> CircuitState:
        """Current state with automatic OPEN → HALF_OPEN transition."""
        with self._lock:
            self._maybe_open_to_half_open_unlocked()
            return self._state

    def can_execute(self) -> bool:
        """Returns True if the action is allowed to execute."""
        with self._lock:
            self._maybe_open_to_half_open_unlocked()
            if self._state == CircuitState.CLOSED:
                return True
            if self._state == CircuitState.HALF_OPEN:
                return True  # Allow one test call
            return False  # OPEN

    def record_success(self):
        """Record a successful execution."""
        with self._lock:
            self._total_successes += 1
            self._consecutive_failures = 0
            self._consecutive_successes += 1

            if self._state == CircuitState.HALF_OPEN:
                if self._consecutive_successes >= self.success_threshold:
                    self._state = CircuitState.CLOSED
                    self._consecutive_successes = 0
                    logger.info(f"[{self.name}] Circuit CLOSED — recovered after {self.success_threshold} successes")

    def record_failure(self, reason: str = "unknown"):
        """Record a failed execution."""
        with self._lock:
            self._total_failures += 1
            self._consecutive_failures += 1
            self._consecutive_successes = 0
            self._last_failure_time = time.time()
            self._last_failure_reason = reason

            if self._state == CircuitState.HALF_OPEN:
                # Immediate trip back to OPEN
                self._state = CircuitState.OPEN
                self._opened_at = time.time()
                logger.warning(f"[{self.name}] Circuit re-OPENED from HALF_OPEN: {reason}")

            elif self._consecutive_failures >= self.failure_threshold:
                self._state = CircuitState.OPEN
                self._opened_at = time.time()
                self._trips += 1
                logger.warning(
                    f"[{self.name}] Circuit OPENED (trip #{self._trips}) after {self._consecutive_failures} "
                    f"consecutive failures. Last: {reason}"
                )
                if self.on_open:
                    try:
                        self.on_open(f"Circuit '{self.name}' opened: {reason}")
                    except Exception as e:
                        import logging
                        logging.warning(f'Suppressed error in {__name__}: {e}')

    def reset(self):
        """Manually reset the circuit breaker to CLOSED."""
        with self._lock:
            self._state = CircuitState.CLOSED
            self._consecutive_failures = 0
            self._consecutive_successes = 0
            logger.info(f"[{self.name}] Circuit manually reset to CLOSED")

    def time_until_half_open(self) -> float:
        """Seconds until the circuit transitions from OPEN to HALF_OPEN."""
        with self._lock:
            self._maybe_open_to_half_open_unlocked()
            if self._state != CircuitState.OPEN:
                return 0.0
            elapsed = time.time() - self._opened_at
            return max(0.0, self.recovery_timeout - elapsed)

    @property
    def trips(self) -> int:
        """Total number of times this circuit has tripped open."""
        return self._trips

    def stats(self) -> CircuitStats:
        """Get current circuit breaker statistics."""
        return CircuitStats(
            name=self.name,
            state=self.state.value,
            consecutive_failures=self._consecutive_failures,
            consecutive_successes=self._consecutive_successes,
            total_failures=self._total_failures,
            total_successes=self._total_successes,
            last_failure_reason=self._last_failure_reason,
            time_until_half_open=self.time_until_half_open(),
        )

    def execute(self, func: Callable[..., Any], *args, **kwargs) -> Any:
        """Execute a function with circuit breaker protection."""
        if not self.can_execute():
            raise CircuitOpenError(
                f"Circuit '{self.name}' is OPEN. "
                f"Try again in {self.time_until_half_open():.0f}s"
            )
        try:
            result = func(*args, **kwargs)
            self.record_success()
            return result
        except Exception as e:
            self.record_failure(str(e))
            raise


class CircuitOpenError(Exception):
    """Raised when trying to execute through an open circuit."""
    pass


# ── Registry (ported from core/circuit_breaker.py) ────────────────────────────

class CircuitBreakerRegistry:
    """
    Global registry of circuit breakers. Singleton pattern.
    Ported from Kernell OS monorepo core/circuit_breaker.py.

    Usage:
        from kernell_sdk.resilience import CircuitBreakerRegistry

        cb = CircuitBreakerRegistry.get("llm:openai", failure_threshold=3)
        if cb.can_execute():
            result = call_openai()
            cb.record_success()
    """

    _breakers: dict[str, CircuitBreaker] = {}
    _on_open_default: Optional[Callable] = None

    @classmethod
    def configure(cls, on_open: Optional[Callable] = None):
        """Set default callbacks for all new circuit breakers."""
        cls._on_open_default = on_open

    @classmethod
    def get(
        cls,
        name: str,
        failure_threshold: int = 3,
        success_threshold: int = 2,
        recovery_timeout: float = 300.0,
        on_open: Optional[Callable] = None,
    ) -> CircuitBreaker:
        """Get or create a circuit breaker by name."""
        if name not in cls._breakers:
            cls._breakers[name] = CircuitBreaker(
                name=name,
                failure_threshold=failure_threshold,
                success_threshold=success_threshold,
                recovery_timeout=recovery_timeout,
                on_open=on_open or cls._on_open_default,
            )
        return cls._breakers[name]

    @classmethod
    def status_all(cls) -> dict[str, CircuitStats]:
        """Get stats for all registered circuit breakers."""
        return {name: cb.stats() for name, cb in cls._breakers.items()}

    @classmethod
    def reset_all(cls):
        """Reset all circuit breakers to CLOSED."""
        for cb in cls._breakers.values():
            cb.reset()

    @classmethod
    def open_circuits(cls) -> list[str]:
        """List names of all currently OPEN circuits."""
        return [name for name, cb in cls._breakers.items() if cb.state == CircuitState.OPEN]

    @classmethod
    def summary(cls) -> dict[str, int]:
        """Quick summary: counts by state."""
        counts = {"CLOSED": 0, "OPEN": 0, "HALF_OPEN": 0}
        for cb in cls._breakers.values():
            counts[cb.state.value] += 1
        return counts
