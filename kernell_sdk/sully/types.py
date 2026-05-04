"""
Kernell OS SDK — Sully Types (Compute Allocation Contracts)
═══════════════════════════════════════════════════════════
Core data contracts for the Sully Compute Allocation Engine.
These types define the language Sully speaks: tasks in, routing decisions out.
"""

from dataclasses import dataclass, field
from enum import Enum
from typing import Dict, Any, List, Optional


class Tier(str, Enum):
    """Execution cost tiers — LOCAL is always preferred, PREMIUM is last resort."""
    LOCAL = "LOCAL"
    ECONOMIC = "ECONOMIC"
    PREMIUM = "PREMIUM"


@dataclass
class TaskFeatures:
    """
    Structured feature vector describing a task.
    This is what Sully uses to decide routing — NOT the task content itself.
    """
    task_type: str                  # e.g. "web_scraping", "code_gen", "data_extraction"
    ui_complexity: float = 0.5      # 0.0 (trivial) → 1.0 (extremely complex UI)
    requires_auth: bool = False
    dom_available: bool = True
    estimated_tokens: int = 1000
    estimated_output_tokens: int = 500  # [AUDIT FIX] output cost awareness
    history_failures: int = 0       # incremented on escalation
    parallelizable: bool = False    # hint for swarm decomposition
    # [AUDIT FIX] output awareness — Sully must know when failure is unacceptable
    expected_output_type: str = "text"  # "text", "json", "code", "critical_action"
    quality_requirement: float = 0.7   # 0.0 (best effort) → 1.0 (must not fail)
    

@dataclass
class ModelMarketInfo:
    """
    Real-time market data for a single model endpoint.
    Fetched from providers, cached with TTL. Never hardcoded into Sully's weights.
    """
    model_id: str
    provider: str                   # "groq", "openrouter", "anthropic", "local"
    
    # 💰 Costs (per 1K tokens)
    input_cost_per_1k: float = 0.0
    output_cost_per_1k: float = 0.0
    
    # ⚡ Performance
    avg_latency_ms: float = 500.0
    p95_latency_ms: float = 2000.0
    
    # 🧠 Capabilities
    context_limit: int = 8192
    max_output_tokens: int = 4096
    supports_vision: bool = False
    supports_reasoning: bool = False
    
    # 🚦 Operational state
    rate_limited: bool = False
    availability: float = 1.0       # 0.0–1.0 recent uptime
    
    # 🧪 Quality (populated from telemetry over time)
    quality_score: float = 0.7      # 0.0–1.0 task success rate on this model
    reliability_score: float = 1.0  # 0.0–1.0 drops when rate limited or failing


@dataclass
class SullyDecision:
    """The output of Sully's compute allocation decision."""
    tier: Tier
    model_id: str                   # specific model chosen (e.g. "groq/llama3-70b")
    confidence: float               # 0.0–1.0 how certain Sully is
    expected_cost: float            # estimated $ for this execution
    expected_latency: float         # estimated ms
    strategy_hint: str = "auto"     # "dom_first", "vision_first", "hybrid", "auto"
    reasoning: str = ""             # why this decision was made (for telemetry)
    shadow_decision: Optional[Dict] = None  # Telemetry payload for shadow model vs prod model eval


@dataclass
class ExecutionResult:
    """Result of executing a task through any tier."""
    success: bool
    output: Any = None
    latency: float = 0.0           # actual ms
    cost: float = 0.0              # actual $
    tokens_used: int = 0
    error_type: Optional[str] = None
    model_used: str = ""


@dataclass
class FinalOutcome:
    """Aggregated outcome after potential escalations."""
    success: bool
    total_cost: float
    total_latency: float
    final_tier: Tier
    steps: int                      # number of tier attempts
    score: float = 0.0              # computed reward signal for training
    output: Any = None              # the actual result content
