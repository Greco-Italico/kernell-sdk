from abc import ABC, abstractmethod
from .models import ExecutionRequest, ExecutionResult

class BaseRuntime(ABC):
    """
    Contrato estable para cualquier runtime:
    - Subprocess
    - Docker
    - Firecracker
    """

    @abstractmethod
    def execute(self, request: ExecutionRequest) -> ExecutionResult:
        pass
