"""
Kernell OS SDK — Execution Insurance
══════════════════════════════════════
Seguro de ejecución con reembolsos, penalizaciones,
backup workers y failover automático.
"""
from dataclasses import dataclass, field
from typing import Optional, Dict
from enum import Enum
import uuid


class RiskLevel(Enum):
    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"
    CRITICAL = "CRITICAL"

RISK_REFUND_RATE = {
    RiskLevel.LOW: 0.90,
    RiskLevel.MEDIUM: 0.75,
    RiskLevel.HIGH: 0.50,
    RiskLevel.CRITICAL: 0.25,
}

@dataclass
class InsurancePolicy:
    policy_id: str = field(default_factory=lambda: str(uuid.uuid4())[:10])
    escrow_id: str = ""
    buyer_id: str = ""
    seller_id: str = ""
    backup_seller_id: Optional[str] = None
    amount_kern: float = 0.0
    risk_level: RiskLevel = RiskLevel.MEDIUM
    refund_rate: float = 0.75
    penalty_rate: float = 0.10
    status: str = "active"  # active, claimed, resolved

class ExecutionInsurance:
    def __init__(self):
        self._policies: Dict[str, InsurancePolicy] = {}

    def create_policy(self, escrow_id: str, buyer_id: str, seller_id: str,
                      amount: float, risk: RiskLevel, backup_id: str = None) -> str:
        policy = InsurancePolicy(
            escrow_id=escrow_id, buyer_id=buyer_id, seller_id=seller_id,
            backup_seller_id=backup_id, amount_kern=amount,
            risk_level=risk, refund_rate=RISK_REFUND_RATE[risk],
        )
        self._policies[policy.policy_id] = policy
        return policy.policy_id

    def claim(self, policy_id: str) -> Dict:
        p = self._policies.get(policy_id)
        if not p:
            return {"error": "not_found"}
        refund = round(p.amount_kern * p.refund_rate, 2)
        penalty = round(p.amount_kern * p.penalty_rate, 2)
        p.status = "claimed"
        return {
            "refund_to_buyer": refund,
            "penalty_to_seller": penalty,
            "failover_to": p.backup_seller_id,
            "risk": p.risk_level.value,
        }
