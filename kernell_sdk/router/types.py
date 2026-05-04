"""
Kernell OS SDK — Intelligent Router Data Contracts
════════════════════════════════════════════════════
Shared types for the 3-layer token economy engine.
Every component in the router pipeline speaks this language.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum, IntEnum
from typing import List, Optional


class DifficultyLevel(IntEnum):
    """Task difficulty scale used by the Decomposer-Classifier."""
    TRIVIAL = 1     # Retrieval, formatting, classification
    EASY = 2        # Summarization, transformations, Q&A on given context
    MEDIUM = 3      # Multi-step reasoning, functional code generation
    HARD = 4        # Complex analysis, multi-source synthesis, advanced code
    EXPERT = 5      # Deep abstract reasoning, genuine creativity, what locals fail


class ModelTier(str, Enum):
    """Execution tier for a subtask."""
    LOCAL_NANO = "local_nano"       # 0.5–1B params  (Qwen3-0.6B, Gemma3-1B)
    LOCAL_SMALL = "local_small"     # 1–2B params    (Qwen3-1.7B, Phi-4-mini)
    LOCAL_MEDIUM = "local_medium"   # 3–5B params    (Qwen3-4B, Gemma3-4B)
    LOCAL_LARGE = "local_large"     # 7–14B params   (Qwen3-8B, Gemma3-12B)
    CHEAP_API = "cheap_api"         # DeepSeek, Groq, Gemini Flash
    PREMIUM_API = "premium_api"     # Claude Opus, GPT-5, Gemini Pro


class TaskDomain(str, Enum):
    """Domain classification for routing specialization."""
    CODE = "code"
    REASONING = "reasoning"
    DATA = "data"
    CREATIVE = "creative"
    GENERAL = "general"
    MATH = "math"


@dataclass
class SubTask:
    """An atomic unit of work produced by the Decomposer."""
    id: str
    description: str
    difficulty: DifficultyLevel
    domain: TaskDomain
    target_tier: ModelTier
    confidence: float                    # 0.0–1.0 classifier confidence
    escalate_if_fail: bool = True
    parallel_ok: bool = False
    depends_on: List[str] = field(default_factory=list)
    context_needed: Optional[str] = None # What prior context this step needs


@dataclass
class ExecutionResult:
    """Result of executing a single subtask."""
    subtask_id: str
    output: str
    success: bool
    model_used: str
    tier_used: ModelTier
    confidence: float = 0.0
    tokens_in: int = 0
    tokens_out: int = 0
    cost_usd: float = 0.0
    latency_ms: float = 0.0
    was_cached: bool = False
    escalated_from: Optional[ModelTier] = None


@dataclass
class RouterStats:
    """Aggregated statistics for the token economy."""
    total_subtasks: int = 0
    cache_hits: int = 0
    local_executions: int = 0
    cheap_api_executions: int = 0
    premium_api_executions: int = 0
    escalations: int = 0
    total_tokens_in: int = 0
    total_tokens_out: int = 0
    total_cost_usd: float = 0.0
    tokens_saved_by_cache: int = 0
    tokens_saved_by_compression: int = 0

    @property
    def local_rate(self) -> float:
        """Percentage of tasks resolved locally."""
        if self.total_subtasks == 0:
            return 0.0
        return (self.local_executions / self.total_subtasks) * 100

    @property
    def premium_rate(self) -> float:
        """Percentage of tasks that required premium API."""
        if self.total_subtasks == 0:
            return 0.0
        return (self.premium_api_executions / self.total_subtasks) * 100


class PolicyRoute(str, Enum):
    """Route decision produced by the Policy Model."""
    LOCAL = "local"        # Free, Ollama/local LLM
    CHEAP = "cheap"        # Groq/DeepSeek/Flash ($0.001-0.01)
    PREMIUM = "premium"    # Claude/GPT ($0.05-2.00)
    HYBRID = "hybrid"      # Decompose into sub-routes


class RiskLevel(str, Enum):
    """Risk assessment for a task."""
    LOW = "low"        # Tolerates approximation (autocompletion, formatting)
    MEDIUM = "medium"  # Requires coherence (standard code, Q&A)
    HIGH = "high"      # Requires precision (payments, auth, security audit)


@dataclass
class PolicyDecision:
    """
    Canonical policy output contract.

    This is what the Policy Model predicts — NOT difficulty,
    but the economically optimal execution decision.

    Used for:
      1. Runtime routing (IntelligentRouter consumes this)
      2. Telemetry persistence (ground truth for training)
      3. Fine-tuning target label (what the model learns to predict)
    """
    route: PolicyRoute
    confidence: float                       # 0.0-1.0, model certainty
    needs_decomposition: bool = False       # True if task has 3+ independent subtasks
    risk: RiskLevel = RiskLevel.LOW
    expected_cost_usd: float = 0.0          # Pre-execution cost estimate
    expected_latency_s: float = 0.0         # Pre-execution latency estimate
    max_budget_usd: float = 0.0             # Max acceptable spend
    policy_version: str = "v0"              # Model version for A/B tracking

    def to_dict(self) -> dict:
        return {
            "route": self.route.value,
            "confidence": self.confidence,
            "needs_decomposition": self.needs_decomposition,
            "risk": self.risk.value,
            "expected_cost_usd": self.expected_cost_usd,
            "expected_latency_s": self.expected_latency_s,
            "max_budget_usd": self.max_budget_usd,
            "policy_version": self.policy_version,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "PolicyDecision":
        return cls(
            route=PolicyRoute(d.get("route", "cheap")),
            confidence=float(d.get("confidence", 0.5)),
            needs_decomposition=bool(d.get("needs_decomposition", False)),
            risk=RiskLevel(d.get("risk", "low")),
            expected_cost_usd=float(d.get("expected_cost_usd", 0.0)),
            expected_latency_s=float(d.get("expected_latency_s", 0.0)),
            max_budget_usd=float(d.get("max_budget_usd", 0.0)),
            policy_version=str(d.get("policy_version", "v0")),
        )
