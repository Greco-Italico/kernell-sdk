import socket
import time
import os
import hashlib
from .base import BaseRuntime
from .models import ExecutionRequest, ExecutionResult
from .firecracker.manager import FirecrackerManager
from .firecracker.pool import SnapshotPool
from .firecracker.tenant import TenantManager
from .firecracker.billing import BillingManager
from .firecracker.telemetry import TelemetryManager
from .firecracker.resilience import CircuitBreaker, CircuitOpenError, retry_with_jitter
from .firecracker import metrics as prom
from ..security.kms import LocalKMS
from .sandbox_validator import validate_code
from .firecracker.auth_protocol import (
    load_shared_secret,
    derive_key,
    AuthenticatedFrame,
    recv_len_prefixed,
    AuthenticationError,
    ProtocolConfigError,
    sha256_hex,
)

VSOCK_PORT = 5000
VSOCK_CID = 3
MAX_RECV = 65536


class FirecrackerRuntime(BaseRuntime):
    """
    Runtime Fase 3D: Production-grade MicroVM runtime.
    Snapshots, Multi-Tenant Isolation, Billing, Telemetry, Resilience, and Prometheus Metrics.
    """

    def __init__(self, kernel_path: str, rootfs_path: str, snapshot_dir="/tmp/fcsnapshots"):
        self.manager = FirecrackerManager(kernel_path, rootfs_path)
        self.pool = SnapshotPool(self.manager, snapshot_dir)
        self.tenant_manager = TenantManager()
        self.billing_manager = BillingManager()
        kms = LocalKMS()
        self.telemetry = TelemetryManager(kms=kms)
        self._shared_secret = None
        try:
            self._shared_secret = load_shared_secret()
        except ProtocolConfigError:
            # Fail-close happens at execution time to allow dev imports,
            # but production must configure the secret.
            self._shared_secret = None
        
        self.pool_breaker = CircuitBreaker(failure_threshold=5, recovery_timeout=15.0, name="snapshot_pool")
        self.vsock_breaker = CircuitBreaker(failure_threshold=3, recovery_timeout=10.0, name="vsock")

    def execute(self, request: ExecutionRequest) -> ExecutionResult:
        tenant_id = getattr(request, "tenant_id", "default_tenant")
        request_id = getattr(request, "request_id", "missing_id")
        
        # Resolve tier label for Prometheus (low cardinality)
        account = self.billing_manager.get_account(tenant_id)
        tier = account.plan.name
        
        self.telemetry.trace(request_id, "START_EXECUTION", {"tenant_id": tenant_id})
        prom.INFLIGHT_REQUESTS.inc()

        # 0. Host-side AST validation BEFORE any delegation (fail-close)
        validation = validate_code(request.code, filename=f"<tenant:{tenant_id}>")
        if not validation.valid:
            prom.REJECTED_TOTAL.labels(reason="sandbox_violation").inc()
            prom.INFLIGHT_REQUESTS.dec()
            return ExecutionResult(
                stdout="",
                stderr=f"SandboxViolation: {validation}",
                exit_code=400,
            )
        
        # 0A. Billing Control (Pre-Auth Reserve)
        if not self.billing_manager.reserve(account, amount=1.0):
            self.telemetry.trace(request_id, "BILLING_REJECTED", {"reason": "Insufficient credits"})
            prom.REJECTED_TOTAL.labels(reason="insufficient_credits").inc()
            prom.INFLIGHT_REQUESTS.dec()
            return ExecutionResult(
                stdout="",
                stderr="InsufficientCredits: Payment required to execute this payload.",
                exit_code=402
            )
            
        # 0B. Admission Control (Rate Limiting & Concurrency Caps)
        tenant = self.tenant_manager.get(tenant_id)
        if not self.tenant_manager.allow_request(tenant):
            self.billing_manager.settle(account, tenant_id, duration_sec=0, memory_mb=request.memory_limit_mb, reserved=1.0)
            self.telemetry.trace(request_id, "ADMISSION_REJECTED", {"reason": "Rate or concurrency limit exceeded"})
            prom.REJECTED_TOTAL.labels(reason="rate_limit").inc()
            prom.INFLIGHT_REQUESTS.dec()
            return ExecutionResult(
                stdout="",
                stderr="RateLimitExceeded: Tenant quota exhausted or concurrency limit reached.",
                exit_code=429
            )

        start_time = time.time()
        self.telemetry.trace(request_id, "ADMISSION_PASSED", {})
        
        try:
            # 1. Get warm VM through circuit breaker
            try:
                vm, is_cold = self.pool_breaker.call(self.pool.get_with_flag)
                self.telemetry.trace(request_id, "VM_ACQUIRED", {"is_cold": is_cold, "vm_id": vm.vm_id})
                if is_cold:
                    prom.COLD_STARTS_TOTAL.inc()
            except CircuitOpenError as e:
                self.telemetry.trace(request_id, "CIRCUIT_OPEN_POOL", {"error": str(e)})
                prom.CIRCUIT_OPENS.labels(breaker="snapshot_pool").inc()
                prom.REQUESTS_TOTAL.labels(tenant_tier=tier, status="circuit_open").inc()
                return ExecutionResult(stdout="", stderr=f"ServiceUnavailable: {e}", exit_code=503)
            except Exception as e:
                self.telemetry.trace(request_id, "VM_ACQUIRE_FAILED", {"error": str(e)})
                prom.REQUESTS_TOTAL.labels(tenant_tier=tier, status="error").inc()
                return ExecutionResult(stdout="", stderr=f"VM Snapshot Restore Error: {e}", exit_code=-1)

            try:
                # 2. Send code via vsock through circuit breaker + retry
                def _do_vsock():
                    return self._send_code_vsock(
                        vm.vm_id,
                        request.code,
                        timeout=request.timeout,
                        tenant_id=tenant_id,
                        request_id=request_id,
                    )
                
                try:
                    result_text = self.vsock_breaker.call(
                        retry_with_jitter,
                        _do_vsock,
                        max_retries=2,
                        base_delay=0.05,
                        retryable_exceptions=(ConnectionRefusedError, OSError),
                    )
                except CircuitOpenError as e:
                    self.telemetry.trace(request_id, "CIRCUIT_OPEN_VSOCK", {"error": str(e)})
                    prom.CIRCUIT_OPENS.labels(breaker="vsock").inc()
                    prom.REQUESTS_TOTAL.labels(tenant_tier=tier, status="circuit_open").inc()
                    return ExecutionResult(stdout="", stderr=f"ServiceUnavailable: {e}", exit_code=503)

                latency = time.time() - start_time
                self.pool.metrics.record_request(latency, is_cold)
                self.telemetry.trace(request_id, "VSOCK_RETURNED", {"latency": latency})
                
                # Prometheus: record latency and success
                prom.EXECUTION_LATENCY.labels(tenant_tier=tier, cold=str(is_cold)).observe(latency)

                if result_text.startswith("ERROR:"):
                    prom.REQUESTS_TOTAL.labels(tenant_tier=tier, status="error").inc()
                    return ExecutionResult(stdout="", stderr=result_text, exit_code=1)

                prom.REQUESTS_TOTAL.labels(tenant_tier=tier, status="ok").inc()
                return ExecutionResult(stdout=result_text, stderr="", exit_code=0)

            except TimeoutError:
                self.telemetry.trace(request_id, "EXECUTION_TIMEOUT", {})
                prom.REQUESTS_TOTAL.labels(tenant_tier=tier, status="timeout").inc()
                return ExecutionResult(
                    stdout="",
                    stderr="ExecutionTimeout: Firecracker VM timed out.",
                    exit_code=-1,
                    timed_out=True
                )
            except Exception as e:
                self.telemetry.trace(request_id, "VSOCK_ERROR", {"error": str(e)})
                prom.REQUESTS_TOTAL.labels(tenant_tier=tier, status="error").inc()
                return ExecutionResult(stdout="", stderr=f"Vsock Error: {e}", exit_code=-1)
            finally:
                self.manager.cleanup_vm(vm.vm_id, vm.socket_path, vm.process)
                self.telemetry.trace(request_id, "VM_CLEANED", {"vm_id": vm.vm_id})
        finally:
            duration = time.time() - start_time
            self.tenant_manager.release_request(tenant)
            self.billing_manager.settle(
                account, tenant_id,
                duration_sec=duration,
                memory_mb=request.memory_limit_mb,
                reserved=1.0
            )
            prom.CREDITS_CONSUMED.labels(tenant_tier=tier).inc(duration * request.memory_limit_mb * 0.0001)
            prom.INFLIGHT_REQUESTS.dec()
            
            self.telemetry.log_audit_event(
                request_id=request_id,
                tenant_id=tenant_id,
                action="EXECUTE_PAYLOAD",
                details={
                    "duration_sec": duration,
                    "memory_mb": request.memory_limit_mb,
                    "code_hash": hashlib.sha256(request.code.encode("utf-8")).hexdigest(),
                }
            )

    def _send_code_vsock(self, vm_id: str, code: str, timeout: int, tenant_id: str, request_id: str) -> str:
        """
        Connect to the VM's vsock server, send authenticated framed payload, receive authenticated response.
        """
        if not self._shared_secret:
            raise ProtocolConfigError("Missing FC_VSOCK_SHARED_SECRET_B64 (fail-close)")

        k_exec = derive_key(self._shared_secret, "kernell.vsock.exec.v1")
        k_resp = derive_key(self._shared_secret, "kernell.vsock.resp.v1")

        s = socket.socket(socket.AF_VSOCK, socket.SOCK_STREAM)
        s.settimeout(timeout)

        connected = False
        start_t = time.time()
        deadline = time.monotonic() + timeout
        for _ in range(20):
            if time.monotonic() > deadline:
                break
            try:
                s.connect((VSOCK_CID, VSOCK_PORT))
                connected = True
                break
            except (ConnectionRefusedError, OSError):
                time.sleep(0.1)
                
        prom.VSOCK_CONNECT_LATENCY.observe(time.time() - start_t)

        if not connected:
            s.close()
            raise TimeoutError("Could not connect to VM vsock server within timeout")

        try:
            payload_bytes = code.encode("utf-8")
            meta = {
                "timeout": int(timeout),
                "payload_sha256": sha256_hex(payload_bytes),
            }
            frame = AuthenticatedFrame.create(
                payload_bytes,
                k_exec,
                tenant_id=tenant_id,
                request_id=request_id,
                meta=meta,
            )
            s.sendall(frame.to_wire())
            s.shutdown(socket.SHUT_WR)

            resp_body = recv_len_prefixed(s)
            resp_frame = AuthenticatedFrame.from_wire(resp_body, k_resp)
            if resp_frame.tenant_id != tenant_id or resp_frame.request_id != request_id:
                raise AuthenticationError("response tenant_id/request_id mismatch")
            return resp_frame.payload_bytes().decode("utf-8", errors="replace")
        finally:
            s.close()
