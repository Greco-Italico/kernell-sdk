from kernell_sdk.reputation.receipt import ExecutionReceipt

class ReputationEngine:
    """
    Evaluates node performance and updates their reputation score 
    based on the cryptographically verified ExecutionReceipt.
    """

    def __init__(self):
        # In a real distributed system, this interacts with a DB/Smart Contract
        self._scores = {}

    def get_score(self, agent_id: str) -> int:
        return self._scores.get(agent_id, 100)  # New nodes start at 100

    def update_reputation(self, receipt: ExecutionReceipt) -> int:
        """
        Updates reputation based on execution truth.
        """
        current_score = self.get_score(receipt.agent_id)
        
        # Base penalty for failing a task
        if not receipt.success:
            current_score -= 20
        # Penalize nodes that advertise isolated/constrained but secretly fallback
        elif receipt.fallback_triggered:
            current_score -= 5
        # Reward nodes that execute cleanly
        else:
            current_score += 2

        # Clamping logic
        new_score = max(0, min(100, current_score))
        self._scores[receipt.agent_id] = new_score
        
        return new_score

    def apply_decay(self, decay_factor: float = 0.98):
        """
        Applies a temporal decay to all nodes.
        Prevents "High reputation exploits" where a node builds trust indefinitely
        and then suddenly attacks or goes lazy.
        """
        for agent_id, score in self._scores.items():
            self._scores[agent_id] = max(0.0, score * decay_factor)

    def compute_slashing_penalty(self, receipt: ExecutionReceipt, stake_amount: float, fraud_detected: bool = False) -> float:
        """
        Calculates the amount of KERN to slash from the node's stake.
        """
        if fraud_detected:
            return stake_amount * 0.50  # Heavy 50% slash for fraud
            
        if receipt.success:
            return 0.0
            
        # Standard SLA failure penalty: 10% of staked amount
        return stake_amount * 0.10
