"""
Kernell OS SDK — Delegation Module
══════════════════════════════════
Handles the creation, management, and result synthesis of sub-agents.
Enables the "Hybrid Swarm" architecture where a cloud agent delegates
heavy processing tasks to local open-source models (Gemma 4/Llama 3).

Exports:
    SubAgentManager: Manages the lifecycle of worker sub-agents.
    TaskQueue: Thread-safe queue for distributing tasks.
"""
from .manager import SubAgentManager
from .task_queue import TaskQueue
from .merger import ResultMerger

__all__ = [
    "SubAgentManager",
    "TaskQueue",
    "ResultMerger",
]
