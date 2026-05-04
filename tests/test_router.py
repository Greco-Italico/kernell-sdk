"""
Kernell OS SDK — Intelligent Router Test Suite
════════════════════════════════════════════════
Critical tests required BEFORE fine-tuning:

1. Decomposer determinism
2. Verifier consistency
3. Cache hit correctness
4. Routing decisions reproducible
5. Entrypoint fallback safety
6. Shadow mode zero-impact
7. Metrics no double-counting
8. Model registry hardware mapping
9. Cost estimator accuracy
10. Budget enforcement
"""
import json
import time
import pytest
from unittest.mock import MagicMock

from kernell_sdk.router.types import (
    SubTask, ExecutionResult, DifficultyLevel, ModelTier, TaskDomain, RouterStats,
)
from kernell_sdk.router.decomposer import TaskDecomposer, DecomposerTrainingCollector
from kernell_sdk.router.verifier import SelfVerifier, VerificationResult
from kernell_sdk.router.summarizer import RollingSummarizer
from kernell_sdk.router.model_registry import ModelRegistry, DEFAULT_CATALOG
from kernell_sdk.router.metrics import RouterMetricsCollector
from kernell_sdk.router.estimator import CostEstimator
from kernell_sdk.router.intelligent_router import IntelligentRouter
from kernell_sdk.router.entrypoint import RouterEntrypoint, RouterConfig


# ── Fixtures ─────────────────────────────────────────────────────────────

class FakeLLM:
    """Deterministic fake LLM for testing."""
    def __init__(self, response: str = "done"):
        self.response = response
        self.call_count = 0

    def generate(self, prompt: str, system: str = "") -> str:
        self.call_count += 1
        return self.response


class FakeDecomposerLLM:
    """Fake LLM that returns valid decomposition JSON."""
    def generate(self, prompt: str, system: str = "") -> str:
        return json.dumps([
            {"id": "s1", "description": "Extract data", "difficulty": 1, "domain": "data", "parallel_ok": True, "depends_on": []},
            {"id": "s2", "description": "Transform data", "difficulty": 2, "domain": "code", "parallel_ok": False, "depends_on": ["s1"]},
            {"id": "s3", "description": "Generate report", "difficulty": 3, "domain": "reasoning", "parallel_ok": False, "depends_on": ["s2"]},
        ])


class FakeVerifierLLM:
    """Fake LLM that returns valid verification JSON."""
    def __init__(self, valid: bool = True, confidence: float = 0.9):
        self.valid = valid
        self.confidence = confidence

    def generate(self, prompt: str, system: str = "") -> str:
        return json.dumps({"valid": self.valid, "confidence": self.confidence, "reason": "test"})


class FakeCache:
    """Minimal cache implementing the CacheBackend protocol."""
    def __init__(self):
        self._store = {}

    def query(self, prompt: str, model: str = ""):
        if prompt in self._store:
            entry = MagicMock()
            entry.response = self._store[prompt]
            return entry
        return None

    def store(self, prompt: str, response: str, model_used: str = "", tokens_used: int = 0):
        self._store[prompt] = response


# ═══════════════════════════════════════════════════════════════════════════
# TEST 1: Decomposer Determinism
# ═══════════════════════════════════════════════════════════════════════════

class TestDecomposerDeterminism:
    """The decomposer must produce identical output for identical input."""

    def test_same_input_same_output(self):
        llm = FakeDecomposerLLM()
        decomposer = TaskDecomposer(model=llm)
        
        result1 = decomposer.decompose("Build a REST API")
        result2 = decomposer.decompose("Build a REST API")
        
        assert len(result1) == len(result2)
        for s1, s2 in zip(result1, result2):
            assert s1.id == s2.id
            assert s1.difficulty == s2.difficulty
            assert s1.domain == s2.domain

    def test_subtask_structure(self):
        llm = FakeDecomposerLLM()
        decomposer = TaskDecomposer(model=llm)
        result = decomposer.decompose("Test task")
        
        assert len(result) == 3
        assert result[0].id == "s1"
        assert result[0].difficulty == DifficultyLevel.TRIVIAL
        assert result[1].difficulty == DifficultyLevel.EASY
        assert result[2].difficulty == DifficultyLevel.MEDIUM

    def test_dependency_preservation(self):
        llm = FakeDecomposerLLM()
        decomposer = TaskDecomposer(model=llm)
        result = decomposer.decompose("Test task")
        
        assert result[1].depends_on == ["s1"]
        assert result[2].depends_on == ["s2"]

    def test_fallback_on_invalid_json(self):
        llm = FakeLLM(response="this is not json at all")
        decomposer = TaskDecomposer(model=llm)
        result = decomposer.decompose("Test task")
        
        # Should fallback to single task
        assert len(result) == 1
        assert result[0].difficulty == DifficultyLevel.MEDIUM

    def test_difficulty_clamping(self):
        """Difficulty must be clamped to 1-5 range."""
        llm = FakeLLM(response=json.dumps([
            {"id": "s1", "description": "test", "difficulty": 0, "domain": "general"},
            {"id": "s2", "description": "test", "difficulty": 99, "domain": "general"},
        ]))
        decomposer = TaskDecomposer(model=llm)
        result = decomposer.decompose("Test")
        
        assert result[0].difficulty == DifficultyLevel.TRIVIAL  # clamped from 0
        assert result[1].difficulty == DifficultyLevel.EXPERT   # clamped from 99


# ═══════════════════════════════════════════════════════════════════════════
# TEST 2: Verifier Consistency
# ═══════════════════════════════════════════════════════════════════════════

class TestVerifierConsistency:
    """Verifier must be consistent and deterministic."""

    def test_accept_high_confidence(self):
        verifier = SelfVerifier(model=FakeVerifierLLM(valid=True, confidence=0.9))
        result = verifier.verify("task", "output")
        assert result.should_accept(0.70) is True

    def test_reject_low_confidence(self):
        verifier = SelfVerifier(model=FakeVerifierLLM(valid=True, confidence=0.3))
        result = verifier.verify("task", "output")
        assert result.should_accept(0.70) is False

    def test_reject_invalid(self):
        verifier = SelfVerifier(model=FakeVerifierLLM(valid=False, confidence=0.9))
        result = verifier.verify("task", "output")
        assert result.should_accept(0.70) is False

    def test_fallback_on_garbage(self):
        verifier = SelfVerifier(model=FakeLLM(response="totally broken output"))
        result = verifier.verify("task", "output")
        # Should not crash, return a safe default
        assert isinstance(result, VerificationResult)
        assert result.confidence == 0.5

    def test_escalation_prevention_counter(self):
        verifier = SelfVerifier(model=FakeVerifierLLM(valid=True, confidence=0.9))
        verifier.verify("task1", "output1")
        verifier.verify("task2", "output2")
        
        stats = verifier.stats
        assert stats["total_checks"] == 2
        assert stats["prevented_escalations"] == 2


# ═══════════════════════════════════════════════════════════════════════════
# TEST 3: Cache Hit Correctness
# ═══════════════════════════════════════════════════════════════════════════

class TestCacheCoherence:
    """Cache must return correct data and not serve stale results across routers."""

    def test_cache_hit_returns_stored_value(self):
        cache = FakeCache()
        cache.store("hello world", "response_123")
        entry = cache.query("hello world")
        assert entry is not None
        assert entry.response == "response_123"

    def test_cache_miss_returns_none(self):
        cache = FakeCache()
        assert cache.query("nonexistent") is None

    def test_router_uses_cache(self):
        cache = FakeCache()
        cache.store("Extract data", "cached_extraction_result")

        local = FakeLLM(response="local_result")
        classifier = FakeDecomposerLLM()

        router = IntelligentRouter(
            classifier=classifier,
            local_models={"local_nano": local},
            cache=cache,
        )
        results = router.execute("Test task")
        
        # s1 "Extract data" should be cache hit
        assert results[0].was_cached is True
        assert results[0].output == "cached_extraction_result"

    def test_cache_populated_after_execution(self):
        cache = FakeCache()
        local = FakeLLM(response="fresh_result")
        classifier = FakeDecomposerLLM()
        verifier = FakeVerifierLLM(valid=True, confidence=0.9)

        router = IntelligentRouter(
            classifier=classifier,
            local_models={"local_nano": local, "local_small": local, "local_medium": local},
            cache=cache,
            verifier_model=verifier,
        )
        router.execute("Test task")
        
        # Cache should now have entries (s2 and s3, s1 was cached)
        assert len(cache._store) > 0


# ═══════════════════════════════════════════════════════════════════════════
# TEST 4: Routing Decisions Reproducible
# ═══════════════════════════════════════════════════════════════════════════

class TestRoutingReproducibility:
    """Same task must produce same routing decisions."""

    def test_same_task_same_tiers(self):
        classifier = FakeDecomposerLLM()
        local = FakeLLM(response="result")

        router1 = IntelligentRouter(
            classifier=classifier,
            local_models={"local_nano": local, "local_small": local, "local_medium": local},
        )
        router2 = IntelligentRouter(
            classifier=classifier,
            local_models={"local_nano": local, "local_small": local, "local_medium": local},
        )

        r1 = router1.execute("Test task")
        r2 = router2.execute("Test task")

        assert len(r1) == len(r2)
        for a, b in zip(r1, r2):
            assert a.tier_used == b.tier_used
            assert a.success == b.success

    def test_topological_sort_preserves_order(self):
        classifier = FakeDecomposerLLM()  # s1 → s2 → s3
        local = FakeLLM(response="ok")

        router = IntelligentRouter(
            classifier=classifier,
            local_models={"local_nano": local, "local_small": local, "local_medium": local},
        )
        results = router.execute("Test")
        
        ids = [r.subtask_id for r in results]
        assert ids == ["s1", "s2", "s3"]  # Dependency order


# ═══════════════════════════════════════════════════════════════════════════
# TEST 5: Entrypoint Fallback Safety
# ═══════════════════════════════════════════════════════════════════════════

class TestEntrypointFallback:
    """Entrypoint must ALWAYS return a result, even when intelligent router crashes."""

    def test_fallback_on_intelligent_failure(self):
        legacy = MagicMock()
        legacy.complete.return_value = "legacy_result"

        broken_intelligent = MagicMock()
        broken_intelligent.execute.side_effect = RuntimeError("BOOM")

        config = RouterConfig(enable_intelligent_router=True, shadow_mode=False, canary_percent=1.0)
        entry = RouterEntrypoint(
            legacy_router=legacy,
            intelligent_router=broken_intelligent,
            config=config,
        )

        result = entry.route("test task", messages=[{"role": "user", "content": "test"}])
        assert result == "legacy_result"
        assert entry._fallbacks == 1

    def test_legacy_only_mode(self):
        legacy = MagicMock()
        legacy.complete.return_value = "legacy_only"

        config = RouterConfig(enable_intelligent_router=False)
        entry = RouterEntrypoint(legacy_router=legacy, config=config)

        result = entry.route("task", messages=[{"role": "user", "content": "test"}])
        assert result == "legacy_only"
        assert entry._legacy_used == 1
        assert entry._intelligent_used == 0


# ═══════════════════════════════════════════════════════════════════════════
# TEST 6: Shadow Mode Zero-Impact
# ═══════════════════════════════════════════════════════════════════════════

class TestShadowMode:
    """Shadow mode must NEVER alter the output returned to the user."""

    def test_shadow_returns_legacy_result(self):
        legacy = MagicMock()
        legacy.complete.return_value = "legacy_answer"

        intelligent = MagicMock()
        intelligent.execute.return_value = [
            ExecutionResult(subtask_id="s1", output="different_answer", success=True,
                          model_used="local", tier_used=ModelTier.LOCAL_NANO),
        ]

        config = RouterConfig(enable_intelligent_router=True, shadow_mode=True)
        entry = RouterEntrypoint(
            legacy_router=legacy,
            intelligent_router=intelligent,
            config=config,
        )

        result = entry.route("task", messages=[{"role": "user", "content": "test"}])
        assert result == "legacy_answer"  # MUST be legacy, not intelligent

    def test_shadow_collects_diffs(self):
        legacy = MagicMock()
        legacy.complete.return_value = "legacy"
        intelligent = MagicMock()
        intelligent.execute.return_value = [
            ExecutionResult(subtask_id="s1", output="x", success=True,
                          model_used="local", tier_used=ModelTier.LOCAL_SMALL),
        ]

        config = RouterConfig(enable_intelligent_router=True, shadow_mode=True, log_diffs=True)
        entry = RouterEntrypoint(legacy_router=legacy, intelligent_router=intelligent, config=config)

        entry.route("task1", messages=[{"role": "user", "content": "test"}])
        entry.route("task2", messages=[{"role": "user", "content": "test2"}])

        diffs = entry.get_shadow_diffs()
        assert len(diffs) == 2

    def test_shadow_survives_intelligent_crash(self):
        legacy = MagicMock()
        legacy.complete.return_value = "safe_result"
        broken = MagicMock()
        broken.execute.side_effect = Exception("crash")

        config = RouterConfig(enable_intelligent_router=True, shadow_mode=True)
        entry = RouterEntrypoint(legacy_router=legacy, intelligent_router=broken, config=config)

        result = entry.route("task", messages=[{"role": "user", "content": "test"}])
        assert result == "safe_result"  # Legacy must still work


# ═══════════════════════════════════════════════════════════════════════════
# TEST 7: Metrics No Double-Counting
# ═══════════════════════════════════════════════════════════════════════════

class TestMetricsIntegrity:
    """Metrics must not inflate costs or counts."""

    def test_single_event_counted_once(self):
        metrics = RouterMetricsCollector()
        result = ExecutionResult(
            subtask_id="s1", output="x", success=True,
            model_used="local", tier_used=ModelTier.LOCAL_NANO,
            tokens_in=100, tokens_out=50,
        )
        metrics.record_event(result, domain="general")

        dash = metrics.get_dashboard_metrics()
        assert dash["tier_distribution"]["total_subtasks"] == 1
        assert dash["cost_overview"]["total_cost_usd"] == 0.0  # Local = free

    def test_premium_cost_tracked(self):
        metrics = RouterMetricsCollector()
        result = ExecutionResult(
            subtask_id="s1", output="x", success=True,
            model_used="premium", tier_used=ModelTier.PREMIUM_API,
            tokens_in=1000, tokens_out=500,
        )
        metrics.record_event(result)

        dash = metrics.get_dashboard_metrics()
        assert dash["cost_overview"]["total_cost_usd"] > 0
        assert dash["cost_overview"]["savings_usd"] == 0.0  # No savings if premium-only

    def test_escalation_counted_as_misclassification(self):
        metrics = RouterMetricsCollector()
        result = ExecutionResult(
            subtask_id="s1", output="x", success=True,
            model_used="cheap", tier_used=ModelTier.CHEAP_API,
            escalated_from=ModelTier.LOCAL_SMALL,
        )
        metrics.record_event(result)

        health = metrics.get_dashboard_metrics()["classifier_health"]
        assert health["total_misclassifications"] == 1
        assert health["misclassification_rate"] > 0

    def test_prometheus_export_valid(self):
        metrics = RouterMetricsCollector()
        result = ExecutionResult(
            subtask_id="s1", output="x", success=True,
            model_used="local", tier_used=ModelTier.LOCAL_NANO,
        )
        metrics.record_event(result)

        output = metrics.export_prometheus()
        assert "kernell_router_total_subtasks 1" in output
        assert "kernell_router_successful_subtasks 1" in output


# ═══════════════════════════════════════════════════════════════════════════
# TEST 8: Model Registry Hardware Mapping
# ═══════════════════════════════════════════════════════════════════════════

class TestModelRegistry:
    """Hardware detection must produce correct model sets."""

    def test_minimal_hardware(self):
        reg = ModelRegistry()
        config = reg.build_config(total_ram_gb=4, has_gpu=False, power_level="minimal")
        # 4GB * 0.25 = 1GB pool: classifier + nano models
        assert config.classifier_model is not None
        assert len(config.installable_models) >= 1

    def test_maximum_hardware(self):
        reg = ModelRegistry()
        config = reg.build_config(total_ram_gb=64, has_gpu=True, vram_gb=24)
        # Should install everything
        assert len(config.installable_models) == len(DEFAULT_CATALOG)

    def test_classifier_always_present(self):
        reg = ModelRegistry()
        config = reg.build_config(total_ram_gb=2, has_gpu=False, power_level="minimal")
        assert config.classifier_model.is_classifier is True

    def test_difficulty_to_model_mapping(self):
        reg = ModelRegistry()
        config = reg.build_config(total_ram_gb=32, has_gpu=False)

        trivial = reg.get_model_for_difficulty(config, DifficultyLevel.TRIVIAL)
        expert = reg.get_model_for_difficulty(config, DifficultyLevel.EXPERT)

        assert trivial is not None  # Nano can handle trivial
        assert expert is None       # Expert needs API


# ═══════════════════════════════════════════════════════════════════════════
# TEST 9: Cost Estimator
# ═══════════════════════════════════════════════════════════════════════════

class TestCostEstimator:
    """Cost estimates must be conservative and consistent."""

    def test_heuristic_estimate(self):
        estimator = CostEstimator()
        result = estimator.estimate("Short task")

        assert "estimated_cost_usd" in result
        assert "premium_only_cost_usd" in result
        assert result["estimated_savings_percent"] > 0
        assert result["confidence"] == "low"  # No decomposer = low confidence

    def test_local_cheaper_than_premium(self):
        estimator = CostEstimator()
        result = estimator.estimate("Any task description here")

        assert result["estimated_cost_usd"] <= result["premium_only_cost_usd"]


# ═══════════════════════════════════════════════════════════════════════════
# TEST 10: Budget Enforcement
# ═══════════════════════════════════════════════════════════════════════════

class TestBudgetEnforcement:
    """Router must respect budget limits."""

    def test_budget_blocks_premium(self):
        classifier = FakeDecomposerLLM()
        local = FakeLLM(response="ok")
        verifier = FakeVerifierLLM(valid=True, confidence=0.9)

        router = IntelligentRouter(
            classifier=classifier,
            local_models={"local_nano": local, "local_small": local, "local_medium": local},
            monthly_budget_usd=0.0,  # Zero budget
            verifier_model=verifier,
        )

        # Should still work (local is free)
        results = router.execute("Test")
        assert all(r.success for r in results)

    def test_budget_tracking(self):
        classifier = FakeDecomposerLLM()
        local = FakeLLM(response="ok")

        router = IntelligentRouter(
            classifier=classifier,
            local_models={"local_nano": local, "local_small": local, "local_medium": local},
            monthly_budget_usd=10.0,
        )
        router.execute("Test")
        # Local is free, budget should be untouched
        assert router.spent_usd == 0.0


# ═══════════════════════════════════════════════════════════════════════════
# TEST 11: Training Data Collector
# ═══════════════════════════════════════════════════════════════════════════

class TestTrainingCollector:
    """Training data must be clean and correctly formatted."""

    def test_escalation_recorded(self):
        collector = DecomposerTrainingCollector()
        subtask = SubTask(
            id="s1", description="test", difficulty=DifficultyLevel.EASY,
            domain=TaskDomain.CODE, target_tier=ModelTier.LOCAL_SMALL,
            confidence=0.7,
        )
        collector.record_escalation("original task", subtask, 3)
        
        data = collector.export_dataset()
        assert len(data) == 1
        assert data[0]["signal"] == "escalation"
        assert data[0]["corrected_difficulty"] == 3

    def test_overestimation_only_for_cheap_outputs(self):
        collector = DecomposerTrainingCollector()
        subtask = SubTask(
            id="s1", description="test", difficulty=DifficultyLevel.HARD,
            domain=TaskDomain.GENERAL, target_tier=ModelTier.PREMIUM_API,
            confidence=0.8,
        )
        # 500 tokens = not trivial, should NOT be recorded as overestimation
        collector.record_overestimation("task", subtask, 500)
        assert collector.size == 0

        # 100 tokens = trivial, SHOULD be recorded
        collector.record_overestimation("task", subtask, 100)
        assert collector.size == 1


# ═══════════════════════════════════════════════════════════════════════════
# TEST 12: Summarizer
# ═══════════════════════════════════════════════════════════════════════════

class TestSummarizer:
    """Summarizer must compress without losing critical info."""

    def test_short_context_kept_raw(self):
        model = FakeLLM(response="compressed")
        summarizer = RollingSummarizer(model=model, max_raw_chars=5000)
        
        summarizer.add_step_output("s1", "short output")
        context = summarizer.get_context_for_step("next step")
        
        assert "short output" in context
        assert model.call_count == 0  # No compression needed

    def test_long_context_compressed(self):
        model = FakeLLM(response="compressed summary")
        summarizer = RollingSummarizer(model=model, max_raw_chars=100)
        
        summarizer.add_step_output("s1", "x" * 200)
        
        assert model.call_count == 1  # Should have triggered compression
