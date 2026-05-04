import json
import os
import asyncio
from kernell_sdk.runtime.scheduler import WorkerState

def load_workers(path: str):
    if not os.path.exists(path):
        return []
        
    with open(path, "r") as f:
        data = json.load(f)
        
    workers = []
    for w in data.get("workers", []):
        if not w.get("enabled", True):
            continue
        workers.append(
            WorkerState(
                id=w["id"],
                url=w["url"],
                max_concurrency=w.get("max_concurrency", 8)
            )
        )
    return workers

async def reload_loop(scheduler, path):
    last_mtime = 0
    while True:
        try:
            if os.path.exists(path):
                mtime = os.path.getmtime(path)
                if mtime != last_mtime:
                    new_workers = load_workers(path)
                    
                    async with scheduler._lock:
                        old_map = {w.url: w for w in scheduler.workers}
                        merged = []
                        for nw in new_workers:
                            if nw.url in old_map:
                                ow = old_map[nw.url]
                                nw.health_score = ow.health_score
                                nw.error_rate = ow.error_rate
                                nw.circuit_state = ow.circuit_state
                                nw.avg_latency_ms = ow.avg_latency_ms
                                nw.inflight = ow.inflight  # MUST PRESERVE
                                nw.last_heartbeat = ow.last_heartbeat
                            merged.append(nw)
                                
                        scheduler.workers = merged
                    last_mtime = mtime
        except Exception as e:
            import logging
            logging.warning(f'Suppressed error in {__name__}: {e}')
        await asyncio.sleep(5)
