import uuid
from dataclasses import dataclass, field
from typing import Optional, Dict

@dataclass
class ExecutionRequest:
    code: str
    timeout: int = 2
    memory_limit_mb: int = 128
    cpu_limit: float = 0.5
    env: Dict[str, str] = field(default_factory=dict)
    allow_network: bool = False
    tenant_id: str = "default_tenant"
    request_id: str = field(default_factory=lambda: str(uuid.uuid4()))

@dataclass
class ExecutionResult:
    stdout: str
    stderr: str
    exit_code: int
    timed_out: bool = False
