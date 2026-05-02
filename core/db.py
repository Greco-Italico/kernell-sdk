import os
import sqlite3
import psycopg
import logging
from contextlib import contextmanager

logger = logging.getLogger(__name__)

DB_URL = os.getenv("DATABASE_URL")
IS_POSTGRES = DB_URL and DB_URL.startswith("postgres")

def _get_postgres_conn():
    return psycopg.connect(DB_URL)

def _get_sqlite_conn(db_path: str):
    if db_path == ":memory:":
        conn = sqlite3.connect(db_path)
    else:
        os.makedirs(os.path.dirname(db_path) or ".", exist_ok=True)
        conn = sqlite3.connect(db_path)
        conn.execute("PRAGMA journal_mode=WAL;")
    conn.row_factory = sqlite3.Row
    return conn

@contextmanager
def get_db_conn(sqlite_fallback_path: str):
    """
    Yields a connection. 
    If DATABASE_URL is set to a postgres URL, uses psycopg (Postgres).
    Otherwise, uses sqlite3 with the provided fallback path.
    """
    if IS_POSTGRES:
        conn = _get_postgres_conn()
        try:
            yield conn
        finally:
            conn.close()
    else:
        conn = _get_sqlite_conn(sqlite_fallback_path)
        try:
            yield conn
        finally:
            conn.close()

def execute(sql: str, params: tuple = (), sqlite_fallback_path: str = "/var/lib/kernell/default.sqlite3"):
    """Executes a query and commits."""
    if IS_POSTGRES:
        # Swap ? for %s automatically to avoid rewriting all SQL
        sql = sql.replace("?", "%s")
    
    with get_db_conn(sqlite_fallback_path) as conn:
        if IS_POSTGRES:
            with conn.cursor() as cur:
                cur.execute(sql, params)
            conn.commit()
        else:
            conn.execute(sql, params)
            conn.commit()

def query_one(sql: str, params: tuple = (), sqlite_fallback_path: str = "/var/lib/kernell/default.sqlite3"):
    """Executes a SELECT query and returns one row as a dict-like object."""
    if IS_POSTGRES:
        sql = sql.replace("?", "%s")
    
    with get_db_conn(sqlite_fallback_path) as conn:
        if IS_POSTGRES:
            # Psycopg3 uses dict_row factory if needed, but we can just use default and wrap it if needed.
            # For simplicity, returning a dict if row_factory is dict_row, but standard cursor returns tuple.
            from psycopg.rows import dict_row
            with conn.cursor(row_factory=dict_row) as cur:
                cur.execute(sql, params)
                return cur.fetchone()
        else:
            return conn.execute(sql, params).fetchone()

def query_all(sql: str, params: tuple = (), sqlite_fallback_path: str = "/var/lib/kernell/default.sqlite3"):
    """Executes a SELECT query and returns all rows."""
    if IS_POSTGRES:
        sql = sql.replace("?", "%s")
    
    with get_db_conn(sqlite_fallback_path) as conn:
        if IS_POSTGRES:
            from psycopg.rows import dict_row
            with conn.cursor(row_factory=dict_row) as cur:
                cur.execute(sql, params)
                return cur.fetchall()
        else:
            return conn.execute(sql, params).fetchall()
