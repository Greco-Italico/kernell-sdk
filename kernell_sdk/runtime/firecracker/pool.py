import os
import threading
import time
from collections import deque
from dataclasses import dataclass
from typing import Optional
from .manager import FirecrackerManager

@dataclass
class RestoredVM:
    vm_id: str
    socket_path: str
    process: object
    created_at: float

class RuntimeMetrics:
    def __init__(self):
        self.lock = threading.Lock()
        self.total_requests = 0
        self.cold_starts = 0
        self.latencies = deque(maxlen=1000)
        self.last_snapshot_time = time.time()
        
        # Para evitar que el fallback rate sea 100% de por vida, reiniciamos ventanas
        # pero por simplicidad de este MVP, dejaremos un decaimiento o ventana
        self.window_requests = 0
        self.window_cold = 0

    def record_request(self, latency: float, cold: bool):
        with self.lock:
            self.total_requests += 1
            self.window_requests += 1
            if cold:
                self.cold_starts += 1
                self.window_cold += 1
            self.latencies.append(latency)

    def snapshot(self):
        with self.lock:
            if self.window_requests == 0:
                return None
                
            qps = self.window_requests / max(0.1, time.time() - self.last_snapshot_time)
            fallback_rate = self.window_cold / self.window_requests
            avg_latency = sum(self.latencies) / len(self.latencies) if self.latencies else 0.0
            
            # Reset window
            self.window_requests = 0
            self.window_cold = 0
            self.last_snapshot_time = time.time()
            
            return {
                "qps": qps,
                "fallback_rate": fallback_rate,
                "avg_latency": avg_latency,
            }

class SnapshotPool:
    def __init__(self, manager: FirecrackerManager, snapshot_dir: str, min_size=5, max_size=50):
        self.manager = manager
        self.snapshot_dir = snapshot_dir
        self.min_size = min_size
        self.max_size = max_size
        self.pool = deque()
        self.lock = threading.Lock()
        self.metrics = RuntimeMetrics()
        self.target_size = min_size
        
        self.base_snap_path: Optional[str] = None
        self.base_mem_path: Optional[str] = None
        
        os.makedirs(self.snapshot_dir, exist_ok=True)
        
        self.running = True
        self.warmer_thread = threading.Thread(target=self._intelligent_auto_scale, daemon=True)
        self.warmer_thread.start()

    def initialize_base_snapshot(self, memory_mb=128, cpu_count=1):
        vm_id, sock, process = self.manager.start_vm(memory_mb, cpu_count)
        self.manager.wait_until_ready(process)
        self.base_snap_path, self.base_mem_path = self.manager.create_snapshot(
            sock, vm_id, self.snapshot_dir
        )
        self.manager.cleanup_vm(vm_id, sock, process)

    def _restore_new(self) -> RestoredVM:
        from . import metrics as prom
        if not self.base_snap_path:
            self.initialize_base_snapshot()
            
        start_t = time.time()
        vm_id, sock, process = self.manager.restore_snapshot(
            self.base_snap_path, self.base_mem_path
        )
        duration = time.time() - start_t
        prom.SNAPSHOT_RESTORE_LATENCY.observe(duration)
        
        return RestoredVM(vm_id, sock, process, time.time())

    def _intelligent_auto_scale(self):
        from . import metrics as prom
        
        while self.running:
            data = self.metrics.snapshot()

            if data:
                fallback = data["fallback_rate"]
                latency = data["avg_latency"]

                if fallback > 0.05:
                    self.target_size += 5
                elif fallback == 0 and latency < 0.01:
                    self.target_size = max(self.min_size, self.target_size - 1)

                if latency > 0.02:
                    self.target_size += 3

                self.target_size = min(self.target_size, self.max_size)

            with self.lock:
                current_size = len(self.pool)
            
            # Prometheus gauges
            prom.POOL_SIZE.set(current_size)
            prom.POOL_TARGET_SIZE.set(self.target_size)
                
            # Scale Up
            if current_size < self.target_size and self.base_snap_path:
                try:
                    vm = self._restore_new()
                    with self.lock:
                        self.pool.append(vm)
                except Exception as e:
                    import logging
                    logging.warning(f'Suppressed error in {__name__}: {e}')

            # Scale Down & TTL cleanup
            now = time.time()
            stale_vms = []
            with self.lock:
                # Keep VMs fresh (TTL 10s) or cull if over max_size
                while self.pool and (now - self.pool[0].created_at) > 10.0:
                    stale_vms.append(self.pool.popleft())
                    
            for vm in stale_vms:
                self.manager.cleanup_vm(vm.vm_id, vm.socket_path, vm.process)
                
            time.sleep(0.1)

    def get_with_flag(self) -> tuple[RestoredVM, bool]:
        with self.lock:
            if self.pool:
                return self.pool.pop(), False
                
        return self._restore_new(), True

    def cleanup(self):
        self.running = False
        with self.lock:
            for vm in self.pool:
                self.manager.cleanup_vm(vm.vm_id, vm.socket_path, vm.process)
            self.pool.clear()
