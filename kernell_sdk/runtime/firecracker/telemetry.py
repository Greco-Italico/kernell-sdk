"""
Telemetry Manager: End-to-End Tracing + Cryptographic Audit via AuditLedger.
"""

import json
import time
from typing import Dict, Any, Optional

from ...security.kms import BaseKMS
from .ledger import AuditLedger


class TelemetryManager:
    """
    Handles End-to-End Tracing and delegates audit events
    to the append-only, hash-chained AuditLedger.
    """

    def __init__(self, kms: Optional[BaseKMS] = None,
                 ledger_path: str = "/var/log/kernell/audit.ledger"):
        self.kms = kms
        self.ledger = AuditLedger(kms=kms, ledger_path=ledger_path)

    def trace(self, request_id: str, stage: str, details: Dict[str, Any]):
        """Logs a trace span for a request (stdout for now, Jaeger/OTLP later)."""
        timestamp = time.time()
        print(f"[TRACE] [{timestamp}] {request_id} | {stage} | {json.dumps(details)}")

    def log_audit_event(self, request_id: str, tenant_id: str, action: str,
                        details: Dict[str, Any], code: str = ""):
        """
        Creates a hash-chained, KMS-signed audit entry in the ledger.
        Essential for SOC2 / ISO compliance.
        """
        self.ledger.append(
            tenant_id=tenant_id,
            request_id=request_id,
            action=action,
            code=code,
            details=details,
        )

    def verify_integrity(self):
        """Verify the full audit chain. Returns (valid, entries_checked, error)."""
        return self.ledger.verify_chain()
