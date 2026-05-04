"""
Append-Only Cryptographic Audit Ledger for Kernell OS.

Each entry is hash-chained (SHA-256) and KMS-signed,
producing a tamper-evident, non-repudiable execution history.

Compliance targets: SOC2 Type II, ISO 27001.
"""

import hashlib
import json
import os
import threading
import time
from dataclasses import dataclass, field
from typing import List, Optional, Tuple

from ...security.kms import BaseKMS


@dataclass
class AuditEntry:
    timestamp: float
    tenant_id: str
    request_id: str
    action: str
    payload_hash: str  # SHA-256 of the actual payload — never log raw code
    details: dict
    prev_hash: str = ""

    def serialize(self) -> str:
        return json.dumps({
            "ts": self.timestamp,
            "tenant": self.tenant_id,
            "request_id": self.request_id,
            "action": self.action,
            "payload_hash": self.payload_hash,
            "details": self.details,
            "prev_hash": self.prev_hash,
        }, sort_keys=True)

    def compute_hash(self) -> str:
        return hashlib.sha256(self.serialize().encode()).hexdigest()


class AuditLedger:
    """
    Hash-chained, KMS-signed, append-only ledger.
    Persists to a local file in append mode.
    """

    GENESIS_HASH = "0" * 64

    def __init__(self, kms: Optional[BaseKMS] = None, ledger_path: str = "/var/log/kernell/audit.ledger"):
        self.kms = kms
        self.ledger_path = ledger_path
        self.lock = threading.Lock()
        self.last_hash = self.GENESIS_HASH
        self.chain_length = 0

        # Ensure directory exists
        os.makedirs(os.path.dirname(self.ledger_path), exist_ok=True)

        # Resume chain from existing file if present
        if os.path.exists(self.ledger_path):
            self._resume_chain()

    def _resume_chain(self):
        """Read the last line to recover last_hash for chain continuity."""
        try:
            with open(self.ledger_path, "r") as f:
                lines = f.readlines()
                if lines:
                    last_line = lines[-1].strip()
                    parts = last_line.split("|")
                    if len(parts) >= 2:
                        self.last_hash = parts[1]
                        self.chain_length = len(lines)
        except Exception as e:
            import logging
            logging.warning(f'Suppressed error in {__name__}: {e}')  # Start fresh if file is corrupted

    def append(self, tenant_id: str, request_id: str, action: str,
               code: str = "", details: Optional[dict] = None):
        """Create, chain, sign, and persist an audit entry."""
        payload_hash = hashlib.sha256(code.encode()).hexdigest() if code else "no_payload"

        entry = AuditEntry(
            timestamp=time.time(),
            tenant_id=tenant_id,
            request_id=request_id,
            action=action,
            payload_hash=payload_hash,
            details=details or {},
            prev_hash=self.last_hash,
        )

        entry_hash = entry.compute_hash()

        # KMS signature (non-repudiation)
        signature = ""
        if self.kms:
            try:
                signature = self.kms.sign(tenant_id, entry.serialize().encode("utf-8")).hex()
            except Exception:
                signature = "SIGNING_FAILED"

        with self.lock:
            # Persist: entry_json | hash | signature
            line = f"{entry.serialize()}|{entry_hash}|{signature}\n"
            with open(self.ledger_path, "a") as f:
                f.write(line)

            self.last_hash = entry_hash
            self.chain_length += 1

    def verify_chain(self, verify_signatures: bool = False) -> Tuple[bool, int, str]:
        """
        Verify the entire chain integrity.
        Returns (valid, entries_checked, error_message).
        """
        if not os.path.exists(self.ledger_path):
            return True, 0, ""

        prev_hash = self.GENESIS_HASH
        checked = 0

        with open(self.ledger_path, "r") as f:
            for i, line in enumerate(f):
                line = line.strip()
                if not line:
                    continue

                parts = line.split("|")
                if len(parts) < 2:
                    return False, i, f"Malformed entry at line {i}"

                entry_json = parts[0]
                stored_hash = parts[1]
                signature_hex = parts[2] if len(parts) >= 3 else ""

                # Verify prev_hash chain
                try:
                    entry_data = json.loads(entry_json)
                except json.JSONDecodeError:
                    return False, i, f"Invalid JSON at line {i}"

                if entry_data.get("prev_hash") != prev_hash:
                    return False, i, f"Chain broken at line {i}: expected prev_hash={prev_hash}, got={entry_data.get('prev_hash')}"

                # Verify hash
                computed = hashlib.sha256(entry_json.encode()).hexdigest()
                if computed != stored_hash:
                    return False, i, f"Hash mismatch at line {i}: computed={computed}, stored={stored_hash}"

                # Optional KMS signature validation
                if verify_signatures and self.kms:
                    tenant_id = entry_data.get("tenant")
                    if not signature_hex:
                        return False, i, f"Missing signature at line {i}"
                    try:
                        sig = bytes.fromhex(signature_hex)
                    except ValueError:
                        return False, i, f"Malformed signature at line {i}"
                    if not self.kms.verify(tenant_id, entry_json.encode("utf-8"), sig):
                        return False, i, f"Invalid signature at line {i}"

                prev_hash = stored_hash
                checked += 1

        return True, checked, ""
