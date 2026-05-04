"""
A2A replay protection — Redis-backed store for multi-instance production.

Same contract as A2AReplayGuard (in-memory), but nonces are stored in Redis
with native TTL expiry.  No LRU eviction problem (D-03 fix).

Usage:
    from kernell_sdk.security.a2a_replay_redis import RedisReplayGuard

    guard = RedisReplayGuard(redis_url="redis://localhost:6379/0")
    guard.assert_timestamp_skew(msg.timestamp_ms)
    guard.consume_nonce(msg.nonce)

Drop-in replacement: both guards share the same method signatures.
"""
from __future__ import annotations

import logging
import time
from typing import Optional

from .a2a_replay import A2AReplayError

logger = logging.getLogger("kernell.a2a_replay_redis")

# Prefix for all nonce keys — avoids collision with other Redis users.
_NONCE_PREFIX = "kernell:a2a:nonce:"


class RedisReplayGuard:
    """
    Production replay guard backed by Redis.

    Properties:
      - Atomic nonce consumption via SETNX (no race conditions).
      - TTL-based expiry (no LRU eviction under load).
      - Works across multiple agent instances sharing the same Redis.
      - Graceful degradation: if Redis is down, raises immediately (fail-close).
    """

    WINDOW_MS: int = 120_000
    NONCE_TTL_SEC: int = 600

    def __init__(
        self,
        *,
        redis_url: str = "redis://localhost:6379/0",
        window_ms: int = WINDOW_MS,
        nonce_ttl_sec: int = NONCE_TTL_SEC,
        redis_client: Optional[object] = None,
    ) -> None:
        self._window_ms = int(window_ms)
        self._nonce_ttl_sec = int(nonce_ttl_sec)

        if redis_client is not None:
            # Allow injection for testing (mock or fakeredis).
            self._redis = redis_client
        else:
            try:
                import redis as _redis_lib
                self._redis = _redis_lib.Redis.from_url(
                    redis_url,
                    decode_responses=True,
                    socket_connect_timeout=2,
                    socket_timeout=2,
                    retry_on_timeout=True,
                )
                # Fail-fast: verify connectivity at init.
                self._redis.ping()
                logger.info("redis_replay_guard_connected", url=redis_url)
            except ImportError:
                raise RuntimeError(
                    "RedisReplayGuard requires the 'redis' package. "
                    "Install it with: pip install redis"
                )
            except Exception as exc:
                raise RuntimeError(
                    f"RedisReplayGuard cannot connect to Redis at {redis_url}: {exc}"
                ) from exc

    def assert_timestamp_skew(self, timestamp_ms: int) -> None:
        """Reject messages outside the anti-replay time window."""
        now_ms = int(time.time() * 1000)
        if abs(now_ms - int(timestamp_ms)) > self._window_ms:
            raise A2AReplayError("A2A timestamp outside allowed skew window")

    def consume_nonce(self, nonce: str) -> None:
        """
        Atomically register a nonce in Redis.

        Uses SET NX EX (set-if-not-exists with expiry) — this is the atomic
        primitive that eliminates both the LRU eviction bug (D-03) and any
        TOCTOU race between check and insert.

        Raises A2AReplayError if the nonce was already consumed.
        """
        if not nonce or len(nonce) > 128:
            raise A2AReplayError("A2A nonce missing or invalid length")

        key = f"{_NONCE_PREFIX}{nonce}"

        try:
            # SET key "1" NX EX ttl — returns True only if key was created.
            was_set = self._redis.set(
                key, "1", nx=True, ex=self._nonce_ttl_sec
            )
        except Exception as exc:
            # Fail-close: if Redis is unreachable, reject the message.
            logger.error("redis_replay_guard_error: %s", str(exc))
            raise A2AReplayError(
                f"A2A replay check failed (Redis unavailable): {exc}"
            ) from exc

        if not was_set:
            raise A2AReplayError("A2A nonce reuse (replay)")

    def health_check(self) -> bool:
        """Returns True if Redis is reachable."""
        try:
            return self._redis.ping()
        except Exception:
            return False
