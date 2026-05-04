"""
Kernell OS SDK — EigenTrust-Hybrid Reputation System
═════════════════════════════════════════════════════════════════════
Calculates global reputation using an EigenTrust variant weighted by
Stake, preventing Sybil clusters from artificially inflating reputation.
"""
from decimal import Decimal
from typing import Dict, List
import numpy as np

class ReputationEngine:
    def __init__(self, damping_factor: float = 0.85, lambda_weight: float = 0.7):
        self.damping_factor = damping_factor
        self.lambda_weight = lambda_weight
        
        # local_trust[i][j] = Trust that i places in j (based on successful transactions)
        self.local_trust: Dict[str, Dict[str, float]] = {}

    def record_interaction(self, source_id: str, target_id: str, success_weight: float):
        """Records a local trust interaction (e.g., successful value transfer)."""
        if source_id not in self.local_trust:
            self.local_trust[source_id] = {}
        if target_id not in self.local_trust[source_id]:
            self.local_trust[source_id][target_id] = 0.0
            
        self.local_trust[source_id][target_id] += success_weight

    def calculate_global_reputation(self, stakes: Dict[str, Decimal]) -> Dict[str, float]:
        """
        Calculates global reputation score R = lambda * EigenTrust + (1-lambda) * StakeWeight.
        Uses Power Iteration for the EigenTrust central eigenvector.
        """
        nodes = list(stakes.keys())
        n = len(nodes)
        if n == 0:
            return {}

        node_idx = {node: i for i, node in enumerate(nodes)}
        
        # Build Normalized Local Trust Matrix (C)
        C = np.zeros((n, n))
        for i, node_i in enumerate(nodes):
            total_trust = sum(self.local_trust.get(node_i, {}).values())
            if total_trust > 0:
                for node_j, trust_val in self.local_trust.get(node_i, {}).items():
                    if node_j in node_idx:
                        j = node_idx[node_j]
                        C[i, j] = trust_val / total_trust
            else:
                # If no trust given, distribute evenly (or default to pre-trusted nodes)
                C[i, :] = 1.0 / n
                
        # Build Stake Vector (P)
        total_stake = sum(float(s) for s in stakes.values())
        P = np.array([float(stakes[node])/total_stake if total_stake > 0 else 1.0/n for node in nodes])
        
        # Power Iteration
        t = P.copy()
        for _ in range(50):
            # t(k+1) = (1-d)*P + d*C^T * t(k)
            t = (1 - self.damping_factor) * P + self.damping_factor * np.dot(C.T, t)
            
        # Hybrid Final Score
        final_scores = {}
        for i, node in enumerate(nodes):
            eigen_score = t[i]
            stake_score = P[i]
            hybrid = self.lambda_weight * eigen_score + (1 - self.lambda_weight) * stake_score
            final_scores[node] = float(hybrid)
            
        return final_scores
