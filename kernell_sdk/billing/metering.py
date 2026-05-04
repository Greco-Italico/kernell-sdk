import sqlite3
import time
import uuid
import json
import os
import logging
from typing import Optional, List, Dict

logger = logging.getLogger(__name__)

class MeteringEngine:
    """
    Production-grade Metering Engine with Exactly-Once semantics.
    """
    def __init__(self, db_path: str = "/var/lib/kernell/metering.sqlite3", ledger=None, pricing_engine=None, window_size: int = 60):
        self.db_path = db_path
        self.ledger = ledger
        self.pricing_engine = pricing_engine
        self.window_size = window_size
        self._ensure_db()

    def _ensure_db(self):
        if self.db_path != ":memory:":
            os.makedirs(os.path.dirname(self.db_path) or ".", exist_ok=True)
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("PRAGMA journal_mode=WAL;")
            # Table: metering_events (Write-Only, Immutable Log)
            conn.execute('''
                CREATE TABLE IF NOT EXISTS metering_events (
                    id TEXT PRIMARY KEY,
                    tenant_id TEXT NOT NULL,
                    event_id TEXT NOT NULL,
                    source TEXT NOT NULL,
                    metric_type TEXT NOT NULL,
                    quantity BIGINT NOT NULL,
                    timestamp REAL NOT NULL,
                    received_at REAL NOT NULL,
                    aggregation_key TEXT NOT NULL,
                    metadata TEXT,
                    UNIQUE(tenant_id, event_id)
                )
            ''')
            # Table: metering_aggregates (Rollup per window)
            conn.execute('''
                CREATE TABLE IF NOT EXISTS metering_aggregates (
                    tenant_id TEXT NOT NULL,
                    metric_type TEXT NOT NULL,
                    window_start REAL NOT NULL,
                    total_quantity BIGINT NOT NULL,
                    status TEXT NOT NULL,
                    updated_at REAL NOT NULL,
                    PRIMARY KEY (tenant_id, metric_type, window_start)
                )
            ''')
            # Table: billing_outbox (Eventual Consistency to Ledger)
            conn.execute('''
                CREATE TABLE IF NOT EXISTS billing_outbox (
                    id TEXT PRIMARY KEY,
                    tenant_id TEXT NOT NULL,
                    metric_type TEXT NOT NULL,
                    window_start REAL NOT NULL,
                    amount_micro BIGINT NOT NULL,
                    status TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    pricing_version INT,
                    pricing_model TEXT
                )
            ''')
            # Table: aggregation_locks (Distributed Locking for workers)
            conn.execute('''
                CREATE TABLE IF NOT EXISTS aggregation_locks (
                    aggregation_key TEXT PRIMARY KEY,
                    locked_by TEXT NOT NULL,
                    locked_at REAL NOT NULL,
                    expires_at REAL NOT NULL
                )
            ''')

    def get_window_start(self, ts: float) -> float:
        return ts - (ts % self.window_size)

    def ingest_event(self, tenant_id: str, event_id: str, source: str, metric_type: str, quantity: int, timestamp: float, metadata: dict = None) -> bool:
        """
        Ingest a metering event idempotently.
        """
        now = time.time()
        
        # 1. Reject future events or extremely old events (Time source inconsistency protection)
        if timestamp > now + 60:
            logger.warning(f"Rejected future event: {event_id} from {tenant_id}")
            raise ValueError("Event timestamp is in the future")
        if timestamp < now - 86400: # 1 day
            logger.warning(f"Event too old (sent to DLQ): {event_id} from {tenant_id}")
            raise ValueError("Event timestamp is too old (DLQ)")
            
        window_start = self.get_window_start(timestamp)
        aggregation_key = f"{tenant_id}:{metric_type}:{window_start}"
        
        meta_str = json.dumps(metadata) if metadata else None
        
        with sqlite3.connect(self.db_path) as conn:
            try:
                conn.execute('''
                    INSERT INTO metering_events 
                    (id, tenant_id, event_id, source, metric_type, quantity, timestamp, received_at, aggregation_key, metadata)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ''', (
                    uuid.uuid4().hex, tenant_id, event_id, source, metric_type, 
                    quantity, timestamp, now, aggregation_key, meta_str
                ))
                return True
            except sqlite3.IntegrityError:
                # Idempotency: UNIQUE(tenant_id, event_id) prevents double counting
                return False

    def acquire_lock(self, aggregation_key: str, worker_id: str, lock_ttl: int = 30) -> bool:
        """Distributed lock for processing an aggregation window."""
        now = time.time()
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                # Clean expired locks
                conn.execute("DELETE FROM aggregation_locks WHERE expires_at < ?", (now,))
                
                conn.execute('''
                    INSERT INTO aggregation_locks (aggregation_key, locked_by, locked_at, expires_at)
                    VALUES (?, ?, ?, ?)
                ''', (aggregation_key, worker_id, now, now + lock_ttl))
                conn.execute("COMMIT")
                return True
            except sqlite3.IntegrityError:
                conn.execute("ROLLBACK")
                return False

    def release_lock(self, aggregation_key: str, worker_id: str):
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("DELETE FROM aggregation_locks WHERE aggregation_key = ? AND locked_by = ?", (aggregation_key, worker_id))

    def aggregate_window(self, tenant_id: str, metric_type: str, window_start: float, worker_id: str) -> Optional[int]:
        """
        Idempotent aggregation of a specific window.
        Uses optimistic locking/upsert to ensure exactly-once semantics.
        """
        aggregation_key = f"{tenant_id}:{metric_type}:{window_start}"
        
        if not self.acquire_lock(aggregation_key, worker_id):
            return None # Another worker is processing this window
            
        try:
            now = time.time()
            with sqlite3.connect(self.db_path) as conn:
                # Step 1: Calculate deterministic total from immutable events
                row = conn.execute(
                    "SELECT COALESCE(SUM(quantity), 0) FROM metering_events WHERE aggregation_key = ?",
                    (aggregation_key,)
                ).fetchone()
                total_quantity = row[0]
                
                if total_quantity == 0:
                    return 0

                # Step 2: UPSERT into aggregates (cumulative and reproducible)
                conn.execute('''
                    INSERT INTO metering_aggregates 
                    (tenant_id, metric_type, window_start, total_quantity, status, updated_at)
                    VALUES (?, ?, ?, ?, 'pending', ?)
                    ON CONFLICT (tenant_id, metric_type, window_start) 
                    DO UPDATE SET 
                        total_quantity = EXCLUDED.total_quantity, 
                        updated_at = EXCLUDED.updated_at
                ''', (
                    tenant_id, metric_type, window_start, total_quantity, now
                ))
                return total_quantity
        finally:
            self.release_lock(aggregation_key, worker_id)

    def commit_aggregate_to_outbox(self, tenant_id: str, metric_type: str, window_start: float) -> bool:
        """
        Reads a 'pending' aggregate and commits it to the billing outbox, preventing double-billing.
        Uses PricingEngine to calculate the amount.
        """
        if not self.pricing_engine:
            raise ValueError("PricingEngine is required to commit aggregates to outbox")
            
        now = time.time()
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("BEGIN IMMEDIATE") # Atomic transition
            try:
                agg = conn.execute('''
                    SELECT total_quantity FROM metering_aggregates
                    WHERE tenant_id = ? AND metric_type = ? AND window_start = ? AND status = 'pending'
                ''', (tenant_id, metric_type, window_start)).fetchone()
                
                if not agg:
                    conn.execute("ROLLBACK")
                    return False
                    
                total_quantity = agg[0]
                
                # Optimistic Lock: Claim the aggregate
                cursor = conn.execute('''
                    UPDATE metering_aggregates SET status = 'committed', updated_at = ?
                    WHERE tenant_id = ? AND metric_type = ? AND window_start = ? AND status = 'pending'
                ''', (now, tenant_id, metric_type, window_start))
                
                if cursor.rowcount == 0:
                    conn.execute("ROLLBACK")
                    return False # Another worker claimed it
                    
                amount_micro, p_version, p_model = self.pricing_engine.calculate(
                    tenant_id=tenant_id, 
                    metric_type=metric_type, 
                    quantity=total_quantity, 
                    timestamp=window_start
                )
                
                # Outbox event generation
                conn.execute('''
                    INSERT INTO billing_outbox 
                    (id, tenant_id, metric_type, window_start, amount_micro, status, created_at, pricing_version, pricing_model)
                    VALUES (?, ?, ?, ?, ?, 'pending', ?, ?, ?)
                ''', (
                    uuid.uuid4().hex, tenant_id, metric_type, window_start, amount_micro, now, p_version, p_model
                ))
                
                conn.execute("COMMIT")
                return True
            except Exception as e:
                conn.execute("ROLLBACK")
                logger.error(f"Failed to commit aggregate to outbox: {e}")
                raise

    def process_billing_outbox(self):
        """
        Reads the outbox and impacts the ledger. Eventual consistency perfect pattern.
        """
        if not self.ledger:
            return
            
        with sqlite3.connect(self.db_path) as conn:
            entries = conn.execute('''
                SELECT id, tenant_id, metric_type, window_start, amount_micro 
                FROM billing_outbox WHERE status = 'pending'
            ''').fetchall()
            
            from core.audit.double_entry_ledger import JournalLine
            
            for entry in entries:
                outbox_id, tenant_id, metric_type, window_start, amount_micro = entry
                
                # Atomically claim the outbox entry (Race condition protection)
                cursor = conn.execute("UPDATE billing_outbox SET status = 'processing' WHERE id = ? AND status = 'pending'", (outbox_id,))
                if cursor.rowcount == 0:
                    continue # Another worker grabbed it
                
                reference_id = f"billing:{tenant_id}:{metric_type}:{window_start}"
                
                try:
                    # Debit expense, Credit revenue
                    wallet_account = f"tenant_{tenant_id}_expense"
                    revenue_account = "platform_revenue"
                    
                    # NOTE: En un entorno de altísima concurrencia, esto debería cachearse en memoria o pre-crearse 
                    # al hacer onboarding del tenant. Lo dejamos por safety fallback.
                    self.ledger.create_account(tenant_id, wallet_account, "liability")
                    self.ledger.create_account(tenant_id, revenue_account, "asset")
                    
                    # Idempotency is guaranteed by UNIQUE(tenant_id, reference_id) in Ledger
                    entry_id = self.ledger.create_journal_entry(
                        tenant_id=tenant_id,
                        reference_id=reference_id,
                        description=f"Metered usage billing for {metric_type}",
                        lines=[
                            JournalLine(wallet_account, "debit", amount_micro),
                            JournalLine(revenue_account, "credit", amount_micro)
                        ]
                    )
                    
                    if entry_id:
                        conn.execute("UPDATE billing_outbox SET status = 'processed' WHERE id = ?", (outbox_id,))
                    else:
                        # Entry existed (idempotency fallback). Safe to mark processed.
                        conn.execute("UPDATE billing_outbox SET status = 'processed' WHERE id = ?", (outbox_id,))
                        
                except Exception as e:
                    # Rollback claim so another worker/retry can pick it up
                    conn.execute("UPDATE billing_outbox SET status = 'pending' WHERE id = ?", (outbox_id,))
                    logger.error(f"Failed to process outbox entry {outbox_id}: {e}")
