import inspect
import json
import re
import subprocess
import shlex
import uuid
import time
from typing import Callable, Dict, Any, List, Optional
from pathlib import Path
from pydantic import BaseModel, validate_call
import structlog

from .config import default_config, KernellConfig
from .memory import Memory
from .wallet import Wallet
from .adapters import OpenInterpreterAdapter, AnthropicGUIAdapter, M2MAdapter, CapabilityRouter
from .identity import (
    AgentPassport, create_passport, load_passport,
    load_private_key, SecurityError, sign_message_bytes, verify_signature_bytes,
)
from .security.a2a_replay import A2AReplayGuard, A2AReplayError
from .sandbox import Sandbox, ResourceLimits, AgentPermissions
from .budget import TokenBudget
from .health import SLOMonitor
from .constants import VALID_PERMISSIONS
from .llm import BaseLLMProvider, LLMMessage
from .policy_engine import PolicyEngine, AgentCapabilities
from .security.loader import load_security_layer
from .risk_engine import RiskEngine, ExecutionContext, ActionTag, DataSensitivity
from .execution_gate import ExecutionGate, ApprovalSignature
from .security.rate_limiter import RateLimitGovernor, RateLimitExceeded
from .runtime import HybridRuntime, HybridRuntimeConfig, ExecutionMode
from .runtime.models import ExecutionRequest

logger = structlog.get_logger("kernell.agent")


def _a2a_canonical_signing_bytes(
    sender_agent_id: str,
    tenant_id: str,
    target_id: str,
    payload: str,
    sensitivity_value: str,
    timestamp_ms: int,
    nonce: str,
) -> bytes:
    """
    Frozen A2A signing contract: UTF-8 JSON bytes, sort_keys, no floats (E2/E3).

    - ``timestamp_ms``: wall clock in integer milliseconds
    - ``nonce``: unique per message (replay protection with A2AReplayGuard)
    """
    body = {
        "nonce": nonce,
        "payload": payload,
        "sender_agent_id": sender_agent_id,
        "sensitivity": sensitivity_value,
        "target_id": target_id,
        "tenant_id": tenant_id,
        "timestamp_ms": int(timestamp_ms),
    }
    return json.dumps(
        body,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")


class A2AMessage(BaseModel):
    """Cryptographically signed Inter-Agent message with Taint Propagation."""
    sender_id: str  # canonical agent_id (never display name)
    tenant_id: str = "default"
    target_id: str
    payload: str
    sensitivity: DataSensitivity
    signature: bytes
    timestamp_ms: int
    nonce: str


class AgentState(BaseModel):
    status: str = "idle"
    tasks_completed: int = 0
    kern_earned: float = 0.0


class Agent:
    """
    The Core Kernell PC Agent.
    Autonomous entity capable of executing tasks, using memory,
    participating in the $KERN M2M economy, and controlling the PC.

    Security features:
      - Shell injection prevention (no shell=True)
      - Command blacklisting
      - Permission name whitelisting
      - Passport with encrypted private key; UDID is best-effort host hint (C-05)
    """
    def __init__(
        self,
        name: str,
        description: str = "",
        system_prompt: str = "You are a highly capable Kernell OS autonomous agent.",
        rate_kern_per_task: float = 0.0,
        storage_dir: str = "~/.kernell/agents",
        limits: Optional[ResourceLimits] = None,
        permissions: Optional[AgentPermissions] = None,
        capabilities: Optional[AgentCapabilities] = None,
        config: Optional[KernellConfig] = None,
        engine: Optional['BaseLLMProvider'] = None,  # Support for custom LLM engines
        runtime_config: Optional[HybridRuntimeConfig] = None,
    ):
        self.name = name
        self.description = description
        self.system_prompt = system_prompt
        self.rate = rate_kern_per_task
        self.config = config or default_config
        self.engine = engine
        
        self.runtime_config = runtime_config or self._default_runtime_config()
        self.runtime = HybridRuntime(self.runtime_config)

        # Identity & Passport
        self.storage_path = Path(storage_dir).expanduser() / name.lower().replace(" ", "_")

        try:
            self.passport = load_passport(self.storage_path)
        except SecurityError as e:
            logger.critical("security_violation", error=str(e), agent_name=name)
            raise

        if not self.passport:
            logger.info("creating_new_passport", agent_name=name)
            self.passport, self._private_key = create_passport(name, storage_dir=self.storage_path)
        else:
            # Load the encrypted private key
            self._private_key = load_private_key(self.storage_path)
            if not self._private_key:
                logger.warning("private_key_not_found", agent_name=name)

        self.id = self.passport.agent_id

        # Core Modules
        self.memory = Memory(agent_id=self.id, config=self.config)
        self.wallet = Wallet(config=self.config)
        
        # PC Container & Permissions (M-10 FIX: must init BEFORE adapters)
        self.limits = limits or ResourceLimits()
        self.permissions = permissions or AgentPermissions()
        self.sandbox = Sandbox(self.id, self.limits, self.permissions)

        # Dynamic Security Layer (Open-Core approach)
        self.csl, self.security_mode = load_security_layer(
            shadow_mode=self.config.shadow_mode if hasattr(self.config, 'shadow_mode') else False
        )
        if self.security_mode == "baseline":
            logger.warning("adaptive_shield_missing", message="Running without Adaptive Shield — reduced security")

        # Capability Layer (Adapters) — sandbox is now guaranteed to exist
        # All adapters receive the CognitiveSecurityLayer (Adapter Security Contract v1.0)
        self.adapters = {
            "terminal": OpenInterpreterAdapter(self.sandbox, security_layer=self.csl),
            "gui": AnthropicGUIAdapter(security_layer=self.csl),
            "m2m": M2MAdapter(self, security_layer=self.csl)
        }
        self.router = CapabilityRouter(self.adapters, self.wallet)

        self._skills: Dict[str, Callable] = {}
        self._skill_schemas: List[Dict[str, Any]] = []
        self.state = AgentState()

        # Policy Engine (capability-based security boundary)
        self.capabilities = capabilities or AgentCapabilities()
        self.policy = PolicyEngine(self.capabilities)

        # Multi-Layer Execution Authority (Paranoid Mode)
        self.execution_context = ExecutionContext()
        self.risk_engine = RiskEngine()
        self.execution_gate = ExecutionGate(required_signatures=2, timelock_seconds=30)

        # Observability
        self.budget = TokenBudget(agent_name=self.name)
        self.slo = SLOMonitor(agent_name=self.name)

        # Rate Limiting & Circuit Breakers (singleton)
        self.governor = RateLimitGovernor()

        # A2A anti-replay (E3 on agent channel); swap for Redis in multi-instance deployments
        self._a2a_replay_guard = A2AReplayGuard()

        # Register default Computer Use skills if enabled
        if self.permissions.gui_automation or self.permissions.execute_commands:
            self._register_computer_use_skills()

        logger.info("agent_initialized", agent_name=self.name, agent_id=self.id, kap_address=self.passport.kap_address)
        logger.info("wallet_status", volatile_address=self.passport.kern_volatile_address, solana_address=self.passport.kern_solana_address or "pending")

    def _default_runtime_config(self) -> HybridRuntimeConfig:
        return HybridRuntimeConfig(
            target_mode=ExecutionMode.CONSTRAINED,
            fallback_on_failure=True,
            min_required_mode=ExecutionMode.DEBUG
        )

    def _execute_in_runtime(self, command: str, command_echo: str) -> dict:
        """Executes a command using the HybridRuntime, enforcing observability and taint propagation."""
        req = ExecutionRequest(
            code=command,
            timeout=self.capabilities.max_cpu_seconds,
            memory_limit_mb=self.limits.memory_mb
        )
        
        try:
            result = self.runtime.execute(req)
        except Exception as e:
            return {"error": str(e), "mode_used": "unknown", "fallback_triggered": False}

        stdout = result.stdout[: self.capabilities.max_output_bytes]

        sensitivity = DataSensitivity.PUBLIC
        if "cat " in command_echo or "grep " in command_echo or "ls " in command_echo or "tree " in command_echo:
            sensitivity = DataSensitivity.INTERNAL
            self.execution_context.holds_sensitive_data = True

        ctx = getattr(result, "_execution_context", {})
        
        self.execution_context.record_action(
            ActionTag(
                command=command_echo,
                timestamp=time.time(),
                bytes_processed=len(stdout),
                sensitivity=sensitivity,
            )
        )
        
        return {
            "output": stdout if result.exit_code == 0 else f"Error (exit {result.exit_code}): {result.stderr[:2000]}",
            "mode_used": ctx.get("mode", "unknown"),
            "fallback_triggered": ctx.get("fallback_triggered", False),
            "execution_time": ctx.get("duration_seconds", 0.0)
        }

    def _is_command_safe(self, command: str) -> bool:
        """
        Capability-based command validation via the formal PolicyEngine.

        This is the security boundary between the LLM planner and system execution.
        All commands, arguments, network egress, filesystem access, and code
        semantics are validated against the agent's AgentCapabilities manifest.
        """
        result = self.policy.validate(command)
        if not result.allowed:
            logger.warning(
                "policy_engine_denied",
                command=command[:80],
                reason=result.reason,
            )
        return result.allowed

    def _register_computer_use_skills(self):
        """Registers native PC control skills (Computer Use)."""
        @self.skill("execute_bash", "Ejecuta un comando bash seguro dentro del sandbox.")
        def execute_bash(command: str) -> str:
            if not self.permissions.execute_commands:
                return "Error: permiso 'execute_commands' está deshabilitado."

            # Rate limit check
            try:
                self.governor.check_skill_call(self.id, "execute_bash")
            except RateLimitExceeded as e:
                return f"Error: [RATE_LIMIT] {e}"

            # 🛡️ CAPA 1: TOOL GOVERNOR
            # Formamos el contexto en base al estado y memoria actual
            csl_context = {
                "task_type": "user_requested_file_read" if "read" in command else "general_query",
                "is_debug_mode": False,  # Determinado por permisos extendidos
                "allow_sensitive_access": False
            }
            allowed, reason = self.csl.tool_governor.approve("execute_bash", {"command": command}, csl_context, self.csl.state)
            if not allowed:
                logger.warning("execute_bash_denied_cognitive", command=command[:80], reason=reason)
                return f"Error: [COGNITIVE_SECURITY] {reason}"

            # PolicyEngine validates command, args, network, filesystem, and semantics
            # Crucially, we pass the current taint status to block exfiltration
            is_tainted = self.execution_context.holds_sensitive_data
            result = self.policy.validate(command, is_tainted=is_tainted)
            if not result.allowed:
                logger.warning(
                    "execute_bash_denied",
                    command=command[:80],
                    reason=result.reason,
                )
                return f"Error: [POLICY] {result.reason}"

            # Multi-Layer Execution Authority
            risk = self.risk_engine.evaluate(command, self.execution_context)

            # Cross-Layer Consistency Check (catches desync edge cases)
            if not self.risk_engine.cross_layer_verify(result.allowed, risk, command):
                return f"Error: [CROSS_LAYER] Risk override blocked '{command[:40]}' despite policy approval."

            if not self.execution_gate.approve(command, risk):
                return f"Error: [EXECUTION_GATE] CRITICAL action denied. Missing Multi-Sig or Oracle approval."

            try:
                try:
                    argv = shlex.split(command, posix=True)
                except ValueError as e:
                    return f"Error: comando mal formado: {e}"
                if not argv:
                    return {"error": "comando vacío tras parseo."}
                
                # Ejecutamos el comando
                exec_result = self._execute_in_runtime(command, command)
                
                # 🛡️ CAPA 2: OUTPUT GUARD (DLP)
                # Formateamos la salida (puede ser str o dict)
                raw_output = exec_result.get("output", str(exec_result)) if isinstance(exec_result, dict) else str(exec_result)
                
                allowed, safe_response, reason = self.csl.output_guard.validate(raw_output, csl_context, self.csl.state)
                if not allowed:
                    if isinstance(exec_result, dict):
                        exec_result["output"] = safe_response
                    else:
                        exec_result = safe_response
                    logger.warning("output_guard_intervened", reason=reason)
                
                return exec_result
                
            except subprocess.TimeoutExpired:
                return {"error": f"Comando expiró después de {self.capabilities.max_cpu_seconds} segundos."}
            except Exception as e:
                return {"error": f"Error inesperado: {str(e)[:500]}"}

        @self.skill(
            "execute_bash_argv",
            "Ejecuta en sandbox con argv tipado (lista de strings); preferido frente a execute_bash(str).",
        )
        def execute_bash_argv(argv: List[str]) -> str:
            if not self.permissions.execute_commands:
                return "Error: permiso 'execute_commands' está deshabilitado."
            if not argv:
                return "Error: argv vacío."
            for part in argv:
                if "\x00" in part:
                    return "Error: byte NUL en argumento no permitido."

            try:
                self.governor.check_skill_call(self.id, "execute_bash_argv")
            except RateLimitExceeded as e:
                return f"Error: [RATE_LIMIT] {e}"

            command = " ".join(shlex.quote(p) for p in argv)  # for logging/risk only
            is_tainted = self.execution_context.holds_sensitive_data
            # D-02 FIX: Use typed validate_argv() — no string→split round-trip.
            result = self.policy.validate_argv(argv, is_tainted=is_tainted)
            if not result.allowed:
                logger.warning("execute_bash_argv_denied", command=command[:80], reason=result.reason)
                return f"Error: [POLICY] {result.reason}"

            risk = self.risk_engine.evaluate(command, self.execution_context)
            if not self.risk_engine.cross_layer_verify(result.allowed, risk, command):
                return f"Error: [CROSS_LAYER] Risk override blocked '{command[:40]}' despite policy approval."
            if not self.execution_gate.approve(command, risk):
                return f"Error: [EXECUTION_GATE] CRITICAL action denied. Missing Multi-Sig or Oracle approval."

            try:
                return self._execute_in_runtime(command, command)
            except subprocess.TimeoutExpired:
                return {"error": f"Comando expiró después de {self.capabilities.max_cpu_seconds} segundos."}
            except Exception as e:
                return {"error": f"Error inesperado: {str(e)[:500]}"}

        @self.skill("mouse_click", "Click a specific coordinate on the screen.")
        def mouse_click(x: int, y: int) -> str:
            if not self.permissions.gui_automation:
                return "Error: GUI automation permission is disabled."
            return f"Clicked at ({x}, {y})"

        @self.skill("send_a2a_message", "Sends a cryptographically signed message to another agent.")
        def send_a2a_message(target_id: str, payload: str) -> str:
            if not self.permissions.network_access:
                return "Error: Network access disabled."
                
            # Distribute Taint: Message inherits agent's current highest sensitivity
            msg_sensitivity = DataSensitivity.PUBLIC
            if self.execution_context.holds_sensitive_data:
                msg_sensitivity = DataSensitivity.INTERNAL

            # C-03: Ed25519 via cryptography only (same stack as identity.sign_message_bytes).
            if not self._private_key:
                return "Error: clave privada no disponible. No se puede firmar."

            nonce = uuid.uuid4().hex
            ts_ms = int(time.time() * 1000)
            tenant_id = getattr(self.config, "tenant_id", None) or "default"
            canonical = _a2a_canonical_signing_bytes(
                self.id,
                tenant_id,
                target_id,
                payload,
                msg_sensitivity.value,
                ts_ms,
                nonce,
            )
            sig_hex = sign_message_bytes(canonical, self._private_key)
            signature = bytes.fromhex(sig_hex)

            # Build A2A Message (sender_id = canonical agent_id, never self.name)
            msg = A2AMessage(
                sender_id=self.id,
                tenant_id=tenant_id,
                target_id=target_id,
                payload=payload,
                sensitivity=msg_sensitivity,
                signature=signature,
                timestamp_ms=ts_ms,
                nonce=nonce,
            )
            
            # Simulated network dispatch...
            logger.info(
                "a2a_message_dispatched",
                target=target_id,
                sensitivity=msg_sensitivity.name,
                nonce_prefix=nonce[:8],
            )
            return f"Message sent securely to {target_id}."

        # Sub-Agent Delegation
        self._delegation_manager = None

    def sell_idle_compute(self, minutes: int):
        """Mock method for GTM Demo: Agent sells compute and earns KERN."""
        import time
        from kernell_sdk.security.ssrf import create_safe_client
        earned = minutes * 0.52
        self.wallet.credit(earned)
        logger.info(f"Earned {earned} KERN selling idle compute.")

        try:
            self.governor.check_webhook(self.id)
        except RateLimitExceeded:
            return earned  # Silently skip webhook if rate limited
        
        try:
            with create_safe_client(agent_id=self.id, timeout=2.0) as client:
                client.post("http://localhost:8000/event", json={
                    "type": "EARN",
                    "agent_id": self.id,
                    "payload": {
                        "amount": earned,
                        "source": "idle_compute",
                        "minutes": minutes
                    }
                })
        except Exception as e:
            import logging
            logging.warning(f'Suppressed error in {__name__}: {e}')
        return earned

    def pay_peer(self, target: str, amount: float, task: str):
        """Mock method for GTM Demo: Agent pays another agent for a task via Escrow."""
        import time
        from kernell_sdk.security.ssrf import create_safe_client
        if not self.wallet.debit(amount):
            logger.error("Insufficient KERN to pay peer.")
            return False
            
        logger.info(f"Paid {amount} KERN to {target} for: {task}")
        
        try:
            with create_safe_client(agent_id=self.id, timeout=2.0) as client:
                client.post("http://localhost:8000/event", json={
                    "type": "SPEND",
                    "agent_id": self.id,
                    "payload": {
                        "amount": amount,
                        "target": target,
                        "task": task
                    }
                })
        except Exception as e:
            import logging
            logging.warning(f'Suppressed error in {__name__}: {e}')
        return True

    def receive_a2a_message(self, message: A2AMessage) -> bool:
        """Processes incoming A2A messages with mandatory signature verification.
        
        Security invariant (C-02): Messages with invalid or missing signatures
        are REJECTED. No taint propagation occurs without verified identity.
        """
        # C-02 / E3: skew, signature, then nonce (no taint before all pass).
        try:
            self._a2a_replay_guard.assert_timestamp_skew(message.timestamp_ms)
        except A2AReplayError as e:
            logger.error("a2a_rejected_time_skew", sender=message.sender_id, reason=str(e))
            return False

        canonical = _a2a_canonical_signing_bytes(
            message.sender_id,
            message.tenant_id,
            message.target_id,
            message.payload,
            message.sensitivity.value,
            message.timestamp_ms,
            message.nonce,
        )

        # Lookup sender's public key from passport or registry (by canonical agent_id)
        sender_pub_key = self._resolve_sender_public_key(message.sender_id)
        if not sender_pub_key:
            logger.error(
                "a2a_rejected_unknown_sender",
                sender=message.sender_id,
            )
            return False

        if not verify_signature_bytes(canonical, message.signature.hex(), sender_pub_key):
            logger.error(
                "a2a_rejected_invalid_signature",
                sender=message.sender_id,
            )
            return False

        try:
            self._a2a_replay_guard.consume_nonce(message.nonce)
        except A2AReplayError as e:
            logger.error("a2a_rejected_replay", sender=message.sender_id, reason=str(e))
            return False
        
        # OBLIGATORY TAINT PROPAGATION (only after verified identity):
        if message.sensitivity > DataSensitivity.PUBLIC:
            logger.warning(
                "agent_tainted_by_a2a",
                sender=message.sender_id,
                sensitivity=message.sensitivity.name
            )
            self.execution_context.holds_sensitive_data = True
            
        return True

    def _resolve_sender_public_key(self, sender_id: str) -> Optional[str]:
        """Resolve a sender's public key from the identity registry or local cache."""
        # Try the IdentityRegistry (Redis-backed) first
        if hasattr(self, 'memory') and self.memory.is_connected:
            try:
                from .identity import IdentityRegistry
                registry = IdentityRegistry(self.memory._redis)
                entry = registry.lookup(sender_id)
                if entry:
                    return entry.get("public_key_hex")
            except Exception as e:
                import logging
                logging.warning(f'Suppressed error in {__name__}: {e}')
        return None

    def enable_delegation(self, max_workers: int, worker_engine: 'BaseLLMProvider', timeout: float = 60.0):
        """Enable local sub-agent delegation."""
        from .delegation import SubAgentManager
        self._delegation_manager = SubAgentManager(self)
        self._delegation_manager.enable(max_workers=max_workers, worker_engine=worker_engine, timeout=timeout)

    def disable_delegation(self):
        """Disable local sub-agent delegation."""
        if self._delegation_manager:
            self._delegation_manager.disable()

    def delegate_batch(self, tasks: list[str], max_concurrent: int = None) -> list[str]:
        """Delegate a batch of tasks to the local sub-agent swarm."""
        if not self._delegation_manager or not self._delegation_manager.is_enabled():
            raise RuntimeError("Delegation is not enabled. Call enable_delegation() first.")
        return self._delegation_manager.execute_batch(tasks, max_concurrent)

    def skill(self, name: str = None, description: str = None):
        """Decorator to register a custom skill (tool) for the agent."""
        def decorator(func: Callable):
            skill_name = name or func.__name__
            skill_desc = description or func.__doc__ or f"Execute {skill_name}"

            sig = inspect.signature(func)
            props = {}
            required = []

            for param_name, param in sig.parameters.items():
                param_type = "string"
                if param.annotation == int: param_type = "integer"
                elif param.annotation == float: param_type = "number"
                elif param.annotation == bool: param_type = "boolean"

                props[param_name] = {"type": param_type}
                if param.default == inspect.Parameter.empty:
                    required.append(param_name)

            schema = {
                "name": skill_name,
                "description": skill_desc,
                "input_schema": {
                    "type": "object",
                    "properties": props,
                    "required": required
                }
            }

            validated_func = validate_call(func)
            self._skills[skill_name] = validated_func
            self._skill_schemas.append(schema)
            return validated_func
        return decorator

    def toggle_permission(self, permission: str, state: bool):
        """Runtime switch to turn permissions ON/OFF dynamically via GUI."""
        # SECURITY: Whitelist-only permission names
        if permission not in VALID_PERMISSIONS:
            logger.error(f"[SECURITY] Rejected invalid permission name: {permission}")
            return

        if hasattr(self.permissions, permission):
            setattr(self.permissions, permission, state)
            logger.info(f"[AUDIT] Permission '{permission}' set to {state}")
        else:
            logger.error(f"Unknown permission: {permission}")

    def _estimate_difficulty(self, task: str) -> str:
        """Heuristic to determine task difficulty to save tokens (Sectorization)."""
        length = len(task.split())
        if length < 10 and "analyze" not in task.lower():
            return "easy" # Route to Llama-3-8B or Haiku
        elif length < 50:
            return "medium" # Route to Claude 3.5 Haiku / Sonnet
        else:
            return "hard" # Route to Opus or deep thinking models

    def prompt(self, task: str) -> str:
        """Executes a task with Advanced RAG and Task Sectorization."""
        self.state.status = "working"

        if not self.permissions.network_access:
            logger.warning("Network access disabled. Agent is running in offline local LLM mode.")

        # Task Sectorization: Route by difficulty to save tokens
        difficulty = self._estimate_difficulty(task)
        logger.info(f"Task Sectorization: '{task[:20]}...' classified as {difficulty.upper()} difficulty.")

        # Advanced RAG: Condense context instead of appending everything
        context = self.memory.summarize_context(max_tokens=300)
        self.memory.add_episodic("task_started", {"task": task, "difficulty": difficulty})

        # TODO: Route to appropriate model based on 'difficulty'
        response = f"[Execution output for '{task}'. Model: {difficulty}_tier. Skills: {list(self._skills.keys())}]"

        # 🛡️ ANTI-STREAMING BUFFER: OutputGuard validates the COMPLETE response
        # before it reaches the caller. This prevents partial-token leakage
        # when the LLM streams sensitive data before validation can intercept.
        csl_context = {"task_type": "general_query", "is_debug_mode": False, "allow_sensitive_access": False}
        allowed, safe_response, reason = self.csl.output_guard.validate(response, csl_context, self.csl.state)
        if not allowed:
            logger.warning("prompt_output_guard_intervened", reason=reason)
            response = safe_response

        self.state.tasks_completed += 1
        self.state.status = "idle"
        self.memory.add_episodic("task_completed", {"task": task, "status": "success"})

        return response

    def install(self):
        """Builds the container and sets up the environment."""
        self.sandbox.start()

    def run(self, task: str = None):
        """
        Universal entry point. 
        If task is given, it analyzes and routes via the Capability Layer (Adapters).
        If none, it starts the idle daemon.
        """
        if not task:
            logger.info(f"Agent {self.name} is live in daemon mode.")
            return

        logger.info(f"Agent {self.name} routing task via CapabilityRouter: {task[:50]}")
        
        context = {
            "execution_context": self.execution_context,
            "policy_engine": self.policy
        }
        
        result = self.router.route_and_execute(task, context)
        
        # Dispatch event for Moltbook Feed if an adapter was used
        if result.get("used_adapter") and result.get("used_adapter") != "none":
            try:
                from kernell_sdk.security.ssrf import create_safe_client
                with create_safe_client(agent_id=self.id, timeout=2.0) as client:
                    client.post("http://localhost:8000/event", json={
                    "type": "ADAPTER_USE",
                    "agent_id": self.id,
                    "payload": {
                        "adapter": result["used_adapter"],
                        "task": task[:100],
                        "status": result.get("status")
                    }
                }, timeout=2)
            except Exception as e:
                import logging
                logging.warning(f'Suppressed error in {__name__}: {e}')
                
        return result

    def shutdown(self):
        """Graceful shutdown: stop sandbox, close wallet, flush memory."""
        logger.info(f"Shutting down agent {self.name}...")
        self.sandbox.stop()
        self.wallet.close()
        logger.info(f"Agent {self.name} stopped.")
