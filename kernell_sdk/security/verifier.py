import json
import base64
import time
from .capabilities import CapabilityToken
from .kms.base import BaseKMS

class CapabilityVerifier:
    def __init__(self, kms: BaseKMS, key_id: str):
        self.kms = kms
        self.key_id = key_id
        self.used_nonces = set()

    def verify(self, token: CapabilityToken) -> bool:
        payload = self._serialize(token)

        try:
            signature_bytes = base64.b64decode(token.signature)
            if not self.kms.verify(self.key_id, payload, signature_bytes):
                return False
        except Exception:
            return False

        # ⏱ expiración
        now = int(time.time())
        if token.expires_at < now:
            return False
            
        # 🛡️ anti-replay
        if token.nonce in self.used_nonces:
            return False
        self.used_nonces.add(token.nonce)

        return True

    def _serialize(self, token: CapabilityToken) -> bytes:
        data = {
            "subject": token.subject,
            "capability": token.capability.__dict__,
            "issued_at": token.issued_at,
            "expires_at": token.expires_at,
            "nonce": token.nonce,
            "code_hash": token.code_hash
        }
        return json.dumps(data, sort_keys=True).encode()
