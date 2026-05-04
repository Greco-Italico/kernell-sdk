import os
import time
import requests
import uuid
import logging
from typing import Dict, Any

from kernell_sdk.identity import generate_keypair, public_key_to_hex
from kernell_sdk.runtime.hybrid_runtime import HybridRuntime
from kernell_sdk.reputation.proof_of_execution import ProofOfExecutionEngine
from kernell_sdk.reputation.receipt import ExecutionReceipt

from prometheus_client import start_http_server, Counter, Histogram

logger = logging.getLogger(__name__)

# Prometheus Metrics
TASKS_EXECUTED = Counter("tasks_executed_total", "Total tasks executed")
EXEC_TIME = Histogram("execution_latency_seconds", "Execution latency")
FAILURES = Counter("failures_total", "Total failures")

class TestnetWorker:
    def __init__(self):
        self.control_plane_url = os.environ.get("CONTROL_PLANE_URL", "http://localhost:8000")
        self.strategy = os.environ.get("NODE_STRATEGY", "honest")
        
        # Crypto Identity
        self.private_key, self.public_key = generate_keypair()
        self.public_key_hex = public_key_to_hex(self.public_key)
        self.agent_id = f"worker_{uuid.uuid4().hex[:8]}"
        
        self.runtime = HybridRuntime()
        self.poe_engine = ProofOfExecutionEngine()

    def register(self):
        """Registers the worker with the Control Plane."""
        payload = {
            "agent_id": self.agent_id,
            "region": "testnet-local",
            "provider": "docker",
            "stake": 1500.0,
            "price_per_sec": 0.05,
            "public_key": self.public_key_hex
        }
        
        if self.strategy == "adaptive":
            payload["price_per_sec"] = 0.02
            payload["stake"] = 3000.0
            
        max_retries = 5
        for i in range(max_retries):
            try:
                response = requests.post(f"{self.control_plane_url}/register", json=payload)
                response.raise_for_status()
                logger.info(f"Registered successfully as {self.agent_id} ({self.strategy})")
                return True
            except Exception as e:
                logger.warning(f"Registration failed, retrying... ({i+1}/{max_retries})")
                time.sleep(2)
        return False

    def poll_and_execute(self):
        """
        In a full testnet, this would subscribe to Redis.
        For this simplified worker, it polls for tasks or waits for pushes.
        """
        logger.info(f"Worker {self.agent_id} waiting for tasks...")
        # Event loop simulating receiving tasks
        while True:
            time.sleep(1)

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    
    # Start Metrics Server
    metrics_port = int(os.environ.get("METRICS_PORT", "9100"))
    start_http_server(metrics_port)
    logger.info(f"Started Prometheus metrics on port {metrics_port}")
    
    worker = TestnetWorker()
    if worker.register():
        worker.poll_and_execute()
    else:
        logger.error("Failed to join testnet. Exiting.")
