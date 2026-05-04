import json

from kernell_sdk.router.policy_lite import PolicyLiteClient
from kernell_sdk.router.types import PolicyRoute, RiskLevel


class FakePolicyLLM:
    def __init__(self, payload: dict):
        self.payload = payload

    def generate(self, prompt: str, system: str = "") -> str:
        return json.dumps(self.payload)


def test_policy_lite_parses_decision():
    llm = FakePolicyLLM({
        "route": "cheap",
        "confidence": 0.91,
        "needs_decomposition": False,
        "risk": "medium",
        "expected_cost_usd": 0.01,
        "expected_latency_s": 1.2,
        "max_budget_usd": 0.2,
    })
    client = PolicyLiteClient(llm)
    decision = client.decide("summarize this")
    assert decision.route == PolicyRoute.CHEAP
    assert decision.risk == RiskLevel.MEDIUM
    assert decision.needs_decomposition is False


def test_policy_lite_low_confidence_forces_hybrid():
    llm = FakePolicyLLM({
        "route": "local",
        "confidence": 0.2,
        "needs_decomposition": False,
        "risk": "low",
    })
    client = PolicyLiteClient(llm)
    decision = client.decide("any task")
    assert decision.route == PolicyRoute.HYBRID
    assert decision.needs_decomposition is True
