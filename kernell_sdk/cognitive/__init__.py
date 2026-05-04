"""
Kernell OS SDK — Cognitive Layer
═════════════════════════════════
The brain of the agentic operating system.

This module provides:
  - Task: atomic unit of work
  - CognitiveAgent: role-based agents with KERN budgets
  - CognitiveRouter: cost-aware model selection policy engine
  - ExecutionGraph: DAG orchestrator with async execution
  - IntentFirewall: immune system for agent actions
"""

from .task import Task, TaskType, Complexity, TaskStatus
from .agent_role import CognitiveAgent, AgentRole, ROLE_CAPABILITIES
from .cognitive_router import CognitiveRouter, ModelConfig, RouterDecision
from .execution_graph import ExecutionGraph, GraphResult
from .intent_firewall import (
    IntentFirewall,
    AgentIntent,
    FirewallDecision,
    ActionType,
    RiskLevel,
    FirewallVerdict,
)
from .tools import (
    AgentTool,
    BashExecutionTool,
    FileEditorTool,
    SemanticSearchTool,
    ToolRegistry,
)
from .agentic_loop import AgenticLoop
from .config_loader import load_config, KernellEnvironment
from .semantic_cache import SemanticCache, CacheEntry

__all__ = [
    # Task
    "Task", "TaskType", "Complexity", "TaskStatus",
    # Agents
    "CognitiveAgent", "AgentRole", "ROLE_CAPABILITIES",
    # Router & Cache
    "CognitiveRouter", "ModelConfig", "RouterDecision",
    "SemanticCache", "CacheEntry",
    # Execution & Config
    "ExecutionGraph", "GraphResult", "AgenticLoop",
    "load_config", "KernellEnvironment",
    # Tools
    "AgentTool", "BashExecutionTool", "FileEditorTool", "SemanticSearchTool", "ToolRegistry",
    # Firewall
    "IntentFirewall", "AgentIntent", "FirewallDecision",
    "ActionType", "RiskLevel", "FirewallVerdict",
]
