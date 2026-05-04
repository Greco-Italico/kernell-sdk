"""
Kernell OS SDK — LLM Provider Registry
════════════════════════════════════════
Unified entry point for ALL LLM calls in the SDK.
Ported from Kernell OS monorepo core/sea/llm_registry.py and adapted
to work standalone (no Redis dependency).

Features:
  - Multi-provider support (Anthropic, OpenAI/Groq/OpenRouter, Gemini, Ollama)
  - Circuit breaker per provider (uses SDK's CircuitBreakerRegistry)
  - Fallback chains by role (preferred → fallback_1 → fallback_n)
  - Retry with exponential backoff
  - Reasoning trace extraction (R1 <think>, Opus thinking blocks)
  - R1 quirk handler (system prompt → user message fusion)
  - Gemini API key format handling
  - OpenRouter required headers
  - Cost-aware routing (integrates with EconomicEngine)
  - Token usage tracking

Usage:
    from kernell_sdk.llm.registry import LLMProviderRegistry

    registry = LLMProviderRegistry()
    registry.register_key("anthropic", "sk-ant-...")
    registry.register_key("groq", "gsk_...")

    response = registry.complete(
        messages=[{"role": "user", "content": "Hello"}],
        role="default",
    )
    print(response.content)
"""

import json
import logging
import os
import re
import time
import urllib.request
import urllib.error
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Callable, Dict, List, Optional, Union

from kernell_sdk.resilience import CircuitBreakerRegistry

logger = logging.getLogger("kernell.llm.registry")

MAX_RETRIES = 2
DEFAULT_TIMEOUT_S = 60


# ══════════════════════════════════════════════════════════════════════
# PROVIDER FORMAT
# ══════════════════════════════════════════════════════════════════════

class ProviderFormat(str, Enum):
    ANTHROPIC     = "anthropic"
    OPENAI_COMPAT = "openai_compat"


# ══════════════════════════════════════════════════════════════════════
# PROVIDER CATALOG (default, extensible by user)
# ══════════════════════════════════════════════════════════════════════

DEFAULT_PROVIDERS: Dict[str, Dict[str, Any]] = {
    "claude_sonnet": {
        "url":       "https://api.anthropic.com/v1/messages",
        "model":     "claude-sonnet-4-20250514",
        "format":    ProviderFormat.ANTHROPIC,
        "env_key":   "ANTHROPIC_API_KEY",
        "cost_tier": "paid",
        "reasoning": False,
    },
    "claude_opus": {
        "url":       "https://api.anthropic.com/v1/messages",
        "model":     "claude-opus-4-20250514",
        "format":    ProviderFormat.ANTHROPIC,
        "env_key":   "ANTHROPIC_API_KEY",
        "cost_tier": "paid_premium",
        "reasoning": True,
    },
    "groq_r1": {
        "url":       "https://api.groq.com/openai/v1/chat/completions",
        "model":     "deepseek-r1-distill-llama-70b",
        "format":    ProviderFormat.OPENAI_COMPAT,
        "env_key":   "GROQ_API_KEY",
        "cost_tier": "free",
        "reasoning": True,
        "r1_quirk":  True,
    },
    "groq_qwen3": {
        "url":       "https://api.groq.com/openai/v1/chat/completions",
        "model":     "qwen/qwen3-32b",
        "format":    ProviderFormat.OPENAI_COMPAT,
        "env_key":   "GROQ_API_KEY",
        "cost_tier": "free",
        "reasoning": True,
    },
    "gemini_flash": {
        "url":       "https://generativelanguage.googleapis.com/v1beta/openai/chat/completions",
        "model":     "gemini-2.5-flash",
        "format":    ProviderFormat.OPENAI_COMPAT,
        "env_key":   "GEMINI_API_KEY",
        "cost_tier": "free",
        "reasoning": True,
        "gemini":    True,
    },
    "gemini_pro": {
        "url":       "https://generativelanguage.googleapis.com/v1beta/openai/chat/completions",
        "model":     "gemini-2.5-pro",
        "format":    ProviderFormat.OPENAI_COMPAT,
        "env_key":   "GEMINI_API_KEY",
        "cost_tier": "free",
        "reasoning": True,
        "gemini":    True,
    },
    "openrouter_r1": {
        "url":        "https://openrouter.ai/api/v1/chat/completions",
        "model":      "deepseek/deepseek-r1",
        "format":     ProviderFormat.OPENAI_COMPAT,
        "env_key":    "OPENROUTER_API_KEY",
        "cost_tier":  "free",
        "reasoning":  True,
        "r1_quirk":   True,
        "openrouter": True,
    },
    "local_ollama": {
        "url":       "http://127.0.0.1:11434/v1/chat/completions",
        "model":     os.getenv("OLLAMA_MODEL", "llama3"),
        "format":    ProviderFormat.OPENAI_COMPAT,
        "env_key":   "",
        "cost_tier": "local",
        "reasoning": False,
    },
}


# ══════════════════════════════════════════════════════════════════════
# ROLE CHAINS (default, extensible by user)
# ══════════════════════════════════════════════════════════════════════

DEFAULT_ROLE_CHAINS: Dict[str, Dict[str, Any]] = {
    "default":     {"preferred": "gemini_flash",  "fallback": ["groq_qwen3", "openrouter_r1", "claude_sonnet"]},
    "premium":     {"preferred": "claude_sonnet", "fallback": ["gemini_pro", "groq_qwen3", "openrouter_r1"]},
    "reasoning":   {"preferred": "groq_r1",       "fallback": ["openrouter_r1", "gemini_flash", "claude_sonnet"]},
    "economy":     {"preferred": "gemini_flash",  "fallback": ["groq_qwen3", "local_ollama"]},
    "local_only":  {"preferred": "local_ollama",  "fallback": []},
    # Code Pipeline roles
    "architect":   {"preferred": "groq_r1",       "fallback": ["gemini_pro", "openrouter_r1", "claude_sonnet"]},
    "implementer": {"preferred": "claude_sonnet", "fallback": ["gemini_pro", "groq_qwen3"]},
    "critic":      {"preferred": "gemini_flash",  "fallback": ["groq_qwen3", "openrouter_r1"]},
    "refiner":     {"preferred": "groq_r1",       "fallback": ["claude_sonnet", "gemini_pro"]},
}


# ══════════════════════════════════════════════════════════════════════
# RESPONSE
# ══════════════════════════════════════════════════════════════════════

@dataclass
class RegistryResponse:
    """Standardized response from the LLM Provider Registry."""
    content: str
    reasoning_trace: Optional[str] = None
    provider_used: str = ""
    model_used: str = ""
    is_reasoning_model: bool = False
    latency_ms: float = 0.0
    tokens_used: int = 0
    cost_tier: str = ""
    fallback_used: bool = False
    timestamp: str = ""


# ══════════════════════════════════════════════════════════════════════
# REGISTRY
# ══════════════════════════════════════════════════════════════════════

class LLMProviderRegistry:
    """
    Unified LLM provider registry with circuit breakers, fallback chains,
    and cost-aware routing. Standalone (no Redis required).

    Ported from Kernell OS monorepo core/sea/llm_registry.py.
    """

    def __init__(
        self,
        providers: Optional[Dict[str, Dict]] = None,
        role_chains: Optional[Dict[str, Dict]] = None,
        economic_engine=None,
    ):
        self._providers = dict(providers or DEFAULT_PROVIDERS)
        self._role_chains = dict(role_chains or DEFAULT_ROLE_CHAINS)
        self._keys: Dict[str, str] = {}  # provider_name → api_key
        self._economic_engine = economic_engine
        self._metrics: List[Dict[str, Any]] = []  # In-memory metrics buffer

    # ── Key Management ───────────────────────────────────────────────

    def register_key(self, provider_family: str, api_key: str):
        """
        Register an API key for a provider family.
        provider_family: "anthropic", "groq", "gemini", "openrouter"
        """
        family_map = {
            "anthropic":  ["claude_sonnet", "claude_opus"],
            "groq":       ["groq_r1", "groq_qwen3"],
            "gemini":     ["gemini_flash", "gemini_pro"],
            "openrouter": ["openrouter_r1"],
        }
        targets = family_map.get(provider_family, [provider_family])
        for name in targets:
            self._keys[name] = api_key
        logger.info(f"[LLM] Key registered for {provider_family} ({len(targets)} providers)")

    def register_provider(self, name: str, config: Dict[str, Any]):
        """Register a custom provider (e.g., local vLLM endpoint)."""
        if "format" not in config:
            config["format"] = ProviderFormat.OPENAI_COMPAT
        self._providers[name] = config
        logger.info(f"[LLM] Custom provider registered: {name}")

    def register_role(self, role: str, preferred: str, fallback: List[str] = None):
        """Register a custom role chain."""
        self._role_chains[role] = {"preferred": preferred, "fallback": fallback or []}

    # ── Main API ─────────────────────────────────────────────────────

    def complete(
        self,
        messages: List[Dict[str, str]],
        system_prompt: str = "",
        role: str = "default",
        preferred_provider: Optional[str] = None,
        max_tokens: int = 1500,
        temperature: float = 0.3,
    ) -> Optional[RegistryResponse]:
        """
        Complete a prompt using the role's provider chain with automatic
        fallback and circuit breaker protection.
        """
        role_cfg = self._role_chains.get(role, self._role_chains["default"])
        chain = (
            [preferred_provider] + list(role_cfg.get("fallback", []))
            if preferred_provider
            else [role_cfg["preferred"]] + list(role_cfg.get("fallback", []))
        )

        for i, name in enumerate(chain):
            if name not in self._providers:
                continue

            # Circuit breaker check (uses SDK's CircuitBreakerRegistry)
            cb = CircuitBreakerRegistry.get(
                f"llm:{name}",
                failure_threshold=3,
                recovery_timeout=600.0,
            )
            if not cb.can_execute():
                logger.debug(f"[LLM] {name} circuit open — skipping")
                continue

            resp = self._call_with_retry(name, messages, system_prompt, max_tokens, temperature)
            if resp:
                cb.record_success()
                resp.fallback_used = (i > 0)
                self._record_metrics(name, resp)

                if i > 0:
                    logger.info(f"[LLM] Fallback success: {name} (attempt {i+1}, role={role})")
                return resp

            cb.record_failure(f"Provider {name} failed for role {role}")
            logger.warning(f"[LLM] {name} failed for role '{role}'")

        logger.error(f"[LLM] All providers failed — role='{role}'")
        return None

    # ── Retry ────────────────────────────────────────────────────────

    def _call_with_retry(self, name, messages, system_prompt, max_tokens, temperature):
        for attempt in range(MAX_RETRIES + 1):
            if attempt > 0:
                time.sleep(1.0 * (2 ** (attempt - 1)))  # Exponential backoff
            r = self._call_provider(name, messages, system_prompt, max_tokens, temperature)
            if r:
                return r
        return None

    def _call_provider(self, name, messages, system_prompt, max_tokens, temperature):
        config = self._providers[name]
        api_key = self._get_api_key(name, config)

        # Local providers don't need API keys
        if not api_key and config.get("cost_tier") != "local":
            logger.warning(f"[LLM] No API key for {name}")
            return None

        t0 = time.time()

        try:
            if config.get("format") == ProviderFormat.ANTHROPIC:
                raw = self._call_anthropic(config, api_key, messages, system_prompt, max_tokens)
            else:
                raw = self._call_openai_compat(config, api_key or "ollama", messages, system_prompt, max_tokens, temperature)

            if not raw:
                return None

            content, reasoning = self._extract(raw, config)
            tokens = self._extract_tokens(raw, config.get("format"))

            return RegistryResponse(
                content=content,
                reasoning_trace=reasoning,
                provider_used=name,
                model_used=config["model"],
                is_reasoning_model=config.get("reasoning", False),
                latency_ms=round((time.time() - t0) * 1000, 1),
                tokens_used=tokens,
                cost_tier=config.get("cost_tier", "unknown"),
                fallback_used=False,
                timestamp=datetime.now(timezone.utc).isoformat(),
            )
        except Exception as e:
            logger.error(f"[LLM] {name}: {type(e).__name__}: {str(e)[:150]}")
            return None

    # ── Call Formats ─────────────────────────────────────────────────

    def _call_anthropic(self, config, api_key, messages, system_prompt, max_tokens):
        p = {"model": config["model"], "max_tokens": max_tokens, "messages": messages}
        if system_prompt:
            p["system"] = [{"type": "text", "text": system_prompt, "cache_control": {"type": "ephemeral"}}]
        return self._post(config["url"], p, {
            "Content-Type": "application/json",
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01",
        })

    def _call_openai_compat(self, config, api_key, messages, system_prompt, max_tokens, temperature):
        msgs = list(messages)

        # R1 quirk: fuse system prompt into first user message
        if config.get("r1_quirk") and system_prompt:
            if msgs and msgs[0]["role"] == "user":
                msgs[0] = {
                    "role": "user",
                    "content": f"[System Instructions]\n{system_prompt}\n\n[Task]\n{msgs[0]['content']}",
                }
            else:
                msgs.insert(0, {"role": "user", "content": f"[System Instructions]\n{system_prompt}"})
        elif system_prompt:
            msgs = [{"role": "system", "content": system_prompt}] + msgs

        p = {"model": config["model"], "max_tokens": max_tokens, "messages": msgs, "temperature": temperature}
        h = {"Content-Type": "application/json", "Authorization": f"Bearer {api_key}"}

        url = config["url"]
        if config.get("openrouter"):
            h["HTTP-Referer"] = "https://kernell.os"
            h["X-Title"] = "Kernell OS SDK"
        if config.get("gemini"):
            url = f"{url}?key={api_key}"
            h.pop("Authorization", None)

        return self._post(url, p, h)

    # ── Extraction ───────────────────────────────────────────────────

    def _extract(self, raw: dict, config: dict) -> tuple:
        reasoning = None
        fmt = config.get("format")

        if fmt == ProviderFormat.ANTHROPIC:
            blocks = raw.get("content", [])
            content = "".join(b.get("text", "") for b in blocks if b.get("type") == "text").strip()
            thinks = [b.get("thinking", "") for b in blocks if b.get("type") == "thinking"]
            if thinks:
                reasoning = "\n---\n".join(thinks).strip()
        else:
            msg = (raw.get("choices") or [{}])[0].get("message", {})
            content = (msg.get("content") or "").strip()
            reasoning = msg.get("reasoning_content") or None
            # Extract <think> blocks from R1 models
            if not reasoning and "<think>" in content:
                m = re.search(r"<think>(.*?)</think>", content, re.DOTALL)
                if m:
                    reasoning = m.group(1).strip()
                    content = re.sub(r"<think>.*?</think>", "", content, flags=re.DOTALL).strip()

        return content, reasoning

    def _extract_tokens(self, raw: dict, fmt) -> int:
        u = raw.get("usage", {})
        if fmt == ProviderFormat.ANTHROPIC:
            return u.get("input_tokens", 0) + u.get("output_tokens", 0)
        return u.get("total_tokens", 0)

    # ── HTTP ─────────────────────────────────────────────────────────

    def _post(self, url: str, payload: dict, headers: dict) -> Optional[dict]:
        data = json.dumps(payload, ensure_ascii=False).encode()
        req = urllib.request.Request(
            url, data=data,
            headers={**headers, "User-Agent": "Kernell-OS-SDK/3.0"},
            method="POST"
        )
        try:
            with urllib.request.urlopen(req, timeout=DEFAULT_TIMEOUT_S) as r:
                return json.loads(r.read().decode())
        except urllib.error.HTTPError as e:
            body = ""
            try:
                body = e.read().decode()[:200]
            except Exception:
                pass
            logger.error(f"[LLM] HTTP {e.code} {url[:60]}: {body}")
        except Exception as e:
            logger.error(f"[LLM] {url[:60]}: {e}")
        return None

    # ── Key Resolution ───────────────────────────────────────────────

    def _get_api_key(self, name: str, config: dict) -> Optional[str]:
        # 1. Explicit registration
        if name in self._keys:
            return self._keys[name]
        # 2. Environment variable
        env_key = config.get("env_key", "")
        if env_key:
            return os.environ.get(env_key)
        return None

    # ── Metrics ──────────────────────────────────────────────────────

    def _record_metrics(self, name: str, resp: RegistryResponse):
        metric = {
            "provider": name,
            "model": resp.model_used,
            "latency_ms": resp.latency_ms,
            "tokens": resp.tokens_used,
            "reasoning": resp.is_reasoning_model,
            "fallback": resp.fallback_used,
            "cost_tier": resp.cost_tier,
            "timestamp": resp.timestamp,
        }
        self._metrics.append(metric)
        # Keep last 200 metrics in memory
        if len(self._metrics) > 200:
            self._metrics = self._metrics[-200:]

        # Integrate with EconomicEngine if available
        if self._economic_engine and resp.tokens_used > 0:
            try:
                self._economic_engine.record_token_usage(
                    tokens_used=resp.tokens_used,
                    provider=name,
                    model=resp.model_used,
                )
            except Exception:
                pass

    def get_metrics(self, provider: Optional[str] = None, limit: int = 50) -> List[Dict]:
        """Get recent metrics, optionally filtered by provider."""
        items = self._metrics
        if provider:
            items = [m for m in items if m["provider"] == provider]
        return items[-limit:]

    def status(self) -> Dict[str, Any]:
        """Get registry status including provider health and circuit breakers."""
        provider_status = {}
        for name in self._providers:
            cb = CircuitBreakerRegistry.get(f"llm:{name}")
            stats = cb.stats()
            has_key = name in self._keys or bool(os.environ.get(self._providers[name].get("env_key", "")))
            provider_status[name] = {
                "circuit_state": stats.state,
                "has_key": has_key,
                "cost_tier": self._providers[name].get("cost_tier", "unknown"),
                "total_failures": stats.total_failures,
                "total_successes": stats.total_successes,
            }
        return {
            "providers": provider_status,
            "roles": list(self._role_chains.keys()),
            "total_calls": len(self._metrics),
            "circuit_summary": CircuitBreakerRegistry.summary(),
        }
