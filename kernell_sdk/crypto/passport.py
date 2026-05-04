# kernell_sdk/crypto/passport.py — PATCH: AES-128-CBC → AES-256-GCM
#
# VULNERABILIDAD ORIGINAL:
#   AES-128-CBC sin MAC → vulnerable a Padding Oracle, sin integridad.
#
# SOLUCIÓN:
#   AES-256-GCM (AEAD) + Scrypt KDF + migration path para passports legacy.

from __future__ import annotations

import base64
import json
import os
import stat
from pathlib import Path
from typing import Optional

from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.scrypt import Scrypt


# ──────────────────────────────────────────────────────────────────────────────
# Constantes de seguridad
# ──────────────────────────────────────────────────────────────────────────────
_CIPHER_LABEL   = b"kernell-passport-v2"   # AAD — autenticación del contexto
_SCRYPT_N       = 2 ** 17                  # CPU cost — ~0.5s en hardware moderno
_SCRYPT_R       = 8
_SCRYPT_P       = 1
_KEY_LEN        = 32                       # 256 bits


# ──────────────────────────────────────────────────────────────────────────────
class PassportVault:
    """
    Gestión segura de claves privadas de agentes usando AES-256-GCM + Scrypt.

    Provee:
    - Confidencialidad    → AES-256 (vs AES-128 anterior)
    - Integridad          → GCM tag de 128 bits (vs ninguna en CBC anterior)
    - Autenticación       → AEAD con AAD (contexto criptográfico vinculado)
    - Resistencia a brute-force → Scrypt (resistente a GPU/ASIC)
    - Binding de hardware → UDID mezclado como pepper en la KDF
    """

    # ── Cifrado ───────────────────────────────────────────────────────────────
    @classmethod
    def seal(
        cls,
        private_key_bytes: bytes,
        passphrase: str,
        udid: str,
    ) -> str:
        """
        Cifra la clave privada y devuelve un JSON blob sellado (string serializable).

        Args:
            private_key_bytes: Clave privada en crudo (Ed25519 = 32 bytes).
            passphrase:        Contraseña del usuario.
            udid:              Machine identity anchor (pepper de hardware).

        Returns:
            String JSON del blob cifrado, listo para guardar en disco.
        """
        # 1. Salt aleatorio para la KDF (único por cada seal)
        salt = os.urandom(32)

        # 2. Derivar clave maestra con Scrypt
        #    El UDID actúa como pepper — sin él no se puede descifrar aunque
        #    se tenga la contraseña, atando el passport a la máquina.
        master_key = cls._derive_key(passphrase, udid, salt)

        # 3. Cifrar con AES-256-GCM
        nonce  = os.urandom(12)   # 96 bits — valor óptimo para GCM
        aesgcm = AESGCM(master_key)
        ciphertext_with_tag = aesgcm.encrypt(
            nonce,
            private_key_bytes,
            _CIPHER_LABEL,         # Additional Authenticated Data
        )

        blob = {
            "version":    2,
            "cipher":     "AES-256-GCM",
            "kdf":        "scrypt",
            "scrypt_n":   _SCRYPT_N,
            "salt":       base64.b64encode(salt).decode(),
            "nonce":      base64.b64encode(nonce).decode(),
            "ciphertext": base64.b64encode(ciphertext_with_tag).decode(),
        }
        return json.dumps(blob, separators=(",", ":"))

    # ── Descifrado ────────────────────────────────────────────────────────────
    @classmethod
    def unseal(
        cls,
        blob_json: str,
        passphrase: str,
        udid: str,
    ) -> bytes:
        """
        Descifra y autentica el blob. Lanza excepción si la integridad falla.

        Si se detecta un blob v1 (AES-128-CBC legacy), se lanza MigrationRequired
        para forzar la actualización al formato seguro.
        """
        blob = json.loads(blob_json)

        # Detectar formato legacy y rechazarlo explícitamente
        if blob.get("version", 1) < 2:
            raise MigrationRequired(
                "Este passport usa el cifrado legacy AES-128-CBC (v1) que ya no es seguro. "
                "Ejecuta `kernell migrate-passport` para actualizarlo a AES-256-GCM."
            )

        salt       = base64.b64decode(blob["salt"])
        nonce      = base64.b64decode(blob["nonce"])
        ciphertext = base64.b64decode(blob["ciphertext"])

        # Usar el N guardado en el blob para compatibilidad futura
        n = blob.get("scrypt_n", _SCRYPT_N)

        master_key = cls._derive_key(passphrase, udid, salt, n=n)
        aesgcm     = AESGCM(master_key)

        # GCM autentica el ciphertext automáticamente.
        # Si los datos fueron manipulados → InvalidTag (excepción automática).
        return aesgcm.decrypt(nonce, ciphertext, _CIPHER_LABEL)

    # ── Persistencia segura en disco ──────────────────────────────────────────
    @classmethod
    def save_to_disk(
        cls,
        blob_json: str,
        path: Path,
    ) -> None:
        """Guarda el passport en disco con permisos 0o600."""
        flags = os.O_CREAT | os.O_WRONLY | os.O_TRUNC
        mode  = stat.S_IRUSR | stat.S_IWUSR  # 0o600

        fd = os.open(str(path), flags, mode)
        try:
            os.write(fd, blob_json.encode())
        finally:
            os.close(fd)

    @classmethod
    def load_from_disk(cls, path: Path) -> str:
        """Carga y valida los permisos del passport antes de leerlo."""
        actual_mode = oct(stat.S_IMODE(os.stat(path).st_mode))
        if actual_mode != "0o600":
            raise PermissionError(
                f"El archivo passport en {path} tiene permisos {actual_mode}. "
                "Se esperaba 0o600. Otro proceso pudo haber modificado el archivo."
            )
        return path.read_text()

    # ── Migración de passports legacy ─────────────────────────────────────────
    @classmethod
    def migrate_from_legacy_cbc(
        cls,
        legacy_ciphertext_hex: str,
        legacy_key_hex: str,
        legacy_iv_hex: str,
        passphrase: str,
        udid: str,
    ) -> str:
        """
        Migra un passport v1 (AES-128-CBC) al formato v2 (AES-256-GCM).
        Requiere la clave e IV del formato anterior para descifrar primero.
        """
        from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
        from cryptography.hazmat.primitives import padding

        key        = bytes.fromhex(legacy_key_hex)
        iv         = bytes.fromhex(legacy_iv_hex)
        ciphertext = bytes.fromhex(legacy_ciphertext_hex)

        # Descifrar CBC legacy
        cipher    = Cipher(algorithms.AES(key), modes.CBC(iv))
        decryptor = cipher.decryptor()
        padded    = decryptor.update(ciphertext) + decryptor.finalize()

        unpadder      = padding.PKCS7(128).unpadder()
        private_key   = unpadder.update(padded) + unpadder.finalize()

        # Re-cifrar con AES-256-GCM
        return cls.seal(private_key, passphrase, udid)

    # ── KDF privada ───────────────────────────────────────────────────────────
    @classmethod
    def _derive_key(
        cls,
        passphrase: str,
        udid: str,
        salt: bytes,
        n: int = _SCRYPT_N,
    ) -> bytes:
        combined = f"{passphrase}:{udid}".encode("utf-8")
        kdf = Scrypt(salt=salt, length=_KEY_LEN, n=n, r=_SCRYPT_R, p=_SCRYPT_P)
        return kdf.derive(combined)


# ──────────────────────────────────────────────────────────────────────────────
class MigrationRequired(Exception):
    """Se lanza cuando un passport legacy intenta ser usado sin migrar."""
    pass
