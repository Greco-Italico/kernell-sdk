"""
Kernell OS SDK — Value Flow Engine V2 (High-Concurrency Edition)
═════════════════════════════════════════════════════════════════════
Transitions security from "Global Serial Lock" to "Fine-Grained Optimistic Sharding".
Protects against:
- Lock Amplification & Queue Buildup
- Global Serialization Bottlenecks
"""
from __future__ import annotations

import hashlib
import time
import uuid
import threading
from decimal import Decimal, getcontext
from dataclasses import dataclass, field
from typing import Dict, List, Set, Optional

getcontext().prec = 28

ATOMIC_TRANSFER_LUA = """
-- KEYS:
-- 1: source_balance_key
-- 2: target_balance_key
-- 3: token_key
-- ARGV:
-- 1: amount
-- 2: token_ttl_seconds
-- 3: tx_id
-- 4: source_id
-- 5: target_id

local source_balance = tonumber(redis.call("GET", KEYS[1]) or "0")
local amount = tonumber(ARGV[1])

if redis.call("EXISTS", KEYS[3]) == 1 then
    return "ERR_TOKEN_USED"
end

if source_balance < amount then
    return "ERR_INSUFFICIENT_FUNDS"
end

redis.call("DECRBYFLOAT", KEYS[1], amount)
redis.call("INCRBYFLOAT", KEYS[2], amount)
redis.call("SET", KEYS[3], "1", "EX", tonumber(ARGV[2]))
redis.call("XADD", "kernell:ledger", "*", "tx_id", ARGV[3], "from", ARGV[4], "to", ARGV[5], "amount", ARGV[1])

return "OK"
"""

class ValueFlowViolation(Exception):
    pass

@dataclass(frozen=True)
class StrictAddress:
    public_key_hash: str
    entity_type: str

@dataclass
class SingleUseCapability:
    granted_to: StrictAddress = field(init=True)
    target_address: StrictAddress = field(init=True)
    action: str = field(init=True)
    max_amount: Decimal = field(init=True)
    capability_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    nonce: str = field(default_factory=lambda: uuid.uuid4().hex)
    expires_at: float = field(default_factory=lambda: time.time() + 60.0)
    
    is_consumed: bool = field(default=False)
    
    def verify_and_consume(self, amount: Decimal):
        if self.is_consumed:
            raise ValueFlowViolation("Capability Replay Attack: Token already consumed.")
        if time.time() > self.expires_at:
            raise ValueFlowViolation("Capability Expired.")
        if amount > self.max_amount:
            raise ValueFlowViolation(f"Capability Overbreadth: {amount} > {self.max_amount}")
        self.is_consumed = True

@dataclass
class ValueNode:
    node_id: str
    address: StrictAddress
    balance: Decimal

@dataclass
class ValueEdge:
    transaction_id: str
    source_node: str
    target_node: str
    amount: Decimal
    previous_hash: str
    timestamp: float = field(default_factory=time.time)
    tx_hash: str = field(init=False)
    
    def __post_init__(self):
        payload = f"{self.transaction_id}|{self.source_node}|{self.target_node}|{self.amount}|{self.previous_hash}"
        self.tx_hash = hashlib.sha256(payload.encode()).hexdigest()

class ValueFlowGraph:
    def __init__(self, redis_url: Optional[str] = None):
        self.nodes: Dict[str, ValueNode] = {}
        self.edges: List[ValueEdge] = []
        self.last_tx_hash: str = "0000000000000000000000000000000000000000000000000000000000000000"
        
        self.provenance: Dict[str, Dict[str, Decimal]] = {}
        self._total_system_value: Decimal = Decimal('0')
        self._emergency_freeze: bool = False
        
        self.redis_client = None
        self._transfer_script_sha = None
        if redis_url:
            import redis
            self.redis_client = redis.Redis.from_url(redis_url, decode_responses=True)
            self._transfer_script_sha = self.redis_client.script_load(ATOMIC_TRANSFER_LUA)

    def activate_kill_switch(self):
        self._emergency_freeze = True
            
    def deactivate_kill_switch(self):
        self._emergency_freeze = False

    def register_node(self, node: ValueNode):
        self.nodes[node.node_id] = node
        self.provenance[node.node_id] = {node.node_id: node.balance}
        self._total_system_value += node.balance

    def validate_trajectory(self, source_id: str, target_id: str, amount: Decimal) -> None:
        source_node = self.nodes.get(source_id)
        target_node = self.nodes.get(target_id)
        
        if not source_node or not target_node:
            raise ValueFlowViolation("Unregistered nodes in value flow.")
        if source_node.balance < amount:
            raise ValueFlowViolation("Insufficient balance for flow.")

        source_history = self.provenance.get(source_id, {})
        
        for origin_id, tainted_amount in source_history.items():
            origin_node = self.nodes.get(origin_id)
            if origin_node and origin_node.address.entity_type == 'escrow_pool':
                proportion = tainted_amount / source_node.balance if source_node.balance > Decimal('0') else Decimal('0')
                tainted_transfer = amount * proportion
                
                if target_node.address.entity_type == 'external_vendor' and tainted_transfer > Decimal('0'):
                    raise ValueFlowViolation(
                        f"MULTI-HOP INVARIANT BROKEN: {tainted_transfer} of Escrow taint reaching External."
                    )

    def commit_transfer(self, capability: SingleUseCapability, amount: Decimal) -> str:
        if self._emergency_freeze:
            raise ValueFlowViolation("SYSTEM FROZEN: Emergency Kill Switch is active. No transactions allowed.")
            
        source_id = capability.granted_to.public_key_hash
        target_id = capability.target_address.public_key_hash
        
        if source_id == target_id:
            raise ValueFlowViolation("Self-transfers are not allowed.")
            
        tx_id = uuid.uuid4().hex
        
        # Redis Atomic Execution path
        if self.redis_client:
            source_key = f"balance:{source_id}"
            target_key = f"balance:{target_id}"
            token_key = f"token:{capability.capability_id}"
            
            try:
                import redis
                result = self.redis_client.evalsha(
                    self._transfer_script_sha, 3,
                    source_key, target_key, token_key,
                    str(amount), "300", tx_id, source_id, target_id
                )
            except redis.exceptions.NoScriptError:
                self._transfer_script_sha = self.redis_client.script_load(ATOMIC_TRANSFER_LUA)
                result = self.redis_client.evalsha(
                    self._transfer_script_sha, 3,
                    source_key, target_key, token_key,
                    str(amount), "300", tx_id, source_id, target_id
                )
            except Exception as e:
                raise ValueFlowViolation(f"Redis execution failed: {e}")
                
            if result == "ERR_TOKEN_USED":
                raise ValueFlowViolation("Capability Replay Attack: Token already consumed.")
            elif result == "ERR_INSUFFICIENT_FUNDS":
                raise ValueFlowViolation("Insufficient balance for flow.")
            elif result != "OK":
                raise ValueFlowViolation(f"Unexpected Redis response: {result}")
        else:
            # Fallback path for local execution (no local locks as they are unsafe and useless)
            capability.verify_and_consume(amount)
            if source_id not in self.nodes or target_id not in self.nodes:
                raise ValueFlowViolation("Nodes not registered.")
                
            self.validate_trajectory(source_id, target_id, amount)
            source_node = self.nodes[source_id]
            target_node = self.nodes[target_id]
            
            source_node.balance -= amount
            target_node.balance += amount
                
        # Append logic (Ledger state)
        edge = ValueEdge(tx_id, source_id, target_id, amount, self.last_tx_hash)
        self.last_tx_hash = edge.tx_hash
        self.edges.append(edge)
        
        return tx_id

    def audit_system_value(self):
        current_total = sum(n.balance for n in self.nodes.values())
        if current_total != self._total_system_value:
            raise ValueFlowViolation("SYSTEMIC ERROR: Value conservation broken.")
