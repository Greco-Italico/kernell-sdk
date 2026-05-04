"""
Kernell OS SDK — Cluster Discovery
══════════════════════════════════
Redis-backed P2P node discovery. Allows agents running on different
hardware to find each other and form a unified swarm.
"""
import time
import json
import logging
import threading
from typing import Dict, List, Optional
from dataclasses import dataclass, asdict

import redis

logger = logging.getLogger("kernell.cluster.discovery")

@dataclass
class ClusterNode:
    """Represents a node (agent) in the P2P cluster."""
    node_id: str
    agent_name: str
    hardware_profile: dict
    status: str = "active"
    last_seen: float = 0.0


class ClusterDiscovery:
    """
    Handles P2P node discovery using Redis Pub/Sub and Key Expiration.
    """
    
    def __init__(self, redis_url: str, cluster_name: str = "default_swarm"):
        self.redis_url = redis_url
        self.cluster_name = cluster_name
        self.r = redis.Redis.from_url(redis_url, decode_responses=True)
        self._heartbeat_thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        self._local_node: Optional[ClusterNode] = None
        
        # Redis keyspaces
        self._prefix = f"kernell:cluster:{cluster_name}"

    def join(self, agent_name: str, hardware_profile: dict) -> ClusterNode:
        """Joins the cluster and starts broadcasting presence."""
        import uuid
        node_id = f"node_{uuid.uuid4().hex[:8]}"
        
        self._local_node = ClusterNode(
            node_id=node_id,
            agent_name=agent_name,
            hardware_profile=hardware_profile,
            last_seen=time.time()
        )
        
        # Announce presence immediately
        self._announce()
        
        # Start heartbeat
        self._stop_event.clear()
        self._heartbeat_thread = threading.Thread(target=self._heartbeat_loop, daemon=True)
        self._heartbeat_thread.start()
        
        logger.info(f"Joined cluster '{self.cluster_name}' as {node_id} ({agent_name})")
        return self._local_node

    def leave(self):
        """Leaves the cluster gracefully."""
        if self._local_node:
            self._stop_event.set()
            if self._heartbeat_thread:
                self._heartbeat_thread.join(timeout=2.0)
                
            # Remove from Redis
            key = f"{self._prefix}:nodes:{self._local_node.node_id}"
            self.r.delete(key)
            logger.info(f"Left cluster '{self.cluster_name}'")
            self._local_node = None

    def get_active_nodes(self) -> List[ClusterNode]:
        """Returns a list of all currently active nodes in the cluster."""
        pattern = f"{self._prefix}:nodes:*"
        keys = self.r.keys(pattern)
        
        nodes = []
        for key in keys:
            data = self.r.get(key)
            if data:
                try:
                    node_dict = json.loads(data)
                    nodes.append(ClusterNode(**node_dict))
                except Exception as e:
                    logger.debug(f"Failed to parse node data from {key}: {e}")
                    
        return nodes

    def _announce(self):
        """Writes presence to Redis with a TTL (Time-To-Live)."""
        if not self._local_node:
            return
            
        self._local_node.last_seen = time.time()
        key = f"{self._prefix}:nodes:{self._local_node.node_id}"
        
        # Data expires in 30 seconds if not renewed by heartbeat
        self.r.setex(key, 30, json.dumps(asdict(self._local_node)))

    def _heartbeat_loop(self):
        """Background thread that continuously announces presence."""
        while not self._stop_event.is_set():
            try:
                self._announce()
            except Exception as e:
                logger.error(f"Cluster heartbeat failed: {e}")
            # Heartbeat every 10 seconds
            self._stop_event.wait(10.0)
