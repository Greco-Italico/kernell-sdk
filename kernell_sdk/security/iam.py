import time
import hmac
import hashlib
import json
from typing import Optional

class SecurityViolation(Exception):
    pass

class Unauthorized(Exception):
    pass

class VaultBackend:
    def __init__(self, backend_dict: dict = None):
        self.store = backend_dict if backend_dict is not None else {}

    def read(self, path: str) -> Optional[str]:
        return self.store.get(path)

    def write(self, path: str, value: str):
        self.store[path] = value

class NamespacedVault:
    def __init__(self, backend: VaultBackend):
        self.backend = backend

    def get_secret(self, tenant_id: str, agent_id: str, secret_name: str) -> Optional[str]:
        if ".." in secret_name or secret_name.startswith("/"):
            raise SecurityViolation("Path traversal attempt")
        full_path = f"{tenant_id}/{agent_id}/{secret_name}"
        return self.backend.read(full_path)

    def put_secret(self, tenant_id: str, agent_id: str, secret_name: str, value: str) -> None:
        if ".." in secret_name or secret_name.startswith("/"):
            raise SecurityViolation("Path traversal attempt")
        full_path = f"{tenant_id}/{agent_id}/{secret_name}"
        self.backend.write(full_path, value)

    # --- Key Versioning Management ---
    def add_key(self, tenant_id: str, agent_id: str, version: str, secret: str, status: str = "active"):
        """Add a new versioned key for an agent."""
        keys_path = f"{tenant_id}/{agent_id}/signing_keys"
        raw = self.backend.read(keys_path)
        keys = json.loads(raw) if raw else {}
        
        keys[version] = {
            "version": version,
            "secret": secret,
            "status": status,
            "created_at": int(time.time())
        }
        self.backend.write(keys_path, json.dumps(keys))

    def revoke_key(self, tenant_id: str, agent_id: str, version: str):
        """Revoke a specific key version."""
        keys_path = f"{tenant_id}/{agent_id}/signing_keys"
        raw = self.backend.read(keys_path)
        keys = json.loads(raw) if raw else {}
        if version in keys:
            keys[version]["status"] = "revoked"
            self.backend.write(keys_path, json.dumps(keys))
            
    def get_signing_keys(self, tenant_id: str, agent_id: str) -> dict:
        """Get all versioned signing keys for an agent."""
        raw = self.backend.read(f"{tenant_id}/{agent_id}/signing_keys")
        return json.loads(raw) if raw else {}

class ReplayCache:
    """Interface for Replay Protection."""
    def set_if_not_exists(self, key: str, ttl: int) -> bool:
        raise NotImplementedError()

class InMemoryReplayCache(ReplayCache):
    """Fallback for tests."""
    def __init__(self):
        self.store = {}
    def set_if_not_exists(self, key: str, ttl: int) -> bool:
        now = time.time()
        # Clean expired (lazy)
        self.store = {k: v for k, v in self.store.items() if v > now}
        if key in self.store:
            return False
        self.store[key] = now + ttl
        return True

class RedisReplayCache(ReplayCache):
    """Production Redis Replay Cache."""
    def __init__(self, redis_client):
        self.redis = redis_client
    def set_if_not_exists(self, key: str, ttl: int) -> bool:
        return bool(self.redis.set(key, "1", nx=True, ex=ttl))

class RateLimiter:
    """Interface for Rate Limiting."""
    def check_rate_limit(self, tenant_id: str, agent_id: str) -> bool:
        raise NotImplementedError()

class InMemoryRateLimiter(RateLimiter):
    def __init__(self, limit=100, window=60):
        self.store = {}
        self.limit = limit
        self.window = window
    def check_rate_limit(self, tenant_id: str, agent_id: str) -> bool:
        now = time.time()
        key = f"{tenant_id}:{agent_id}"
        data = self.store.get(key, {"count": 0, "expires": 0})
        if now > data["expires"]:
            data = {"count": 1, "expires": now + self.window}
        else:
            data["count"] += 1
        self.store[key] = data
        return data["count"] <= self.limit

class RedisRateLimiter(RateLimiter):
    def __init__(self, redis_client, limit=100, window=60):
        self.redis = redis_client
        self.limit = limit
        self.window = window
    def check_rate_limit(self, tenant_id: str, agent_id: str) -> bool:
        key = f"kernell:iam:rl:{tenant_id}:{agent_id}"
        current = self.redis.incr(key)
        if current == 1:
            self.redis.expire(key, self.window)
        return current <= self.limit

class IAMPolicyEngine:
    def __init__(self, vault: NamespacedVault, replay_cache: ReplayCache = None, rate_limiter: RateLimiter = None):
        self.vault = vault
        self.replay_cache = replay_cache or InMemoryReplayCache()
        self.rate_limiter = rate_limiter or InMemoryRateLimiter()
        self.policies = {} # tenant_id:agent_id -> list of allowed actions/patterns

    def grant_policy(self, tenant_id: str, agent_id: str, actions: list[str]):
        """Grant a list of actions to an agent (AWS IAM style)."""
        key = f"{tenant_id}:{agent_id}"
        if key not in self.policies:
            self.policies[key] = []
        self.policies[key].extend(actions)

    def is_action_allowed(self, tenant_id: str, agent_id: str, action: str) -> bool:
        """Evaluate if an agent is authorized to perform a specific action."""
        key = f"{tenant_id}:{agent_id}"
        allowed_actions = self.policies.get(key, [])
        for allowed in allowed_actions:
            if allowed == "*":
                return True
            if allowed.endswith("*"):
                prefix = allowed[:-1]
                if action.startswith(prefix):
                    return True
            if action == allowed:
                return True
        return False

    def verify_request(self, tenant_id: str, agent_id: str, signature: str, timestamp: int, method: str, path: str, body: str, action: str = None, key_version: str = None) -> bool:
        """
        Verify that a request was signed by the agent, is not a replay,
        and optionally verify if the agent is authorized for the action.
        Supports Key Versioning (kid) for rotation and instant revocation.
        """
        # 0. Rate Limiting Check
        if not self.rate_limiter.check_rate_limit(tenant_id, agent_id):
            raise Unauthorized("Rate limit exceeded for agent")

        if not signature:
            raise Unauthorized("Missing signature")
            
        now = int(time.time())
        # 1. Validate Timestamp Window (Asymmetric Clock Skew)
        if timestamp > now + 5:
            raise Unauthorized("Future timestamp attack detected")
        if now - timestamp > 30:
            raise Unauthorized("Replay window exceeded")

        keys = self.vault.get_signing_keys(tenant_id, agent_id)
        valid = False
        
        # Payload for signature
        payload = f"{tenant_id}.{timestamp}.{method}.{path}.{body}".encode('utf-8')
        
        # 2. Strict Key Version Lookup (kid)
        if key_version:
            if key_version not in keys:
                raise Unauthorized("Unknown key version")
            keys_to_check = [keys[key_version]]
        else:
            if not keys:
                # Fallback for legacy systems without versioned keys
                secret = self.vault.get_secret(tenant_id, agent_id, "signing_key")
                if not secret:
                    raise Unauthorized("Agent signing keys not found")
                expected = hmac.new(secret.encode('utf-8'), payload, hashlib.sha256).hexdigest()
                if hmac.compare_digest(expected, signature):
                    valid = True
                keys_to_check = []
            else:
                keys_to_check = keys.values()
        
        # 3. Validate Signature
        for key_data in keys_to_check:
            if key_data["status"] != "active":
                continue
            
            # Rollback Protection (Max key age 90 days)
            if now - key_data["created_at"] > (90 * 24 * 3600):
                continue
                
            expected = hmac.new(
                key_data["secret"].encode('utf-8'),
                payload,
                hashlib.sha256
            ).hexdigest()
            
            if hmac.compare_digest(expected, signature):
                valid = True
                break
                
        if not valid:
            raise Unauthorized("Invalid signature or key revoked")
            
        # 4. Replay Protection (Redis) AFTER Signature verification to prevent DoS
        replay_key = f"kernell:iam:replay:{tenant_id}:{agent_id}:{signature}"
        is_new = self.replay_cache.set_if_not_exists(replay_key, ttl=60)
        if not is_new:
            raise Unauthorized("Replay attack detected")

        # 5. Policy Check
        if action and not self.is_action_allowed(tenant_id, agent_id, action):
            raise Unauthorized(f"IAM Policy Deny: Agent {agent_id} in Tenant {tenant_id} is not authorized for {action}")

        return True
