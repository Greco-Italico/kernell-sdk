import asyncio
import secrets
import time
from typing import List, Optional

# -----------------------------
# Worker State
# -----------------------------
class WorkerState:
    def __init__(self, id: str, url: str, max_concurrency: int = 8):
        self.id = id
        self.url = url

        # salud
        self.health_score = 1.0
        self.last_heartbeat = time.time()

        # carga
        self.inflight = 0
        self.max_concurrency = max_concurrency

        # performance
        self.avg_latency_ms = 0.0
        self.error_rate = 0.0

        # circuito
        self.circuit_state = "CLOSED"

    def load_factor(self):
        if self.max_concurrency == 0:
            return 1.0
        return self.inflight / self.max_concurrency


# -----------------------------
# Scheduler
# -----------------------------
class Scheduler:
    def __init__(self, workers: List[WorkerState], metrics):
        self.workers = workers
        self.metrics = metrics
        self._lock = asyncio.Lock()

    # -----------------------------
    # Health computation
    # -----------------------------
    def compute_health(self, w: WorkerState) -> float:
        # Base health decremented by errors
        score = max(1.0 - w.error_rate, 0.1)
        load_factor = w.load_factor()

        # penalización por latencia (EMA) con decaimiento asintótico
        score *= 1 / (1 + w.avg_latency_ms / 100.0)

        # penalización suave por carga (asegura un suelo de 0.05 para el goteo)
        score *= max(1.0 - load_factor, 0.05)

        # circuito abierto es casi fatal
        if w.circuit_state == "OPEN":
            score *= 0.1

        # castigo duro si está saturado (cliff)
        if load_factor > 0.85:
            score *= 0.3

        return max(score, 0.0)

    # -----------------------------
    # Worker selection
    # -----------------------------
    async def pick_worker(self) -> Optional[WorkerState]:
        async with self._lock:
            for w in self.workers:
                w.health_score = self.compute_health(w)

            healthy = [w for w in self.workers if w.health_score > 0.2]

            if not healthy:
                # Assuming metrics object has a method .inc() that accepts string
                if hasattr(self.metrics, "inc"):
                    self.metrics.inc("scheduler_no_workers")
                return None

            weights = [w.health_score for w in healthy]
            chosen = secrets.SystemRandom().choices(healthy, weights=weights, k=1)[0]

            if hasattr(self.metrics, "inc"):
                self.metrics.inc("scheduler_pick_success")
            return chosen

    # -----------------------------
    # Record success
    # -----------------------------
    def record_success(self, w: WorkerState, latency_ms: float):
        w.avg_latency_ms = (w.avg_latency_ms * 0.8) + (latency_ms * 0.2)
        w.error_rate *= 0.9  # decay

        if hasattr(self.metrics, "observe"):
            self.metrics.observe("worker_latency_ms", latency_ms)
        elif hasattr(self.metrics, "inc"):
            self.metrics.inc("worker_latency_ms_measured")
            
        if hasattr(self.metrics, "inc"):
            self.metrics.inc("worker_success")

    # -----------------------------
    # Record failure
    # -----------------------------
    def record_failure(self, w: WorkerState):
        w.error_rate = min(w.error_rate + 0.1, 1.0)
        if hasattr(self.metrics, "inc"):
            self.metrics.inc("worker_failure")

    # -----------------------------
    # Execution wrapper
    # -----------------------------
    async def execute(self, code: str, client_factory, nsjail):
        worker = await self.pick_worker()

        if not worker:
            return await nsjail.execute(code)

        start = time.time()

        try:
            worker.inflight += 1

            client = client_factory(worker.url)
            result = await client.execute(code)

            latency_ms = (time.time() - start) * 1000
            self.record_success(worker, latency_ms)

            return result

        except Exception:
            self.record_failure(worker)
            return await nsjail.execute(code)

        finally:
            worker.inflight = max(worker.inflight - 1, 0)

    # -----------------------------
    # Eviction logic
    # -----------------------------
    def should_evict(self, w: WorkerState) -> bool:
        if w.health_score < 0.1:
            return True

        if time.time() - w.last_heartbeat > 30:
            return True

        if w.error_rate > 0.5:
            return True

        return False

    async def eviction_loop(self):
        while True:
            async with self._lock:
                before = len(self.workers)
                self.workers = [w for w in self.workers if not self.should_evict(w)]
                after = len(self.workers)

                if after < before and hasattr(self.metrics, "inc"):
                    # Optionally pass amount to inc if supported, otherwise just call it
                    try:
                        self.metrics.inc("scheduler_evictions", before - after)
                    except TypeError:
                        for _ in range(before - after):
                            self.metrics.inc("scheduler_evictions")

            await asyncio.sleep(5)

    # -----------------------------
    # Health check loop
    # -----------------------------
    async def health_check_loop(self, client_factory):
        while True:
            async with self._lock:
                for w in self.workers:
                    try:
                        client = client_factory(w.url)
                        data = await client.health()

                        w.last_heartbeat = time.time()
                        w.avg_latency_ms = data.get("latency_ms", w.avg_latency_ms)

                    except Exception:
                        w.error_rate = min(w.error_rate + 0.05, 1.0)

            await asyncio.sleep(2)
