import json
import base64
from .capabilities import CapabilityToken
from .kms.base import BaseKMS

class CapabilitySigner:
    def __init__(self, kms: BaseKMS, key_id: str):
        self.kms = kms
        self.key_id = key_id

    def sign(self, token: CapabilityToken) -> str:
        payload = self._serialize(token)
        signature_bytes = self.kms.sign(self.key_id, payload)
        return base64.b64encode(signature_bytes).decode('utf-8')

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
