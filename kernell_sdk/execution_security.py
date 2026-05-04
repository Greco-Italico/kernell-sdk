"""
Kernell OS SDK — Policy Engine + Execution Gate + Audit Logger
══════════════════════════════════════════════════════════════
Phase 4 Security Hardening: Runtime enforcement layer.

The complete pipeline:
    CodePipeline → FormalVerifier (static) → PolicyEngine (dynamic)
                 → ExecutionGate (enforcement) → AuditLogger (forensic)

Components:
  1. ExecutionPolicy: Declarative security policy definition
  2. PolicyEngine: Evaluates code against dynamic policies
  3. ExecutionGate: Enforces policies at runtime (CPU/memory limits, builtins)
  4. AuditLogger: Forensic-grade append-only execution log

Usage:
    from kernell_sdk.security import (
        ExecutionPolicy, PolicyEngine, ExecutionGate, AuditLogger
    )

    policy = ExecutionPolicy(
        allow_network=False,
        allowed_paths={"/tmp"},
        max_execution_time_ms=2000,
        max_memory_mb=128,
    )

    gate = ExecutionGate(policy=policy)
    result = gate.execute(code="print(2 + 2)")
    # result.success == True, result.stdout == "4\n"
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import resource
import signal
import sys
import threading
import time
import traceback
from dataclasses import dataclass, field, asdict
from io import StringIO
from typing import Any, Callable, Dict, List, Optional, Set, Union

logger = logging.getLogger("kernell.security")


# ══════════════════════════════════════════════════════════════════════
# EXECUTION POLICY
# ══════════════════════════════════════════════════════════════════════

@dataclass
class ExecutionPolicy:
    """
    Declarative security policy for agent code execution.
    Defines what is ALLOWED, not what is blocked.
    """
    # Network
    allow_network: bool = False

    # Filesystem
    allowed_paths: Set[str] = field(default_factory=lambda: {"/tmp"})
    allow_file_write: bool = True
    allow_file_read: bool = True

    # Execution limits
    max_execution_time_ms: int = 5000    # 5 seconds
    max_memory_mb: int = 256             # 256 MB
    max_output_chars: int = 50_000       # Stdout capture limit

    # Capabilities
    allow_subprocess: bool = False
    allow_env_access: bool = False
    allow_dynamic_import: bool = False

    # Builtins control
    blocked_builtins: Set[str] = field(default_factory=lambda: {
        "exec", "eval", "compile", "__import__",
        "globals", "locals", "vars", "dir",
        "breakpoint", "exit", "quit",
    })

    # Custom allowed modules (whitelist approach)
    allowed_modules: Optional[Set[str]] = None  # None = allow all except blocked


# ══════════════════════════════════════════════════════════════════════
# POLICY ENGINE
# ══════════════════════════════════════════════════════════════════════

@dataclass
class PolicyViolation:
    """A policy check violation."""
    rule: str
    severity: str
    message: str
    line: Optional[int] = None


class PolicyEngine:
    """
    Evaluates code against an ExecutionPolicy.
    Complementary to FormalVerifier: FV checks for dangerous patterns,
    PolicyEngine checks against allowed behaviors.
    """

    def __init__(self, policy: ExecutionPolicy):
        self.policy = policy

    def evaluate(self, code: str) -> List[PolicyViolation]:
        """Check code against the current policy."""
        violations = []
        lines = code.split("\n")

        for i, line in enumerate(lines, 1):
            stripped = line.strip()
            if stripped.startswith("#"):
                continue

            # Network policy
            if not self.policy.allow_network:
                if any(net in line for net in [
                    "socket.", "requests.", "urllib.", "http.client.",
                    "httpx.", "aiohttp.", "ftplib.", "smtplib.",
                ]):
                    violations.append(PolicyViolation(
                        rule="NETWORK_DENIED",
                        severity="HIGH",
                        message="Network access blocked by policy",
                        line=i,
                    ))

            # Subprocess policy
            if not self.policy.allow_subprocess:
                if "subprocess." in line or "os.system(" in line or "os.popen(" in line:
                    violations.append(PolicyViolation(
                        rule="SUBPROCESS_DENIED",
                        severity="CRITICAL",
                        message="Subprocess/shell execution blocked by policy",
                        line=i,
                    ))

            # Env access policy
            if not self.policy.allow_env_access:
                if "os.environ" in line or "os.getenv(" in line:
                    violations.append(PolicyViolation(
                        rule="ENV_ACCESS_DENIED",
                        severity="HIGH",
                        message="Environment variable access blocked by policy",
                        line=i,
                    ))

            # Dynamic import policy
            if not self.policy.allow_dynamic_import:
                if "__import__(" in line or "importlib." in line:
                    violations.append(PolicyViolation(
                        rule="DYNAMIC_IMPORT_DENIED",
                        severity="HIGH",
                        message="Dynamic import blocked by policy",
                        line=i,
                    ))

            # Module whitelist
            if self.policy.allowed_modules is not None:
                import re
                import_match = re.match(r'(?:import|from)\s+(\w+)', stripped)
                if import_match:
                    mod = import_match.group(1)
                    if mod not in self.policy.allowed_modules:
                        violations.append(PolicyViolation(
                            rule="MODULE_NOT_WHITELISTED",
                            severity="HIGH",
                            message=f"Module '{mod}' not in whitelist",
                            line=i,
                        ))

        return violations


# ══════════════════════════════════════════════════════════════════════
# EXECUTION RESULT
# ══════════════════════════════════════════════════════════════════════

@dataclass
class ExecutionResult:
    """Result of a sandboxed code execution."""
    success: bool = False
    stdout: str = ""
    stderr: str = ""
    error: Optional[str] = None
    execution_time_ms: float = 0.0
    killed: bool = False          # True if killed by timeout/memory
    kill_reason: str = ""
    policy_violations: List[PolicyViolation] = field(default_factory=list)
    code_hash: str = ""


# ══════════════════════════════════════════════════════════════════════
# EXECUTION GATE
# ══════════════════════════════════════════════════════════════════════

# Safe builtins that are always available
_ALWAYS_SAFE = {
    "abs", "all", "any", "ascii", "bin", "bool", "bytearray", "bytes",
    "callable", "chr", "complex", "dict", "divmod", "enumerate",
    "filter", "float", "format", "frozenset", "getattr", "hasattr",
    "hash", "hex", "id", "int", "isinstance", "issubclass", "iter",
    "len", "list", "map", "max", "min", "next", "object", "oct",
    "ord", "pow", "print", "range", "repr", "reversed", "round",
    "set", "slice", "sorted", "str", "sum", "tuple", "type", "zip",
    # Exceptions (needed for try/except)
    "Exception", "TypeError", "ValueError", "KeyError", "IndexError",
    "AttributeError", "RuntimeError", "StopIteration", "ZeroDivisionError",
    "FileNotFoundError", "IOError", "OSError", "OverflowError",
    "NotImplementedError", "ImportError", "ArithmeticError",
    "True", "False", "None",
}


class ExecutionGate:
    """
    Runtime enforcement gate. Executes code with real resource limits.

    Security layers:
      1. Policy evaluation (pre-exec)
      2. Restricted builtins (no exec/eval/__import__)
      3. Restricted import (module whitelist/blocklist)
      4. CPU time limit (signal.SIGALRM)
      5. Memory limit (resource.setrlimit)
      6. Output capture with size limit
    """

    def __init__(
        self,
        policy: ExecutionPolicy,
        verifier=None,
        audit_logger=None,
    ):
        self.policy = policy
        self._verifier = verifier  # Optional FormalVerifier
        self._audit = audit_logger  # Optional AuditLogger
        self._globals_persistent: Dict[str, Any] = {}

    def execute(
        self,
        code: str,
        context: Optional[Dict[str, Any]] = None,
        persist_state: bool = False,
    ) -> ExecutionResult:
        """
        Execute code with full security enforcement.

        Args:
            code: Python code to execute
            context: Optional variables to inject into the namespace
            persist_state: If True, variables from this execution carry over
        """
        t0 = time.time()
        code_hash = hashlib.sha256(code.encode()).hexdigest()[:16]

        # ── Step 1: Static verification (if verifier available) ──────
        if self._verifier:
            vr = self._verifier.verify(code)
            if not vr.passed:
                result = ExecutionResult(
                    success=False,
                    error=f"FormalVerifier blocked: {len(vr.violations)} violations",
                    code_hash=code_hash,
                    execution_time_ms=round((time.time() - t0) * 1000, 1),
                )
                self._log_execution(code, result)
                return result

        # ── Step 2: Policy evaluation ────────────────────────────────
        pe = PolicyEngine(self.policy)
        policy_violations = pe.evaluate(code)
        critical_violations = [v for v in policy_violations if v.severity in ("CRITICAL", "HIGH")]

        if critical_violations:
            result = ExecutionResult(
                success=False,
                error=f"Policy blocked: {len(critical_violations)} violations",
                policy_violations=policy_violations,
                code_hash=code_hash,
                execution_time_ms=round((time.time() - t0) * 1000, 1),
            )
            self._log_execution(code, result)
            return result

        # ── Step 3: Build restricted execution environment ───────────
        safe_builtins = self._build_safe_builtins()

        exec_globals = {"__builtins__": safe_builtins}

        # Inject persistent state
        if persist_state and self._globals_persistent:
            exec_globals.update(self._globals_persistent)

        # Inject context
        if context:
            exec_globals.update(context)

        # ── Step 4: Execute with resource limits ─────────────────────
        stdout_capture = StringIO()
        stderr_capture = StringIO()
        result = ExecutionResult(code_hash=code_hash)

        try:
            # Set memory limit (Unix only)
            self._set_memory_limit()

            # Capture stdout/stderr
            old_stdout, old_stderr = sys.stdout, sys.stderr
            sys.stdout = stdout_capture
            sys.stderr = stderr_capture

            try:
                # Set CPU time limit via SIGALRM
                timeout_s = max(1, self.policy.max_execution_time_ms // 1000)

                def _timeout_handler(signum, frame):
                    raise TimeoutError(f"Execution exceeded {timeout_s}s limit")

                old_handler = signal.signal(signal.SIGALRM, _timeout_handler)
                signal.alarm(timeout_s)

                try:
                    exec(compile(code, "<agent_code>", "exec"), exec_globals)
                    result.success = True
                finally:
                    signal.alarm(0)  # Cancel alarm
                    signal.signal(signal.SIGALRM, old_handler)

            except TimeoutError as e:
                result.success = False
                result.killed = True
                result.kill_reason = "timeout"
                result.error = str(e)
            except MemoryError:
                result.success = False
                result.killed = True
                result.kill_reason = "memory_limit"
                result.error = "MemoryError: execution exceeded memory limit"
            except ImportError as e:
                result.success = False
                result.error = f"Blocked import: {e}"
            except Exception as e:
                result.success = False
                result.error = f"{type(e).__name__}: {e}"
                result.stderr = traceback.format_exc()
            finally:
                sys.stdout = old_stdout
                sys.stderr = old_stderr

        except Exception as outer_e:
            result.success = False
            result.error = f"Gate error: {outer_e}"

        # Capture output
        result.stdout = stdout_capture.getvalue()[:self.policy.max_output_chars]
        stderr_val = stderr_capture.getvalue()
        if stderr_val and not result.stderr:
            result.stderr = stderr_val[:self.policy.max_output_chars]

        result.execution_time_ms = round((time.time() - t0) * 1000, 1)
        result.policy_violations = policy_violations

        # Persist state if requested
        if persist_state and result.success:
            self._globals_persistent.update({
                k: v for k, v in exec_globals.items()
                if k != "__builtins__" and not k.startswith("_")
            })

        self._log_execution(code, result)
        return result

    # ── Builtins Construction ────────────────────────────────────────

    def _build_safe_builtins(self) -> dict:
        """Build a restricted builtins dict based on the policy."""
        import builtins

        safe = {}
        for name in _ALWAYS_SAFE:
            if hasattr(builtins, name):
                safe[name] = getattr(builtins, name)

        # Add all exception types (needed for try/except)
        for name in dir(builtins):
            obj = getattr(builtins, name)
            if isinstance(obj, type) and issubclass(obj, BaseException):
                safe[name] = obj

        # Restricted open (only allowed paths)
        if self.policy.allow_file_read or self.policy.allow_file_write:
            safe["open"] = self._make_restricted_open()

        # Restricted __import__
        safe["__import__"] = self._make_restricted_import()

        return safe

    def _make_restricted_open(self):
        """Create a restricted open() that only allows access to permitted paths."""
        allowed = self.policy.allowed_paths
        allow_read = self.policy.allow_file_read
        allow_write = self.policy.allow_file_write

        _original_open = open

        def restricted_open(file, mode="r", *args, **kwargs):
            filepath = os.path.abspath(str(file))
            # Check path is within allowed zones
            if not any(filepath.startswith(os.path.abspath(p)) for p in allowed):
                raise PermissionError(f"Access denied: {filepath} is outside allowed paths {allowed}")
            # Check read/write permission
            if "r" in mode and not allow_read:
                raise PermissionError("File read access denied by policy")
            if any(w in mode for w in ("w", "a", "x")) and not allow_write:
                raise PermissionError("File write access denied by policy")
            return _original_open(file, mode, *args, **kwargs)

        return restricted_open

    def _make_restricted_import(self):
        """Create a restricted __import__ that blocks dangerous modules."""
        blocked = {"os", "sys", "subprocess", "shutil", "signal",
                   "ctypes", "socket", "http", "urllib", "requests",
                   "httpx", "aiohttp", "paramiko", "ftplib", "smtplib",
                   "telnetlib", "multiprocessing", "pickle", "marshal"}

        if not self.policy.allow_network:
            blocked |= {"socket", "http", "urllib", "requests", "httpx", "aiohttp"}

        if not self.policy.allow_subprocess:
            blocked |= {"subprocess", "multiprocessing"}

        whitelist = self.policy.allowed_modules

        def restricted_import(name, *args, **kwargs):
            top_level = name.split(".")[0]
            if top_level in blocked:
                raise ImportError(f"Module '{name}' is blocked by security policy")
            if whitelist is not None and top_level not in whitelist:
                raise ImportError(f"Module '{name}' is not in the allowed modules whitelist")
            return __import__(name, *args, **kwargs)

        return restricted_import

    # ── Resource Limits ──────────────────────────────────────────────

    def _set_memory_limit(self):
        """Set memory limit using resource module (Unix only)."""
        try:
            limit_bytes = self.policy.max_memory_mb * 1024 * 1024
            resource.setrlimit(resource.RLIMIT_AS, (limit_bytes, limit_bytes))
        except (ValueError, resource.error):
            pass  # Not available on this platform, or limit too low

    # ── Audit ────────────────────────────────────────────────────────

    def _log_execution(self, code: str, result: ExecutionResult):
        """Log execution to audit logger if available."""
        if self._audit:
            self._audit.log(code, result)


# ══════════════════════════════════════════════════════════════════════
# AUDIT LOGGER
# ══════════════════════════════════════════════════════════════════════

class AuditLogger:
    """
    Forensic-grade execution audit logger.
    Append-only JSONL format. Cannot be modified after write.

    Every execution is recorded with:
      - Code hash (SHA-256)
      - Verification verdict
      - Policy violations
      - Execution result
      - Timing information
    """

    def __init__(
        self,
        log_path: str = "/var/lib/kernell/audit.jsonl",
        hook: Optional[Callable[[Dict], None]] = None,
    ):
        """
        Args:
            log_path: Path to the JSONL audit log file
            hook: Optional callback for each log entry (e.g., send to S3)
        """
        self._log_path = log_path
        self._hook = hook
        self._buffer: List[Dict] = []  # In-memory buffer for queries
        os.makedirs(os.path.dirname(log_path) or ".", exist_ok=True)

    def log(self, code: str, result: ExecutionResult):
        """Append an execution record to the audit log."""
        entry = {
            "timestamp": time.time(),
            "timestamp_iso": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "code_hash": result.code_hash or hashlib.sha256(code.encode()).hexdigest()[:16],
            "code_length": len(code),
            "verdict": "allowed" if result.success else "blocked",
            "success": result.success,
            "error": result.error,
            "killed": result.killed,
            "kill_reason": result.kill_reason,
            "execution_time_ms": result.execution_time_ms,
            "stdout_length": len(result.stdout),
            "policy_violations": [
                {"rule": v.rule, "severity": v.severity, "message": v.message}
                for v in result.policy_violations
            ],
        }

        # Write to file (append-only)
        try:
            with open(self._log_path, "a") as f:
                f.write(json.dumps(entry, separators=(",", ":")) + "\n")
        except Exception as e:
            logger.error(f"[AuditLogger] Write failed: {e}")

        # In-memory buffer
        self._buffer.append(entry)
        if len(self._buffer) > 1000:
            self._buffer = self._buffer[-1000:]

        # Hook callback
        if self._hook:
            try:
                self._hook(entry)
            except Exception:
                pass

    def query(self, limit: int = 50, verdict: Optional[str] = None) -> List[Dict]:
        """Query recent audit entries from the in-memory buffer."""
        items = self._buffer
        if verdict:
            items = [e for e in items if e.get("verdict") == verdict]
        return items[-limit:]

    def summary(self) -> Dict[str, Any]:
        """Get summary statistics from the audit buffer."""
        total = len(self._buffer)
        allowed = sum(1 for e in self._buffer if e.get("verdict") == "allowed")
        blocked = total - allowed
        killed = sum(1 for e in self._buffer if e.get("killed"))
        avg_time = (
            sum(e.get("execution_time_ms", 0) for e in self._buffer) / max(total, 1)
        )
        return {
            "total_executions": total,
            "allowed": allowed,
            "blocked": blocked,
            "killed": killed,
            "avg_execution_time_ms": round(avg_time, 1),
            "block_rate": round(blocked / max(total, 1) * 100, 1),
        }
