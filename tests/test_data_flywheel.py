"""
Kernell OS SDK — Data Flywheel Tests
═════════════════════════════════════
Tests for the distributed telemetry collector and Classifier-Pro client.
"""
import time
import pytest
from unittest.mock import MagicMock, patch

from kernell_sdk.router.telemetry_collector import (
    TelemetryCollector, TelemetryConfig, TelemetryEvent,
)
from kernell_sdk.router.classifier_pro import (
    ClassifierProClient, ClassifierProConfig, ProClassification,
)
from kernell_sdk.router.types import ModelTier, ExecutionResult


# ═══════════════════════════════════════════════════════════════════════════
# TELEMETRY COLLECTOR
# ═══════════════════════════════════════════════════════════════════════════

class TestTelemetryPrivacy:
    """Telemetry must respect user privacy at all times."""

    def test_disabled_by_default(self):
        collector = TelemetryCollector()
        assert collector._config.enabled is False
        assert collector._config.consent_given is False

    def test_no_collection_without_consent(self):
        collector = TelemetryCollector(TelemetryConfig(enabled=True, consent_given=False))
        event = TelemetryEvent(
            sdk_instance_id="test", event_id="e1", timestamp=time.time(),
            task_hash="test_task", task_token_count=10, task_domain="code",
            predicted_difficulty=2, predicted_tier="local_small",
            classifier_confidence=0.8, actual_tier_used="local_small",
            was_escalated=False,
        )
        collector.record(event)
        assert len(collector._buffer) == 0

    def test_collection_with_consent(self):
        config = TelemetryConfig(enabled=True, consent_given=True)
        collector = TelemetryCollector(config)
        event = TelemetryEvent(
            sdk_instance_id="test", event_id="e1", timestamp=time.time(),
            task_hash="test_task", task_token_count=10, task_domain="code",
            predicted_difficulty=2, predicted_tier="local_small",
            classifier_confidence=0.8, actual_tier_used="local_small",
            was_escalated=False,
        )
        collector.record(event)
        assert len(collector._buffer) == 1

    def test_task_content_is_hashed(self):
        config = TelemetryConfig(enabled=True, consent_given=True, anonymize_tasks=True)
        collector = TelemetryCollector(config)
        event = TelemetryEvent(
            sdk_instance_id="test", event_id="e1", timestamp=time.time(),
            task_hash="Build a REST API with authentication",
            task_token_count=10, task_domain="code",
            predicted_difficulty=3, predicted_tier="local_medium",
            classifier_confidence=0.7, actual_tier_used="local_medium",
            was_escalated=False,
        )
        collector.record(event)
        stored = collector._buffer[0]
        # Original text must NOT be in the hash
        assert stored.task_hash != "Build a REST API with authentication"
        assert len(stored.task_hash) == 16  # SHA-256 truncated

    def test_user_can_inspect_buffer(self):
        config = TelemetryConfig(enabled=True, consent_given=True)
        collector = TelemetryCollector(config)
        event = TelemetryEvent(
            sdk_instance_id="test", event_id="e1", timestamp=time.time(),
            task_hash="task", task_token_count=5, task_domain="general",
            predicted_difficulty=1, predicted_tier="local_nano",
            classifier_confidence=0.9, actual_tier_used="local_nano",
            was_escalated=False,
        )
        collector.record(event)
        inspection = collector.inspect_buffer()
        assert len(inspection) == 1
        assert isinstance(inspection[0], dict)
        assert "task_hash" in inspection[0]

    def test_user_can_purge_buffer(self):
        config = TelemetryConfig(enabled=True, consent_given=True)
        collector = TelemetryCollector(config)
        for i in range(10):
            event = TelemetryEvent(
                sdk_instance_id="test", event_id=f"e{i}", timestamp=time.time(),
                task_hash=f"task_{i}", task_token_count=5, task_domain="general",
                predicted_difficulty=1, predicted_tier="local_nano",
                classifier_confidence=0.9, actual_tier_used="local_nano",
                was_escalated=False,
            )
            collector.record(event)
        assert len(collector._buffer) == 10
        purged = collector.purge_buffer()
        assert purged == 10
        assert len(collector._buffer) == 0

    def test_disable_stops_collection(self):
        config = TelemetryConfig(enabled=True, consent_given=True)
        collector = TelemetryCollector(config)
        collector.disable()
        event = TelemetryEvent(
            sdk_instance_id="test", event_id="e1", timestamp=time.time(),
            task_hash="task", task_token_count=5, task_domain="general",
            predicted_difficulty=1, predicted_tier="local_nano",
            classifier_confidence=0.9, actual_tier_used="local_nano",
            was_escalated=False,
        )
        collector.record(event)
        assert len(collector._buffer) == 0


class TestTelemetryCollection:
    """Telemetry must collect correct data."""

    def test_buffer_bounded(self):
        config = TelemetryConfig(enabled=True, consent_given=True, max_buffer_size=5)
        collector = TelemetryCollector(config)
        for i in range(20):
            event = TelemetryEvent(
                sdk_instance_id="test", event_id=f"e{i}", timestamp=time.time(),
                task_hash=f"task_{i}", task_token_count=5, task_domain="general",
                predicted_difficulty=1, predicted_tier="local_nano",
                classifier_confidence=0.9, actual_tier_used="local_nano",
                was_escalated=False,
            )
            collector.record(event)
        assert len(collector._buffer) <= 5

    def test_stats_tracking(self):
        config = TelemetryConfig(enabled=True, consent_given=True)
        collector = TelemetryCollector(config)
        for i in range(3):
            event = TelemetryEvent(
                sdk_instance_id="test", event_id=f"e{i}", timestamp=time.time(),
                task_hash=f"task_{i}", task_token_count=5, task_domain="general",
                predicted_difficulty=1, predicted_tier="local_nano",
                classifier_confidence=0.9, actual_tier_used="local_nano",
                was_escalated=False,
            )
            collector.record(event)
        stats = collector.get_stats()
        assert stats["total_collected"] == 3
        assert stats["enabled"] is True

    def test_record_from_result(self):
        config = TelemetryConfig(enabled=True, consent_given=True)
        collector = TelemetryCollector(config)
        result = ExecutionResult(
            subtask_id="s1", output="ok", success=True,
            model_used="qwen3:1.7b", tier_used=ModelTier.LOCAL_SMALL,
            tokens_in=100, tokens_out=50, latency_ms=200,
        )
        collector.record_from_result(
            task="Test task", subtask_desc="do thing",
            predicted_difficulty=2, predicted_tier="local_small",
            confidence=0.85, result=result,
            hardware_tier="balanced", ram_gb=16,
        )
        assert len(collector._buffer) == 1
        assert collector._buffer[0].ram_bucket == "16gb"

    def test_ram_bucketing(self):
        assert TelemetryCollector._bucket_ram(2) == "4gb"
        assert TelemetryCollector._bucket_ram(8) == "8gb"
        assert TelemetryCollector._bucket_ram(16) == "16gb"
        assert TelemetryCollector._bucket_ram(32) == "32gb"
        assert TelemetryCollector._bucket_ram(128) == "64gb+"


# ═══════════════════════════════════════════════════════════════════════════
# CLASSIFIER-PRO CLIENT
# ═══════════════════════════════════════════════════════════════════════════

class TestClassifierProDecisions:
    """Classifier-Pro escalation logic must be correct."""

    def test_no_escalation_when_disabled(self):
        client = ClassifierProClient(ClassifierProConfig(enabled=False))
        assert client.should_consult_pro(confidence=0.3, difficulty=5) is False

    def test_escalation_on_low_confidence(self):
        client = ClassifierProClient(ClassifierProConfig(
            enabled=True, api_key="test", confidence_threshold=0.70,
        ))
        assert client.should_consult_pro(confidence=0.65, difficulty=2) is True
        assert client.should_consult_pro(confidence=0.80, difficulty=2) is False

    def test_escalation_on_high_difficulty(self):
        client = ClassifierProClient(ClassifierProConfig(
            enabled=True, api_key="test", difficulty_threshold=4,
        ))
        assert client.should_consult_pro(confidence=0.90, difficulty=4) is True
        assert client.should_consult_pro(confidence=0.90, difficulty=3) is False

    def test_escalation_on_high_cost(self):
        client = ClassifierProClient(ClassifierProConfig(
            enabled=True, api_key="test", cost_threshold_usd=0.10,
        ))
        assert client.should_consult_pro(confidence=0.90, difficulty=2, estimated_cost=0.50) is True
        assert client.should_consult_pro(confidence=0.90, difficulty=2, estimated_cost=0.05) is False

    def test_escalation_on_repeated_failures(self):
        client = ClassifierProClient(ClassifierProConfig(
            enabled=True, api_key="test", repeated_failure_count=2,
        ))
        assert client.should_consult_pro(confidence=0.90, difficulty=2, domain="code") is False
        client.record_domain_failure("code")
        client.record_domain_failure("code")
        assert client.should_consult_pro(confidence=0.90, difficulty=2, domain="code") is True
        client.reset_domain_failures("code")
        assert client.should_consult_pro(confidence=0.90, difficulty=2, domain="code") is False


class TestClassifierProFallback:
    """Classifier-Pro must always fallback safely."""

    def test_fallback_returns_local_subtasks(self):
        client = ClassifierProClient(ClassifierProConfig(
            enabled=True, api_key="test", fallback_to_local=True,
        ))
        local_subtasks = [{"difficulty": 2, "domain": "code"}]
        result = client._fallback_local(local_subtasks)
        assert result.source == "lite_fallback"
        assert result.subtasks == local_subtasks
        assert result.confidence == 0.5

    def test_rate_limiting(self):
        client = ClassifierProClient(ClassifierProConfig(
            enabled=True, api_key="test", max_requests_per_minute=2,
        ))
        assert client._check_rate_limit() is True
        assert client._check_rate_limit() is True
        assert client._check_rate_limit() is False  # 3rd request blocked

    def test_stats(self):
        client = ClassifierProClient(ClassifierProConfig(enabled=True, api_key="test"))
        stats = client.get_stats()
        assert stats["enabled"] is True
        assert stats["total_requests"] == 0
        assert stats["api_errors"] == 0

    def test_cache_key_deterministic(self):
        k1 = ClassifierProClient._cache_key("task A", "powerful")
        k2 = ClassifierProClient._cache_key("task A", "powerful")
        k3 = ClassifierProClient._cache_key("task B", "powerful")
        assert k1 == k2  # Same input = same key
        assert k1 != k3  # Different input = different key
