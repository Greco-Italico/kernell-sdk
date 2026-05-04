"""Kernell OS SDK — Open-Source Agent Framework"""

from importlib.metadata import version as _pkg_version, PackageNotFoundError
try:
    __version__ = _pkg_version("kernell-os")
except PackageNotFoundError:
    __version__ = "dev"

__author__ = "Kernell OS"

from kernell_sdk.agent import Agent
from kernell_sdk.memory import Memory
from kernell_sdk.cluster import ClusterNode, ClusterDiscovery, BountyBoard, Bounty, MemorySync
from kernell_sdk.wallet import Wallet
from kernell_sdk.config import KernellConfig
from kernell_sdk.sandbox import ResourceLimits, AgentPermissions
from kernell_sdk.identity import AgentPassport, SecurityError
from kernell_sdk.gui import AgentGUI
from kernell_sdk.dashboard import CommandCenter
from kernell_sdk.telemetry import HardwareFingerprint
from kernell_sdk.budget import TokenBudget
from kernell_sdk.resilience import CircuitBreaker, CircuitOpenError
from kernell_sdk.tracing import TraceContext, get_current_trace_id
from kernell_sdk.health import SLOMonitor, HealthStatus
from kernell_sdk.skill_loader import SkillLoader, SkillConfig
from kernell_sdk.token_estimator import estimate_tokens
from kernell_sdk.persister import ToolResultPersister
from kernell_sdk.llm import (
    BaseLLMProvider, OllamaProvider, AnthropicProvider,
    OpenAIProvider, LLMRouter, ComplexityLevel, LLMMessage
)
from kernell_sdk.delegation import SubAgentManager, TaskQueue
from kernell_sdk.learning.loop import LearningLoop, TaskTrace

__all__ = [
    "Agent", "Memory", "ClusterNode", "ClusterDiscovery", "BountyBoard", "Bounty", "MemorySync",
    "Wallet", "KernellConfig",
    "ResourceLimits", "AgentPermissions", "AgentPassport",
    "AgentGUI", "CommandCenter",
    "HardwareFingerprint", "SecurityError",
    "TokenBudget", "CircuitBreaker", "CircuitOpenError",
    "TraceContext", "get_current_trace_id",
    "SLOMonitor", "HealthStatus",
    "SkillLoader", "SkillConfig",
    "estimate_tokens", "ToolResultPersister",
    "BaseLLMProvider", "OllamaProvider", "AnthropicProvider",
    "OpenAIProvider", "LLMRouter", "ComplexityLevel", "LLMMessage",
    "SubAgentManager", "TaskQueue",
    "LearningLoop", "TaskTrace",
    "__version__",
]
