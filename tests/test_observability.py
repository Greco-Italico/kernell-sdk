import pytest
import sqlite3
import time

import sys
import os
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '../../')))

from kernell_os_sdk.billing.metering import MeteringEngine
from kernell_os_sdk.billing.pricing import PricingEngine
from kernell_os_sdk.billing.spend_guard import SpendGuard
from kernell_os_sdk.billing.observability import FinancialObserver
from core.audit.double_entry_ledger import DoubleEntryLedger


def _build_stack(tmp_path):
    """Wires the full billing stack for integration tests."""
    metering_path = str(tmp_path / "metering.sqlite3")
    pricing_path = str(tmp_path / "pricing.sqlite3")
    guard_path = str(tmp_path / "guard.sqlite3")
    ledger_path = str(tmp_path / "ledger.sqlite3")

    ledger = DoubleEntryLedger(ledger_path)
    pricing = PricingEngine(pricing_path)
    guard = SpendGuard(guard_path, ledger=ledger)
    engine = MeteringEngine(metering_path, ledger=ledger, pricing_engine=pricing, window_size=60)
    observer = FinancialObserver(metering_path, guard_path, ledger_path)

    return engine, pricing, guard, ledger, observer


def test_tenant_snapshot_end_to_end(tmp_path):
    """Full pipeline: ingest → aggregate → price → outbox → ledger → observe."""
    engine, pricing, guard, ledger, observer = _build_stack(tmp_path)

    tenant = "acme_corp"
    guard.provision_tenant(tenant, initial_balance_micro=10_000_000)
    pricing.add_price_rule("api_call", "flat", 1000, timestamp=0)

    now = time.time()
    ws = engine.get_window_start(now)
    ts = ws + 10

    # Ingest 100 events
    for i in range(100):
        engine.ingest_event(tenant, f"evt_{i}", "api", "api_call", 1, timestamp=ts)

    # Process pipeline
    engine.aggregate_window(tenant, "api_call", ws, "w1")
    engine.commit_aggregate_to_outbox(tenant, "api_call", ws)
    engine.process_billing_outbox()

    # Observe
    snap = observer.tenant_snapshot(tenant)
    assert snap.tenant_id == tenant
    assert snap.current_balance == 10_000_000  # guard not deducted via metering path
    assert snap.total_spend_alltime == 100 * 1000  # 100 events * 1000 micro
    assert snap.pending_outbox_count == 0  # all processed


def test_system_health_reports_correctly(tmp_path):
    engine, pricing, guard, ledger, observer = _build_stack(tmp_path)

    guard.provision_tenant("t1", initial_balance_micro=500)
    guard.provision_tenant("t2", initial_balance_micro=500)
    pricing.add_price_rule("api_call", "flat", 100, timestamp=0)

    now = time.time()
    ws = engine.get_window_start(now)
    ts = ws + 5

    engine.ingest_event("t1", "e1", "api", "api_call", 10, timestamp=ts)
    engine.ingest_event("t2", "e2", "api", "api_call", 20, timestamp=ts)

    health = observer.system_health()
    assert health.total_tenants == 2
    assert health.total_events_ingested == 2
    assert health.drift_detected is False


def test_drift_detection(tmp_path):
    """Shadow balance manually desynchronized from ledger → drift detected."""
    engine, pricing, guard, ledger, observer = _build_stack(tmp_path)

    tenant = "drifty"
    guard.provision_tenant(tenant, initial_balance_micro=100_000)

    # Manually corrupt shadow balance to simulate drift
    with sqlite3.connect(str(tmp_path / "guard.sqlite3")) as conn:
        conn.execute("UPDATE tenant_budgets SET balance_micro = 999999 WHERE tenant_id = ?", (tenant,))

    # Ledger has no entries for this tenant → net = 0
    # Shadow = 999999, ledger = 0 → drift > tolerance
    health = observer.system_health()
    assert health.drift_detected is True


def test_cost_per_endpoint(tmp_path):
    engine, pricing, guard, ledger, observer = _build_stack(tmp_path)
    pricing.add_price_rule("api_call", "flat", 100, timestamp=0)
    pricing.add_price_rule("gpu_second", "flat", 5000, timestamp=0)

    now = time.time()
    ws = engine.get_window_start(now)
    ts = ws + 5

    for i in range(50):
        engine.ingest_event("t1", f"api_{i}", "api_gateway", "api_call", 1, timestamp=ts)
    for i in range(10):
        engine.ingest_event("t1", f"gpu_{i}", "worker_pool", "gpu_second", 100, timestamp=ts)

    breakdown = observer.cost_per_endpoint("t1")
    assert len(breakdown) == 2

    # gpu_second should be first (higher total_quantity: 10*100=1000 vs 50*1=50)
    assert breakdown[0]["metric_type"] == "gpu_second"
    assert breakdown[0]["total_quantity"] == 1000
    assert breakdown[1]["metric_type"] == "api_call"
    assert breakdown[1]["total_quantity"] == 50
