import pytest
import sqlite3
import threading
import time
import random
import uuid

import sys
import os
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '../../')))

from kernell_sdk.billing.metering import MeteringEngine
from kernell_sdk.billing.pricing import PricingEngine
from core.audit.double_entry_ledger import DoubleEntryLedger

def test_metering_engine_chaos(tmp_path):
    """
    Chaos test for MeteringEngine:
    Simulates multiple workers concurrently ingesting events, aggregating, 
    and committing to outbox/ledger under heavy race conditions.
    Validates exactly-once semantics and invariant integrity.
    """
    db_path = str(tmp_path / "metering_chaos.sqlite3")
    ledger_path = str(tmp_path / "ledger_chaos.sqlite3")
    
    ledger = DoubleEntryLedger(ledger_path)
    # create tenant globally just to avoid issues
    ledger.create_account("tenant_A", "wallet_A", "asset")
    
    pricing_path = str(tmp_path / "pricing_chaos.sqlite3")
    pricing_engine = PricingEngine(pricing_path)
    # Add a flat pricing rule
    pricing_engine.add_price_rule("api_call", "flat", 100, timestamp=0)
    
    engine = MeteringEngine(db_path=db_path, ledger=ledger, pricing_engine=pricing_engine, window_size=60)
    
    tenant_id = "tenant_A"
    metric_type = "api_call"
    
    # We will simulate 500 unique events sent across 10 threads.
    # Each thread will try to push events, and occasionally run aggregator and outbox.
    num_events = 500
    events = [{"event_id": f"evt_{i}", "quantity": random.randint(1, 10)} for i in range(num_events)]
    
    # Pre-calculate expected totals
    expected_total_quantity = sum(e["quantity"] for e in events)
    conversion_rate = 100 # 100 micro_cents per quantity
    expected_amount_micro = expected_total_quantity * conversion_rate
    
    # Pin to center of window so 10s jitter never crosses a 60s boundary
    now = time.time()
    window_start_now = engine.get_window_start(now)
    base_ts = window_start_now + 30  # center of the window
    
    def ingest_task(worker_id: int):
        my_events = events.copy()
        random.shuffle(my_events)
        
        for evt in my_events:
            ts = base_ts - random.uniform(0, 10) 
            try:
                engine.ingest_event(
                    tenant_id=tenant_id, 
                    event_id=evt["event_id"], 
                    source=f"worker_{worker_id}", 
                    metric_type=metric_type, 
                    quantity=evt["quantity"], 
                    timestamp=ts
                )
            except ValueError:
                pass
            
            if random.random() < 0.20:
                window_start = engine.get_window_start(ts)
                engine.aggregate_window(tenant_id, metric_type, window_start, f"worker_{worker_id}")

    # Phase 1: Concurrent Ingestion & Aggregation
    threads = []
    for i in range(10):
        t = threading.Thread(target=ingest_task, args=(i,))
        threads.append(t)
        t.start()
        
    for t in threads:
        t.join()
        
    # Final aggregate to make sure all are caught
    window_start = engine.get_window_start(base_ts)
    engine.aggregate_window(tenant_id, metric_type, window_start, "final_worker")
    
    # Phase 2: Concurrent Outbox Commit
    def commit_task(worker_id: int):
        engine.commit_aggregate_to_outbox(tenant_id, metric_type, window_start)

    threads = []
    for i in range(10):
        t = threading.Thread(target=commit_task, args=(i,))
        threads.append(t)
        t.start()
        
    for t in threads:
        t.join()
        
    # Phase 3: Concurrent Ledger Processing
    def process_task(worker_id: int):
        engine.process_billing_outbox()

    threads = []
    for i in range(10):
        t = threading.Thread(target=process_task, args=(i,))
        threads.append(t)
        t.start()
        
    for t in threads:
        t.join()
    
    # Validation Phase
    
    # 1. Check metering_events total
    with sqlite3.connect(db_path) as conn:
        row = conn.execute("SELECT COUNT(*), SUM(quantity) FROM metering_events").fetchone()
        assert row[0] == num_events, f"Expected {num_events} unique events, got {row[0]}"
        assert row[1] == expected_total_quantity, f"Expected quantity {expected_total_quantity}, got {row[1]}"
        
    # 2. Check metering_aggregates
    with sqlite3.connect(db_path) as conn:
        rows = conn.execute("SELECT total_quantity, status FROM metering_aggregates WHERE tenant_id=? AND metric_type=?", (tenant_id, metric_type)).fetchall()
        assert len(rows) == 1, "Expected exactly 1 window aggregate"
        assert rows[0][0] == expected_total_quantity, f"Expected aggregate {expected_total_quantity}, got {rows[0][0]}"
        assert rows[0][1] == 'committed', f"Aggregate status should be committed, got {rows[0][1]}"
        
    # 3. Check billing_outbox
    with sqlite3.connect(db_path) as conn:
        rows = conn.execute("SELECT amount_micro, status FROM billing_outbox WHERE tenant_id=? AND metric_type=?", (tenant_id, metric_type)).fetchall()
        assert len(rows) == 1, "Expected exactly 1 outbox entry (no double billing)"
        assert rows[0][0] == expected_amount_micro, f"Expected amount {expected_amount_micro}, got {rows[0][0]}"
        assert rows[0][1] == 'processed', "Outbox entry should be processed"
        
    # 4. Check Ledger
    with sqlite3.connect(ledger_path) as conn:
        # One entry for the funding, one for the billing. Oh wait, we didn't fund. Just billing.
        rows = conn.execute("SELECT COUNT(*) FROM journal_entries WHERE tenant_id=?", (tenant_id,)).fetchall()
        assert rows[0][0] == 1, "Expected exactly 1 ledger journal entry"
        
        lines = conn.execute("SELECT account_id, direction, amount_micro FROM journal_lines WHERE tenant_id=?", (tenant_id,)).fetchall()
        assert len(lines) == 2, "Expected exactly 2 ledger lines (debit/credit)"
        
        # Verify the global invariant holds
        assert ledger.check_global_invariant() is True
        # Verify the tenant invariant holds
        assert ledger.check_tenant_invariant(tenant_id) is True
