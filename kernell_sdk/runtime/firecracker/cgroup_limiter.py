"""
Kernell OS SDK — cgroups v2 Resource Limiter for Firecracker VMMs
══════════════════════════════════════════════════════════════════
Hard resource limits per VMM process to prevent a single VM from
starving the host. Uses cgroups v2 unified hierarchy.

Limits enforced:
  - CPU quota (fractional vCPU cap)
  - Memory max (hard OOM kill boundary)
  - PIDs max (fork bomb protection)
"""
import os
import logging

logger = logging.getLogger("kernell.cgroups")

CGROUP_ROOT = "/sys/fs/cgroup"


class CgroupLimitError(Exception):
    pass


class VMResourceLimiter:
    """
    Manages a cgroup v2 scope for a single Firecracker VMM process.
    
    Usage:
        limiter = VMResourceLimiter("vm-abc123", cpu_quota=1.0, memory_mb=256, pids_max=64)
        limiter.create()
        limiter.attach(pid)
        # ... VM runs ...
        limiter.destroy()
    """

    def __init__(
        self,
        vm_id: str,
        cpu_quota: float = 1.0,
        memory_mb: int = 256,
        pids_max: int = 64,
    ):
        self.vm_id = vm_id
        self.cpu_quota = cpu_quota
        self.memory_mb = memory_mb
        self.pids_max = pids_max
        self.cgroup_path = os.path.join(CGROUP_ROOT, "kernell", f"vm-{vm_id}")

    def create(self) -> None:
        """Create the cgroup directory and write resource limits."""
        try:
            os.makedirs(self.cgroup_path, exist_ok=True)
        except OSError as e:
            raise CgroupLimitError(f"Cannot create cgroup for {self.vm_id}: {e}")

        # CPU quota: period=100000us, quota = cpu_quota * period
        period_us = 100_000
        quota_us = int(self.cpu_quota * period_us)
        self._write("cpu.max", f"{quota_us} {period_us}")

        # Memory hard limit
        mem_bytes = self.memory_mb * 1024 * 1024
        self._write("memory.max", str(mem_bytes))

        # Disable swap for deterministic OOM behavior
        self._write("memory.swap.max", "0")

        # PIDs limit (fork bomb defense)
        self._write("pids.max", str(self.pids_max))

        logger.info(
            "cgroup_created",
            vm_id=self.vm_id,
            cpu_quota=self.cpu_quota,
            memory_mb=self.memory_mb,
            pids_max=self.pids_max,
        )

    def attach(self, pid: int) -> None:
        """Move a process into this cgroup."""
        self._write("cgroup.procs", str(pid))
        logger.info("process_attached", vm_id=self.vm_id, pid=pid)

    def destroy(self) -> None:
        """Remove the cgroup after the VM is cleaned up."""
        try:
            if os.path.exists(self.cgroup_path):
                os.rmdir(self.cgroup_path)
                logger.info("cgroup_destroyed", vm_id=self.vm_id)
        except OSError:
            # cgroup may still have zombie refs; log but don't crash
            logger.warning("cgroup_destroy_failed", vm_id=self.vm_id)

    def _write(self, filename: str, value: str) -> None:
        path = os.path.join(self.cgroup_path, filename)
        try:
            with open(path, "w") as f:
                f.write(value)
        except OSError as e:
            # Non-fatal in dev/test (no cgroups v2 available)
            logger.debug("cgroup_write_skipped", file=filename, error=str(e))
