"""
Kernell OS SDK — Shadow Mode Tests
════════════════════════════════════
Tests for the Shadow Proxy, counterfactual cost engine,
and FinOps Dashboard data generation.
"""
import time
import pytest
from unittest.mock import MagicMock

from kernell_sdk.shadow.proxy import (
    ShadowProxy, ShadowConfig, ShadowEvent, API_PRICING,
)


class TestShadowProxyObservation:
    """The proxy must observe without modifying anything."""

    def setup_method(self):
        self.proxy = ShadowProxy(ShadowConfig(agent_id="test-001"))

    def _mock_response(self, prompt_tokens=100, completion_tokens=50):
        resp = MagicMock()
        resp.usage = MagicMock()
        resp.usage.prompt_tokens = prompt_tokens
        resp.usage.completion_tokens = completion_tokens
        return resp

    def test_observe_records_event(self):
        resp = self._mock_response()
        event = self.proxy.observe(
            model="gpt-4o",
            messages=[{"role": "user", "content": "hello"}],
            response=resp,
            latency_ms=150.0,
        )
        assert event is not None
        assert event.original_model == "gpt-4o"
        assert event.original_tokens_in == 100
        assert event.original_tokens_out == 50
        assert event.agent_id == "test-001"

    def test_observe_computes_savings(self):
        resp = self._mock_response(prompt_tokens=100, completion_tokens=50)
        event = self.proxy.observe(
            model="gpt-4o",
            messages=[{"role": "user", "content": "short task"}],
            response=resp,
            latency_ms=200.0,
        )
        # gpt-4o costs more than local/cheap alternatives
        assert event.savings_usd >= 0
        assert event.savings_pct >= 0

    def test_observe_hashes_content(self):
        resp = self._mock_response()
        event = self.proxy.observe(
            model="gpt-4o",
            messages=[{"role": "user", "content": "Build a REST API"}],
            response=resp,
            latency_ms=100.0,
        )
        # Task hash must NOT contain the original text
        assert "Build" not in event.task_hash
        assert "REST" not in event.task_hash
        assert len(event.task_hash) == 16  # SHA-256 truncated

    def test_disabled_proxy_returns_none(self):
        proxy = ShadowProxy(ShadowConfig(enabled=False))
        resp = self._mock_response()
        event = proxy.observe(
            model="gpt-4o",
            messages=[{"role": "user", "content": "test"}],
            response=resp,
            latency_ms=50.0,
        )
        assert event is None


class TestCounterfactualCostEngine:
    """The counterfactual engine must compute accurate cost deltas."""

    def setup_method(self):
        self.proxy = ShadowProxy(ShadowConfig(agent_id="cost-test"))

    def test_cost_computation_gpt4o(self):
        # gpt-4o: $2.50/M input, $10.00/M output
        cost = self.proxy._compute_cost("gpt-4o", 1_000_000, 1_000_000)
        assert cost == pytest.approx(12.50, abs=0.01)

    def test_cost_computation_local(self):
        cost = self.proxy._compute_cost("local", 1_000_000, 1_000_000)
        assert cost == 0.0

    def test_cost_computation_deepseek(self):
        # deepseek-chat: $0.14/M input, $0.28/M output
        cost = self.proxy._compute_cost("deepseek-chat", 1_000_000, 1_000_000)
        assert cost == pytest.approx(0.42, abs=0.01)

    def test_savings_always_non_negative(self):
        resp = MagicMock()
        resp.usage = MagicMock()
        resp.usage.prompt_tokens = 50
        resp.usage.completion_tokens = 10
        event = self.proxy.observe(
            model="gpt-4o-mini",  # Already cheap
            messages=[{"role": "user", "content": "hi"}],
            response=resp,
            latency_ms=20.0,
        )
        assert event.savings_usd >= 0


class TestComplexityClassification:
    """Classification must be purely structural (no content analysis)."""

    def setup_method(self):
        self.proxy = ShadowProxy(ShadowConfig(agent_id="class-test"))

    def test_trivial_classification(self):
        assert self.proxy._classify_complexity(50, 20, 1) == "trivial"

    def test_easy_classification(self):
        assert self.proxy._classify_complexity(300, 100, 3) == "easy"

    def test_moderate_classification(self):
        assert self.proxy._classify_complexity(1000, 500, 5) == "moderate"

    def test_hard_classification(self):
        assert self.proxy._classify_complexity(3000, 1500, 10) == "hard"

    def test_extreme_classification(self):
        assert self.proxy._classify_complexity(4000, 2000, 20) == "extreme"


class TestDashboardDataGeneration:
    """Dashboard data must be complete and accurate."""

    def setup_method(self):
        self.proxy = ShadowProxy(ShadowConfig(agent_id="dash-test"))

    def _inject_events(self, count=10):
        for i in range(count):
            resp = MagicMock()
            resp.usage = MagicMock()
            resp.usage.prompt_tokens = 200 + i * 50
            resp.usage.completion_tokens = 100 + i * 20
            self.proxy.observe(
                model="gpt-4o",
                messages=[{"role": "user", "content": f"task {i}"}],
                response=resp,
                latency_ms=100.0 + i * 10,
            )

    def test_empty_dashboard(self):
        data = self.proxy.get_dashboard_data()
        assert data["baseline_spend"] == 0
        assert len(data.get("recent_events", data.get("events", []))) == 0

    def test_populated_dashboard(self):
        self._inject_events(20)
        data = self.proxy.get_dashboard_data()

        assert data["total_events"] == 20
        assert data["baseline_spend"] > 0
        assert data["verified_savings"] >= 0
        assert data["confidence"] > 0
        assert 0 <= data["savings_pct"] <= 100
        assert data["audit_coverage_pct"] == 100.0

    def test_routing_distribution(self):
        self._inject_events(10)
        data = self.proxy.get_dashboard_data()
        routing = data["routing"]

        # All events should be categorized
        total_routed = (
            routing["local"]["count"]
            + routing["cheap_api"]["count"]
            + routing["premium_api"]["count"]
        )
        assert total_routed == 10

    def test_recent_events_capped(self):
        self._inject_events(100)
        data = self.proxy.get_dashboard_data()
        assert len(data["recent_events"]) <= 50

    def test_savings_at_risk_from_low_confidence(self):
        self._inject_events(5)
        data = self.proxy.get_dashboard_data()
        # savings_at_risk should be a number (may be 0 if all routes are high-conf)
        assert isinstance(data["savings_at_risk"], (int, float))


class TestBufferManagement:
    """Buffer must be bounded and flushable."""

    def test_buffer_bounded(self):
        proxy = ShadowProxy(ShadowConfig(
            agent_id="buf-test", buffer_max_events=5
        ))
        for i in range(20):
            resp = MagicMock()
            resp.usage = MagicMock()
            resp.usage.prompt_tokens = 100
            resp.usage.completion_tokens = 50
            proxy.observe(
                model="gpt-4o",
                messages=[{"role": "user", "content": f"t{i}"}],
                response=resp,
                latency_ms=10.0,
            )
        # After flush, buffer should be smaller than max
        assert len(proxy._events) <= 5

    def test_stats_tracking(self):
        proxy = ShadowProxy(ShadowConfig(agent_id="stat-test"))
        resp = MagicMock()
        resp.usage = MagicMock()
        resp.usage.prompt_tokens = 100
        resp.usage.completion_tokens = 50
        proxy.observe(
            model="gpt-4o",
            messages=[{"role": "user", "content": "test"}],
            response=resp,
            latency_ms=50.0,
        )
        stats = proxy.stats
        assert stats["total_observed"] == 1
        assert stats["total_original_cost"] > 0
        assert stats["agent_id"] == "stat-test"
