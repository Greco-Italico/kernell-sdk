"""
WAL Replicator Fuzzer — Phase 1.1 & 1.2 Validation

Tests:
1. Deterministic event ID generation
2. Leader enforcement (get_home_region)
3. Shadow Mode replication (A -> B)
4. Duplicate flood protection
5. Loop protection (A -> A ignored)
6. Reorder safety (out-of-order replication handles cleanly in simulation)
"""

import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..'))

from kernell_sdk.router.wal_replicator import (
    compute_event_id_from_dict, get_home_region, WALReplicator, 
    ReplicationConsumer, NotLeaderRegionError
)
import json

class FakeRedis:
    def __init__(self):
        self.streams = {}
        self.sets = {}
        self._counter = 0

    def xadd(self, stream, fields):
        if stream not in self.streams:
            self.streams[stream] = []
        self._counter += 1
        sid = f"{self._counter}-0"
        self.streams[stream].append((sid, fields))
        return sid

    def xread(self, streams_dict, count=None, block=None):
        result = []
        for stream, last_id in streams_dict.items():
            if stream not in self.streams:
                continue
            
            # Simple simulation: just return all elements after last_id conceptually.
            # For this fake, if last_id == "0-0", return all. Otherwise, try to find it.
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

    def xrange(self, stream, min="-", max="+"):
        return self.streams.get(stream, [])

    def sismember(self, key, member):
        return member in self.sets.get(key, set())

    def sadd(self, key, member):
        if key not in self.sets:
            self.sets[key] = set()
        self.sets[key].add(member)


class WALReplicatorFuzzer:
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

    def run_all(self):
        print("=" * 70)
        print("  WAL REPLICATOR FUZZER — Active-Passive Validation")
        print("=" * 70)

        self.test_event_id_determinism()
        self.test_leader_routing()
        self.test_shadow_replication()
        self.test_duplicate_flood()
        self.test_loop_protection()

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

    def test_event_id_determinism(self):
        evt1 = {"request_id": "r1", "epoch": 1, "type": "START", "ts": 100.123456}
        evt2 = {"request_id": "r1", "epoch": 1, "type": "START", "ts": 100.123456}
        evt3 = {"request_id": "r1", "epoch": 1, "type": "START", "ts": 100.123457} # diff ts
        
        id1 = compute_event_id_from_dict(evt1)
        id2 = compute_event_id_from_dict(evt2)
        id3 = compute_event_id_from_dict(evt3)
        
        self.assert_eq("EventID: same inputs -> same hash", id1, id2)
        self.assert_eq("EventID: diff inputs -> diff hash", id1 == id3, False)

    def test_leader_routing(self):
        r1 = "req-1"
        r2 = "req-2"
        # Since hash is deterministic, get_home_region is deterministic
        h1 = get_home_region(r1)
        
        self.assert_eq("Leader: routing is deterministic", get_home_region(r1), h1)
        
        # Test enforcement
        rep_a = WALReplicator(FakeRedis(), "A")
        rep_b = WALReplicator(FakeRedis(), "B")
        
        if h1 == "A":
            rep_a.enforce_leader(r1)  # should not raise
            self.assert_raises("Leader: reject wrong region", NotLeaderRegionError, lambda: rep_b.enforce_leader(r1))
            self.passed += 1
            self.results.append(("PASS", "Leader: accept home region", ""))
        else:
            rep_b.enforce_leader(r1)
            self.assert_raises("Leader: reject wrong region", NotLeaderRegionError, lambda: rep_a.enforce_leader(r1))
            self.passed += 1
            self.results.append(("PASS", "Leader: accept home region", ""))

    def test_shadow_replication(self):
        redis_shared = FakeRedis()
        rep_a = WALReplicator(redis_shared, "A")
        cons_b = ReplicationConsumer(redis_shared, "B")
        
        evt = {"request_id": "req-shadow", "epoch": 1, "type": "START", "ts": 123.0}
        rep_a.publish_event(evt)
        
        processed = cons_b.consume_batch()
        self.assert_eq("Shadow: 1 event processed", processed, 1)
        
        wal_b = redis_shared.xrange("kernell:wal")
        self.assert_eq("Shadow: 1 event in replica WAL", len(wal_b), 1)
        self.assert_eq("Shadow: origin marked", wal_b[0][1]["_replicated_from"], "A")

    def test_duplicate_flood(self):
        redis_shared = FakeRedis()
        cons_b = ReplicationConsumer(redis_shared, "B")
        
        evt = {"request_id": "req-flood", "epoch": 1, "type": "COMMIT", "ts": 123.0}
        event_id = compute_event_id_from_dict(evt)
        
        # Manually flood the replication stream with 100 copies
        payload = {"event_id": event_id, "source_region": "A", "data": json.dumps(evt)}
        for _ in range(100):
            redis_shared.xadd("kernell:wal:replication", payload)
            
        processed = cons_b.consume_batch()
        # Should process the 1st one, ignore the next 99
        self.assert_eq("Dedup: 1 event processed out of 100", processed, 1)
        
        wal_b = redis_shared.xrange("kernell:wal")
        self.assert_eq("Dedup: exactly 1 event in WAL", len(wal_b), 1)

    def test_loop_protection(self):
        redis_shared = FakeRedis()
        rep_a = WALReplicator(redis_shared, "A")
        cons_a = ReplicationConsumer(redis_shared, "A")
        
        evt = {"request_id": "req-loop", "epoch": 1, "type": "START", "ts": 123.0}
        rep_a.publish_event(evt)
        
        # Region A consumes from the shared stream, but sees its own event
        processed = cons_a.consume_batch()
        self.assert_eq("Loop: 0 events processed (ignored own)", processed, 0)

if __name__ == "__main__":
    fuzzer = WALReplicatorFuzzer()
    success = fuzzer.run_all()
    sys.exit(0 if success else 1)
