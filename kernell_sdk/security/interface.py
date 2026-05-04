class SecurityLayer:
    """
    Base interface for Kernell OS Security.
    Defines the contract that any security layer (baseline or adaptive) must fulfill.
    """
    def validate_input(self, input_data: str, context: dict) -> tuple[bool, str]:
        raise NotImplementedError

    def approve_tool(self, tool_name: str, args: dict, context: dict, origin: str = "unknown", actor_id: str = "anonymous") -> tuple[bool, str]:
        raise NotImplementedError

    def validate_output(self, output: str, context: dict, origin: str = "unknown", actor_id: str = "anonymous") -> tuple[bool, str, str]:
        raise NotImplementedError

    @property
    def current_risk_score(self) -> int:
        return 0
