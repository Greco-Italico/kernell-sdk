import hashlib
from typing import Optional
from kernell_sdk.runtime.models import ExecutionRequest
from .capabilities import CapabilityToken

class CapabilityPolicyEngine:

    def enforce(self, token: CapabilityToken, request: ExecutionRequest):
        cap = token.capability

        # 🔴 Binding al código (Evita uso del mismo token para código malicioso)
        code_hash = hashlib.sha256(request.code.encode()).hexdigest()
        if token.code_hash != code_hash:
            raise Exception("Code hash mismatch: The token does not authorize this specific code payload")

        # 🔴 acción
        if cap.action != "python.execute":
            raise Exception("Action not allowed")

        # 🔴 recursos
        if request.memory_limit_mb > cap.max_memory_mb:
            raise Exception(f"Memory limit exceeded (Requested: {request.memory_limit_mb}, Allowed: {cap.max_memory_mb})")

        if request.cpu_limit > cap.max_cpu:
            raise Exception(f"CPU limit exceeded (Requested: {request.cpu_limit}, Allowed: {cap.max_cpu})")

        # 🔴 red
        if request.allow_network and not cap.allow_network:
            raise Exception("Network not allowed")

        return True
