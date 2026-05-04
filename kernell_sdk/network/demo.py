import time
import logging
from threading import Thread
from kernell_sdk.network.node import P2PNode

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(message)s",
    datefmt="%H:%M:%S"
)
logger = logging.getLogger("p2p.demo")

def run_demo():
    redis_url = "redis://localhost:6379/0"
    
    logger.info("Initializing 3 P2P Nodes...")
    node_a = P2PNode("pubkey_A_1234567890", redis_url)
    node_b = P2PNode("pubkey_B_1234567890", redis_url)
    node_c = P2PNode("pubkey_C_1234567890", redis_url)
    
    node_a.start()
    node_b.start()
    node_c.start()
    
    time.sleep(1) # Allow pubsub to connect
    
    logger.info("\n--- Phase 1: Valid Gossip ---")
    node_a.broadcast_event("TASK_ANNOUNCEMENT", {"task_id": "task_1", "reward": 50})
    time.sleep(0.5)
    
    logger.info("\n--- Phase 2: Peer Discovery Check ---")
    logger.info(f"Node B peers: {list(node_b.peer_manager.peers.keys())}")
    
    logger.info("\n--- Phase 3: Malicious Message (Invalid Epoch) ---")
    # Node C crafts a malicious message from the future
    bad_msg = node_c.create_message("EXECUTION_RECEIPT", {"task_id": "task_1"})
    bad_msg["epoch"] += 10 
    node_c.gossip.broadcast(bad_msg)
    
    time.sleep(0.5)
    logger.info(f"Node A peer score for Node C: {node_a.peer_manager.peers[node_c.node_id].score}")
    
    logger.info("\n--- Phase 4: Banning Peer ---")
    # Node C sends more bad messages to trigger a ban
    for _ in range(5):
        bad_msg = node_c.create_message("STATE_SYNC", {})
        bad_msg["signature"] = "invalid_sig"
        node_c.gossip.broadcast(bad_msg)
        
    time.sleep(0.5)
    is_banned = node_a.peer_manager.peers[node_c.node_id].is_banned
    logger.info(f"Is Node C banned by Node A? {is_banned}")
    
    logger.info("\nDemo complete.")

if __name__ == "__main__":
    run_demo()
