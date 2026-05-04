import os
import uuid
import time
import pytest
from unittest.mock import patch
from cryptography.hazmat.primitives.asymmetric import ed25519
from cryptography.hazmat.primitives import serialization

# VSOCK Protocol Imports
from kernell_sdk.runtime.firecracker.auth_protocol import (
    AuthenticatedFrame,
    ReplayAttackError,
    AuthenticationError,
    ProtocolConfigError,
    derive_key
)

try:
    from kernell_sdk.runtime.firecracker_runtime import FirecrackerRuntime
    HAS_RUNTIME = True
except ImportError:
    HAS_RUNTIME = False

# Escrow Imports
from kernell_sdk.escrow.manager import EscrowManager, EscrowState, Unauthorized, InvalidTransition, ReplayDetected, InvalidSignature

import sqlite3

# --- VSOCK Invariant Tests ---

@pytest.mark.invariant("E1")
def test_vsock_requires_secret():
    if not HAS_RUNTIME:
        pytest.skip("FirecrackerRuntime not available")
    
    os.environ.pop("FC_VSOCK_SHARED_SECRET_B64", None)
    
    with pytest.raises(ProtocolConfigError) as exc:
        rt = FirecrackerRuntime(kernel_path="/vmlinux", rootfs_path="/rootfs.ext4")
        rt._send_code_vsock("vm1", "print(1)", 10, "tenant1", "req1")
    
    assert "FC_VSOCK_SHARED_SECRET_B64" in str(exc.value)

@pytest.mark.invariant("E1")
def test_derive_key_hkdf_domain_separation():
    secret = b"x" * 32
    k_exec = derive_key(secret, "kernell.vsock.exec.v1")
    k_resp = derive_key(secret, "kernell.vsock.resp.v1")
    assert len(k_exec) == 32 and len(k_resp) == 32
    assert k_exec != k_resp


@pytest.mark.invariant("E3")
def test_replay_nonce_rejected():
    key = b"A" * 32
    frame = AuthenticatedFrame.create(b"payload", key, tenant_id="t1", request_id="r1")
    wire_bytes = frame.to_wire()
    
    # First extraction should succeed
    parsed1 = AuthenticatedFrame.from_wire(wire_bytes[4:], key)
    assert parsed1.nonce == frame.nonce
    
    # Second extraction with same bytes (same nonce & timestamp) should fail
    with pytest.raises(ReplayAttackError):
        AuthenticatedFrame.from_wire(wire_bytes[4:], key)

@pytest.mark.invariant("E2")
def test_invalid_hmac_rejected():
    key = b"B" * 32
    frame = AuthenticatedFrame.create(b"payload", key, tenant_id="t1", request_id="r1")
    frame.signature = "tampered"
    
    import json
    body = json.dumps({
        "v": frame.version, "ts": frame.timestamp, "nid": frame.nonce,
        "tid": frame.tenant_id, "rid": frame.request_id, "pay": frame.payload_b64,
        "meta": frame.meta, "sig": frame.signature,
    }, sort_keys=True, separators=(",", ":")).encode("utf-8")
    
    with pytest.raises(AuthenticationError) as exc:
        AuthenticatedFrame.from_wire(body, key)
    assert "invalid HMAC" in str(exc.value)


# --- Escrow Invariant Tests Helper ---

def generate_ed25519_keypair():
    priv = ed25519.Ed25519PrivateKey.generate()
    pub = priv.public_key()
    pub_bytes = pub.public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    return priv, pub_bytes.hex()

def sign_intent(priv_key, intent):
    import json
    msg = json.dumps(intent, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return priv_key.sign(msg).hex()

@pytest.fixture
def escrow():
    # Use in-memory DB for pure invariant testing without filesystem artifacts
    return EscrowManager(db_path=":memory:")

# --- Escrow Invariant Tests ---

@pytest.mark.invariant("S3")
def test_escrow_rejects_invalid_signature(escrow):
    priv, pub_hex = generate_ed25519_keypair()
    escrow.register_actor_key("buyer", pub_hex)
    
    with pytest.raises((InvalidSignature, Exception)):
        escrow.create_escrow(
            buyer_id="buyer", seller_id="seller", amount=100.0, contract_id="c-invalid",
            nonce="n1", signature_hex="bad_signature"
        )

@pytest.mark.invariant("S4")
def test_escrow_replay_nonce(escrow):
    priv, pub_hex = generate_ed25519_keypair()
    escrow.register_actor_key("buyer", pub_hex)
    
    intent = {
        "action": "CREATE",
        "contract_id": "c1",
        "buyer_id": "buyer",
        "seller_id": "seller",
        "arbitrator_id": None,
        "amount_kern": "100.0",
        "expected_prev_state": "NONE",
        "nonce": "n1",
    }
    sig = sign_intent(priv, intent)
    
    escrow.create_escrow(buyer_id="buyer", seller_id="seller", amount=100.0, contract_id="c1", nonce="n1", signature_hex=sig)
    
    with pytest.raises((sqlite3.IntegrityError, ReplayDetected)):
        # A replay of the same nonce/contract should fail.
        escrow.create_escrow(buyer_id="buyer", seller_id="seller", amount=100.0, contract_id="c1", nonce="n1", signature_hex=sig)

@pytest.mark.invariant("S5")
def test_invalid_state_transition(escrow):
    priv, pub_hex = generate_ed25519_keypair()
    escrow.register_actor_key("buyer", pub_hex)
    
    intent1 = {
        "action": "CREATE", "contract_id": "c2", "buyer_id": "buyer",
        "seller_id": "seller", "arbitrator_id": None, "amount_kern": "100.0",
        "expected_prev_state": "NONE", "nonce": "n1",
    }
    escrow.create_escrow(buyer_id="buyer", seller_id="seller", amount=100.0, contract_id="c2", nonce="n1", signature_hex=sign_intent(priv, intent1))
    
    with patch("kernell_sdk.escrow.manager._now", return_value=1000.0):
        # Current state is CREATED. Try to RELEASE (which requires LOCKED)
        intent2 = {
            "action": "RELEASE", "contract_id": "c2", "expected_prev_state": "LOCKED",
            "nonce": "n2", "ts": 1000.0,
        }
        sig2 = sign_intent(priv, intent2)
        with pytest.raises(InvalidTransition):
            escrow.release_funds("c2", actor_id="buyer", expected_prev_state=EscrowState.LOCKED, nonce="n2", signature_hex=sig2)

def test_hash_chain_integrity(escrow):
    priv, pub_hex = generate_ed25519_keypair()
    escrow.register_actor_key("buyer", pub_hex)
    
    intent1 = {
        "action": "CREATE", "contract_id": "c3", "buyer_id": "buyer",
        "seller_id": "seller", "arbitrator_id": None, "amount_kern": "100.0",
        "expected_prev_state": "NONE", "nonce": "n1",
    }
    escrow.create_escrow(buyer_id="buyer", seller_id="seller", amount=100.0, contract_id="c3", nonce="n1", signature_hex=sign_intent(priv, intent1))
    
    with patch("kernell_sdk.escrow.manager._now", return_value=1000.0):
        intent2 = {
            "action": "FUND", "contract_id": "c3",
            "expected_prev_state": "CREATED", "nonce": "n2", "ts": 1000.0
        }
        escrow.fund_escrow("c3", actor_id="buyer", expected_prev_state=EscrowState.CREATED, nonce="n2", signature_hex=sign_intent(priv, intent2))
        
        # Check hashes sequence
        rows = escrow._conn.execute("SELECT prev_hash, event_hash FROM events ORDER BY id ASC").fetchall()
        assert len(rows) == 2
        assert rows[1][0] == rows[0][1], "Hash chain broken: subsequent event prev_hash does not match preceding event_hash"
