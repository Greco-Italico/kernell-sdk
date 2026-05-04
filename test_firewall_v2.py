from kernell_sdk.cognitive.intent_firewall import (
    IntentFirewall, AgentIntent, ActionType, TaintLevel, Capability
)
import base64
import json

firewall = IntentFirewall()

# Give some basic capabilities for testing
firewall.grant_capability("token-123", Capability(
    action=ActionType.TRANSFER,
    constraints={"amount": 100, "from": "wallet_A", "to": "wallet_B"},
    granted_to="agent_1"
))

print("=== ATTACK 1: Semantic Obfuscation (Rebalance Portfolio) ===")
intent1 = AgentIntent(
    agent_id="agent_1",
    action_type=ActionType.TRANSFER, # Assuming it gets mapped here
    capability_token="token-123",
    structured_payload={
        "action": "rebalance_portfolio",
        "operation": "reallocate",
        "from": "escrow_pool_A",
        "to": "external_liquidity_sink",
        "amount": "97%"
    }
)
decision1 = firewall.evaluate(intent1)
print(f"Result: {decision1.verdict} | Reason: {decision1.reason}\n")


print("=== ATTACK 3: Payload Encoding (Base64) ===")
# Base64 for 'transfer(escrow, attacker_wallet, 100%)'
b64_payload = base64.b64encode(b'{"from": "escrow_pool_A", "to": "external_liquidity_sink", "amount": 100}').decode('utf-8')

intent3 = AgentIntent(
    agent_id="agent_1",
    action_type=ActionType.TRANSFER,
    capability_token="token-123", # Let's assume they stole a token
    structured_payload={
        "from": "escrow_pool_A",
        "to": b64_payload, # Hiding it here
        "amount": 100
    }
)
decision3 = firewall.evaluate(intent3)
print(f"Result: {decision3.verdict} | Reason: {decision3.reason}\n")


print("=== VALID ACTION (With correct capability) ===")
intent_valid = AgentIntent(
    agent_id="agent_1",
    action_type=ActionType.TRANSFER,
    capability_token="token-123",
    structured_payload={
        "from": "wallet_A",
        "to": "wallet_B",
        "amount": 100
    }
)
decision_valid = firewall.evaluate(intent_valid)
print(f"Result: {decision_valid.verdict} | Reason: {decision_valid.reason}\n")

