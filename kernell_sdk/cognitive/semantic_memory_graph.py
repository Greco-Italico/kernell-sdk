"""
Kernell OS SDK — Semantic Memory Graph (Path Intelligence)
══════════════════════════════════════════════════════════
Transitions the system from stateless prompt-generation to
cumulative intelligence composition.

It stores reusable components (nodes) and their relationships (edges).
Crucially, it traverses these edges to find complete structural *paths*
(architectures) rather than just isolated snippets, passing confidence
and topology metrics to the Cognitive Router v2.
"""
from __future__ import annotations

import json
import time
import logging
from dataclasses import dataclass, field
from typing import List, Dict, Optional, Any, Set, Tuple

logger = logging.getLogger("kernell.cognitive.memory_graph")


@dataclass
class MemoryNode:
    """A discrete, reusable semantic component."""
    id: str
    type: str               # "function", "api_endpoint", "auth_flow", "db_schema", "glue"
    description: str        # Human-readable intent of this node
    content: str            # The actual code or configuration payload
    embedding: List[float] = field(default_factory=list)
    success_rate: float = 1.0                           # Adjusts over time based on execution success
    usage_count: int = 0
    created_at: float = field(default_factory=time.time)

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "type": self.type,
            "description": self.description,
            "content": self.content,
            "success_rate": round(self.success_rate, 4),
            "usage_count": self.usage_count
        }


@dataclass
class MemoryEdge:
    """Relationship between two MemoryNodes."""
    from_node: str
    to_node: str
    relation: str           # "depends_on", "extends", "validates", "implements"
    success_rate: float = 1.0 # Routes can fail even if nodes are good
    usage_count: int = 0


@dataclass
class GraphQueryResult:
    """The result of querying the memory graph for a task."""
    nodes: List[MemoryNode]
    edges_traversed: List[MemoryEdge]
    coverage_score: float   # 0.0 to 1.0 (How much of the problem is solved by these nodes)
    confidence_score: float # 0.0 to 1.0 (How structurally reliable this path has proven to be)
    relevance_score: float  # 0.0 to 1.0 (How contextually appropriate this path is for the current task)
    missing_capabilities: List[str] # What the LLM needs to generate (the glue)

    @property
    def novelty_score(self) -> float:
        return 1.0 - self.relevance_score

    def to_context_state(self, past_failures: int = 0) -> dict:
        """Converts graph result to the ContextState needed by Router v2."""
        from .cognitive_router import ContextState
        return ContextState(
            rag_match_score=self.coverage_score,
            graph_confidence=self.confidence_score,
            graph_relevance=self.relevance_score,
            past_failures=past_failures,
            similar_tasks_solved=len(self.nodes)
        )

    def build_llm_prompt(self, original_task: str) -> str:
        """Constructs the prompt forcing the LLM to act as an assembler."""
        prompt = f"Task: {original_task}\n\n"
        if not self.nodes:
            return prompt
            
        prompt += f"System Confidence in this Architecture: {self.confidence_score*100:.1f}%\n"
        prompt += "I have retrieved the following verified component architecture from the Memory Graph:\n"
        
        for i, node in enumerate(self.nodes, 1):
            prompt += f"\n--- Component {i}: {node.id} ({node.type}) [Success Rate: {node.success_rate*100:.0f}%] ---\n"
            prompt += f"Description: {node.description}\n"
            prompt += f"Code:\n```\n{node.content}\n```\n"
            
        if self.edges_traversed:
            prompt += "\n--- Topological Relationships ---\n"
            for e in self.edges_traversed:
                prompt += f"[{e.from_node}] --({e.relation})--> [{e.to_node}]\n"
                
        prompt += "\nINSTRUCTIONS:\n"
        prompt += "DO NOT regenerate the components above. They are already structurally verified.\n"
        if self.missing_capabilities:
            prompt += f"Your job is ONLY to write the glue code to assemble them, specifically fulfilling: {', '.join(self.missing_capabilities)}.\n"
        else:
            prompt += "Your job is ONLY to write the final execution script that wires these components together based on their relationships.\n"
            
        return prompt


class SemanticMemoryGraph:
    """
    Path-Intelligence graph for Kernell OS.
    Traverses architectural routes and scores them based on historical success.
    """

    def __init__(self, embedding_provider=None):
        self.nodes: Dict[str, MemoryNode] = {}
        # Dict[from_node, Dict[to_node, MemoryEdge]]
        self.adjacency: Dict[str, Dict[str, MemoryEdge]] = {}
        self.embedding_provider = embedding_provider

    def add_node(self, node: MemoryNode):
        self.nodes[node.id] = node
        if node.id not in self.adjacency:
            self.adjacency[node.id] = {}
        logger.debug(f"Graph: Added node {node.id} ({node.type})")

    def add_edge(self, edge: MemoryEdge):
        if edge.from_node in self.nodes and edge.to_node in self.nodes:
            if edge.from_node not in self.adjacency:
                self.adjacency[edge.from_node] = {}
            self.adjacency[edge.from_node][edge.to_node] = edge
            logger.debug(f"Graph: Added edge {edge.from_node} -[{edge.relation}]-> {edge.to_node}")

    def query(self, task_description: str, task_type: str = "") -> GraphQueryResult:
        """
        Retrieves a connected architectural path (subgraph) based on the task.
        """
        task_lower = task_description.lower()
        seed_nodes = []
        
        # 1. Semantic Match (Find seeds) - Heuristic for MVP
        if "auth" in task_lower or "jwt" in task_lower:
            seed_nodes.extend([n for n in self.nodes.values() if n.type == "auth_flow"])
        if "api" in task_lower or "server" in task_lower:
            seed_nodes.extend([n for n in self.nodes.values() if n.type == "api_endpoint" or n.type == "express_server"])
        if "db" in task_lower or "database" in task_lower or "user" in task_lower:
            seed_nodes.extend([n for n in self.nodes.values() if n.type == "db_schema"])
            
        # Eliminate seeds that are historically broken
        seed_nodes = [n for n in seed_nodes if n.success_rate > 0.4]

        if not seed_nodes:
            return GraphQueryResult([], [], 0.0, 0.0, ["Complete implementation from scratch"])

        # 2. Graph Traversal (BFS) to find dependencies and connected components
        subgraph_nodes: Set[str] = set()
        subgraph_edges: List[MemoryEdge] = []
        
        queue = [n.id for n in seed_nodes]
        visited = set(queue)
        
        while queue:
            current = queue.pop(0)
            subgraph_nodes.add(current)
            
            # Follow outbound edges (dependencies)
            if current in self.adjacency:
                for target_id, edge in self.adjacency[current].items():
                    # Prune toxic routes (bad architecture combinations)
                    if edge.success_rate < 0.4:
                        continue
                        
                    subgraph_edges.append(edge)
                    if target_id not in visited and target_id in self.nodes:
                        # Ensure target node itself isn't broken
                        if self.nodes[target_id].success_rate > 0.4:
                            visited.add(target_id)
                            queue.append(target_id)

        resolved_nodes = [self.nodes[nid] for nid in subgraph_nodes]

        # 3. Path Scoring (Structural Intelligence)
        avg_node_success = sum(n.success_rate for n in resolved_nodes) / max(1, len(resolved_nodes))
        avg_edge_success = sum(e.success_rate for e in subgraph_edges) / max(1, len(subgraph_edges)) if subgraph_edges else avg_node_success
        confidence = (avg_node_success * 0.6) + (avg_edge_success * 0.4)
        
        # 4. Contextual Relevance (Heuristic MVP)
        # In prod: semantic similarity between task embedding and node embeddings
        # Here: type match and keyword density
        task_keywords = set(task_lower.split())
        type_match_score = 0.5 if any(n.type in task_lower for n in resolved_nodes) else 0.0
        semantic_sim = min(1.0, len(task_keywords & set(" ".join([n.description.lower() for n in resolved_nodes]).split())) / max(1, len(task_keywords)))
        
        relevance = min(1.0, (semantic_sim * 0.7) + (type_match_score * 0.3))
        # Boost relevance if no nodes were retrieved (meaning relevance of empty path is 0)
        if not resolved_nodes:
            relevance = 0.0

        # 5. Coverage Math
        base_coverage = len(resolved_nodes) * 0.25
        coverage = min(0.95, base_coverage + (avg_node_success * 0.5))

        # 6. Missing Capabilities
        missing = []
        if coverage < 0.95:
            missing.append("Main server glue logic and cross-module wiring")

        # Increment usage
        for n in resolved_nodes:
            n.usage_count += 1
        for e in subgraph_edges:
            e.usage_count += 1

        return GraphQueryResult(
            nodes=resolved_nodes,
            edges_traversed=subgraph_edges,
            coverage_score=coverage,
            confidence_score=confidence,
            relevance_score=relevance,
            missing_capabilities=missing
        )

    def feedback(self, node_ids: List[str], edges_traversed: List[Tuple[str, str]], success: bool):
        """Reinforce or penalize nodes AND specific architectural paths."""
        delta = 0.05 if success else -0.20
        
        for nid in node_ids:
            if nid in self.nodes:
                self.nodes[nid].success_rate = max(0.0, min(1.0, self.nodes[nid].success_rate + delta))
                
        for from_nid, to_nid in edges_traversed:
            if from_nid in self.adjacency and to_nid in self.adjacency[from_nid]:
                edge = self.adjacency[from_nid][to_nid]
                edge.success_rate = max(0.0, min(1.0, edge.success_rate + delta))
                logger.info(f"Graph: Route [{from_nid}->{to_nid}] success updated to {edge.success_rate:.2f}")

    def save(self, filepath: str):
        edges_out = []
        for targets in self.adjacency.values():
            for e in targets.values():
                edges_out.append({"from": e.from_node, "to": e.to_node, "relation": e.relation, "success_rate": e.success_rate})
                
        data = {
            "nodes": [n.to_dict() for n in self.nodes.values()],
            "edges": edges_out
        }
        with open(filepath, "w") as f:
            json.dump(data, f, indent=2)
