import shutil
import subprocess
import uuid
import os
import time
import logging
from kernell_sdk.security.ssrf import create_uds_client
from .integrity import verify_artifacts, IntegrityError
from .cgroup_limiter import VMResourceLimiter

logger = logging.getLogger("kernell.firecracker.manager")
class FirecrackerManager:
    """
    Manager to orchestrate Firecracker MicroVMs.
    Handles VM lifecycle: start, configure API socket, and cleanup.
    """

    def __init__(self, kernel_path: str, rootfs_path: str, artifact_manifest: dict = None):
        self.kernel = kernel_path
        self.rootfs = rootfs_path
        self._cgroups: dict[str, VMResourceLimiter] = {}

        # Supply chain integrity check at boot (fail-close)
        if artifact_manifest:
            try:
                verify_artifacts(artifact_manifest)
                logger.info("supply_chain_verified")
            except IntegrityError as e:
                logger.critical("supply_chain_violation", error=str(e))
                raise

    def start_vm(self, memory_mb=128, cpu_count=1, rootfs_read_only: bool = True):
        vm_id = str(uuid.uuid4())
        import tempfile
        import os
        runtime_dir = os.environ.get("XDG_RUNTIME_DIR", "/var/run/kernell")
        try:
            os.makedirs(runtime_dir, mode=0o700, exist_ok=True)
        except OSError:
            runtime_dir = tempfile.gettempdir()
        socket_path = os.path.join(runtime_dir, f"firecracker-{vm_id}.sock")

        # 1. Start firecracker process
        fc_bin = shutil.which("firecracker")
        if not fc_bin: raise RuntimeError("firecracker not found in PATH")
        process = subprocess.Popen([fc_bin, "--api-sock", socket_path],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True
        )

        # 2. Apply cgroups v2 resource limits
        limiter = VMResourceLimiter(
            vm_id=vm_id,
            cpu_quota=cpu_count,
            memory_mb=memory_mb,
            pids_max=64,
        )
        try:
            limiter.create()
            limiter.attach(process.pid)
            self._cgroups[vm_id] = limiter
        except Exception:
            logger.warning("cgroup_setup_skipped", vm_id=vm_id)

        # Wait for socket to be ready
        for _ in range(10):
            if os.path.exists(socket_path):
                break
            time.sleep(0.05)

        client = create_uds_client(uds_path=socket_path, timeout=2.0)

        try:
            # 2. Configure Machine
            client.put("http://localhost/machine-config", json={
                "vcpu_count": cpu_count,
                "mem_size_mib": memory_mb
            }).raise_for_status()

            # 3. Configure Boot Source
            client.put("http://localhost/boot-source", json={
                "kernel_image_path": self.kernel,
                "boot_args": "console=ttyS0 reboot=k panic=1 pci=off"
            }).raise_for_status()

            # 4. Configure Drives
            client.put("http://localhost/drives/rootfs", json={
                "drive_id": "rootfs",
                "path_on_host": self.rootfs,
                "is_root_device": True,
                "is_read_only": bool(rootfs_read_only)
            }).raise_for_status()

            # 4.5 Configure VSOCK
            client.put("http://localhost/vsock", json={
                "vsock_id": "vsock0",
                "guest_cid": 3,
                "uds_path": f"/tmp/vsock-{vm_id}.sock"
            }).raise_for_status()

            # 5. Start Instance
            client.put("http://localhost/actions", json={
                "action_type": "InstanceStart"
            }).raise_for_status()

        except Exception as e:
            process.kill()
            if os.path.exists(socket_path):
                os.remove(socket_path)
            raise RuntimeError(f"Failed to configure Firecracker VM: {e}")

    def wait_until_ready(self, process: subprocess.Popen, timeout=5.0):
        deadline = time.monotonic() + timeout
        for line in iter(process.stdout.readline, ""):
            if time.monotonic() > deadline:
                raise TimeoutError("VM did not reach VM_READY state in time")
            if "[VM_READY]" in line:
                return

    def create_snapshot(self, socket_path: str, vm_id: str, snapshot_dir: str):
        snap_path = os.path.join(snapshot_dir, f"{vm_id}.snap")
        mem_path = os.path.join(snapshot_dir, f"{vm_id}.mem")
        
        client = create_uds_client(uds_path=socket_path, timeout=2.0)
        client.put("http://localhost/snapshot/create", json={
            "snapshot_type": "Full",
            "snapshot_path": snap_path,
            "mem_file_path": mem_path
        }).raise_for_status()
        
        return snap_path, mem_path

    def restore_snapshot(self, snap_path: str, mem_path: str):
        vm_id = str(uuid.uuid4())
        import tempfile
        import os
        runtime_dir = os.environ.get("XDG_RUNTIME_DIR", "/var/run/kernell")
        try:
            os.makedirs(runtime_dir, mode=0o700, exist_ok=True)
        except OSError:
            runtime_dir = tempfile.gettempdir()
        socket_path = os.path.join(runtime_dir, f"firecracker-{vm_id}.sock")

        fc_bin = shutil.which("firecracker")
        if not fc_bin: raise RuntimeError("firecracker not found in PATH")
        process = subprocess.Popen([fc_bin, "--api-sock", socket_path],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True
        )

        for _ in range(20):
            if os.path.exists(socket_path):
                break
            time.sleep(0.05)

        client = create_uds_client(uds_path=socket_path, timeout=2.0)
        
        try:
            client.put("http://localhost/snapshot/load", json={
                "snapshot_path": snap_path,
                "mem_file_path": mem_path,
                "enable_diff_snapshots": False
            }).raise_for_status()
            
            client.put("http://localhost/actions", json={
                "action_type": "Resume"
            }).raise_for_status()
        except Exception as e:
            process.kill()
            if os.path.exists(socket_path):
                os.remove(socket_path)
            raise RuntimeError(f"Failed to restore Firecracker VM: {e}")

        return vm_id, socket_path, process

    def cleanup_vm(self, vm_id: str, socket_path: str, process: subprocess.Popen):
        process.terminate()
        try:
            process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            process.kill()
            
        if os.path.exists(socket_path):
            os.remove(socket_path)

        # Destroy cgroup
        limiter = self._cgroups.pop(vm_id, None)
        if limiter:
            limiter.destroy()
