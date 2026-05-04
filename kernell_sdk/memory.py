"""
Kernell OS SDK — Cortex Shared Memory
══════════════════════════════════════
Provides short-term (key-value) and long-term (episodic stream) memory
backed by Redis. Dramatically reduces token usage by offloading context
to persistent storage instead of stuffing it into every prompt.

Usage:
    memory = Memory(agent_id="ka_abc123")

    memory.store("last_url", "https://example.com", ttl=3600)
    url = memory.fetch("last_url")

    memory.add_episodic("task_completed", {"task": "scrape", "rows": 42})
    summary = memory.summarize_context(max_tokens=300)
"""
import json
from typing import Any, Dict, List, Optional

import structlog

from .config import default_config, KernellConfig

logger = structlog.get_logger("kernell.memory")

try:
    import redis
    HAS_REDIS = True
except ImportError:
    HAS_REDIS = False

# Redis key namespace prefixes
MEMORY_KEY_PREFIX = "kernell:memory"
STREAM_KEY_PREFIX = "kernell:stream"

# Default number of recent events to retrieve for context summarization
DEFAULT_RECENT_EVENT_COUNT = 10


def _build_memory_key(agent_id: str, key: str) -> str:
    """Build a namespaced Redis key for short-term memory."""
    return f"{MEMORY_KEY_PREFIX}:{agent_id}:{key}"


def _build_stream_key(agent_id: str) -> str:
    """Build a namespaced Redis key for the episodic event stream."""
    return f"{STREAM_KEY_PREFIX}:{agent_id}"


class Memory:
    """
    Cortex Shared Memory interface.

    Provides two storage layers:
      - **Short-term**: Key-value pairs with TTL (ideal for task state).
      - **Long-term**: Append-only episodic event stream (ideal for audit trails).

    Falls back gracefully to no-ops when Redis is not installed or configured.
    """

    def __init__(self, agent_id: str, config: Optional[KernellConfig] = None):
        self.agent_id = agent_id
        self.config = config or default_config
        self._redis: Optional[Any] = None

        if HAS_REDIS and self.config.redis_url:
            try:
                self._redis = redis.Redis.from_url(self.config.redis_url)
                self._redis.ping()
                logger.info("memory_connected", agent_id=agent_id)
            except redis.ConnectionError as error:
                logger.warning("redis_connection_failed", error=str(error), agent_id=agent_id)
                self._redis = None

    @property
    def is_connected(self) -> bool:
        """True if Redis is available and connected."""
        return self._redis is not None

    def store(self, key: str, value: Any, ttl: int = 86400) -> None:
        """Store a value in short-term memory with a TTL (default: 24 hours).

        Args:
            key: The lookup key (e.g., "last_url", "task_state").
            value: Any JSON-serializable value.
            ttl: Time-to-live in seconds.
        """
        if not self._redis:
            return

        redis_key = _build_memory_key(self.agent_id, key)
        try:
            serialized_value = json.dumps(value)
            self._redis.setex(redis_key, ttl, serialized_value)
        except TypeError as error:
            logger.error("memory_store_failed_serialization", key=key, error=str(error), agent_id=self.agent_id)
        except redis.RedisError as error:
            logger.error("memory_store_failed_redis", key=key, error=str(error), agent_id=self.agent_id)

    def fetch(self, key: str) -> Optional[Any]:
        """Fetch a value from short-term memory.

        Returns None if the key doesn't exist or Redis is unavailable.
        """
        if not self._redis:
            return None

        redis_key = _build_memory_key(self.agent_id, key)
        try:
            raw_data = self._redis.get(redis_key)
            if raw_data is None:
                return None
            return json.loads(raw_data)
        except json.JSONDecodeError as error:
            logger.error("memory_fetch_failed_decode", key=key, error=str(error), agent_id=self.agent_id)
            return None
        except redis.RedisError as error:
            logger.error("memory_fetch_failed_redis", key=key, error=str(error), agent_id=self.agent_id)
            return None

    def add_episodic(self, event: str, metadata: Optional[Dict[str, Any]] = None) -> None:
        """Append an event to the long-term episodic memory stream.

        Args:
            event: Name of the event (e.g., "task_completed", "error").
            metadata: Optional dictionary of event details.
        """
        if not self._redis:
            return

        stream_key = _build_stream_key(self.agent_id)
        try:
            payload = json.dumps({"event": event, "metadata": metadata or {}})
            self._redis.xadd(stream_key, {"payload": payload})
        except TypeError as error:
            logger.error("memory_add_episodic_failed_serialization", event=event, error=str(error), agent_id=self.agent_id)
        except redis.RedisError as error:
            logger.error("memory_add_episodic_failed_redis", event=event, error=str(error), agent_id=self.agent_id)

    def summarize_context(self, max_tokens: int = 500) -> str:
        """Retrieve a condensed summary of the agent's recent history.

        This avoids passing 50k+ token chat histories to the LLM.

        Args:
            max_tokens: Maximum token budget for the summary (advisory).

        Returns:
            A human-readable summary string.
        """
        if not self._redis:
            return "No recent memory available."

        stream_key = _build_stream_key(self.agent_id)
        
        try:
            raw_events = self._redis.xrevrange(
                stream_key,
                count=DEFAULT_RECENT_EVENT_COUNT,
            )
        except redis.RedisError as error:
            logger.error("memory_summarize_failed_redis", error=str(error), agent_id=self.agent_id)
            return "No recent memory available."

        if not raw_events:
            return "No recent memory available."

        summary_lines = ["Recent events:"]
        for _event_id, event_data in reversed(raw_events):
            try:
                payload = json.loads(event_data[b"payload"])
                event_name = payload.get("event", "unknown")
                summary_lines.append(f"- {event_name}")
            except (json.JSONDecodeError, KeyError) as error:
                logger.warning("memory_summarize_skipped_event", error=str(error), agent_id=self.agent_id)
                continue

        return "\n".join(summary_lines)
