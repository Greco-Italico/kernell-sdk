import time
import threading
import secrets
from enum import Enum
from typing import Callable, TypeVar, Optional

T = TypeVar("T")


class CircuitState(Enum):
    CLOSED = "closed"      # Normal operation
    OPEN = "open"          # Failing, reject fast
    HALF_OPEN = "half_open"  # Testing recovery


class CircuitBreaker:
    """
    Prevents cascading failures by fast-failing when a downstream dependency
    (Firecracker, vsock, snapshot restore) is consistently broken.
    """

    def __init__(self, failure_threshold: int = 5, recovery_timeout: float = 10.0, name: str = "default"):
        self.failure_threshold = failure_threshold
        self.recovery_timeout = recovery_timeout
        self.name = name

        self.state = CircuitState.CLOSED
        self.failure_count = 0
        self.last_failure_time = 0.0
        self.lock = threading.Lock()

    def call(self, fn: Callable[..., T], *args, **kwargs) -> T:
        """Execute fn through the circuit breaker."""
        with self.lock:
            if self.state == CircuitState.OPEN:
                if time.time() - self.last_failure_time >= self.recovery_timeout:
                    self.state = CircuitState.HALF_OPEN
                else:
                    raise CircuitOpenError(
                        f"Circuit '{self.name}' is OPEN — "
                        f"fast-failing after {self.failure_count} consecutive errors. "
                        f"Recovery in {self.recovery_timeout - (time.time() - self.last_failure_time):.1f}s"
                    )

        try:
            result = fn(*args, **kwargs)
            with self.lock:
                # Success: reset
                self.failure_count = 0
                self.state = CircuitState.CLOSED
            return result
        except Exception as e:
            with self.lock:
                self.failure_count += 1
                self.last_failure_time = time.time()
                if self.failure_count >= self.failure_threshold:
                    self.state = CircuitState.OPEN
            raise


class CircuitOpenError(Exception):
    """Raised when circuit breaker is open and rejecting calls."""
    pass


def retry_with_jitter(
    fn: Callable[..., T],
    max_retries: int = 3,
    base_delay: float = 0.1,
    max_delay: float = 2.0,
    jitter_factor: float = 0.5,
    retryable_exceptions: tuple = (ConnectionRefusedError, OSError, TimeoutError),
) -> T:
    """
    Retries a function with exponential backoff + jitter.
    Prevents thundering herd on transient failures.
    """
    last_exception = None
    for attempt in range(max_retries + 1):
        try:
            return fn()
        except retryable_exceptions as e:
            last_exception = e
            if attempt == max_retries:
                break
            # Exponential backoff with jitter
            delay = min(base_delay * (2 ** attempt), max_delay)
            jitter = delay * jitter_factor * secrets.SystemRandom().random()
            time.sleep(delay + jitter)

    raise last_exception
