import httpx
import asyncio
import time
from typing import Optional

class CircuitBreaker:
    def __init__(self, failure_threshold=5, base_recovery_time=10):
        self.failure_threshold = failure_threshold
        self.base_recovery_time = base_recovery_time
        self.recovery_time = base_recovery_time
        self.failures = 0
        self.last_failure_time = 0
        self.state = "CLOSED"  # CLOSED | OPEN | HALF_OPEN
        self.half_open_in_flight = False

    def record_success(self):
        self.failures = 0
        self.recovery_time = self.base_recovery_time
        self.state = "CLOSED"
        self.half_open_in_flight = False

    def record_failure(self):
        self.failures += 1
        self.last_failure_time = time.time()
        self.state = "OPEN"
        self.half_open_in_flight = False
        if self.failures >= self.failure_threshold:
            # Backoff exponencial: 10s, 20s, 40s, max 60s
            exponent = self.failures - self.failure_threshold
            self.recovery_time = min(self.base_recovery_time * (2 ** exponent), 60)

    def can_execute(self):
        if self.state == "CLOSED":
            return True
        if self.state == "OPEN":
            if time.time() - self.last_failure_time > self.recovery_time:
                self.state = "HALF_OPEN"
                self.half_open_in_flight = False
                return True
            return False
        if self.state == "HALF_OPEN":
            if not self.half_open_in_flight:
                self.half_open_in_flight = True
                return True
            return False

class FirecrackerClient:
    def __init__(
        self,
        base_url: str,
        token: str,
        timeout: float = 1.2,
    ):
        self.base_url = base_url
        self.token = token
        self.timeout = timeout
        self.cb = CircuitBreaker()
        self.client = httpx.AsyncClient(timeout=self.timeout)

    async def execute(self, code: str) -> dict:
        if not self.cb.can_execute():
            raise RuntimeError("CircuitBreakerOpen")

        headers = {
            "Authorization": f"Bearer {self.token}"
        }

        try:
            start_ts = time.time()
            resp = await self.client.post(
                f"{self.base_url}/execute",
                json={"code": code},
                headers=headers,
            )
            latency_ms = (time.time() - start_ts) * 1000

            if resp.status_code != 200:
                raise RuntimeError(f"BadStatus {resp.status_code}")

            self.cb.record_success()
            res = resp.json()
            res["_latency_ms"] = latency_ms
            return res

        except Exception as e:
            self.cb.record_failure()
            raise e

    async def health(self) -> dict:
        start_ts = time.time()
        headers = {"Authorization": f"Bearer {self.token}"}
        resp = await self.client.get(f"{self.base_url}/health", headers=headers, timeout=self.timeout)
        latency_ms = (time.time() - start_ts) * 1000
        
        if resp.status_code != 200:
            raise RuntimeError(f"BadStatus {resp.status_code}")
            
        data = resp.json()
        data["latency_ms"] = latency_ms
        return data
