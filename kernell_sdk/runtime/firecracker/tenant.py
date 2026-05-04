import time
import threading

class TenantState:
    def __init__(self, rate_limit: float, burst: int, max_concurrency: int):
        self.rate_limit = rate_limit  # req/sec
        self.burst = burst
        self.tokens = float(burst)
        self.last_refill = time.time()
        self.max_concurrency = max_concurrency
        self.active = 0
        self.lock = threading.Lock()

class TenantManager:
    def __init__(self, max_global_inflight: int = 100):
        self.tenants = {}
        self.lock = threading.Lock()
        self.max_global_inflight = max_global_inflight
        self.global_active = 0

    def get(self, tenant_id: str) -> TenantState:
        with self.lock:
            if tenant_id not in self.tenants:
                self.tenants[tenant_id] = TenantState(
                    rate_limit=20.0,
                    burst=40,
                    max_concurrency=10
                )
            return self.tenants[tenant_id]

    def allow_request(self, state: TenantState) -> bool:
        with self.lock:
            if self.global_active >= self.max_global_inflight:
                return False
            
            with state.lock:
                now = time.time()
                elapsed = now - state.last_refill

                # Refill tokens
                state.tokens = min(
                    float(state.burst),
                    state.tokens + elapsed * state.rate_limit
                )
                state.last_refill = now

                # Check concurrency cap
                if state.active >= state.max_concurrency:
                    return False

                # Check token bucket rate limit
                if state.tokens >= 1.0:
                    state.tokens -= 1.0
                    state.active += 1
                    self.global_active += 1
                    return True

                return False

    def release_request(self, state: TenantState):
        with self.lock:
            self.global_active = max(0, self.global_active - 1)
        with state.lock:
            state.active = max(0, state.active - 1)
