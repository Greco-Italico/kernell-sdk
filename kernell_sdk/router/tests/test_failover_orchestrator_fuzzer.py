"""
Failover Orchestrator Fuzzer — Phase 1.4 Validation

Validates autonomous takeover conditions, split-brain protection,
anti-thrashing backoff, and quorum guard.
"""

import sys
import os
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..'))

from kernell_sdk.router.failover_orchestrator import (
    HeartbeatEmitter, HeartbeatMonitor, QuorumGuard, FailoverOrchestrator
)
from kernell_sdk.router.lease_manager import LeaseManager
import json

class FakeRedis:
    def __init__(self):
        self.store = {}
        self.streams = {}
        self.ping_fails = False

    def set(self, key, value, ex=None):
        self.store[key] = value

    def get(self, key):
        return self.store.get(key)
        
    def xadd(self, stream, fields):
        if stream not in self.streams:
            self.streams[stream] = []
        self.streams[stream].append(fields)
        return "1-0"

    def ping(self):
        if self.ping_fails:
            raise Exception("Connection Error")
        return True


class OrchestratorFuzzer:
    def __init__(self):
        self.passed = 0
        self.failed = 0
        self.results = []

    def assert_eq(self, name, actual, expected):
        if actual == expected:
            self.passed += 1
            self.results.append(("PASS", name, ""))
        else:
            self.failed += 1
            self.results.append(("FAIL", name, f"expected={expected}, got={actual}"))

    def assert_raises(self, name, exc_type, fn):
        try:
            fn()
            self.failed += 1
            self.results.append(("FAIL", name, "No exception raised"))
        except exc_type:
            self.passed += 1
            self.results.append(("PASS", name, ""))

    def fresh(self, region):
        r = FakeRedis()
        lease = LeaseManager(r, region)
        orch = FailoverOrchestrator(r, region, lease)
        return r, lease, orch

    def run_all(self):
        print("=" * 70)
        print("  FAILOVER ORCHESTRATOR FUZZER — Autonomous Active-Active")
        print("=" * 70)

        self.test_leader_dies_takeover_occurs()
        self.test_leader_alive_no_takeover()
        self.test_local_network_down_no_takeover()
        self.test_thrashing_backoff()
        self.test_split_brain_partition()

        print()
        print("-" * 70)
        for status, name, detail in self.results:
            icon = "✅" if status == "PASS" else "❌"
            suffix = f"  ({detail})" if detail and status == "FAIL" else ""
            print(f"  {icon} {name}{suffix}")
        print("-" * 70)
        total = self.passed + self.failed
        print(f"\n  Results: {self.passed}/{total} passed", end="")
        if self.failed > 0:
            print(f" — {self.failed} FAILED ⚠️")
        else:
            print(" — ALL CLEAR 🟢")
        print("=" * 70)
        return self.failed == 0

    def test_leader_dies_takeover_occurs(self):
        r, lease_b, orch_b = self.fresh("B")
        # Simulate A as leader
        lease_a = LeaseManager(r, "A")
        lease_a.acquire("req-1", epoch=1, ttl=10.0)
        
        # A's heartbeat is OLD (dead)
        r.set("kernell:heartbeat:A", json.dumps({"ts": time.time() - 10, "status": "healthy"}))
        # B's heartbeat is ALIVE
        r.set("kernell:heartbeat:B", json.dumps({"ts": time.time(), "status": "healthy"}))
        
        taken = orch_b.tick(["req-1"])
        
        self.assert_eq("Leader dies -> takeover occurs", len(taken), 1)
        self.assert_eq("Takeover -> epoch increments", lease_b.get("req-1")["epoch"], 2)
        self.assert_eq("Takeover -> WAL event emitted", len(r.streams["kernell:wal"]), 1)
        self.assert_eq("Takeover -> B is new leader", lease_b.get("req-1")["holder"], "B")

    def test_leader_alive_no_takeover(self):
        r, lease_b, orch_b = self.fresh("B")
        lease_a = LeaseManager(r, "A")
        lease_a.acquire("req-2", epoch=1, ttl=10.0)
        
        # A's heartbeat is FRESH
        r.set("kernell:heartbeat:A", json.dumps({"ts": time.time(), "status": "healthy"}))
        # B's heartbeat is ALIVE
        r.set("kernell:heartbeat:B", json.dumps({"ts": time.time(), "status": "healthy"}))
        
        taken = orch_b.tick(["req-2"])
        
        self.assert_eq("Leader alive -> NO takeover", len(taken), 0)
        self.assert_eq("Leader alive -> leader unchanged", lease_b.get("req-2")["holder"], "A")

    def test_local_network_down_no_takeover(self):
        r, lease_b, orch_b = self.fresh("B")
        lease_a = LeaseManager(r, "A")
        lease_a.acquire("req-3", epoch=1, ttl=10.0)
        
        # A's heartbeat is OLD (seems dead)
        r.set("kernell:heartbeat:A", json.dumps({"ts": time.time() - 10, "status": "healthy"}))
        
        # B's heartbeat is ALSO OLD (B is disconnected from Redis, or network partitioned)
        r.set("kernell:heartbeat:B", json.dumps({"ts": time.time() - 10, "status": "healthy"}))
        
        taken = orch_b.tick(["req-3"])
        
        self.assert_eq("Local net down -> NO takeover (Anti-suicide)", len(taken), 0)
        
        # What if B's heartbeat is fresh, but B's redis connection drops entirely?
        r.set("kernell:heartbeat:B", json.dumps({"ts": time.time(), "status": "healthy"}))
        r.ping_fails = True
        taken = orch_b.tick(["req-3"])
        self.assert_eq("Redis disconnected -> NO takeover", len(taken), 0)

    def test_thrashing_backoff(self):
        r, lease_b, orch_b = self.fresh("B")
        lease_a = LeaseManager(r, "A")
        lease_a.acquire("req-4", epoch=1, ttl=10.0)
        
        # A's heartbeat is dead, B is alive
        r.set("kernell:heartbeat:A", json.dumps({"ts": time.time() - 10, "status": "healthy"}))
        r.set("kernell:heartbeat:B", json.dumps({"ts": time.time(), "status": "healthy"}))
        
        # Tick 1: Takeover happens
        taken = orch_b.tick(["req-4"])
        self.assert_eq("Thrashing: 1st takeover works", len(taken), 1)
        
        # Force A to take it back instantly (simulate thrashing)
        lease_a.takeover("req-4", ttl=10.0)
        
        # Tick 2: B should NOT takeover immediately due to backoff
        taken2 = orch_b.tick(["req-4"])
        self.assert_eq("Thrashing: 2nd takeover blocked by backoff", len(taken2), 0)

    def test_split_brain_partition(self):
        # Scenario: Redis is shared (or synced), but both A and B think the other is dead
        r = FakeRedis()
        
        lease_a = LeaseManager(r, "A")
        orch_a = FailoverOrchestrator(r, "A", lease_a)
        
        lease_b = LeaseManager(r, "B")
        orch_b = FailoverOrchestrator(r, "B", lease_b)
        
        # Initial state: C is leader (just for setup), but C is dead
        lease_c = LeaseManager(r, "C")
        lease_c.acquire("req-split", epoch=3, ttl=10.0)
        r.set("kernell:heartbeat:C", json.dumps({"ts": time.time() - 10, "status": "healthy"}))
        
        # Both A and B are alive (can write to Redis)
        r.set("kernell:heartbeat:A", json.dumps({"ts": time.time(), "status": "healthy"}))
        r.set("kernell:heartbeat:B", json.dumps({"ts": time.time(), "status": "healthy"}))
        
        # Network partition simulation: A thinks B is dead, B thinks A is dead
        # Because C is dead, A takes over from C
        taken_a = orch_a.tick(["req-split"])
        epoch_a = lease_a.get("req-split")["epoch"]
        
        # Now B ticks. In a network partition, B cannot see A's heartbeat.
        # Let's mock B's monitor to think A is dead.
        original_is_alive_b = orch_b.monitor.is_alive
        def partitioned_is_alive_b(region, threshold=2.0):
            if region == "A": return False
            return original_is_alive_b(region, threshold)
        orch_b.monitor.is_alive = partitioned_is_alive_b
        
        # B takes over (overwriting A's takeover due to split brain)
        taken_b = orch_b.tick(["req-split"])
        epoch_b = lease_b.get("req-split")["epoch"]
        
        self.assert_eq("Split-brain: A performs takeover", len(taken_a), 1)
        self.assert_eq("Split-brain: B performs takeover", len(taken_b), 1)
        
        # Because they both did a takeover sequentially on the same state, 
        # B overwrote A. B's epoch is 5, A's epoch was 4.
        self.assert_eq("Split-brain: A gets epoch 4", epoch_a, 4)
        self.assert_eq("Split-brain: B gets epoch 5", epoch_b, 5)
        
        # If A tries to write using its lease (epoch 4), it will be fenced
        # because the current lease epoch is 5.
        from kernell_sdk.router.lease_manager import FencedError, NotLeaderError
        
        def a_writes():
            # A tries to write assuming it still holds epoch 4
            lease_a.validate_write("req-split", 4)
            
        self.assert_raises("Split-brain: A is fenced after B takes over", FencedError, a_writes)


if __name__ == "__main__":
    fuzzer = OrchestratorFuzzer()
    success = fuzzer.run_all()
    sys.exit(0 if success else 1)
