"""
Kernell OS SDK — Consensus Engine
═════════════════════════════════
Hierarchical consensus resolution for swarm outputs.

Pipeline:
    1. Normalize → clean outputs
    2. Cluster  → group semantically similar results
    3. Vote     → score clusters by confidence weight
    4. Judge    → LLM arbiter ONLY on ambiguous ties (budget-aware)

This is what converts "many answers" into "one trustworthy answer".
Without this, swarm execution is just expensive parallelism.
"""

import hashlib
import logging
import math
from collections import Counter
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from kernell_sdk.sully.types import FinalOutcome, Tier
from kernell_sdk.observability.event_bus import GLOBAL_EVENT_BUS

logger = logging.getLogger("kernell.swarm.consensus")


# ══════════════════════════════════════════════════════════════════════
# TYPES
# ══════════════════════════════════════════════════════════════════════

@dataclass
class Candidate:
    """A single output from one swarm agent."""
    output: str
    confidence: float
    cost: float
    latency: float
    model_used: str = ""
    task_id: str = ""


@dataclass
class ConsensusResult:
    """The consolidated, trustworthy output."""
    output: str
    confidence: float
    agreement_score: float          # 0.0 (total disagreement) → 1.0 (unanimous)
    method: str                     # "unanimous", "majority_vote", "judge", "single"
    candidate_count: int = 0
    cluster_count: int = 0


@dataclass
class Cluster:
    """A group of semantically similar candidates."""
    representative: str             # the "best" output in this cluster
    items: List[Candidate] = field(default_factory=list)
    fingerprint: Any = None         # TF-IDF vector for cosine similarity
    score: float = 0.0


# ══════════════════════════════════════════════════════════════════════
# CONSENSUS ENGINE
# ══════════════════════════════════════════════════════════════════════

class ConsensusEngine:
    """
    Resolves multiple swarm outputs into a single trustworthy result.
    
    Strategy hierarchy (cheapest first):
        1. Unanimous agreement → return immediately (no cost)
        2. Majority cluster wins → return best from top cluster
        3. Close tie → invoke LLM Judge (budget-controlled)
        4. No agreement → return highest confidence candidate
    """
    
    def __init__(
        self,
        llm_registry=None,         # for LLM Judge calls
        similarity_threshold: float = 0.85,  # Adjusted for cosine similarity
        judge_ambiguity_threshold: float = 0.2,
    ):
        self.llm = llm_registry
        self.similarity_threshold = similarity_threshold
        self.judge_threshold = judge_ambiguity_threshold
    
    def resolve(
        self,
        candidates: List[Candidate],
        use_judge: bool = True,
        budget_remaining: float = 1.0,
    ) -> ConsensusResult:
        """
        Main entry point. Takes N candidate outputs, returns 1 truth.
        """
        if not candidates:
            return ConsensusResult(
                output="", confidence=0.0, agreement_score=0.0,
                method="empty", candidate_count=0,
            )
        
        if len(candidates) == 1:
            c = candidates[0]
            result = ConsensusResult(
                output=c.output, confidence=c.confidence,
                agreement_score=1.0, method="single",
                candidate_count=1, cluster_count=1,
            )
            self._emit_consensus(result, candidates)
            return result
        
        # Stage 1: Normalize
        for c in candidates:
            c.output = self._normalize(c.output)
        
        # Stage 2: Cluster by semantic similarity
        clusters = self._cluster_candidates(candidates)
        
        # Stage 3: Score clusters
        for cluster in clusters:
            cluster.score = self._score_cluster(cluster)
        
        clusters.sort(key=lambda c: c.score, reverse=True)
        
        # Stage 4: Decide resolution strategy
        if len(clusters) == 1:
            # Unanimous agreement
            best = self._best_candidate(clusters[0])
            result = ConsensusResult(
                output=best.output, confidence=best.confidence,
                agreement_score=1.0, method="unanimous",
                candidate_count=len(candidates), cluster_count=1,
            )
        elif self._needs_judge(clusters) and use_judge and self.llm and budget_remaining > 0.002:
            # Ambiguous — invoke LLM judge
            result = self._run_judge(clusters, candidates)
        else:
            # Majority wins
            best = self._best_candidate(clusters[0])
            agreement = len(clusters[0].items) / len(candidates)
            result = ConsensusResult(
                output=best.output, confidence=best.confidence,
                agreement_score=agreement, method="majority_vote",
                candidate_count=len(candidates), cluster_count=len(clusters),
            )
        
        self._emit_consensus(result, candidates)
        return result
    
    # ── Stage 1: Normalization ───────────────────────────────────────
    
    def _normalize(self, text: str) -> str:
        """Clean output for comparison (preserve original for return)."""
        return text.strip()
    
    # ── Stage 2: Clustering ──────────────────────────────────────────
    
    def _cluster_candidates(self, candidates: List[Candidate]) -> List[Cluster]:
        """
        Group candidates by content similarity.
        Uses fingerprint-based clustering (fast, no external dependencies).
        For v1: swap to embedding-based clustering.
        """
        clusters: List[Cluster] = []
        
        for c in candidates:
            fp = self._fingerprint(c.output)
            placed = False
            
            for cluster in clusters:
                if self._similarity(fp, cluster.fingerprint) >= self.similarity_threshold:
                    cluster.items.append(c)
                    placed = True
                    break
            
            if not placed:
                clusters.append(Cluster(
                    representative=c.output,
                    items=[c],
                    fingerprint=fp,
                ))
        
        return clusters
    
    def _fingerprint(self, text: str) -> Dict[str, float]:
        """
        Content fingerprint for semantic similarity comparison.
        v1: Bag-of-Words Vector (TF) for local Cosine Similarity
        (zero external dependencies, runs anywhere).
        """
        import string
        # Normalize: lowercase, remove punctuation, tokenize
        translator = str.maketrans('', '', string.punctuation)
        words = text.lower().translate(translator).split()
        return dict(Counter(words))
    
    def _similarity(self, fp1: Dict[str, float], fp2: Dict[str, float]) -> float:
        """
        Cosine similarity between two term-frequency vectors.
        """
        intersection = set(fp1.keys()) & set(fp2.keys())
        dot_product = sum(fp1[w] * fp2[w] for w in intersection)
        
        mag1 = math.sqrt(sum(v**2 for v in fp1.values()))
        mag2 = math.sqrt(sum(v**2 for v in fp2.values()))
        
        if mag1 == 0 or mag2 == 0:
            return 0.0
            
        return dot_product / (mag1 * mag2)
    
    # ── Stage 3: Cluster Scoring ─────────────────────────────────────
    
    def _score_cluster(self, cluster: Cluster) -> float:
        """
        Score = weighted vote count.
        More items + higher confidence = stronger cluster.
        """
        items = cluster.items
        vote_weight = sum(c.confidence for c in items)
        avg_cost = sum(c.cost for c in items) / max(len(items), 1)
        
        # Bonus for cluster size (consensus strength)
        size_bonus = len(items) * 0.5
        
        return vote_weight + size_bonus - (avg_cost * 0.1)
    
    def _best_candidate(self, cluster: Cluster) -> Candidate:
        """Return the highest-confidence candidate from a cluster."""
        return max(cluster.items, key=lambda c: c.confidence)
    
    # ── Stage 4: Judge Decision ──────────────────────────────────────
    
    def _needs_judge(self, clusters: List[Cluster]) -> bool:
        """Check if top clusters are too close to decide without a judge."""
        if len(clusters) < 2:
            return False
        
        diff = abs(clusters[0].score - clusters[1].score)
        return diff < self.judge_threshold
    
    def _run_judge(
        self,
        clusters: List[Cluster],
        all_candidates: List[Candidate],
    ) -> ConsensusResult:
        """
        Invoke an LLM as a tie-breaking judge.
        Only called when clusters are too close to pick a winner.
        Budget-aware: uses ECONOMIC role.
        """
        # Build judge prompt with top candidates from each cluster
        top_outputs = []
        for i, cluster in enumerate(clusters[:3]):  # max 3 clusters to judge
            best = self._best_candidate(cluster)
            top_outputs.append({
                "index": i,
                "text": best.output[:2000],  # truncate for cost control
                "confidence": best.confidence,
                "support_count": len(cluster.items),
            })
        
        judge_prompt = (
            "You are an impartial judge. Given multiple candidate answers to the same task, "
            "select the BEST one based on correctness, completeness, and consistency.\n\n"
            f"Candidates:\n"
        )
        for o in top_outputs:
            judge_prompt += f"\n--- Candidate {o['index']} (confidence: {o['confidence']:.2f}, supporters: {o['support_count']}) ---\n{o['text']}\n"
        
        judge_prompt += "\nRespond with ONLY the index number of the best candidate (e.g. '0' or '1')."
        
        try:
            response = self.llm.complete(
                messages=[{"role": "user", "content": judge_prompt}],
                role="economy",  # cheap judge
                max_tokens=10,
            )
            
            if response and response.content:
                chosen_idx = int(response.content.strip())
                if 0 <= chosen_idx < len(clusters):
                    best = self._best_candidate(clusters[chosen_idx])
                    
                    GLOBAL_EVENT_BUS.emit("consensus_judge_used", "current", {
                        "chosen_cluster": chosen_idx,
                        "cluster_count": len(clusters),
                    })
                    
                    return ConsensusResult(
                        output=best.output,
                        confidence=best.confidence * 0.95,  # slight penalty for needing judge
                        agreement_score=len(clusters[chosen_idx].items) / len(all_candidates),
                        method="judge",
                        candidate_count=len(all_candidates),
                        cluster_count=len(clusters),
                    )
        except Exception as e:
            logger.warning(f"[Consensus] Judge failed: {e}, falling back to majority")
        
        # Judge failed → fall back to top cluster
        best = self._best_candidate(clusters[0])
        return ConsensusResult(
            output=best.output,
            confidence=best.confidence * 0.8,
            agreement_score=len(clusters[0].items) / len(all_candidates),
            method="majority_vote_fallback",
            candidate_count=len(all_candidates),
            cluster_count=len(clusters),
        )
    
    # ── Telemetry ────────────────────────────────────────────────────
    
    def _emit_consensus(self, result: ConsensusResult, candidates: List[Candidate]):
        """Emit structured event for Sully training and observability."""
        GLOBAL_EVENT_BUS.emit("consensus_resolved", "current", {
            "method": result.method,
            "agreement_score": result.agreement_score,
            "confidence": result.confidence,
            "candidate_count": result.candidate_count,
            "cluster_count": result.cluster_count,
        })
        
        # If disagreement was high, emit a special event (gold for training)
        if result.agreement_score < 0.6 and result.candidate_count > 1:
            GLOBAL_EVENT_BUS.emit("consensus_conflict", "current", {
                "agreement_score": result.agreement_score,
                "method": result.method,
                "candidate_count": result.candidate_count,
            })
