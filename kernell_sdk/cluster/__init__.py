"""
Kernell OS SDK — Cluster Module
═══════════════════════════════
Enables P2P orchestration of agents across multiple machines.
Agents can discover each other, share context via distributed memory,
and exchange tasks for KERN tokens via the Bounty Board.

Exports:
    ClusterNode: The main entry point for joining an agent to the swarm.
    ClusterDiscovery: Redis-backed P2P node discovery.
    BountyBoard: Distributed task market for agents.
"""
from .discovery import ClusterDiscovery, ClusterNode
from .bounty import BountyBoard, Bounty
from .sync import MemorySync

__all__ = [
    "ClusterNode",
    "ClusterDiscovery",
    "BountyBoard",
    "Bounty",
    "MemorySync",
]
