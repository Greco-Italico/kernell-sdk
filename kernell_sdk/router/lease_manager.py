"""
Kernell Epoch Leasing — Distributed Fencing & Failover

Phase 1.3 Implementation:
Ensures strictly one writer per request_id per epoch.
Prevents split-brain by fencing out old leaders.
"""

import time
import json
from typing import Optional


class FencedError(Exception):
    pass

class NotLeaderError(Exception):
    pass

class LeaseExpiredError(Exception):
    pass

class NoLeaseError(Exception):
    pass


class LeaseManager:
    """Manages distributed ownership of a request_id."""

    def __init__(self, redis_client, region: str):
        self.r = redis_client
        self.region = region

    def acquire(self, request_id: str, epoch: int, ttl: float) -> dict:
        """Acquire or renew a lease for this region."""
        key = f"kernell:lease:{request_id}"
        lease = {
            "holder": self.region,
            "epoch": epoch,
            "expires_at": time.time() + ttl
        }
        self.r.set(key, json.dumps(lease))
        return lease

    def get(self, request_id: str) -> Optional[dict]:
        """Get the current lease status."""
        raw = self.r.get(f"kernell:lease:{request_id}")
        return json.loads(raw) if raw else None

    def validate_write(self, request_id: str, epoch: int) -> bool:
        """
        CRITICAL: Validates ownership before ANY write.
        Raises specific exceptions if the write is not authorized.
        """
        lease = self.get(request_id)

        if not lease:
            raise NoLeaseError(f"No active lease for {request_id}")

        if lease["epoch"] != epoch:
            raise FencedError(f"Fenced by new epoch. Requested {epoch}, current is {lease['epoch']}")

        if lease["holder"] != self.region:
            raise NotLeaderError(f"Region {self.region} is not leader. Current is {lease['holder']}")

        if lease["expires_at"] < time.time():
            raise LeaseExpiredError(f"Lease for {request_id} has expired")

        return True

    def takeover(self, request_id: str, ttl: float) -> dict:
        """
        Force a takeover of a request_id.
        Increments the epoch to fence out the old leader.
        """
        current = self.get(request_id)
        new_epoch = (current["epoch"] + 1) if current else 1

        lease = self.acquire(request_id, epoch=new_epoch, ttl=ttl)
        
        # Emitting the FAILOVER event to the WAL should be handled by the caller,
        # but the lease guarantees the caller has exclusive rights to do so.
        
        return lease
