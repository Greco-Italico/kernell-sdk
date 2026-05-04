import pytest
import time
import secrets
import hashlib
from kernell_sdk.security.kms import LocalKMS
from kernell_sdk.security import Capability, CapabilityToken, CapabilitySigner, CapabilityVerifier, CapabilityPolicyEngine
from kernell_sdk.runtime.models import ExecutionRequest

@pytest.fixture
def keys():
    kms = LocalKMS()
    key_id = "tenant:123:agent:007"
    kms.create_key(key_id)
    return kms, key_id

@pytest.fixture
def signer_verifier(keys):
    kms, key_id = keys
    return CapabilitySigner(kms, key_id), CapabilityVerifier(kms, key_id)

def test_capability_lifecycle(signer_verifier):
    signer, verifier = signer_verifier
    policy = CapabilityPolicyEngine()
    
    code = "print('hello')"
    code_hash = hashlib.sha256(code.encode()).hexdigest()
    
    token = CapabilityToken(
        subject="agent_007",
        capability=Capability(
            action="python.execute",
            max_cpu=1.0,
            max_memory_mb=128,
            allow_network=False,
            allowed_modules=[]
        ),
        issued_at=int(time.time()),
        expires_at=int(time.time()) + 60,
        nonce=secrets.token_hex(16),
        code_hash=code_hash
    )
    
    token.signature = signer.sign(token)
    
    # 1. Valid signature
    assert verifier.verify(token) is True
    
    # 2. Replay attack should fail
    assert verifier.verify(token) is False

def test_capability_policy_enforcement(signer_verifier):
    signer, verifier = signer_verifier
    policy = CapabilityPolicyEngine()
    
    code = "import os"
    code_hash = hashlib.sha256(code.encode()).hexdigest()
    
    token = CapabilityToken(
        subject="agent_x",
        capability=Capability(
            action="python.execute",
            max_cpu=1.0,
            max_memory_mb=64,
            allow_network=False,
            allowed_modules=[]
        ),
        issued_at=int(time.time()),
        expires_at=int(time.time()) + 60,
        nonce="unique_nonce",
        code_hash=code_hash
    )
    
    token.signature = signer.sign(token)
    assert verifier.verify(token) is True
    
    # Valid request
    req = ExecutionRequest(code=code, memory_limit_mb=64, cpu_limit=1.0, allow_network=False)
    assert policy.enforce(token, req) is True
    
    # Mismatch code
    req_bad_code = ExecutionRequest(code="import sys", memory_limit_mb=64, cpu_limit=1.0)
    with pytest.raises(Exception, match="Code hash mismatch"):
        policy.enforce(token, req_bad_code)
        
    # Exceed memory
    req_bad_mem = ExecutionRequest(code=code, memory_limit_mb=128, cpu_limit=1.0)
    with pytest.raises(Exception, match="Memory limit exceeded"):
        policy.enforce(token, req_bad_mem)
        
    # Attempt network
    req_net = ExecutionRequest(code=code, memory_limit_mb=64, cpu_limit=1.0, allow_network=True)
    with pytest.raises(Exception, match="Network not allowed"):
        policy.enforce(token, req_net)
