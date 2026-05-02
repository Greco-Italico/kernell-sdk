import pytest
import sqlite3
import time

import sys
import os
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '../../')))

from kernell_os_sdk.billing.metering import MeteringEngine
from kernell_os_sdk.billing.pricing import PricingEngine
from core.audit.double_entry_ledger import DoubleEntryLedger


def test_pricing_engine_temporal_accuracy(tmp_path):
    """
    Validates that events in different time windows are priced using the
    pricing rule that was active at the window_start timestamp.
    Uses real-time offsets to avoid the metering engine's time guards.
    """
    db_path = str(tmp_path / "metering.sqlite3")
    ledger_path = str(tmp_path / "ledger.sqlite3")
    pricing_path = str(tmp_path / "pricing.sqlite3")

    ledger = DoubleEntryLedger(ledger_path)
    pricing_engine = PricingEngine(pricing_path)
    engine = MeteringEngine(db_path=db_path, ledger=ledger, pricing_engine=pricing_engine, window_size=3600)

    tenant_id = "tenant_test"
    metric_type = "gpu_second"

    now = time.time()
    # Window A: starts 2 hours ago
    window_a_start = engine.get_window_start(now - 7200)
    # Window B: starts 1 hour ago
    window_b_start = engine.get_window_start(now - 3600)

    # Price v1: valid from the beginning of window A
    pricing_engine.add_price_rule(metric_type, "flat", 1_000_000, timestamp=window_a_start)
    # Price v2: valid from the beginning of window B (expires v1)
    pricing_engine.add_price_rule(metric_type, "flat", 2_000_000, timestamp=window_b_start)

    # Ingest events — timestamps inside each window
    ts_a = window_a_start + 100
    ts_b = window_b_start + 100
    engine.ingest_event(tenant_id, "evt1", "test", metric_type, 10, timestamp=ts_a)
    engine.ingest_event(tenant_id, "evt2", "test", metric_type, 10, timestamp=ts_b)

    # Aggregate both windows
    engine.aggregate_window(tenant_id, metric_type, window_a_start, "w1")
    engine.aggregate_window(tenant_id, metric_type, window_b_start, "w1")

    # Commit both to outbox
    engine.commit_aggregate_to_outbox(tenant_id, metric_type, window_a_start)
    engine.commit_aggregate_to_outbox(tenant_id, metric_type, window_b_start)

    # Verify outbox amounts and versions
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        outbox = conn.execute("SELECT * FROM billing_outbox ORDER BY window_start ASC").fetchall()

        assert len(outbox) == 2

        window_0 = outbox[0]
        assert window_0['window_start'] == window_a_start
        assert window_0['amount_micro'] == 10 * 1_000_000  # v1 pricing
        assert window_0['pricing_version'] == 1

        window_1 = outbox[1]
        assert window_1['window_start'] == window_b_start
        assert window_1['amount_micro'] == 10 * 2_000_000  # v2 pricing
        assert window_1['pricing_version'] == 2


def test_pricing_engine_tiered(tmp_path):
    pricing_path = str(tmp_path / "pricing.sqlite3")
    pricing_engine = PricingEngine(pricing_path)

    # 0-100: $1, >100: $0.5
    tiers = {
        "tiers": [
            {"up_to": 100, "price": 1_000_000},
            {"up_to": None, "price": 500_000}
        ]
    }
    pricing_engine.add_price_rule("api_call", "tiered", 0, metadata=tiers, timestamp=0)

    amount, ver, model = pricing_engine.calculate(None, "api_call", 150, 1000)
    # 100 * 1,000,000 + 50 * 500,000 = 125,000,000
    assert amount == 125_000_000

    amount, ver, model = pricing_engine.calculate(None, "api_call", 50, 1000)
    # 50 * 1,000,000 = 50,000,000
    assert amount == 50_000_000


def test_pricing_engine_volume(tmp_path):
    pricing_path = str(tmp_path / "pricing.sqlite3")
    pricing_engine = PricingEngine(pricing_path)

    # <=100: $1, >100: $0.5 for all
    tiers = {
        "tiers": [
            {"up_to": 100, "price": 1_000_000},
            {"up_to": None, "price": 500_000}
        ]
    }
    pricing_engine.add_price_rule("api_call", "volume", 0, metadata=tiers, timestamp=0)

    amount, ver, model = pricing_engine.calculate(None, "api_call", 150, 1000)
    # 150 * 500,000 = 75,000,000
    assert amount == 75_000_000

    amount, ver, model = pricing_engine.calculate(None, "api_call", 50, 1000)
    # 50 * 1,000,000 = 50,000,000
    assert amount == 50_000_000
