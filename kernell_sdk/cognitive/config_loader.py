"""
Kernell OS SDK — Configuration Loader
══════════════════════════════════════
Loads `kernell.yaml` and instantiates the entire Cognitive Layer:
  - Models → ModelConfig registry
  - Agents → CognitiveAgent instances
  - Router → CognitiveRouter with strategy
  - Firewall → IntentFirewall with policies

Usage:
    from kernell_sdk.cognitive.config_loader import load_config
    env = load_config("kernell.yaml")
    # env.router, env.agents, env.firewall, env.graph — all ready
"""
from __future__ import annotations

import os
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Set

try:
    import yaml
except ImportError:
    yaml = None

from .task import Task
from .agent_role import CognitiveAgent, AgentRole
from .cognitive_router import CognitiveRouter, ModelConfig, SystemPolicy
from .semantic_memory_graph import SemanticMemoryGraph
from .execution_graph import ExecutionGraph
from .intent_firewall import IntentFirewall, ActionType

logger = logging.getLogger("kernell.cognitive.config")


@dataclass
class KernellEnvironment:
    """Fully initialized Kernell OS runtime environment."""
    project_name: str
    workspace: str
    models: Dict[str, ModelConfig]
    agents: Dict[str, CognitiveAgent]
    router: CognitiveRouter
    firewall: IntentFirewall
    graph: ExecutionGraph
    economy_config: dict = field(default_factory=dict)


def _resolve_env_vars(value: str) -> str:
    """Replace ${VAR} with environment variable values."""
    if not isinstance(value, str):
        return value
    if value.startswith("${") and value.endswith("}"):
        env_key = value[2:-1]
        return os.getenv(env_key, "")
    return value


def _parse_models(raw: dict) -> Dict[str, ModelConfig]:
    """Parse models section from YAML."""
    models = {}
    for model_id, cfg in (raw or {}).items():
        models[model_id] = ModelConfig(
            model_id=model_id,
            provider=cfg.get("provider", "ollama"),
            model_name=cfg.get("model", model_id),
            cost_per_1k_input=cfg.get("cost_per_1k_tokens", 0.0),
            cost_per_1k_output=cfg.get("cost_per_1k_tokens", 0.0) * 1.5,
            max_context=cfg.get("max_context", 32768),
            tags=cfg.get("tags", []),
            is_local=cfg.get("provider", "ollama") == "ollama",
            precision_score=cfg.get("precision_score", 0.8),
            reasoning_score=cfg.get("reasoning_score", 0.5),
            code_score=cfg.get("code_score", 0.5),
        )
        # Resolve API keys from env
        if "api_key" in cfg:
            resolved = _resolve_env_vars(cfg["api_key"])
            if resolved:
                models[model_id].api_key = resolved
    return models


def _parse_agents(raw: dict, models: Dict[str, ModelConfig]) -> Dict[str, CognitiveAgent]:
    """Parse agents section from YAML."""
    agents = {}
    role_map = {r.value: r for r in AgentRole}

    for agent_name, cfg in (raw or {}).items():
        role_str = cfg.get("role", "coder")
        role = role_map.get(role_str, AgentRole.CODER)
        model_id = cfg.get("model", "")

        agents[agent_name] = CognitiveAgent(
            name=agent_name,
            role=role,
            model_id=model_id,
            budget_kern=cfg.get("budget_kern", 10.0),
        )
    return agents


def _parse_firewall(raw: dict) -> IntentFirewall:
    """Parse firewall section from YAML."""
    action_map = {a.value: a for a in ActionType}

    auto = set()
    for name in (raw or {}).get("auto_approve", ["read_file", "list_dir", "run_tests"]):
        if name in action_map:
            auto.add(action_map[name])

    manual = set()
    for name in (raw or {}).get("manual_approve", ["write_file", "execute_command", "network_request"]):
        if name in action_map:
            manual.add(action_map[name])

    return IntentFirewall(auto_approve=auto, manual_approve=manual)


def load_config(config_path: str = "kernell.yaml") -> KernellEnvironment:
    """
    Load a kernell.yaml and return a fully initialized environment.

    If yaml is not installed, falls back to a minimal default config.
    """
    path = Path(config_path)

    if yaml and path.exists():
        with open(path) as f:
            raw = yaml.safe_load(f) or {}
        logger.info(f"Loaded config from {path}")
    else:
        if not path.exists():
            logger.warning(f"Config {path} not found, using defaults")
        elif not yaml:
            logger.warning("PyYAML not installed, using defaults")
        raw = _default_config()

    # Parse sections
    project = raw.get("project", {})
    models = _parse_models(raw.get("models", {}))
    agents = _parse_agents(raw.get("agents", {}), models)
    firewall = _parse_firewall(raw.get("firewall", {}))

    # Router Policy
    router_cfg = raw.get("router", {})
    policy = SystemPolicy(
        max_cost_usd=router_cfg.get("max_cost_per_task", 0.05),
        prefer_local_models=router_cfg.get("prefer_local", True),
        allow_high_risk_models=router_cfg.get("allow_high_risk", False)
    )
    
    router = CognitiveRouter(
        models=models,
        strategy=router_cfg.get("strategy", "policy-based"),
        policy=policy
    )

    # Memory Graph
    memory_graph = SemanticMemoryGraph()
    # In a real setup, we would load existing nodes/edges from disk here.

    # Graph
    graph = ExecutionGraph(
        router=router,
        agents=agents,
        memory_graph=memory_graph,
        on_task_event=callbacks.get("on_task_event"),
        on_escrow_event=callbacks.get("on_escrow_event"),
        on_firewall_event=callbacks.get("on_firewall_event"),
        on_router_event=callbacks.get("on_router_event"),
    )

    return KernellEnvironment(
        project_name=project.get("name", "kernell-project"),
        workspace=project.get("workspace", "./workspace"),
        models=models,
        agents=agents,
        router=router,
        firewall=firewall,
        graph=graph,
        economy_config=raw.get("economy", {}),
    )


def _default_config() -> dict:
    """Minimal default configuration when no YAML is present."""
    return {
        "project": {"name": "default", "workspace": "."},
        "models": {
            "local": {
                "provider": "ollama",
                "model": "llama3:8b",
                "cost_per_1k_tokens": 0.0,
                "max_context": 32768,
                "tags": ["fast", "local", "code", "reasoning"],
            }
        },
        "agents": {
            "planner": {"role": "planner", "model": "local", "budget_kern": 5.0},
            "coder": {"role": "coder", "model": "local", "budget_kern": 15.0},
            "tester": {"role": "verifier", "model": "local", "budget_kern": 10.0},
        },
        "router": {"strategy": "cost-aware", "cascade": True},
        "firewall": {
            "auto_approve": ["read_file", "list_dir", "run_tests"],
            "manual_approve": ["write_file", "execute_command"],
        },
        "economy": {"escrow_fee_percent": 1.0},
    }
