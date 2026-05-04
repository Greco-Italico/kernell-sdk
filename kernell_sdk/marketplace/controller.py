from typing import Dict, Any

class EconomicController:
    """
    Automatic Economic Controller (PID-style loop) for the Compute Marketplace.
    Adjusts weights and incentives dynamically to maintain protocol invariants 
    in a healthy equilibrium (avoiding both extreme centralization and extreme fragmentation).
    """
    def __init__(self):
        # Base weights for the Scheduler
        self.w_dominance = 50.0
        self.w_price = 2.0
        self.w_reputation = 1.2
        self.w_stake = 2.0
        
        # Redundancy and security params
        self.redundancy_probability = 0.1  # Base probability of using verifiers for non-critical tasks
        self.slashing_multiplier = 1.0     # Multiplier for slashing penalties

    def update(self, metrics: Dict[str, float]):
        """
        Takes real-time metrics and adjusts the control knobs.
        """
        # 1. Dominance Control (Target: ~0.3 or 30%)
        # If the market is too fragmented (Top 5 control very little), reduce dominance penalty
        if metrics.get("top_k_dominance", 0.0) < 0.2:
            self.w_dominance *= 0.9
        # If the market is centralizing, increase dominance penalty
        elif metrics.get("top_k_dominance", 0.0) > 0.5:
            self.w_dominance *= 1.2
            
        # 2. Cost Control
        avg_cost = metrics.get("avg_cost", 10.0)
        target_cost = 10.0
        if avg_cost > target_cost * 1.2:
            self.w_price *= 1.1
        elif avg_cost < target_cost * 0.7:
            self.w_price *= 0.9
            
        # 3. Security / Fraud Control
        # If fraud rate is high, increase redundancy and slashing
        fraud_rate = metrics.get("fraud_rate", 0.0)
        if fraud_rate > 0.03:
            self.redundancy_probability += 0.05
            self.slashing_multiplier *= 1.1
        # If fraud rate is very low, we can save compute by lowering redundancy
        elif fraud_rate < 0.01:
            self.redundancy_probability -= 0.02
            
        self._clamp()

    def _clamp(self):
        """Prevents infinite oscillations and extreme values."""
        self.w_dominance = min(max(self.w_dominance, 5.0), 200.0)
        self.w_price = min(max(self.w_price, 0.5), 10.0)
        self.redundancy_probability = min(max(self.redundancy_probability, 0.05), 0.5)
        self.slashing_multiplier = min(max(self.slashing_multiplier, 1.0), 3.0)

    def get_scheduler_weights(self) -> Dict[str, float]:
        return {
            "reputation": self.w_reputation,
            "stake": self.w_stake,
            "price": self.w_price,
            "reliability": 1.5,
            "dominance": self.w_dominance
        }
