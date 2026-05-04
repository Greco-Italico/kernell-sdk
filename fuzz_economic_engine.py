import threading
import random
from decimal import Decimal, getcontext
from kernell_sdk.cognitive.value_flow_engine import (
    StrictAddress, SingleUseCapability, ValueNode, ValueFlowGraph, ValueFlowViolation
)

getcontext().prec = 28

graph = ValueFlowGraph()

# Setup Entities
escrow_addr = StrictAddress("0xESCROW_HASH", "escrow_pool")
internal_addr_1 = StrictAddress("0xINTERNAL_1", "internal_agent")
internal_addr_2 = StrictAddress("0xINTERNAL_2", "internal_agent")
external_addr = StrictAddress("0xEXTERNAL_HASH", "external_vendor")

graph.register_node(ValueNode("0xESCROW_HASH", escrow_addr, Decimal('1000.0')))
graph.register_node(ValueNode("0xINTERNAL_1", internal_addr_1, Decimal('500.0')))
graph.register_node(ValueNode("0xINTERNAL_2", internal_addr_2, Decimal('500.0')))
graph.register_node(ValueNode("0xEXTERNAL_HASH", external_addr, Decimal('0.0')))

print("=== STARTING MASSIVE ADVERSARIAL FUZZING ===")

successes = 0
failures = 0
external_drains = 0

def fuzz_worker():
    global successes, failures, external_drains
    
    for _ in range(500):
        # Randomize flow types:
        # 1. Escrow to Internal
        # 2. Internal to Internal
        # 3. Internal to External
        choice = random.randint(1, 3)
        
        amount = Decimal(str(random.uniform(0.0001, 10.0)))
        
        if choice == 1:
            source = escrow_addr
            target = random.choice([internal_addr_1, internal_addr_2])
        elif choice == 2:
            source = internal_addr_1
            target = internal_addr_2
        else:
            source = random.choice([internal_addr_1, internal_addr_2])
            target = external_addr
            
        cap = SingleUseCapability(
            granted_to=source,
            target_address=target,
            action="transfer",
            max_amount=amount
        )
        
        try:
            graph.commit_transfer(cap, amount)
            successes += 1
            if target == external_addr:
                external_drains += 1
        except ValueFlowViolation:
            failures += 1
        except Exception as e:
            print(f"CRITICAL SYSTEM ERROR: {e}")

threads = []
for _ in range(10): # 10 threads, 500 ops each = 5000 transactions
    t = threading.Thread(target=fuzz_worker)
    threads.append(t)
    t.start()

for t in threads:
    t.join()

print(f"\n--- FUZZING RESULTS ---")
print(f"Total Transactions Attempted: {successes + failures}")
print(f"Successful Transfers: {successes}")
print(f"Blocked Violations (Invariants/Overbreadth): {failures}")
print(f"External Drains Allowed: {external_drains}")

print("\n--- INVARIANT CHECKS ---")
expected_total = Decimal('2000.0')
actual_total = sum(n.balance for n in graph.nodes.values())
print(f"Total Value Conserved: {expected_total == actual_total} (Expected: {expected_total}, Actual: {actual_total})")

# Check if any escrow taint leaked to external
external_provenance = graph.provenance.get("0xEXTERNAL_HASH", {})
escrow_leakage = external_provenance.get("0xESCROW_HASH", Decimal('0'))
print(f"Escrow Taint Leaked to External: {escrow_leakage} KERN")
if escrow_leakage > Decimal('0'):
    print("❌ SYSTEM COMPROMISED: Taint Collapse detected.")
else:
    print("✅ SYSTEM ANTIFRAGILE: 0.000 Escrow funds reached external wallets under massive fuzzing.")
