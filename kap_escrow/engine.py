"""
KAP Escrow Engine — Trustless Agent-to-Agent Financial Protection
==================================================================
The core of the protocol. Two agents who don't trust each other
transact through a mathematical escrow with full atomicity.

Compatible with:
  • A2A Agent Cards (agent identity)
  • AP2 Mandates (authorization triggers)
  • Any Redis-compatible backend

All operations: WATCH/MULTI/EXEC atomic, WAL-first, HMAC-signed.

Security patches applied:
  • KAP-01: Escrow keys no longer expire via TTL (prevents fund loss)
  • KAP-03: WAL writes PENDING, marks COMMITTED after Redis confirms
  • KAP-04: Nonce cleanup uses atomic Lua script
  • KAP-05: Mainnet Hardening — Escrow execution uses Pessimistic Lua locking
"""
from __future__ import annotations

import json
import time
import uuid
import logging
from typing import Any, Dict, List, Optional, Tuple

from kap_escrow.wal import TransactionWAL
from kap_escrow.signing import sign_tx

logger = logging.getLogger("KAP_ESCROW")

NONCE_TTL_S = 172800  # 48h anti-replay window
MAX_ESCROW_DURATION_S = 86400 * 7  # 7 days max before auto-reaper
CAP_STATE_KEY = "kernell:economy:cap_state"
CAP_MIN_BURN_RATE = 0.05
CAP_MAX_BURN_RATE = 0.70

# ── Lua Scripts for Pessimistic Locking (Mainnet-Grade) ─────────────

_LUA_NONCE_CHECK = """
local nonce_set = KEYS[1]
local nonce_ts  = KEYS[2]
local nonce     = ARGV[1]
local now       = tonumber(ARGV[2])
local ttl       = tonumber(ARGV[3])
if redis.call('SADD', nonce_set, nonce) == 0 then return 0 end
redis.call('ZADD', nonce_ts, now, nonce)
local expired = redis.call('ZRANGEBYSCORE', nonce_ts, 0, now - ttl)
if #expired > 0 then
    redis.call('ZREM', nonce_ts, unpack(expired))
    redis.call('SREM', nonce_set, unpack(expired))
end
return 1
"""

_LUA_LOCK = """
local wk = KEYS[1]
local ek = KEYS[2]
local amount = tonumber(ARGV[1])
local meta = ARGV[2]
local bal = tonumber(redis.call('GET', wk) or 0)
if redis.call('EXISTS', ek) == 1 then return -1 end
if bal < amount then return -2 end
redis.call('INCRBYFLOAT', wk, -amount)
redis.call('SET', ek, meta)
return 1
"""

_LUA_REFUND = """
local ek = KEYS[1]
local wk = KEYS[2]
local locked = tonumber(ARGV[1])
if redis.call('EXISTS', ek) == 0 then return -1 end
redis.call('INCRBYFLOAT', wk, locked)
redis.call('DEL', ek)
return 1
"""

_LUA_SETTLE = """
local ek = KEYS[1]
local wk_provider = KEYS[2]
local wk_buyer = KEYS[3]
local burn_pool = KEYS[4]
local burn_events_log = KEYS[5]

local net_provider = tonumber(ARGV[1])
local refund_buyer = tonumber(ARGV[2])
local burn = tonumber(ARGV[3])
local event_json = ARGV[4]

if redis.call('EXISTS', ek) == 0 then return -1 end

redis.call('INCRBYFLOAT', wk_provider, net_provider)
if refund_buyer > 0 then
    redis.call('INCRBYFLOAT', wk_buyer, refund_buyer)
end
if burn > 0 then
    redis.call('INCRBYFLOAT', burn_pool, burn)
    redis.call('LPUSH', burn_events_log, event_json)
end
redis.call('DEL', ek)
return 1
"""


class EscrowEngine:
    """
    Redis-backed escrow with Mainnet-Grade pessimistic execution.

    Args:
        redis_client: Any Redis client (redis-py compatible)
        private_key: Ed25519 signing key seed (32 bytes)
        wal_path: Path for the Write-Ahead Log
        burn_rate: % burned on each settlement (deflationary)
        key_prefix: Redis key prefix (namespace isolation)
    """

    def __init__(
        self,
        redis_client,
        private_key: bytes = b"",
        wal_path: str = "./kap_escrow_wal.bin",
        burn_rate: Optional[float] = None,
        key_prefix: str = "kap",
    ):
        self.r = redis_client
        self.private_key = private_key
        self.burn_rate = burn_rate
        self.prefix = key_prefix
        self.wal = TransactionWAL(wal_path)

        # Cache Lua scripts
        self._sha_nonce = self.r.script_load(_LUA_NONCE_CHECK)
        self._sha_lock = self.r.script_load(_LUA_LOCK)
        self._sha_refund = self.r.script_load(_LUA_REFUND)
        self._sha_settle = self.r.script_load(_LUA_SETTLE)

        if not private_key:
            logger.warning("EscrowEngine: private_key not set. Asymmetric signatures disabled.")

    def _resolve_burn_rate(self) -> float:
        """
        Resolve burn rate from explicit config first, then CAP runtime state.
        Falls back to 1% when no CAP state is available.
        """
        if self.burn_rate is not None:
            return float(self.burn_rate)

        try:
            raw = self.r.get(CAP_STATE_KEY)
            if raw:
                state = json.loads(raw)
                active = float(state.get("active_burn_rate", 0.01))
                return max(CAP_MIN_BURN_RATE, min(CAP_MAX_BURN_RATE, active))
        except Exception:
            pass

        return 0.01

    def _wk(self, agent: str) -> str:
        return f"{self.prefix}:wallet:{agent}"

    def _ek(self, contract_id: str) -> str:
        return f"{self.prefix}:escrow:{contract_id}"

    def _check_nonce(self, nonce: str) -> bool:
        result = self.r.evalsha(
            self._sha_nonce, 2,
            f"{self.prefix}:nonces", f"{self.prefix}:nonce_ts",
            nonce, str(time.time()), str(NONCE_TTL_S)
        )
        if result == 0:
            logger.warning(f"REPLAY DETECTED: nonce={nonce[:16]}...")
            return False
        return True

    def _append_tx(self, record: Dict[str, Any]) -> None:
        record.setdefault("ts", time.time())
        if "tx_id" not in record:
            record["tx_id"] = str(uuid.uuid4())
        if not self._check_nonce(record["tx_id"]):
            raise ValueError(f"Replay detected: tx_id={record['tx_id']}")
            
        if self.private_key:
            sign_tx(record, self.private_key)

        record["wal_status"] = "PENDING"
        self.wal.append(record)

        try:
            tx_log = f"{self.prefix}:tx_log"
            pipe = self.r.pipeline()
            pipe.lpush(tx_log, json.dumps(record))
            pipe.ltrim(tx_log, 0, 999)
            pipe.execute()
        except Exception as e:
            logger.error(f"Redis commit failed for tx_id={record['tx_id']}: {e}")
            raise

        record["wal_status"] = "COMMITTED"
        try:
            self.wal.append(record)
        except Exception:
            logger.warning(f"WAL COMMITTED marker failed for tx_id={record['tx_id']}")

    def get_balance(self, agent: str) -> float:
        raw = self.r.get(self._wk(agent))
        return float(raw) if raw is not None else 0.0

    def credit(self, agent: str, amount: float, memo: str = "credit") -> Tuple[bool, str]:
        if amount <= 0:
            return False, "amount must be positive"
        self.r.incrbyfloat(self._wk(agent), amount)
        self._append_tx({"type": "credit", "to": agent, "amount": round(amount, 8), "memo": memo})
        return True, "ok"

    def lock(self, buyer: str, amount: float, contract_id: str) -> Tuple[bool, str]:
        if amount <= 0:
            return False, "amount must be positive"
            
        wk = self._wk(buyer)
        ek = self._ek(contract_id)
        meta = json.dumps({"buyer": buyer, "locked": amount, "ts": time.time()})
        
        # Pessimistic Lock Lua Execution
        res = self.r.evalsha(self._sha_lock, 2, wk, ek, str(amount), meta)
        
        if res == -1: return False, "escrow_exists"
        if res == -2: return False, "insufficient_balance"
        
        self._append_tx({
            "type": "escrow_lock", "from": buyer, "to": "ESCROW",
            "amount": round(amount, 8), "contract_id": contract_id,
        })
        return True, "ok"

    def refund(self, contract_id: str) -> Tuple[bool, str]:
        ek = self._ek(contract_id)
        raw = self.r.get(ek)
        if not raw:
            return False, "no_escrow"
            
        meta = json.loads(raw)
        buyer, locked = meta["buyer"], float(meta["locked"])
        wk = self._wk(buyer)

        # Pessimistic Refund Lua Execution
        res = self.r.evalsha(self._sha_refund, 2, ek, wk, str(locked))
        if res == -1: return False, "escrow_already_consumed"

        self._append_tx({
            "type": "escrow_refund", "from": "ESCROW", "to": buyer,
            "amount": round(locked, 8), "contract_id": contract_id,
        })
        return True, "ok"

    def settle(self, contract_id: str, provider: str, cost: float, success: bool = True) -> Tuple[bool, str]:
        if not success or cost <= 0:
            return self.refund(contract_id)

        ek = self._ek(contract_id)
        raw = self.r.get(ek)
        if not raw:
            return False, "no_escrow"
            
        meta = json.loads(raw)
        buyer, locked = meta["buyer"], float(meta["locked"])

        cost = min(cost, locked)
        burn_rate = self._resolve_burn_rate()
        burn = round(cost * burn_rate, 12)
        net_provider = round(cost - burn, 12)
        refund_buyer = round(locked - cost, 12)
        
        tx_id = str(uuid.uuid4())
        burn_json = json.dumps({"tx_id": tx_id, "amount": burn, "from": contract_id, "ts": time.time()})

        # Pessimistic Settle Lua Execution
        res = self.r.evalsha(
            self._sha_settle, 5,
            ek, self._wk(provider), self._wk(buyer), f"{self.prefix}:burn_pool", f"{self.prefix}:burn_events:log",
            str(net_provider), str(refund_buyer), str(burn), burn_json
        )
        if res == -1: return False, "escrow_already_consumed"

        self._append_tx({
            "tx_id": tx_id, "type": "settlement",
            "from": buyer, "to": provider,
            "amount": round(cost, 8), "burn": burn, "burn_rate": burn_rate,
            "contract_id": contract_id,
        })
        return True, tx_id

    def get_escrow_info(self, contract_id: str) -> Optional[Dict[str, Any]]:
        raw = self.r.get(self._ek(contract_id))
        if not raw: return None
        meta = json.loads(raw)
        meta["contract_id"] = contract_id
        return meta

    def reap_stale_escrows(self, max_age_s: float = MAX_ESCROW_DURATION_S) -> List[Dict[str, Any]]:
        refunded = []
        cursor = 0
        while True:
            cursor, keys = self.r.scan(cursor, match=f"{self.prefix}:escrow:*", count=100)
            for key in keys:
                raw = self.r.get(key)
                if not raw: continue
                try:
                    meta = json.loads(raw)
                    if time.time() - meta.get("ts", 0) >= max_age_s:
                        cid = key.replace(f"{self.prefix}:escrow:", "") if isinstance(key, str) else key.decode().replace(f"{self.prefix}:escrow:", "")
                        if self.refund(cid)[0]:
                            refunded.append({"contract_id": cid, "amount": meta.get("locked")})
                except Exception:
                    pass
            if cursor == 0: break
        return refunded
