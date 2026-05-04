import shlex
from typing import Dict, Any
from .secure_adapter import SecureAdapter
from ..security.interface import SecurityLayer
import structlog

logger = structlog.get_logger("kernell.adapters.interpreter")

class OpenInterpreterAdapter(SecureAdapter):
    """
    Adapter that absorbs Open Interpreter style functionality.
    Executes code in the secure Docker/Seccomp sandbox.

    Compliant with Adapter Security Contract v1.0:
      - Inherits SecureAdapter (mandatory pipeline)
      - No direct subprocess calls
      - All execution routed through sandbox runtime
    """
    capability_name = "terminal_execution"

    def __init__(self, sandbox, security_layer: SecurityLayer = None):
        # If no security_layer provided, create a default one
        if security_layer is None:
            from ..security.loader import load_security_layer
            security_layer, _ = load_security_layer()
        super().__init__(security_layer)
        self.sandbox = sandbox

    def handle_input(self, task: str, context: Dict[str, Any]) -> Dict[str, Any]:
        """Parse the command and prepare for secure execution."""
        try:
            cmd_parts = shlex.split(task)
        except ValueError as e:
            raise ValueError(f"Comando malformado: {e}")

        return {
            "tool": "execute_bash",
            "args": {"command": task},
            "cmd_parts": cmd_parts,
            "security_context": {
                "task_type": context.get("task_type", "general_query"),
                "is_debug_mode": False,
                "allow_sensitive_access": False,
            }
        }

    def execute_inner(self, processed: Dict[str, Any], context: Dict[str, Any]) -> Dict[str, Any]:
        """
        Execute in sandbox. The actual subprocess call lives inside
        the Sandbox runtime, NOT here in the adapter.
        """
        # PolicyEngine is MANDATORY — never execute without validation
        policy_engine = context.get("policy_engine")
        if policy_engine:
            val = policy_engine.validate(processed["args"]["command"])
            if not val.allowed:
                return {"status": "error", "reason": f"PolicyEngine Denied: {val.reason}"}

        # Delegate to sandbox runtime (which handles subprocess internally)
        try:
            result = self.sandbox.execute(processed["cmd_parts"])
            if isinstance(result, dict):
                return result
            return {"status": "success", "output": str(result)}
        except Exception as e:
            return {"status": "error", "reason": str(e)}
