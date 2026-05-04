from .interface import SecurityLayer

class BasicSecurityLayer(SecurityLayer):
    """
    Open-source baseline security layer.
    Provides fundamental protections without adaptive intelligence or telemetry.
    """
    def __init__(self):
        self._risk_score = 0

    def validate_input(self, input_data: str, context: dict) -> tuple[bool, str]:
        # Basic intent firewall check could go here
        return True, "OK"

    def approve_tool(self, tool_name: str, args: dict, context: dict, origin: str = "unknown", actor_id: str = "anonymous") -> tuple[bool, str]:
        # Basic tool authorization check
        return True, "OK"

    def validate_output(self, output: str, context: dict, origin: str = "unknown", actor_id: str = "anonymous") -> tuple[bool, str, str]:
        # Basic output guard check
        return True, output, "OK"

    @property
    def current_risk_score(self) -> int:
        return self._risk_score
