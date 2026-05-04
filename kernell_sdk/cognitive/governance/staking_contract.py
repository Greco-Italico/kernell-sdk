"""
Kernell OS SDK — Staking & Identity Contract
═════════════════════════════════════════════════════════════════════
Implements the Cost of Identity (Quadratic Staking) and Slashing 
mechanisms as defined in the Economic Consensus Specification v0.1.
"""
from decimal import Decimal
from typing import Dict, List
import hashlib

class StakingViolation(Exception):
    pass

class StakingContract:
    def __init__(self, s_min: Decimal = Decimal('100.0'), alpha: Decimal = Decimal('0.1')):
        self.s_min = s_min
        self.alpha = alpha
        
        # Entity mapping to track multiple identities controlled by one entity
        self.entity_identities: Dict[str, List[str]] = {} 
        self.stakes: Dict[str, Decimal] = {}

    def calculate_cost_of_identity(self, entity_id: str) -> Decimal:
        """C(n) = S_min * (1 + alpha * n^2)"""
        n = Decimal(len(self.entity_identities.get(entity_id, [])))
        return self.s_min * (Decimal('1.0') + self.alpha * (n ** 2))

    def register_identity(self, entity_id: str, public_key_hash: str, deposit: Decimal) -> None:
        required_stake = self.calculate_cost_of_identity(entity_id)
        if deposit < required_stake:
            raise StakingViolation(f"Insufficient stake. Required: {required_stake}, Provided: {deposit}")
        
        if entity_id not in self.entity_identities:
            self.entity_identities[entity_id] = []
            
        self.entity_identities[entity_id].append(public_key_hash)
        self.stakes[public_key_hash] = deposit

    def slash(self, public_key_hash: str, severity: Decimal) -> Decimal:
        """Burns a percentage of the stake based on severity (0.0 to 1.0)."""
        if public_key_hash not in self.stakes:
            raise StakingViolation("Identity not registered.")
            
        if not Decimal('0.0') <= severity <= Decimal('1.0'):
            raise ValueError("Severity must be between 0.0 and 1.0")
            
        current_stake = self.stakes[public_key_hash]
        burned_amount = current_stake * severity
        self.stakes[public_key_hash] -= burned_amount
        return burned_amount
