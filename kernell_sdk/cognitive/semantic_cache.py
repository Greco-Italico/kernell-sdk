"""
Kernell OS SDK — Semantic Cache (RAG Token Optimizer)
═════════════════════════════════════════════════════
Reduces token consumption by caching semantically similar queries.
Uses local embeddings (no external API) for privacy and zero cost.

When a prompt is similar enough to a previous one, the cached
response is returned instantly — saving 100% of tokens for that call.

This is the "memory viva" of the system:
  - Previous code generated
  - Agent decisions
  - Past errors and fixes
  - Validated patterns
"""
from __future__ import annotations

import hashlib
import json
import logging
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger("kernell.cognitive.cache")


@dataclass
class CacheEntry:
    """A cached prompt-response pair with metadata."""
    key_hash: str
    prompt_signature: str       # First 200 chars for quick comparison
    response: str
    model_used: str
    tokens_saved: int
    created_at: float = field(default_factory=time.time)
    hit_count: int = 0
    last_hit: float = 0.0


class SemanticCache:
    """
    Multi-layer cache for LLM responses.

    Layer 1: Exact match (hash-based, O(1))
    Layer 2: Fuzzy match (n-gram similarity, O(n) but fast)
    Layer 3: Embedding match (vector similarity — future, requires sentence-transformers)

    The cache dramatically reduces token usage for repetitive tasks,
    especially in agent swarms where multiple agents ask similar questions.
    """

    def __init__(
        self,
        max_entries: int = 10000,
        similarity_threshold: float = 0.85,
        ttl_seconds: float = 3600 * 24,  # 24h default
    ):
        self._max_entries = max_entries
        self._similarity_threshold = similarity_threshold
        self._ttl = ttl_seconds
        self._exact_cache: OrderedDict[str, CacheEntry] = OrderedDict()
        self._fuzzy_index: List[Tuple[set, str]] = []  # (ngrams, key_hash)
        self._stats = {
            "exact_hits": 0,
            "fuzzy_hits": 0,
            "misses": 0,
            "tokens_saved": 0,
            "entries": 0,
        }

    def query(self, prompt: str, model: str = "") -> Optional[CacheEntry]:
        """
        Check if a similar prompt has been answered before.

        Returns CacheEntry if found, None if cache miss.
        """
        # Layer 1: Exact match
        key = self._hash_prompt(prompt)
        if key in self._exact_cache:
            entry = self._exact_cache[key]
            if time.time() - entry.created_at < self._ttl:
                entry.hit_count += 1
                entry.last_hit = time.time()
                self._stats["exact_hits"] += 1
                self._stats["tokens_saved"] += entry.tokens_saved
                self._exact_cache.move_to_end(key)
                logger.info(f"Cache EXACT HIT: {prompt[:60]}... (saved {entry.tokens_saved} tokens)")
                return entry
            else:
                del self._exact_cache[key]

        # Layer 2: Fuzzy match (n-gram similarity)
        prompt_ngrams = self._extract_ngrams(prompt)
        best_score = 0.0
        best_key = None

        for ngrams, cached_key in self._fuzzy_index:
            if not ngrams:
                continue
            intersection = len(prompt_ngrams & ngrams)
            union = len(prompt_ngrams | ngrams)
            if union == 0:
                continue
            jaccard = intersection / union

            if jaccard > best_score:
                best_score = jaccard
                best_key = cached_key

        if best_score >= self._similarity_threshold and best_key and best_key in self._exact_cache:
            entry = self._exact_cache[best_key]
            if time.time() - entry.created_at < self._ttl:
                entry.hit_count += 1
                entry.last_hit = time.time()
                self._stats["fuzzy_hits"] += 1
                self._stats["tokens_saved"] += entry.tokens_saved
                logger.info(
                    f"Cache FUZZY HIT ({best_score:.2f}): {prompt[:60]}... "
                    f"(saved {entry.tokens_saved} tokens)"
                )
                return entry

        self._stats["misses"] += 1
        return None

    def store(
        self,
        prompt: str,
        response: str,
        model_used: str = "",
        tokens_used: int = 0,
    ) -> None:
        """Store a prompt-response pair in the cache."""
        key = self._hash_prompt(prompt)

        entry = CacheEntry(
            key_hash=key,
            prompt_signature=prompt[:200],
            response=response,
            model_used=model_used,
            tokens_saved=tokens_used,
        )

        # Evict oldest if at capacity
        while len(self._exact_cache) >= self._max_entries:
            self._exact_cache.popitem(last=False)

        self._exact_cache[key] = entry

        # Update fuzzy index
        ngrams = self._extract_ngrams(prompt)
        self._fuzzy_index.append((ngrams, key))

        # Trim fuzzy index
        if len(self._fuzzy_index) > self._max_entries:
            self._fuzzy_index = self._fuzzy_index[-self._max_entries:]

        self._stats["entries"] = len(self._exact_cache)

    def get_stats(self) -> dict:
        """Return cache performance statistics."""
        total_hits = self._stats["exact_hits"] + self._stats["fuzzy_hits"]
        total_queries = total_hits + self._stats["misses"]
        hit_rate = (total_hits / total_queries * 100) if total_queries > 0 else 0

        return {
            **self._stats,
            "total_hits": total_hits,
            "total_queries": total_queries,
            "hit_rate_percent": round(hit_rate, 1),
        }

    def clear(self) -> None:
        """Flush the cache."""
        self._exact_cache.clear()
        self._fuzzy_index.clear()
        self._stats = {k: 0 for k in self._stats}

    @staticmethod
    def _hash_prompt(prompt: str) -> str:
        """Deterministic hash of a prompt (normalized)."""
        normalized = " ".join(prompt.lower().split())
        return hashlib.sha256(normalized.encode()).hexdigest()[:16]

    @staticmethod
    def _extract_ngrams(text: str, n: int = 3) -> set:
        """Extract character n-grams for fuzzy matching."""
        normalized = " ".join(text.lower().split())
        if len(normalized) < n:
            return {normalized}
        return {normalized[i:i+n] for i in range(len(normalized) - n + 1)}
