import pytest
import json
from kernell_sdk.security.intent_firewall import (
    PlanIR, PlanStep, DataRef, ActionType, DataSensitivity,
    DataSourceType, DataSinkType, PlanValidator
)

@pytest.mark.invariant("T3")
def test_deterministic_hashing():
    """Test that PlanIR hashing is deterministic regardless of key order."""
    plan1 = PlanIR(
        plan_id="test_hash_01",
        goal="Hash Test",
        declared_intent="Test",
        data_nodes=[
            DataRef(id="node1", sensitivity=DataSensitivity.PUBLIC, source=DataSourceType.FILE)
        ],
        steps=[
            PlanStep(id="s1", action=ActionType.READ_FILE, outputs=["node1"])
        ]
    )
    
    hash1 = plan1.hash_plan()
    
    # We create the exact same JSON with different dictionary key ordering (by changing the kwargs)
    # The Pydantic model dump will output keys in definition order, and the JSON canonicalizer
    # will sort them alphabetically.
    plan2 = PlanIR(
        goal="Hash Test",
        plan_id="test_hash_01",
        declared_intent="Test",
        data_nodes=[
            DataRef(source=DataSourceType.FILE, id="node1", sensitivity=DataSensitivity.PUBLIC)
        ],
        steps=[
            PlanStep(outputs=["node1"], action=ActionType.READ_FILE, id="s1")
        ]
    )
    
    hash2 = plan2.hash_plan()
    assert hash1 == hash2

    # A tiny change should change the hash
    plan1.goal = "Changed"
    assert plan1.hash_plan() != hash1

def test_validator_valid_case():
    """USER_INPUT -> TRANSFORM -> MEMORY should pass"""
    plan = PlanIR(
        plan_id="test_valid",
        goal="Valid test",
        declared_intent="Test",
        data_nodes=[
            DataRef(id="in1", sensitivity=DataSensitivity.PUBLIC, source=DataSourceType.USER_INPUT),
            DataRef(id="mem1", sensitivity=DataSensitivity.PUBLIC, source=DataSourceType.MEMORY)
        ],
        steps=[
            PlanStep(id="s1", action=ActionType.TRANSFORM, inputs=["in1"], outputs=["mem1"], sink_type=DataSinkType.MEMORY)
        ]
    )
    validator = PlanValidator(plan)
    res = validator.validate()
    assert res["valid"] is True, f"Expected valid, got: {res['violations']}"

def test_validator_direct_exfiltration():
    """FILE (public declared, but internal by source rule) -> NETWORK should fail"""
    plan = PlanIR(
        plan_id="test_exfil",
        goal="Leak",
        declared_intent="Test",
        data_nodes=[
            # The LLM claims it's public, but our system upgrades FILE to INTERNAL
            DataRef(id="file1", sensitivity=DataSensitivity.PUBLIC, source=DataSourceType.FILE)
        ],
        steps=[
            PlanStep(id="s1", action=ActionType.READ_FILE, outputs=["file1"]),
            PlanStep(id="s2", action=ActionType.NETWORK_REQUEST, inputs=["file1"], sink_type=DataSinkType.NETWORK)
        ]
    )
    res = PlanValidator(plan).validate()
    assert res["valid"] is False
    assert any(v["rule"] == "DATA_EXFILTRATION" for v in res["violations"])

def test_validator_implicit_branch():
    """
    FILE (internal) -> TRANSFORM -> MERGE -> NETWORK
    PUBLIC -------> TRANSFORM
    """
    plan = PlanIR(
        plan_id="test_implicit",
        goal="Leak via merge",
        declared_intent="Test",
        data_nodes=[
            DataRef(id="secret_file", sensitivity=DataSensitivity.SECRET, source=DataSourceType.FILE),
            DataRef(id="public_data", sensitivity=DataSensitivity.PUBLIC, source=DataSourceType.MEMORY),
            DataRef(id="merged_out", sensitivity=DataSensitivity.PUBLIC, source=DataSourceType.MEMORY)
        ],
        steps=[
            PlanStep(id="s_read", action=ActionType.READ_FILE, outputs=["secret_file"]),
            PlanStep(id="s_read_pub", action=ActionType.READ_FILE, outputs=["public_data"]),
            PlanStep(id="s_merge", action=ActionType.TRANSFORM, inputs=["secret_file", "public_data"], outputs=["merged_out"]),
            PlanStep(id="s_net", action=ActionType.NETWORK_REQUEST, inputs=["merged_out"], sink_type=DataSinkType.NETWORK)
        ]
    )
    res = PlanValidator(plan).validate()
    assert res["valid"] is False
    assert any(v["rule"] == "DATA_EXFILTRATION" for v in res["violations"])

def test_validator_closed_world():
    """Undeclared node should fail"""
    plan = PlanIR(
        plan_id="test_closed_world",
        goal="Test",
        declared_intent="Test",
        data_nodes=[],
        steps=[
            PlanStep(id="s1", action=ActionType.READ_FILE, outputs=["ghost_node"])
        ]
    )
    res = PlanValidator(plan).validate()
    assert res["valid"] is False
    assert any(v["rule"] == "UNDECLARED_NODE" for v in res["violations"])

def test_validator_source_forgery():
    """A transform step with no inputs is a forged source"""
    plan = PlanIR(
        plan_id="test_forgery",
        goal="Test",
        declared_intent="Test",
        data_nodes=[
            DataRef(id="magic_node", sensitivity=DataSensitivity.PUBLIC, source=DataSourceType.MEMORY)
        ],
        steps=[
            PlanStep(id="s1", action=ActionType.TRANSFORM, inputs=[], outputs=["magic_node"])
        ]
    )
    res = PlanValidator(plan).validate()
    assert res["valid"] is False
    assert any(v["rule"] == "SOURCE_FORGERY" for v in res["violations"])

import time
from cryptography.hazmat.primitives.asymmetric import ed25519
from kernell_sdk.security.intent_firewall import (
    TokenAuthority, CapabilityToken, OrchestratorStub,
    PlanDriftError, InvalidCapabilityToken, UnsupportedActionError
)

@pytest.fixture
def auth_keys():
    priv = ed25519.Ed25519PrivateKey.generate()
    return TokenAuthority(priv)

@pytest.fixture
def sample_plan():
    return PlanIR(
        plan_id="test_exec_01",
        goal="Test Exec",
        declared_intent="Test",
        data_nodes=[
            DataRef(id="in1", sensitivity=DataSensitivity.PUBLIC, source=DataSourceType.FILE),
            DataRef(id="out1", sensitivity=DataSensitivity.PUBLIC, source=DataSourceType.MEMORY)
        ],
        steps=[
            PlanStep(id="s1", action=ActionType.READ_FILE, outputs=["in1"]),
            PlanStep(id="s2", action=ActionType.TRANSFORM, inputs=["in1"], outputs=["out1"])
        ]
    )

def test_orchestrator_valid(auth_keys, sample_plan):
    token = CapabilityToken(
        plan_hash=sample_plan.hash_plan(),
        policy_version="1.0",
        issued_at=time.time(),
        expires_at=time.time() + 3600,
        execution_id="exec_1"
    )
    token = auth_keys.sign_token(token)
    
    orchestrator = OrchestratorStub(auth_keys)
    result = orchestrator.execute(sample_plan, token)
    assert result["status"] == "success"
    assert "out1" in result["runtime_state"]

def test_orchestrator_plan_drift(auth_keys, sample_plan):
    token = CapabilityToken(
        plan_hash=sample_plan.hash_plan(),
        policy_version="1.0",
        issued_at=time.time(),
        expires_at=time.time() + 3600,
        execution_id="exec_1"
    )
    token = auth_keys.sign_token(token)
    
    # Mutate the plan after signature
    sample_plan.goal = "Hacked Goal"
    
    orchestrator = OrchestratorStub(auth_keys)
    with pytest.raises(PlanDriftError):
        orchestrator.execute(sample_plan, token)

def test_orchestrator_invalid_signature(auth_keys, sample_plan):
    token = CapabilityToken(
        plan_hash=sample_plan.hash_plan(),
        policy_version="1.0",
        issued_at=time.time(),
        expires_at=time.time() + 3600,
        execution_id="exec_1"
    )
    token = auth_keys.sign_token(token)
    
    # Tamper with the token
    token.execution_id = "exec_hacked"
    
    orchestrator = OrchestratorStub(auth_keys)
    with pytest.raises(InvalidCapabilityToken):
        orchestrator.execute(sample_plan, token)

def test_orchestrator_unknown_action(auth_keys):
    plan = PlanIR(
        plan_id="test_exec_bad",
        goal="Test Exec",
        declared_intent="Test",
        data_nodes=[],
        steps=[
            PlanStep(id="s1", action=ActionType.EXECUTE_CODE)
        ]
    )
    token = CapabilityToken(
        plan_hash=plan.hash_plan(),
        policy_version="1.0",
        issued_at=time.time(),
        expires_at=time.time() + 3600,
        execution_id="exec_1"
    )
    token = auth_keys.sign_token(token)
    
    orchestrator = OrchestratorStub(auth_keys)
    with pytest.raises(UnsupportedActionError):
        orchestrator.execute(plan, token)

class MockProvenanceStore:
    def __init__(self):
        self.data = {}
    def get(self, key):
        return self.data.get(key)
    def set(self, key, value):
        self.data[key] = value

def test_multi_plan_exfiltration(auth_keys):
    store = MockProvenanceStore()
    
    # PLAN 1: Write a secret to a file
    plan1 = PlanIR(
        plan_id="plan_write",
        goal="Write secret",
        declared_intent="Test",
        data_nodes=[
            DataRef(id="secret", sensitivity=DataSensitivity.SECRET, source=DataSourceType.MEMORY)
        ],
        steps=[
            PlanStep(id="s1", action=ActionType.WRITE_FILE, inputs=["secret"], params={"path": "/tmp/shared_leak"})
        ]
    )
    token1 = CapabilityToken(
        plan_hash=plan1.hash_plan(),
        policy_version="1.0",
        issued_at=time.time(),
        expires_at=time.time() + 3600,
        execution_id="exec_1"
    )
    token1 = auth_keys.sign_token(token1)
    
    orchestrator1 = OrchestratorStub(auth_keys, provenance_store=store)
    # This must populate the runtime values beforehand so the write_file step sees the input as SECRET.
    # We do a quick hijack of the method to simulate "secret" existing in state.
    # In reality, a read or transform would produce "secret". Let's add a transform step to create it cleanly.
    plan1.steps.insert(0, PlanStep(id="s0", action=ActionType.TRANSFORM, inputs=[], outputs=["secret"]))
    token1.plan_hash = plan1.hash_plan() # recalculate
    token1 = auth_keys.sign_token(token1)
    
    orchestrator1.execute(plan1, token1)
    
    # PLAN 2: Read from the SAME file and try to exfiltrate
    plan2 = PlanIR(
        plan_id="plan_read",
        goal="Read leak",
        declared_intent="Test",
        data_nodes=[
            DataRef(id="leaked_data", sensitivity=DataSensitivity.PUBLIC, source=DataSourceType.FILE) # LLM lies!
        ],
        steps=[
            PlanStep(id="s2", action=ActionType.READ_FILE, outputs=["leaked_data"], params={"path": "/tmp/shared_leak"}),
            PlanStep(id="s3", action=ActionType.NETWORK_REQUEST, inputs=["leaked_data"], sink_type=DataSinkType.NETWORK)
        ]
    )
    token2 = CapabilityToken(
        plan_hash=plan2.hash_plan(),
        policy_version="1.0",
        issued_at=time.time(),
        expires_at=time.time() + 3600,
        execution_id="exec_2"
    )
    token2 = auth_keys.sign_token(token2)
    
    orchestrator2 = OrchestratorStub(auth_keys, provenance_store=store)
    
    # Exfiltration should be blocked at runtime!
    with pytest.raises(Exception, match="RUNTIME EXFILTRATION BLOCKED"):
        orchestrator2.execute(plan2, token2)

def test_inode_persistence_overwrite_attack(auth_keys):
    store = MockProvenanceStore()
    
    # PLAN 1: Write SECRET
    plan1 = PlanIR(
        plan_id="plan_overwrite_1",
        goal="Write secret",
        declared_intent="Test",
        data_nodes=[
            DataRef(id="secret", sensitivity=DataSensitivity.SECRET, source=DataSourceType.MEMORY)
        ],
        steps=[
            PlanStep(id="s0", action=ActionType.TRANSFORM, inputs=[], outputs=["secret"]),
            PlanStep(id="s1", action=ActionType.WRITE_FILE, inputs=["secret"], params={"path": "/tmp/overwrite_leak"})
        ]
    )
    token1 = auth_keys.sign_token(CapabilityToken(
        plan_hash=plan1.hash_plan(), policy_version="1.0", issued_at=time.time(), expires_at=time.time() + 3600, execution_id="exec_1"
    ))
    OrchestratorStub(auth_keys, provenance_store=store).execute(plan1, token1)
    
    # PLAN 2: Overwrite same path with PUBLIC (Attempt laundering)
    plan2 = PlanIR(
        plan_id="plan_overwrite_2",
        goal="Launder secret",
        declared_intent="Test",
        data_nodes=[
            DataRef(id="public", sensitivity=DataSensitivity.PUBLIC, source=DataSourceType.MEMORY)
        ],
        steps=[
            PlanStep(id="s0", action=ActionType.TRANSFORM, inputs=[], outputs=["public"]),
            PlanStep(id="s1", action=ActionType.WRITE_FILE, inputs=["public"], params={"path": "/tmp/overwrite_leak"})
        ]
    )
    token2 = auth_keys.sign_token(CapabilityToken(
        plan_hash=plan2.hash_plan(), policy_version="1.0", issued_at=time.time(), expires_at=time.time() + 3600, execution_id="exec_2"
    ))
    OrchestratorStub(auth_keys, provenance_store=store).execute(plan2, token2)
    
    # PLAN 3: Read and attempt exfiltration
    plan3 = PlanIR(
        plan_id="plan_overwrite_3",
        goal="Exfiltrate",
        declared_intent="Test",
        data_nodes=[
            DataRef(id="laundered_data", sensitivity=DataSensitivity.PUBLIC, source=DataSourceType.FILE)
        ],
        steps=[
            PlanStep(id="s1", action=ActionType.READ_FILE, outputs=["laundered_data"], params={"path": "/tmp/overwrite_leak"}),
            PlanStep(id="s2", action=ActionType.NETWORK_REQUEST, inputs=["laundered_data"], sink_type=DataSinkType.NETWORK)
        ]
    )
    token3 = auth_keys.sign_token(CapabilityToken(
        plan_hash=plan3.hash_plan(), policy_version="1.0", issued_at=time.time(), expires_at=time.time() + 3600, execution_id="exec_3"
    ))
    
    # EXPECT: still blocked because Historical Taint > Overwrite Level
    with pytest.raises(Exception, match="RUNTIME EXFILTRATION BLOCKED"):
        OrchestratorStub(auth_keys, provenance_store=store).execute(plan3, token3)

