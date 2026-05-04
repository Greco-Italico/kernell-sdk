"""
Kernell OS SDK — Correlation ID & Distributed Tracing
══════════════════════════════════════════════════════
Propagates unique trace IDs across agent operations for debugging
and audit trail purposes.

Usage:
    from kernell_sdk.tracing import TraceContext, get_current_trace_id

    with TraceContext(agent_name="scraper", operation="fetch_data") as trace:
        trace.log_event("started", {"url": "https://..."})
        result = do_work()
        trace.log_event("completed", {"rows": len(result)})

    # trace.correlation_id is attached to all logs and memory entries
"""
import contextvars
import time
import uuid
import logging
from dataclasses import dataclass, field, asdict
from typing import Any, Dict, List, Optional

logger = logging.getLogger("kernell.tracing")

# Context variable for propagation across async/threaded calls
_current_cid: contextvars.ContextVar[Optional[str]] = contextvars.ContextVar(
    "kernell_correlation_id", default=None
)


def get_current_trace_id() -> Optional[str]:
    """Get the current correlation ID from the context."""
    return _current_cid.get()


@dataclass
class TraceSpan:
    """A single span within a trace."""
    correlation_id: str
    agent: str
    operation: str
    started_at: float = field(default_factory=time.time)
    ended_at: Optional[float] = None
    parent_id: Optional[str] = None
    status: str = "running"
    error: Optional[str] = None
    events: List[Dict[str, Any]] = field(default_factory=list)

    def finish(self, status: str = "ok", error: Optional[str] = None):
        self.ended_at = time.time()
        self.status = status
        self.error = error

    @property
    def duration_ms(self) -> Optional[float]:
        if self.ended_at:
            return round((self.ended_at - self.started_at) * 1000, 2)
        return None

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        d["duration_ms"] = self.duration_ms
        return d


class TraceContext:
    """
    Context manager for distributed tracing.
    Inspired by Kernell OS core/correlation_id.py.
    """

    def __init__(
        self,
        agent_name: str,
        operation: str,
        parent_id: Optional[str] = None,
    ):
        self.correlation_id = f"cid_{uuid.uuid4().hex[:12]}"
        self.span = TraceSpan(
            correlation_id=self.correlation_id,
            agent=agent_name,
            operation=operation,
            parent_id=parent_id or _current_cid.get(),
        )
        self._token: Optional[contextvars.Token] = None

    def __enter__(self) -> "TraceContext":
        self._token = _current_cid.set(self.correlation_id)
        logger.info(
            f"[TRACE:{self.correlation_id}] "
            f"{self.span.agent}.{self.span.operation} started"
        )
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        if exc_type:
            self.span.finish(status="error", error=str(exc_val))
            logger.error(
                f"[TRACE:{self.correlation_id}] "
                f"{self.span.agent}.{self.span.operation} FAILED: {exc_val}"
            )
        else:
            self.span.finish(status="ok")
            logger.info(
                f"[TRACE:{self.correlation_id}] "
                f"{self.span.agent}.{self.span.operation} completed "
                f"({self.span.duration_ms}ms)"
            )
        if self._token:
            _current_cid.reset(self._token)
        return False  # Don't suppress exceptions

    def log_event(self, event_name: str, metadata: Optional[Dict[str, Any]] = None):
        """Log a named event within this trace span."""
        entry = {
            "event": event_name,
            "timestamp": time.time(),
            "metadata": metadata or {},
        }
        self.span.events.append(entry)
        logger.debug(
            f"[TRACE:{self.correlation_id}] event: {event_name} "
            f"{metadata or ''}"
        )

    def inject(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        """Inject correlation ID into an outgoing payload (Redis, HTTP, etc.)."""
        payload["_cid"] = self.correlation_id
        payload["_agent"] = self.span.agent
        payload["_ts"] = time.time()
        return payload

    @staticmethod
    def extract(payload: Dict[str, Any]) -> Optional[str]:
        """Extract correlation ID from an incoming payload."""
        return payload.get("_cid")
