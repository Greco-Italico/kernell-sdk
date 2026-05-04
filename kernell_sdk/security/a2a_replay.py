"""
A2A replay protection (E3 extended to inter-agent channel).

- Timestamp skew window (milliseconds).
- Nonce store with TTL + LRU cap (thread-safe).

⚠️  MULTI-NODE WARNING: This implementation is single-process (in-memory).
    In a multi-node or multi-process deployment, use A2AReplayGuardRedis
    from `kernell_sdk.security.a2a_replay_redis` to share the nonce store
    across all nodes via Redis. Without this, replay attacks across nodes
    will not be detected.
"""
from __future__ import annotations

import os
import threading
import time
import warnings
from collections import OrderedDict
class A2AReplayError(ValueError):
    """Raised when an A2A message is outside the time window or reuses a nonce."""


class A2AReplayGuard:
    """
    In-memory nonce store suitable for single-process agents.
    For multi-node routing, replace with Redis-backed store (same interface).
    """

    WINDOW_MS = 120_000
    NONCE_TTL_SEC = 600.0
    MAX_NONCES = 10_000

    def __init__(
        self,
        *,
        window_ms: int = WINDOW_MS,
        nonce_ttl_sec: float = NONCE_TTL_SEC,
        max_nonces: int = MAX_NONCES,
    ) -> None:
        self._window_ms = int(window_ms)
        self._nonce_ttl_sec = float(nonce_ttl_sec)
        self._max_nonces = int(max_nonces)
        self._nonce_seen_at: "OrderedDict[str, float]" = OrderedDict()
        self._lock = threading.Lock()

        # HARD FAIL in production to prevent single-node replay bypass.
        if os.environ.get("KERNELL_ENV", "development") == "production":
            raise RuntimeError(
                "A2AReplayGuard is using an IN-MEMORY nonce store. "
                "In a multi-node or multi-process deployment this does NOT prevent "
                "cross-node replay attacks. "
                "Use A2AReplayGuardRedis (kernell_sdk.security.a2a_replay_redis) "
                "and set KERNELL_REDIS_URL to a shared Redis instance."
            )

    def _prune_unlocked(self, now: float) -> None:
        cutoff = now - self._nonce_ttl_sec
        stale = [n for n, ts in self._nonce_seen_at.items() if ts < cutoff]
        for n in stale:
            del self._nonce_seen_at[n]
        while len(self._nonce_seen_at) > self._max_nonces:
            self._nonce_seen_at.popitem(last=False)

    def assert_timestamp_skew(self, timestamp_ms: int) -> None:
        """Reject messages outside the anti-replay time window."""
        now_ms = int(time.time() * 1000)
        if abs(now_ms - int(timestamp_ms)) > self._window_ms:
            raise A2AReplayError("A2A timestamp outside allowed skew window")

    def consume_nonce(self, nonce: str) -> None:
        """
        Register a nonce after cryptographic verification succeeded.
        Raises A2AReplayError if nonce was already consumed.
        """
        if not nonce or len(nonce) > 128:
            raise A2AReplayError("A2A nonce missing or invalid length")
        now = time.time()
        with self._lock:
            self._prune_unlocked(now)
            if nonce in self._nonce_seen_at:
                raise A2AReplayError("A2A nonce reuse (replay)")
            self._nonce_seen_at[nonce] = now
            while len(self._nonce_seen_at) > self._max_nonces:
                self._nonce_seen_at.popitem(last=False)
