"""
Kernell WAL Replicator — Active-Passive Shadow Replication & Leader Enforcement

Phases 1.1 & 1.2 Implementation:
1. compute_event_id: Global deterministic identity for deduplication
2. WALReplicator: Publishes strictly ordered events to a replication stream
3. ReplicationConsumer: Shadow mode replica (no business logic execution)
4. Leader Enforcement: home_region routing based on request_id hash
"""

import time
import json
import hashlib
from typing import Optional, Dict


def compute_event_id(request_id: str, epoch: int, event_type: str, ts: float) -> str:
    """CRITICAL: Generates a deterministic global ID for any event."""
    canonical = {
        "request_id": request_id,
        "epoch": epoch,
        "type": event_type,
        "ts": round(ts, 6)
    }
    return hashlib.sha256(json.dumps(canonical, sort_keys=True).encode()).hexdigest()

def compute_event_id_from_dict(event: dict) -> str:
    return compute_event_id(
        request_id=event.get("request_id", ""),
        epoch=event.get("epoch", 0),
        event_type=event.get("type", "UNKNOWN"),
        ts=event.get("ts", 0.0)
    )

def get_home_region(request_id: str) -> str:
    """FASE 1.2: Deterministic leader routing via modulo hashing."""
    # We use md5 just for uniform distribution, not security
    val = int(hashlib.md5(request_id.encode()).hexdigest(), 16)
    return "A" if val % 2 == 0 else "B"


class NotLeaderRegionError(Exception):
    pass


from kernell_sdk.router.lease_manager import LeaseManager

class WALReplicator:
    """Runs on the SOURCE region. Forwards local WAL events to the replication stream."""
    
    STREAM_KEY = "kernell:wal:replication"
    
    def __init__(self, redis_local, region: str):
        self.local = redis_local
        self.region = region
        self.lease_manager = LeaseManager(redis_local, region)

    def publish_event(self, event: dict) -> str:
        """Publish a local WAL event to the replication stream."""
        event_id = compute_event_id_from_dict(event)
        
        payload = {
            "event_id": event_id,
            "source_region": self.region,
            "data": json.dumps(event)
        }
        
        return self.local.xadd(self.STREAM_KEY, payload)

    def enforce_write(self, request_id: str, epoch: int):
        """CRITICAL: Called before ANY write. Raises exception if not leased."""
        self.lease_manager.validate_write(request_id, epoch)


class ReplicationConsumer:
    """Runs on the DESTINATION region. Consumes the stream in shadow mode."""
    
    STREAM_KEY = "kernell:wal:replication"
    WAL_KEY = "kernell:wal"
    
    def __init__(self, redis_local, region: str):
        self.r = redis_local
        self.region = region
        self.seen_key = f"kernell:replication:seen:{region}"
        self.last_id = "0-0"

    def process_event(self, entry: dict) -> bool:
        """Process an incoming replication payload. Returns True if processed, False if ignored."""
        event_id = entry.get("event_id")
        source_region = entry.get("source_region")
        
        # 1. Loop Protection
        if source_region == self.region:
            return False
            
        # 2. Strict Deduplication
        if self.r.sismember(self.seen_key, event_id):
            return False
            
        self.r.sadd(self.seen_key, event_id)
        
        # 3. Shadow Mode Write (NO BUSINESS LOGIC)
        event = json.loads(entry["data"])
        
        # Add metadata showing this was replicated
        event["_replicated_from"] = source_region
        event["_replicated_at"] = time.time()
        
        self.r.xadd(self.WAL_KEY, event)
        return True

    def consume_batch(self, count: int = 100, block: int = 1000) -> int:
        """Consume a batch of events from the replication stream."""
        entries = self.r.xread(
            {self.STREAM_KEY: self.last_id},
            count=count,
            block=block
        )
        
        processed = 0
        for stream, events in entries:
            for eid, data in events:
                if self.process_event(data):
                    processed += 1
                self.last_id = eid
                
        return processed
