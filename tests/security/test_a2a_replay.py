"""A2A replay guard (E3 on agent channel)."""
import time

import pytest

from kernell_sdk.security.a2a_replay import A2AReplayGuard, A2AReplayError


def test_nonce_reuse_rejected():
    g = A2AReplayGuard()
    g.consume_nonce("once")
    with pytest.raises(A2AReplayError, match="reuse"):
        g.consume_nonce("once")


def test_timestamp_skew():
    g = A2AReplayGuard(window_ms=1000)
    ok_ms = int(time.time() * 1000)
    g.assert_timestamp_skew(ok_ms)
    far_future = ok_ms + 9_000_000
    with pytest.raises(A2AReplayError, match="skew"):
        g.assert_timestamp_skew(far_future)
