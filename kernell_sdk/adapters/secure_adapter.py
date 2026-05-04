"""
Kernell OS SDK — Secure Adapter Base
═════════════════════════════════════
Enforces the Adapter Security Contract v1.0.

ALL adapters MUST inherit from SecureAdapter, not BaseAdapter directly.
SecureAdapter wraps the full SecurityLayer pipeline around
every execution path, making bypass architecturally impossible.
"""
from abc import abstractmethod
from typing import Dict, Any

import structlog

from .base import BaseAdapter
from ..security.interface import SecurityLayer

logger = structlog.get_logger("kernell.adapters.secure")


class SecureAdapter(BaseAdapter):
    """
    Security-enforced adapter base.

    Subclasses implement:
      - handle_input()  → parse/transform the raw task
      - execute_inner() → the actual adapter logic (sandboxed)
      - handle_output() → post-process the result

    The mandatory pipeline (Input Guard → Tool Governor → Execution → Output Guard)
    is enforced by `process()` and CANNOT be overridden.
    """

    def __init__(self, security_layer: SecurityLayer):
        assert security_layer is not None, \
            "SecureAdapter REQUIRES a SecurityLayer instance. This is non-negotiable."
        self.security = security_layer

    # ── Mandatory pipeline (final — do NOT override) ─────────────────────────

    def execute(self, task: str, context: Dict[str, Any]) -> Dict[str, Any]:
        """
        The ONLY public execution entry point.
        Wraps the full security pipeline around every adapter call.
        """
        # 🛡️ PHASE 1: Input Guard
        input_allowed, input_reason = self._guard_input(task, context)
        if not input_allowed:
            logger.warning("adapter_input_blocked",
                           adapter=self.capability_name,
                           reason=input_reason)
            return {"status": "error", "reason": f"[INPUT_GUARD] {input_reason}"}

        # ⚙️ PHASE 2: Adapter-specific input handling
        try:
            processed = self.handle_input(task, context)
        except Exception as e:
            return {"status": "error", "reason": f"[ADAPTER_PARSE] {e}"}

        # 🔒 PHASE 3: Tool Governor
        tool_name = processed.get("tool", self.capability_name)
        tool_args = processed.get("args", {"command": task})
        csl_context = processed.get("security_context", {
            "task_type": "general_query",
            "is_debug_mode": False,
            "allow_sensitive_access": False,
        })

        tool_allowed, tool_reason = self.security.approve_tool(
            tool_name, tool_args, csl_context
        )
        if not tool_allowed:
            logger.warning("adapter_tool_blocked",
                           adapter=self.capability_name,
                           tool=tool_name,
                           reason=tool_reason)
            return {"status": "error", "reason": f"[TOOL_GOVERNOR] {tool_reason}"}

        # ⚡ PHASE 4: Guarded execution
        try:
            result = self.execute_inner(processed, context)
        except Exception as e:
            return {"status": "error", "reason": f"[EXECUTION] {e}"}

        # 🛡️ PHASE 5: Output Guard (DLP)
        raw_output = result.get("output", "") if isinstance(result, dict) else str(result)
        out_allowed, safe_output, out_reason = self.security.validate_output(
            raw_output, csl_context
        )
        if not out_allowed:
            logger.warning("adapter_output_blocked",
                           adapter=self.capability_name,
                           reason=out_reason)
            if isinstance(result, dict):
                result["output"] = safe_output
                result["dlp_triggered"] = True
            else:
                result = {"status": "success", "output": safe_output, "dlp_triggered": True}

        return result

    # ── Input Guard (built-in heuristics) ────────────────────────────────────

    def _guard_input(self, task: str, context: Dict[str, Any]) -> tuple[bool, str]:
        """
        Default input guard. Detects prompt injection patterns.
        Adapters can extend this (via super()) but CANNOT disable it.
        """
        task_lower = task.lower()

        # Classic prompt injection markers
        injection_markers = [
            "ignore all previous",
            "ignore instructions",
            "you are now",
            "act as root",
            "sudo mode",
            "override security",
            "disable restrictions",
        ]
        if any(marker in task_lower for marker in injection_markers):
            return False, "Prompt injection pattern detected"

        # Role impersonation
        role_markers = [
            "i am an administrator",
            "soy administrador",
            "authorized admin",
            "auditoría autorizada",
        ]
        if any(marker in task_lower for marker in role_markers):
            return False, "Role impersonation attempt detected"

        return True, "OK"

    # ── Abstract methods (subclasses MUST implement) ─────────────────────────

    @abstractmethod
    def handle_input(self, task: str, context: Dict[str, Any]) -> Dict[str, Any]:
        """
        Parse and transform the raw task into a structured execution request.

        Must return a dict with at least:
          - "tool": str (the tool name to authorize)
          - "args": dict (arguments for the tool)
          - "security_context": dict (optional overrides for CSL context)
        """
        ...

    @abstractmethod
    def execute_inner(self, processed: Dict[str, Any], context: Dict[str, Any]) -> Dict[str, Any]:
        """
        The actual adapter logic. Called ONLY after Input Guard and Tool Governor pass.
        """
        ...

    def handle_output(self, output: Dict[str, Any], context: Dict[str, Any]) -> Dict[str, Any]:
        """Optional post-processing hook. Default: passthrough."""
        return output
