import time
import hashlib
from typing import Any, Dict
from pydantic import BaseModel, Field

class ExecutionReceipt(BaseModel):
    """
    The fundamental economic truth of the Kernell OS system.
    This receipt is required to claim payment from the Escrow.
    """
    agent_id: str
    task_hash: str
    output_hash: str
    mode_used: str
    fallback_triggered: bool
    execution_time: float
    success: bool
    canary_nonce: str = ""
    timestamp: float = Field(default_factory=time.time)
    signature: str = ""

    def get_signing_payload(self) -> bytes:
        """Canonical deterministic byte representation for cryptographic signing."""
        payload = (
            f"{self.agent_id}:{self.task_hash}:{self.output_hash}:"
            f"{self.mode_used}:{self.fallback_triggered}:{self.success}:"
            f"{self.canary_nonce}"
        )
        return payload.encode("utf-8")
