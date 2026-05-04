import time
import threading
from concurrent.futures import Future
from typing import Dict, Optional
import uuid

from ..models import ExecutionRequest, ExecutionResult
from .scheduler import Scheduler
from ..base import BaseRuntime

class RuntimeOrchestrator:
    def __init__(self, runtime: BaseRuntime, num_workers: int = 10):
        self.runtime = runtime
        self.scheduler = Scheduler()
        self.num_workers = num_workers
        self.running = True
        self.futures: Dict[str, Future] = {}
        self.lock = threading.Lock()
        
        self.workers = []
        for _ in range(num_workers):
            t = threading.Thread(target=self._worker_loop, daemon=True)
            t.start()
            self.workers.append(t)

    def submit(self, request: ExecutionRequest, request_id: Optional[str] = None) -> Future:
        request_id = request_id or getattr(request, "request_id", None) or uuid.uuid4().hex
        future = Future()
        with self.lock:
            self.futures[request_id] = future
        
        # Attach the request ID and submit timestamp for tracing
        request._internal_id = request_id
        request._submit_time = time.time()
        
        self.scheduler.submit(request)
        return future

    def _worker_loop(self):
        from . import metrics as prom
        while self.running:
            req = self.scheduler.next()
            
            if not req:
                time.sleep(0.005) # 5ms backoff if queue is empty
                continue
                
            req_id = getattr(req, "_internal_id", None)
            submit_time = getattr(req, "_submit_time", None)
            
            if submit_time:
                wait_time = time.time() - submit_time
                # Extract tier for prometheus labels
                tier = getattr(req, "tenant_id", "default_tenant")
                if tier not in ["free", "pro", "enterprise"]:
                    tier = "free" # Fallback mapping
                prom.QUEUE_WAIT_LATENCY.labels(tenant_tier=tier).observe(wait_time)
            
            try:
                # 1. Execute via the underlying FirecrackerRuntime (which applies Admission Control)
                result = self.runtime.execute(req)
                
                if req_id:
                    with self.lock:
                        if req_id in self.futures:
                            self.futures[req_id].set_result(result)
                            del self.futures[req_id]
            except Exception as e:
                if req_id:
                    with self.lock:
                        if req_id in self.futures:
                            self.futures[req_id].set_exception(e)
                            del self.futures[req_id]

    def shutdown(self):
        self.running = False
        for w in self.workers:
            w.join(timeout=1.0)
