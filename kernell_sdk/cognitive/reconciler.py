import time
import logging
from redis import Redis
from decimal import Decimal
import psycopg2

logger = logging.getLogger("kernell.reconciler")

BALANCE_PREFIX = "balance:"

def reconcile_postgres_vs_cache(pg_conn, redis_client: Redis):
    with pg_conn.cursor() as cur:
        cur.execute("SELECT account_id, balance FROM balances")
        db_balances = {r[0]: Decimal(str(r[1])) for r in cur.fetchall()}
        
    mismatches = []
    for acc, db_val in db_balances.items():
        cache_val = redis_client.get(f"{BALANCE_PREFIX}{acc}")
        if cache_val is not None:
            cache_dec = Decimal(cache_val)
            if cache_dec != db_val:
                mismatches.append((acc, cache_dec, db_val))
                
    return mismatches

def start_reconciler_loop(pg_dsn: str, redis_url: str):
    logger.info("Starting automated Hybrid Reconciler (Postgres vs Redis Cache)...")
    pg_conn = psycopg2.connect(pg_dsn)
    redis_client = Redis.from_url(redis_url, decode_responses=True)
    
    while True:
        try:
            mismatches = reconcile_postgres_vs_cache(pg_conn, redis_client)
            if mismatches:
                logger.error(f"⚠️ MISMATCH DETECTED: {len(mismatches)} nodes out of sync!")
                for m in mismatches:
                    logger.error(f"Account: {m[0]}, Cache: {m[1]}, DB(Source of Truth): {m[2]}")
            else:
                logger.debug("✅ Postgres and Redis Cache are perfectly synchronized")
        except Exception as e:
            logger.error(f"Reconciler error: {e}")
            pg_conn.rollback()
            
        time.sleep(10)

if __name__ == "__main__":
    import os
    logging.basicConfig(level=logging.INFO)
    pg_dsn = os.environ.get("KERNELL_PG_DSN", "dbname=kernell user=kernell password=securepassword host=localhost")
    redis_url = os.environ.get("KERNELL_REDIS_URL", "redis://localhost:6379")
    start_reconciler_loop(pg_dsn, redis_url)
