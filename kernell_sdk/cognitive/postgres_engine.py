import psycopg2
from psycopg2.errors import SerializationFailure
from decimal import Decimal
import logging

logger = logging.getLogger("kernell.postgres_engine")

def get_connection(dsn: str):
    conn = psycopg2.connect(dsn)
    # Require SERIALIZABLE isolation for financial atomicity
    conn.set_session(isolation_level=psycopg2.extensions.ISOLATION_LEVEL_SERIALIZABLE)
    return conn

def init_db(conn):
    with conn:
        with conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS ledger (
                    id BIGSERIAL PRIMARY KEY,
                    tx_id TEXT UNIQUE NOT NULL,
                    from_account TEXT NOT NULL,
                    to_account TEXT NOT NULL,
                    amount NUMERIC(38, 18) NOT NULL,
                    created_at TIMESTAMP DEFAULT NOW()
                );
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS balances (
                    account_id TEXT PRIMARY KEY,
                    balance NUMERIC(38, 18) NOT NULL DEFAULT 0
                );
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS idempotency_keys (
                    token_id TEXT PRIMARY KEY,
                    created_at TIMESTAMP DEFAULT NOW()
                );
            """)

def commit_transfer(conn, token_id: str, source: str, target: str, amount: Decimal) -> str:
    with conn:
        with conn.cursor() as cur:
            # 1. Idempotency (strong lock)
            cur.execute("""
                INSERT INTO idempotency_keys (token_id)
                VALUES (%s)
                ON CONFLICT DO NOTHING
            """, (token_id,))
            if cur.rowcount == 0:
                return "ALREADY_PROCESSED"
                
            # 2. Lock rows in a consistent order to prevent deadlocks
            accts = sorted([source, target])
            cur.execute("""
                SELECT account_id, balance
                FROM balances
                WHERE account_id IN (%s, %s)
                FOR UPDATE
            """, (accts[0], accts[1]))
            
            rows = {r[0]: Decimal(str(r[1])) for r in cur.fetchall()}
            if source not in rows:
                rows[source] = Decimal(0)
            if target not in rows:
                rows[target] = Decimal(0)
                
            if rows[source] < amount:
                raise ValueError("Insufficient funds")
                
            # 3. Update balances
            cur.execute("""
                INSERT INTO balances (account_id, balance)
                VALUES (%s, %s)
                ON CONFLICT (account_id) DO UPDATE SET balance = balances.balance - %s;
            """, (source, -amount, amount))
            
            cur.execute("""
                INSERT INTO balances (account_id, balance)
                VALUES (%s, %s)
                ON CONFLICT (account_id) DO UPDATE SET balance = balances.balance + %s;
            """, (target, amount, amount))
            
            # 4. Ledger append (immutable)
            cur.execute("""
                INSERT INTO ledger (tx_id, from_account, to_account, amount)
                VALUES (%s, %s, %s, %s)
            """, (token_id, source, target, amount))
            
            return "OK"

def safe_commit_transfer(dsn: str, token_id: str, source: str, target: str, amount: Decimal):
    """Executes the transfer with automatic retry on serialization failures."""
    conn = get_connection(dsn)
    try:
        for i in range(3):
            try:
                return commit_transfer(conn, token_id, source, target, amount)
            except SerializationFailure:
                conn.rollback()
                logger.warning(f"SerializationFailure on {token_id}, retrying ({i+1}/3)...")
                if i == 2:
                    raise
    finally:
        conn.close()
