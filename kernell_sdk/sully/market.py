"""
Kernell OS SDK — Model Market Registry (Pricing Oracle)
═══════════════════════════════════════════════════════
Real-time market data aggregator for all available LLM endpoints.
Provides Sully with live pricing, latency, rate-limit status, and quality scores.

This is the Oracle — Sully NEVER memorizes prices. It always asks here.

v3.2.0: Live providers hit actual APIs (Groq, OpenRouter, Ollama).
Graceful fallback to static data if API calls fail.
"""

import json
import logging
import os
import time
import urllib.request
import urllib.error
from typing import Dict, List, Optional

from kernell_sdk.sully.types import ModelMarketInfo, Tier

logger = logging.getLogger("kernell.sully.market")

# Timeout for market API calls — must be fast or we fall back
_MARKET_TIMEOUT_S = 5


class ModelMarketProvider:
    """Base class for market data providers."""
    
    def fetch_models(self) -> List[ModelMarketInfo]:
        raise NotImplementedError


# ══════════════════════════════════════════════════════════════════════
# GROQ — LIVE PROVIDER
# ══════════════════════════════════════════════════════════════════════

# Groq publishes pricing separately; the /models endpoint gives availability.
# This maps model IDs to known pricing (updated from https://groq.com/pricing/)
_GROQ_PRICING = {
    "llama-3.3-70b-versatile":       {"in": 0.00059,  "out": 0.00079,  "ctx": 131072, "reasoning": True},
    "llama-3.1-8b-instant":          {"in": 0.00005,  "out": 0.00008,  "ctx": 131072, "reasoning": False},
    "llama3-70b-8192":               {"in": 0.00059,  "out": 0.00079,  "ctx": 8192,   "reasoning": True},
    "llama3-8b-8192":                {"in": 0.00005,  "out": 0.00008,  "ctx": 8192,   "reasoning": False},
    "gemma2-9b-it":                  {"in": 0.00020,  "out": 0.00020,  "ctx": 8192,   "reasoning": False},
    "deepseek-r1-distill-llama-70b": {"in": 0.00075,  "out": 0.00099,  "ctx": 131072, "reasoning": True},
    "qwen/qwen3-32b":               {"in": 0.00,     "out": 0.00,     "ctx": 131072, "reasoning": True},
    "meta-llama/llama-4-scout-17b-16e-instruct": {"in": 0.00011, "out": 0.00034, "ctx": 131072, "reasoning": False},
}


class GroqLiveMarketProvider(ModelMarketProvider):
    """
    Fetches live model availability from Groq API.
    Cross-references with known pricing table.
    Falls back to static data on failure.
    """
    
    def __init__(self, api_key: str = ""):
        self._api_key = api_key or os.environ.get("GROQ_API_KEY", "")
    
    def fetch_models(self) -> List[ModelMarketInfo]:
        if not self._api_key:
            logger.debug("[Market:Groq] No API key, using static data")
            return self._static_fallback()
        
        try:
            return self._fetch_live()
        except Exception as e:
            logger.warning(f"[Market:Groq] Live fetch failed: {e}, using static fallback")
            return self._static_fallback()
    
    def _fetch_live(self) -> List[ModelMarketInfo]:
        """Hit Groq /v1/models and cross-reference with pricing table."""
        req = urllib.request.Request(
            "https://api.groq.com/openai/v1/models",
            headers={
                "Authorization": f"Bearer {self._api_key}",
                "User-Agent": "Kernell-OS-SDK/3.1",
            },
        )
        
        with urllib.request.urlopen(req, timeout=_MARKET_TIMEOUT_S) as resp:
            data = json.loads(resp.read().decode())
        
        models = []
        for item in data.get("data", []):
            model_id = item.get("id", "")
            pricing = _GROQ_PRICING.get(model_id, {})
            
            if not pricing:
                # Unknown model — skip or use defaults
                continue
            
            ctx = item.get("context_window", pricing.get("ctx", 8192))
            
            models.append(ModelMarketInfo(
                model_id=f"groq/{model_id}",
                provider="groq",
                input_cost_per_1k=pricing["in"],
                output_cost_per_1k=pricing["out"],
                context_limit=ctx,
                max_output_tokens=min(ctx, 8192),
                avg_latency_ms=300,     # will be updated from telemetry
                p95_latency_ms=800,
                supports_reasoning=pricing.get("reasoning", False),
                availability=1.0 if item.get("active", True) else 0.0,
                quality_score=0.75,     # will be updated from telemetry
            ))
        
        if models:
            logger.info(f"[Market:Groq] Live: {len(models)} models fetched")
        
        return models or self._static_fallback()
    
    def _static_fallback(self) -> List[ModelMarketInfo]:
        """Static data for when the API is unreachable."""
        return [
            ModelMarketInfo(
                model_id="groq/llama3-70b",
                provider="groq",
                input_cost_per_1k=0.00059,
                output_cost_per_1k=0.00079,
                context_limit=8192,
                max_output_tokens=8192,
                avg_latency_ms=300,
                p95_latency_ms=800,
                supports_reasoning=True,
                quality_score=0.75,
            ),
            ModelMarketInfo(
                model_id="groq/llama3-8b",
                provider="groq",
                input_cost_per_1k=0.00005,
                output_cost_per_1k=0.00008,
                context_limit=8192,
                max_output_tokens=8192,
                avg_latency_ms=150,
                p95_latency_ms=400,
                quality_score=0.60,
            ),
            ModelMarketInfo(
                model_id="groq/qwen3-32b",
                provider="groq",
                input_cost_per_1k=0.00,
                output_cost_per_1k=0.00,
                context_limit=131072,
                max_output_tokens=8192,
                avg_latency_ms=400,
                p95_latency_ms=1200,
                supports_reasoning=True,
                quality_score=0.80,
            ),
        ]


# ══════════════════════════════════════════════════════════════════════
# OPENROUTER — LIVE PROVIDER
# ══════════════════════════════════════════════════════════════════════

# Models we care about on OpenRouter (whitelist for cost control)
_OPENROUTER_WHITELIST = {
    "deepseek/deepseek-r1",
    "anthropic/claude-sonnet-4",
    "anthropic/claude-3.5-sonnet",
    "google/gemini-2.5-flash-preview",
    "google/gemini-2.5-pro-preview",
    "meta-llama/llama-3.3-70b-instruct",
    "qwen/qwen3-32b",
    "deepseek/deepseek-chat-v3-0324",
    "mistralai/mistral-small-3.2-24b-instruct",
}


class OpenRouterLiveMarketProvider(ModelMarketProvider):
    """
    Fetches live model data from OpenRouter API.
    OpenRouter is the best source: /models returns pricing directly.
    Falls back to static data on failure.
    """
    
    def __init__(self, api_key: str = ""):
        self._api_key = api_key or os.environ.get("OPENROUTER_API_KEY", "")
    
    def fetch_models(self) -> List[ModelMarketInfo]:
        try:
            return self._fetch_live()
        except Exception as e:
            logger.warning(f"[Market:OpenRouter] Live fetch failed: {e}, using static fallback")
            return self._static_fallback()
    
    def _fetch_live(self) -> List[ModelMarketInfo]:
        """Hit OpenRouter /v1/models — includes pricing in response."""
        headers = {"User-Agent": "Kernell-OS-SDK/3.1"}
        if self._api_key:
            headers["Authorization"] = f"Bearer {self._api_key}"
        
        req = urllib.request.Request(
            "https://openrouter.ai/api/v1/models",
            headers=headers,
        )
        
        with urllib.request.urlopen(req, timeout=_MARKET_TIMEOUT_S) as resp:
            data = json.loads(resp.read().decode())
        
        models = []
        for item in data.get("data", []):
            model_id = item.get("id", "")
            
            # Only include whitelisted models
            if model_id not in _OPENROUTER_WHITELIST:
                continue
            
            pricing = item.get("pricing", {})
            input_cost = float(pricing.get("prompt", "0")) * 1000   # per-token → per-1K
            output_cost = float(pricing.get("completion", "0")) * 1000
            ctx = item.get("context_length", 8192)
            top_provider = item.get("top_provider", {})
            
            # Detect vision support
            arch = item.get("architecture", {})
            modality = arch.get("modality", "")
            supports_vision = "image" in modality.lower() if modality else False
            
            models.append(ModelMarketInfo(
                model_id=f"openrouter/{model_id}",
                provider="openrouter",
                input_cost_per_1k=input_cost,
                output_cost_per_1k=output_cost,
                context_limit=ctx,
                max_output_tokens=item.get("top_provider", {}).get("max_completion_tokens", min(ctx, 8192)),
                avg_latency_ms=1500,     # will be updated from telemetry
                p95_latency_ms=4000,
                supports_reasoning="deepseek" in model_id or "gemini" in model_id,
                supports_vision=supports_vision,
                availability=1.0,
                quality_score=0.80,      # will be updated from telemetry
            ))
        
        if models:
            logger.info(f"[Market:OpenRouter] Live: {len(models)} models fetched")
        
        return models or self._static_fallback()
    
    def _static_fallback(self) -> List[ModelMarketInfo]:
        """Static data for when the API is unreachable."""
        return [
            ModelMarketInfo(
                model_id="openrouter/deepseek-r1",
                provider="openrouter",
                input_cost_per_1k=0.00055,
                output_cost_per_1k=0.0022,
                context_limit=65536,
                max_output_tokens=8192,
                avg_latency_ms=1500,
                p95_latency_ms=4000,
                supports_reasoning=True,
                quality_score=0.82,
            ),
            ModelMarketInfo(
                model_id="openrouter/claude-3-5-sonnet",
                provider="openrouter",
                input_cost_per_1k=0.003,
                output_cost_per_1k=0.015,
                context_limit=200000,
                max_output_tokens=8192,
                avg_latency_ms=2000,
                p95_latency_ms=5000,
                supports_reasoning=True,
                supports_vision=True,
                quality_score=0.95,
            ),
            ModelMarketInfo(
                model_id="openrouter/gemini-2.5-flash",
                provider="openrouter",
                input_cost_per_1k=0.0001,
                output_cost_per_1k=0.0004,
                context_limit=1000000,
                max_output_tokens=65536,
                avg_latency_ms=800,
                p95_latency_ms=2500,
                supports_reasoning=True,
                supports_vision=True,
                quality_score=0.85,
            ),
        ]


# ══════════════════════════════════════════════════════════════════════
# LOCAL — OLLAMA LIVE PROVIDER
# ══════════════════════════════════════════════════════════════════════

class OllamaLiveMarketProvider(ModelMarketProvider):
    """
    Detects locally running Ollama models.
    Hits http://localhost:11434/api/tags for model list.
    Falls back to static data if Ollama isn't running.
    """
    
    def __init__(self, host: str = "http://127.0.0.1:11434"):
        self._host = host
    
    def fetch_models(self) -> List[ModelMarketInfo]:
        try:
            return self._fetch_live()
        except Exception:
            return self._static_fallback()
    
    def _fetch_live(self) -> List[ModelMarketInfo]:
        """Hit Ollama /api/tags."""
        req = urllib.request.Request(
            f"{self._host}/api/tags",
            headers={"User-Agent": "Kernell-OS-SDK/3.1"},
        )
        
        with urllib.request.urlopen(req, timeout=2) as resp:
            data = json.loads(resp.read().decode())
        
        models = []
        for item in data.get("models", []):
            name = item.get("name", "unknown")
            size = item.get("size", 0)
            
            # Estimate context from model size
            ctx = 8192 if size < 10_000_000_000 else 32768
            
            models.append(ModelMarketInfo(
                model_id=f"local/{name}",
                provider="local",
                input_cost_per_1k=0.0,
                output_cost_per_1k=0.0,
                context_limit=ctx,
                max_output_tokens=4096,
                avg_latency_ms=200,
                p95_latency_ms=600,
                quality_score=0.55,
            ))
        
        if models:
            logger.info(f"[Market:Ollama] Live: {len(models)} local models detected")
        
        return models or self._static_fallback()
    
    def _static_fallback(self) -> List[ModelMarketInfo]:
        return [
            ModelMarketInfo(
                model_id="local/sully-8b",
                provider="local",
                input_cost_per_1k=0.0,
                output_cost_per_1k=0.0,
                context_limit=8192,
                max_output_tokens=4096,
                avg_latency_ms=200,
                p95_latency_ms=600,
                quality_score=0.55,
            ),
            ModelMarketInfo(
                model_id="local/llama3-8b",
                provider="local",
                input_cost_per_1k=0.0,
                output_cost_per_1k=0.0,
                context_limit=8192,
                max_output_tokens=4096,
                avg_latency_ms=250,
                p95_latency_ms=700,
                quality_score=0.60,
            ),
        ]


# Legacy aliases for backward compatibility
GroqMarketProvider = GroqLiveMarketProvider
OpenRouterMarketProvider = OpenRouterLiveMarketProvider
LocalMarketProvider = OllamaLiveMarketProvider


# ══════════════════════════════════════════════════════════════════════
# MARKET REGISTRY
# ══════════════════════════════════════════════════════════════════════

class ModelMarketRegistry:
    """
    Aggregated, cached market view across all providers.
    TTL-based cache to avoid hammering APIs on every Sully decision.
    """
    
    def __init__(self, providers: List[ModelMarketProvider] = None, ttl: float = 60.0):
        self.providers = providers or [
            OllamaLiveMarketProvider(),
            GroqLiveMarketProvider(),
            OpenRouterLiveMarketProvider(),
        ]
        self.ttl = ttl  # 60s for live APIs (was 5s for static)
        self._cache: Dict[str, ModelMarketInfo] = {}
        self._last_fetch: float = 0.0
        self._fetch_count: int = 0
        self._last_fetch_source: str = ""  # "live" or "static"
    
    def get_market(self) -> Dict[str, ModelMarketInfo]:
        """Return current market snapshot (cached with TTL)."""
        now = time.time()
        if self._cache and (now - self._last_fetch < self.ttl):
            return self._cache
        
        market = {}
        live_count = 0
        for provider in self.providers:
            try:
                models = provider.fetch_models()
                for m in models:
                    market[m.model_id] = m
                    if hasattr(provider, '_fetch_live'):
                        live_count += 1
            except Exception as e:
                logger.warning(f"[Market] Provider {provider.__class__.__name__} failed: {e}")
        
        self._cache = market
        self._last_fetch = now
        self._fetch_count += 1
        self._last_fetch_source = "live" if live_count > 0 else "static"
        
        logger.info(
            f"[Market] Refreshed: {len(market)} models "
            f"(source: {self._last_fetch_source}, fetch #{self._fetch_count})"
        )
        return market
    
    def get_models_by_tier(self, market: Dict[str, ModelMarketInfo] = None) -> Dict[Tier, List[ModelMarketInfo]]:
        """Categorize models into tiers based on cost."""
        market = market or self.get_market()
        tiers = {Tier.LOCAL: [], Tier.ECONOMIC: [], Tier.PREMIUM: []}
        
        for m in market.values():
            if m.provider == "local":
                tiers[Tier.LOCAL].append(m)
            elif m.input_cost_per_1k < 0.001:
                tiers[Tier.ECONOMIC].append(m)
            else:
                tiers[Tier.PREMIUM].append(m)
        
        return tiers
    
    def mark_rate_limited(self, model_id: str):
        """Mark a model as rate-limited (called by execution layer on 429s)."""
        if model_id in self._cache:
            m = self._cache[model_id]
            m.rate_limited = True
            # Penalize reliability on rate limit
            m.reliability_score = m.reliability_score * 0.8
            logger.info(f"[Market] {model_id} marked as rate-limited (reliability: {m.reliability_score:.2f})")
    
    def update_market_feedback(self, model_id: str, success: bool, reward: float = 0.0, latency_ms: float = 0.0):
        """
        Dynamically adjust market parameters based on real execution telemetry.
        - quality_score: EMA of real rewards
        - reliability_score: drops on failure, recovers on success
        - avg_latency_ms: EMA of real latency
        """
        if model_id in self._cache:
            m = self._cache[model_id]
            
            # EMA Learning rates
            alpha_q = 0.1  # Quality adapts slowly
            alpha_l = 0.2  # Latency adapts faster
            
            # 1. Update Quality (using Reward V2 if available, else success binary)
            signal = max(0.0, min(1.0, reward)) if reward != 0.0 else (1.0 if success else 0.0)
            m.quality_score = m.quality_score * (1 - alpha_q) + signal * alpha_q
            
            # 2. Update Reliability
            if success:
                m.reliability_score = min(1.0, m.reliability_score + 0.05) # Recover
            else:
                m.reliability_score = m.reliability_score * 0.7 # Punish hard
                
            # 3. Update Latency
            if success and latency_ms > 0:
                m.avg_latency_ms = m.avg_latency_ms * (1 - alpha_l) + latency_ms * alpha_l
                
            logger.debug(f"[Market] Updated {model_id}: Q={m.quality_score:.2f}, Rel={m.reliability_score:.2f}, Lat={m.avg_latency_ms:.0f}ms")
            
    # Keep for backward compatibility during transition
    def update_quality_score(self, model_id: str, success: bool):
        self.update_market_feedback(model_id, success)
    
    def status(self) -> Dict:
        """Market registry status for observability."""
        return {
            "cached_models": len(self._cache),
            "last_fetch_source": self._last_fetch_source,
            "fetch_count": self._fetch_count,
            "ttl": self.ttl,
            "model_ids": list(self._cache.keys()),
        }
