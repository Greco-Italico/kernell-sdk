import base64
import json
import os
import struct

import pytest
from hypothesis import given, settings, strategies as st

from kernell_sdk.runtime.firecracker.auth_protocol import (
    AuthenticatedFrame,
    AuthenticationError,
    PayloadTooLargeError,
    ReplayAttackError,
    recv_len_prefixed,
)


def _mutate_one_byte(b: bytes) -> bytes:
    if not b:
        return b
    # flip one bit in the middle
    i = len(b) // 2
    return b[:i] + bytes([b[i] ^ 0x01]) + b[i + 1 :]


@given(
    payload=st.binary(min_size=0, max_size=512),
    tenant_id=st.text(alphabet=st.characters(whitelist_categories=('Lu', 'Ll', 'Nd')), min_size=1, max_size=16),
    request_id=st.text(alphabet=st.characters(whitelist_categories=('Lu', 'Ll', 'Nd')), min_size=1, max_size=16),
)
@pytest.mark.invariant("E2")
@pytest.mark.slow
@settings(max_examples=200, deadline=None)
def test_vsock_tamper_rejected(payload, tenant_id, request_id):
    key = b"K" * 32
    frame = AuthenticatedFrame.create(payload, key, tenant_id=tenant_id, request_id=request_id)
    wire = frame.to_wire()
    body = wire[4:]

    tampered = _mutate_one_byte(body)
    # Could fail as malformed JSON or invalid HMAC, both are acceptable.
    with pytest.raises((AuthenticationError, PayloadTooLargeError, UnicodeDecodeError, json.JSONDecodeError, UnicodeEncodeError)):
        AuthenticatedFrame.from_wire(tampered, key)


@given(payload=st.binary(min_size=0, max_size=512))
@pytest.mark.invariant("E7")
@pytest.mark.slow
@settings(max_examples=200, deadline=None)
def test_vsock_len_prefix_mismatch_causes_failure(payload):
    key = b"K" * 32
    frame = AuthenticatedFrame.create(payload, key, tenant_id="t1", request_id="r1")
    wire = frame.to_wire()
    body = wire[4:]
    class FakeSock:
        def __init__(self, chunks):
            self._chunks = list(chunks)
        def recv(self, _n):
            return self._chunks.pop(0) if self._chunks else b""

    # Declare an invalid larger length than actual; recv_len_prefixed must fail.
    declared = struct.pack(">I", len(body) + 10)
    fake = FakeSock([declared, body])
    with pytest.raises(ConnectionError):
        recv_len_prefixed(fake)


@pytest.mark.invariant("E3")
def test_vsock_replay_is_rejected_via_nonce_store():
    key = b"K" * 32
    frame = AuthenticatedFrame.create(b"hello", key, tenant_id="t1", request_id="r1")
    body = frame.to_wire()[4:]
    AuthenticatedFrame.from_wire(body, key)
    with pytest.raises(ReplayAttackError):
        AuthenticatedFrame.from_wire(body, key)


@pytest.mark.invariant("E3")
def test_vsock_rejects_old_timestamp():
    key = b"K" * 32
    frame = AuthenticatedFrame.create(b"payload", key, tenant_id="t1", request_id="r1")
    body = json.loads(frame.to_wire()[4:].decode("utf-8"))
    body["ts"] = 0  # stale timestamp
    stale = json.dumps(body, sort_keys=True, separators=(",", ":")).encode("utf-8")
    with pytest.raises(ReplayAttackError):
        AuthenticatedFrame.from_wire(stale, key)


@pytest.mark.invariant("E4")
def test_payload_hash_mismatch_rejected():
    key = b"K" * 32
    frame = AuthenticatedFrame.create(
        b"payload",
        key,
        tenant_id="t1",
        request_id="r1",
        meta={"payload_sha256": "0" * 64},
    )
    body = frame.to_wire()[4:]
    with pytest.raises(AuthenticationError):
        AuthenticatedFrame.from_wire(body, key)

