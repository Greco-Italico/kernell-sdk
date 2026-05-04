from dataclasses import dataclass, field
from typing import Optional, Dict, Any
import time

@dataclass
class ValidationResult:
    valid: bool
    reason: Optional[str] = None
    penalty: Optional[int] = None

class ProtocolValidator:
    """
    The Gatekeeper. Every incoming message must pass this pipeline
    before touching the node's local state.
    """
    def __init__(self, get_epoch_fn):
        self.get_epoch = get_epoch_fn

    def validate(self, message: Dict[str, Any], sender_pubkey: str) -> ValidationResult:
        # 1. Structure
        if not self._validate_structure(message):
            return ValidationResult(False, "invalid_structure", penalty=10)

        # 2. Epoch
        if not self._validate_epoch(message):
            return ValidationResult(False, "invalid_epoch", penalty=5)

        # 3. Signature
        if not self._verify_signature(message, sender_pubkey):
            return ValidationResult(False, "invalid_signature", penalty=50)
            
        # If it passes, it's structurally valid (economic validation happens later)
        return ValidationResult(True)

    def _validate_structure(self, msg: Dict[str, Any]) -> bool:
        required_fields = ["msg_id", "type", "sender", "epoch", "timestamp", "payload", "signature"]
        return all(f in msg for f in required_fields)

    def _validate_epoch(self, msg: Dict[str, Any]) -> bool:
        current_epoch = self.get_epoch()
        # Accept messages from previous, current, or next epoch (handle clock drift)
        return abs(msg["epoch"] - current_epoch) <= 1

    def _verify_signature(self, msg: Dict[str, Any], pubkey: str) -> bool:
        # In a real implementation:
        # payload_bytes = canonical_serialize(msg["payload"])
        # return ed25519_verify(pubkey, payload_bytes, msg["signature"])
        # For this prototype, we simulate a fast signature check
        return msg.get("signature") != "invalid_sig"
