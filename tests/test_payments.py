import pytest
import sqlite3
import time

import sys
import os
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '../../')))

from kernell_sdk.billing.payments import PaymentEngine
from kernell_sdk.billing.invoicing import InvoiceEngine
from kernell_sdk.billing.spend_guard import SpendGuard
from kernell_sdk.billing.metering import MeteringEngine
from core.audit.double_entry_ledger import DoubleEntryLedger


def _build_stack(tmp_path):
    invoice_path = str(tmp_path / "invoicing.sqlite3")
    metering_path = str(tmp_path / "metering.sqlite3")
    ledger_path = str(tmp_path / "ledger.sqlite3")
    guard_path = str(tmp_path / "guard.sqlite3")
    payment_path = str(tmp_path / "payments.sqlite3")

    ledger = DoubleEntryLedger(ledger_path)
    spend_guard = SpendGuard(guard_path, ledger=ledger)
    MeteringEngine(metering_path)  # Init tables
    invoicing = InvoiceEngine(invoice_path, metering_path)
    payments = PaymentEngine(payment_path, invoicing=invoicing, ledger=ledger, spend_guard=spend_guard)

    return invoicing, spend_guard, ledger, payments


def test_payment_invoice_settlement(tmp_path):
    """Full loop: Invoice -> Pending Payment -> Success -> Invoice Paid -> Ledger"""
    invoicing, guard, ledger, payments = _build_stack(tmp_path)
    
    tenant_id = "tenant_biz"
    
    # 1. Create a finalized invoice manually (since we skip metering here)
    inv = invoicing.create_invoice(tenant_id, 0, 3600)
    invoicing.finalize_invoice(inv.id)
    
    # 2. Initiate payment against invoice
    pay = payments.initiate_payment(tenant_id, 1000, "stripe", invoice_id=inv.id, provider_ref="ch_123")
    assert pay.status == "pending"
    assert pay.invoice_id == inv.id
    
    # 3. Process success
    res = payments.process_payment_success(pay.id)
    assert res is True
    
    # 4. Verify Invoice is Paid
    inv_after = invoicing.get_invoice(inv.id)
    assert inv_after.status == "paid"
    
    # 5. Verify Idempotency
    res2 = payments.process_payment_success(pay.id)
    assert res2 is True  # Should not throw or double-process
    
    # 6. Verify Payment Record
    pay_after = payments.get_payment(pay.id)
    assert pay_after.status == "succeeded"


def test_payment_prepaid_topup(tmp_path):
    """Direct payment (no invoice) tops up SpendGuard shadow balance."""
    invoicing, guard, ledger, payments = _build_stack(tmp_path)
    
    tenant_id = "tenant_prepaid"
    guard.provision_tenant(tenant_id, initial_balance_micro=0)
    
    # Initiate standalone payment
    pay = payments.initiate_payment(tenant_id, 5000_000, "wire", provider_ref="wire_456")
    
    # Guard balance should still be 0 while pending
    assert guard.get_balance(tenant_id) == 0
    
    # Success
    payments.process_payment_success(pay.id)
    
    # Guard balance updated!
    assert guard.get_balance(tenant_id) == 5000_000


def test_payment_failure_handling(tmp_path):
    invoicing, guard, ledger, payments = _build_stack(tmp_path)
    
    pay = payments.initiate_payment("t1", 100, "stripe", provider_ref="ch_fail")
    assert pay.status == "pending"
    
    ok = payments.process_payment_failure(pay.id)
    assert ok is True
    
    pay_after = payments.get_payment(pay.id)
    assert pay_after.status == "failed"
    
    # Cannot succeed a failed payment
    with pytest.raises(ValueError):
        payments.process_payment_success(pay.id)


def test_invalid_invoice_payment(tmp_path):
    """Cannot pay a draft invoice or one that doesn't belong to the tenant."""
    invoicing, guard, ledger, payments = _build_stack(tmp_path)
    
    inv = invoicing.create_invoice("t1", 0, 3600)
    assert inv.status == "draft"
    
    with pytest.raises(ValueError, match="Cannot pay invoice in status: draft"):
        payments.initiate_payment("t1", 100, "stripe", invoice_id=inv.id)
        
    invoicing.finalize_invoice(inv.id)
    
    with pytest.raises(ValueError, match="Invoice does not belong to this tenant"):
        payments.initiate_payment("t2", 100, "stripe", invoice_id=inv.id)
