import logging
import time
from enum import Enum
from dataclasses import dataclass, field
from typing import Optional, Dict, Any

from .base import BaseRuntime
from .subprocess_runtime import SubprocessRuntime
from .docker_runtime import DockerRuntime
from .firecracker_runtime import FirecrackerRuntime
from .models import ExecutionRequest, ExecutionResult

logger = logging.getLogger(__name__)

class ExecutionMode(Enum):
    DEBUG = "debug"              # Full local visibility, no limits. For dev/testing.
    CONSTRAINED = "constrained"  # Local but with cgroup limits, timeout, and basic iptables.
    ISOLATED = "isolated"        # Firecracker microVM execution via Execution Fabric.

@dataclass
class HybridRuntimeConfig:
    target_mode: ExecutionMode = ExecutionMode.CONSTRAINED
    fallback_on_failure: bool = True
    min_required_mode: ExecutionMode = ExecutionMode.DEBUG
    observability_enabled: bool = True
    cpu_limit_mb: int = 512
    timeout_seconds: int = 30
    metadata: Dict[str, Any] = field(default_factory=dict)

class HybridRuntime(BaseRuntime):
    """
    Hybrid Runtime for Kernell OS SDK.
    Provides a tiered execution strategy:
    1. ISOLATED (Firecracker)
    2. CONSTRAINED (Docker / Subprocess with cgroups)
    3. DEBUG (Bare subprocess)
    
    Includes robust telemetry, timing, and automatic fallback if the 
    target mode is unavailable (avoiding catastrophic failure).
    """
    
    def __init__(self, config: Optional[HybridRuntimeConfig] = None):
        super().__init__()
        self.config = config or HybridRuntimeConfig()
        
        # Initialize underlying runtimes lazily or pre-warm them
        self._runtimes = {}
        
        logger.info(f"Initialized HybridRuntime in {self.config.target_mode.value.upper()} mode")

    def _get_runtime(self, mode: ExecutionMode) -> BaseRuntime:
        if mode not in self._runtimes:
            if mode == ExecutionMode.DEBUG:
                self._runtimes[mode] = SubprocessRuntime()
            elif mode == ExecutionMode.CONSTRAINED:
                # DockerRuntime offers built-in constraints (cgroups, memory, network isolation)
                self._runtimes[mode] = DockerRuntime()
            elif mode == ExecutionMode.ISOLATED:
                # Requires Kernell OS Firecracker worker running
                self._runtimes[mode] = FirecrackerRuntime()
        return self._runtimes[mode]

    def _record_telemetry(self, mode: ExecutionMode, status: str, duration: float, error: Optional[str] = None):
        """Mock telemetry injection (can be wired to Palantir/Redis pubsub)"""
        if not self.config.observability_enabled:
            return
            
        logger.info(
            f"EXECUTION_TRUTH | Mode: {mode.value} | Status: {status} | "
            f"Time: {duration:.4f}s | Error: {error or 'None'}"
        )

    def execute(self, request: ExecutionRequest, **kwargs) -> ExecutionResult:
        """
        Executes a request using the target mode. 
        Falls back to safer/lower modes if isolated execution fails and fallback is enabled.
        """
        start_time = time.time()
        current_mode = self.config.target_mode
        
        try:
            runtime = self._get_runtime(current_mode)
            
            if self.config.observability_enabled:
                logger.debug(f"Attempting execution via {current_mode.value} runtime...")
                
            # Apply constraints if constrained
            if current_mode == ExecutionMode.CONSTRAINED:
                request.timeout = self.config.timeout_seconds
                request.memory_limit_mb = self.config.cpu_limit_mb
            
            result = runtime.execute(request, **kwargs)
            
            duration = time.time() - start_time
            self._record_telemetry(current_mode, "SUCCESS", duration)
            
            # Inject execution context for auditability
            if not hasattr(result, "_execution_context"):
                setattr(result, "_execution_context", {})
            
            result._execution_context.update({
                "mode": current_mode.value,
                "duration_seconds": duration,
                "fallback_triggered": False
            })
            return result

        except Exception as e:
            duration = time.time() - start_time
            self._record_telemetry(current_mode, "FAILED", duration, str(e))
            
            if not self.config.fallback_on_failure:
                raise RuntimeError(f"Execution failed in {current_mode.value} mode: {e}") from e
                
            # Fallback Logic: ISOLATED -> CONSTRAINED -> DEBUG
            return self._handle_fallback(request, current_mode, start_time, kwargs, original_error=e)

    def _handle_fallback(self, request: ExecutionRequest, failed_mode: ExecutionMode, 
                         start_time: float, kwargs: dict, original_error: Exception) -> ExecutionResult:
        fallback_mode = None
        
        if failed_mode == ExecutionMode.ISOLATED:
            fallback_mode = ExecutionMode.CONSTRAINED
        elif failed_mode == ExecutionMode.CONSTRAINED:
            fallback_mode = ExecutionMode.DEBUG
            
        # Security Guard: Do not fallback below min_required_mode
        if fallback_mode:
            # Mode priorities: ISOLATED (2) > CONSTRAINED (1) > DEBUG (0)
            priorities = {ExecutionMode.DEBUG: 0, ExecutionMode.CONSTRAINED: 1, ExecutionMode.ISOLATED: 2}
            if priorities[fallback_mode] < priorities[self.config.min_required_mode]:
                logger.error(f"Security constraint: Cannot fallback to {fallback_mode.value}. Minimum required is {self.config.min_required_mode.value}.")
                fallback_mode = None
            
        if not fallback_mode:
            logger.error("All allowed fallback tiers exhausted or blocked. Execution hard-failed.")
            raise original_error
            
        logger.warning(f"FALLBACK TRIGGERED: {failed_mode.value} -> {fallback_mode.value} due to: {original_error}")
        
        try:
            fallback_runtime = self._get_runtime(fallback_mode)
            result = fallback_runtime.execute(request, **kwargs)
            
            duration = time.time() - start_time
            self._record_telemetry(fallback_mode, "SUCCESS_VIA_FALLBACK", duration)
            
            if not hasattr(result, "_execution_context"):
                setattr(result, "_execution_context", {})
                
            result._execution_context.update({
                "mode": fallback_mode.value,
                "duration_seconds": duration,
                "fallback_triggered": True,
                "original_error": str(original_error)
            })
            return result
            
        except Exception as fallback_err:
            # Recursive fallback if necessary
            return self._handle_fallback(request, fallback_mode, start_time, kwargs, fallback_err)

    def setup(self):
        """Prepare all required runtimes."""
        pass
        
    def teardown(self):
        """Cleanup resources for all initialized runtimes."""
        for mode, runtime in self._runtimes.items():
            try:
                runtime.teardown()
            except Exception as e:
                logger.warning(f"Failed to teardown runtime {mode.value}: {e}")
