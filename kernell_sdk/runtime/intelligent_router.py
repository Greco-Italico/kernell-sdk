import secrets
import asyncio
import logging

logger = logging.getLogger("kernell.router")

class IntelligentRouter:
    def __init__(
        self,
        nsjail_executor,
        firecracker_client,
        metrics,
        config
    ):
        self.nsjail = nsjail_executor
        self.fc = firecracker_client
        self.metrics = metrics
        self.config = config
        self.shadow_semaphore = asyncio.Semaphore(10)
        self.recent_shadow_results = []
        self._lock = asyncio.Lock()
        self._firecracker_enabled = self.config.get("FIRECRACKER_ENABLED", False)

    async def route(self, code: str):
        mode = self.config.get("FIRECRACKER_MODE", "off")
        
        if not self._firecracker_enabled or mode == "off":
            return await self._nsjail(code)
            
        if mode == "shadow":
            return await self._shadow_mode(code)
            
        if mode == "canary":
            return await self._canary_mode(code)
            
        return await self._nsjail(code)

    async def _record_shadow_result(self, success: bool):
        async with self._lock:
            self.recent_shadow_results.append(success)
            if len(self.recent_shadow_results) > 10:
                self.recent_shadow_results.pop(0)
                
            if not success:
                failures = self.recent_shadow_results.count(False)
                if failures >= 5:
                    logger.critical("auto_kill_switch_triggered: 5 shadow failures in last 10 requests")
                    self._firecracker_enabled = False
                    self.recent_shadow_results.clear()

    # ------------------------
    # SHADOW MODE
    # ------------------------
    async def _shadow_mode(self, code: str):
        result = await self._nsjail(code)
        # fire and forget
        asyncio.create_task(self._firecracker_shadow(code, result))
        return result

    async def _firecracker_shadow(self, code, expected):
        # Record circuit state
        state_map = {"CLOSED": 0, "HALF_OPEN": 1, "OPEN": 2}
        self.metrics.inc(f"firecracker_circuit_state_{state_map.get(self.fc.cb.state, 0)}")
        
        async with self.shadow_semaphore:
            try:
                fc_result = await self.fc.execute(code)
                self.metrics.inc("firecracker_shadow_calls")
                
                # Record latency metric
                if "_latency_ms" in fc_result:
                    # In a real metrics system this would be an Observe, using inc for now
                    self.metrics.inc("firecracker_latency_measured")
                    logger.debug(f"firecracker_latency_ms: {fc_result['_latency_ms']}")
                
                fc_stdout = fc_result.get("stdout", "")
                fc_stderr = fc_result.get("stderr", "")
                
                exp_stdout = expected.stdout if hasattr(expected, "stdout") else expected.get("stdout", "")
                exp_stderr = expected.stderr if hasattr(expected, "stderr") else expected.get("stderr", "")
                
                if fc_stdout != exp_stdout or fc_stderr != exp_stderr:
                    self.metrics.inc("firecracker_divergence")
                    logger.warning("firecracker_divergence_detected", code=code[:50])
                
                await self._record_shadow_result(True)
            except Exception as e:
                import httpx
                if isinstance(e, httpx.TimeoutException):
                    self.metrics.inc("firecracker_timeout_rate")
                    
                self.metrics.inc("firecracker_shadow_failures")
                logger.debug(f"shadow_execution_failed: {e}")
                await self._record_shadow_result(False)

    # ------------------------
    # CANARY MODE
    # ------------------------
    async def _canary_mode(self, code: str):
        percent = self.config.get("FIRECRACKER_CANARY_PERCENT", 0.01)
        if secrets.SystemRandom().random() < percent:
            try:
                result = await self.fc.execute(code)
                self.metrics.inc("firecracker_success")
                return result
            except Exception as e:
                self.metrics.inc("firecracker_failures")
                logger.warning(f"canary_fallback_triggered: {e}")
                return await self._nsjail(code)
        else:
            return await self._nsjail(code)

    # ------------------------
    # NSJAIL (fallback)
    # ------------------------
    async def _nsjail(self, code: str):
        # Determine if execution is async
        if asyncio.iscoroutinefunction(self.nsjail.execute):
            return await self.nsjail.execute(code)
        else:
            loop = asyncio.get_running_loop()
            return await loop.run_in_executor(None, self.nsjail.execute, code)
