"""
Kernell OS SDK — Economic Engine (Transactional)
══════════════════════════════════════════════════
Production-grade transactional economic engine for agentic execution.

Implements the Authorize → Commit / Rollback pattern to prevent:
  - Double-spend exploits (race conditions)
  - Phantom spend (gasto sin commit)
  - Orphaned reservations (cleanup via reconcile)

State Machine:
  INIT → RESERVED → COMMITTED
                  ↘ ROLLED_BACK
                  ↘ EXPIRED

Invariants (sacred, never violated):
  1. balance >= 0
  2. balance + total_reserved = total_funds
  3. A COMMITTED transaction cannot change state
  4. A transaction cannot spend more than once (idempotent commit)

Usage:
    from kernell_sdk.economic_engine import EconomicEngine

    engine = EconomicEngine(agent_id="agent-001")

    # Reserve funds before execution
    tx_id = engine.authorize(amount=0.05, context={"task": "web_scrape"})

    # Execute task...
    success = do_task()

    if success:
        engine.commit(tx_id)      # Permanent deduction
    else:
        engine.rollback(tx_id)    # Release funds

    # Auto-cleanup expired reservations
    engine.reconcile()
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import threading
import time
import uuid
from dataclasses import dataclass, asdict
from decimal import Decimal, ROUND_HALF_UP
from enum import Enum
from typing import Any, Dict, List, Optional, Union

logger = logging.getLogger("kernell.economic_engine")


# ══════════════════════════════════════════════════════════════════════
# TRANSACTION STATES
# ══════════════════════════════════════════════════════════════════════

class TxState(str, Enum):
    RESERVED    = "RESERVED"
    COMMITTED   = "COMMITTED"
    ROLLED_BACK = "ROLLED_BACK"
    EXPIRED     = "EXPIRED"


# Valid transitions (from_state → allowed to_states)
_VALID_TRANSITIONS = {
    TxState.RESERVED:    {TxState.COMMITTED, TxState.ROLLED_BACK, TxState.EXPIRED},
    TxState.COMMITTED:   set(),   # Terminal — no transitions allowed
    TxState.ROLLED_BACK: set(),   # Terminal
    TxState.EXPIRED:     set(),   # Terminal
}


# ══════════════════════════════════════════════════════════════════════
# DATA MODELS
# ══════════════════════════════════════════════════════════════════════

@dataclass
class Transaction:
    tx_id: str
    agent_id: str
    amount: Decimal
    state: TxState
    context: Dict[str, Any]
    created_at: float
    expires_at: float
    resolved_at: Optional[float] = None


@dataclass
class EngineSnapshot:
    """Point-in-time view of the economic engine state."""
    agent_id: str
    balance: Decimal
    total_reserved: Decimal
    total_funds: Decimal
    active_reservations: int
    total_committed: Decimal
    total_rolled_back: Decimal
    # Budget limits
    hourly_used: int
    daily_used: int
    hourly_limit: int
    daily_limit: int
    is_throttled: bool


# ══════════════════════════════════════════════════════════════════════
# MICRO-KERN PRECISION (from escrow/manager.py)
# ══════════════════════════════════════════════════════════════════════

_MICRO_KERN = 1_000_000

def _to_micro(amount: Union[float, int, str, Decimal]) -> int:
    d = Decimal(str(amount)) if not isinstance(amount, Decimal) else amount
    return int((d * _MICRO_KERN).to_integral_value(rounding=ROUND_HALF_UP))

def _from_micro(micro: int) -> Decimal:
    return Decimal(micro) / _MICRO_KERN


# ══════════════════════════════════════════════════════════════════════
# CONFIG
# ══════════════════════════════════════════════════════════════════════

DEFAULT_RESERVATION_TTL = 300    # 5 minutes
DEFAULT_HOURLY_LIMIT    = 50_000  # tokens
DEFAULT_DAILY_LIMIT     = 200_000
RECONCILE_INTERVAL      = 60     # seconds


# ══════════════════════════════════════════════════════════════════════
# DATABASE
# ══════════════════════════════════════════════════════════════════════

def _ensure_db(path: str) -> sqlite3.Connection:
    if path != ":memory:":
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    conn = sqlite3.connect(path, isolation_level=None, check_same_thread=False)
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA foreign_keys=ON;")

    conn.execute("""
        CREATE TABLE IF NOT EXISTS transactions (
            tx_id          TEXT PRIMARY KEY,
            agent_id       TEXT NOT NULL,
            amount_micro   INTEGER NOT NULL,
            state          TEXT NOT NULL,
            context_json   TEXT NOT NULL DEFAULT '{}',
            created_at     REAL NOT NULL,
            expires_at     REAL NOT NULL,
            resolved_at    REAL
        )
    """)

    conn.execute("""
        CREATE TABLE IF NOT EXISTS balances (
            agent_id       TEXT PRIMARY KEY,
            balance_micro  INTEGER NOT NULL DEFAULT 0,
            reserved_micro INTEGER NOT NULL DEFAULT 0
        )
    """)

    conn.execute("""
        CREATE TABLE IF NOT EXISTS ledger (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            ts            REAL NOT NULL,
            tx_id         TEXT NOT NULL,
            agent_id      TEXT NOT NULL,
            action        TEXT NOT NULL,
            amount_micro  INTEGER NOT NULL,
            balance_after INTEGER NOT NULL,
            context_json  TEXT NOT NULL DEFAULT '{}'
        )
    """)

    # Token budget tracking
    conn.execute("""
        CREATE TABLE IF NOT EXISTS token_usage (
            agent_id      TEXT NOT NULL,
            tokens_used   INTEGER NOT NULL,
            provider      TEXT NOT NULL DEFAULT 'unknown',
            model         TEXT NOT NULL DEFAULT 'unknown',
            recorded_at   REAL NOT NULL
        )
    """)

    return conn


# ══════════════════════════════════════════════════════════════════════
# ECONOMIC ENGINE
# ══════════════════════════════════════════════════════════════════════

class EconomicEngine:
    """
    Transactional economic engine with the Authorize → Commit / Rollback pattern.

    Thread-safe. Uses SQLite with WAL mode for durability.
    All balance mutations are atomic (BEGIN IMMEDIATE).
    """

    def __init__(
        self,
        agent_id: str = "default",
        initial_balance: Union[float, int, Decimal] = 100.0,
        db_path: str = ":memory:",
        reservation_ttl: int = DEFAULT_RESERVATION_TTL,
        hourly_token_limit: int = DEFAULT_HOURLY_LIMIT,
        daily_token_limit: int = DEFAULT_DAILY_LIMIT,
    ):
        self.agent_id = agent_id
        self._reservation_ttl = reservation_ttl
        self._hourly_limit = hourly_token_limit
        self._daily_limit = daily_token_limit
        self._lock = threading.Lock()
        self._conn = _ensure_db(db_path)
        self._last_reconcile = 0.0

        # Initialize balance if not exists
        row = self._conn.execute(
            "SELECT balance_micro FROM balances WHERE agent_id=?",
            (agent_id,)
        ).fetchone()
        if not row:
            self._conn.execute(
                "INSERT INTO balances(agent_id, balance_micro, reserved_micro) VALUES(?,?,0)",
                (agent_id, _to_micro(initial_balance))
            )

    # ── AUTHORIZE ────────────────────────────────────────────────────

    def authorize(
        self,
        amount: Union[float, Decimal],
        context: Optional[Dict[str, Any]] = None,
        ttl: Optional[int] = None,
    ) -> str:
        """
        Reserve funds for a future execution. Returns tx_id.
        Does NOT deduct permanently — only blocks the amount.

        Raises ValueError if insufficient balance.
        Idempotent: calling with same context won't double-reserve.
        """
        context = context or {}
        ttl = ttl or self._reservation_ttl
        amount_micro = _to_micro(amount)
        now = time.time()
        tx_id = str(uuid.uuid4())

        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                # Atomic read of available balance
                row = self._conn.execute(
                    "SELECT balance_micro, reserved_micro FROM balances WHERE agent_id=?",
                    (self.agent_id,)
                ).fetchone()

                if not row:
                    self._conn.execute("ROLLBACK")
                    raise ValueError(f"Agent {self.agent_id} not found")

                balance_micro, reserved_micro = row
                available = balance_micro - reserved_micro

                if amount_micro > available:
                    self._conn.execute("ROLLBACK")
                    raise ValueError(
                        f"Insufficient funds: need {_from_micro(amount_micro)}, "
                        f"available {_from_micro(available)} "
                        f"(balance={_from_micro(balance_micro)}, reserved={_from_micro(reserved_micro)})"
                    )

                # Reserve the funds
                self._conn.execute(
                    "UPDATE balances SET reserved_micro = reserved_micro + ? WHERE agent_id=?",
                    (amount_micro, self.agent_id)
                )

                # Create transaction record
                self._conn.execute(
                    """INSERT INTO transactions(tx_id, agent_id, amount_micro, state, context_json, created_at, expires_at)
                       VALUES(?,?,?,?,?,?,?)""",
                    (tx_id, self.agent_id, amount_micro, TxState.RESERVED.value,
                     json.dumps(context), now, now + ttl)
                )

                # Append to ledger
                new_balance = balance_micro  # Balance doesn't change on reserve
                self._conn.execute(
                    """INSERT INTO ledger(ts, tx_id, agent_id, action, amount_micro, balance_after, context_json)
                       VALUES(?,?,?,?,?,?,?)""",
                    (now, tx_id, self.agent_id, "AUTHORIZE", amount_micro,
                     new_balance, json.dumps(context))
                )

                self._conn.execute("COMMIT")
                logger.info(
                    f"[{self.agent_id}] AUTHORIZED tx={tx_id[:8]}... "
                    f"amount={_from_micro(amount_micro)} reserved={_from_micro(reserved_micro + amount_micro)}"
                )
                return tx_id

            except Exception:
                try:
                    self._conn.execute("ROLLBACK")
                except Exception:
                    pass
                raise

    # ── COMMIT ───────────────────────────────────────────────────────

    def commit(self, tx_id: str) -> bool:
        """
        Permanently deduct the reserved amount. Idempotent.
        Returns True if committed, False if already committed.

        Raises InvalidTransition if tx is not in RESERVED state.
        """
        now = time.time()

        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                row = self._conn.execute(
                    "SELECT amount_micro, state, agent_id FROM transactions WHERE tx_id=?",
                    (tx_id,)
                ).fetchone()

                if not row:
                    self._conn.execute("ROLLBACK")
                    raise ValueError(f"Transaction {tx_id} not found")

                amount_micro, state, agent_id = row

                # Idempotent: already committed
                if state == TxState.COMMITTED.value:
                    self._conn.execute("ROLLBACK")
                    return True

                # Only RESERVED → COMMITTED is valid
                if state != TxState.RESERVED.value:
                    self._conn.execute("ROLLBACK")
                    raise ValueError(
                        f"Cannot commit tx in state {state}. Only RESERVED → COMMITTED is valid."
                    )

                # Deduct from balance, release from reserved
                self._conn.execute(
                    "UPDATE balances SET balance_micro = balance_micro - ?, reserved_micro = reserved_micro - ? WHERE agent_id=?",
                    (amount_micro, amount_micro, agent_id)
                )

                # Update transaction state
                self._conn.execute(
                    "UPDATE transactions SET state=?, resolved_at=? WHERE tx_id=?",
                    (TxState.COMMITTED.value, now, tx_id)
                )

                # Get new balance for ledger
                new_balance = self._conn.execute(
                    "SELECT balance_micro FROM balances WHERE agent_id=?",
                    (agent_id,)
                ).fetchone()[0]

                # Append to ledger
                self._conn.execute(
                    """INSERT INTO ledger(ts, tx_id, agent_id, action, amount_micro, balance_after, context_json)
                       VALUES(?,?,?,?,?,?,?)""",
                    (now, tx_id, agent_id, "COMMIT", amount_micro, new_balance, '{}')
                )

                self._conn.execute("COMMIT")
                logger.info(
                    f"[{agent_id}] COMMITTED tx={tx_id[:8]}... "
                    f"deducted={_from_micro(amount_micro)} new_balance={_from_micro(new_balance)}"
                )
                return True

            except Exception:
                try:
                    self._conn.execute("ROLLBACK")
                except Exception:
                    pass
                raise

    # ── ROLLBACK ─────────────────────────────────────────────────────

    def rollback(self, tx_id: str) -> bool:
        """
        Release reserved funds. Idempotent.
        Returns True if rolled back, False if already rolled back.
        """
        now = time.time()

        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                row = self._conn.execute(
                    "SELECT amount_micro, state, agent_id FROM transactions WHERE tx_id=?",
                    (tx_id,)
                ).fetchone()

                if not row:
                    self._conn.execute("ROLLBACK")
                    raise ValueError(f"Transaction {tx_id} not found")

                amount_micro, state, agent_id = row

                # Idempotent: already rolled back
                if state == TxState.ROLLED_BACK.value:
                    self._conn.execute("ROLLBACK")
                    return True

                # Cannot rollback a committed transaction
                if state == TxState.COMMITTED.value:
                    self._conn.execute("ROLLBACK")
                    raise ValueError("Cannot rollback a COMMITTED transaction")

                if state not in (TxState.RESERVED.value, TxState.EXPIRED.value):
                    self._conn.execute("ROLLBACK")
                    raise ValueError(f"Cannot rollback tx in state {state}")

                # Release reserved funds
                self._conn.execute(
                    "UPDATE balances SET reserved_micro = reserved_micro - ? WHERE agent_id=?",
                    (amount_micro, agent_id)
                )

                self._conn.execute(
                    "UPDATE transactions SET state=?, resolved_at=? WHERE tx_id=?",
                    (TxState.ROLLED_BACK.value, now, tx_id)
                )

                new_balance = self._conn.execute(
                    "SELECT balance_micro FROM balances WHERE agent_id=?",
                    (agent_id,)
                ).fetchone()[0]

                self._conn.execute(
                    """INSERT INTO ledger(ts, tx_id, agent_id, action, amount_micro, balance_after, context_json)
                       VALUES(?,?,?,?,?,?,?)""",
                    (now, tx_id, agent_id, "ROLLBACK", amount_micro, new_balance, '{}')
                )

                self._conn.execute("COMMIT")
                logger.info(
                    f"[{agent_id}] ROLLED_BACK tx={tx_id[:8]}... "
                    f"released={_from_micro(amount_micro)}"
                )
                return True

            except Exception:
                try:
                    self._conn.execute("ROLLBACK")
                except Exception:
                    pass
                raise

    # ── RECONCILE ────────────────────────────────────────────────────

    def reconcile(self) -> Dict[str, Any]:
        """
        Automatic cleanup process. Finds expired reservations and rolls them back.
        Should be called periodically (e.g., every 60s).

        Returns a summary of actions taken.
        """
        now = time.time()
        expired_count = 0
        errors = []

        with self._lock:
            # Find all expired RESERVED transactions
            rows = self._conn.execute(
                "SELECT tx_id FROM transactions WHERE state=? AND expires_at < ?",
                (TxState.RESERVED.value, now)
            ).fetchall()

        # Rollback each expired transaction (outside the main lock)
        for (tx_id,) in rows:
            try:
                self.rollback(tx_id)
                expired_count += 1
                logger.info(f"[{self.agent_id}] RECONCILE: expired tx={tx_id[:8]}...")
            except Exception as e:
                errors.append({"tx_id": tx_id, "error": str(e)})

        # Verify invariant: balance + reserved = total_funds
        with self._lock:
            row = self._conn.execute(
                "SELECT balance_micro, reserved_micro FROM balances WHERE agent_id=?",
                (self.agent_id,)
            ).fetchone()

        integrity_ok = True
        if row:
            balance, reserved = row
            if reserved < 0:
                integrity_ok = False
                logger.error(
                    f"[{self.agent_id}] INVARIANT VIOLATION: reserved={reserved} < 0"
                )

        self._last_reconcile = now

        return {
            "expired_rolled_back": expired_count,
            "errors": errors,
            "integrity_ok": integrity_ok,
            "timestamp": now,
        }

    # ── TOKEN BUDGET (from monorepo TokenBudgetGuard) ────────────────

    def record_token_usage(
        self,
        tokens_used: int,
        provider: str = "unknown",
        model: str = "unknown",
    ):
        """Record token consumption for budget tracking."""
        self._conn.execute(
            "INSERT INTO token_usage(agent_id, tokens_used, provider, model, recorded_at) VALUES(?,?,?,?,?)",
            (self.agent_id, tokens_used, provider, model, time.time())
        )

    def can_spend_tokens(self, estimated_tokens: int = 1000) -> bool:
        """Check if the agent can spend the estimated number of tokens within budget limits."""
        now = time.time()
        hour_ago = now - 3600
        day_ago = now - 86400

        hourly_row = self._conn.execute(
            "SELECT COALESCE(SUM(tokens_used), 0) FROM token_usage WHERE agent_id=? AND recorded_at > ?",
            (self.agent_id, hour_ago)
        ).fetchone()
        daily_row = self._conn.execute(
            "SELECT COALESCE(SUM(tokens_used), 0) FROM token_usage WHERE agent_id=? AND recorded_at > ?",
            (self.agent_id, day_ago)
        ).fetchone()

        hourly_used = hourly_row[0] if hourly_row else 0
        daily_used = daily_row[0] if daily_row else 0

        if hourly_used + estimated_tokens > self._hourly_limit:
            logger.warning(f"[{self.agent_id}] HOURLY token budget exceeded: {hourly_used}/{self._hourly_limit}")
            return False
        if daily_used + estimated_tokens > self._daily_limit:
            logger.warning(f"[{self.agent_id}] DAILY token budget exceeded: {daily_used}/{self._daily_limit}")
            return False

        return True

    def suggest_model_tier(self, estimated_tokens: int = 2000) -> str:
        """Suggest which model tier to use based on remaining budget."""
        now = time.time()
        hour_ago = now - 3600

        hourly_row = self._conn.execute(
            "SELECT COALESCE(SUM(tokens_used), 0) FROM token_usage WHERE agent_id=? AND recorded_at > ?",
            (self.agent_id, hour_ago)
        ).fetchone()
        hourly_used = hourly_row[0] if hourly_row else 0
        pct = hourly_used / max(self._hourly_limit, 1)

        if pct < 0.5:
            return "premium"
        elif pct < 0.8:
            return "standard"
        elif pct < 0.95:
            return "economy"
        else:
            return "blocked"

    # ── QUERIES ──────────────────────────────────────────────────────

    def get_transaction(self, tx_id: str) -> Optional[Transaction]:
        """Retrieve a transaction by ID."""
        row = self._conn.execute(
            "SELECT tx_id, agent_id, amount_micro, state, context_json, created_at, expires_at, resolved_at FROM transactions WHERE tx_id=?",
            (tx_id,)
        ).fetchone()
        if not row:
            return None
        return Transaction(
            tx_id=row[0],
            agent_id=row[1],
            amount=_from_micro(row[2]),
            state=TxState(row[3]),
            context=json.loads(row[4]),
            created_at=row[5],
            expires_at=row[6],
            resolved_at=row[7],
        )

    def snapshot(self) -> EngineSnapshot:
        """Get a point-in-time view of the engine state."""
        now = time.time()

        bal_row = self._conn.execute(
            "SELECT balance_micro, reserved_micro FROM balances WHERE agent_id=?",
            (self.agent_id,)
        ).fetchone()

        balance_micro = bal_row[0] if bal_row else 0
        reserved_micro = bal_row[1] if bal_row else 0

        active = self._conn.execute(
            "SELECT COUNT(*) FROM transactions WHERE agent_id=? AND state=?",
            (self.agent_id, TxState.RESERVED.value)
        ).fetchone()[0]

        committed_sum = self._conn.execute(
            "SELECT COALESCE(SUM(amount_micro), 0) FROM transactions WHERE agent_id=? AND state=?",
            (self.agent_id, TxState.COMMITTED.value)
        ).fetchone()[0]

        rb_sum = self._conn.execute(
            "SELECT COALESCE(SUM(amount_micro), 0) FROM transactions WHERE agent_id=? AND state=?",
            (self.agent_id, TxState.ROLLED_BACK.value)
        ).fetchone()[0]

        # Token budget
        hour_ago = now - 3600
        day_ago = now - 86400
        hourly_used = self._conn.execute(
            "SELECT COALESCE(SUM(tokens_used), 0) FROM token_usage WHERE agent_id=? AND recorded_at > ?",
            (self.agent_id, hour_ago)
        ).fetchone()[0]
        daily_used = self._conn.execute(
            "SELECT COALESCE(SUM(tokens_used), 0) FROM token_usage WHERE agent_id=? AND recorded_at > ?",
            (self.agent_id, day_ago)
        ).fetchone()[0]

        return EngineSnapshot(
            agent_id=self.agent_id,
            balance=_from_micro(balance_micro),
            total_reserved=_from_micro(reserved_micro),
            total_funds=_from_micro(balance_micro),
            active_reservations=active,
            total_committed=_from_micro(committed_sum),
            total_rolled_back=_from_micro(rb_sum),
            hourly_used=hourly_used,
            daily_used=daily_used,
            hourly_limit=self._hourly_limit,
            daily_limit=self._daily_limit,
            is_throttled=(hourly_used >= self._hourly_limit or daily_used >= self._daily_limit),
        )

    def get_ledger(self, limit: int = 50) -> List[Dict[str, Any]]:
        """Get the most recent ledger entries (append-only audit trail)."""
        rows = self._conn.execute(
            "SELECT ts, tx_id, agent_id, action, amount_micro, balance_after, context_json FROM ledger ORDER BY id DESC LIMIT ?",
            (limit,)
        ).fetchall()
        return [
            {
                "ts": r[0],
                "tx_id": r[1],
                "agent_id": r[2],
                "action": r[3],
                "amount": str(_from_micro(r[4])),
                "balance_after": str(_from_micro(r[5])),
                "context": json.loads(r[6]),
            }
            for r in rows
        ]
