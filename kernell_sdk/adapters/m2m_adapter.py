from typing import Dict, Any
from .secure_adapter import SecureAdapter
from ..security.interface import SecurityLayer
import structlog

logger = structlog.get_logger("kernell.adapters.m2m")

class M2MAdapter(SecureAdapter):
    """
    Adapter that handles M2M economic delegation.
    If the agent lacks a capability or prefers to outsource, it uses this adapter
    to pay another agent to execute the task.

    Compliant with Adapter Security Contract v1.0:
      - Inherits SecureAdapter (mandatory pipeline)
      - Zero-trust: treats all M2M inputs as untrusted external
      - All delegation passes through ToolGovernor
    """
    capability_name = "peer_delegation"

    def __init__(self, agent, security_layer: SecurityLayer = None):
        if security_layer is None:
            from ..security.loader import load_security_layer
            security_layer, _ = load_security_layer()
        super().__init__(security_layer)
        self.agent = agent

    def handle_input(self, task: str, context: Dict[str, Any]) -> Dict[str, Any]:
        """Parse the delegation task."""
        return {
            "tool": "m2m_delegation",
            "args": {"task": task, "target": "MarketplaceNode"},
            "security_context": {
                "task_type": context.get("task_type", "general_query"),
                "is_debug_mode": False,
                "allow_sensitive_access": False,
            }
        }

    def execute_inner(self, processed: Dict[str, Any], context: Dict[str, Any]) -> Dict[str, Any]:
        """Delegate to peer via escrow."""
        logger.info("m2m_delegating", task=processed["args"]["task"][:50])

        # 1. Estimate cost
        estimated_cost = 2.0

        # 2. Check balance
        if self.agent.wallet.balance < estimated_cost:
            return {"status": "error", "reason": "insufficient_kern"}

        # 3. Pay Peer (Mock Escrow execution)
        success = self.agent.pay_peer(
            target=processed["args"]["target"],
            amount=estimated_cost,
            task=processed["args"]["task"]
        )

        if success:
            return {
                "status": "success",
                "output": "Delegated successfully to peer.",
                "cost": estimated_cost
            }
        else:
            return {"status": "error", "reason": "escrow_failed"}
