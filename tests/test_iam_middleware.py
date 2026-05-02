import json
import time
import hmac
import hashlib
import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from kernell_os_sdk.security.iam import (
    VaultBackend,
    NamespacedVault,
    IAMPolicyEngine,
)
from kernell_os_sdk.security.middleware import IAMSecurityMiddleware

def sign_request(tenant_id: str, agent_id: str, secret: str, body: str, timestamp: int, method: str = "POST", path: str = "/test") -> str:
    return hmac.new(
        secret.encode('utf-8'),
        f"{tenant_id}.{timestamp}.{method}.{path}.{body}".encode('utf-8'),
        hashlib.sha256
    ).hexdigest()

@pytest.fixture
def middleware_system():
    # Setup IAM Engine
    backend = VaultBackend()
    vault = NamespacedVault(backend)
    engine = IAMPolicyEngine(vault)
    
    # Setup test agents
    vault.put_secret("t1", "agent_admin", "signing_key", "secret_ADMIN")
    vault.put_secret("t1", "agent_user", "signing_key", "secret_USER")
    
    # Grant policies
    engine.grant_policy("t1", "agent_admin", ["execute:escrow.release", "read:escrow.release"])
    engine.grant_policy("t1", "agent_user", ["execute:some.other.action"])
    
    # Setup Dummy FastAPI app
    app = FastAPI()
    app.add_middleware(IAMSecurityMiddleware, iam_engine=engine, exempt_paths=["/health"])
    
    @app.post("/escrow/release")
    async def escrow_release(req: Request):
        return {"status": "released"}
        
    @app.get("/escrow/release")
    async def get_escrow_release(req: Request):
        return {"status": "info"}
        
    @app.get("/health")
    async def health():
        return {"status": "ok"}
        
    client = TestClient(app)
    
    return client, engine

def test_unprotected_endpoint_fails(middleware_system):
    client, engine = middleware_system
    
    # 1. Intento sin headers en absoluto
    res = client.post("/escrow/release", json={})
    assert res.status_code == 401
    assert "Missing auth headers" in res.text
    
    # 2. Intento con headers pero sin firmar (faltan o están mal)
    res2 = client.post(
        "/escrow/release", 
        json={},
        headers={"X-Tenant-Id": "t1", "X-Agent-Id": "agent_admin", "X-Timestamp": str(int(time.time()))}
    )
    assert res2.status_code == 401
    
    # El endpoint health está exento, debería pasar 200
    res_health = client.get("/health")
    assert res_health.status_code == 200

def test_path_trick_bypass(middleware_system):
    client, engine = middleware_system
    
    body = "{}"
    ts = int(time.time())
    
    # Creamos un payload válido para /health, y tratamos de colarlo con path trick a /escrow/release
    # Agent admin tiene permiso para execute:escrow.release, pero intentaremos con agent_user que NO tiene permiso,
    # simulando que trata de bypassear con algo que parece /health.
    
    # El path es /health/../escrow/release que urllib normaliza a /escrow/release.
    # Así que el middleware lo evaluará como execute:escrow.release y lo denegará.
    sig = sign_request("t1", "agent_user", "secret_USER", body, ts, method="POST", path="/escrow/release")
    headers = {
        "X-Tenant-Id": "t1",
        "X-Agent-Id": "agent_user",
        "X-Signature": sig,
        "X-Timestamp": str(ts)
    }
    
    res = client.post("/health/../escrow/release", content=body, headers=headers)
    
    # Dependiendo de si testclient normaliza o el servidor, 
    # El status code debería ser 403 (Denegado por política IAM) o 404 (si FastAPI no mapea el path trick)
    assert res.status_code in [403, 404]
    
def test_method_confusion(middleware_system):
    client, engine = middleware_system
    
    body = ""
    ts = int(time.time())
    
    # El agent_user intentará usar un GET a /escrow/release
    # La acción resuelta será read:escrow.release. Como agent_user NO la tiene, fallará 403.
    
    sig = sign_request("t1", "agent_user", "secret_USER", body, ts, method="GET", path="/escrow/release")
    headers = {
        "X-Tenant-Id": "t1",
        "X-Agent-Id": "agent_user",
        "X-Signature": sig,
        "X-Timestamp": str(ts)
    }
    
    res = client.get("/escrow/release", headers=headers)
    assert res.status_code == 403
    assert "IAM Policy Deny" in res.text
    
    # Verifiquemos que agent_admin SI puede porque tiene read:escrow.release
    ts2 = int(time.time())
    sig2 = sign_request("t1", "agent_admin", "secret_ADMIN", body, ts2, method="GET", path="/escrow/release")
    headers2 = {
        "X-Tenant-Id": "t1",
        "X-Agent-Id": "agent_admin",
        "X-Signature": sig2,
        "X-Timestamp": str(ts2)
    }
    res2 = client.get("/escrow/release", headers=headers2)
    assert res2.status_code == 200

def test_happy_path_execution(middleware_system):
    client, engine = middleware_system
    
    body = '{"contract":"123"}'
    ts = int(time.time())
    
    sig = sign_request("t1", "agent_admin", "secret_ADMIN", body, ts, method="POST", path="/escrow/release")
    headers = {
        "X-Tenant-Id": "t1",
        "X-Agent-Id": "agent_admin",
        "X-Signature": sig,
        "X-Timestamp": str(ts),
        "Content-Type": "application/json"
    }
    
    res = client.post("/escrow/release", content=body, headers=headers)
    assert res.status_code == 200
    assert res.json() == {"status": "released"}
