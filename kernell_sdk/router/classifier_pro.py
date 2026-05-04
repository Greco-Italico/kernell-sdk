"""
Kernell OS SDK — Classifier-Pro API Client
════════════════════════════════════════════
When the local classifier (Classifier-Lite) has low confidence,
this client escalates to the cloud-hosted Classifier-Pro for a
superior routing decision.

Architecture:
  SDK (local)                     Kernell Cloud
  ┌──────────────┐               ┌──────────────────┐
  │ Classifier   │  confidence   │ Classifier-Pro   │
  │ Lite (Qwen)  │──< 0.70? ──→ │ (fine-tuned on   │
  │ Local, free  │               │  fleet data)     │
  │ 70-80% acc   │               │  95%+ accuracy   │
  └──────────────┘               └──────────────────┘
         ↓                               ↓
    Use local decision            Use pro decision
    (free, fast)                  (paid, precise)

The Pro model is NEVER distributed. It lives as an API.
The local model IS distributed — it's the free tier.
This separation is the core of the business model.
"""
from __future__ import annotations

import hashlib
import json
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger("kernell.router.classifier_pro")


@dataclass
class ClassifierProConfig:
    """Configuration for the Classifier-Pro API."""
    enabled: bool = False
    endpoint: str = "https://api.kernellos.com/v1/classify"
    api_key: str = ""
    
    # When to escalate to Pro
    confidence_threshold: float = 0.70    # Below this → ask Pro
    difficulty_threshold: int = 4          # Difficulty >= this → ask Pro
    cost_threshold_usd: float = 0.10      # Estimated cost > this → ask Pro
    repeated_failure_count: int = 2        # N failures on same domain → ask Pro
    
    # Rate limiting (don't hammer the API)
    max_requests_per_minute: int = 30
    max_requests_per_hour: int = 500
    
    # Caching Pro responses locally
    cache_pro_decisions: bool = True
    cache_ttl_seconds: float = 3600.0     # 1 hour
    
    # Fallback
    fallback_to_local: bool = True        # If Pro API fails, use local decision
    timeout_seconds: float = 5.0


@dataclass
class ProClassification:
    """Response from Classifier-Pro."""
    subtasks: List[dict]           # Decomposed subtasks with optimal tiers
    confidence: float              # Pro model confidence (typically 0.90+)
    estimated_savings_pct: float   # How much cheaper vs premium-only
    recommended_models: List[str]  # Specific model recommendations
    source: str = "pro"            # "pro" or "lite_fallback"
    latency_ms: float = 0.0
    cached: bool = False


class ClassifierProClient:
    """
    Client for the Kernell Cloud Classifier-Pro API.
    
    Decision logic (called by IntelligentRouter):
    
      1. Local classifier produces a routing decision
      2. If confidence < threshold OR difficulty >= 4 OR cost > limit:
         → Consult Classifier-Pro for a better decision
      3. If Pro is unavailable → fallback to local (always safe)
    
    Pricing model for Pro:
      Based on % of savings achieved, NOT on token count.
      Example: If Pro saves user $0.50 on a task, Kernell takes 10% = $0.05.
      This aligns incentives — Kernell only earns when the user saves money.
    """

    def __init__(self, config: Optional[ClassifierProConfig] = None):
        self._config = config or ClassifierProConfig()
        
        # Rate limiting state
        self._minute_requests: List[float] = []
        self._hour_requests: List[float] = []
        
        # Local cache for Pro decisions
        self._cache: Dict[str, Tuple[ProClassification, float]] = {}
        self._max_cache = 1000
        
        # Stats
        self._total_requests = 0
        self._cache_hits = 0
        self._api_calls = 0
        self._api_errors = 0
        self._fallbacks = 0
        
        # Failure tracking per domain
        self._domain_failures: Dict[str, int] = {}

    def should_consult_pro(self, confidence: float, difficulty: int,
                            estimated_cost: float = 0.0,
                            domain: str = "general") -> bool:
        """
        Determine if we should escalate to Classifier-Pro.
        
        Returns True if ANY escalation condition is met.
        """
        if not self._config.enabled or not self._config.api_key:
            return False
        
        reasons = []
        
        if confidence < self._config.confidence_threshold:
            reasons.append(f"low_confidence({confidence:.2f})")
        
        if difficulty >= self._config.difficulty_threshold:
            reasons.append(f"high_difficulty({difficulty})")
        
        if estimated_cost > self._config.cost_threshold_usd:
            reasons.append(f"high_cost(${estimated_cost:.4f})")
        
        domain_fails = self._domain_failures.get(domain, 0)
        if domain_fails >= self._config.repeated_failure_count:
            reasons.append(f"domain_failures({domain}={domain_fails})")
        
        if reasons:
            logger.debug(f"Classifier-Pro escalation: {', '.join(reasons)}")
            return True
        
        return False

    def classify(self, task: str, local_subtasks: List[dict],
                  hardware_tier: str = "",
                  ram_gb: int = 0,
                  has_gpu: bool = False) -> ProClassification:
        """
        Consult Classifier-Pro for an optimized routing decision.
        
        Args:
            task: The original task description
            local_subtasks: What the local classifier produced
            hardware_tier: User's hardware tier for context
            ram_gb: Available RAM
            has_gpu: Whether GPU is available
            
        Returns:
            ProClassification with optimized routing decisions
        """
        self._total_requests += 1
        
        # Check rate limits
        if not self._check_rate_limit():
            logger.debug("Classifier-Pro rate limited, using local")
            return self._fallback_local(local_subtasks)
        
        # Check cache
        cache_key = self._cache_key(task, hardware_tier)
        if self._config.cache_pro_decisions:
            cached = self._check_cache(cache_key)
            if cached:
                self._cache_hits += 1
                return cached
        
        # Call the API
        try:
            result = self._call_api(task, local_subtasks, hardware_tier, ram_gb, has_gpu)
            
            # Cache the result
            if self._config.cache_pro_decisions:
                self._cache[cache_key] = (result, time.time())
                if len(self._cache) > self._max_cache:
                    # Evict oldest
                    oldest = min(self._cache, key=lambda k: self._cache[k][1])
                    del self._cache[oldest]
            
            self._api_calls += 1
            return result
            
        except Exception as e:
            self._api_errors += 1
            logger.warning(f"Classifier-Pro API error: {e}")
            
            if self._config.fallback_to_local:
                self._fallbacks += 1
                return self._fallback_local(local_subtasks)
            raise

    def record_domain_failure(self, domain: str) -> None:
        """Track failures by domain for escalation logic."""
        self._domain_failures[domain] = self._domain_failures.get(domain, 0) + 1

    def reset_domain_failures(self, domain: str) -> None:
        """Reset failure counter after success."""
        self._domain_failures.pop(domain, None)

    def get_stats(self) -> dict:
        """Return client statistics."""
        return {
            "enabled": self._config.enabled,
            "total_requests": self._total_requests,
            "cache_hits": self._cache_hits,
            "api_calls": self._api_calls,
            "api_errors": self._api_errors,
            "fallbacks": self._fallbacks,
            "cache_size": len(self._cache),
            "domain_failures": dict(self._domain_failures),
            "cache_hit_rate": (
                f"{(self._cache_hits / self._total_requests * 100):.1f}%"
                if self._total_requests > 0 else "0%"
            ),
        }

    # ── Private Methods ──────────────────────────────────────────────

    def _call_api(self, task: str, local_subtasks: List[dict],
                   hardware_tier: str, ram_gb: int,
                   has_gpu: bool) -> ProClassification:
        """Make the actual API call to Classifier-Pro."""
        import httpx
        
        # Anonymize: send task hash, not content
        task_hash = hashlib.sha256(task.encode()).hexdigest()[:16]
        
        payload = {
            "task_hash": task_hash,
            "task_token_count": len(task.split()),
            "local_decomposition": [
                {
                    "difficulty": s.get("difficulty", 3),
                    "domain": s.get("domain", "general"),
                    "confidence": s.get("confidence", 0.5),
                }
                for s in local_subtasks
            ],
            "hardware": {
                "tier": hardware_tier,
                "ram_gb": ram_gb,
                "has_gpu": has_gpu,
            },
        }
        
        t0 = time.monotonic()
        resp = httpx.post(
            self._config.endpoint,
            json=payload,
            headers={
                "Authorization": f"Bearer {self._config.api_key}",
                "Content-Type": "application/json",
            },
            timeout=self._config.timeout_seconds,
        )
        resp.raise_for_status()
        latency = (time.monotonic() - t0) * 1000
        
        data = resp.json()
        
        return ProClassification(
            subtasks=data.get("subtasks", local_subtasks),
            confidence=data.get("confidence", 0.95),
            estimated_savings_pct=data.get("estimated_savings_pct", 0.0),
            recommended_models=data.get("recommended_models", []),
            source="pro",
            latency_ms=latency,
        )

    def _fallback_local(self, local_subtasks: List[dict]) -> ProClassification:
        """Fallback to local classification when Pro is unavailable."""
        return ProClassification(
            subtasks=local_subtasks,
            confidence=0.5,
            estimated_savings_pct=0.0,
            recommended_models=[],
            source="lite_fallback",
        )

    def _check_rate_limit(self) -> bool:
        """Check if we're within rate limits."""
        now = time.time()
        
        # Clean old entries
        self._minute_requests = [t for t in self._minute_requests if now - t < 60]
        self._hour_requests = [t for t in self._hour_requests if now - t < 3600]
        
        if len(self._minute_requests) >= self._config.max_requests_per_minute:
            return False
        if len(self._hour_requests) >= self._config.max_requests_per_hour:
            return False
        
        self._minute_requests.append(now)
        self._hour_requests.append(now)
        return True

    def _check_cache(self, key: str) -> Optional[ProClassification]:
        """Check if we have a cached Pro decision."""
        if key in self._cache:
            result, timestamp = self._cache[key]
            if time.time() - timestamp < self._config.cache_ttl_seconds:
                cached_result = ProClassification(
                    subtasks=result.subtasks,
                    confidence=result.confidence,
                    estimated_savings_pct=result.estimated_savings_pct,
                    recommended_models=result.recommended_models,
                    source="pro",
                    latency_ms=0.0,
                    cached=True,
                )
                return cached_result
            else:
                del self._cache[key]
        return None

    @staticmethod
    def _cache_key(task: str, hardware_tier: str) -> str:
        """Generate a cache key for a task + hardware combo."""
        return hashlib.sha256(
            f"{task}:{hardware_tier}".encode()
        ).hexdigest()[:16]
