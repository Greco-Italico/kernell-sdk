from .base import BaseKMS
import base64
import httpx
import urllib.parse

class VaultKMS(BaseKMS):
    """
    HashiCorp Vault implementation of KMS for production.
    Uses Vault's Transit Secrets Engine.
    """

    def __init__(self, vault_url: str, token: str):
        self.url = vault_url.rstrip('/')
        self.token = token
        parsed = urllib.parse.urlparse(self.url)
        if parsed.scheme != "https":
            raise ValueError("VaultKMS requires https:// vault_url")
        # Vault is commonly internal/private IP space; do NOT apply generic SSRF blocks here.
        # Instead, pin to the configured Vault host only.
        self._vault_host = parsed.hostname
        self.client = httpx.Client(timeout=5.0, verify=True, follow_redirects=False)

    def sign(self, key_id: str, payload: bytes) -> bytes:
        # Vault expects base64 encoded input for transit engine
        encoded_payload = base64.b64encode(payload).decode('utf-8')
        
        res = self.client.post(
            f"{self.url}/v1/transit/sign/{key_id}",
            headers={"X-Vault-Token": self.token},
            json={"input": encoded_payload}
        )
        res.raise_for_status()
        
        # Vault returns signature like "vault:v1:base64_string"
        signature_str = res.json()["data"]["signature"]
        
        # We might want to strip the "vault:v1:" prefix depending on our architecture, 
        # but standard vault verify expects it. Let's return raw bytes.
        # Actually, let's just return the UTF-8 encoded vault signature format.
        return signature_str.encode('utf-8')

    def verify(self, key_id: str, payload: bytes, signature: bytes) -> bool:
        encoded_payload = base64.b64encode(payload).decode('utf-8')
        signature_str = signature.decode('utf-8')
        
        try:
            res = self.client.post(
                f"{self.url}/v1/transit/verify/{key_id}",
                headers={"X-Vault-Token": self.token},
                json={
                    "input": encoded_payload,
                    "signature": signature_str
                }
            )
            res.raise_for_status()
            return res.json()["data"].get("valid", False)
        except Exception:
            return False
