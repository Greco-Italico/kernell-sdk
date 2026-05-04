import pytest
import time
import os
import threading
from unittest.mock import MagicMock, patch

from kernell_sdk.runtime.models import ExecutionRequest
from kernell_sdk.runtime.firecracker_runtime import FirecrackerRuntime
from kernell_sdk.runtime.firecracker.resilience import CircuitOpenError
from kernell_sdk.runtime.firecracker import metrics as prom

# ══════════════════════════════════════════════════════════════════════════════
# CHAOS GUARDRAILS: Kill switch & Staging validation
# ══════════════════════════════════════════════════════════════════════════════
CHAOS_ENABLED = os.getenv("KERNELL_ENABLE_CHAOS_TESTS", "1") == "1"

pytestmark = pytest.mark.skipif(
    not CHAOS_ENABLED, 
    reason="Chaos tests are disabled globally. Set KERNELL_ENABLE_CHAOS_TESTS=1."
)

def count_open_fds():
    """Helper to detect file descriptor leaks."""
    try:
        return len(os.listdir("/proc/self/fd"))
    except OSError:
        return 0


class TestChaosFirecracker:
    """
    Chaos Engineering Suite para Kernell OS.
    Prueba que el sistema se degrada de forma elegante bajo escenarios catastróficos.
    """

    @pytest.fixture
    def runtime(self):
        """Fixture de FirecrackerRuntime con dependencias mockeadas para simular infraestructura."""
        with patch("kernell_sdk.runtime.firecracker_runtime.FirecrackerManager"), \
             patch("kernell_sdk.runtime.firecracker_runtime.TenantManager"), \
             patch("kernell_sdk.runtime.firecracker_runtime.BillingManager"), \
             patch("kernell_sdk.runtime.firecracker_runtime.LocalKMS"):
            
            rt = FirecrackerRuntime("/fake/vmlinux", "/fake/rootfs", "/tmp/fcsnapshots")
            # Bypass billing/admission control for chaos logic testing
            rt.billing_manager.reserve.return_value = True
            rt.billing_manager.get_account.return_value.plan.name = "free"
            rt.tenant_manager.allow_request.return_value = True
            rt._shared_secret = b"12345678901234567890123456789012"
            yield rt

    def test_snapshot_exhaustion_circuit_breaker(self, runtime):
        """
        Fase 1: Simula un agotamiento total del SnapshotPool.
        Valida que el Circuit Breaker se abre y rechaza el tráfico en < 50ms sin leak.
        """
        # Simulamos que el pool está lanzando timeouts o errores
        runtime.pool.get_with_flag = MagicMock(side_effect=Exception("Pool Exhausted"))
        
        req = ExecutionRequest(code="print('test')", memory_limit_mb=128, timeout=5)
        
        start_time = time.time()
        
        # Bombardear hasta que el breaker se abra (failure_threshold=5)
        for _ in range(6):
            res = runtime.execute(req)
            
        latency = (time.time() - start_time) * 1000  # en ms
        
        # El último resultado DEBE ser un 503 ServiceUnavailable por CircuitOpenError
        assert res.exit_code == 503
        assert "ServiceUnavailable" in res.stderr
        
        # Validación crítica: La latencia de fallo debe ser microscópica al estar el circuito abierto
        # Un circuito abierto debe fallar instantáneamente (fail-fast)
        fast_fail_start = time.time()
        res_fast = runtime.execute(req)
        fast_fail_latency = (time.time() - fast_fail_start) * 1000
        
        assert fast_fail_latency < 50, f"Circuit Breaker fail-fast es muy lento: {fast_fail_latency}ms"
        assert res_fast.exit_code == 503

    def test_vsock_blackhole_cleanup(self, runtime):
        """
        Fase 2: Simula un PID 1 congelado en la microVM (Vsock Blackhole).
        Valida que el host respeta el timeout, destruye el VMM y NO filtra descriptores de archivo.
        """
        # VM se adquiere correctamente
        mock_vm = MagicMock(vm_id="vm_chaos_1", socket_path="/tmp/sock", process=MagicMock())
        runtime.pool_breaker.call = MagicMock(return_value=(mock_vm, False))
        
        # Vsock se congela y lanza TimeoutError
        runtime.vsock_breaker.call = MagicMock(side_effect=TimeoutError("Vsock timed out"))
        
        initial_fds = count_open_fds()
        
        req = ExecutionRequest(code="print('blackhole')", memory_limit_mb=128, timeout=1)
        res = runtime.execute(req)
        
        # Validaciones
        assert res.timed_out is True
        assert res.exit_code == -1
        assert "Timeout" in res.stderr
        
        # Cleanup Validation: El VM manager debió destruir el proceso
        runtime.manager.cleanup_vm.assert_called_once_with("vm_chaos_1", "/tmp/sock", mock_vm.process)
        
        # FD Leak Validation
        final_fds = count_open_fds()
        assert final_fds <= initial_fds, f"FD Leak detectado: {initial_fds} -> {final_fds}"

    def test_byzantine_payload_rejection(self, runtime):
        """
        Fase 3: Simula un payload manipulado interceptado en tránsito (o HMAC inválido).
        Valida que el sistema aborta la ejecución criptográfica inmediatamente.
        """
        from kernell_sdk.runtime.firecracker.auth_protocol import AuthenticationError
        
        mock_vm = MagicMock(vm_id="vm_chaos_2")
        runtime.pool_breaker.call = MagicMock(return_value=(mock_vm, False))
        
        # Simulamos que la respuesta criptográfica es inválida (ej. tampering)
        runtime.vsock_breaker.call = MagicMock(side_effect=AuthenticationError("Invalid HMAC signature"))
        
        start_time = time.time()
        req = ExecutionRequest(code="print('hack')", memory_limit_mb=128, timeout=2)
        res = runtime.execute(req)
        latency = (time.time() - start_time) * 1000
        
        # Rechazo debe ser inmediato (< 50ms)
        assert latency < 50
        assert res.exit_code == -1
        assert "Invalid HMAC signature" in res.stderr
        
        # La VM DEBE ser destruida por seguridad (no devuelta al pool si hubo tampering)
        runtime.manager.cleanup_vm.assert_called_once()

    def test_telemetry_accuracy_under_chaos(self, runtime):
        """
        Fase 4: Valida que Prometheus no pierda el conteo bajo estrés.
        Si la observabilidad falla, vamos a ciegas.
        """
        # Obtenemos valores iniciales
        initial_errors = prom.REQUESTS_TOTAL.labels(tenant_tier="free", status="error")._value.get()
        initial_opens = prom.CIRCUIT_OPENS.labels(breaker="snapshot_pool")._value.get()
        
        # Inducir caos
        runtime.pool.get_with_flag = MagicMock(side_effect=Exception("Chaos"))
        req = ExecutionRequest(code="x", memory_limit_mb=128, timeout=1)
        
        # Forzamos 10 peticiones (para abrir el breaker y generar errores)
        for _ in range(10):
            runtime.execute(req)
            
        # Validación de asertividad métrica
        final_errors = prom.REQUESTS_TOTAL.labels(tenant_tier="free", status="error")._value.get()
        final_opens = prom.CIRCUIT_OPENS.labels(breaker="snapshot_pool")._value.get()
        
        # Inflight requests no deben tener leak tampoco
        inflight = prom.INFLIGHT_REQUESTS._value.get()
        
        assert final_errors > initial_errors, "Prometheus no registró los fallos de ejecución."
        assert final_opens > initial_opens, "Prometheus no registró la apertura del Circuit Breaker."
        assert inflight == 0, f"Inflight requests leak: {inflight} peticiones colgadas en la métrica."
