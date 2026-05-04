from dataclasses import dataclass, field
from typing import List

@dataclass
class Capability:
    action: str              # e.g., "python.execute"
    max_cpu: float
    max_memory_mb: int
    allow_network: bool
    allowed_modules: List[str]

@dataclass
class CapabilityToken:
    subject: str             # agent_id
    capability: Capability
    issued_at: int
    expires_at: int
    nonce: str
    code_hash: str           # Binding to the specific request code
    signature: str = ""
