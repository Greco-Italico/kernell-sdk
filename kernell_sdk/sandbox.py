import shutil
"""
Kernell OS SDK — Containerized Execution & Resource Management
══════════════════════════════════════════════════════════════
Handles the secure, isolated execution of the agent on Windows/Linux
using Docker. Allows assigning specific resources (RAM, CPU, Disk)
and managing permission boundaries.

SECURITY:
  - Never mounts root filesystem (/) — only user-approved directories
  - Enforces disk quotas
  - Drops all capabilities by default
  - Prevents privilege escalation
"""
import subprocess
import logging
from typing import Dict, List, Optional
from pathlib import Path
from pydantic import BaseModel, Field

logger = logging.getLogger("kernell.sandbox")

# Docker image digest for supply chain verification
AGENT_BASE_IMAGE_TAG = "kernell/agent-base:latest"  # Para referencia humana
# ↓ Usar este en producción (inmutable, no puede ser reemplazado silenciosamente)
AGENT_BASE_IMAGE = "kernell/agent-base@sha256:34a57c2f12b3b22305db83db1e2aa58fb12907dd428d6f22f3ebc6e4e260d926"

def _verify_image_integrity() -> bool:
    """
    Verifica que la imagen Docker local coincide con el digest esperado.
    Llama esto antes de start() para detectar imágenes comprometidas.
    """
    assert "REEMPLAZAR" not in AGENT_BASE_IMAGE, "Falta pin de SHA256 para Docker (KOS-015)"
    try:
        result = subprocess.run(
            ["/usr/bin/docker", "inspect", "--format={{index .RepoDigests 0}}", AGENT_BASE_IMAGE_TAG],
            capture_output=True, text=True, timeout=15
        )
        if result.returncode != 0:
            logger.error("No se pudo inspeccionar la imagen Docker")
            return False

        actual_ref = result.stdout.strip()
        expected_sha = AGENT_BASE_IMAGE.split("@")[-1]
        
        # Debe coincidir de forma exacta
        if not actual_ref.endswith(expected_sha):
            logger.critical(
                f"⚠️  ALERTA DE SEGURIDAD: Digest de imagen no coincide!\n"
                f"   Esperado exacto: {expected_sha}\n"
                f"   Actual completo: {actual_ref}"
            )
            return False
        return True
    except subprocess.TimeoutExpired:
        logger.error("Timeout verificando imagen Docker")
        return False


class ResourceLimits(BaseModel):
    ram_mb: int = Field(default=512, ge=256, le=65536)
    cpu_cores: float = Field(default=1.0, ge=0.25, le=32.0)
    disk_gb: int = Field(default=10, ge=1, le=500)
    runtime: str = Field(default="runsc", description="Container runtime (e.g., 'runsc' for gVisor or 'runc')")


class AgentPermissions(BaseModel):
    network_access: bool = True
    file_system_read: bool = True
    file_system_write: bool = False
    execute_commands: bool = False
    browser_control: bool = False
    gui_automation: bool = False  # Full computer use

    # Allowed filesystem paths (never mount / wholesale)
    allowed_paths: List[str] = Field(default_factory=lambda: [
        str(Path.home() / "Documents"),
        str(Path.home() / "Downloads"),
    ])


class Sandbox:
    """Manages the Docker container for the agent."""
    def __init__(self, agent_id: str, limits: ResourceLimits, permissions: AgentPermissions):
        self.agent_id = agent_id
        self.limits = limits
        self.permissions = permissions
        self.container_name = f"kernell_agent_{self.agent_id}"

    def _validate_mount_path(self, path: str) -> None:
        """Validates a host path before mounting it in the container.

        Security notes:
        - Null byte injection prevention
        - URL-encoding traversal decoding
        - Resolved AND raw path are both checked to prevent symlink bypass
          (e.g. /run/docker.sock → /var/run/docker.sock on modern Linux)
        """
        from pathlib import Path
        from urllib.parse import unquote

        # Null byte injection
        if '\x00' in path:
            raise ValueError(f"Null byte in path: {path!r}")

        # URL encoding traversal
        decoded = unquote(path)

        resolved = Path(decoded).resolve()
        # Also check the raw (unresolved) path to catch symlink targets
        # that may not match the resolved form on all distros.
        raw = Path(decoded)

        forbidden_prefixes = [
            "/etc", "/root", "/var", "/sys",
            "/dev", "/boot", "/usr/lib", "/proc", "/run",
        ]
        # Explicit file block-list: covers both canonical paths and symlink aliases.
        # /run/docker.sock is the resolved path on modern Linux (systemd);
        # /var/run/docker.sock is the legacy symlink — both must be blocked.
        forbidden_files = {
            "/var/run/docker.sock",
            "/run/docker.sock",
        }

        if str(resolved) == "/":
            raise PermissionError("Mounting root filesystem is forbidden")

        # Check both resolved and raw path against prefixes
        for p in (str(resolved), str(raw)):
            if any(p.startswith(prefix) for prefix in forbidden_prefixes):
                raise PermissionError(f"Mounting path {resolved} is forbidden")

        # Check both resolved and raw path against explicit block-list
        if str(resolved) in forbidden_files or str(raw) in forbidden_files:
            raise PermissionError(f"Mounting {resolved} is explicitly blocked")

    def _build_docker_args(self) -> List[str]:
        args = [
            "/usr/bin/docker", "run", "-d",
            "--name", self.container_name,
            "--runtime", self.limits.runtime,
            "--memory", f"{self.limits.ram_mb}m",
            "--memory-swap", f"{self.limits.ram_mb}m",  # No swap — prevent OOM abuse
            "--cpus", str(self.limits.cpu_cores),
            "--pids-limit", "64",  # Prevent fork bombs
            # Disk quota enforcement
            "--storage-opt", f"size={self.limits.disk_gb}g",
            # ── Attack #4 Mitigation: Kernel-Level Isolation ─────────
            "--security-opt=no-new-privileges",       # Block privilege escalation
            "--security-opt", f"seccomp={Path(__file__).parent / 'seccomp_agent.json'}",
            "--cap-drop=ALL",                          # Drop ALL Linux capabilities
            "--read-only",                             # Read-only root filesystem
            "--ipc=none",                              # No shared memory (prevents ptrace attacks)
            "--user", "1000:1000",                     # Run as non-root user
            # C-09 FIX: NEVER mount host /proc — exposes env vars, PIDs, memory maps of all host processes
            # Use 'docker stats' externally if process info is needed
            # tmpfs for writable areas inside the container
            "--tmpfs", "/tmp:rw,noexec,nosuid,size=64m",
            "--tmpfs", "/run:rw,noexec,nosuid,size=16m",
        ]

        # Network isolation
        if not self.permissions.network_access:
            args.extend(["--network", "none"])

        # Mount ONLY allowed paths (NEVER mount /)
        if self.permissions.file_system_read or self.permissions.file_system_write:
            mode = "rw" if self.permissions.file_system_write else "ro"
            for host_path in self.permissions.allowed_paths:
                try:
                    self._validate_mount_path(host_path)
                except Exception as e:
                    logger.error(f"SECURITY: Refused to mount sensitive host path: {host_path} - {e}")
                    continue
                
                resolved_path = Path(host_path).resolve()
                host_path_str = str(resolved_path)
                

                container_path = f"/workspace/{resolved_path.name}"
                args.extend(["-v", f"{host_path_str}:{container_path}:{mode}"])

        # For full computer use, we need X11/Wayland socket access
        if self.permissions.gui_automation:
            import os
            if os.environ.get("ALLOW_UNSAFE_GUI") != "1":
                raise PermissionError("GUI automation exposes X11 sockets (Escape Risk). Set ALLOW_UNSAFE_GUI=1 to bypass.")
            display = os.environ.get("DISPLAY", ":0")
            args.extend([
                "-e", f"DISPLAY={display}",
                "-v", "/tmp/.X11-unix:/tmp/.X11-unix:ro"
            ])
            
        import os
        auth_key = os.environ.get("KERNELL_SANDBOX_AUTH_KEY")
        if auth_key:
             args.extend(["-e", f"KERNELL_SANDBOX_AUTH_KEY={auth_key}"])

        args.append(AGENT_BASE_IMAGE)
        return args

    def start(self) -> bool:
        """Deploys the agent in the container."""
        logger.info(f"Deploying agent {self.agent_id} in isolated sandbox...")
        try:
            # Remove existing container if present
            subprocess.run(
                ["/usr/bin/docker", "rm", "-f", self.container_name],
                capture_output=True, timeout=10
            )

            if not _verify_image_integrity():
                logger.error("Sandbox deployment aborted due to image integrity verification failure.")
                return False

            cmd = self._build_docker_args()
            logger.info(f"Docker command: {' '.join(cmd[:6])}...")  # Log truncated for security
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=60)

            if result.returncode == 0:
                logger.info(f"Sandbox started successfully: {result.stdout.strip()[:12]}...")
                return True
            else:
                logger.error(f"Failed to start sandbox: {result.stderr}")
                return False
        except FileNotFoundError:
            logger.error("Docker is not installed or not running.")
            return False
        except subprocess.TimeoutExpired:
            logger.error("Docker command timed out.")
            return False

    def stop(self):
        """Terminates the sandbox gracefully."""
        try:
            subprocess.run(
                ["/usr/bin/docker", "stop", self.container_name],
                capture_output=True, timeout=30
            )
            subprocess.run(
                ["/usr/bin/docker", "rm", self.container_name],
                capture_output=True, timeout=10
            )
            logger.info(f"Sandbox {self.container_name} stopped and removed.")
        except subprocess.TimeoutExpired:
            # Force kill
            subprocess.run(
                ["/usr/bin/docker", "kill", self.container_name],
                capture_output=True
            )
            logger.warning(f"Sandbox {self.container_name} force-killed.")
