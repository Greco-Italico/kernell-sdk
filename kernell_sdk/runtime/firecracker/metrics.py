"""
Prometheus Metrics for Kernell OS Firecracker Runtime.

Exposes operational metrics at /metrics (default port 9090).
Designed for low-cardinality labels to keep Prometheus healthy.

Usage:
    from kernell_sdk.runtime.firecracker.metrics import Metrics, start_metrics_server
    start_metrics_server(port=9090)
"""

try:
    from prometheus_client import Counter, Histogram, Gauge, start_http_server
except ImportError:  # pragma: no cover - runtime fallback path
    class _NoOpMetric:
        def labels(self, *args, **kwargs):
            return self

        def observe(self, *args, **kwargs):
            return None

        def inc(self, *args, **kwargs):
            return None

        def set(self, *args, **kwargs):
            return None

    _NOOP = _NoOpMetric()

    def Counter(*args, **kwargs):  # type: ignore
        return _NOOP

    def Histogram(*args, **kwargs):  # type: ignore
        return _NOOP

    def Gauge(*args, **kwargs):  # type: ignore
        return _NOOP

    def start_http_server(*args, **kwargs):  # type: ignore
        return None

# ── Latency ──────────────────────────────────────────────────────────────────
EXECUTION_LATENCY = Histogram(
    "kernell_execution_latency_seconds",
    "End-to-end execution latency including VM acquire + vsock + cleanup",
    labelnames=["tenant_tier", "cold"],
    buckets=[0.001, 0.003, 0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0],
)

# ── Precision Bottleneck Profiling ───────────────────────────────────────────
SNAPSHOT_RESTORE_LATENCY = Histogram(
    "kernell_snapshot_restore_latency_seconds",
    "Time spent physically restoring a Firecracker microVM from snapshot",
    buckets=[0.001, 0.005, 0.01, 0.02, 0.05, 0.1, 0.2, 0.5],
)

VSOCK_CONNECT_LATENCY = Histogram(
    "kernell_vsock_connect_latency_seconds",
    "Time spent in the retry loop attempting to establish vsock connection",
    buckets=[0.001, 0.005, 0.01, 0.02, 0.05, 0.1, 0.2, 0.5],
)

QUEUE_WAIT_LATENCY = Histogram(
    "kernell_queue_wait_latency_seconds",
    "Time spent waiting in the Orchestrator/Scheduler queue before worker pick-up",
    labelnames=["tenant_tier"],
    buckets=[0.001, 0.005, 0.01, 0.05, 0.1, 0.2, 0.5, 1.0],
)

# ── Throughput ───────────────────────────────────────────────────────────────
REQUESTS_TOTAL = Counter(
    "kernell_requests_total",
    "Total execution requests by outcome",
    labelnames=["tenant_tier", "status"],
)

# ── Pool ─────────────────────────────────────────────────────────────────────
COLD_STARTS_TOTAL = Counter(
    "kernell_cold_starts_total",
    "Total cold-start fallbacks (pool miss)",
)

POOL_SIZE = Gauge(
    "kernell_snapshot_pool_size",
    "Current number of warm VMs in the snapshot pool",
)

POOL_TARGET_SIZE = Gauge(
    "kernell_snapshot_pool_target",
    "Target pool size set by the auto-scaler",
)

# ── Backpressure ─────────────────────────────────────────────────────────────
INFLIGHT_REQUESTS = Gauge(
    "kernell_inflight_requests",
    "Currently executing requests (global)",
)

# ── Admission ────────────────────────────────────────────────────────────────
REJECTED_TOTAL = Counter(
    "kernell_rejected_total",
    "Total rejected requests by reason",
    labelnames=["reason"],
)

# ── Billing ──────────────────────────────────────────────────────────────────
CREDITS_CONSUMED = Counter(
    "kernell_credits_consumed_total",
    "Total credits consumed across all tenants",
    labelnames=["tenant_tier"],
)

# ── Resilience ───────────────────────────────────────────────────────────────
CIRCUIT_OPENS = Counter(
    "kernell_circuit_breaker_opens_total",
    "Total times a circuit breaker tripped open",
    labelnames=["breaker"],
)


def start_metrics_server(port: int = 9090):
    """Start the Prometheus /metrics HTTP endpoint."""
    start_http_server(port)
