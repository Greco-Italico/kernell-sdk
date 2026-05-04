from fastapi import FastAPI, HTTPException, BackgroundTasks
from pydantic import BaseModel
from typing import Dict, List, Optional
import uuid
import time
import logging

from kernell_sdk.marketplace.scheduler import MarketScheduler, MarketNode
from kernell_sdk.marketplace.controller import EconomicController
from kernell_sdk.reputation.engine import ReputationEngine
from kernell_sdk.reputation.dispute import DisputeArbitrationSystem
from kernell_sdk.reputation.receipt import ExecutionReceipt

from prometheus_client import start_http_server, Counter, Gauge

app = FastAPI(title="Kernell OS Control Plane")
logger = logging.getLogger(__name__)

# Prometheus Metrics
TASKS_SUBMITTED = Counter("tasks_submitted_total", "Total tasks submitted to control plane")
TASKS_ASSIGNED = Counter("tasks_assigned_total", "Total tasks successfully assigned")
SCHEDULER_LATENCY = Gauge("scheduler_latency_seconds", "Time taken by scheduler")
MARKET_DOMINANCE = Gauge("market_dominance", "Top 5 Node Market Dominance")

# Start Prometheus metrics server
start_http_server(9100)

class NodeRegistration(BaseModel):
    agent_id: str
    region: str
    provider: str
    stake: float
    price_per_sec: float
    public_key: str

class TaskRequest(BaseModel):
    task_code: str
    is_critical: bool = False
    task_value: float = 10.0

# Global State
active_nodes: Dict[str, MarketNode] = {}
reputation_engine = ReputationEngine()
scheduler = MarketScheduler()
controller = EconomicController()
arbitration = DisputeArbitrationSystem(reputation_engine)

@app.post("/register")
async def register_node(registration: NodeRegistration):
    """Worker nodes call this to join the network."""
    node = MarketNode(
        agent_id=registration.agent_id,
        region=registration.region,
        provider=registration.provider,
        reputation=100.0,
        stake=registration.stake,
        price_per_sec=registration.price_per_sec,
        reliability=1.0
    )
    active_nodes[registration.agent_id] = node
    reputation_engine._scores[registration.agent_id] = 100.0
    logger.info(f"Node registered: {registration.agent_id} from {registration.region}")
    return {"status": "registered", "agent_id": node.agent_id}

@app.post("/tasks/submit")
async def submit_task(request: TaskRequest):
    """External clients submit tasks here to be scheduled and routed to worker nodes."""
    TASKS_SUBMITTED.inc()
    
    if not active_nodes:
        raise HTTPException(status_code=503, detail="No nodes available in the network")
        
    start_time = time.time()
    try:
        nodes_list = list(active_nodes.values())
        assignment = scheduler.schedule_task(
            nodes_list, 
            request.task_value, 
            request.is_critical,
            dynamic_weights=controller.get_scheduler_weights(),
            redundancy_probability=controller.redundancy_probability
        )
        TASKS_ASSIGNED.inc()
    except ValueError as e:
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        SCHEDULER_LATENCY.set(time.time() - start_time)
        
    # In a full Pub/Sub system, this would push the task to Redis queues assigned to the specific nodes
    # For now, we return the routing decision
    
    response = {
        "task_id": uuid.uuid4().hex,
        "primary": assignment["primary"].agent_id,
        "verifiers": [v.agent_id for v in assignment["verifiers"]],
        "status": "dispatched"
    }
    return response

@app.post("/receipts/verify")
async def verify_receipts(receipt: ExecutionReceipt):
    """Worker nodes submit their signed receipts here after execution."""
    # This endpoint will eventually handle the arbitration logic and update the reputation engine
    # and trigger controller updates if necessary.
    pass
