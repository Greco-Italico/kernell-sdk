import json
import logging
import threading
from typing import Dict, Any, Callable
from collections import OrderedDict
import redis

logger = logging.getLogger("p2p.gossip")

class LRUCache:
    def __init__(self, capacity: int):
        self.cache = OrderedDict()
        self.capacity = capacity

    def contains(self, key: str) -> bool:
        if key not in self.cache:
            return False
        self.cache.move_to_end(key)
        return True

    def add(self, key: str):
        self.cache[key] = True
        self.cache.move_to_end(key)
        if len(self.cache) > self.capacity:
            self.cache.popitem(last=False)

class GossipLayer:
    """Propagates and receives messages probabilistically."""
    def __init__(self, redis_url: str, channel: str = "kernell_gossip"):
        self.redis_client = redis.Redis.from_url(redis_url)
        self.pubsub = self.redis_client.pubsub()
        self.channel = channel
        self.seen_messages = LRUCache(10000)
        self.on_message_callback = None
        self._listener_thread = None

    def start(self, callback: Callable[[Dict[str, Any]], None]):
        self.on_message_callback = callback
        self.pubsub.subscribe(**{self.channel: self._handle_raw_message})
        self._listener_thread = threading.Thread(target=self.pubsub.run_in_thread, kwargs={'sleep_time': 0.01})
        self._listener_thread.start()
        logger.info(f"Gossip layer started on channel {self.channel}")

    def _handle_raw_message(self, item):
        if item['type'] != 'message':
            return
            
        try:
            msg = json.loads(item['data'].decode('utf-8'))
            msg_id = msg.get("msg_id")
            
            if not msg_id or self.seen_messages.contains(msg_id):
                return
                
            self.seen_messages.add(msg_id)
            
            if self.on_message_callback:
                self.on_message_callback(msg)
                
        except json.JSONDecodeError:
            logger.warning("Received malformed JSON gossip message")
        except Exception as e:
            logger.error(f"Error handling gossip message: {e}")

    def broadcast(self, message: Dict[str, Any]):
        """
        In a true P2P mesh this would select peers and send directly.
        Using Redis pub/sub as the transport bus for the MVP.
        """
        msg_id = message.get("msg_id")
        if msg_id:
            self.seen_messages.add(msg_id)
            
        payload = json.dumps(message)
        self.redis_client.publish(self.channel, payload)
