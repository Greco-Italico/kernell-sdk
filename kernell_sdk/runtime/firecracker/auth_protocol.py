from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import struct
import time
import uuid
from dataclasses import dataclass
from typing import Optional, Tuple

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.hkdf import HKDF


_PROTOCOL_VERSION = 1
_TIMESTAMP_TOLERANCE_SEC = 30
_MAX_FRAME_BYTES = 250_000
_MAX_PAYLOAD_BYTES = 100_000


class AuthenticationError(Exception):
    pass


class ReplayAttackError(AuthenticationError):
    pass


class PayloadTooLargeError(ValueError):
    pass


class ProtocolConfigError(RuntimeError):
    pass


class _NonceStore:
    _MAX_SIZE = 10_000

    def __init__(self, window_sec: int = _TIMESTAMP_TOLERANCE_SEC * 2) -> None:
        self._seen: dict[str, float] = {}
        self._window = window_sec

    def check_and_register(self, nonce: str, timestamp: float) -> None:
        now = time.time()
        cutoff = now - self._window
        expired = [n for n, ts in self._seen.items() if ts < cutoff]
        for n in expired:
            del self._seen[n]

        if nonce in self._seen:
            raise ReplayAttackError(f"Nonce replay detected: {nonce[:8]}…")

        if len(self._seen) >= self._MAX_SIZE:
            oldest = sorted(self._seen.items(), key=lambda kv: kv[1])[: self._MAX_SIZE // 10]
            for n, _ in oldest:
                del self._seen[n]

        self._seen[nonce] = timestamp


_nonce_store = _NonceStore()


def load_shared_secret() -> bytes:
    """
    Alpha contract (fail-close): provide a per-environment shared secret via env.

    - FC_VSOCK_SHARED_SECRET_B64: base64-encoded secret bytes (>=32 bytes)
    """
    raw = os.getenv("FC_VSOCK_SHARED_SECRET_B64", "").strip()
    if not raw:
        raise ProtocolConfigError("FC_VSOCK_SHARED_SECRET_B64 is required (fail-close)")
    try:
        secret = base64.b64decode(raw)
    except Exception as exc:  # noqa: BLE001
        raise ProtocolConfigError("FC_VSOCK_SHARED_SECRET_B64 is not valid base64") from exc
    if len(secret) < 32:
        raise ProtocolConfigError("FC_VSOCK_SHARED_SECRET_B64 must decode to >=32 bytes")
    return secret


def derive_key(shared_secret: bytes, context: str) -> bytes:
    """
    Domain-separated subkeys from the VSOCK shared secret (C-13 / HKDF-SHA256).

    Salt is derived from the shared secret (not empty-string HKDF), for auditor
    expectations while remaining deterministic for a given deployment secret.

    Both host and guest must use the same derivation (SDK version alignment).
    """
    salt = hashlib.sha256(shared_secret + b"kernell.vsock.hkdf.salt.v1").digest()[:16]
    return HKDF(
        algorithm=hashes.SHA256(),
        length=32,
        salt=salt,
        info=context.encode("utf-8"),
    ).derive(shared_secret)


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


@dataclass
class AuthenticatedFrame:
    version: int
    timestamp: float
    nonce: str
    tenant_id: str
    request_id: str
    payload_b64: str
    meta: dict
    signature: str

    @classmethod
    def create(
        cls,
        payload: bytes,
        key: bytes,
        *,
        tenant_id: str,
        request_id: str,
        meta: Optional[dict] = None,
    ) -> "AuthenticatedFrame":
        if len(payload) > _MAX_PAYLOAD_BYTES:
            raise PayloadTooLargeError(f"payload {len(payload):,} > {_MAX_PAYLOAD_BYTES:,} bytes")
        frame = cls(
            version=_PROTOCOL_VERSION,
            timestamp=time.time(),
            nonce=uuid.uuid4().hex,
            tenant_id=tenant_id,
            request_id=request_id,
            payload_b64=base64.b64encode(payload).decode("ascii"),
            meta=meta or {},
            signature="",
        )
        frame.signature = _compute_hmac(frame, key)
        return frame

    def to_wire(self) -> bytes:
        body = json.dumps(
            {
                "v": self.version,
                "ts": self.timestamp,
                "nid": self.nonce,
                "tid": self.tenant_id,
                "rid": self.request_id,
                "pay": self.payload_b64,
                "meta": self.meta,
                "sig": self.signature,
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")

        if len(body) > _MAX_FRAME_BYTES:
            raise PayloadTooLargeError(f"frame {len(body):,} > {_MAX_FRAME_BYTES:,} bytes")
        return struct.pack(">I", len(body)) + body

    @classmethod
    def from_wire(cls, body: bytes, key: bytes) -> "AuthenticatedFrame":
        if len(body) > _MAX_FRAME_BYTES:
            raise PayloadTooLargeError(f"frame {len(body):,} > {_MAX_FRAME_BYTES:,} bytes")
        try:
            doc = json.loads(body.decode("utf-8"))
            frame = cls(
                version=int(doc["v"]),
                timestamp=float(doc["ts"]),
                nonce=str(doc["nid"]),
                tenant_id=str(doc["tid"]),
                request_id=str(doc["rid"]),
                payload_b64=str(doc["pay"]),
                meta=dict(doc.get("meta") or {}),
                signature=str(doc["sig"]),
            )
        except Exception as exc:  # noqa: BLE001
            raise AuthenticationError(f"malformed frame: {exc}") from exc

        if frame.version != _PROTOCOL_VERSION:
            raise AuthenticationError(f"unsupported protocol version: {frame.version}")

        now = time.time()
        if abs(now - frame.timestamp) > _TIMESTAMP_TOLERANCE_SEC:
            raise ReplayAttackError("timestamp outside anti-replay window")

        _nonce_store.check_and_register(frame.nonce, frame.timestamp)

        expected = _compute_hmac(frame, key)
        if not hmac.compare_digest(expected, frame.signature):
            raise AuthenticationError("invalid HMAC")

        payload = base64.b64decode(frame.payload_b64)
        if len(payload) > _MAX_PAYLOAD_BYTES:
            raise PayloadTooLargeError(f"payload {len(payload):,} > {_MAX_PAYLOAD_BYTES:,} bytes")
        expected_hash = frame.meta.get("payload_sha256")
        if expected_hash is not None and expected_hash != sha256_hex(payload):
            raise AuthenticationError("payload hash mismatch")
        return frame

    def payload_bytes(self) -> bytes:
        return base64.b64decode(self.payload_b64)


def _compute_hmac(frame: AuthenticatedFrame, key: bytes) -> str:
    msg = (
        f"{frame.version}|"
        f"{frame.timestamp:.6f}|"
        f"{frame.nonce}|"
        f"{frame.tenant_id}|"
        f"{frame.request_id}|"
        f"{frame.payload_b64}|"
        f"{json.dumps(frame.meta, sort_keys=True, separators=(',', ':'))}"
    ).encode("utf-8")
    return hmac.new(key, msg, hashlib.sha256).hexdigest()


def recv_len_prefixed(sock, max_len: int = _MAX_FRAME_BYTES) -> bytes:
    raw_len = _recv_exact(sock, 4)
    n = struct.unpack(">I", raw_len)[0]
    if n > max_len:
        raise PayloadTooLargeError(f"declared frame length {n:,} > {max_len:,}")
    return _recv_exact(sock, n)


def _recv_exact(sock, n: int) -> bytes:
    buf = bytearray()
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise ConnectionError("socket closed early")
        buf.extend(chunk)
    return bytes(buf)

