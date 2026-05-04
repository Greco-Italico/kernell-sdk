import os
import sys
import time
import subprocess
import threading
import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from decimal import Decimal
import uuid
import psycopg2
from psycopg2.errors import SerializationFailure

# Ensure we can import kernell_sdk components
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
from kernell_sdk.cognitive.postgres_engine import safe_commit_transfer, get_connection, init_db

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("chaos_engine")

PG_DSN = os.environ.get("KERNELL_PG_DSN", "dbname=kernell user=kernell password=securepassword host=localhost")

def run_cmd(cmd):
    """Executes a command safely without shell injection risk."""
    import shlex
    argv = shlex.split(cmd) if isinstance(cmd, str) else cmd
    subprocess.run(argv, shell=False, check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

def setup_db():
    conn = get_connection(PG_DSN)
    init_db(conn)
    with conn.cursor() as cur:
        # Seed test accounts
        cur.execute("INSERT INTO balances (account_id, balance) VALUES ('CHAOS_A', 100000) ON CONFLICT DO NOTHING")
        cur.execute("INSERT INTO balances (account_id, balance) VALUES ('CHAOS_B', 100000) ON CONFLICT DO NOTHING")
    conn.commit()
    conn.close()

def worker_load(thread_id, num_transfers=50):
    """Simulates a worker making continuous transfers."""
    success = 0
    failed = 0
    for i in range(num_transfers):
        token = str(uuid.uuid4())
        try:
            res = safe_commit_transfer(PG_DSN, token, "CHAOS_A", "CHAOS_B", Decimal("10.00"))
            if res == "OK":
                success += 1
        except Exception as e:
            failed += 1
        time.sleep(0.01)  # small delay
    return success, failed

def chaos_kill_postgres():
    """TEST 1: Simulates Postgres crash (kill -9) during active transfers."""
    logger.info("⚡ CHAOS 1: Injecting PostgreSQL hard crash...")
    time.sleep(1)
    run_cmd("docker kill kernell-os-sdk-postgres-1")
    time.sleep(3)
    logger.info("🔄 Recovering PostgreSQL...")
    run_cmd("docker start kernell-os-sdk-postgres-1")
    time.sleep(5)  # wait for recovery

def chaos_stop_redis():
    """TEST 3: Simulates Redis failure."""
    logger.info("⚡ CHAOS 3: Stopping Redis Cache...")
    run_cmd("docker stop kernell-os-sdk-redis-1")
    time.sleep(3)
    logger.info("🔄 Recovering Redis...")
    run_cmd("docker start kernell-os-sdk-redis-1")

def run_chaos_suite():
    logger.info("🚀 Starting Chaos Test Suite")
    setup_db()
    
    # Run concurrent workers
    num_workers = 20
    futures = []
    
    with ThreadPoolExecutor(max_workers=num_workers + 2) as executor:
        for i in range(num_workers):
            futures.append(executor.submit(worker_load, i, 50))
            
        # Inject Chaos concurrently
        executor.submit(chaos_kill_postgres)
        executor.submit(chaos_stop_redis)
        
        total_success = 0
        total_failed = 0
        for f in as_completed(futures):
            if f.result():
                s, failed = f.result()
                total_success += s
                total_failed += failed
                
    logger.info(f"🏁 Chaos Suite Finished. Success: {total_success}, Failed (Expected via DB crash): {total_failed}")
    
    # Verify invariants
    conn = get_connection(PG_DSN)
    with conn.cursor() as cur:
        cur.execute("SELECT balance FROM balances WHERE account_id = 'CHAOS_A'")
        bal_a = cur.fetchone()[0]
        cur.execute("SELECT balance FROM balances WHERE account_id = 'CHAOS_B'")
        bal_b = cur.fetchone()[0]
        
        logger.info(f"Balance A: {bal_a}, Balance B: {bal_b}")
        if (bal_a + bal_b) == Decimal("200000"):
            logger.info("✅ INVARIANT MAINTAINED: Zero sum conservation holds despite chaos.")
        else:
            logger.error("❌ FATAL: Ledger state corrupted!")
    conn.close()

if __name__ == "__main__":
    run_chaos_suite()
