"""A2A canonical UTF-8 bytes + Ed25519 + nonce (Sprint 2.5)."""
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from kernell_sdk.agent import _a2a_canonical_signing_bytes, A2AMessage
from kernell_sdk.identity import sign_message_bytes, verify_signature_bytes
from kernell_sdk.risk_engine import DataSensitivity


@pytest.mark.invariant("E2")
def test_a2a_sign_verify_roundtrip():
    priv = Ed25519PrivateKey.generate()
    pub = priv.public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    ).hex()
    priv_hex = priv.private_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PrivateFormat.Raw,
        encryption_algorithm=serialization.NoEncryption(),
    ).hex()

    canonical = _a2a_canonical_signing_bytes(
        sender_agent_id="agent-uuid-1",
        tenant_id="tenant-a",
        target_id="agent-uuid-2",
        payload="hello",
        sensitivity_value=DataSensitivity.PUBLIC.value,
        timestamp_ms=1700000000000,
        nonce="n1",
    )
    sig_hex = sign_message_bytes(canonical, priv_hex)
    assert verify_signature_bytes(canonical, sig_hex, pub)


@pytest.mark.invariant("E2")
def test_a2a_tenant_binding_changes_signature():
    priv = Ed25519PrivateKey.generate()
    priv_hex = priv.private_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PrivateFormat.Raw,
        encryption_algorithm=serialization.NoEncryption(),
    ).hex()
    pub = priv.public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    ).hex()

    c1 = _a2a_canonical_signing_bytes(
        "a1", "tenant-x", "a2", "p", DataSensitivity.PUBLIC.value, 1, "n0"
    )
    c2 = _a2a_canonical_signing_bytes(
        "a1", "tenant-y", "a2", "p", DataSensitivity.PUBLIC.value, 1, "n0"
    )
    sig = sign_message_bytes(c1, priv_hex)
    assert c1 != c2
    assert not verify_signature_bytes(c2, sig, pub)


def test_a2amessage_model_fields():
    msg = A2AMessage(
        sender_id="sid",
        target_id="tid",
        payload="x",
        sensitivity=DataSensitivity.PUBLIC,
        signature=b"\x00" * 64,
        timestamp_ms=1,
        nonce="abc",
    )
    assert msg.tenant_id == "default"


@pytest.mark.invariant("E3")
def test_utf8_contract_ensure_ascii_false():
    """Unicode payload must round-trip through canonical bytes."""
    priv = Ed25519PrivateKey.generate()
    priv_hex = priv.private_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PrivateFormat.Raw,
        encryption_algorithm=serialization.NoEncryption(),
    ).hex()
    pub = priv.public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    ).hex()
    canonical = _a2a_canonical_signing_bytes(
        "a1", "t", "a2", "café-日本", DataSensitivity.PUBLIC.value, 42, "n2"
    )
    sig = sign_message_bytes(canonical, priv_hex)
    assert verify_signature_bytes(canonical, sig, pub)
