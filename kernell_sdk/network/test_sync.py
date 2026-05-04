import time
import logging
from kernell_sdk.network.node import P2PNode
from kernell_sdk.network.state_sync import Metrics

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(message)s",
    datefmt="%H:%M:%S"
)
logger = logging.getLogger("p2p.sync_demo")

def run_sync_demo():
    redis_url = "redis://localhost:6379/0"
    
    logger.info("Initializing 2 P2P Nodes for Sync Test...")
    node_a = P2PNode("pubkey_A", redis_url)
    node_b = P2PNode("pubkey_B", redis_url)
    
    node_a.start()
    node_b.start()
    
    time.sleep(1)
    
    logger.info("\n--- Phase 1: Normal Sync ---")
    node_a.broadcast_event("TASK_ANNOUNCEMENT", {"task_id": "task_1"})
    time.sleep(0.5)
    
    head_a = node_a.event_log.head()
    head_b = node_b.event_log.head()
    logger.info(f"Node A Head: {head_a}")
    logger.info(f"Node B Head: {head_b}")
    assert head_a == head_b, "Nodes failed to sync normally"
    
    logger.info("\n--- Phase 2: Conflict Generation (Fork) ---")
    # Simulate a network partition by generating conflicting events locally
    # Node A assigns task to X, Node B assigns task to Y
    event_a = node_a.create_event("TASK_ASSIGNED", {"task_id": "task_1", "assignee": "X"})
    event_b = node_b.create_event("TASK_ASSIGNED", {"task_id": "task_1", "assignee": "Y"})
    
    logger.info("Simulating partition and local appends...")
    node_a.state_sync.process_incoming_event(event_a.__dict__)
    node_b.state_sync.process_incoming_event(event_b.__dict__)
    
    logger.info(f"Node A Head before reconnect: {node_a.event_log.head()}")
    logger.info(f"Node B Head before reconnect: {node_b.event_log.head()}")
    
    logger.info("\n--- Phase 3: Reconnection & Conflict Resolution ---")
    # Now they gossip their differing heads
    msg_a = node_a.create_message("STATE_UPDATE", {"event": event_a.__dict__})
    msg_b = node_b.create_message("STATE_UPDATE", {"event": event_b.__dict__})
    
    node_a.gossip.broadcast(msg_a)
    node_b.gossip.broadcast(msg_b)
    
    time.sleep(1) # wait for conflict resolution
    
    head_a_final = node_a.event_log.head()
    head_b_final = node_b.event_log.head()
    logger.info(f"Node A Final Head: {head_a_final}")
    logger.info(f"Node B Final Head: {head_b_final}")
    logger.info(f"Total Divergences Resolved: {Metrics.state_divergence_count}")
    
    if head_a_final == head_b_final:
        logger.info("SUCCESS: Nodes converged to the same state despite the fork.")
    else:
        logger.error("FAILURE: Nodes failed to converge.")

if __name__ == "__main__":
    run_sync_demo()
