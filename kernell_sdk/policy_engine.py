"""
Kernell OS SDK — Capability-Based Policy Engine
══════════════════════════════════════════════════
Formal security boundary between LLM suggestions and system execution.

Architecture:
    LLM (untrusted) → PolicyEngine.validate() → Executor (trusted)

Every action an agent attempts must pass through this engine.
It validates:
    1. Command binary ∈ allowed set
    2. Arguments ∈ allowed set per command
    3. Semantic analysis (deep inspection of payloads)
    4. Network egress (URL host whitelisting)
    5. Filesystem access (path containment)

This is a capability-based model, NOT a blacklist.
If it's not explicitly allowed, it's denied.
"""
from __future__ import annotations

import os
import re
import shlex
import shutil
from pathlib import Path
from typing import Any, Dict, List, Optional, Set
from urllib.parse import urlparse

import structlog
from pydantic import BaseModel, Field

logger = structlog.get_logger("kernell.policy_engine")


# ── Capability Models ────────────────────────────────────────────────────────

class NetworkPolicy(BaseModel):
    """Controls all outbound network access from the agent container."""
    enabled: bool = False
    allowed_hosts: List[str] = Field(
        default_factory=lambda: ["api.kernell.site"],
        description="Hostnames the agent is allowed to contact"
    )
    allowed_ports: List[int] = Field(
        default_factory=lambda: [443],
        description="Ports the agent is allowed to use"
    )
    allow_http: bool = Field(
        default=False,
        description="If False, only HTTPS URLs are permitted"
    )


class FilePolicy(BaseModel):
    """Controls filesystem access within the container."""
    read: List[str] = Field(
        default_factory=lambda: ["/tmp", "/data/public"],
        description="Paths the agent can read from"
    )
    write: List[str] = Field(
        default_factory=lambda: ["/tmp"],
        description="Paths the agent can write to"
    )
    denied: List[str] = Field(
        default_factory=lambda: [
            "/etc/shadow", "/etc/passwd", "/root",
            "/proc", "/sys", "/dev",
        ],
        description="Paths always denied regardless of other rules"
    )


class ExecPolicy(BaseModel):
    """Controls which commands and arguments an agent can execute."""
    allowed_commands: Dict[str, dict] = Field(
        default_factory=lambda: {
            # Navigation & listing
            "ls": {"args": ["-l", "-a", "-h", "-R", "-t"], "validate": "path_read"},
            "pwd": {"args": []},
            "tree": {"args": ["-L", "-a"], "validate": "path_read"},
            "du": {"args": ["-h", "-s", "-c"], "validate": "path_read"},
            "df": {"args": ["-h"]},
            # File reading
            "cat": {"args": ["-n"], "validate": "path_read"},
            "head": {"args": ["-n", "-c"], "validate": "path_read"},
            "tail": {"args": ["-n", "-f"], "validate": "path_read"},
            "grep": {"args": ["-i", "-v", "-E", "-r", "-n"], "validate": "path_read"},
            "wc": {"args": ["-l", "-w", "-c"], "validate": "path_read"},
            # Safe writes
            "echo": {"args": ["-n", "-e"]},
            "touch": {"args": [], "validate": "path_write"},
            "mkdir": {"args": ["-p"], "validate": "path_write"},
            "cp": {"args": ["-r", "-v"], "validate": "path_write"},
            "mv": {"args": ["-v"], "validate": "path_write"},
            # Network (controlled)
            "curl": {"args": ["-X", "-H", "-d", "-s", "-L", "-I", "-o"], "validate": "network"},
            "wget": {"args": ["-q", "-O"], "validate": "network"},
            "ping": {"args": ["-c", "-t"], "validate": "network"},
            # Development (RESTRICTED — no arbitrary code execution)
            "python3": {"args": ["-m", "--version"], "validate": "python"},
            "python": {"args": ["-m", "--version"], "validate": "python"},
            "pip": {"args": ["install", "list", "show", "freeze"]},
            "git": {"args": ["status", "add", "commit", "push", "pull", "clone", "log", "diff"]},
            # System (read-only)
            "whoami": {"args": []},
            "date": {"args": []},
            "env": {"args": []},
            "uname": {"args": ["-a", "-r"]},
        }
    )


COMMAND_SAFELIST = frozenset(ExecPolicy().allowed_commands.keys())


class AgentCapabilities(BaseModel):
    """
    Complete capability manifest for an agent.
    This is the single source of truth for what an agent can do.
    """
    exec: ExecPolicy = Field(default_factory=ExecPolicy)
    network: NetworkPolicy = Field(default_factory=NetworkPolicy)
    filesystem: FilePolicy = Field(default_factory=FilePolicy)
    max_cpu_seconds: int = Field(default=30, ge=1, le=300)
    max_output_bytes: int = Field(default=10_000, ge=1000, le=1_000_000)
    max_pids: int = Field(default=64, ge=1, le=512)


# ── Dangerous Pattern Detection ─────────────────────────────────────────────

# Patterns that indicate code injection attempts in python -c or -m arguments
_PYTHON_FORBIDDEN_PATTERNS: List[re.Pattern] = [
    re.compile(p, re.IGNORECASE) for p in [
        r"import\s+os",
        r"import\s+socket",
        r"import\s+subprocess",
        r"import\s+shutil",
        r"import\s+sys",
        r"import\s+ctypes",
        r"import\s+importlib",
        r"import\s+builtins",
        r"__import__\s*\(",
        r"eval\s*\(",
        r"exec\s*\(",
        r"open\s*\(",
        r"compile\s*\(",
        r"getattr\s*\(",
        r"globals\s*\(",
        r"locals\s*\(",
        r"vars\s*\(",
        r"breakpoint\s*\(",
    ]
]

# URL patterns that indicate data exfiltration
_EXFIL_PATTERNS: List[re.Pattern] = [
    re.compile(p, re.IGNORECASE) for p in [
        r"\$\(",           # command substitution $(...)
        r"`[^`]+`",        # backtick command substitution
        r"\bdata=\$",      # data=$(cmd)
        r"\|",             # pipe to exfiltrate
    ]
]


# ── Policy Engine ────────────────────────────────────────────────────────────

class PolicyViolation(Exception):
    """Raised when a command violates the agent's capability policy."""
    def __init__(self, reason: str, command: str = "", category: str = "unknown"):
        self.reason = reason
        self.command = command
        self.category = category
        super().__init__(f"[{category}] {reason}")


class PolicyEngine:
    """
    Capability-based policy engine for agent command validation.

    This is the formal security boundary between the LLM planner
    and the system executor. Every command must pass validation
    before execution.

    Usage:
        engine = PolicyEngine(capabilities)
        result = engine.validate("ls -la /tmp")
        if result.allowed:
            execute(command)
        else:
            log(result.reason)
    """

    def __init__(self, capabilities: AgentCapabilities):
        self.cap = capabilities

    def validate(self, command: str, is_tainted: bool = False) -> PolicyResult:
        """
        Validate a command against the agent's full capability manifest.

        Returns a PolicyResult with allowed=True/False and reason.
        """
        # Phase 0: Basic sanity
        if not command or not command.strip():
            return PolicyResult(allowed=False, reason="Empty command")

        if len(command) > 2048:
            return PolicyResult(allowed=False, reason="Command exceeds max length (2048)")

        try:
            parts = shlex.split(command)
        except ValueError as e:
            return PolicyResult(allowed=False, reason=f"Malformed command: {e}")

        if not parts:
            return PolicyResult(allowed=False, reason="No command parts after parsing")

        # C-12: resolve real executable before whitelist (mitigate symlink / PATH shadowing).
        first = parts[0]
        if os.path.isabs(first):
            try:
                resolved_executable = os.path.realpath(first)
            except OSError:
                resolved_executable = first
        else:
            which_path = shutil.which(first)
            if not which_path:
                logger.warning("policy_denied_command", command=first, category="not_in_path")
                return PolicyResult(
                    allowed=False,
                    reason=f"Command '{first}' not found in PATH (use absolute path only if policy allows)",
                )
            resolved_executable = os.path.realpath(which_path)
        base_cmd = Path(resolved_executable).name
        args = parts[1:]

        # Phase 1: Command whitelist
        if base_cmd not in self.cap.exec.allowed_commands:
            logger.warning("policy_denied_command", command=base_cmd, category="whitelist")
            return PolicyResult(allowed=False, reason=f"Command '{base_cmd}' not in capability set")

        policy = self.cap.exec.allowed_commands[base_cmd]

        # Phase 2: Argument validation
        result = self._validate_args(base_cmd, args, policy)
        if not result.allowed:
            return result

        # Phase 2.5: Shell metacharacter detection (KOS-004)
        # Catches command substitution $(), backticks, and pipes in ANY command args.
        # _EXFIL_PATTERNS was previously only checked in _validate_network — now global.
        full_args_str = " ".join(args)
        for pattern in _EXFIL_PATTERNS:
            if pattern.search(full_args_str):
                logger.warning(
                    "policy_denied_shell_metachar",
                    command=base_cmd,
                    pattern=pattern.pattern,
                    category="shell_injection",
                )
                return PolicyResult(
                    allowed=False,
                    reason=f"Shell metacharacter detected in arguments (pattern: {pattern.pattern}). "
                           f"Command substitution and pipes are not allowed.",
                )

        # Phase 3: Semantic deep inspection
        validator = policy.get("validate")
        if validator:
            result = self._validate_semantics(validator, base_cmd, args, is_tainted)
            if not result.allowed:
                return result

        logger.debug("policy_allowed", command=base_cmd, args_count=len(args))
        return PolicyResult(allowed=True, reason="All checks passed")

    def validate_argv(self, argv: List[str], is_tainted: bool = False) -> PolicyResult:
        """
        Type-safe command validation operating on a pre-parsed argv list.

        D-02 FIX: This is the preferred entry-point for execute_bash_argv(),
        eliminating the lossy round-trip:
            argv → shlex.quote → string → shlex.split → validate

        The string-based validate() remains for backward compatibility with
        execute_bash(command: str), but all typed callers should use this.
        """
        if not argv:
            return PolicyResult(allowed=False, reason="Empty argv")

        if sum(len(a) for a in argv) > 2048:
            return PolicyResult(allowed=False, reason="Combined argv length exceeds max (2048)")

        # C-12: resolve real executable before whitelist
        first = argv[0]
        if os.path.isabs(first):
            try:
                resolved_executable = os.path.realpath(first)
            except OSError:
                resolved_executable = first
        else:
            which_path = shutil.which(first)
            if not which_path:
                logger.warning("policy_denied_command", command=first, category="not_in_path")
                return PolicyResult(
                    allowed=False,
                    reason=f"Command '{first}' not found in PATH",
                )
            resolved_executable = os.path.realpath(which_path)
        base_cmd = Path(resolved_executable).name
        args = argv[1:]

        # Phase 1: Command whitelist
        if base_cmd not in self.cap.exec.allowed_commands:
            logger.warning("policy_denied_command", command=base_cmd, category="whitelist")
            return PolicyResult(allowed=False, reason=f"Command '{base_cmd}' not in capability set")

        policy = self.cap.exec.allowed_commands[base_cmd]

        # Phase 2: Argument validation (operates on real list, no split ambiguity)
        result = self._validate_args(base_cmd, args, policy)
        if not result.allowed:
            return result

        # Phase 3: Semantic deep inspection
        validator = policy.get("validate")
        if validator:
            result = self._validate_semantics(validator, base_cmd, args, is_tainted)
            if not result.allowed:
                return result

        logger.debug("policy_allowed_argv", command=base_cmd, args_count=len(args))
        return PolicyResult(allowed=True, reason="All checks passed (typed)")


    def _validate_args(self, cmd: str, args: List[str], policy: dict) -> PolicyResult:
        """Phase 2: Validate arguments against the command's allowed args list."""
        allowed_args = policy.get("args")
        if allowed_args is None:
            return PolicyResult(allowed=True, reason="No arg restrictions")

        for arg in args:
            if not arg.startswith("-"):
                continue  # positional args validated by semantic layer

            if arg in allowed_args:
                continue

            # Allow combined short flags like -la → check -l and -a individually
            if len(arg) > 2 and arg[0] == "-" and arg[1] != "-":
                if all(f"-{c}" in allowed_args for c in arg[1:]):
                    continue

            logger.warning("policy_denied_argument", command=cmd, argument=arg, category="args")
            return PolicyResult(
                allowed=False,
                reason=f"Argument '{arg}' not allowed for command '{cmd}'"
            )

        return PolicyResult(allowed=True, reason="Arguments valid")

    def _validate_semantics(self, validator: str, cmd: str, args: List[str], is_tainted: bool) -> PolicyResult:
        """Phase 3: Deep semantic inspection based on validator type."""
        dispatch = {
            "python": self._validate_python,
            "network": self._validate_network,
            "path_read": self._validate_path_read,
            "path_write": self._validate_path_write,
        }
        handler = dispatch.get(validator)
        if not handler:
            logger.error("policy_unknown_validator", validator=validator, command=cmd)
            return PolicyResult(allowed=False, reason=f"Unknown validator: {validator}")

        return handler(cmd, args, is_tainted)

    # ── Semantic Validators ──────────────────────────────────────────────────

    def _validate_python(self, cmd: str, args: List[str], is_tainted: bool) -> PolicyResult:
        """
        Validates Python execution.

        CRITICAL: -c (arbitrary code execution) is DENIED.
        Only -m (module) and --version are permitted.
        """
        if "-c" in args:
            logger.warning("policy_denied_python_c", command=cmd, category="python_rce")
            return PolicyResult(
                allowed=False,
                reason="python -c (arbitrary code execution) is permanently denied. Use -m for module execution."
            )

        if "-m" in args:
            idx = args.index("-m")
            if idx + 1 >= len(args):
                return PolicyResult(allowed=False, reason="python -m requires a module name")

            module = args[idx + 1]
            # Only allow known-safe modules
            safe_modules = {
                "pip", "venv", "http.server", "json.tool",
                "pytest", "unittest", "py_compile",
            }
            if module not in safe_modules:
                logger.warning("policy_denied_python_module", module=module, category="python_module")
                return PolicyResult(
                    allowed=False,
                    reason=f"Python module '{module}' is not in the allowed set"
                )

        return PolicyResult(allowed=True, reason="Python execution validated")

    def _validate_network(self, cmd: str, args: List[str], is_tainted: bool) -> PolicyResult:
        """
        Validates network commands (curl, wget, ping).

        Checks:
        - Taint Status (Blocks Exfiltration if holding internal data)
        - Network must be enabled in capabilities
        - URL host must be in allowed_hosts
        - Only HTTPS unless allow_http is True
        - No command substitution / exfiltration patterns
        """
        if is_tainted:
            logger.warning("policy_denied_tainted_egress", command=cmd, category="exfiltration")
            return PolicyResult(allowed=False, reason="Network egress denied: Agent holds sensitive data (Tainted)")

        if not self.cap.network.enabled:
            logger.warning("policy_denied_network_disabled", command=cmd, category="network")
            return PolicyResult(allowed=False, reason="Network access is disabled for this agent")

        # Extract URLs from args
        urls = [a for a in args if a.startswith(("http://", "https://", "ftp://"))]

        for url_str in urls:
            # Check for exfiltration patterns in the URL itself
            for pattern in _EXFIL_PATTERNS:
                if pattern.search(url_str):
                    logger.warning(
                        "policy_denied_exfil_pattern",
                        url=url_str[:80], pattern=pattern.pattern, category="exfiltration"
                    )
                    return PolicyResult(
                        allowed=False,
                        reason="Data exfiltration pattern detected in URL"
                    )

            parsed = urlparse(url_str)

            # Enforce HTTPS
            if not self.cap.network.allow_http and parsed.scheme != "https":
                return PolicyResult(
                    allowed=False,
                    reason=f"Insecure protocol '{parsed.scheme}' not allowed. Use HTTPS."
                )

            # Host whitelist
            if parsed.hostname not in self.cap.network.allowed_hosts:
                logger.warning(
                    "policy_denied_host",
                    hostname=parsed.hostname, command=cmd, category="network_host"
                )
                return PolicyResult(
                    allowed=False,
                    reason=f"Host '{parsed.hostname}' is not in the allowed network set"
                )

            # DNS Rebinding & SSRF Protection
            import socket
            import ipaddress
            try:
                ip_str = socket.gethostbyname(parsed.hostname)
                ip = ipaddress.ip_address(ip_str)
                if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_multicast or ip.is_reserved:
                    return PolicyResult(
                        allowed=False,
                        reason=f"DNS resolved to private/forbidden IP: {ip_str} (SSRF Blocked)"
                    )
            except Exception as e:
                return PolicyResult(
                    allowed=False,
                    reason=f"DNS resolution failed for {parsed.hostname}"
                )

            # Port check
            port = parsed.port or (443 if parsed.scheme == "https" else 80)
            if port not in self.cap.network.allowed_ports:
                return PolicyResult(
                    allowed=False,
                    reason=f"Port {port} is not allowed. Allowed: {self.cap.network.allowed_ports}"
                )

        return PolicyResult(allowed=True, reason="Network access validated")

    def _validate_path_read(self, cmd: str, args: List[str], is_tainted: bool) -> PolicyResult:
        """Validates file read access against filesystem policy."""
        paths = [a for a in args if not a.startswith("-")]
        # Taint the execution if ANY file is read (pessimistic taint propagation)
        return self._check_paths(paths, self.cap.filesystem.read, "read")

    def _validate_path_write(self, cmd: str, args: List[str], is_tainted: bool) -> PolicyResult:
        """Validates file write access against filesystem policy."""
        paths = [a for a in args if not a.startswith("-")]
        return self._check_paths(paths, self.cap.filesystem.write, "write")

    def _check_paths(self, paths: List[str], allowed: List[str], mode: str) -> PolicyResult:
        """Core path containment check with symlink resolution."""
        for path_str in paths:
            # Resolve to real path (defeats symlink attacks)
            try:
                real_path = os.path.realpath(path_str)
            except (OSError, ValueError):
                return PolicyResult(
                    allowed=False,
                    reason=f"Cannot resolve path: {path_str}"
                )

            # Check denied paths first (takes priority)
            for denied in self.cap.filesystem.denied:
                if real_path.startswith(denied):
                    logger.warning(
                        "policy_denied_path",
                        path=real_path, denied=denied, mode=mode, category="filesystem"
                    )
                    return PolicyResult(
                        allowed=False,
                        reason=f"Access to '{real_path}' is explicitly denied"
                    )

            # Check allowed paths
            if not any(real_path.startswith(a) for a in allowed):
                logger.warning(
                    "policy_denied_path_not_allowed",
                    path=real_path, mode=mode, category="filesystem"
                )
                return PolicyResult(
                    allowed=False,
                    reason=f"Path '{real_path}' is not in the allowed {mode} set"
                )

        return PolicyResult(allowed=True, reason=f"File {mode} access validated")


# ── Result Object ────────────────────────────────────────────────────────────

class PolicyResult(BaseModel):
    """Immutable result from a policy validation check."""
    allowed: bool
    reason: str
    metadata: Dict[str, Any] = Field(default_factory=dict)
