import time
import hmac
import hashlib
import pytest
import json

from kernell_os_sdk.security.iam import (
    VaultBackend,
    NamespacedVault,
    IAMPolicyEngine,
    SecurityViolation,
    Unauthorized,
)

@pytest.fixture
def iam_system():
    backend = VaultBackend()
    vault = NamespacedVault(backend)
    engine = IAMPolicyEngine(vault)
    
    # Setup test agents for tenant 't1'
    vault.put_secret("t1", "agent_a", "signing_key", "secret_A")
    vault.put_secret("t1", "agent_b", "signing_key", "secret_B")
    
    return engine, vault

def sign_request(tenant_id: str, agent_id: str, secret: str, body: str, timestamp: int, method: str = "POST", path: str = "/test") -> str:
    return hmac.new(
        secret.encode('utf-8'),
        f"{tenant_id}.{timestamp}.{method}.{path}.{body}".encode('utf-8'),
        hashlib.sha256
    ).hexdigest()

def test_agent_impersonation(iam_system):
    engine, vault = iam_system
    
    body = json.dumps({"action": "withdraw"})
    ts = int(time.time())
    
    # agent_b intenta firmar como agent_a usando su propio secreto (fake_secret para agent_a)
    signature = sign_request("t1", "agent_a", "secret_B", body, ts)
    
    with pytest.raises(Unauthorized, match="Invalid signature"):
        engine.verify_request("t1", "agent_a", signature, ts, "POST", "/test", body)

def test_internal_replay_attack(iam_system):
    engine, vault = iam_system
    
    body = json.dumps({"action": "withdraw"})
    ts = int(time.time())
    
    signature = sign_request("t1", "agent_a", "secret_A", body, ts)
    
    # Intento 1: válido
    assert engine.verify_request("t1", "agent_a", signature, ts, "POST", "/test", body) is True
    
    # Intento 2: Replay exacto (mismo signature, ts, body)
    with pytest.raises(Unauthorized, match="Replay attack detected"):
        engine.verify_request("t1", "agent_a", signature, ts, "POST", "/test", body)

def test_vault_scope_escape(iam_system):
    engine, vault = iam_system
    
    # Intentar acceder al secreto de agent_b usando directory traversal
    with pytest.raises(SecurityViolation, match="Path traversal attempt"):
        vault.get_secret("t1", "agent_a", "../agent_b/signing_key")
        
    with pytest.raises(SecurityViolation, match="Path traversal attempt"):
        vault.get_secret("t1", "agent_a", "/etc/passwd")

def test_missing_signature(iam_system):
    engine, vault = iam_system
    
    body = json.dumps({"action": "withdraw"})
    ts = int(time.time())
    
    # Intento con signature vacío
    with pytest.raises(Unauthorized, match="Missing signature"):
        engine.verify_request("t1", "agent_a", "", ts, "POST", "/test", body)
        
    # Intento con signature en None
    with pytest.raises(Unauthorized, match="Missing signature"):
        engine.verify_request("t1", "agent_a", None, ts, "POST", "/test", body)

def test_replay_window_exceeded(iam_system):
    engine, vault = iam_system
    
    body = json.dumps({"action": "withdraw"})
    ts = int(time.time()) - 35 # 35 segundos en el pasado (fuera de la ventana de 30s)
    
    signature = sign_request("t1", "agent_a", "secret_A", body, ts)
    
    with pytest.raises(Unauthorized, match="Replay window exceeded"):
        engine.verify_request("t1", "agent_a", signature, ts, "POST", "/test", body)

def test_iam_policy_authorization(iam_system):
    engine, vault = iam_system
    
    # Configurar políticas IAM
    engine.grant_policy("t1", "agent_a", ["read:own_secrets", "execute:escrow.*"])
    engine.grant_policy("t1", "agent_b", ["read:own_secrets"])
    
    body = json.dumps({"data": "payload"})
    ts = int(time.time())
    
    # agent_a intenta hacer execute:escrow.release (debería permitirse)
    sig_a = sign_request("t1", "agent_a", "secret_A", body, ts)
    assert engine.verify_request("t1", "agent_a", sig_a, ts, "POST", "/test", body, action="execute:escrow.release") is True
    
    # agent_b intenta hacer execute:escrow.release (debería denegarse)
    ts2 = ts + 1
    sig_b = sign_request("t1", "agent_b", "secret_B", body, ts2)
    with pytest.raises(Unauthorized, match="IAM Policy Deny: Agent agent_b in Tenant t1 is not authorized for execute:escrow.release"):
        engine.verify_request("t1", "agent_b", sig_b, ts2, "POST", "/test", body, action="execute:escrow.release")
        
    # agent_b intenta hacer read:own_secrets (debería permitirse)
    ts3 = ts + 2
    sig_b2 = sign_request("t1", "agent_b", "secret_B", body, ts3)
    assert engine.verify_request("t1", "agent_b", sig_b2, ts3, "POST", "/test", body, action="read:own_secrets") is True

def test_key_rotation_and_revocation(iam_system):
    engine, vault = iam_system
    agent = "agent_c"
    
    # Setup key versioning
    v1 = "secret_v1_old"
    v2 = "secret_v2_new"
    
    vault.add_key("t1", agent, "v1", v1, status="active")
    vault.add_key("t1", agent, "v2", v2, status="active")
    
    body = '{"action": "rotate"}'
    ts = int(time.time())
    
    # Ambas keys deben funcionar
    sig_v1 = sign_request("t1", agent, v1, body, ts)
    sig_v2 = sign_request("t1", agent, v2, body, ts)
    
    assert engine.verify_request("t1", agent, sig_v1, ts, "POST", "/test", body, key_version="v1") is True
    
    # También deberíamos poder verificar v2 sin usar el header (O(N) fallback lookup)
    # pero como el timestamp y body es idéntico al de sig_v1, saltará la protección de Replay Attack!
    # Así que incrementamos el timestamp
    ts2 = ts + 1
    sig_v2_2 = sign_request("t1", agent, v2, body, ts2)
    assert engine.verify_request("t1", agent, sig_v2_2, ts2, "POST", "/test", body) is True
    
    # Revocar v1 (Instant Revocation sin downtime)
    vault.revoke_key("t1", agent, "v1")
    
    ts3 = ts + 2
    sig_v1_revoked = sign_request("t1", agent, v1, body, ts3)
    
    # v1 debe fallar inmediatamente
    with pytest.raises(Unauthorized, match="Invalid signature or key revoked"):
        engine.verify_request("t1", agent, sig_v1_revoked, ts3, "POST", "/test", body, key_version="v1")
        
    # v2 sigue funcionando
    ts4 = ts + 3
    sig_v2_active = sign_request("t1", agent, v2, body, ts4)
    assert engine.verify_request("t1", agent, sig_v2_active, ts4, "POST", "/test", body, key_version="v2") is True

def test_invalid_kid_rejected(iam_system):
    engine, vault = iam_system
    agent = "agent_c"
    
    # Setup key versioning
    v1 = "secret_v1_old"
    vault.add_key("t1", agent, "v1", v1, status="active")
    
    body = "{}"
    ts = int(time.time())
    sig_v1 = sign_request("t1", agent, v1, body, ts)
    
    # Debe fallar con Unknown key version
    with pytest.raises(Unauthorized, match="Unknown key version"):
        engine.verify_request("t1", agent, sig_v1, ts, "POST", "/test", body, key_version="v999")
