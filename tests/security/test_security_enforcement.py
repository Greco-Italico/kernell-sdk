import base64
import inspect
import types

import pytest

from kernell_sdk.escrow.manager import EscrowManager, EscrowState
from kernell_sdk.runtime.firecracker.auth_protocol import AuthenticatedFrame
from kernell_sdk.runtime.firecracker_runtime import FirecrackerRuntime
from kernell_sdk.runtime.models import ExecutionRequest
from kernell_sdk.runtime.firecracker import server as firecracker_server


class _FakeSocket:
    def __init__(self, *args, **kwargs):
        self.closed = False
    def settimeout(self, _t):
        return None
    def connect(self, _addr):
        return None
    def sendall(self, _b):
        return None
    def shutdown(self, _how):
        return None
    def close(self):
        self.closed = True


@pytest.mark.invariant("E5")
def test_response_context_mismatch_rejected(monkeypatch):
    secret = base64.b64encode(b"K" * 32).decode("ascii")
    monkeypatch.setenv("FC_VSOCK_SHARED_SECRET_B64", secret)
    rt = FirecrackerRuntime(kernel_path="/vmlinux", rootfs_path="/rootfs.ext4")

    monkeypatch.setattr("kernell_sdk.runtime.firecracker_runtime.socket.socket", _FakeSocket)
    monkeypatch.setattr("kernell_sdk.runtime.firecracker_runtime.prom.VSOCK_CONNECT_LATENCY.observe", lambda *_: None)
    monkeypatch.setattr(
        "kernell_sdk.runtime.firecracker_runtime.recv_len_prefixed",
        lambda _sock: b'{"v":1,"ts":1,"nid":"x","tid":"attacker","rid":"wrong","pay":"","meta":{},"sig":"x"}',
    )
    monkeypatch.setattr(
        "kernell_sdk.runtime.firecracker_runtime.AuthenticatedFrame.from_wire",
        lambda _b, _k: AuthenticatedFrame(
            version=1,
            timestamp=1.0,
            nonce="n",
            tenant_id="attacker",
            request_id="wrong",
            payload_b64="",
            meta={},
            signature="s",
        ),
    )

    with pytest.raises(Exception):
        rt._send_code_vsock("vm1", "print(1)", timeout=1, tenant_id="tenant-ok", request_id="req-ok")


@pytest.mark.invariant("L4")
def test_escrow_events_append_only_enforced():
    m = EscrowManager(db_path=":memory:")
    # Insert a dummy row so the trigger has something to fire on.
    m._conn.execute(
        "INSERT INTO contracts(contract_id,buyer_id,seller_id,arbitrator_id,amount_micro_kern,state,created_at,timeout_ts) VALUES(?,?,?,?,?,?,?,?)",
        ("c-trigger", "b", "s", None, 1000000, "CREATED", 1.0, 99999.0),
    )
    m._conn.execute(
        "INSERT INTO events(contract_id,ts,actor_id,action,expected_prev_state,nonce,event_json,prev_hash,event_hash,signature_hex) VALUES(?,?,?,?,?,?,?,?,?,?)",
        ("c-trigger", 1.0, "b", "CREATE", "NONE", "n-trg", '{}', '0'*64, 'a'*64, 'sig'),
    )
    # Direct mutation must fail due to BEFORE UPDATE trigger.
    with pytest.raises(Exception):
        m._conn.execute(
            "UPDATE events SET actor_id='x' WHERE contract_id='c-trigger'"
        )


@pytest.mark.invariant("T2")
def test_every_escrow_transition_emits_event():
    from cryptography.hazmat.primitives.asymmetric import ed25519
    from cryptography.hazmat.primitives import serialization
    import json
    import uuid

    def sign(priv, intent):
        msg = json.dumps(intent, sort_keys=True, separators=(",", ":")).encode("utf-8")
        return priv.sign(msg).hex()

    def gen():
        priv = ed25519.Ed25519PrivateKey.generate()
        pub = priv.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw).hex()
        return priv, pub

    m = EscrowManager(db_path=":memory:")
    b_priv, b_pub = gen()
    s_priv, s_pub = gen()
    a_priv, a_pub = gen()
    m.register_actor_key("buyer", b_pub)
    m.register_actor_key("seller", s_pub)
    m.register_actor_key("arb", a_pub)

    cid = f"c-{uuid.uuid4().hex[:8]}"
    create_intent = {
        "action": "CREATE",
        "contract_id": cid,
        "buyer_id": "buyer",
        "seller_id": "seller",
        "arbitrator_id": "arb",
        "amount_kern": "10.0",
        "expected_prev_state": "NONE",
        "nonce": "n1",
    }
    m.create_escrow(
        buyer_id="buyer",
        seller_id="seller",
        amount=10.0,
        contract_id=cid,
        arbitrator_id="arb",
        nonce="n1",
        signature_hex=sign(b_priv, create_intent),
    )

    from unittest.mock import patch
    with patch("kernell_sdk.escrow.manager._now", return_value=1.0):
        fund_intent = {"action": "FUND", "contract_id": cid, "expected_prev_state": "CREATED", "nonce": "n2", "ts": 1.0}
        m.fund_escrow(cid, actor_id="buyer", expected_prev_state=EscrowState.CREATED, nonce="n2", signature_hex=sign(b_priv, fund_intent))
        lock_intent = {"action": "LOCK", "contract_id": cid, "expected_prev_state": "FUNDED", "nonce": "n3", "ts": 1.0}
        m.lock_escrow(cid, actor_id="buyer", expected_prev_state=EscrowState.FUNDED, nonce="n3", signature_hex=sign(b_priv, lock_intent))
        release_intent = {"action": "RELEASE", "contract_id": cid, "expected_prev_state": "LOCKED", "nonce": "n4", "ts": 1.0}
        m.release_funds(cid, actor_id="buyer", expected_prev_state=EscrowState.LOCKED, nonce="n4", signature_hex=sign(b_priv, release_intent))

    events = m._conn.execute("SELECT COUNT(*) FROM events WHERE contract_id=?", (cid,)).fetchone()[0]
    assert events == 4


@pytest.mark.invariant("T2")
@pytest.mark.invariant("E6")
def test_every_execution_emits_audit_event(monkeypatch):
    secret = base64.b64encode(b"K" * 32).decode("ascii")
    monkeypatch.setenv("FC_VSOCK_SHARED_SECRET_B64", secret)
    rt = FirecrackerRuntime(kernel_path="/vmlinux", rootfs_path="/rootfs.ext4")

    class _Account:
        class _Plan:
            name = "free"
        plan = _Plan()

    rt.billing_manager.get_account = lambda _tenant: _Account()
    rt.billing_manager.reserve = lambda *_a, **_k: True
    rt.billing_manager.settle = lambda *_a, **_k: None
    tenant_state = types.SimpleNamespace()
    rt.tenant_manager.get = lambda _t: tenant_state
    rt.tenant_manager.allow_request = lambda _s: True
    rt.tenant_manager.release_request = lambda _s: None
    vm = types.SimpleNamespace(vm_id="vm1", socket_path="/tmp/fc.sock", process=None)
    rt.pool.get_with_flag = lambda: (vm, False)
    rt.manager.cleanup_vm = lambda *_a, **_k: None
    rt._send_code_vsock = lambda *_a, **_k: "ok"

    observed = {"count": 0}
    def _log(**_kwargs):
        observed["count"] += 1
    rt.telemetry.log_audit_event = _log
    rt.telemetry.trace = lambda *_a, **_k: None

    req = ExecutionRequest(code="x=1", timeout=1, memory_limit_mb=64, tenant_id="t1", request_id="r1")
    res = rt.execute(req)
    assert res.exit_code == 0
    assert observed["count"] == 1


@pytest.mark.invariant("T1")
def test_control_plane_requires_authorization_header():
    class _Req:
        headers = {}
    with pytest.raises(Exception):
        firecracker_server._require_control_token(_Req())


@pytest.mark.invariant("T3")
def test_no_silent_pass_in_critical_security_modules():
    modules = [
        firecracker_server,
        __import__("kernell_sdk.runtime.firecracker.auth_protocol", fromlist=["*"]),
        __import__("kernell_sdk.runtime.firecracker_runtime", fromlist=["*"]),
        __import__("kernell_sdk.escrow.manager", fromlist=["*"]),
    ]
    for mod in modules:
        src = inspect.getsource(mod)
        assert "except Exception:\n        pass" not in src

