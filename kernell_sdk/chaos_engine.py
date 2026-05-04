"""
Kernell Chaos Engine — Phase 2: Deterministic Chaos Testing
"""

import time
import random
import uuid
import json
import logging
from typing import List, Dict, Any
from copy import deepcopy

from kernell_sdk.router.failover_orchestrator import FailoverOrchestrator
from kernell_sdk.router.lease_manager import LeaseManager, FencedError, NotLeaderError
from kernell_sdk.router.simulation_engine import SimulationEngine, NormalizedEvent, ExecutionFingerprint

logger = logging.getLogger("kernell.chaos")

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Invariants
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class Invariants:
    
    @staticmethod
    def no_double_commit(timeline: List[NormalizedEvent]):
        commits = [e for e in timeline if e.type == "COMMIT"]
        assert len(commits) <= 1, f"CRITICAL: Double commit detected! {commits}"

    @staticmethod
    def balance_conservation(ledger_before: dict, ledger_after: dict):
        assert sum(ledger_before.values()) == sum(ledger_after.values()), "CRITICAL: Balance conservation violated!"

    @staticmethod
    def monotonic_epoch(timeline: List[NormalizedEvent]):
        epochs = [e.epoch for e in timeline]
        # Allow same epoch, but no regressions unless we freeze (but here we strictly verify final applied order)
        assert epochs == sorted(epochs), f"CRITICAL: Epoch regression detected! {epochs}"

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Chaos Simulator Base
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class ChaosRedis:
    """Mock Redis that supports network partitions, delays, and packet duplication."""
    def __init__(self):
        self.store = {}
        self.streams = {"kernell:wal": []}
        self.partitions = set()
        self.delayed_events = []
    
    def set(self, key, value, ex=None):
        if "kernell:heartbeat:" in key:
            region = key.split(":")[-1]
            if region in self.partitions:
                return # Dropped packet
        self.store[key] = value

    def get(self, key):
        if "kernell:heartbeat:" in key:
            region = key.split(":")[-1]
            if region in self.partitions:
                return None # Network partitioned
        return self.store.get(key)
        
    def xadd(self, stream, fields):
        if stream not in self.streams:
            self.streams[stream] = []
            
        # Extract the source region from the lease logic if possible, or assume it's normal
        self.streams[stream].append((f"{time.time()}-0", fields))
        return "1-0"

    def xread(self, streams_dict, count=None, block=None):
        result = []
        for stream, last_id in streams_dict.items():
            if stream not in self.streams:
                continue
            
            events = []
            capture = (last_id == "0-0")
            for sid, fields in self.streams[stream]:
                if capture:
                    events.append((sid, fields))
                elif sid == last_id:
                    capture = True
            
            if count and len(events) > count:
                events = events[:count]
                
            if events:
                result.append((stream, events))
        return result

    def sismember(self, key, member):
        return member in self.store.get(key, set())

    def sadd(self, key, member):
        if key not in self.store:
            self.store[key] = set()
        self.store[key].add(member)

    def ping(self):
        return True

    def fetch_wal(self) -> List[NormalizedEvent]:
        events = []
        for sid, data in self.streams.get("kernell:wal", []):
            req_id = data.get("request_id")
            epoch = int(data.get("epoch", 0))
            event_type = data.get("type", "UNKNOWN")
            ts = data.get("ts", time.time())
            events.append(NormalizedEvent(req_id, epoch, event_type, ts, data))
        return events

class ChaosController:
    def __init__(self):
        self.redis = ChaosRedis()
        self.lease_a = LeaseManager(self.redis, "A")
        self.lease_b = LeaseManager(self.redis, "B")
        self.orch_a = FailoverOrchestrator(self.redis, "A", self.lease_a)
        self.orch_b = FailoverOrchestrator(self.redis, "B", self.lease_b)
        
    def setup_base_state(self, request_id: str):
        self.lease_a.acquire(request_id, epoch=1, ttl=10.0)
        self.redis.set("kernell:heartbeat:A", json.dumps({"ts": time.time(), "status": "healthy"}))
        self.redis.set("kernell:heartbeat:B", json.dumps({"ts": time.time(), "status": "healthy"}))

        self.redis.xadd("kernell:wal", {
            "request_id": request_id, "epoch": 1, "type": "START", "ts": time.time()
        })

    def get_timeline(self) -> List[NormalizedEvent]:
        return self.redis.fetch_wal()

    def run_scenario(self, scenario_cls, request_id: str):
        scenario = scenario_cls(self, request_id)
        
        self.setup_base_state(request_id)
        scenario.inject()
        
        timeline = self.get_timeline()
        
        # 1. Verification via SimulationEngine
        engine = SimulationEngine(timeline)
        engine.build()
        
        # 2. Hard Invariants Check
        Invariants.no_double_commit(timeline)
        
        return {
            "request_id": request_id,
            "final_epoch": max([e.epoch for e in timeline] + [0]),
            "fingerprint": ExecutionFingerprint.from_history([{"type": e.type, "epoch": e.epoch, "ts": e.ts} for e in timeline]),
            "events_applied": len(timeline),
            "state": engine.state.executions.get(request_id, {}).get("state", "UNKNOWN")
        }

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Scenarios
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class BaseScenario:
    def __init__(self, ctrl: ChaosController, request_id: str):
        self.ctrl = ctrl
        self.request_id = request_id
        
    def inject(self):
        pass

class NetworkPartitionScenario(BaseScenario):
    """
    Split Brain: A and B both alive, but network partition separates them.
    Both perform takeover. We verify the final state doesn't corrupt.
    """
    def inject(self):
        # A doesn't see B, B doesn't see A
        self.ctrl.redis.partitions.add("A")
        self.ctrl.redis.partitions.add("B")
        
        # Wait for heartbeats to "expire" conceptually
        self.ctrl.redis.set("kernell:heartbeat:A", json.dumps({"ts": time.time() - 10, "status": "healthy"}))
        self.ctrl.redis.set("kernell:heartbeat:B", json.dumps({"ts": time.time() - 10, "status": "healthy"}))
        
        # Both orchestrators will tick and try to takeover
        self.ctrl.orch_a.tick([self.request_id])
        self.ctrl.orch_b.tick([self.request_id])
        
        # Verify A cannot write because B took over last, OR B is fenced.
        # But wait, A's takeover happened, then B's takeover.
        try:
            self.ctrl.lease_a.validate_write(self.request_id, 2)
            # A tries to write
            self.ctrl.redis.xadd("kernell:wal", {"request_id": self.request_id, "epoch": 2, "type": "COMMIT", "ts": time.time()})
        except (FencedError, NotLeaderError):
            pass # Fenced correctly
        
        try:
            self.ctrl.lease_b.validate_write(self.request_id, 3)
            # B tries to write
            self.ctrl.redis.xadd("kernell:wal", {"request_id": self.request_id, "epoch": 3, "type": "COMMIT", "ts": time.time()})
        except (FencedError, NotLeaderError):
            pass

class InFlightCommitScenario(BaseScenario):
    """
    In-Flight Commit + Failover
    A initiates a COMMIT, but network lags.
    B takes over.
    A's COMMIT finally arrives.
    """
    def inject(self):
        # A's heartbeat expires
        self.ctrl.redis.set("kernell:heartbeat:A", json.dumps({"ts": time.time() - 10, "status": "healthy"}))
        
        # B does takeover
        self.ctrl.orch_b.tick([self.request_id])
        
        # A tries to commit using its OLD epoch 1
        # In reality, the LeaseManager on the server would reject it.
        # But let's say the network delay means it bypasses the validation at the EXACT SAME MS,
        # and lands in the WAL directly.
        self.ctrl.redis.xadd("kernell:wal", {"request_id": self.request_id, "epoch": 1, "type": "COMMIT", "ts": time.time() + 1.0})
        
        # The engine will read this and catch the Epoch regression (1 < 2) 

from kernell_sdk.router.wal_replicator import ReplicationConsumer

class WALStormScenario(BaseScenario):
    """
    WAL Reordering + Duplication Storm
    """
    def inject(self):
        # Inject 100 duplicate replication payloads
        evt = {"request_id": self.request_id, "epoch": 1, "type": "COMMIT", "ts": time.time() + 1.0}
        
        # Simulate replication from region A to B
        for _ in range(100):
            payload = {
                "event_id": "duplicate-id-123",
                "source_region": "A",
                "data": json.dumps(evt)
            }
            self.ctrl.redis.xadd("kernell:wal:replication", payload)
            
        # Shuffle stream
        random.shuffle(self.ctrl.redis.streams["kernell:wal:replication"])
        
        # Process via B's consumer
        consumer = ReplicationConsumer(self.ctrl.redis, "B")
        consumer.consume_batch()
