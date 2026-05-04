from abc import ABC, abstractmethod
from typing import Dict, Any

class BaseAdapter(ABC):
    """
    Base class for all Capability Adapters.
    Adapters act as translation layers between Kernell OS's unified Agent interface
    and various execution environments (Terminal, GUI, External Agents).
    """

    @property
    @abstractmethod
    def capability_name(self) -> str:
        """Unique identifier for this capability (e.g., 'terminal_execution')."""
        pass

    @abstractmethod
    def execute(self, task: str, context: Dict[str, Any]) -> Dict[str, Any]:
        """
        Executes the given task using this specific adapter.
        
        Args:
            task: The natural language or parsed command to execute.
            context: Execution context containing auth, environment, and sandboxing details.
            
        Returns:
            Dict containing execution results, status, and output.
        """
        pass
