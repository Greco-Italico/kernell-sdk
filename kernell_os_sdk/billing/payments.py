import sqlite3
import time
import uuid
import logging
from typing import Optional, List, Dict
from dataclasses import dataclass, asdict

from kernell_os_sdk.billing.invoicing import InvoiceEngine
from kernell_os_sdk.billing.spend_guard import SpendGuard
from core.audit.double_entry_ledger import DoubleEntryLedger

logger = logging.getLogger(__name__)


@dataclass
class PaymentRecord:
    id: str
    tenant_id: str
    invoice_id: Optional[str]
    amount_micro: int
    currency: str
    status: str  # pending, succeeded, failed, refunded
    provider: str
    provider_ref: Optional[str]
    created_at: float
    updated_at: float

    def to_dict(self) -> dict:
        return asdict(self)


class PaymentEngine:
    """
    Payment reconciliation and settlement layer.
    
    Closes the financial loop by:
    1. Tracking real money movement (payments).
    2. Reconciling payments against invoices.
    3. Emitting definitive DoubleEntryLedger records for actual cash flow.
    4. Top-ups logic for prepaid tenants via SpendGuard.
    """

    def __init__(self, db_path: str = "/var/lib/kernell/payments.sqlite3",
                 invoicing: InvoiceEngine = None,
                 ledger: DoubleEntryLedger = None,
                 spend_guard: SpendGuard = None):
        self.db_path = db_path
        self.invoicing = invoicing
        self.ledger = ledger
        self.spend_guard = spend_guard
        self._ensure_db()

    def _ensure_db(self):
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("PRAGMA journal_mode=WAL;")
            conn.execute('''
                CREATE TABLE IF NOT EXISTS payments (
                    id TEXT PRIMARY KEY,
                    tenant_id TEXT NOT NULL,
                    invoice_id TEXT,
                    amount_micro BIGINT NOT NULL,
                    currency TEXT NOT NULL DEFAULT 'usd',
                    status TEXT NOT NULL,
                    provider TEXT NOT NULL,
                    provider_ref TEXT,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    UNIQUE(provider, provider_ref)
                )
            ''')

    def initiate_payment(self, tenant_id: str, amount_micro: int, provider: str, 
                         invoice_id: Optional[str] = None, provider_ref: Optional[str] = None) -> PaymentRecord:
        """
        Record a new payment attempt.
        """
        now = time.time()
        payment_id = f"pay_{uuid.uuid4().hex}"

        if invoice_id and self.invoicing:
            inv = self.invoicing.get_invoice(invoice_id)
            if not inv:
                raise ValueError(f"Invoice {invoice_id} not found")
            if inv.status != 'finalized':
                raise ValueError(f"Cannot pay invoice in status: {inv.status}")
            if inv.tenant_id != tenant_id:
                raise ValueError("Invoice does not belong to this tenant")

        with sqlite3.connect(self.db_path) as conn:
            try:
                conn.execute('''
                    INSERT INTO payments (id, tenant_id, invoice_id, amount_micro, currency, status, provider, provider_ref, created_at, updated_at)
                    VALUES (?, ?, ?, ?, 'usd', 'pending', ?, ?, ?, ?)
                ''', (payment_id, tenant_id, invoice_id, amount_micro, provider, provider_ref, now, now))
            except sqlite3.IntegrityError:
                raise ValueError(f"Payment with provider_ref {provider_ref} already exists")

        return PaymentRecord(
            id=payment_id, tenant_id=tenant_id, invoice_id=invoice_id,
            amount_micro=amount_micro, currency='usd', status='pending',
            provider=provider, provider_ref=provider_ref, created_at=now, updated_at=now
        )

    def process_payment_success(self, payment_id: str) -> bool:
        """
        Idempotent success handler.
        1. Marks payment succeeded.
        2. If linked to invoice -> marks invoice paid.
        3. Impacts ledger (Cash increases, AR decreases).
        4. If prepaid top-up -> hits SpendGuard.
        """
        now = time.time()
        
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            pay = conn.execute("SELECT * FROM payments WHERE id = ?", (payment_id,)).fetchone()
            if not pay:
                raise ValueError("Payment not found")
                
            if pay['status'] == 'succeeded':
                return True  # Idempotent success
                
            if pay['status'] != 'pending':
                raise ValueError(f"Cannot succeed payment in status: {pay['status']}")

            # Update status
            conn.execute("UPDATE payments SET status = 'succeeded', updated_at = ? WHERE id = ?", (now, payment_id))

        # Handle Invoice Settlement
        if pay['invoice_id']:
            if self.invoicing:
                # 1. Mark Invoice Paid
                self.invoicing.mark_paid(pay['invoice_id'])
                
                # 2. Ledger Impact (Accounts Receivable -> Cash)
                if self.ledger:
                    # In a real accounting setup:
                    # DEBIT: Bank/Cash account (System level)
                    # CREDIT: Tenant Accounts Receivable
                    self.ledger.save_incoming_event(
                        tenant_id=pay['tenant_id'],
                        event_id=f"settle_{payment_id}",
                        payload=f"Invoice Settlement {pay['invoice_id']}"
                    )
                    # Note: We would have a dedicated ledger helper for dual-entry here.
                    
        # Handle Prepaid Top-up
        else:
            if self.spend_guard:
                self.spend_guard.top_up(pay['tenant_id'], pay['amount_micro'])
                
            if self.ledger:
                # DEBIT: Bank/Cash
                # CREDIT: Tenant Prepaid Liability
                self.ledger.save_incoming_event(
                    tenant_id=pay['tenant_id'],
                    event_id=f"topup_{payment_id}",
                    payload=f"Prepaid Top-up"
                )

        return True

    def process_payment_failure(self, payment_id: str) -> bool:
        """Marks a payment as failed."""
        now = time.time()
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.execute(
                "UPDATE payments SET status = 'failed', updated_at = ? WHERE id = ? AND status = 'pending'", 
                (now, payment_id)
            )
            return cursor.rowcount > 0

    def get_payment(self, payment_id: str) -> Optional[PaymentRecord]:
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute("SELECT * FROM payments WHERE id = ?", (payment_id,)).fetchone()
            if row:
                return PaymentRecord(**dict(row))
            return None
