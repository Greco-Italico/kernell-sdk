import pytest
import sqlite3
import time

import sys
import os
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '../../')))

from kernell_sdk.billing.metering import MeteringEngine
from kernell_sdk.billing.pricing import PricingEngine
from kernell_sdk.billing.invoicing import InvoiceEngine
from core.audit.double_entry_ledger import DoubleEntryLedger


def _build_stack(tmp_path):
    metering_path = str(tmp_path / "metering.sqlite3")
    pricing_path = str(tmp_path / "pricing.sqlite3")
    ledger_path = str(tmp_path / "ledger.sqlite3")
    invoice_path = str(tmp_path / "invoicing.sqlite3")

    ledger = DoubleEntryLedger(ledger_path)
    pricing = PricingEngine(pricing_path)
    engine = MeteringEngine(metering_path, ledger=ledger, pricing_engine=pricing, window_size=60)
    invoicing = InvoiceEngine(invoice_path, metering_path)

    return engine, pricing, ledger, invoicing


def _ingest_and_bill(engine, pricing, tenant, metric, count, ts):
    """Helper: ingest events, aggregate, price, outbox, ledger."""
    ws = engine.get_window_start(ts)
    for i in range(count):
        engine.ingest_event(tenant, f"{metric}_{ws}_{i}", "api", metric, 1, timestamp=ts)
    engine.aggregate_window(tenant, metric, ws, "w1")
    engine.commit_aggregate_to_outbox(tenant, metric, ws)
    engine.process_billing_outbox()


def test_invoice_end_to_end(tmp_path):
    """Full pipeline → invoice generation → finalization → CSV export."""
    engine, pricing, ledger, invoicing = _build_stack(tmp_path)
    pricing.add_price_rule("api_call", "flat", 500, timestamp=0)

    tenant = "acme"
    now = time.time()
    ws = engine.get_window_start(now)
    ts = ws + 5

    _ingest_and_bill(engine, pricing, tenant, "api_call", 200, ts)

    # Create invoice covering this period
    period_start = ws - 10
    period_end = now + 3600
    inv = invoicing.create_invoice(tenant, period_start, period_end)

    assert inv.status == "draft"
    assert inv.total_amount_micro == 200 * 500
    assert len(inv.lines) == 1
    assert inv.lines[0].metric_type == "api_call"
    assert inv.lines[0].quantity == 200

    # Finalize
    ok = invoicing.finalize_invoice(inv.id)
    assert ok is True

    # Cannot finalize again
    ok2 = invoicing.finalize_invoice(inv.id)
    assert ok2 is False

    # CSV export
    csv_str = invoicing.export_csv(inv.id)
    assert "api_call" in csv_str
    assert "100000" in csv_str  # 200 * 500 = 100000
    assert "TOTAL" in csv_str


def test_invoice_idempotency(tmp_path):
    """Creating the same invoice twice returns the same one."""
    engine, pricing, ledger, invoicing = _build_stack(tmp_path)
    pricing.add_price_rule("api_call", "flat", 100, timestamp=0)

    now = time.time()
    ws = engine.get_window_start(now)
    _ingest_and_bill(engine, pricing, "t1", "api_call", 10, ws + 5)

    inv1 = invoicing.create_invoice("t1", 0, now + 3600)
    inv2 = invoicing.create_invoice("t1", 0, now + 3600)
    assert inv1.id == inv2.id


def test_credit_note(tmp_path):
    """Credit notes can only be issued against finalized invoices."""
    engine, pricing, ledger, invoicing = _build_stack(tmp_path)
    pricing.add_price_rule("api_call", "flat", 100, timestamp=0)

    now = time.time()
    ws = engine.get_window_start(now)
    _ingest_and_bill(engine, pricing, "t1", "api_call", 50, ws + 5)

    inv = invoicing.create_invoice("t1", 0, now + 3600)

    # Cannot credit a draft
    with pytest.raises(ValueError, match="draft"):
        invoicing.create_credit_note(inv.id, 1000, "test error")

    # Finalize first
    invoicing.finalize_invoice(inv.id)

    # Now credit note works
    cn_id = invoicing.create_credit_note(inv.id, 1000, "Overcharge correction")
    assert cn_id.startswith("cn_")

    # Verify credit note stored
    with sqlite3.connect(str(tmp_path / "invoicing.sqlite3")) as conn:
        cn = conn.execute("SELECT * FROM credit_notes WHERE id = ?", (cn_id,)).fetchone()
        assert cn is not None


def test_multi_metric_invoice(tmp_path):
    """Invoice with multiple metric types produces multiple lines."""
    engine, pricing, ledger, invoicing = _build_stack(tmp_path)
    pricing.add_price_rule("api_call", "flat", 100, timestamp=0)
    pricing.add_price_rule("gpu_second", "flat", 5000, timestamp=0)

    now = time.time()
    ws = engine.get_window_start(now)
    ts = ws + 5

    _ingest_and_bill(engine, pricing, "t1", "api_call", 100, ts)
    _ingest_and_bill(engine, pricing, "t1", "gpu_second", 10, ts)

    inv = invoicing.create_invoice("t1", 0, now + 3600)

    assert len(inv.lines) == 2
    # api_call: 100 * 100 = 10,000
    # gpu_second: 10 * 5000 = 50,000
    assert inv.total_amount_micro == 60_000

    metrics = {l.metric_type: l for l in inv.lines}
    assert metrics["api_call"].amount_micro == 10_000
    assert metrics["gpu_second"].amount_micro == 50_000


def test_list_invoices(tmp_path):
    engine, pricing, ledger, invoicing = _build_stack(tmp_path)
    pricing.add_price_rule("api_call", "flat", 100, timestamp=0)

    now = time.time()
    ws = engine.get_window_start(now)
    _ingest_and_bill(engine, pricing, "t1", "api_call", 10, ws + 5)

    invoicing.create_invoice("t1", 0, now + 3600)
    invoicing.create_invoice("t1", now + 3600, now + 7200)

    all_inv = invoicing.list_invoices("t1")
    assert len(all_inv) == 2

    drafts = invoicing.list_invoices("t1", status="draft")
    assert len(drafts) == 2
