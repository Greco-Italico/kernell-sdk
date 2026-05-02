import pytest
import sqlite3
import threading
import time
import random

import sys
import os
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '../../')))

from kernell_os_sdk.billing.spend_guard import SpendGuard


def test_concurrent_spend_exactly_n_pass(tmp_path):
    """
    50 concurrent requests, each costing 10.
    Balance = 100. Hard limit = 0.
    Exactly 10 must pass, 40 must be denied.
    """
    db_path = str(tmp_path / "guard.sqlite3")
    guard = SpendGuard(db_path=db_path)
    guard.provision_tenant("t1", initial_balance_micro=100, hard_limit_micro=0)

    results = {"allowed": 0, "denied": 0}
    lock = threading.Lock()

    def spend_task():
        decision = guard.check_and_deduct("t1", 10)
        with lock:
            if decision.allowed:
                results["allowed"] += 1
            else:
                results["denied"] += 1

    threads = [threading.Thread(target=spend_task) for _ in range(50)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert results["allowed"] == 10, f"Expected 10 allowed, got {results['allowed']}"
    assert results["denied"] == 40, f"Expected 40 denied, got {results['denied']}"
    assert guard.get_balance("t1") == 0


def test_hard_limit_blocks(tmp_path):
    """Hard limit = -50 means tenant can go 50 into debt, but not more."""
    db_path = str(tmp_path / "guard.sqlite3")
    guard = SpendGuard(db_path=db_path)
    guard.provision_tenant("t1", initial_balance_micro=0, hard_limit_micro=-50)

    d1 = guard.check_and_deduct("t1", 30)
    assert d1.allowed is True
    assert d1.balance_after == -30

    d2 = guard.check_and_deduct("t1", 30)
    assert d2.allowed is False  # would go to -60, below -50


def test_soft_limit_warns_but_allows(tmp_path):
    """Soft limit triggers warning but doesn't block."""
    db_path = str(tmp_path / "guard.sqlite3")
    guard = SpendGuard(db_path=db_path)
    guard.provision_tenant("t1", initial_balance_micro=100, soft_limit_micro=30, hard_limit_micro=0)

    # Spend 80 → balance = 20, below soft_limit 30 → warning emitted but allowed
    d = guard.check_and_deduct("t1", 80)
    assert d.allowed is True
    assert d.balance_after == 20


def test_top_up_increases_balance(tmp_path):
    db_path = str(tmp_path / "guard.sqlite3")
    guard = SpendGuard(db_path=db_path)
    guard.provision_tenant("t1", initial_balance_micro=0)

    new_balance = guard.top_up("t1", 500)
    assert new_balance == 500

    d = guard.check_and_deduct("t1", 200)
    assert d.allowed is True
    assert d.balance_after == 300


def test_unknown_tenant_denied(tmp_path):
    db_path = str(tmp_path / "guard.sqlite3")
    guard = SpendGuard(db_path=db_path)

    d = guard.check_and_deduct("ghost_tenant", 10)
    assert d.allowed is False
    assert d.reason == "tenant_not_found"


def test_zero_cost_always_allowed(tmp_path):
    db_path = str(tmp_path / "guard.sqlite3")
    guard = SpendGuard(db_path=db_path)
    # Don't even provision — zero cost bypasses everything
    d = guard.check_and_deduct("t1", 0)
    assert d.allowed is True


def test_concurrent_mixed_topup_and_spend(tmp_path):
    """
    Stress test: interleave top-ups and spends from many threads.
    Final balance must be mathematically consistent.
    """
    db_path = str(tmp_path / "guard.sqlite3")
    guard = SpendGuard(db_path=db_path)
    guard.provision_tenant("t1", initial_balance_micro=1000, hard_limit_micro=0)

    allowed_count = {"n": 0}
    lock = threading.Lock()

    def mixed_task(worker_id):
        for _ in range(20):
            if random.random() < 0.3:
                guard.top_up("t1", 5)
            else:
                d = guard.check_and_deduct("t1", 10)
                if d.allowed:
                    with lock:
                        allowed_count["n"] += 1

    threads = [threading.Thread(target=mixed_task, args=(i,)) for i in range(10)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    final_balance = guard.get_balance("t1")
    
    # Count actual top-ups and spends from the event log
    with sqlite3.connect(db_path) as conn:
        topups = conn.execute(
            "SELECT COALESCE(SUM(amount_micro), 0) FROM spend_events WHERE tenant_id='t1' AND event_type='top_up'"
        ).fetchone()[0]
        spends = conn.execute(
            "SELECT COALESCE(SUM(amount_micro), 0) FROM spend_events WHERE tenant_id='t1' AND event_type='spend'"
        ).fetchone()[0]

    expected = 1000 + topups - spends
    assert final_balance == expected, f"Balance drift detected: expected {expected}, got {final_balance}"
    assert final_balance >= 0, "Balance went negative past hard limit"
