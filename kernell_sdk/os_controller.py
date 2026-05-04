"""
Kernell OS SDK — OS Controller Client (Phase 7b)
════════════════════════════════════════════════
Provides a secure client to interface with the isolated OS Daemon.
Executes physical UI actions (mouse, keyboard) and captures screen state.
"""

import base64
import json
import logging
import urllib.request
import urllib.error
from typing import Dict, Any, Optional

logger = logging.getLogger("kernell.agent.os_controller")

class OSController:
    """Client for the isolated OS Daemon."""
    
    def __init__(self, daemon_url: str = "http://localhost:8505"):
        self.base_url = daemon_url.rstrip("/")
        self.session_id = None
        
    def set_session(self, session_id: str):
        self.session_id = session_id

    def _post(self, endpoint: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        url = f"{self.base_url}{endpoint}"
        if self.session_id:
            payload["session_id"] = self.session_id
            
        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
        
        try:
            with urllib.request.urlopen(req, timeout=5) as response:
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.URLError as e:
            logger.error(f"[OSController] Request to {endpoint} failed: {e}")
            return {"success": False, "error": str(e)}

    def _get(self, endpoint: str) -> Dict[str, Any]:
        url = f"{self.base_url}{endpoint}"
        req = urllib.request.Request(url)
        if self.session_id:
            req.add_header("X-Session-ID", self.session_id)
            
        try:
            with urllib.request.urlopen(req, timeout=10) as response:
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.URLError as e:
            logger.error(f"[OSController] GET {endpoint} failed: {e}")
            return {"success": False, "error": str(e), "image_base64": ""}

    # ── Capabilities ────────────────────────────────────────────────

    def click(self, x: int, y: int) -> bool:
        """Move mouse and click at absolute coordinates."""
        res = self._post("/click", {"x": x, "y": y})
        return res.get("success", False)

    def type_text(self, text: str) -> bool:
        """Type literal text string."""
        res = self._post("/type", {"text": text})
        return res.get("success", False)

    def keypress(self, key: str) -> bool:
        """Press a special key (e.g., 'enter', 'tab', 'escape')."""
        res = self._post("/keypress", {"key": key})
        return res.get("success", False)

    def screenshot(self) -> str:
        """Capture the screen and return base64 encoded image."""
        res = self._get("/screenshot")
        return res.get("image_base64", "")
        
    def register_tools(self, registry) -> None:
        """Register OS interaction capabilities into the agent's ToolRegistry."""
        # Using late import to prevent circular dependency
        from kernell_sdk.agent_runtime import Tool
        
        registry.register(Tool(
            name="os_click",
            func=self.click,
            description="Click on the screen at specific x, y coordinates.",
            parameters={"x": "integer X coordinate", "y": "integer Y coordinate"}
        ))
        
        registry.register(Tool(
            name="os_type",
            func=self.type_text,
            description="Type a text string into the currently focused input.",
            parameters={"text": "string to type"}
        ))
        
        registry.register(Tool(
            name="os_keypress",
            func=self.keypress,
            description="Press a special key like 'enter', 'tab', or 'escape'.",
            parameters={"key": "key name string"}
        ))
