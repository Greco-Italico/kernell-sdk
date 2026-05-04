from kernell_sdk.cognitive.value_flow_engine import (
    StrictAddress, SingleUseCapability, ValueNode, ValueFlowGraph, ValueFlowViolation
)

# 1. Setup Graph and Addresses
graph = ValueFlowGraph()

escrow_addr = StrictAddress("0xESCROW_HASH", "escrow_pool")
wallet_a_addr = StrictAddress("0xWALLET_A_HASH", "internal_agent")
attacker_addr = StrictAddress("0xATTACKER_HASH", "external_vendor")

graph.register_node(ValueNode("0xESCROW_HASH", escrow_addr, 1000.0))
graph.register_node(ValueNode("0xWALLET_A_HASH", wallet_a_addr, 0.0))
graph.register_node(ValueNode("0xATTACKER_HASH", attacker_addr, 0.0))

print("=== SCENARIO: Multi-step Escrow Drain (Phase 3 Attack) ===")
# Step 1: Escrow -> Wallet A (Allowed by local rules)
cap1 = SingleUseCapability(
    granted_to=escrow_addr,
    target_address=wallet_a_addr,
    action="transfer",
    max_amount=100.0
)

try:
    tx1 = graph.commit_transfer(cap1, 100.0)
    print(f"[STEP 1] Escrow -> Wallet A: SUCCESS (Tx: {tx1})")
except Exception as e:
    print(f"[STEP 1] Escrow -> Wallet A: FAILED - {e}")

# Step 2: Wallet A -> Attacker (Should fail due to provenance!)
cap2 = SingleUseCapability(
    granted_to=wallet_a_addr,
    target_address=attacker_addr,
    action="transfer",
    max_amount=100.0
)

try:
    tx2 = graph.commit_transfer(cap2, 100.0)
    print(f"[STEP 2] Wallet A -> Attacker: SUCCESS (Tx: {tx2})")
except Exception as e:
    print(f"[STEP 2] Wallet A -> Attacker: FAILED - {e}")

print("\n=== SCENARIO: Capability Replay Attack (Phase 4 Attack) ===")
try:
    tx_replay = graph.commit_transfer(cap1, 50.0)
    print(f"Replay Attack: SUCCESS")
except Exception as e:
    print(f"Replay Attack: FAILED - {e}")

