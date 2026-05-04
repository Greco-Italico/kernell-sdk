import subprocess
import os
import signal
import logging
from typing import Dict, Any

try:
    import resource
    HAS_RESOURCE = True
except ImportError:
    HAS_RESOURCE = False

from ..identity import SecurityError
from .base import BaseRuntime
from .models import ExecutionRequest, ExecutionResult
from .sandbox import SandboxFS, validate_code
from .errors import ExecutionTimeout

logger = logging.getLogger("kernell.subprocess_runtime")


def _is_production_env() -> bool:
    return os.getenv("KERNELL_ENV", "").strip().lower() == "production"


class SubprocessRuntime(BaseRuntime):
    """
    Runtime Fase 1A: Ejecución aislada nativa usando subprocess + flags de Python.
    """
    def __init__(self, allow_insecure_exec: bool = False, *args, **kwargs):
        if _is_production_env():
            raise SecurityError(
                "SubprocessRuntime is forbidden when KERNELL_ENV=production "
                "(unsafe native exec; use DockerRuntime / FirecrackerRuntime)."
            )
        if not allow_insecure_exec:
            raise RuntimeError("CRITICAL SECURITY: SubprocessRuntime with exec() is disabled for production. Use DockerRuntime or pass allow_insecure_exec=True for testing/opt-in.")
        logger.warning(
            "unsafe_subprocess_runtime_initialized",
            extra={"allow_insecure_exec": True, "KERNELL_ENV": os.getenv("KERNELL_ENV", "unset")},
        )
        super().__init__(*args, **kwargs)

    def _limit_resources(self, request: ExecutionRequest):
        """Aplica límites de sistema (solo en Linux/Unix)."""
        if not HAS_RESOURCE:
            return
            
        try:
            # Limitar memoria
            mem_bytes = request.memory_limit_mb * 1024 * 1024
            resource.setrlimit(resource.RLIMIT_AS, (mem_bytes, mem_bytes))

            # Limitar CPU (segundos de tiempo de CPU)
            # Esto dispara SIGXCPU si se pasa
            cpu_limit = int(request.timeout) + 1
            resource.setrlimit(resource.RLIMIT_CPU, (cpu_limit, cpu_limit))
            
            # Limitar número de procesos (evitar fork bombs)
            resource.setrlimit(resource.RLIMIT_NPROC, (10, 10))
            
            # Limitar tamaño de archivo creado
            resource.setrlimit(resource.RLIMIT_FSIZE, (10 * 1024 * 1024, 10 * 1024 * 1024)) # 10MB
        except (ValueError, OSError):
            pass

    def _drop_privileges(self):
        """Drops privileges to a non-root user if running as root."""
        if hasattr(os, 'getuid') and os.getuid() == 0:
            try:
                # Intenta cambiar al usuario 'nobody' (UID genérico 65534)
                # En un entorno real, crearíamos un usuario 'kernell-jail'
                os.setgid(65534)
                os.setuid(65534)
            except OSError as e:
                import logging
                logging.warning(f'Suppressed error in {__name__}: {e}')

    def execute(self, request: ExecutionRequest) -> ExecutionResult:
        if _is_production_env():
            raise SecurityError("SubprocessRuntime.execute blocked: KERNELL_ENV=production.")
        if os.environ.get("KERNELL_ALLOW_UNSAFE_SUBPROCESS_RUNTIME") != "1":
            raise RuntimeError(
                "SubprocessRuntime.execute blocked: set KERNELL_ALLOW_UNSAFE_SUBPROCESS_RUNTIME=1 "
                "for explicit non-production consent."
            )
        
        validate_code(request.code)

        NSJAIL_PATH = "/usr/bin/nsjail"
        # Determine config path based on installation layout
        sdk_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        NSJAIL_CONFIG = os.path.join(os.path.dirname(sdk_root), "nsjail.cfg")
        if not os.path.exists(NSJAIL_CONFIG):
            NSJAIL_CONFIG = "nsjail.cfg"  # fallback for testing

        import tempfile
        with tempfile.NamedTemporaryFile(delete=False, suffix=".py") as f:
            f.write(request.code.encode())
            tmp_file = f.name

        try:
            result = subprocess.run(
                [
                    NSJAIL_PATH,
                    "--config",
                    NSJAIL_CONFIG,
                    "--",
                    "python3",
                    tmp_file,
                ],
                capture_output=True,
                text=True,
                timeout=request.timeout,
            )

            return ExecutionResult(
                stdout=result.stdout,
                stderr=result.stderr,
                exit_code=result.returncode
            )

        except subprocess.TimeoutExpired as e:
            return ExecutionResult(
                stdout=e.stdout.decode() if e.stdout else "",
                stderr=e.stderr.decode() if e.stderr else "TimeoutExpired",
                exit_code=-1,
                timed_out=True
            )
        finally:
            if os.path.exists(tmp_file):
                os.remove(tmp_file)
