from __future__ import annotations

from enum import Enum
from dataclasses import dataclass, asdict
from decimal import Decimal, ROUND_HALF_UP
import hashlib
import json
import os
import sqlite3
import time
import uuid
from typing import Optional, Iterable, Union

class EscrowState(Enum):
    CREATED = "CREATED"
    FUNDED = "FUNDED"
    LOCKED = "LOCKED"
    RELEASED = "RELEASED"
    DISPUTED = "DISPUTED"
    REFUNDED = "REFUNDED"
    EXPIRED = "EXPIRED"

# ── H-09 FIX: Micro-KERN integer representation ──
from decimal import Decimal, ROUND_HALF_UP, InvalidOperation

# ── H-09 FIX: Micro-KERN integer representation ──
# 1 KERN = 1,000,000 μKERN.  All DB storage uses INTEGER to avoid float rounding.
_MICRO_KERN = 1_000_000

def validate_amount(amount: Union[float, int, str, Decimal]) -> Decimal:
    """Fintech-grade strict validation for financial amounts."""
    try:
        amt = Decimal(str(amount)) if not isinstance(amount, Decimal) else amount
    except InvalidOperation:
        raise ValueError("Invalid numeric value")
        
    if not amt.is_finite():
        raise ValueError("Amount must be finite (cannot be NaN or Inf)")
    if amt <= 0:
        raise ValueError("Amount must be positive")
    return amt

def _kern_to_micro(amount: Union[float, int, str, Decimal]) -> int:
    """Convert a human-readable KERN amount to μKERN integer with strict validation."""
    amt = validate_amount(amount)
    micro = int((amt * _MICRO_KERN).to_integral_value(rounding=ROUND_HALF_UP))
    if micro <= 0:
        raise ValueError("Invalid micro amount (underflow after rounding)")
    return micro

def _micro_to_kern(micro: int) -> Decimal:
    """Convert μKERN integer back to human-readable KERN Decimal."""
    return Decimal(micro) / _MICRO_KERN


@dataclass
class EscrowContract:
    contract_id: str
    buyer_id: str
    seller_id: str
    amount_kern: Decimal  # H-09: Decimal, stored as μKERN INTEGER in DB
    state: EscrowState
    created_at: float
    timeout_ts: float
    arbitrator_id: Optional[str] = None


class EscrowError(Exception):
    pass


class Unauthorized(EscrowError):
    pass


class InvalidSignature(EscrowError):
    pass


class InvalidTransition(EscrowError):
    pass


class ReplayDetected(EscrowError):
    pass


def _canonical_json(obj: dict) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"))


def _hash_chain(prev_hash: str, event_json: str) -> str:
    return hashlib.sha256((prev_hash + event_json).encode("utf-8")).hexdigest()


def _now() -> float:
    return time.time()


def _ensure_db(path: str) -> sqlite3.Connection:
    # SQLite special-case: in-memory DB has no directory.
    if path != ":memory:":
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    conn = sqlite3.connect(path, isolation_level=None)
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA foreign_keys=ON;")
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS contracts (
            contract_id TEXT PRIMARY KEY,
            buyer_id TEXT NOT NULL,
            seller_id TEXT NOT NULL,
            arbitrator_id TEXT,
            amount_micro_kern INTEGER NOT NULL,
            state TEXT NOT NULL,
            created_at REAL NOT NULL,
            timeout_ts REAL NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            contract_id TEXT NOT NULL,
            ts REAL NOT NULL,
            actor_id TEXT NOT NULL,
            action TEXT NOT NULL,
            expected_prev_state TEXT NOT NULL,
            nonce TEXT NOT NULL,
            event_json TEXT NOT NULL,
            prev_hash TEXT NOT NULL,
            event_hash TEXT NOT NULL,
            signature_hex TEXT NOT NULL,
            UNIQUE(contract_id, nonce),
            FOREIGN KEY(contract_id) REFERENCES contracts(contract_id)
        )
        """
    )
    # Enforce append-only semantics at DB level for escrow events.
    conn.execute(
        """
        CREATE TRIGGER IF NOT EXISTS events_no_update
        BEFORE UPDATE ON events
        BEGIN
            SELECT RAISE(FAIL, 'append-only enforced');
        END;
        """
    )
    conn.execute(
        """
        CREATE TRIGGER IF NOT EXISTS events_no_delete
        BEFORE DELETE ON events
        BEGIN
            SELECT RAISE(FAIL, 'append-only enforced');
        END;
        """
    )
    return conn


def _get_last_hash(conn: sqlite3.Connection, contract_id: str) -> str:
    row = conn.execute(
        "SELECT event_hash FROM events WHERE contract_id=? ORDER BY id DESC LIMIT 1",
        (contract_id,),
    ).fetchone()
    return row[0] if row else ("0" * 64)


def _verify_actor_signature(actor_public_key_hex: str, message: str, signature_hex: str) -> bool:
    # reuse identity verifier (Ed25519)
    from kernell_os_sdk.identity import verify_signature
    return verify_signature(message, signature_hex, actor_public_key_hex)

class EscrowManager:
    """
    Alpha-secure Escrow Manager.

    Properties:
    - SQLite persistence (WAL)
    - Append-only event log with hash-chain
    - Mandatory Ed25519 signatures + anti-replay nonces
    - Strict state transitions (fail-close)
    """
    
    def __init__(self, db_path: str = "/var/lib/kernell/escrow.sqlite3", actor_keys: Optional[dict[str, str]] = None, ledger=None):
        self._conn = _ensure_db(db_path)
        # actor_id -> public_key_hex (Ed25519, 32 bytes -> 64 hex chars)
        self._actor_keys = actor_keys or {}
        self.ledger = ledger

    def register_actor_key(self, actor_id: str, public_key_hex: str) -> None:
        if not isinstance(public_key_hex, str) or len(public_key_hex) != 64:
            raise ValueError("public_key_hex must be 64 hex chars (32 bytes)")
        self._actor_keys[actor_id] = public_key_hex

    def _require_sig(self, actor_id: str, intent: dict, signature_hex: str) -> None:
        pub = self._actor_keys.get(actor_id)
        if not pub:
            raise Unauthorized(f"Actor not registered: {actor_id}")
        msg = _canonical_json(intent)
        if not _verify_actor_signature(pub, msg, signature_hex):
            raise InvalidSignature("Invalid Ed25519 signature")

    def _append_event(self, contract_id: str, actor_id: str, action: str, expected_prev_state: str, nonce: str, intent: dict, signature_hex: str) -> None:
        prev_hash = _get_last_hash(self._conn, contract_id)
        event_json = _canonical_json(intent)
        event_hash = _hash_chain(prev_hash, event_json)
        try:
            self._conn.execute(
                """
                INSERT INTO events(contract_id, ts, actor_id, action, expected_prev_state, nonce, event_json, prev_hash, event_hash, signature_hex)
                VALUES(?,?,?,?,?,?,?,?,?,?)
                """,
                (contract_id, _now(), actor_id, action, expected_prev_state, nonce, event_json, prev_hash, event_hash, signature_hex),
            )
        except sqlite3.IntegrityError as exc:
            raise ReplayDetected("Nonce already used for this contract") from exc

    def _get_contract_row(self, contract_id: str):
        row = self._conn.execute(
            "SELECT contract_id,buyer_id,seller_id,arbitrator_id,amount_micro_kern,state,created_at,timeout_ts FROM contracts WHERE contract_id=?",
            (contract_id,),
        ).fetchone()
        return row

    def get_contract(self, contract_id: str) -> Optional[EscrowContract]:
        row = self._get_contract_row(contract_id)
        if not row:
            return None
        return EscrowContract(
            contract_id=row[0],
            buyer_id=row[1],
            seller_id=row[2],
            arbitrator_id=row[3],
            amount_kern=_micro_to_kern(int(row[4])),  # H-09: μKERN → Decimal
            state=EscrowState(row[5]),
            created_at=float(row[6]),
            timeout_ts=float(row[7]),
        )

    def create_escrow(
        self,
        buyer_id: str,
        seller_id: str,
        amount: Union[float, int, str, Decimal],
        *,
        contract_id: str,
        timeout_hours: int = 24,
        arbitrator_id: Optional[str] = None,
        nonce: str,
        signature_hex: str,
    ) -> str:
        if _kern_to_micro(amount) <= 0:
            raise ValueError("amount must be > 0")
        now = _now()
        timeout_ts = now + (timeout_hours * 3600)

        intent = {
            "action": "CREATE",
            "contract_id": contract_id,
            "buyer_id": buyer_id,
            "seller_id": seller_id,
            "arbitrator_id": arbitrator_id,
            "amount_kern": str(amount),  # canonical: string representation for signing
            "expected_prev_state": "NONE",
            "nonce": nonce,
        }
        self._require_sig(buyer_id, intent, signature_hex)

        micro = _kern_to_micro(amount)
        self._conn.execute(
            """
            INSERT INTO contracts(contract_id,buyer_id,seller_id,arbitrator_id,amount_micro_kern,state,created_at,timeout_ts)
            VALUES(?,?,?,?,?,?,?,?)
            """,
            (contract_id, buyer_id, seller_id, arbitrator_id, micro, EscrowState.CREATED.value, now, timeout_ts),
        )
        self._append_event(contract_id, buyer_id, "CREATE", "NONE", nonce, intent, signature_hex)
        return contract_id

    def fund_escrow(self, contract_id: str, *, actor_id: str, expected_prev_state: EscrowState, nonce: str, signature_hex: str) -> bool:
        c = self.get_contract(contract_id)
        if not c:
            return False
        if actor_id != c.buyer_id:
            raise Unauthorized("Only buyer can fund escrow")
        if c.state != expected_prev_state:
            raise InvalidTransition("State mismatch")
        if c.state != EscrowState.CREATED:
            raise InvalidTransition("Can only fund from CREATED")

        intent = {
            "action": "FUND",
            "contract_id": contract_id,
            "expected_prev_state": expected_prev_state.value,
            "nonce": nonce,
            "ts": _now(),
        }
        self._require_sig(actor_id, intent, signature_hex)

        amount_micro = _kern_to_micro(c.amount_kern)

        # C-07 FIX: Atomic state transition with BEGIN IMMEDIATE to prevent TOCTOU
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            rows = self._conn.execute(
                "UPDATE contracts SET state=? WHERE contract_id=? AND state=?",
                (EscrowState.FUNDED.value, contract_id, EscrowState.CREATED.value),
            ).rowcount
            if rows == 0:
                self._conn.execute("ROLLBACK")
                raise InvalidTransition("Concurrent state change detected (TOCTOU)")
                
            if self.ledger:
                from core.audit.double_entry_ledger import JournalLine
                self.ledger.create_account("system", f"wallet_{c.buyer_id}", "asset")
                self.ledger.create_account("system", "escrow_locked", "asset")
                self.ledger.create_journal_entry(
                    tenant_id="system",
                    reference_id=f"escrow_fund_{contract_id}",
                    description=f"Fund escrow {contract_id}",
                    lines=[
                        JournalLine(f"wallet_{c.buyer_id}", "credit", amount_micro),
                        JournalLine("escrow_locked", "debit", amount_micro)
                    ]
                )
                
            self._append_event(contract_id, actor_id, "FUND", expected_prev_state.value, nonce, intent, signature_hex)
            self._conn.execute("COMMIT")
        except Exception:
            try:
                self._conn.execute("ROLLBACK")
            except Exception as e:
                import logging
                logging.warning(f'Suppressed error in {__name__}: {e}')
            raise
        return True

    def lock_escrow(self, contract_id: str, *, actor_id: str, expected_prev_state: EscrowState, nonce: str, signature_hex: str) -> bool:
        c = self.get_contract(contract_id)
        if not c:
            return False
        if actor_id != c.buyer_id:
            raise Unauthorized("Only buyer can lock escrow")
        if c.state != expected_prev_state:
            raise InvalidTransition("State mismatch")
        if c.state != EscrowState.FUNDED:
            raise InvalidTransition("Can only lock from FUNDED")

        intent = {
            "action": "LOCK",
            "contract_id": contract_id,
            "expected_prev_state": expected_prev_state.value,
            "nonce": nonce,
            "ts": _now(),
        }
        self._require_sig(actor_id, intent, signature_hex)

        self._conn.execute("BEGIN IMMEDIATE")
        try:
            rows = self._conn.execute(
                "UPDATE contracts SET state=? WHERE contract_id=? AND state=?",
                (EscrowState.LOCKED.value, contract_id, EscrowState.FUNDED.value),
            ).rowcount
            if rows == 0:
                self._conn.execute("ROLLBACK")
                raise InvalidTransition("Concurrent state change detected (TOCTOU)")
            self._append_event(contract_id, actor_id, "LOCK", expected_prev_state.value, nonce, intent, signature_hex)
            self._conn.execute("COMMIT")
        except Exception:
            try:
                self._conn.execute("ROLLBACK")
            except Exception as e:
                import logging
                logging.warning(f'Suppressed error in {__name__}: {e}')
            raise
        return True

    def release_funds(self, contract_id: str, *, actor_id: str, expected_prev_state: EscrowState, nonce: str, signature_hex: str) -> bool:
        c = self.get_contract(contract_id)
        if not c:
            return False
        if actor_id not in (c.buyer_id, c.arbitrator_id):
            raise Unauthorized("Only buyer or arbitrator can release")
        if c.state != expected_prev_state:
            raise InvalidTransition("State mismatch")
        if c.state != EscrowState.LOCKED:
            raise InvalidTransition("Can only release from LOCKED")

        intent = {
            "action": "RELEASE",
            "contract_id": contract_id,
            "expected_prev_state": expected_prev_state.value,
            "nonce": nonce,
            "ts": _now(),
        }
        self._require_sig(actor_id, intent, signature_hex)
        
        amount_micro = _kern_to_micro(c.amount_kern)

        self._conn.execute("BEGIN IMMEDIATE")
        try:
            rows = self._conn.execute(
                "UPDATE contracts SET state=? WHERE contract_id=? AND state=?",
                (EscrowState.RELEASED.value, contract_id, EscrowState.LOCKED.value),
            ).rowcount
            if rows == 0:
                self._conn.execute("ROLLBACK")
                raise InvalidTransition("Concurrent state change detected (TOCTOU)")
                
            if self.ledger:
                from core.audit.double_entry_ledger import JournalLine
                self.ledger.create_account("system", f"wallet_{c.seller_id}", "asset")
                self.ledger.create_account("system", "escrow_locked", "asset")
                self.ledger.create_journal_entry(
                    tenant_id="system",
                    reference_id=f"escrow_release_{contract_id}",
                    description=f"Release escrow {contract_id} to vendor",
                    lines=[
                        JournalLine("escrow_locked", "credit", amount_micro),
                        JournalLine(f"wallet_{c.seller_id}", "debit", amount_micro)
                    ]
                )
                
            self._append_event(contract_id, actor_id, "RELEASE", expected_prev_state.value, nonce, intent, signature_hex)
            self._conn.execute("COMMIT")
        except Exception:
            try:
                self._conn.execute("ROLLBACK")
            except Exception as e:
                import logging
                logging.warning(f'Suppressed error in {__name__}: {e}')
            raise
        return True

    def open_dispute(self, contract_id: str, *, actor_id: str, expected_prev_state: EscrowState, nonce: str, signature_hex: str) -> bool:
        c = self.get_contract(contract_id)
        if not c:
            return False
        if actor_id not in (c.buyer_id, c.seller_id):
            raise Unauthorized("Only buyer or seller can dispute")
        if c.state != expected_prev_state:
            raise InvalidTransition("State mismatch")
        if c.state != EscrowState.LOCKED:
            raise InvalidTransition("Can only dispute from LOCKED")

        intent = {
            "action": "DISPUTE",
            "contract_id": contract_id,
            "expected_prev_state": expected_prev_state.value,
            "nonce": nonce,
            "ts": _now(),
        }
        self._require_sig(actor_id, intent, signature_hex)

        self._conn.execute("BEGIN IMMEDIATE")
        try:
            rows = self._conn.execute(
                "UPDATE contracts SET state=? WHERE contract_id=? AND state=?",
                (EscrowState.DISPUTED.value, contract_id, EscrowState.LOCKED.value),
            ).rowcount
            if rows == 0:
                self._conn.execute("ROLLBACK")
                raise InvalidTransition("Concurrent state change detected (TOCTOU)")
            self._append_event(contract_id, actor_id, "DISPUTE", expected_prev_state.value, nonce, intent, signature_hex)
            self._conn.execute("COMMIT")
        except Exception:
            try:
                self._conn.execute("ROLLBACK")
            except Exception as e:
                import logging
                logging.warning(f'Suppressed error in {__name__}: {e}')
            raise
        return True

    def refund(self, contract_id: str, *, actor_id: str, expected_prev_state: EscrowState, nonce: str, signature_hex: str) -> bool:
        c = self.get_contract(contract_id)
        if not c:
            return False
        if actor_id != c.arbitrator_id:
            raise Unauthorized("Only arbitrator can refund")
        if c.state != expected_prev_state:
            raise InvalidTransition("State mismatch")
        if c.state not in (EscrowState.DISPUTED, EscrowState.EXPIRED):
            raise InvalidTransition("Can only refund from DISPUTED or EXPIRED")

        intent = {
            "action": "REFUND",
            "contract_id": contract_id,
            "expected_prev_state": expected_prev_state.value,
            "nonce": nonce,
            "ts": _now(),
        }
        self._require_sig(actor_id, intent, signature_hex)

        self._conn.execute("BEGIN IMMEDIATE")
        try:
            rows = self._conn.execute(
                "UPDATE contracts SET state=? WHERE contract_id=? AND state IN (?,?)",
                (EscrowState.REFUNDED.value, contract_id, EscrowState.DISPUTED.value, EscrowState.EXPIRED.value),
            ).rowcount
            if rows == 0:
                self._conn.execute("ROLLBACK")
                raise InvalidTransition("Concurrent state change detected (TOCTOU)")
            self._append_event(contract_id, actor_id, "REFUND", expected_prev_state.value, nonce, intent, signature_hex)
            self._conn.execute("COMMIT")
        except Exception:
            try:
                self._conn.execute("ROLLBACK")
            except Exception as e:
                import logging
                logging.warning(f'Suppressed error in {__name__}: {e}')
            raise
        return True

    def expire_if_due(self, contract_id: str) -> bool:
        """Expire a contract if past timeout. C-06 FIX: Records audit event."""
        c = self.get_contract(contract_id)
        if not c:
            return False
        if c.state in (EscrowState.RELEASED, EscrowState.REFUNDED):
            return False
        if _now() < c.timeout_ts:
            return False

        self._conn.execute("BEGIN IMMEDIATE")
        try:
            rows = self._conn.execute(
                "UPDATE contracts SET state=? WHERE contract_id=? AND state NOT IN (?,?)",
                (EscrowState.EXPIRED.value, contract_id, EscrowState.RELEASED.value, EscrowState.REFUNDED.value),
            ).rowcount
            if rows == 0:
                self._conn.execute("ROLLBACK")
                return False
            # C-06 FIX: Record expiry as a SYSTEM event in the hash-chain
            expire_intent = {
                "action": "EXPIRE",
                "contract_id": contract_id,
                "expected_prev_state": c.state.value,
                "reason": "timeout_reached",
                "timeout_ts": c.timeout_ts,
                "expired_at": _now(),
            }
            prev_hash = _get_last_hash(self._conn, contract_id)
            event_json = _canonical_json(expire_intent)
            event_hash = _hash_chain(prev_hash, event_json)
            self._conn.execute(
                """
                INSERT INTO events(contract_id, ts, actor_id, action, expected_prev_state, nonce, event_json, prev_hash, event_hash, signature_hex)
                VALUES(?,?,?,?,?,?,?,?,?,?)
                """,
                (contract_id, _now(), "SYSTEM", "EXPIRE", c.state.value, f"sys_expire_{contract_id}", event_json, prev_hash, event_hash, "SYSTEM_UNSIGNED"),
            )
            self._conn.execute("COMMIT")
        except Exception:
            try:
                self._conn.execute("ROLLBACK")
            except Exception as e:
                import logging
                logging.warning(f'Suppressed error in {__name__}: {e}')
            raise
        return True
