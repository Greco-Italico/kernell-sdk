import time
import random
from typing import Dict, List, Optional
import logging

logger = logging.getLogger("p2p.peer_manager")

class Peer:
    def __init__(self, node_id: str, address: str, pubkey: str):
        self.node_id = node_id
        self.address = address
        self.pubkey = pubkey
        self.last_seen = time.time()
        self.score = 100
        self.is_banned = False

class PeerManager:
    """Manages the network view of other peers."""
    def __init__(self, local_node_id: str):
        self.local_node_id = local_node_id
        self.peers: Dict[str, Peer] = {}
        
    def add_peer(self, node_id: str, address: str, pubkey: str):
        if node_id == self.local_node_id or node_id in self.peers:
            return
        self.peers[node_id] = Peer(node_id, address, pubkey)
        logger.debug(f"Added peer {node_id}")

    def get_random_peers(self, k: int = 3) -> List[Peer]:
        valid_peers = [p for p in self.peers.values() if not p.is_banned]
        if not valid_peers:
            return []
        return random.sample(valid_peers, min(k, len(valid_peers)))

    def penalize_peer(self, node_id: str, penalty: int):
        if node_id not in self.peers:
            return
        peer = self.peers[node_id]
        peer.score -= penalty
        if peer.score < -50:
            peer.is_banned = True
            logger.warning(f"Peer {node_id} BANNED (score: {peer.score})")

    def update_last_seen(self, node_id: str):
        if node_id in self.peers:
            self.peers[node_id].last_seen = time.time()
