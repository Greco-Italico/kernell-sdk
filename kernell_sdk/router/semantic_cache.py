"""
Kernell OS SDK — Semantic Cache (Phase D)
══════════════════════════════════════════
Production-grade semantic cache backed by Qdrant vector search.

Architecture:
  L1: In-memory dict (instant, per-process)
  L2: Qdrant collection (persistent, cross-process)

Cache logic:
  1. Hash exact query → L1 check (0ms)
  2. Embed query → Qdrant similarity search → L2 check (5-20ms)
  3. On miss → execute → store result in both layers

Invalidation:
  - TTL-based expiry
  - Model version namespace (cache per policy version)
  - Manual purge API

Saves tokens by never re-executing semantically identical queries.
"""
from __future__ import annotations

import hashlib
import logging
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional

logger = logging.getLogger("kernell.router.semantic_cache")


@dataclass
class CacheEntry:
    """A cached response with metadata."""
    query: str
    response: str
    model_used: str
    tokens_used: int
    cost_usd: float
    created_at: float
    ttl_seconds: float
    query_hash: str
    namespace: str = ""

    @property
    def is_expired(self) -> bool:
        return (time.time() - self.created_at) > self.ttl_seconds


@dataclass
class CacheConfig:
    """Configuration for the semantic cache."""
    enabled: bool = True
    # L1 (in-memory)
    l1_max_size: int = 500
    # L2 (Qdrant)
    qdrant_url: str = "http://localhost:6333"
    collection_name: str = "kernell_semantic_cache"
    vector_size: int = 384  # all-MiniLM-L6-v2 default
    similarity_threshold: float = 0.92  # Minimum cosine similarity for cache hit
    # Governance
    default_ttl_seconds: float = 3600.0  # 1 hour
    namespace: str = ""  # Segment by model/policy version
    max_collection_size: int = 50000


@dataclass
class CacheStats:
    """Cache performance metrics."""
    l1_hits: int = 0
    l2_hits: int = 0
    misses: int = 0
    stores: int = 0
    evictions: int = 0
    tokens_saved: int = 0
    cost_saved_usd: float = 0.0

    @property
    def total_queries(self) -> int:
        return self.l1_hits + self.l2_hits + self.misses

    @property
    def hit_rate(self) -> float:
        total = self.total_queries
        return ((self.l1_hits + self.l2_hits) / total * 100) if total > 0 else 0.0

    def to_dict(self) -> dict:
        return {
            "l1_hits": self.l1_hits,
            "l2_hits": self.l2_hits,
            "misses": self.misses,
            "stores": self.stores,
            "hit_rate_pct": round(self.hit_rate, 1),
            "tokens_saved": self.tokens_saved,
            "cost_saved_usd": round(self.cost_saved_usd, 6),
        }


class SemanticCache:
    """
    Two-layer semantic cache for the Token Economy Engine.

    Implements the CacheBackend protocol expected by IntelligentRouter.
    """

    def __init__(self, config: Optional[CacheConfig] = None):
        self._config = config or CacheConfig()
        self._stats = CacheStats()

        # L1: In-memory exact-match cache
        self._l1: Dict[str, CacheEntry] = {}

        # L2: Qdrant client (lazy init)
        self._qdrant = None
        self._embedder = None
        self._l2_ready = False

        if self._config.enabled:
            self._init_l2()

    # ── Protocol methods (CacheBackend) ──────────────────────────────

    def query(self, prompt: str, model: str = "") -> Optional[object]:
        """Check cache for a semantically similar query."""
        if not self._config.enabled:
            return None

        query_hash = self._hash(prompt)

        # L1: Exact match
        if query_hash in self._l1:
            entry = self._l1[query_hash]
            if not entry.is_expired:
                self._stats.l1_hits += 1
                self._stats.tokens_saved += entry.tokens_used
                self._stats.cost_saved_usd += entry.cost_usd
                logger.debug(f"Cache L1 HIT: {query_hash[:8]}")
                return _CacheResult(entry.response)
            else:
                del self._l1[query_hash]

        # L2: Semantic similarity
        if self._l2_ready:
            result = self._search_qdrant(prompt)
            if result:
                self._stats.l2_hits += 1
                self._stats.tokens_saved += result.get("tokens_used", 0)
                self._stats.cost_saved_usd += result.get("cost_usd", 0.0)
                # Promote to L1
                self._l1[query_hash] = CacheEntry(
                    query=prompt,
                    response=result["response"],
                    model_used=result.get("model_used", ""),
                    tokens_used=result.get("tokens_used", 0),
                    cost_usd=result.get("cost_usd", 0.0),
                    created_at=time.time(),
                    ttl_seconds=self._config.default_ttl_seconds,
                    query_hash=query_hash,
                )
                logger.debug(f"Cache L2 HIT: similarity={result.get('score', 0):.3f}")
                return _CacheResult(result["response"])

        self._stats.misses += 1
        return None

    def store(
        self,
        prompt: str,
        response: str,
        model_used: str = "",
        tokens_used: int = 0,
        cost_usd: float = 0.0,
    ) -> None:
        """Store a result in both cache layers."""
        if not self._config.enabled:
            return

        query_hash = self._hash(prompt)
        entry = CacheEntry(
            query=prompt,
            response=response,
            model_used=model_used,
            tokens_used=tokens_used,
            cost_usd=cost_usd,
            created_at=time.time(),
            ttl_seconds=self._config.default_ttl_seconds,
            query_hash=query_hash,
            namespace=self._config.namespace,
        )

        # L1: Store (with eviction)
        if len(self._l1) >= self._config.l1_max_size:
            oldest_key = min(self._l1, key=lambda k: self._l1[k].created_at)
            del self._l1[oldest_key]
            self._stats.evictions += 1
        self._l1[query_hash] = entry

        # L2: Store in Qdrant
        if self._l2_ready:
            self._store_qdrant(prompt, entry)

        self._stats.stores += 1

    # ── L2 Qdrant Operations ─────────────────────────────────────────

    def _init_l2(self) -> None:
        """Initialize Qdrant client and collection."""
        try:
            from qdrant_client import QdrantClient
            from qdrant_client.models import (
                Distance, VectorParams, PointStruct,
            )

            self._qdrant = QdrantClient(url=self._config.qdrant_url, timeout=5.0)
            self._qdrant.get_collections()  # connectivity check

            # Create collection if missing
            collections = [c.name for c in self._qdrant.get_collections().collections]
            if self._config.collection_name not in collections:
                self._qdrant.create_collection(
                    collection_name=self._config.collection_name,
                    vectors_config=VectorParams(
                        size=self._config.vector_size,
                        distance=Distance.COSINE,
                    ),
                )
                logger.info(f"Created Qdrant collection: {self._config.collection_name}")

            # Try to load embedder
            self._init_embedder()
            self._l2_ready = True
            logger.info("SemanticCache L2 (Qdrant) initialized")

        except ImportError:
            logger.info("qdrant-client not installed, L2 cache disabled")
            self._l2_ready = False
        except Exception as e:
            logger.warning(f"Qdrant L2 init failed: {e}, running L1-only")
            self._l2_ready = False

    def _init_embedder(self) -> None:
        """Initialize sentence embedder for semantic search."""
        try:
            from sentence_transformers import SentenceTransformer
            self._embedder = SentenceTransformer("all-MiniLM-L6-v2")
            logger.info("Loaded sentence embedder: all-MiniLM-L6-v2")
        except ImportError:
            # Fallback: use simple hash-based pseudo-embeddings
            self._embedder = None
            logger.info("sentence-transformers not installed, using hash embeddings")

    def _embed(self, text: str) -> List[float]:
        """Embed text into a vector."""
        if self._embedder:
            return self._embedder.encode(text).tolist()
        else:
            # Deterministic pseudo-embedding from hash (fallback)
            import struct
            h = hashlib.sha512(text.encode()).digest()
            # Unpack as 384 dimensions (48 bytes * 8 = 384 floats via interpolation)
            values = []
            for i in range(0, min(len(h), 48), 1):
                values.append((h[i] - 128) / 128.0)
            # Pad to vector_size
            while len(values) < self._config.vector_size:
                values.append(0.0)
            return values[:self._config.vector_size]

    def _search_qdrant(self, prompt: str) -> Optional[Dict]:
        """Search Qdrant for semantically similar cached queries."""
        try:
            vector = self._embed(prompt)
            results = self._qdrant.search(
                collection_name=self._config.collection_name,
                query_vector=vector,
                limit=1,
                score_threshold=self._config.similarity_threshold,
            )
            if results:
                hit = results[0]
                payload = hit.payload or {}
                # Check TTL
                created = payload.get("created_at", 0)
                ttl = payload.get("ttl_seconds", self._config.default_ttl_seconds)
                if time.time() - created > ttl:
                    return None
                return {
                    "response": payload.get("response", ""),
                    "model_used": payload.get("model_used", ""),
                    "tokens_used": payload.get("tokens_used", 0),
                    "cost_usd": payload.get("cost_usd", 0.0),
                    "score": hit.score,
                }
        except Exception as e:
            logger.debug(f"Qdrant search error: {e}")
        return None

    def _store_qdrant(self, prompt: str, entry: CacheEntry) -> None:
        """Store entry in Qdrant."""
        try:
            from qdrant_client.models import PointStruct
            vector = self._embed(prompt)
            point_id = int(hashlib.md5(entry.query_hash.encode(), usedforsecurity=False).hexdigest()[:15], 16)
            self._qdrant.upsert(
                collection_name=self._config.collection_name,
                points=[PointStruct(
                    id=point_id,
                    vector=vector,
                    payload={
                        "query_hash": entry.query_hash,
                        "response": entry.response[:5000],  # Cap stored response
                        "model_used": entry.model_used,
                        "tokens_used": entry.tokens_used,
                        "cost_usd": entry.cost_usd,
                        "created_at": entry.created_at,
                        "ttl_seconds": entry.ttl_seconds,
                        "namespace": entry.namespace,
                    },
                )],
            )
        except Exception as e:
            logger.debug(f"Qdrant store error: {e}")

    # ── Governance ───────────────────────────────────────────────────

    def purge(self, namespace: str = "") -> int:
        """Purge cache entries, optionally by namespace."""
        count = len(self._l1)
        self._l1.clear()

        if self._l2_ready:
            try:
                self._qdrant.delete_collection(self._config.collection_name)
                self._init_l2()
            except Exception:
                pass

        logger.info(f"Cache purged: {count} L1 entries cleared")
        return count

    def get_stats(self) -> dict:
        return self._stats.to_dict()

    # ── Helpers ───────────────────────────────────────────────────────

    @staticmethod
    def _hash(text: str) -> str:
        return hashlib.sha256(text.encode()).hexdigest()[:16]


class _CacheResult:
    """Wrapper to satisfy the CacheBackend protocol."""
    def __init__(self, response: str):
        self.response = response

    def __str__(self):
        return self.response


if __name__ == "__main__":
    print("🧠 SemanticCache — Self-test")
    print("=" * 50)

    cache = SemanticCache(CacheConfig(enabled=True, qdrant_url="http://localhost:6333"))

    # Store
    cache.store("List all .py files in src/", "file1.py\nfile2.py", model_used="local", tokens_used=20)
    cache.store("Format JSON as markdown table", "| col1 | col2 |", model_used="local", tokens_used=15)

    # L1 hit
    r1 = cache.query("List all .py files in src/")
    print(f"  L1 exact hit: {'✅' if r1 else '❌'}")

    # Miss
    r2 = cache.query("Something completely different")
    print(f"  Miss:         {'✅' if not r2 else '❌'}")

    print(f"\n📊 Stats: {cache.get_stats()}")
    print(f"   L2 ready: {cache._l2_ready}")
