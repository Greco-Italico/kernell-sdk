"""
Tests for the Kernell OS SDK core modules.
Run with: python -m pytest tests/ -v
"""
import time
import json
import pytest
from pathlib import Path
from unittest.mock import patch, MagicMock


# ═══════════════════════════════════════════════════════════════
# Token Budget Tests
# ═══════════════════════════════════════════════════════════════

class TestTokenBudget:
    """Tests for the TokenBudget rate limiter."""

    def setup_method(self):
        from kernell_sdk.budget import TokenBudget
        self.budget = TokenBudget(
            agent_name="test_agent",
            hourly_limit=10_000,
            daily_limit=50_000,
        )

    def test_initial_state_allows_spending(self):
        assert self.budget.can_spend(1000) is True

    def test_rejects_when_hourly_limit_exceeded(self):
        self.budget.record(9_500)
        assert self.budget.can_spend(1_000) is False

    def test_rejects_when_daily_limit_exceeded(self):
        self.budget.record(49_500)
        assert self.budget.can_spend(1_000) is False

    def test_record_accumulates_correctly(self):
        self.budget.record(1_000)
        self.budget.record(2_000)
        snapshot = self.budget.snapshot()
        assert snapshot.hourly_used == 3_000
        assert snapshot.daily_used == 3_000
        assert snapshot.total_used == 3_000

    def test_snapshot_shows_remaining(self):
        self.budget.record(3_000)
        snapshot = self.budget.snapshot()
        assert snapshot.hourly_remaining == 7_000
        assert snapshot.daily_remaining == 47_000

    def test_throttled_flag_when_exceeded(self):
        self.budget.record(10_000)
        snapshot = self.budget.snapshot()
        assert snapshot.is_throttled is True
        assert snapshot.throttle_reason == "hourly_limit_reached"

    def test_suggest_model_tier_premium_when_low_usage(self):
        assert self.budget.suggest_model_tier() == "premium"

    def test_suggest_model_tier_economy_when_high_usage(self):
        self.budget.record(9_200)
        assert self.budget.suggest_model_tier() == "economy"

    def test_suggest_model_tier_blocked_when_full(self):
        self.budget.record(10_000)
        assert self.budget.suggest_model_tier() == "blocked"


# ═══════════════════════════════════════════════════════════════
# Circuit Breaker Tests
# ═══════════════════════════════════════════════════════════════

class TestCircuitBreaker:
    """Tests for the Circuit Breaker resilience pattern."""

    def setup_method(self):
        from kernell_sdk.resilience import CircuitBreaker
        self.cb = CircuitBreaker(
            name="test_circuit",
            failure_threshold=3,
            success_threshold=2,
            recovery_timeout=1.0,  # 1 second for fast tests
        )

    def test_starts_closed(self):
        from kernell_sdk.resilience import CircuitState
        assert self.cb.state == CircuitState.CLOSED

    def test_allows_execution_when_closed(self):
        assert self.cb.can_execute() is True

    def test_opens_after_threshold_failures(self):
        from kernell_sdk.resilience import CircuitState
        self.cb.record_failure("error 1")
        self.cb.record_failure("error 2")
        self.cb.record_failure("error 3")
        assert self.cb.state == CircuitState.OPEN

    def test_blocks_execution_when_open(self):
        for _ in range(3):
            self.cb.record_failure("fail")
        assert self.cb.can_execute() is False

    def test_transitions_to_half_open_after_timeout(self):
        from kernell_sdk.resilience import CircuitState
        for _ in range(3):
            self.cb.record_failure("fail")
        time.sleep(1.1)  # Wait for recovery timeout
        assert self.cb.state == CircuitState.HALF_OPEN

    def test_closes_after_success_threshold_in_half_open(self):
        from kernell_sdk.resilience import CircuitState
        for _ in range(3):
            self.cb.record_failure("fail")
        time.sleep(1.1)
        # Access .state to trigger the OPEN → HALF_OPEN transition
        assert self.cb.state == CircuitState.HALF_OPEN
        self.cb.record_success()
        self.cb.record_success()
        assert self.cb.state == CircuitState.CLOSED

    def test_reopens_on_failure_in_half_open(self):
        from kernell_sdk.resilience import CircuitState
        for _ in range(3):
            self.cb.record_failure("fail")
        time.sleep(1.1)
        assert self.cb.state == CircuitState.HALF_OPEN
        self.cb.record_failure("still broken")
        assert self.cb.state == CircuitState.OPEN

    def test_reset_returns_to_closed(self):
        from kernell_sdk.resilience import CircuitState
        for _ in range(3):
            self.cb.record_failure("fail")
        self.cb.reset()
        assert self.cb.state == CircuitState.CLOSED

    def test_execute_wrapper_records_success(self):
        result = self.cb.execute(lambda: 42)
        assert result == 42
        stats = self.cb.stats()
        assert stats.total_successes == 1

    def test_execute_wrapper_records_failure(self):
        from kernell_sdk.resilience import CircuitOpenError
        with pytest.raises(ValueError):
            self.cb.execute(lambda: (_ for _ in ()).throw(ValueError("boom")))

    def test_on_open_callback_fires(self):
        messages = []
        self.cb.on_open = lambda msg: messages.append(msg)
        for _ in range(3):
            self.cb.record_failure("fail")
        assert len(messages) == 1
        assert "opened" in messages[0]


# ═══════════════════════════════════════════════════════════════
# SLO Monitor Tests
# ═══════════════════════════════════════════════════════════════

class TestSLOMonitor:
    """Tests for the Service Level Objective monitor."""

    def setup_method(self):
        from kernell_sdk.health import SLOMonitor
        self.slo = SLOMonitor(
            agent_name="test_agent",
            targets={"uptime_pct": 95.0, "error_rate_max": 5.0, "latency_p95_ms": 1000},
        )

    def test_healthy_when_no_errors(self):
        from kernell_sdk.health import HealthStatus
        for _ in range(10):
            self.slo.record_success(latency_ms=100)
        score = self.slo.score()
        assert score.status == HealthStatus.HEALTHY

    def test_degraded_when_error_rate_exceeds_target(self):
        from kernell_sdk.health import HealthStatus
        for _ in range(90):
            self.slo.record_success()
        for _ in range(10):
            self.slo.record_error(reason="timeout")
        score = self.slo.score()
        assert score.error_rate_pct > 5.0
        assert score.status in (HealthStatus.DEGRADED, HealthStatus.CRITICAL)

    def test_counts_events_correctly(self):
        self.slo.record_success()
        self.slo.record_success()
        self.slo.record_error()
        score = self.slo.score()
        assert score.total_events == 3
        assert score.error_events == 1


# ═══════════════════════════════════════════════════════════════
# Tracing Tests
# ═══════════════════════════════════════════════════════════════

class TestTracing:
    """Tests for the distributed tracing context."""

    def test_trace_context_generates_correlation_id(self):
        from kernell_sdk.tracing import TraceContext
        with TraceContext(agent_name="test", operation="fetch") as trace:
            assert trace.correlation_id.startswith("cid_")

    def test_trace_context_sets_global_id(self):
        from kernell_sdk.tracing import TraceContext, get_current_trace_id
        with TraceContext(agent_name="test", operation="fetch") as trace:
            assert get_current_trace_id() == trace.correlation_id
        # After exit, the context is reset
        assert get_current_trace_id() is None

    def test_trace_logs_events(self):
        from kernell_sdk.tracing import TraceContext
        with TraceContext(agent_name="test", operation="fetch") as trace:
            trace.log_event("started", {"url": "https://example.com"})
            trace.log_event("completed")
        assert len(trace.span.events) == 2
        assert trace.span.events[0]["event"] == "started"

    def test_trace_records_duration(self):
        from kernell_sdk.tracing import TraceContext
        with TraceContext(agent_name="test", operation="fetch") as trace:
            time.sleep(0.05)
        assert trace.span.duration_ms is not None
        assert trace.span.duration_ms >= 40  # At least 40ms

    def test_inject_and_extract(self):
        from kernell_sdk.tracing import TraceContext
        with TraceContext(agent_name="test", operation="fetch") as trace:
            payload = trace.inject({"data": "hello"})
            assert payload["_cid"] == trace.correlation_id
            extracted_id = TraceContext.extract(payload)
            assert extracted_id == trace.correlation_id


# ═══════════════════════════════════════════════════════════════
# Token Estimator Tests
# ═══════════════════════════════════════════════════════════════

class TestTokenEstimator:
    """Tests for the local token estimation heuristic."""

    def test_estimate_returns_positive_for_nonempty_text(self):
        from kernell_sdk.token_estimator import estimate_tokens
        tokens = estimate_tokens("Hello, world!")
        assert tokens > 0

    def test_json_estimates_more_tokens_per_byte(self):
        from kernell_sdk.token_estimator import estimate_tokens
        text = '{"key": "value", "number": 42}'
        json_tokens = estimate_tokens(text, file_type="json")
        text_tokens = estimate_tokens(text, file_type="text")
        # JSON has 2 bytes/token vs text at 4 bytes/token → more tokens
        assert json_tokens > text_tokens

    def test_empty_string_returns_zero(self):
        from kernell_sdk.token_estimator import estimate_tokens
        assert estimate_tokens("") == 0

    def test_estimate_messages_counts_overhead(self):
        from kernell_sdk.token_estimator import estimate_messages_tokens
        messages = [
            {"role": "user", "content": "Hello"},
            {"role": "assistant", "content": "Hi there"},
        ]
        tokens = estimate_messages_tokens(messages)
        # Should include message overhead (4 tokens per message)
        assert tokens > 0


# ═══════════════════════════════════════════════════════════════
# Constants & Shared Utilities Tests
# ═══════════════════════════════════════════════════════════════

class TestConstants:
    """Tests for shared constants and utilities."""

    def test_valid_permissions_is_frozen(self):
        from kernell_sdk.constants import VALID_PERMISSIONS
        assert isinstance(VALID_PERMISSIONS, frozenset)

    def test_rate_limiter_allows_initial_requests(self):
        from kernell_sdk.constants import RateLimiter
        limiter = RateLimiter(max_requests=5, window_seconds=60)
        assert limiter.is_allowed("test_client") is True

    def test_rate_limiter_blocks_after_max(self):
        from kernell_sdk.constants import RateLimiter
        limiter = RateLimiter(max_requests=3, window_seconds=60)
        for _ in range(3):
            limiter.is_allowed("test_client")
        assert limiter.is_allowed("test_client") is False

    def test_audit_log_records_and_trims(self):
        from kernell_sdk.constants import AuditLog
        log = AuditLog(max_entries=5)
        for i in range(10):
            log.record("action", f"detail_{i}")
        recent = log.recent(count=10)
        assert len(recent) == 5  # Trimmed to max_entries

    def test_audit_log_recent_returns_latest(self):
        from kernell_sdk.constants import AuditLog
        log = AuditLog()
        log.record("first", "detail_1")
        log.record("second", "detail_2")
        entries = log.recent(count=1)
        assert entries[0]["action"] == "second"


# ═══════════════════════════════════════════════════════════════
# Tool Result Persister Tests
# ═══════════════════════════════════════════════════════════════

class TestToolResultPersister:
    """Tests for the tool output persistence system."""

    def test_small_output_passes_through(self, tmp_path):
        from kernell_sdk.persister import ToolResultPersister
        persister = ToolResultPersister("test", persist_dir=str(tmp_path))
        small_output = "just a small result"
        result = persister.maybe_persist(small_output, tool_name="bash")
        assert result == small_output  # Not persisted

    def test_large_output_is_persisted(self, tmp_path):
        from kernell_sdk.persister import ToolResultPersister
        persister = ToolResultPersister("test", persist_dir=str(tmp_path))
        large_output = "x" * 60_000  # Exceeds default 50k threshold
        result = persister.maybe_persist(large_output, tool_name="bash")
        assert "persisted to disk" in result.lower()
        assert persister.stats["persisted"] == 1

    def test_persisted_file_exists_on_disk(self, tmp_path):
        from kernell_sdk.persister import ToolResultPersister
        persister = ToolResultPersister("test", persist_dir=str(tmp_path))
        large_output = "data " * 20_000
        persister.maybe_persist(large_output, tool_name="grep")
        saved_files = list((tmp_path / "test").glob("grep_*"))
        assert len(saved_files) == 1
