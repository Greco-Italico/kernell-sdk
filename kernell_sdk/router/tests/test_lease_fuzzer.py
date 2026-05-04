"""
Epoch Leasing Fuzzer — Phase 1.3 Validation
"""

import sys
import os
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..'))

from kernell_sdk.router.lease_manager import (
    LeaseManager, FencedError, NotLeaderError, LeaseExpiredError, NoLeaseError
)
import json

class FakeRedis:
    def __init__(self):
        self.store = {}

    def set(self, key, value):
        self.store[key] = value

    def get(self, key):
        return self.store.get(key)


class LeaseFuzzer:
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
        print("  EPOCH LEASING FUZZER — Fencing & Failover Validation")
        print("=" * 70)

        self.test_happy_path()
        self.test_wrong_region()
        self.test_lease_expired()
        self.test_epoch_fencing()
        self.test_takeover()
        self.test_old_leader_fenced()

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

    def test_happy_path(self):
        r = FakeRedis()
        manager_a = LeaseManager(r, "A")
        
        # Region A acquires lease
        manager_a.acquire("req-1", epoch=1, ttl=10.0)
        
        # Validates successfully
        self.assert_eq("Valid lease allows write", manager_a.validate_write("req-1", 1), True)

    def test_wrong_region(self):
        r = FakeRedis()
        manager_a = LeaseManager(r, "A")
        manager_b = LeaseManager(r, "B")
        
        manager_a.acquire("req-2", epoch=1, ttl=10.0)
        
        # Region B tries to write using epoch 1
        self.assert_raises("Reject wrong region", NotLeaderError, lambda: manager_b.validate_write("req-2", 1))

    def test_lease_expired(self):
        r = FakeRedis()
        manager_a = LeaseManager(r, "A")
        
        # Region A acquires lease but it expires
        manager_a.acquire("req-3", epoch=1, ttl=-1.0) # Expired immediately
        
        self.assert_raises("Reject expired lease", LeaseExpiredError, lambda: manager_a.validate_write("req-3", 1))

    def test_epoch_fencing(self):
        r = FakeRedis()
        manager_a = LeaseManager(r, "A")
        
        manager_a.acquire("req-4", epoch=2, ttl=10.0)
        
        # Region A tries to write with an older epoch (e.g. delayed packet)
        self.assert_raises("Fence old epoch", FencedError, lambda: manager_a.validate_write("req-4", 1))

    def test_takeover(self):
        r = FakeRedis()
        manager_a = LeaseManager(r, "A")
        manager_b = LeaseManager(r, "B")
        
        manager_a.acquire("req-5", epoch=1, ttl=10.0)
        
        # B does a takeover
        new_lease = manager_b.takeover("req-5", ttl=10.0)
        
        self.assert_eq("Takeover assigns new leader", new_lease["holder"], "B")
        self.assert_eq("Takeover increments epoch", new_lease["epoch"], 2)
        
        # B can now write with epoch 2
        self.assert_eq("New leader can write", manager_b.validate_write("req-5", 2), True)

    def test_old_leader_fenced(self):
        r = FakeRedis()
        manager_a = LeaseManager(r, "A")
        manager_b = LeaseManager(r, "B")
        
        manager_a.acquire("req-6", epoch=1, ttl=10.0)
        
        # B does a takeover
        manager_b.takeover("req-6", ttl=10.0)
        
        # A comes back online and tries to write with its old epoch 1
        self.assert_raises("Fence resurrected leader", FencedError, lambda: manager_a.validate_write("req-6", 1))
        # Even if A tries to write with epoch 2, it fails because it's no longer the holder
        self.assert_raises("Fence old leader new epoch", NotLeaderError, lambda: manager_a.validate_write("req-6", 2))


if __name__ == "__main__":
    fuzzer = LeaseFuzzer()
    success = fuzzer.run_all()
    sys.exit(0 if success else 1)
