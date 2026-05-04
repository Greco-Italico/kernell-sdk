"""
docker_runtime.py — Kernell OS SDK
Fix #1: Docker runtime con hardening completo.

Mejoras sobre la versión original:
  - --cap-drop=ALL (elimina todas las Linux capabilities)
  - --security-opt=no-new-privileges (bloquea escalada de privilegios)
  - --user=65534:65534 (nobody:nogroup — sin root)
  - --tmpfs /tmp:rw,noexec,nosuid,size=64m (tmp sin ejecución)
  - Límites de output y timeout explícitos
  - Validación AST antes de enviar el código al contenedor
"""

from __future__ import annotations

import shutil
import subprocess
import tempfile
import textwrap
import uuid
from pathlib import Path
from typing import Optional

from .sandbox_validator import SandboxViolation, validate_code
from .models import ExecutionRequest, ExecutionResult

# ---------------------------------------------------------------------------
# Constantes de límites (Fix #4)
# ---------------------------------------------------------------------------

MAX_OUTPUT_BYTES: int = 1_000_000   # 1 MB de output máximo
DEFAULT_TIMEOUT_SEC: int = 30       # timeout por defecto
DEFAULT_MEMORY: str = "256m"
DEFAULT_CPUS: str = "1.0"
DEFAULT_PIDS: int = 64
SANDBOX_IMAGE: str = "python:3.12-slim"

# ---------------------------------------------------------------------------
# Flags base de hardening Docker
# ---------------------------------------------------------------------------

_HARDENING_FLAGS: list[str] = [
    "--read-only",                              # filesystem de solo lectura
    "--network=none",                           # sin red
    "--cap-drop=ALL",                           # eliminar TODAS las capabilities
    "--security-opt=no-new-privileges",         # bloquear setuid/setgid
    # "--security-opt=seccomp={Path(__file__).parent / 'seccomp.json'}", # Temporarily using Docker default seccomp
    "--user=65534:65534",                       # nobody:nogroup
    "--tmpfs=/tmp:rw,noexec,nosuid,size=64m",   # /tmp sin exec
]


# ---------------------------------------------------------------------------
# Runtime
# ---------------------------------------------------------------------------

class DockerRuntime:
    """
    Ejecuta código Python dentro de un contenedor Docker endurecido.

    Uso:
        runtime = DockerRuntime(timeout=15, memory="128m")
        req = ExecutionRequest(code=user_code)
        result = runtime.execute(req)
        print(result.stdout)
    """

    def __init__(
        self,
        image: str = SANDBOX_IMAGE,
        timeout: int = DEFAULT_TIMEOUT_SEC,
        memory: str = DEFAULT_MEMORY,
        cpus: str = DEFAULT_CPUS,
        pids_limit: int = DEFAULT_PIDS,
        extra_flags: Optional[list[str]] = None,
    ) -> None:
        self.image = image
        self.timeout = timeout
        self.memory = memory
        self.cpus = cpus
        self.pids_limit = pids_limit
        self.extra_flags = extra_flags or []

        _check_docker_available()

    # ------------------------------------------------------------------

    def execute(self, request: ExecutionRequest) -> ExecutionResult:
        """
        Valida y ejecuta `request.code` dentro del sandbox Docker.

        Raises:
            SandboxViolation: si el código no pasa la validación AST.
            DockerRuntimeError: si Docker falla al lanzar el contenedor.
        """
        # 1. Validación AST antes de tocar Docker
        validation = validate_code(request.code)
        if not validation.valid:
            raise SandboxViolation(validation)

        # 2. Escribir código a archivo temporal (evita inyección via args)
        with tempfile.NamedTemporaryFile(
            suffix=".py", prefix="kap_", delete=False, mode="w", encoding="utf-8"
        ) as tmp:
            tmp.write(request.code)
            tmp_path = Path(tmp.name)
            
        import os
        os.chmod(tmp_path, 0o644)

        try:
            container_name = f"kap-sandbox-{uuid.uuid4().hex[:12]}"
            cmd = self._build_command(tmp_path, container_name)

            proc = subprocess.run(
                cmd,
                capture_output=True,
                timeout=request.timeout or self.timeout,
                check=False,  # manejamos returncode manualmente
            )

            stdout = _truncate(proc.stdout, MAX_OUTPUT_BYTES)
            stderr = _truncate(proc.stderr, MAX_OUTPUT_BYTES)

            return ExecutionResult(
                exit_code=proc.returncode,
                stdout=stdout.decode("utf-8", errors="replace"),
                stderr=stderr.decode("utf-8", errors="replace"),
                timed_out=False,
            )

        except subprocess.TimeoutExpired:
            _force_kill_container(container_name)
            return ExecutionResult(
                exit_code=-1,
                stdout="",
                stderr=f"Timeout: ejecución superó {self.timeout}s",
                timed_out=True,
            )

        except FileNotFoundError as exc:
            raise DockerRuntimeError(
                "Docker no encontrado. ¿Está instalado y en PATH?"
            ) from exc

        finally:
            tmp_path.unlink(missing_ok=True)

    # ------------------------------------------------------------------

    def _build_command(self, code_path: Path, container_name: str) -> list[str]:
        return [
            "docker", "run",
            "--rm",
            "--name", container_name,
            # Hardening flags
            *_HARDENING_FLAGS,
            # Límites de recursos
            f"--memory={self.memory}",
            f"--memory-swap={self.memory}",   # swap = memory → sin swap
            f"--cpus={self.cpus}",
            f"--pids-limit={self.pids_limit}",
            # Flags adicionales opcionales
            *self.extra_flags,
            # Montar el código como volumen de solo lectura
            "-v", f"{code_path}:/sandbox/code.py:ro",
            # Imagen y comando
            self.image,
            "python3", "/sandbox/code.py",
        ]


# ---------------------------------------------------------------------------
# Excepciones
# ---------------------------------------------------------------------------

class DockerRuntimeError(RuntimeError):
    """Error al lanzar o comunicarse con Docker."""


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _check_docker_available() -> None:
    try:
        docker_bin = shutil.which("docker")
        if not docker_bin: raise RuntimeError("docker not found in PATH")
        subprocess.run([docker_bin, "info"],
            capture_output=True,
            timeout=5,
            check=True,
        )
    except (FileNotFoundError, subprocess.CalledProcessError) as exc:
        raise DockerRuntimeError(
            "Docker no disponible. Verifica que el daemon esté corriendo."
        ) from exc


def _truncate(data: bytes, max_bytes: int) -> bytes:
    """
    Trunca output de Docker sin cortar secuencias JSON ni UTF-8 a la mitad.

    Estrategia:
      1. Si el output cabe, retorna sin modificar.
      2. Si no cabe, busca el último newline antes del límite para cortar
         en un límite de línea seguro (evita romper JSON objects parciales).
      3. Añade un marcador legible que indica el truncado (parseable como
         línea de texto, no rompe consumidores que iteran por líneas).
    """
    if len(data) <= max_bytes:
        return data

    # Intentar cortar en el último salto de línea dentro del límite
    # para no romper líneas JSON a la mitad
    cutoff = max_bytes - 128  # dejar espacio para el marcador
    last_newline = data.rfind(b"\n", 0, cutoff)
    cut_at = last_newline if last_newline > cutoff // 2 else cutoff

    marker = (
        b"\n"
        b"# [KERNELL-TRUNCATED] "
        + f"output cortado en {cut_at:,} de {len(data):,} bytes total".encode()
        + b"\n"
    )
    return data[:cut_at] + marker


def _force_kill_container(name: str) -> None:
    docker_bin = shutil.which("docker")
    if not docker_bin: raise RuntimeError("docker not found in PATH")
    subprocess.run([docker_bin, "kill", name],
        capture_output=True,
        timeout=5,
        check=False,
    )
