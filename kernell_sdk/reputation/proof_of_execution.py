import hashlib
import uuid
from typing import Tuple, Dict

from kernell_sdk.identity import sign_message_bytes, verify_signature_bytes
from kernell_sdk.reputation.receipt import ExecutionReceipt

class ProofOfExecutionEngine:
    """
    Prevents fraud in the decentralized compute marketplace.
    Implements Canary Injection to catch 'lazy nodes' that fake execution
    without actually running the code.
    """

    def __init__(self):
        pass

    def prepare_verifiable_task(self, original_code: str) -> Tuple[str, str]:
        """
        Injects a cryptographic canary into the code. 
        The canary requires the node to actually compute a state-dependent hash
        to prevent static parsing and fake outputs.
        """
        secret_nonce = uuid.uuid4().hex
        
        injected_code = f"""
import hashlib
import json
import sys

def __kernell_poe_wrap():
    # Capture standard output locally
    from io import StringIO
    old_stdout = sys.stdout
    sys.stdout = capture = StringIO()
    
    try:
        # Original Code Execution
{chr(10).join('        ' + line for line in original_code.split(chr(10)))}
    finally:
        sys.stdout = old_stdout
        
    output_val = capture.getvalue()
    # Data-dependent structural canary
    _canary_hash = hashlib.sha256((output_val + '{secret_nonce}').encode()).hexdigest()
    
    print(output_val, end='')
    print(f"\\n[KERNELL_POE_HASH]\\n{{_canary_hash}}\\n[/KERNELL_POE_HASH]")

__kernell_poe_wrap()
"""
        return injected_code, secret_nonce

    def verify_output_and_create_receipt(
        self,
        agent_id: str,
        private_key: str,
        original_code: str,
        output: str,
        expected_canary: str,
        mode_used: str,
        fallback_triggered: bool,
        execution_time: float,
        success: bool
    ) -> ExecutionReceipt:
        """
        Validates the output contains the correct data-dependent canary, 
        hashes the results, and signs the receipt for the Escrow contract.
        """
        # Extract data and canary hash
        if "[KERNELL_POE_HASH]" not in output or "[/KERNELL_POE_HASH]" not in output:
            raise ValueError("Proof of Execution Failed: Structural canary missing. Node is faking execution.")
            
        parts = output.split("\\n[KERNELL_POE_HASH]\\n")
        actual_output = parts[0]
        actual_hash = parts[1].replace("\\n[/KERNELL_POE_HASH]", "").strip()
        
        # Verify data-dependent canary
        expected_hash = hashlib.sha256((actual_output + expected_canary).encode("utf-8")).hexdigest()
        if actual_hash != expected_hash:
            raise ValueError("Proof of Execution Failed: Invalid canary signature. Output was tampered or simulated.")

        task_hash = hashlib.sha256(original_code.encode("utf-8")).hexdigest()
        output_hash = hashlib.sha256(actual_output.encode("utf-8")).hexdigest()

        receipt = ExecutionReceipt(
            agent_id=agent_id,
            task_hash=task_hash,
            output_hash=output_hash,
            mode_used=mode_used,
            fallback_triggered=fallback_triggered,
            execution_time=execution_time,
            success=success,
            canary_nonce=expected_canary
        )

        # Sign the deterministic payload
        payload_bytes = receipt.get_signing_payload()
        receipt.signature = sign_message_bytes(payload_bytes, private_key)

        return receipt

    def verify_receipt(self, receipt: ExecutionReceipt, public_key_hex: str) -> bool:
        """
        Used by the Escrow smart contract / backend to verify the receipt 
        was legitimately signed by the agent claiming the bounty.
        """
        payload_bytes = receipt.get_signing_payload()
        return verify_signature_bytes(payload_bytes, receipt.signature, public_key_hex)
