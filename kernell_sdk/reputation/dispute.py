from enum import Enum
from typing import Optional, List
import logging

from kernell_sdk.reputation.receipt import ExecutionReceipt
from kernell_sdk.reputation.engine import ReputationEngine

logger = logging.getLogger(__name__)

class DisputeResult(Enum):
    MATCH = "match"
    MISMATCH = "mismatch"
    FRAUD_DETECTED = "fraud_detected"
    PENDING_ARBITRATION = "pending_arbitration"

class DisputeArbitrationSystem:
    """
    Handles automatic arbitration for redundant execution.
    Missions that are deemed critical are sent to a Primary node and a Verifier node.
    This system compares their receipts and enforces economic slashing if they differ.
    """

    def __init__(self, reputation_engine: ReputationEngine):
        self.reputation = reputation_engine

    def verify_redundancy(
        self, 
        primary_receipt: ExecutionReceipt, 
        verifier_receipt: ExecutionReceipt
    ) -> DisputeResult:
        """
        Compares two verified receipts for the same task.
        """
        if primary_receipt.task_hash != verifier_receipt.task_hash:
            raise ValueError("Cannot arbitrate: Receipts do not belong to the same task.")

        # 1. Check Output Hashes
        if primary_receipt.output_hash == verifier_receipt.output_hash:
            logger.info("Redundant verification successful. Outputs match.")
            return DisputeResult.MATCH

        # 2. Output Mismatch - Arbitration Triggered
        logger.warning(
            f"ARBITRATION TRIGGERED: Output mismatch between primary({primary_receipt.agent_id}) "
            f"and verifier({verifier_receipt.agent_id})."
        )
        return self._resolve_dispute(primary_receipt, verifier_receipt)

    def _resolve_dispute(
        self, 
        primary_receipt: ExecutionReceipt, 
        verifier_receipt: ExecutionReceipt
    ) -> DisputeResult:
        """
        Resolves the dispute using economic weighting.
        In a fully decentralized system, this triggers an on-chain oracle or a third verifier.
        Here, we use deterministic local slashing based on historical reputation.
        """
        score_primary = self.reputation.get_score(primary_receipt.agent_id)
        score_verifier = self.reputation.get_score(verifier_receipt.agent_id)
        
        # If one node has drastically lower reputation, assume it is fraudulent
        score_diff = abs(score_primary - score_verifier)
        
        if score_diff > 30:
            fraudulent_node = primary_receipt.agent_id if score_primary < score_verifier else verifier_receipt.agent_id
            logger.critical(f"Fraud resolved via Reputation Oracle. Slashing node: {fraudulent_node}")
            
            # Heavy penalty for being caught in a dispute with a trusted node
            self.reputation._scores[fraudulent_node] = max(0, self.reputation.get_score(fraudulent_node) - 50)
            return DisputeResult.FRAUD_DETECTED
            
        # If reputation is similar, we cannot automatically resolve.
        # Action: Freeze payment for both, slash both slightly for uncertainty,
        # or require a 3rd node to break the tie.
        logger.warning("Reputation tie. Dispatching to 3rd Verifier node for tie-breaker.")
        return DisputeResult.PENDING_ARBITRATION
