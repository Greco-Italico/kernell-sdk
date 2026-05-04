import random
import math
from typing import List, Dict, Optional, Tuple, Any
from dataclasses import dataclass

@dataclass
class MarketNode:
    agent_id: str
    region: str
    provider: str
    reputation: float
    stake: float
    price_per_sec: float
    reliability: float
    
class MarketScheduler:
    """
    Advanced Anti-Collusion Scheduler for the Kernell OS Compute Marketplace.
    Assigns tasks based on optimal risk/cost balance and prevents Sybil/Collusion attacks.
    """
    def __init__(self):
        # Tracks how many times Node A and Node B verified each other
        # format: tuple(sorted(A, B)) -> int
        self._interaction_history: Dict[Tuple[str, str], int] = {}
        # Global market state to enforce invariants
        self.market_shares: Dict[str, float] = {}
        self.market_avg_price: float = 0.0
        
    def _compute_correlation_risk(self, node_a: MarketNode, node_b: MarketNode) -> float:
        """
        Calculates the probability that two nodes are colluding.
        Risk increases if they share region, provider, or have a high history of matching.
        """
        risk = 0.0
        if node_a.region == node_b.region:
            risk += 0.4
        if node_a.provider == node_b.provider:
            risk += 0.3
            
        pair = tuple(sorted([node_a.agent_id, node_b.agent_id]))
        interactions = self._interaction_history.get(pair, 0)
        
        # Non-linear correlation risk based on interaction frequency
        if interactions > 5:
            risk += min(0.3, (interactions - 5) * 0.05)
            
        return min(1.0, risk)

    def _score_node(self, node: MarketNode, task_value: float, weights: Dict[str, float]) -> float:
        """
        Calculates the node's market score ensuring protocol invariants.
        """
        # INVARIANT 4: Stake proportional (Diminishing returns via sqrt)
        # Prevents whales from buying the routing
        effective_stake = math.sqrt(node.stake)
        
        stake_penalty = 0.0
        if node.stake < (task_value * 2.0):
            stake_penalty = 100.0  # Massive penalty if under-staked
            
        # INVARIANT 5: Anti-price dumping
        price_dumping_penalty = 0.0
        if self.market_avg_price > 0 and node.price_per_sec < (self.market_avg_price * 0.5):
            price_dumping_penalty = 50.0  # Penalize predatory pricing

        # INVARIANT 1: Anti-centralization (Dominance penalty)
        node_share = self.market_shares.get(node.agent_id, 0.0)
        dominance_penalty = node_share * weights.get("dominance", 50.0)
            
        return (
            (node.reputation * weights.get("reputation", 1.2))
            + (effective_stake * weights.get("stake", 0.5))
            - (node.price_per_sec * weights.get("price", 2.0))
            + (node.reliability * weights.get("reliability", 1.5))
            - stake_penalty
            - price_dumping_penalty
            - dominance_penalty
        )

    def update_market_state(self, shares: Dict[str, float], avg_price: float):
        self.market_shares = shares
        self.market_avg_price = avg_price

    def schedule_task(
        self, 
        available_nodes: List[MarketNode], 
        task_value: float, 
        is_critical: bool,
        dynamic_weights: Dict[str, float] = None,
        redundancy_probability: float = 0.1,
        seed: str = None
    ) -> Dict[str, Any]:
        """
        Selects primary and verifiable nodes ensuring maximum anti-collusion diversity.
        If a seed is provided, the decision is deterministic (P2P convergence requirement).
        """
        if not available_nodes:
            raise ValueError("No available nodes to schedule task.")

        rng = random.Random(seed) if seed else random.Random()

        weights = dynamic_weights or {
            "reputation": 1.2, 
            "stake": 2.0, 
            "price": 2.0, 
            "reliability": 1.5,
            "dominance": 50.0
        }
        
        # Sort nodes by baseline score, use agent_id as secondary key for strict determinism
        scored_nodes = sorted(
            available_nodes, 
            key=lambda n: (self._score_node(n, task_value, weights), n.agent_id), 
            reverse=True
        )
        
        primary = scored_nodes[0]
        
        # Probabilistic redundancy for cost-saving vs security
        # Critical tasks always get at least 1 verifier, sometimes 2
        # Non-critical tasks randomly get verifiers 10% of the time
        verifiers: List[MarketNode] = []
        redundancy_level = 0
        
        if is_critical:
            redundancy_level = 2 if rng.random() < 0.2 else 1
        elif rng.random() < redundancy_probability:
            redundancy_level = 1
            
        if redundancy_level > 0:
            candidates = scored_nodes[1:]
            for candidate in candidates:
                if len(verifiers) >= redundancy_level:
                    break
                    
                correlation = self._compute_correlation_risk(primary, candidate)
                # Ensure verifying node is completely uncorrelated (< 0.5 risk)
                if correlation < 0.5:
                    verifiers.append(candidate)
                    pair = tuple(sorted([primary.agent_id, candidate.agent_id]))
                    self._interaction_history[pair] = self._interaction_history.get(pair, 0) + 1

        # 5% chance of Random Audit (re-run a task even if everything matches)
        trigger_audit = rng.random() < 0.05
        
        return {
            "primary": primary,
            "verifiers": verifiers,
            "redundancy_level": len(verifiers),
            "trigger_audit": trigger_audit
        }
