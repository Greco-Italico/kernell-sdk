import sqlite3
import time
import uuid
import os
import logging
import threading
from typing import Optional, Tuple
from dataclasses import dataclass

logger = logging.getLogger(__name__)


@dataclass
class SpendDecision:
    allowed: bool
    balance_after: int
    reason: str


class SpendGuard:
    """
    Real-time spend enforcement with shadow balance.
    
    Architecture:
    - Shadow balance lives in SQLite (swappable to Redis for prod scale).
    - Atomic DECRBY-style check: balance >= cost → deduct → allow, else deny.
    - Periodic reconciliation resets shadow from ledger truth.
    - Soft limits emit warnings; hard limits block requests.
    """

    def __init__(self, db_path: str = "/var/lib/kernell/spend_guard.sqlite3", ledger=None):
        self.db_path = db_path
        self.ledger = ledger
        self._lock = threading.Lock()  # Process-level fallback for SQLite
        self._ensure_db()

    def _ensure_db(self):
        if self.db_path != ":memory:":
            os.makedirs(os.path.dirname(self.db_path) or ".", exist_ok=True)
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("PRAGMA journal_mode=WAL;")
            conn.execute('''
                CREATE TABLE IF NOT EXISTS tenant_budgets (
                    tenant_id TEXT PRIMARY KEY,
                    balance_micro BIGINT NOT NULL DEFAULT 0,
                    hard_limit_micro BIGINT NOT NULL DEFAULT 0,
                    soft_limit_micro BIGINT NOT NULL DEFAULT 0,
                    last_reconciled_at REAL NOT NULL DEFAULT 0,
                    updated_at REAL NOT NULL,
                    status TEXT NOT NULL DEFAULT 'active'
                )
            ''')
            # Handle migration for existing DB
            try:
                conn.execute("ALTER TABLE tenant_budgets ADD COLUMN status TEXT NOT NULL DEFAULT 'active'")
            except sqlite3.OperationalError:
                pass  # Column likely already exists
                
            conn.execute('''
                CREATE TABLE IF NOT EXISTS spend_events (
                    id TEXT PRIMARY KEY,
                    tenant_id TEXT NOT NULL,
                    amount_micro BIGINT NOT NULL,
                    event_type TEXT NOT NULL,
                    created_at REAL NOT NULL
                )
            ''')
            conn.execute('''
                CREATE TABLE IF NOT EXISTS tenant_spend_window (
                    tenant_id TEXT NOT NULL,
                    window_start BIGINT NOT NULL,
                    window_size_sec INT NOT NULL,
                    spend_micro BIGINT NOT NULL,
                    PRIMARY KEY (tenant_id, window_start, window_size_sec)
                )
            ''')
            conn.execute('''
                CREATE TABLE IF NOT EXISTS tenant_rate_limit (
                    tenant_id TEXT NOT NULL,
                    window_start BIGINT NOT NULL,
                    request_count INT NOT NULL,
                    PRIMARY KEY (tenant_id, window_start)
                )
            ''')
            conn.execute('''
                CREATE TABLE IF NOT EXISTS system_control (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL,
                    updated_at REAL NOT NULL
                )
            ''')
            conn.execute('''
                INSERT INTO system_control (key, value, updated_at) 
                VALUES ('kill_switch', 'off', 0)
                ON CONFLICT(key) DO NOTHING
            ''')
            conn.execute('''
                CREATE TABLE IF NOT EXISTS spend_holds (
                    id TEXT PRIMARY KEY,
                    tenant_id TEXT NOT NULL,
                    reserved_micro BIGINT NOT NULL,
                    consumed_micro BIGINT NOT NULL DEFAULT 0,
                    status TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    expires_at REAL NOT NULL
                )
            ''')
            # Handle migration for existing DB
            try:
                conn.execute("ALTER TABLE spend_holds ADD COLUMN expires_at REAL NOT NULL DEFAULT 0")
            except sqlite3.OperationalError:
                pass

            conn.execute('''
                CREATE TABLE IF NOT EXISTS spend_guard_logs (
                    id TEXT PRIMARY KEY,
                    tenant_id TEXT NOT NULL,
                    allowed BOOLEAN NOT NULL,
                    reason TEXT NOT NULL,
                    latency_ms REAL NOT NULL,
                    created_at REAL NOT NULL
                )
            ''')

    def provision_tenant(self, tenant_id: str, initial_balance_micro: int = 0,
                         hard_limit_micro: int = 0, soft_limit_micro: int = 0):
        """Onboard a tenant with initial balance and limits."""
        now = time.time()
        with sqlite3.connect(self.db_path) as conn:
            conn.execute('''
                INSERT INTO tenant_budgets 
                (tenant_id, balance_micro, hard_limit_micro, soft_limit_micro, updated_at, status)
                VALUES (?, ?, ?, ?, ?, 'active')
                ON CONFLICT(tenant_id) DO UPDATE SET
                    hard_limit_micro = EXCLUDED.hard_limit_micro,
                    soft_limit_micro = EXCLUDED.soft_limit_micro,
                    updated_at = EXCLUDED.updated_at,
                    status = 'active'
            ''', (tenant_id, initial_balance_micro, hard_limit_micro, soft_limit_micro, now))

    def suspend_tenant(self, tenant_id: str):
        """Suspend execution capability for a tenant (e.g. for dunning/past_due)."""
        now = time.time()
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("UPDATE tenant_budgets SET status = 'suspended', updated_at = ? WHERE tenant_id = ?", (now, tenant_id))
            
    def unsuspend_tenant(self, tenant_id: str):
        """Restore execution capability for a tenant."""
        now = time.time()
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("UPDATE tenant_budgets SET status = 'active', updated_at = ? WHERE tenant_id = ?", (now, tenant_id))

    def top_up(self, tenant_id: str, amount_micro: int) -> int:
        """Add credits to a tenant's shadow balance. Returns new balance."""
        if amount_micro <= 0:
            raise ValueError("Top-up amount must be positive")
        now = time.time()
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                conn.execute('''
                    UPDATE tenant_budgets 
                    SET balance_micro = balance_micro + ?, updated_at = ?
                    WHERE tenant_id = ?
                ''', (amount_micro, now, tenant_id))
                
                conn.execute('''
                    INSERT INTO spend_events (id, tenant_id, amount_micro, event_type, created_at)
                    VALUES (?, ?, ?, 'top_up', ?)
                ''', (uuid.uuid4().hex, tenant_id, amount_micro, now))
                
                row = conn.execute(
                    "SELECT balance_micro FROM tenant_budgets WHERE tenant_id = ?", 
                    (tenant_id,)
                ).fetchone()
                conn.execute("COMMIT")
                return row[0]
            except Exception:
                conn.execute("ROLLBACK")
                raise

    def check_and_deduct(self, tenant_id: str, estimated_cost_micro: int) -> SpendDecision:
        start = time.time()
        decision = SpendDecision(allowed=False, balance_after=0, reason="error")
        try:
            decision = self._check_and_deduct_internal(tenant_id, estimated_cost_micro)
            return decision
        finally:
            latency_ms = (time.time() - start) * 1000
            from core.db import execute, IS_POSTGRES
            sql = "INSERT INTO spend_guard_logs (id, tenant_id, allowed, reason, latency_ms, created_at) VALUES "
            sql += "(%s, %s, %s, %s, %s, %s)" if IS_POSTGRES else "(?, ?, ?, ?, ?, ?)"
            try:
                execute(sql, (uuid.uuid4().hex, tenant_id, decision.allowed, decision.reason, latency_ms, time.time()), sqlite_fallback_path=self.db_path)
            except Exception as e:
                logger.error(f"Failed to write spend_guard_log: {e}")

    def _check_and_deduct_internal(self, tenant_id: str, estimated_cost_micro: int) -> SpendDecision:
        """
        Atomic check-and-deduct. This is the critical path.
        
        Uses Postgres FOR UPDATE lock to prevent double spend across concurrent workers,
        or SQLite BEGIN IMMEDIATE as fallback.
        """
        if estimated_cost_micro <= 0:
            return SpendDecision(allowed=True, balance_after=0, reason="zero_cost")

        now = time.time()
        now_int = int(now)
        from core.db import get_db_conn, IS_POSTGRES, query_one
        
        # -1. GLOBAL KILL SWITCH
        kill_switch_row = query_one("SELECT value FROM system_control WHERE key = 'kill_switch'")
        kill_switch_val = kill_switch_row[0] if isinstance(kill_switch_row, tuple) else (kill_switch_row["value"] if kill_switch_row else "off")
        if kill_switch_val == "on":
            return SpendDecision(allowed=False, balance_after=0, reason="system_kill_switch_active")
            
        with get_db_conn(self.db_path) as conn:
            with conn.cursor() as cur:
                try:
                    if not IS_POSTGRES:
                        cur.execute("BEGIN IMMEDIATE")
                        
                    # 1. Lock the tenant record
                    select_sql = "SELECT balance_micro, hard_limit_micro, soft_limit_micro, status FROM tenant_budgets WHERE tenant_id = "
                    select_sql += "%s FOR UPDATE" if IS_POSTGRES else "? LIMIT 1"
                    
                    cur.execute(select_sql, (tenant_id,))
                    row = cur.fetchone()

                    if not row:
                        conn.rollback()
                        return SpendDecision(allowed=False, balance_after=0, reason="tenant_not_found")

                    # Handle dict-like vs tuple rows depending on Postgres/SQLite drivers
                    balance = row['balance_micro'] if isinstance(row, dict) else row[0]
                    hard_limit = row['hard_limit_micro'] if isinstance(row, dict) else row[1]
                    soft_limit = row['soft_limit_micro'] if isinstance(row, dict) else row[2]
                    status = row['status'] if isinstance(row, dict) else row[3]
                    
                    if status == 'suspended':
                        conn.rollback()
                        return SpendDecision(allowed=False, balance_after=balance, reason="tenant_suspended")

                    new_balance = balance - estimated_cost_micro

                    # Hard limit check
                    if new_balance < hard_limit:
                        conn.rollback()
                        return SpendDecision(
                            allowed=False, 
                            balance_after=balance, 
                            reason=f"hard_limit_exceeded: balance {balance} - cost {estimated_cost_micro} = {new_balance} < hard_limit {hard_limit}"
                        )

                    # 2. Budget Window Check INSIDE the lock
                    WINDOWS = [
                        (10, 5_000_000),    # $5 in 10s
                        (60, 20_000_000),   # $20 in 60s
                    ]
                    window_updates = []
                    
                    for window_size, limit in WINDOWS:
                        window_start = now_int - (now_int % window_size)
                        
                        w_sql = "SELECT spend_micro FROM tenant_spend_window WHERE tenant_id = "
                        w_sql += "%s AND window_start = %s AND window_size_sec = %s" if IS_POSTGRES else "? AND window_start = ? AND window_size_sec = ?"
                        
                        cur.execute(w_sql, (tenant_id, window_start, window_size))
                        w_row = cur.fetchone()
                        
                        current = w_row['spend_micro'] if isinstance(w_row, dict) else (w_row[0] if w_row else 0)
                        projected = current + estimated_cost_micro
                        
                        if projected > limit:
                            conn.rollback()
                            if window_size == 10:
                                self.suspend_tenant(tenant_id)
                            return SpendDecision(allowed=False, balance_after=balance, reason=f"rate_limit_window_exceeded_{window_size}s")
                            
                        window_updates.append((window_start, window_size, estimated_cost_micro))

                    # 3. Apply all updates
                    for w_start, w_size, w_cost in window_updates:
                        upd_sql = """
                            INSERT INTO tenant_spend_window (tenant_id, window_start, window_size_sec, spend_micro)
                            VALUES """
                        upd_sql += "(%s, %s, %s, %s) ON CONFLICT (tenant_id, window_start, window_size_sec) DO UPDATE SET spend_micro = tenant_spend_window.spend_micro + EXCLUDED.spend_micro" if IS_POSTGRES else "(?, ?, ?, ?) ON CONFLICT (tenant_id, window_start, window_size_sec) DO UPDATE SET spend_micro = tenant_spend_window.spend_micro + EXCLUDED.spend_micro"
                        
                        cur.execute(upd_sql, (tenant_id, w_start, w_size, w_cost))

                    # Deduct atomically
                    update_sql = "UPDATE tenant_budgets SET balance_micro = balance_micro - "
                    update_sql += "%s, updated_at = %s WHERE tenant_id = %s" if IS_POSTGRES else "?, updated_at = ? WHERE tenant_id = ?"
                    cur.execute(update_sql, (estimated_cost_micro, now, tenant_id))

                    # Log the spend event
                    insert_sql = "INSERT INTO spend_events (id, tenant_id, amount_micro, event_type, created_at) VALUES "
                    insert_sql += "(%s, %s, %s, 'spend', %s)" if IS_POSTGRES else "(?, ?, ?, 'spend', ?)"
                    cur.execute(insert_sql, (uuid.uuid4().hex, tenant_id, estimated_cost_micro, now))

                    conn.commit()

                    # Soft limit warning (non-blocking)
                    if new_balance < soft_limit:
                        logger.warning(f"Tenant {tenant_id} approaching limit: balance={new_balance}, soft_limit={soft_limit}")

                    return SpendDecision(allowed=True, balance_after=new_balance, reason="ok")

                except Exception:
                    conn.rollback()
                    raise

    def get_balance(self, tenant_id: str) -> Optional[int]:
        """Read current shadow balance."""
        with sqlite3.connect(self.db_path) as conn:
            row = conn.execute(
                "SELECT balance_micro FROM tenant_budgets WHERE tenant_id = ?",
                (tenant_id,)
            ).fetchone()
            return row[0] if row else None

    def reconcile_from_ledger(self, tenant_id: str) -> int:
        """
        Reconciliation job: recompute shadow balance from ledger truth.
        
        This corrects any drift caused by crashes, missed events, or 
        shadow balance desync. Should run periodically (e.g., every 5 min).
        
        Formula: 
            shadow_balance = sum(top_ups from ledger) - sum(billing from ledger)
        """
        if not self.ledger:
            raise ValueError("Ledger required for reconciliation")

        # Get the actual ledger balance for the tenant's expense account
        wallet_account = f"tenant_{tenant_id}_expense"
        try:
            ledger_expense = self.ledger.get_account_balance(tenant_id, wallet_account)
        except ValueError:
            ledger_expense = 0

        # Get the credit balance (top-ups)
        credit_account = f"tenant_{tenant_id}_credits"
        try:
            ledger_credits = self.ledger.get_account_balance(tenant_id, credit_account)
        except ValueError:
            ledger_credits = 0

        # True balance = credits deposited - expenses charged
        true_balance = ledger_credits - ledger_expense

        now = time.time()
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                conn.execute('''
                    UPDATE tenant_budgets 
                    SET balance_micro = ?, last_reconciled_at = ?, updated_at = ?
                    WHERE tenant_id = ?
                ''', (true_balance, now, now, tenant_id))
                conn.execute("COMMIT")
            except Exception:
                conn.execute("ROLLBACK")
                raise

        logger.info(f"Reconciled tenant {tenant_id}: shadow_balance reset to {true_balance}")
        return true_balance
        
    def pre_authorize(self, tenant_id: str, estimated_cost_micro: int) -> Tuple[bool, Optional[str]]:
        """Phase 1/3: HOLD (Reserve money in shadow balance)"""
        now = time.time()
        decision = self.check_and_deduct(tenant_id, estimated_cost_micro)
        if not decision.allowed:
            return False, None
            
        hold_id = uuid.uuid4().hex
        expires_at = now + 300 # 5 minutes TTL
        from core.db import execute, IS_POSTGRES
        
        insert_sql = """
            INSERT INTO spend_holds (id, tenant_id, reserved_micro, consumed_micro, status, created_at, updated_at, expires_at)
            VALUES """
        insert_sql += "(%s, %s, %s, 0, 'active', %s, %s, %s)" if IS_POSTGRES else "(?, ?, ?, 0, 'active', ?, ?, ?)"
        execute(insert_sql, (hold_id, tenant_id, estimated_cost_micro, now, now, expires_at), sqlite_fallback_path=self.db_path)
        
        return True, hold_id

    def capture_usage(self, hold_id: str, amount_micro: int):
        """Phase 2/3: CAPTURE (incremental partial consumption)"""
        now = time.time()
        from core.db import get_db_conn, IS_POSTGRES
        
        with get_db_conn(self.db_path) as conn:
            with conn.cursor() as cur:
                try:
                    if not IS_POSTGRES:
                        cur.execute("BEGIN IMMEDIATE")
                        
                    sql = "SELECT tenant_id, reserved_micro, consumed_micro, status FROM spend_holds WHERE id = "
                    sql += "%s FOR UPDATE" if IS_POSTGRES else "? LIMIT 1"
                    
                    cur.execute(sql, (hold_id,))
                    row = cur.fetchone()
                    
                    if not row:
                        conn.rollback()
                        raise ValueError("hold_not_found")
                        
                    tenant_id = row['tenant_id'] if isinstance(row, dict) else row[0]
                    reserved = row['reserved_micro'] if isinstance(row, dict) else row[1]
                    consumed = row['consumed_micro'] if isinstance(row, dict) else row[2]
                    status = row['status'] if isinstance(row, dict) else row[3]
                    
                    if status != 'active':
                        conn.rollback()
                        return # idempotent exit
                        
                    new_consumed = consumed + amount_micro
                    if new_consumed > reserved:
                        conn.rollback()
                        raise ValueError("over_capture_detected")
                        
                    upd = "UPDATE spend_holds SET consumed_micro = "
                    upd += "%s, updated_at = %s WHERE id = %s" if IS_POSTGRES else "?, updated_at = ? WHERE id = ?"
                    cur.execute(upd, (new_consumed, now, hold_id))
                    
                    conn.commit()
                except Exception:
                    conn.rollback()
                    raise

    def finalize_hold(self, hold_id: str):
        """Phase 3/3: RELEASE (refund unconsumed funds to shadow balance)"""
        now = time.time()
        from core.db import get_db_conn, IS_POSTGRES
        
        with get_db_conn(self.db_path) as conn:
            with conn.cursor() as cur:
                try:
                    if not IS_POSTGRES:
                        cur.execute("BEGIN IMMEDIATE")
                        
                    sql = "SELECT tenant_id, reserved_micro, consumed_micro, status FROM spend_holds WHERE id = "
                    sql += "%s FOR UPDATE" if IS_POSTGRES else "? LIMIT 1"
                    
                    cur.execute(sql, (hold_id,))
                    row = cur.fetchone()
                    
                    if not row:
                        conn.rollback()
                        return
                        
                    tenant_id = row['tenant_id'] if isinstance(row, dict) else row[0]
                    reserved = row['reserved_micro'] if isinstance(row, dict) else row[1]
                    consumed = row['consumed_micro'] if isinstance(row, dict) else row[2]
                    status = row['status'] if isinstance(row, dict) else row[3]
                    
                    if status != 'active':
                        conn.rollback()
                        return
                        
                    refund = reserved - consumed
                    
                    upd_hold = "UPDATE spend_holds SET status = 'finalized', updated_at = "
                    upd_hold += "%s WHERE id = %s" if IS_POSTGRES else "? WHERE id = ?"
                    cur.execute(upd_hold, (now, hold_id))
                    
                    if refund > 0:
                        # Refund balance
                        upd_bal = "UPDATE tenant_budgets SET balance_micro = balance_micro + "
                        upd_bal += "%s, updated_at = %s WHERE tenant_id = %s" if IS_POSTGRES else "?, updated_at = ? WHERE tenant_id = ?"
                        cur.execute(upd_bal, (refund, now, tenant_id))
                        
                        # Log refund event
                        ins_ev = "INSERT INTO spend_events (id, tenant_id, amount_micro, event_type, created_at) VALUES "
                        ins_ev += "(%s, %s, %s, 'refund', %s)" if IS_POSTGRES else "(?, ?, ?, 'refund', ?)"
                        cur.execute(ins_ev, (uuid.uuid4().hex, tenant_id, refund, now))
                        
                    conn.commit()
                except Exception:
                    conn.rollback()
                    raise

    def gc_expired_holds(self, batch_size: int = 100):
        """Auto-release funds for workers that died before finalize_hold()"""
        now = time.time()
        from core.db import query_all, IS_POSTGRES
        
        sql = "SELECT id FROM spend_holds WHERE status = 'active' AND expires_at < "
        sql += "%s LIMIT %s" if IS_POSTGRES else "? LIMIT ?"
        
        rows = query_all(sql, (now, batch_size), sqlite_fallback_path=self.db_path)
        
        count = 0
        for row in rows:
            hold_id = row['id'] if isinstance(row, dict) else row[0]
            try:
                self.finalize_hold(hold_id)
                count += 1
            except Exception:
                logger.exception(f"GC failed for hold {hold_id}")
        return count

    def check_rate_limit(self, tenant_id: str, limit=100, window_sec=60) -> bool:
        """
        Infrastructure protection against API abuse.
        """
        now_int = int(time.time())
        window_start = now_int - (now_int % window_sec)
        from core.db import query_one, execute
        
        row = query_one(
            "SELECT request_count FROM tenant_rate_limit WHERE tenant_id = ? AND window_start = ?",
            (tenant_id, window_start)
        )
        count = row[0] if isinstance(row, tuple) else (row["request_count"] if row else 0)
        
        if count >= limit:
            return False
            
        execute(
            """
            INSERT INTO tenant_rate_limit (tenant_id, window_start, request_count)
            VALUES (?, ?, 1)
            ON CONFLICT (tenant_id, window_start)
            DO UPDATE SET request_count = tenant_rate_limit.request_count + 1
            """,
            (tenant_id, window_start)
        )
        return True
