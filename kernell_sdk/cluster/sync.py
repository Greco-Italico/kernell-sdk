"""
Kernell OS SDK — Distributed Memory Sync
════════════════════════════════════════
Synchronizes the 'Cortex Memory' (Episodic context) across all 
nodes in the cluster using Redis Streams. This ensures that a 
sub-agent on Node B knows what a sub-agent on Node A just did.
"""
import json
import time
import logging
import threading
from typing import Callable, Optional

import redis
from ..memory import Memory

logger = logging.getLogger("kernell.cluster.sync")


class MemorySync:
    """
    Hooks into the local Memory instance and broadcasts new episodes
    to the Redis cluster, while subscribing to episodes from other nodes.
    """

    def __init__(self, local_memory: Memory, redis_url: str, cluster_name: str = "default_swarm", node_id: str = "unknown"):
        self.local_memory = local_memory
        self.r = redis.Redis.from_url(redis_url, decode_responses=True)
        self.cluster_name = cluster_name
        self.node_id = node_id
        
        self._stream_key = f"kernell:cluster:{cluster_name}:memory_stream"
        self._consumer_group = f"group_{cluster_name}"
        self._stop_event = threading.Event()
        self._listen_thread: Optional[threading.Thread] = None
        
        self._setup_stream()

    def _setup_stream(self):
        """Creates the Redis Stream and Consumer Group if they don't exist."""
        try:
            # XGROUP CREATE stream_key group_name $ MKSTREAM
            # Creates group reading from the end ($) and makes stream if needed
            self.r.xgroup_create(self._stream_key, self._consumer_group, id="$", mkstream=True)
        except redis.exceptions.ResponseError as e:
            if "BUSYGROUP" in str(e):
                pass # Group already exists
            else:
                logger.error(f"Error setting up memory sync stream: {e}")

    def start(self):
        """Starts the background thread to listen for cluster memory updates."""
        self._stop_event.clear()
        self._listen_thread = threading.Thread(target=self._listen_loop, daemon=True)
        self._listen_thread.start()
        logger.info(f"Memory sync started for node {self.node_id}")

    def stop(self):
        """Stops the synchronization thread."""
        self._stop_event.set()
        if self._listen_thread:
            self._listen_thread.join(timeout=2.0)
        logger.info("Memory sync stopped.")

    def broadcast(self, episode_type: str, data: dict):
        """
        Sends a new local memory episode to the cluster.
        Should be called by the local Memory instance when add_episodic is used.
        """
        payload = {
            "source_node": self.node_id,
            "timestamp": str(time.time()),
            "type": episode_type,
            "data": json.dumps(data)
        }
        # Add to Redis Stream
        self.r.xadd(self._stream_key, payload)

    def _listen_loop(self):
        """Reads from the Redis stream to ingest memories from other nodes."""
        consumer_name = f"consumer_{self.node_id}"
        
        while not self._stop_event.is_set():
            try:
                # Block for up to 2 seconds waiting for new messages
                # > means read messages never delivered to this consumer
                streams = self.r.xreadgroup(
                    groupname=self._consumer_group,
                    consumername=consumer_name,
                    streams={self._stream_key: ">"},
                    count=10,
                    block=2000
                )
                
                for stream_name, messages in streams:
                    for message_id, payload in messages:
                        source_node = payload.get("source_node")
                        
                        # Ignore our own broadcasts
                        if source_node != self.node_id:
                            ep_type = payload.get("type", "cluster_event")
                            try:
                                data = json.loads(payload.get("data", "{}"))
                                # Add the remote memory to our local Redis instance
                                # (prepended to indicate it came from the cluster)
                                data["_from_node"] = source_node
                                self.local_memory.add_episodic(ep_type, data)
                            except json.JSONDecodeError:
                                logger.error(f"Failed to decode memory payload from {source_node}")
                                
                        # Acknowledge the message so it's not delivered again
                        self.r.xack(self._stream_key, self._consumer_group, message_id)
                        
            except Exception as e:
                logger.error(f"Memory sync stream read error: {e}")
                self._stop_event.wait(5.0) # Backoff
