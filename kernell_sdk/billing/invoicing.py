import sqlite3
import time
import uuid
import csv
import io
import json
import os
import logging
from typing import Optional, List, Dict
from dataclasses import dataclass, asdict

logger = logging.getLogger(__name__)


@dataclass
class InvoiceLine:
    id: str
    invoice_id: str
    metric_type: str
    quantity: int
    amount_micro: int
    pricing_version: Optional[int]
    pricing_model: Optional[str]
    window_start: float
    window_end: float


@dataclass
class Invoice:
    id: str
    tenant_id: str
    period_start: float
    period_end: float
    total_amount_micro: int
    currency: str
    status: str  # draft, finalized, paid, void
    created_at: float
    finalized_at: Optional[float]
    lines: List[InvoiceLine]

    def to_dict(self) -> dict:
        d = asdict(self)
        return d


class InvoiceEngine:
    """
    Invoice generation engine.
    
    Reads from billing_outbox (processed entries) to build immutable invoices.
    Once finalized, an invoice never changes. Corrections use credit notes.
    """

    def __init__(self, db_path: str = "/var/lib/kernell/invoicing.sqlite3",
                 metering_db_path: str = "/var/lib/kernell/metering.sqlite3"):
        self.db_path = db_path
        self.metering_db_path = metering_db_path
        self._ensure_db()

    def _ensure_db(self):
        if self.db_path != ":memory:":
            os.makedirs(os.path.dirname(self.db_path) or ".", exist_ok=True)
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("PRAGMA journal_mode=WAL;")
            conn.execute('''
                CREATE TABLE IF NOT EXISTS invoices (
                    id TEXT PRIMARY KEY,
                    tenant_id TEXT NOT NULL,
                    period_start REAL NOT NULL,
                    period_end REAL NOT NULL,
                    total_amount_micro BIGINT NOT NULL DEFAULT 0,
                    currency TEXT NOT NULL DEFAULT 'usd',
                    status TEXT NOT NULL DEFAULT 'draft',
                    created_at REAL NOT NULL,
                    finalized_at REAL,
                    UNIQUE(tenant_id, period_start, period_end)
                )
            ''')
            conn.execute('''
                CREATE TABLE IF NOT EXISTS invoice_lines (
                    id TEXT PRIMARY KEY,
                    invoice_id TEXT NOT NULL,
                    metric_type TEXT NOT NULL,
                    quantity BIGINT NOT NULL,
                    amount_micro BIGINT NOT NULL,
                    pricing_version INT,
                    pricing_model TEXT,
                    window_start REAL NOT NULL,
                    window_end REAL NOT NULL,
                    FOREIGN KEY (invoice_id) REFERENCES invoices(id)
                )
            ''')
            conn.execute('''
                CREATE TABLE IF NOT EXISTS credit_notes (
                    id TEXT PRIMARY KEY,
                    invoice_id TEXT NOT NULL,
                    tenant_id TEXT NOT NULL,
                    amount_micro BIGINT NOT NULL,
                    reason TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    FOREIGN KEY (invoice_id) REFERENCES invoices(id)
                )
            ''')

    def create_invoice(self, tenant_id: str, period_start: float, period_end: float,
                       currency: str = "usd") -> Invoice:
        """
        Create a draft invoice and populate lines from billing_outbox.
        Idempotent: UNIQUE(tenant_id, period_start, period_end) prevents duplicates.
        """
        now = time.time()
        invoice_id = f"inv_{uuid.uuid4().hex}"

        with sqlite3.connect(self.db_path) as conn:
            # Idempotency check
            existing = conn.execute(
                "SELECT id FROM invoices WHERE tenant_id = ? AND period_start = ? AND period_end = ?",
                (tenant_id, period_start, period_end)
            ).fetchone()
            if existing:
                return self.get_invoice(existing[0])

            conn.execute('''
                INSERT INTO invoices (id, tenant_id, period_start, period_end, currency, status, created_at)
                VALUES (?, ?, ?, ?, ?, 'draft', ?)
            ''', (invoice_id, tenant_id, period_start, period_end, currency, now))

        # Populate lines from billing_outbox
        lines = self._populate_lines(invoice_id, tenant_id, period_start, period_end)

        # Calculate total
        total = sum(l.amount_micro for l in lines)
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                "UPDATE invoices SET total_amount_micro = ? WHERE id = ?",
                (total, invoice_id)
            )

        return Invoice(
            id=invoice_id, tenant_id=tenant_id,
            period_start=period_start, period_end=period_end,
            total_amount_micro=total, currency=currency,
            status="draft", created_at=now, finalized_at=None,
            lines=lines
        )

    def _populate_lines(self, invoice_id: str, tenant_id: str,
                        period_start: float, period_end: float) -> List[InvoiceLine]:
        """Pull processed outbox entries into invoice lines."""
        lines = []
        with sqlite3.connect(self.metering_db_path) as m_conn:
            m_conn.row_factory = sqlite3.Row
            rows = m_conn.execute('''
                SELECT metric_type, window_start, amount_micro, pricing_version, pricing_model
                FROM billing_outbox
                WHERE tenant_id = ? AND created_at >= ? AND created_at < ? AND status = 'processed'
                ORDER BY window_start ASC
            ''', (tenant_id, period_start, period_end)).fetchall()

        # Also need quantities from metering_events for each window
        with sqlite3.connect(self.metering_db_path) as m_conn:
            for row in rows:
                metric = row['metric_type']
                ws = row['window_start']
                agg_key = f"{tenant_id}:{metric}:{ws}"

                qty_row = m_conn.execute(
                    "SELECT COALESCE(SUM(quantity), 0) FROM metering_events WHERE aggregation_key = ?",
                    (agg_key,)
                ).fetchone()
                quantity = qty_row[0]

                line = InvoiceLine(
                    id=f"il_{uuid.uuid4().hex}",
                    invoice_id=invoice_id,
                    metric_type=metric,
                    quantity=quantity,
                    amount_micro=row['amount_micro'],
                    pricing_version=row['pricing_version'],
                    pricing_model=row['pricing_model'],
                    window_start=ws,
                    window_end=ws + 3600,  # Default 1h window for display
                )
                lines.append(line)

        # Persist lines
        with sqlite3.connect(self.db_path) as conn:
            for l in lines:
                conn.execute('''
                    INSERT INTO invoice_lines 
                    (id, invoice_id, metric_type, quantity, amount_micro, pricing_version, pricing_model, window_start, window_end)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ''', (l.id, l.invoice_id, l.metric_type, l.quantity, l.amount_micro,
                      l.pricing_version, l.pricing_model, l.window_start, l.window_end))

        return lines

    def finalize_invoice(self, invoice_id: str) -> bool:
        """
        Finalize an invoice. After this, it is IMMUTABLE.
        Returns False if already finalized or not found.
        """
        now = time.time()
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.execute(
                "UPDATE invoices SET status = 'finalized', finalized_at = ? WHERE id = ? AND status = 'draft'",
                (now, invoice_id)
            )
            return cursor.rowcount > 0

    def mark_paid(self, invoice_id: str) -> bool:
        """Mark a finalized invoice as paid."""
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.execute(
                "UPDATE invoices SET status = 'paid' WHERE id = ? AND status = 'finalized'",
                (invoice_id,)
            )
            return cursor.rowcount > 0

    def create_credit_note(self, invoice_id: str, amount_micro: int, reason: str) -> str:
        """
        Issue a credit note against a finalized invoice.
        Never mutates the original invoice.
        """
        with sqlite3.connect(self.db_path) as conn:
            inv = conn.execute(
                "SELECT status, tenant_id FROM invoices WHERE id = ?", (invoice_id,)
            ).fetchone()
            if not inv:
                raise ValueError(f"Invoice {invoice_id} not found")
            if inv[0] == 'draft':
                raise ValueError("Cannot credit a draft invoice — edit it instead")

            cn_id = f"cn_{uuid.uuid4().hex}"
            conn.execute('''
                INSERT INTO credit_notes (id, invoice_id, tenant_id, amount_micro, reason, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
            ''', (cn_id, invoice_id, inv[1], amount_micro, reason, time.time()))
            return cn_id

    def get_invoice(self, invoice_id: str) -> Optional[Invoice]:
        """Retrieve a full invoice with lines."""
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute("SELECT * FROM invoices WHERE id = ?", (invoice_id,)).fetchone()
            if not row:
                return None

            line_rows = conn.execute(
                "SELECT * FROM invoice_lines WHERE invoice_id = ? ORDER BY window_start ASC",
                (invoice_id,)
            ).fetchall()

            lines = [
                InvoiceLine(
                    id=lr['id'], invoice_id=lr['invoice_id'],
                    metric_type=lr['metric_type'], quantity=lr['quantity'],
                    amount_micro=lr['amount_micro'], pricing_version=lr['pricing_version'],
                    pricing_model=lr['pricing_model'],
                    window_start=lr['window_start'], window_end=lr['window_end']
                )
                for lr in line_rows
            ]

            return Invoice(
                id=row['id'], tenant_id=row['tenant_id'],
                period_start=row['period_start'], period_end=row['period_end'],
                total_amount_micro=row['total_amount_micro'], currency=row['currency'],
                status=row['status'], created_at=row['created_at'],
                finalized_at=row['finalized_at'], lines=lines
            )

    def export_csv(self, invoice_id: str) -> str:
        """Export invoice as CSV string."""
        inv = self.get_invoice(invoice_id)
        if not inv:
            raise ValueError(f"Invoice {invoice_id} not found")

        output = io.StringIO()
        writer = csv.writer(output)

        # Header
        writer.writerow(["Invoice ID", inv.id])
        writer.writerow(["Tenant", inv.tenant_id])
        writer.writerow(["Period", f"{inv.period_start} - {inv.period_end}"])
        writer.writerow(["Status", inv.status])
        writer.writerow(["Currency", inv.currency])
        writer.writerow([])
        writer.writerow(["Metric", "Quantity", "Amount (micro)", "Pricing Version", "Model", "Window Start"])

        for line in inv.lines:
            writer.writerow([
                line.metric_type, line.quantity, line.amount_micro,
                line.pricing_version, line.pricing_model, line.window_start
            ])

        writer.writerow([])
        writer.writerow(["TOTAL", "", inv.total_amount_micro])

        return output.getvalue()

    def list_invoices(self, tenant_id: str, status: str = None) -> List[Dict]:
        """List invoices for a tenant, optionally filtered by status."""
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            query = "SELECT * FROM invoices WHERE tenant_id = ?"
            params = [tenant_id]
            if status:
                query += " AND status = ?"
                params.append(status)
            query += " ORDER BY period_start DESC"
            rows = conn.execute(query, tuple(params)).fetchall()
            return [dict(r) for r in rows]
