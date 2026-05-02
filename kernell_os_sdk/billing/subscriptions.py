import sqlite3
import time
import uuid
import logging
from typing import Optional, List, Dict
from dataclasses import dataclass, asdict

from kernell_os_sdk.billing.invoicing import InvoiceEngine
from kernell_os_sdk.billing.payments import PaymentEngine
from kernell_os_sdk.billing.spend_guard import SpendGuard

logger = logging.getLogger(__name__)


@dataclass
class Plan:
    id: str
    name: str
    base_price_micro: int
    billing_cycle: str  # monthly, weekly
    created_at: float

    def to_dict(self):
        return asdict(self)


@dataclass
class Subscription:
    id: str
    tenant_id: str
    plan_id: str
    status: str  # active, past_due, canceled
    current_period_start: float
    current_period_end: float
    billing_cycle: str
    auto_renew: bool
    created_at: float
    updated_at: float

    def to_dict(self):
        return asdict(self)


class SubscriptionEngine:
    """
    SaaS Subscription Orchestrator.
    
    Binds Plans, automates periodic invoicing, triggers auto-payments,
    and manages the Dunning process (suspend on fail).
    """

    def __init__(self, db_path: str = "/var/lib/kernell/subscriptions.sqlite3",
                 invoicing: InvoiceEngine = None,
                 payments: PaymentEngine = None,
                 spend_guard: SpendGuard = None):
        self.db_path = db_path
        self.invoicing = invoicing
        self.payments = payments
        self.spend_guard = spend_guard
        self._ensure_db()

    def _ensure_db(self):
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("PRAGMA journal_mode=WAL;")
            
            conn.execute('''
                CREATE TABLE IF NOT EXISTS plans (
                    id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    base_price_micro BIGINT NOT NULL DEFAULT 0,
                    billing_cycle TEXT NOT NULL,
                    created_at REAL NOT NULL
                )
            ''')
            
            conn.execute('''
                CREATE TABLE IF NOT EXISTS subscriptions (
                    id TEXT PRIMARY KEY,
                    tenant_id TEXT NOT NULL,
                    plan_id TEXT NOT NULL,
                    status TEXT NOT NULL,
                    current_period_start REAL NOT NULL,
                    current_period_end REAL NOT NULL,
                    billing_cycle TEXT NOT NULL,
                    auto_renew BOOLEAN NOT NULL DEFAULT 1,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    UNIQUE(tenant_id) -- Assuming 1 active sub per tenant for simplicity
                )
            ''')

    def create_plan(self, name: str, base_price_micro: int, billing_cycle: str = "monthly") -> Plan:
        plan_id = f"plan_{uuid.uuid4().hex}"
        now = time.time()
        
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                "INSERT INTO plans (id, name, base_price_micro, billing_cycle, created_at) VALUES (?, ?, ?, ?, ?)",
                (plan_id, name, base_price_micro, billing_cycle, now)
            )
            
        return Plan(plan_id, name, base_price_micro, billing_cycle, now)

    def subscribe_tenant(self, tenant_id: str, plan_id: str) -> Subscription:
        """Create or update a subscription for a tenant."""
        now = time.time()
        
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            plan_row = conn.execute("SELECT * FROM plans WHERE id = ?", (plan_id,)).fetchone()
            if not plan_row:
                raise ValueError("Plan not found")
                
            billing_cycle = plan_row['billing_cycle']
            
            # Simple period calculation
            period_length = 86400 * 30 if billing_cycle == 'monthly' else 86400 * 7
            period_end = now + period_length
            
            sub_id = f"sub_{uuid.uuid4().hex}"
            
            # Upsert
            conn.execute('''
                INSERT INTO subscriptions (id, tenant_id, plan_id, status, current_period_start, current_period_end, billing_cycle, auto_renew, created_at, updated_at)
                VALUES (?, ?, ?, 'active', ?, ?, ?, 1, ?, ?)
                ON CONFLICT(tenant_id) DO UPDATE SET
                    plan_id = excluded.plan_id,
                    status = 'active',
                    current_period_start = excluded.current_period_start,
                    current_period_end = excluded.current_period_end,
                    billing_cycle = excluded.billing_cycle,
                    updated_at = excluded.updated_at
            ''', (sub_id, tenant_id, plan_id, now, period_end, billing_cycle, now, now))
            
        return self.get_subscription(tenant_id)

    def get_subscription(self, tenant_id: str) -> Optional[Subscription]:
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute("SELECT * FROM subscriptions WHERE tenant_id = ?", (tenant_id,)).fetchone()
            if row:
                return Subscription(**dict(row))
            return None

    def process_billing_cycle(self) -> int:
        """
        The CRON Worker:
        Detects subscriptions that crossed their period_end,
        generates invoice, attempts auto-charge, advances period.
        Returns the number of processed subscriptions.
        """
        now = time.time()
        processed = 0
        
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            # Find all active or past_due subs whose period ended
            subs = conn.execute(
                "SELECT * FROM subscriptions WHERE status IN ('active', 'past_due') AND current_period_end <= ?", 
                (now,)
            ).fetchall()
            
        for sub_row in subs:
            tenant_id = sub_row['tenant_id']
            sub_id = sub_row['id']
            
            # 1. Generate Invoice via InvoiceEngine
            if self.invoicing:
                inv = self.invoicing.create_invoice(tenant_id, sub_row['current_period_start'], sub_row['current_period_end'])
                self.invoicing.finalize_invoice(inv.id)
                
                # 2. Auto-Charge via PaymentEngine
                if self.payments:
                    try:
                        # Real life: fetch saved payment method. Here we simulate "auto" provider
                        pay = self.payments.initiate_payment(
                            tenant_id=tenant_id, 
                            amount_micro=inv.total_amount_micro, 
                            provider="auto_charge", 
                            invoice_id=inv.id,
                            provider_ref=f"auto_{inv.id}"
                        )
                        # Assume success for now. Dunning handles failure below.
                        self.payments.process_payment_success(pay.id)
                        
                        # 3. Advance Period
                        self._advance_period(sub_row, now)
                        
                    except Exception as e:
                        logger.error(f"Auto-charge failed for {tenant_id}: {e}")
                        self._handle_dunning(tenant_id)
            processed += 1
            
        return processed

    def _advance_period(self, sub_row: sqlite3.Row, now: float):
        """Advances the subscription period after a successful charge."""
        period_length = 86400 * 30 if sub_row['billing_cycle'] == 'monthly' else 86400 * 7
        
        # Exact alignment to avoid drift
        new_start = sub_row['current_period_end']
        new_end = new_start + period_length
        
        # Edge case: if system was offline for months, catch up to 'now'
        while new_end <= now:
            new_start = new_end
            new_end += period_length
            
        with sqlite3.connect(self.db_path) as conn:
            conn.execute('''
                UPDATE subscriptions 
                SET current_period_start = ?, current_period_end = ?, status = 'active', updated_at = ?
                WHERE id = ?
            ''', (new_start, new_end, now, sub_row['id']))

    def _handle_dunning(self, tenant_id: str):
        """
        Dunning Logic:
        If charge fails -> Mark past_due.
        Suspend tenant consumption via SpendGuard.
        """
        now = time.time()
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("UPDATE subscriptions SET status = 'past_due', updated_at = ? WHERE tenant_id = ?", (now, tenant_id))
            
        if self.spend_guard:
            # Suspend execution capability immediately to stop hemorrhage
            self.spend_guard.suspend_tenant(tenant_id)
            logger.warning(f"Tenant {tenant_id} suspended due to failed auto-charge.")
