"""
Kernell Failover Orchestrator — Autonomous Active-Active Coordinator

Phase 1.4 Implementation:
1. HeartbeatEmitter: Maintains regional presence.
2. HeartbeatMonitor: Detects regional death.
3. QuorumGuard: Prevents split-brain by avoiding suicide on network partitions.
4. FailoverOrchestrator: Coordinates safe lease takeovers.
"""

import time
import json
import logging
from typing import Optional, List

logger = logging.getLogger("kernell.orchestrator")

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 1. Heartbeats
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class HeartbeatEmitter:
    """Emits periodic presence signals."""
    def __init__(self, redis_client, region: str):
        self.r = redis_client
        self.region = region

    def beat(self, status: str = "healthy", ttl: int = 2):
        key = f"kernell:heartbeat:{self.region}"
        payload = json.dumps({
            "ts": time.time(),
            "status": status
        })
        self.r.set(key, payload, ex=ttl)


class HeartbeatMonitor:
    """Monitors the presence of regions."""
    def __init__(self, redis_client):
        self.r = redis_client

    def is_alive(self, region: str, threshold: float = 2.0) -> bool:
        raw = self.r.get(f"kernell:heartbeat:{region}")
        if not raw:
            return False

        hb = json.loads(raw)
        return (time.time() - hb["ts"]) < threshold

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 2. QuorumGuard (Anti-Suicide)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class QuorumGuard:
    """Ensures we only takeover if the other side is actually dead, not if we are isolated."""
    def __init__(self, redis_client, region: str):
        self.r = redis_client
        self.region = region

    def can_takeover(self, other_region: str, monitor: HeartbeatMonitor) -> bool:
        # 1. El otro parece muerto
        if monitor.is_alive(other_region):
            return False

        # 2. Yo sigo conectado a Redis (sanity check)
        try:
            self.r.ping()
        except Exception:
            return False

        # 3. Mi heartbeat existe (no estoy aislado)
        if not monitor.is_alive(self.region):
            return False

        return True

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 3. Failover Executor
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class FailoverExecutor:
    """Executes the safe transfer of leadership."""
    def __init__(self, redis_client, lease_manager):
        self.r = redis_client
        self.lease_manager = lease_manager

    def execute(self, request_id: str, ttl: float = 300.0) -> dict:
        current = self.lease_manager.get(request_id)
        old_holder = current["holder"] if current else "UNKNOWN"
        
        # Takeover automatically increments epoch
        new_lease = self.lease_manager.takeover(request_id, ttl=ttl)

        event = {
            "type": "FAILOVER",
            "request_id": request_id,
            "epoch": new_lease["epoch"],
            "from": old_holder,
            "to": self.lease_manager.region,
            "ts": time.time()
        }
        
        self.r.xadd("kernell:wal", event)
        return new_lease

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 4. Orchestrator Brain
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class FailoverOrchestrator:
    """The main loop coordinating the failover process autonomously."""
    def __init__(self, redis_client, region: str, lease_manager):
        self.r = redis_client
        self.region = region
        self.lease_manager = lease_manager
        self.monitor = HeartbeatMonitor(redis_client)
        self.quorum = QuorumGuard(redis_client, region)
        self.executor = FailoverExecutor(redis_client, lease_manager)
        
        self.backoff = {}
        self.BACKOFF_TTL = 5.0

    def _in_backoff(self, request_id: str) -> bool:
        return time.time() < self.backoff.get(request_id, 0)

    def _set_backoff(self, request_id: str):
        self.backoff[request_id] = time.time() + self.BACKOFF_TTL

    def tick(self, active_requests: List[str]) -> List[str]:
        """
        Evaluate active requests and trigger failover if necessary.
        Returns a list of request_ids that were taken over in this tick.
        """
        taken_over = []
        
        for request_id in active_requests:
            lease = self.lease_manager.get(request_id)
            if not lease:
                continue

            holder = lease["holder"]

            if holder == self.region:
                continue  # Ya soy líder

            # Anti-thrashing (No pelearse por el liderazgo si recién fallamos)
            if self._in_backoff(request_id):
                continue
                
            # ¿Es seguro robar el liderazgo?
            if self.quorum.can_takeover(holder, self.monitor):
                logger.warning(f"Failover triggered for {request_id} from {holder} to {self.region}")
                
                try:
                    self.executor.execute(request_id)
                    self._set_backoff(request_id)
                    taken_over.append(request_id)
                except Exception as e:
                    logger.error(f"Failed to execute takeover for {request_id}: {e}")
                    self._set_backoff(request_id) # Set backoff anyway to avoid spamming errors

        return taken_over
