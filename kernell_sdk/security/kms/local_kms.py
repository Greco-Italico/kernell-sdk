from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey
from .base import BaseKMS

class LocalKMS(BaseKMS):
    """
    Local implementation of KMS for development and testing.
    DO NOT USE IN PRODUCTION.
    """

    def __init__(self):
        self.keys = {}

    def create_key(self, key_id: str):
        self.keys[key_id] = Ed25519PrivateKey.generate()

    def import_key(self, key_id: str, private_key_bytes: bytes):
        """Allows importing a known key, mostly for test fixture compatibility."""
        self.keys[key_id] = Ed25519PrivateKey.from_private_bytes(private_key_bytes)

    def import_public_key(self, key_id: str, public_key_bytes: bytes):
        """Imports only a public key (for verification-only node)."""
        self.keys[key_id] = Ed25519PublicKey.from_public_bytes(public_key_bytes)

    def sign(self, key_id: str, payload: bytes) -> bytes:
        if key_id not in self.keys:
            raise KeyError(f"Key {key_id} not found in LocalKMS")
        
        key = self.keys[key_id]
        if isinstance(key, Ed25519PublicKey):
            raise ValueError("Cannot sign with a public key")
            
        return key.sign(payload)

    def verify(self, key_id: str, payload: bytes, signature: bytes) -> bool:
        if key_id not in self.keys:
            return False
            
        key = self.keys[key_id]
        if hasattr(key, "public_key"):
            pub = key.public_key()
        else:
            pub = key # It's already a public key
            
        try:
            pub.verify(signature, payload)
            return True
        except Exception:
            return False
