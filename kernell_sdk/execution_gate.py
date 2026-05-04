"""
Kernell OS SDK — Execution Gate & Consensus
════════════════════════════════════════════════════
Enforces the final security frontier for CRITICAL actions.
Requires Multi-Sig approvals and enforces Time-Locks.
"""
import time
import structlog
from typing import List, Optional
from pydantic import BaseModel
from .risk_engine import RiskLevel

logger = structlog.get_logger("kernell.execution_gate")


class ApprovalSignature(BaseModel):
    signer_id: str
    signer_role: str = "agent"  # 'agent', 'human', 'oracle'
    public_key_hex: str         # Required for crypto verification
    signature: bytes
    timestamp: float


class ExecutionGate:
    """
    Multi-Layer Execution Authority.
    Gatekeeper that stops execution even if Policy Engine allows it,
    demanding external consensus for High/Critical risk profiles.
    """
    def __init__(self, required_signatures: int = 2, timelock_seconds: int = 30):
        self.required_signatures = required_signatures
        self.timelock_seconds = timelock_seconds
        self.required_roles = {"agent", "oracle"}  # Trust Diversity Requirement

    def approve(self, command: str, risk: RiskLevel, signatures: Optional[List[ApprovalSignature]] = None) -> bool:
        if risk == RiskLevel.LOW:
            return True
            
        if risk == RiskLevel.MEDIUM:
            # Could enforce soft-rate limits here
            return True
            
        if risk == RiskLevel.HIGH:
            # C-10 FIX: Fail-close for HIGH risk. The old string-matching blocklist
            # was trivially bypasseable (/usr/bin/curl, python -m urllib, etc.).
            # HIGH risk actions require explicit multi-sig approval, same as CRITICAL.
            logger.warning("execution_gate_high_risk_denied", command=command)
            return self._enforce_multisig_and_timelock(command, signatures)
            
        if risk == RiskLevel.CRITICAL:
            logger.critical("execution_gate_critical_risk_triggered", command=command)
            return self._enforce_multisig_and_timelock(command, signatures)
            
        return False

    def _enforce_multisig_and_timelock(self, command: str, signatures: Optional[List[ApprovalSignature]]) -> bool:
        """Enforces N-of-M signatures, Trust Diversity, and delays execution."""
        sigs = signatures or []
        if len(sigs) < self.required_signatures:
            logger.error(
                "execution_gate_multisig_failed",
                required=self.required_signatures,
                provided=len(sigs),
                command=command
            )
            return False
            
        # Trust Diversity Verification (Anti Multisig-Abuse)
        provided_roles = {sig.signer_role for sig in sigs}
        missing_roles = self.required_roles - provided_roles
        if missing_roles:
            logger.error(
                "execution_gate_trust_diversity_failed",
                missing_roles=list(missing_roles)
            )
            return False
            
        # Verify timestamps to prevent replay attacks
        now = time.time()
        for sig in sigs:
            if now - sig.timestamp > 300:  # Signatures valid for 5 minutes
                logger.error("execution_gate_signature_expired", signer=sig.signer_id)
                return False
                
            # KOS-C04: Cryptographic Verification Enforced
            if len(sig.signature) != 64:
                logger.error("execution_gate_invalid_signature_length", signer=sig.signer_id)
                return False
                
            from .identity import verify_signature
            payload = f"{command}:{sig.timestamp}"
            if not verify_signature(payload, sig.signature.hex(), sig.public_key_hex):
                logger.error("execution_gate_invalid_signature", signer=sig.signer_id)
                return False

        # Apply Time-Lock Delay & Freeze State
        logger.warning(f"TIMELOCK ENGAGED: Executing CRITICAL action in {self.timelock_seconds} seconds.")
        logger.warning("AGENT STATE FROZEN: Ignoring external stimuli to prevent evasion.")
        
        # For now, blocking time.sleep enforces the freeze synchronously, but we use threading.Event
        # to allow interrupts and avoid freezing the event loop if run in threads.
        import threading
        _freeze_event = threading.Event()
        _freeze_event.wait(timeout=self.timelock_seconds)
        
        logger.info("execution_gate_approved", command=command)
        return True
