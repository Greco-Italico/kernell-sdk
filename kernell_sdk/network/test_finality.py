import time
import logging
from kernell_sdk.network.node import P2PNode

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(message)s",
    datefmt="%H:%M:%S"
)
logger = logging.getLogger("p2p.finality_demo")

def run_finality_demo():
    redis_url = "redis://localhost:6379/0"
    
    logger.info("Initializing 3 P2P Nodes for Consensus & Finality Test...")
    node_a = P2PNode("pubkey_A", redis_url)
    node_b = P2PNode("pubkey_B", redis_url)
    node_c = P2PNode("pubkey_C", redis_url)
    
    node_a.start()
    node_b.start()
    node_c.start()
    
    time.sleep(1)
    
    logger.info("\n--- Phase 1: Normal Operation (Quorum & Depth) ---")
    # A broadcasts event 1
    node_a.broadcast_event("TASK_1", {"data": "test"})
    time.sleep(0.5)
    
    event_1_id = node_a.event_log.head()
    event_1_status = node_a.event_log.index[event_1_id].status
    logger.info(f"Event 1 Status (should be CONFIRMED): {event_1_status}")
    
    # B broadcasts event 2
    node_b.broadcast_event("TASK_2", {"data": "test"})
    time.sleep(0.5)
    
    # C broadcasts event 3
    node_c.broadcast_event("TASK_3", {"data": "test"})
    time.sleep(0.5)
    
    # Now Event 1 should have depth >= 2
    event_1 = node_a.event_log.index[event_1_id]
    logger.info(f"Event 1 Depth: {len(node_a.event_log.events) - node_a.event_log.events.index(event_1) - 1}")
    logger.info(f"Event 1 Finalized? {event_1.finalized}")
    assert event_1.finalized, "Event 1 should be finalized by now"

    logger.info("\n--- Phase 2: Attempting to Fork a Finalized Event ---")
    # We attempt to create a fork starting from before event_1
    # Node C tries to rewrite history
    malicious_event = node_c.create_event("TASK_MALICIOUS", {"data": "hack"})
    malicious_event.prev_hash = event_1.prev_hash # Pointing to the same parent as Event 1
    
    logger.info("Node C sending malicious conflicting event...")
    node_c.state_sync.process_incoming_event(malicious_event.__dict__)
    
    logger.info(f"Did Event 1 get rolled back on Node A? {not node_a.event_log.events[0].finalized}")
    if event_1.finalized:
        logger.info("SUCCESS: The finalized event cannot be rolled back. The network is secure.")
    else:
        logger.error("FAILURE: A finalized event was rolled back!")
        
    logger.info("\nDemo complete.")

if __name__ == "__main__":
    run_finality_demo()
