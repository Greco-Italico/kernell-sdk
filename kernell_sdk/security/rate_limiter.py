"""
Kernell OS SDK — Multi-Layer Rate Limiter & Circuit Breaker
════════════════════════════════════════════════════════════
Defense-in-depth throttling to prevent:
  - Recursion storms (LLM loops)
  - Webhook floods
  - Escrow spam
  - Skill abuse
  - Sub-agent explosion
  - CPU / memory / Redis / budget exhaustion

Mathematical model (Sliding Window):
    R(t) = Σ 𝟙(t - tᵢ < W) ≤ L
    where W = window size, L = limit, tᵢ = event timestamps

Circuit Breaker states:
    CLOSED  → normal operation
    OPEN    → all requests rejected (cooldown active)
    HALF_OPEN → probe: allow 1 request to test recovery
"""
import time
import threading
import logging
from enum import Enum
from typing import Optional, Dict, List, Tuple, Any
from dataclasses import dataclass, field
from collections import deque

logger = logging.getLogger("kernell.security.rate_limiter")


# ─── Sliding Window Rate Limiter ────────────────────────────────────

class RateLimitExceeded(Exception):
    """Raised when a rate limit is breached."""
    def __init__(self, key: str, dimension: str, current: int, limit: int, window: float):
        self.key = key
        self.dimension = dimension
        self.current = current
        self.limit = limit
        self.window = window
        super().__init__(
            f"Rate limit exceeded: {dimension}:{key} "
            f"({current}/{limit} in {window}s window)"
        )


@dataclass
class QuotaConfig:
    """Configures a single quota dimension."""
    limit: int              # Max events allowed in the window
    window_seconds: float   # Sliding window size
    description: str = ""   # Human-readable label


@dataclass
class QuotaState:
    """Live state for one quota key."""
    timestamps: deque = field(default_factory=deque)
    lock: threading.Lock = field(default_factory=threading.Lock)

    def count_in_window(self, now: float, window: float) -> int:
        """Purge expired timestamps and return count within window."""
        cutoff = now - window
        while self.timestamps and self.timestamps[0] < cutoff:
            self.timestamps.popleft()
        return len(self.timestamps)

    def record(self, now: float):
        self.timestamps.append(now)


class SlidingWindowLimiter:
    """
    Thread-safe sliding window rate limiter.
    Supports multiple named quota dimensions per key.

    Usage:
        limiter = SlidingWindowLimiter()
        limiter.add_quota("agent_calls", QuotaConfig(limit=100, window_seconds=60))
        limiter.check_and_record("agent_calls", "agent_007")
    """

    def __init__(self):
        self._quotas: Dict[str, QuotaConfig] = {}
        self._states: Dict[str, Dict[str, QuotaState]] = {}
        self._global_lock = threading.Lock()

    def add_quota(self, dimension: str, config: QuotaConfig):
        """Register a quota dimension."""
        self._quotas[dimension] = config
        self._states[dimension] = {}
        logger.info(
            "quota_registered",
            extra={"dimension": dimension, "limit": config.limit,
                   "window": config.window_seconds, "desc": config.description}
        )

    def _get_state(self, dimension: str, key: str) -> QuotaState:
        """Get or create state for a dimension:key pair."""
        states = self._states.get(dimension)
        if states is None:
            raise ValueError(f"Unknown quota dimension: {dimension}")
        if key not in states:
            with self._global_lock:
                if key not in states:
                    states[key] = QuotaState()
        return states[key]

    def check(self, dimension: str, key: str) -> Tuple[bool, int, int]:
        """Check if a request is within quota. Returns (allowed, current, limit)."""
        config = self._quotas.get(dimension)
        if not config:
            return (True, 0, 0)
        state = self._get_state(dimension, key)
        now = time.time()
        with state.lock:
            current = state.count_in_window(now, config.window_seconds)
            return (current < config.limit, current, config.limit)

    def check_and_record(self, dimension: str, key: str) -> bool:
        """
        Atomically check quota and record the event if allowed.
        Raises RateLimitExceeded if the limit is breached.
        """
        config = self._quotas.get(dimension)
        if not config:
            return True  # No quota configured = allow

        state = self._get_state(dimension, key)
        now = time.time()

        with state.lock:
            current = state.count_in_window(now, config.window_seconds)
            if current >= config.limit:
                logger.warning(
                    "rate_limit_exceeded",
                    extra={"dimension": dimension, "key": key,
                           "current": current, "limit": config.limit}
                )
                raise RateLimitExceeded(key, dimension, current, config.limit, config.window_seconds)
            state.record(now)
            return True

    def record_only(self, dimension: str, key: str):
        """Record an event without checking (for tracking purposes)."""
        config = self._quotas.get(dimension)
        if not config:
            return
        state = self._get_state(dimension, key)
        now = time.time()
        with state.lock:
            state.count_in_window(now, config.window_seconds)  # purge
            state.record(now)

    def get_usage(self, dimension: str, key: str) -> Dict[str, Any]:
        """Get current usage stats for a dimension:key."""
        config = self._quotas.get(dimension)
        if not config:
            return {}
        state = self._get_state(dimension, key)
        now = time.time()
        with state.lock:
            current = state.count_in_window(now, config.window_seconds)
            return {
                "dimension": dimension,
                "key": key,
                "current": current,
                "limit": config.limit,
                "window_seconds": config.window_seconds,
                "utilization_pct": round((current / max(config.limit, 1)) * 100, 1),
                "remaining": max(0, config.limit - current),
            }

    def reset(self, dimension: str, key: str):
        """Reset a specific key's state (admin override)."""
        states = self._states.get(dimension)
        if states and key in states:
            with states[key].lock:
                states[key].timestamps.clear()
            logger.info("quota_reset", extra={"dimension": dimension, "key": key})


# ─── Circuit Breaker ────────────────────────────────────────────────

class CircuitState(Enum):
    CLOSED = "CLOSED"       # Normal — all requests pass
    OPEN = "OPEN"           # Tripped — all requests blocked
    HALF_OPEN = "HALF_OPEN" # Probing — allow one request to test


@dataclass
class CircuitBreakerConfig:
    """Configuration for a circuit breaker."""
    failure_threshold: int = 5         # Failures before tripping
    recovery_timeout: float = 60.0     # Seconds to wait before probing
    half_open_max_calls: int = 1       # Calls allowed in HALF_OPEN
    success_threshold: int = 2         # Successes needed to close from HALF_OPEN
    window_seconds: float = 120.0      # Window for counting failures


class CircuitBreaker:
    """
    Per-key circuit breaker with automatic recovery.

    States:
        CLOSED → OPEN (after failure_threshold failures in window)
        OPEN → HALF_OPEN (after recovery_timeout)
        HALF_OPEN → CLOSED (after success_threshold successes)
        HALF_OPEN → OPEN (on any failure)

    Usage:
        cb = CircuitBreaker("llm_calls", CircuitBreakerConfig(failure_threshold=5))
        if cb.allow_request():
            try:
                result = call_llm()
                cb.record_success()
            except Exception:
                cb.record_failure("timeout")
    """

    def __init__(self, name: str, config: Optional[CircuitBreakerConfig] = None):
        self.name = name
        self.config = config or CircuitBreakerConfig()
        self._state = CircuitState.CLOSED
        self._failures: deque = deque()  # (timestamp, reason)
        self._successes_in_half_open: int = 0
        self._calls_in_half_open: int = 0
        self._opened_at: float = 0.0
        self._lock = threading.Lock()

    @property
    def state(self) -> CircuitState:
        with self._lock:
            return self._evaluate_state()

    def _evaluate_state(self) -> CircuitState:
        """Re-evaluate state based on timers (must hold lock)."""
        if self._state == CircuitState.OPEN:
            elapsed = time.time() - self._opened_at
            if elapsed >= self.config.recovery_timeout:
                self._state = CircuitState.HALF_OPEN
                self._successes_in_half_open = 0
                self._calls_in_half_open = 0
                logger.info(
                    "circuit_half_open",
                    extra={"breaker": self.name, "elapsed": round(elapsed, 1)}
                )
        return self._state

    def allow_request(self) -> bool:
        """Check if a request should be allowed through."""
        with self._lock:
            state = self._evaluate_state()
            if state == CircuitState.CLOSED:
                return True
            elif state == CircuitState.OPEN:
                return False
            else:  # HALF_OPEN
                if self._calls_in_half_open < self.config.half_open_max_calls:
                    self._calls_in_half_open += 1
                    return True
                return False

    def record_success(self):
        """Record a successful operation."""
        with self._lock:
            if self._state == CircuitState.HALF_OPEN:
                self._successes_in_half_open += 1
                if self._successes_in_half_open >= self.config.success_threshold:
                    self._state = CircuitState.CLOSED
                    self._failures.clear()
                    logger.info("circuit_closed", extra={"breaker": self.name})

    def record_failure(self, reason: str = ""):
        """Record a failed operation."""
        now = time.time()
        with self._lock:
            if self._state == CircuitState.HALF_OPEN:
                # Any failure in HALF_OPEN reopens immediately
                self._state = CircuitState.OPEN
                self._opened_at = now
                logger.warning(
                    "circuit_reopened",
                    extra={"breaker": self.name, "reason": reason}
                )
                return

            # Purge old failures outside window
            cutoff = now - self.config.window_seconds
            while self._failures and self._failures[0][0] < cutoff:
                self._failures.popleft()

            self._failures.append((now, reason))

            if len(self._failures) >= self.config.failure_threshold:
                self._state = CircuitState.OPEN
                self._opened_at = now
                logger.warning(
                    "circuit_opened",
                    extra={"breaker": self.name, "failures": len(self._failures),
                           "threshold": self.config.failure_threshold, "reason": reason}
                )

    def force_open(self):
        """Manually trip the breaker (admin override)."""
        with self._lock:
            self._state = CircuitState.OPEN
            self._opened_at = time.time()
            logger.warning("circuit_force_opened", extra={"breaker": self.name})

    def force_close(self):
        """Manually close the breaker (admin override)."""
        with self._lock:
            self._state = CircuitState.CLOSED
            self._failures.clear()
            logger.info("circuit_force_closed", extra={"breaker": self.name})

    def snapshot(self) -> Dict[str, Any]:
        """Get current breaker state."""
        with self._lock:
            state = self._evaluate_state()
            return {
                "name": self.name,
                "state": state.value,
                "recent_failures": len(self._failures),
                "failure_threshold": self.config.failure_threshold,
                "recovery_timeout": self.config.recovery_timeout,
                "opened_at": self._opened_at if self._state != CircuitState.CLOSED else None,
            }


# ─── Delegation Tree Budget ────────────────────────────────────────

@dataclass
class DelegationNode:
    """Tracks budget consumption in a delegation tree."""
    agent_id: str
    parent_id: Optional[str]
    depth: int
    budget_allocated: float      # Tokens allocated from parent
    budget_consumed: float = 0.0  # Tokens actually used
    children_count: int = 0
    max_children: int = 5
    created_at: float = field(default_factory=time.time)


class DelegationBudgetTracker:
    """
    Tracks and enforces cumulative budget across the delegation tree.

    Key invariant:
        B_remaining = B_root - Σ cᵢ
        Each sub-agent CANNOT reinvent its own budget.

    Also enforces:
        - Max tree depth
        - Max children per node
        - Max total agents in tree
    """

    def __init__(
        self,
        root_budget: float = 200_000,
        max_depth: int = 4,
        max_children_per_node: int = 5,
        max_total_agents: int = 50,
        budget_decay_factor: float = 0.5,  # Each level gets 50% of parent's remaining
    ):
        self.root_budget = root_budget
        self.max_depth = max_depth
        self.max_children_per_node = max_children_per_node
        self.max_total_agents = max_total_agents
        self.budget_decay_factor = budget_decay_factor

        self._nodes: Dict[str, DelegationNode] = {}
        self._lock = threading.Lock()
        self._total_consumed: float = 0.0

    def register_root(self, agent_id: str) -> DelegationNode:
        """Register the root agent of a delegation tree."""
        with self._lock:
            node = DelegationNode(
                agent_id=agent_id,
                parent_id=None,
                depth=0,
                budget_allocated=self.root_budget,
            )
            self._nodes[agent_id] = node
            return node

    def spawn_child(self, parent_id: str, child_id: str) -> DelegationNode:
        """
        Spawn a child agent from a parent. Budget is carved from parent's remaining.
        Raises if limits are exceeded.
        """
        with self._lock:
            parent = self._nodes.get(parent_id)
            if not parent:
                raise ValueError(f"Unknown parent agent: {parent_id}")

            # Depth check
            child_depth = parent.depth + 1
            if child_depth > self.max_depth:
                raise RateLimitExceeded(
                    parent_id, "delegation_depth",
                    child_depth, self.max_depth, 0
                )

            # Children count check
            if parent.children_count >= parent.max_children:
                raise RateLimitExceeded(
                    parent_id, "delegation_children",
                    parent.children_count, parent.max_children, 0
                )

            # Total agents check
            if len(self._nodes) >= self.max_total_agents:
                raise RateLimitExceeded(
                    parent_id, "delegation_total_agents",
                    len(self._nodes), self.max_total_agents, 0
                )

            # Budget allocation: child gets decay_factor * parent's remaining
            parent_remaining = parent.budget_allocated - parent.budget_consumed
            child_budget = parent_remaining * self.budget_decay_factor

            if child_budget < 100:  # Minimum viable budget
                raise RateLimitExceeded(
                    parent_id, "delegation_budget_exhausted",
                    int(child_budget), 100, 0
                )

            # Deduct from parent
            parent.budget_consumed += child_budget
            parent.children_count += 1

            child = DelegationNode(
                agent_id=child_id,
                parent_id=parent_id,
                depth=child_depth,
                budget_allocated=child_budget,
                max_children=max(1, self.max_children_per_node - child_depth),
            )
            self._nodes[child_id] = child

            logger.info(
                "delegation_child_spawned",
                extra={
                    "parent": parent_id, "child": child_id,
                    "depth": child_depth, "budget": round(child_budget, 0),
                    "total_agents": len(self._nodes),
                }
            )
            return child

    def consume_budget(self, agent_id: str, tokens: float) -> bool:
        """
        Record token consumption for an agent.
        Returns False if budget is exhausted.
        """
        with self._lock:
            node = self._nodes.get(agent_id)
            if not node:
                return False

            if node.budget_consumed + tokens > node.budget_allocated:
                logger.warning(
                    "delegation_budget_exceeded",
                    extra={
                        "agent": agent_id,
                        "consumed": node.budget_consumed,
                        "allocated": node.budget_allocated,
                        "requested": tokens,
                    }
                )
                return False

            node.budget_consumed += tokens
            self._total_consumed += tokens
            return True

    def get_remaining(self, agent_id: str) -> float:
        """Get remaining budget for an agent."""
        with self._lock:
            node = self._nodes.get(agent_id)
            if not node:
                return 0.0
            return max(0.0, node.budget_allocated - node.budget_consumed)

    def tree_snapshot(self) -> Dict[str, Any]:
        """Get a full snapshot of the delegation tree."""
        with self._lock:
            nodes = {}
            for aid, node in self._nodes.items():
                nodes[aid] = {
                    "parent": node.parent_id,
                    "depth": node.depth,
                    "budget_allocated": round(node.budget_allocated, 0),
                    "budget_consumed": round(node.budget_consumed, 0),
                    "budget_remaining": round(node.budget_allocated - node.budget_consumed, 0),
                    "children": node.children_count,
                }
            return {
                "root_budget": self.root_budget,
                "total_consumed": round(self._total_consumed, 0),
                "total_remaining": round(self.root_budget - self._total_consumed, 0),
                "total_agents": len(self._nodes),
                "max_depth": self.max_depth,
                "nodes": nodes,
            }


# ─── Unified Rate Limiting Governor ────────────────────────────────

class RateLimitGovernor:
    """
    Central governor that owns all rate limiters and circuit breakers
    for the entire SDK. Singleton per process.

    Dimensions enforced:
        - agent_calls:       Max LLM calls per agent per window
        - agent_skills:      Max skill invocations per agent
        - skill_calls:       Max calls per specific skill
        - endpoint_calls:    Max calls to a specific HTTP endpoint
        - escrow_create:     Max escrow creations per wallet
        - webhook_dispatch:  Max webhook dispatches per agent
        - delegation_spawn:  Max sub-agent spawns per agent
        - global_calls:      Global max across entire system

    Circuit breakers:
        - llm_timeouts:      Trip on excessive LLM timeouts
        - ssrf_blocked:      Trip on excessive SSRF violations
        - signature_invalid: Trip on excessive invalid signatures
        - sandbox_errors:    Trip on excessive sandbox failures
    """

    _instance = None
    _init_lock = threading.Lock()

    def __new__(cls, *args, **kwargs):
        if not cls._instance:
            with cls._init_lock:
                if not cls._instance:
                    cls._instance = super().__new__(cls)
        return cls._instance

    def __init__(self):
        if hasattr(self, "_initialized"):
            return
        self._initialized = True

        # ── Sliding Window Quotas ──
        self.limiter = SlidingWindowLimiter()
        self._setup_default_quotas()

        # ── Circuit Breakers ──
        self.breakers: Dict[str, CircuitBreaker] = {}
        self._setup_default_breakers()

        # ── Delegation Budget ──
        self.delegation = DelegationBudgetTracker()

        logger.info("rate_limit_governor_initialized")

    def _setup_default_quotas(self):
        """Install production-safe default quotas."""
        defaults = {
            "agent_calls": QuotaConfig(
                limit=200, window_seconds=60,
                description="Max LLM calls per agent per minute"
            ),
            "agent_calls_hourly": QuotaConfig(
                limit=2000, window_seconds=3600,
                description="Max LLM calls per agent per hour"
            ),
            "agent_skills": QuotaConfig(
                limit=300, window_seconds=60,
                description="Max skill invocations per agent per minute"
            ),
            "skill_calls": QuotaConfig(
                limit=100, window_seconds=60,
                description="Max calls to a specific skill per minute"
            ),
            "endpoint_calls": QuotaConfig(
                limit=50, window_seconds=60,
                description="Max HTTP calls to a specific endpoint per minute"
            ),
            "escrow_create": QuotaConfig(
                limit=10, window_seconds=60,
                description="Max escrow creations per wallet per minute"
            ),
            "escrow_create_hourly": QuotaConfig(
                limit=100, window_seconds=3600,
                description="Max escrow creations per wallet per hour"
            ),
            "webhook_dispatch": QuotaConfig(
                limit=60, window_seconds=60,
                description="Max webhook dispatches per agent per minute"
            ),
            "delegation_spawn": QuotaConfig(
                limit=10, window_seconds=60,
                description="Max sub-agent spawns per agent per minute"
            ),
            "delegation_spawn_hourly": QuotaConfig(
                limit=50, window_seconds=3600,
                description="Max sub-agent spawns per agent per hour"
            ),
            "global_calls": QuotaConfig(
                limit=5000, window_seconds=60,
                description="Global max calls across entire system per minute"
            ),
        }
        for dimension, config in defaults.items():
            self.limiter.add_quota(dimension, config)

    def _setup_default_breakers(self):
        """Install default circuit breakers."""
        breaker_configs = {
            "llm_timeouts": CircuitBreakerConfig(
                failure_threshold=10, recovery_timeout=120, window_seconds=300
            ),
            "ssrf_blocked": CircuitBreakerConfig(
                failure_threshold=5, recovery_timeout=300, window_seconds=600
            ),
            "signature_invalid": CircuitBreakerConfig(
                failure_threshold=3, recovery_timeout=600, window_seconds=300
            ),
            "sandbox_errors": CircuitBreakerConfig(
                failure_threshold=5, recovery_timeout=60, window_seconds=120
            ),
            "escrow_failures": CircuitBreakerConfig(
                failure_threshold=5, recovery_timeout=120, window_seconds=300
            ),
            "delegation_failures": CircuitBreakerConfig(
                failure_threshold=3, recovery_timeout=180, window_seconds=120
            ),
        }
        for name, config in breaker_configs.items():
            self.breakers[name] = CircuitBreaker(name, config)

    # ── Convenience Methods ──

    def check_agent_call(self, agent_id: str):
        """Gate an LLM call for an agent. Raises RateLimitExceeded if blocked."""
        # Check circuit breaker first
        if not self.breakers["llm_timeouts"].allow_request():
            raise RateLimitExceeded(agent_id, "circuit:llm_timeouts", 0, 0, 0)

        self.limiter.check_and_record("agent_calls", agent_id)
        self.limiter.check_and_record("agent_calls_hourly", agent_id)
        self.limiter.check_and_record("global_calls", "__global__")

    def check_skill_call(self, agent_id: str, skill_name: str):
        """Gate a skill invocation."""
        self.limiter.check_and_record("agent_skills", agent_id)
        self.limiter.check_and_record("skill_calls", f"{agent_id}:{skill_name}")

    def check_endpoint_call(self, agent_id: str, endpoint: str):
        """Gate an HTTP endpoint call."""
        self.limiter.check_and_record("endpoint_calls", f"{agent_id}:{endpoint}")

    def check_escrow_create(self, wallet_address: str):
        """Gate escrow creation."""
        if not self.breakers["escrow_failures"].allow_request():
            raise RateLimitExceeded(wallet_address, "circuit:escrow_failures", 0, 0, 0)
        self.limiter.check_and_record("escrow_create", wallet_address)
        self.limiter.check_and_record("escrow_create_hourly", wallet_address)

    def check_webhook(self, agent_id: str):
        """Gate webhook dispatch."""
        self.limiter.check_and_record("webhook_dispatch", agent_id)

    def check_delegation_spawn(self, parent_agent_id: str):
        """Gate sub-agent spawning."""
        if not self.breakers["delegation_failures"].allow_request():
            raise RateLimitExceeded(
                parent_agent_id, "circuit:delegation_failures", 0, 0, 0
            )
        self.limiter.check_and_record("delegation_spawn", parent_agent_id)
        self.limiter.check_and_record("delegation_spawn_hourly", parent_agent_id)

    def report_llm_timeout(self, agent_id: str):
        self.breakers["llm_timeouts"].record_failure(f"agent:{agent_id}")

    def report_llm_success(self):
        self.breakers["llm_timeouts"].record_success()

    def report_ssrf_violation(self, agent_id: str, target: str):
        self.breakers["ssrf_blocked"].record_failure(f"{agent_id}->{target}")

    def report_signature_failure(self, agent_id: str):
        self.breakers["signature_invalid"].record_failure(f"agent:{agent_id}")

    def report_sandbox_error(self, agent_id: str, error: str):
        self.breakers["sandbox_errors"].record_failure(f"{agent_id}:{error[:50]}")

    def report_escrow_failure(self, wallet: str):
        self.breakers["escrow_failures"].record_failure(f"wallet:{wallet}")

    def report_delegation_failure(self, parent_id: str, reason: str):
        self.breakers["delegation_failures"].record_failure(f"{parent_id}:{reason}")

    def full_snapshot(self) -> Dict[str, Any]:
        """Get complete rate-limit and circuit breaker status."""
        return {
            "breakers": {
                name: cb.snapshot() for name, cb in self.breakers.items()
            },
            "delegation": self.delegation.tree_snapshot(),
        }

    @classmethod
    def reset_singleton(cls):
        """Reset the singleton (for testing)."""
        cls._instance = None
