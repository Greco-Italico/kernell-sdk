"""
Kernell OS SDK — Intelligent Router Package
═════════════════════════════════════════════
3-Layer Token Economy Engine for maximum cost reduction
in agentic AI workloads.

Architecture:
  Layer 0: Hardware Profiler (install-time)
  Layer 1: Fine-tuned Classifier-Decomposer (indispensable)
  Layer 2: Local inference (Nano → Small → Medium → Large)
  Layer 2.5: Cheap API inference (DeepSeek, Groq, Flash)
  Layer 3: Premium API (Claude, GPT-5, Gemini Pro — last resort)

Anti-waste components:
  - SemanticCache: Skip repeated work entirely
  - RollingSummarizer: Compress context between steps (kills O(n²))
  - SelfVerifier: Validate before escalating (prevents premature spend)
  - DecomposerTrainingCollector: Auto-calibrate with implicit feedback

Observability:
  - RouterMetricsCollector: Full Prometheus-compatible metrics
  - CostEstimator: Pre-execution cost simulation
"""
from .types import (
    SubTask,
    ExecutionResult,
    RouterStats,
    DifficultyLevel,
    ModelTier,
    TaskDomain,
    PolicyDecision,
    PolicyRoute,
    RiskLevel,
)
from .model_registry import (
    ModelRegistry,
    LocalModelSpec,
    HardwareTierConfig,
    DEFAULT_CATALOG,
)
from .decomposer import (
    TaskDecomposer,
    DecomposerTrainingCollector,
    DECOMPOSER_SYSTEM_PROMPT,
)
from .summarizer import RollingSummarizer
from .verifier import SelfVerifier, VerificationResult
from .intelligent_router import IntelligentRouter
from .metrics import RouterMetricsCollector, API_COST_TABLE
from .estimator import CostEstimator
from .telemetry_collector import TelemetryCollector, TelemetryConfig, TelemetryEvent
from .classifier_pro import ClassifierProClient, ClassifierProConfig, ProClassification
from .offline_labeler import OfflineLabeler, LabelConfig, LabeledExample
from .policy_lite import PolicyLiteClient, PolicyLiteConfig
from .semantic_cache import SemanticCache, CacheConfig, CacheStats

__all__ = [
    # Core types
    "SubTask",
    "ExecutionResult",
    "RouterStats",
    "DifficultyLevel",
    "ModelTier",
    "TaskDomain",
    # Model registry
    "ModelRegistry",
    "LocalModelSpec",
    "HardwareTierConfig",
    "DEFAULT_CATALOG",
    # Decomposer
    "TaskDecomposer",
    "DecomposerTrainingCollector",
    "DECOMPOSER_SYSTEM_PROMPT",
    # Anti-waste
    "RollingSummarizer",
    "SelfVerifier",
    "VerificationResult",
    # Main engine
    "IntelligentRouter",
    # Observability
    "RouterMetricsCollector",
    "API_COST_TABLE",
    "CostEstimator",
    # Data Flywheel
    "TelemetryCollector",
    "TelemetryConfig",
    "TelemetryEvent",
    "ClassifierProClient",
    "ClassifierProConfig",
    "ProClassification",
    # Policy Model
    "PolicyDecision",
    "PolicyRoute",
    "RiskLevel",
    "PolicyLiteClient",
    "PolicyLiteConfig",
    # Offline Labeler
    "OfflineLabeler",
    "LabelConfig",
    "LabeledExample",
    # Semantic Cache
    "SemanticCache",
    "CacheConfig",
    "CacheStats",
]
