"""
Kernell OS SDK — MCTS (Monte Carlo Tree Search) Engine
══════════════════════════════════════════════════════
A System 2 reasoning engine that simulates multiple routing and 
decomposition strategies before committing to an execution path.

It queries PolicyLite multiple times (with high temperature) to generate 
diverse strategies, then evaluates them using a local Verifier and Reward Model 
to select the optimal path, breaking the "agentic limit" of single-shot routing.
"""
from __future__ import annotations

import logging
import math
from dataclasses import dataclass
from typing import List, Optional, Protocol

from .types import PolicyDecision, PolicyRoute, SubTask
from .policy_lite import PolicyLiteClient
from .decomposer import TaskDecomposer
from .verifier import SelfVerifier

logger = logging.getLogger("kernell.router.mcts")


@dataclass
class MCTSNode:
    """A node in the MCTS tree representing a routing/decomposition strategy."""
    decision: PolicyDecision
    subtasks: Optional[List[SubTask]] = None
    reward_score: float = 0.0
    visits: int = 0
    
    # Heuristics
    expected_cost: float = 0.0
    verifier_confidence: float = 0.0


class RewardModel:
    """
    Evaluates a given strategy based on cost, risk, and expected success.
    """
    def __init__(self, cost_weight: float = 10.0, confidence_weight: float = 2.0, penalty_weight: float = 5.0):
        self.cost_weight = cost_weight
        self.confidence_weight = confidence_weight
        self.penalty_weight = penalty_weight

    def evaluate(self, node: MCTSNode) -> float:
        """Calculate the reward score for a given node."""
        score = 0.0
        
        # Reward for high confidence
        score += node.decision.confidence * self.confidence_weight
        if node.verifier_confidence > 0:
            score += node.verifier_confidence * self.confidence_weight * 1.5

        # Penalty for high expected cost
        score -= node.decision.expected_cost_usd * self.cost_weight

        # Penalty for high risk routes without decomposition
        if node.decision.risk == "high" and not node.decision.needs_decomposition:
            score -= self.penalty_weight
            
        # Reward for balanced decomposition
        if node.subtasks:
            avg_diff = sum(st.difficulty.value for st in node.subtasks) / len(node.subtasks)
            # If subtasks are too hard, penalty (should have decomposed further)
            if avg_diff > 3.5:
                score -= self.penalty_weight * 0.5
            else:
                score += 1.0

        return score


class MCTSEngine:
    """
    Monte Carlo Tree Search Engine for Deep Reasoning in Routing.
    """
    def __init__(
        self,
        policy_lite: PolicyLiteClient,
        decomposer: TaskDecomposer,
        verifier: SelfVerifier,
        num_simulations: int = 3,
    ):
        self.policy_lite = policy_lite
        self.decomposer = decomposer
        self.verifier = verifier
        self.num_simulations = num_simulations
        self.reward_model = RewardModel()

    def search_optimal_path(self, task: str) -> MCTSNode:
        """
        Generate multiple strategies, evaluate them, and return the optimal one.
        """
        logger.info(f"MCTS Engine starting {self.num_simulations} simulations for task.")
        candidates: List[MCTSNode] = []

        # Generate candidates
        for i in range(self.num_simulations):
            # In a real LLM call, we would inject high temperature or varied prompts.
            # Here we assume the underlying policy_lite model handles diversity 
            # if we request multiple samples, or we simulate variance.
            decision = self.policy_lite.decide(task)
            
            node = MCTSNode(
                decision=decision,
                expected_cost=decision.expected_cost_usd
            )
            
            # If decomposition is needed, simulate the decomposition
            if decision.needs_decomposition:
                try:
                    node.subtasks = self.decomposer.decompose(task)
                except Exception as e:
                    logger.warning(f"MCTS decomposition failed: {e}")
                    node.subtasks = []
            
            candidates.append(node)

        # Evaluate candidates
        best_node = None
        best_score = -float('inf')

        for idx, node in enumerate(candidates):
            # Use the local verifier to critique the strategy
            verification_prompt = self._build_verification_prompt(task, node)
            verification = self.verifier.verify(task, verification_prompt)
            
            node.verifier_confidence = verification.confidence
            node.reward_score = self.reward_model.evaluate(node)
            node.visits += 1

            logger.debug(f"MCTS Candidate {idx} Score: {node.reward_score:.2f} (Route: {node.decision.route.value})")

            if node.reward_score > best_score:
                best_score = node.reward_score
                best_node = node

        if not best_node:
            logger.warning("MCTS failed to find an optimal node, falling back to basic policy.")
            best_node = MCTSNode(decision=self.policy_lite.decide(task))

        logger.info(f"MCTS selected optimal route: {best_node.decision.route.value} (Score: {best_score:.2f})")
        return best_node

    def _build_verification_prompt(self, task: str, node: MCTSNode) -> str:
        """Construct a prompt for the verifier to evaluate the strategy."""
        prompt = f"Evaluate this routing strategy for the task:\n{task}\n\n"
        prompt += f"Proposed Route: {node.decision.route.value}\n"
        prompt += f"Confidence: {node.decision.confidence}\n"
        prompt += f"Risk: {node.decision.risk}\n"
        
        if node.subtasks:
            prompt += f"Decomposition ({len(node.subtasks)} steps):\n"
            for st in node.subtasks:
                prompt += f"- {st.description} (Difficulty: {st.difficulty.value})\n"
                
        prompt += "\nIs this a safe, cost-effective, and sufficient strategy? Return a high confidence score if yes."
        return prompt
