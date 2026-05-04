"""
firecracker/runner.py — Kernell OS SDK
Fix #6: Autenticación del canal VSOCK con HMAC-SHA256 + nonce + timestamp.

Formato del frame autenticado:
  {
    "v":   1,                    // versión del protocolo
    "ts":  1713571200,           // timestamp Unix (valido ±30s)
    "nid": "uuid4-hex",          // nonce único (evita replay attacks)
    "pay": "<base64 del código>",// payload cifrado o en claro
    "sig": "<hmac-sha256-hex>"   // HMAC sobre (v|ts|nid|pay)
  }

El secreto compartido se genera en bootstrap del microVM y se pasa
a través del mmds (Firecracker metadata service), nunca por el socket.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import socket
import struct
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from ..runtime.sandbox_validator import SandboxViolation, validate_code

# ---------------------------------------------------------------------------
# Constantes
# ---------------------------------------------------------------------------

_PROTOCOL_VERSION: int = 1
_TIMESTAMP_TOLERANCE_SEC: int = 30      # ventana anti-replay temporal
_MAX_FRAME_BYTES: int = 200_000         # 200 KB máximo por frame
_MAX_PAYLOAD_BYTES: int = 100_000       # igual que MAX_CODE_BYTES del sandbox
_HMAC_DIGEST: str = "sha256"
_SOCKET_TIMEOUT_SEC: float = 10.0


# ---------------------------------------------------------------------------
# Excepciones
# ---------------------------------------------------------------------------

class AuthenticationError(Exception):
    """HMAC inválido o frame malformado."""


class ReplayAttackError(AuthenticationError):
    """Nonce ya visto o timestamp fuera de ventana."""


class PayloadTooLargeError(ValueError):
    """Payload excede el límite permitido."""


# ---------------------------------------------------------------------------
# Nonce store (en memoria — en producción usar Redis con TTL)
# ---------------------------------------------------------------------------

class _NonceStore:
    """
    Almacena nonces vistos recientemente para prevenir replay attacks.

    Garantías de memoria:
      - Limpieza automática de entradas expiradas en cada inserción.
      - Tamaño máximo acotado: si se supera _MAX_SIZE, se expulsan las entradas
        más antiguas (LRU), asumiendo que un volumen tan alto de nonces únicos
        en la ventana de tiempo indica un ataque de flooding.
    """

    _MAX_SIZE: int = 10_000  # máximo de nonces en memoria simultáneamente

    def __init__(self, window_sec: int = _TIMESTAMP_TOLERANCE_SEC * 2) -> None:
        self._seen: dict[str, float] = {}   # nonce → timestamp de llegada
        self._window = window_sec

    def check_and_register(self, nonce: str, timestamp: float) -> None:
        """
        Raises:
            ReplayAttackError: si el nonce ya fue visto.
        """
        now = time.time()
        self._evict_expired(now)

        if nonce in self._seen:
            raise ReplayAttackError(f"Nonce reutilizado detectado: {nonce[:8]}…")

        # Si el cache está lleno tras eviction, expulsar los más antiguos
        if len(self._seen) >= self._MAX_SIZE:
            self._evict_oldest(count=self._MAX_SIZE // 10)  # liberar 10%

        self._seen[nonce] = timestamp

    def _evict_expired(self, now: float) -> None:
        cutoff = now - self._window
        expired = [n for n, ts in self._seen.items() if ts < cutoff]
        for n in expired:
            del self._seen[n]

    def _evict_oldest(self, count: int) -> None:
        """Expulsa los `count` nonces más antiguos por timestamp."""
        oldest = sorted(self._seen.items(), key=lambda kv: kv[1])[:count]
        for nonce, _ in oldest:
            del self._seen[nonce]

    def __len__(self) -> int:
        return len(self._seen)


_nonce_store = _NonceStore()


# ---------------------------------------------------------------------------
# Frame autenticado
# ---------------------------------------------------------------------------

@dataclass
class AuthenticatedFrame:
    version: int
    timestamp: float
    nonce: str
    payload: bytes      # código Python en bytes crudos
    signature: str      # HMAC-SHA256 en hex

    @classmethod
    def create(cls, payload: bytes, secret: bytes) -> "AuthenticatedFrame":
        """Crea un frame firmado listo para enviar."""
        if len(payload) > _MAX_PAYLOAD_BYTES:
            raise PayloadTooLargeError(
                f"Payload {len(payload):,} bytes > máximo {_MAX_PAYLOAD_BYTES:,}"
            )

        frame = cls(
            version=_PROTOCOL_VERSION,
            timestamp=time.time(),
            nonce=uuid.uuid4().hex,
            payload=payload,
            signature="",  # se calculará abajo
        )
        frame.signature = _compute_hmac(frame, secret)
        return frame

    def to_wire(self) -> bytes:
        """Serializa el frame para envío por socket."""
        doc = {
            "v":   self.version,
            "ts":  self.timestamp,
            "nid": self.nonce,
            "pay": base64.b64encode(self.payload).decode(),
            "sig": self.signature,
        }
        body = json.dumps(doc).encode("utf-8")
        # Prefijo de 4 bytes con longitud (big-endian) — framing TCP
        return struct.pack(">I", len(body)) + body

    @classmethod
    def from_wire(cls, data: bytes, secret: bytes) -> "AuthenticatedFrame":
        """
        Deserializa y verifica un frame recibido.

        Raises:
            AuthenticationError, ReplayAttackError, PayloadTooLargeError
        """
        if len(data) > _MAX_FRAME_BYTES:
            raise PayloadTooLargeError(f"Frame demasiado grande: {len(data):,} bytes")

        try:
            doc = json.loads(data.decode("utf-8"))
            frame = cls(
                version=int(doc["v"]),
                timestamp=float(doc["ts"]),
                nonce=str(doc["nid"]),
                payload=base64.b64decode(doc["pay"]),
                signature=str(doc["sig"]),
            )
        except (KeyError, ValueError, json.JSONDecodeError) as exc:
            raise AuthenticationError(f"Frame malformado: {exc}") from exc

        # 1. Verificar versión
        if frame.version != _PROTOCOL_VERSION:
            raise AuthenticationError(f"Versión de protocolo desconocida: {frame.version}")

        # 2. Verificar timestamp (ventana anti-replay)
        now = time.time()
        if abs(now - frame.timestamp) > _TIMESTAMP_TOLERANCE_SEC:
            raise ReplayAttackError(
                f"Timestamp fuera de ventana: delta={abs(now - frame.timestamp):.1f}s"
            )

        # 3. Verificar nonce (anti-replay exacto)
        _nonce_store.check_and_register(frame.nonce, frame.timestamp)

        # 4. Verificar HMAC (comparación en tiempo constante)
        expected = _compute_hmac(frame, secret)
        if not hmac.compare_digest(expected, frame.signature):
            raise AuthenticationError("HMAC inválido — payload rechazado")

        # 5. Verificar tamaño del payload
        if len(frame.payload) > _MAX_PAYLOAD_BYTES:
            raise PayloadTooLargeError(
                f"Payload {len(frame.payload):,} bytes > máximo {_MAX_PAYLOAD_BYTES:,}"
            )

        return frame


# ---------------------------------------------------------------------------
# Runner Firecracker
# ---------------------------------------------------------------------------

class FirecrackerRunner:
    """
    Envía código Python a una microVM Firecracker vía VSOCK autenticado.

    El secreto compartido debe ser:
      1. Generado aleatoriamente en bootstrap de la microVM.
      2. Entregado al host a través del MMDS (metadata service).
      3. NUNCA transmitido por el socket VSOCK.

    Uso:
        secret = load_mmds_secret()   # tu función de bootstrap
        runner = FirecrackerRunner(vsock_path="/run/fc.sock", shared_secret=secret)
        result = runner.run(user_code)
    """

    def __init__(
        self,
        vsock_path: str,
        shared_secret: bytes,
        timeout: float = _SOCKET_TIMEOUT_SEC,
    ) -> None:
        if len(shared_secret) < 32:
            raise ValueError("shared_secret debe tener al menos 32 bytes")

        self._vsock_path = vsock_path
        self._secret = shared_secret
        self._timeout = timeout

    def run(self, source: str) -> str:
        """
        Valida, firma y envía `source` a la microVM.

        Returns:
            Output de la ejecución como string.

        Raises:
            SandboxViolation: código no pasa validación AST.
            AuthenticationError: respuesta inválida de la microVM.
        """
        # 1. Validación AST local antes de enviar
        validation = validate_code(source)
        if not validation.valid:
            raise SandboxViolation(validation)

        payload = source.encode("utf-8")
        frame = AuthenticatedFrame.create(payload, self._secret)
        wire = frame.to_wire()

        # 2. Enviar por VSOCK
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
            sock.settimeout(self._timeout)
            sock.connect(self._vsock_path)
            sock.sendall(wire)

            # 3. Leer respuesta (4 bytes de longitud + body)
            raw_len = _recv_exact(sock, 4)
            resp_len = struct.unpack(">I", raw_len)[0]

            if resp_len > _MAX_FRAME_BYTES:
                raise PayloadTooLargeError(
                    f"Respuesta demasiado grande: {resp_len:,} bytes"
                )

            resp_body = _recv_exact(sock, resp_len)

        # 4. Verificar respuesta autenticada de la microVM
        resp_frame = AuthenticatedFrame.from_wire(resp_body, self._secret)
        return resp_frame.payload.decode("utf-8", errors="replace")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _compute_hmac(frame: AuthenticatedFrame, secret: bytes) -> str:
    """Calcula HMAC-SHA256 sobre los campos relevantes del frame."""
    # Concatenar campos canonicamente (evitar ambigüedad)
    message = (
        f"{frame.version}|"
        f"{frame.timestamp:.6f}|"
        f"{frame.nonce}|"
        f"{base64.b64encode(frame.payload).decode()}"
    ).encode("utf-8")

    return hmac.new(secret, message, digestmod=hashlib.sha256).hexdigest()


def _recv_exact(sock: socket.socket, n: int) -> bytes:
    """Lee exactamente n bytes del socket."""
    buf = bytearray()
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise ConnectionError("Socket cerrado prematuramente")
        buf.extend(chunk)
    return bytes(buf)


# ---------------------------------------------------------------------------
# Utilidad: generar secreto compartido
# ---------------------------------------------------------------------------

def generate_shared_secret() -> bytes:
    """
    Genera un secreto compartido criptográficamente seguro de 32 bytes.
    Llamar en bootstrap de la microVM, almacenar vía MMDS.
    """
    return os.urandom(32)
