"""
Kernell OS SDK — Matching Engine
═════════════════════════════════
Motor de matching automático que encuentra el mejor agente worker
para una tarea dada, usando una fórmula ponderada multivariable.

Fórmula:
  M = 0.25R + 0.20B + 0.20A + 0.15L + 0.10P + 0.10U

  R: reputación global (0-100)
  B: benchmark score para la categoría (0-100)
  A: disponibilidad actual (0-100)
  L: latencia normalizada invertida (0-100, menor = mejor)
  P: competitividad de precio (0-100, menor precio = mayor score)
  U: uptime histórico (0-100)
"""
from dataclasses import dataclass, field
from typing import List, Optional, Dict
from enum import Enum


@dataclass
class AgentCandidate:
    """Representa a un agente candidato para ser matcheado."""
    agent_id: str
    agent_name: str
    reputation: float = 0.0        # R (0-100)
    benchmark_score: float = 0.0   # B (0-100)
    availability: float = 100.0    # A (0-100): % de recursos libres
    latency_ms: float = 50.0       # Latencia en ms (se normaliza internamente)
    price_kern: float = 0.0        # Precio del servicio en KERN
    uptime: float = 100.0          # U (0-100): uptime histórico
    region: str = "unknown"
    category_scores: Dict[str, float] = field(default_factory=dict)
    gpu_vram_gb: float = 0.0
    cpu_cores: int = 0
    ram_gb: float = 0.0
    badges: List[str] = field(default_factory=list)
    completed_jobs: int = 0


@dataclass
class MatchResult:
    """Resultado de matching con score desglosado."""
    agent: AgentCandidate
    total_score: float
    breakdown: Dict[str, float]
    rank: int = 0


class MatchingEngine:
    """
    Motor de matching automático multiagente.
    Evalúa candidatos con fórmula ponderada y devuelve ranking ordenado.
    """

    # Pesos de la fórmula
    W_REPUTATION = 0.25
    W_BENCHMARK = 0.20
    W_AVAILABILITY = 0.20
    W_LATENCY = 0.15
    W_PRICE = 0.10
    W_UPTIME = 0.10

    # Constantes de normalización
    MAX_LATENCY_MS = 500.0   # Latencia máxima aceptable
    MAX_PRICE_KERN = 1000.0  # Precio máximo para normalización

    def __init__(self):
        self._candidate_pool: List[AgentCandidate] = []

    def register_candidate(self, candidate: AgentCandidate):
        """Registra un agente como candidato disponible en el pool."""
        self._candidate_pool.append(candidate)

    def register_candidates(self, candidates: List[AgentCandidate]):
        self._candidate_pool.extend(candidates)

    def _normalize_latency(self, latency_ms: float) -> float:
        """Menor latencia = mayor score. Normaliza de 0-100 invertido."""
        if latency_ms <= 0:
            return 100.0
        normalized = max(0.0, (1.0 - (latency_ms / self.MAX_LATENCY_MS))) * 100.0
        return min(100.0, normalized)

    def _normalize_price(self, price: float, max_price: float) -> float:
        """Menor precio = mayor score competitivo."""
        if max_price <= 0:
            return 100.0
        normalized = max(0.0, (1.0 - (price / max_price))) * 100.0
        return min(100.0, normalized)

    def _score_candidate(self, candidate: AgentCandidate, category: str = None, max_price: float = None) -> MatchResult:
        """Calcula el score total de un candidato."""
        r = candidate.reputation
        b = candidate.category_scores.get(category, candidate.benchmark_score) if category else candidate.benchmark_score
        a = candidate.availability
        l = self._normalize_latency(candidate.latency_ms)
        p = self._normalize_price(candidate.price_kern, max_price or self.MAX_PRICE_KERN)
        u = candidate.uptime

        total = (
            self.W_REPUTATION * r +
            self.W_BENCHMARK * b +
            self.W_AVAILABILITY * a +
            self.W_LATENCY * l +
            self.W_PRICE * p +
            self.W_UPTIME * u
        )

        return MatchResult(
            agent=candidate,
            total_score=round(total, 2),
            breakdown={
                "reputation": round(self.W_REPUTATION * r, 2),
                "benchmark": round(self.W_BENCHMARK * b, 2),
                "availability": round(self.W_AVAILABILITY * a, 2),
                "latency": round(self.W_LATENCY * l, 2),
                "price": round(self.W_PRICE * p, 2),
                "uptime": round(self.W_UPTIME * u, 2),
            }
        )

    def find_best_match(
        self,
        category: str = None,
        min_reputation: float = 0.0,
        max_price: float = None,
        region: str = None,
        min_gpu_vram: float = 0.0,
        top_n: int = 5,
    ) -> List[MatchResult]:
        """
        Busca y rankea los mejores candidatos según filtros y fórmula de matching.

        Args:
            category: Categoría del trabajo (e.g., "GPU_RENDER")
            min_reputation: Reputación mínima requerida
            max_price: Precio máximo en KERN
            region: Filtro por región geográfica
            min_gpu_vram: VRAM mínima requerida en GB
            top_n: Número de resultados a devolver

        Returns:
            Lista ordenada de MatchResult (mejor primero)
        """
        # Filtrar candidatos
        filtered = []
        for c in self._candidate_pool:
            if c.reputation < min_reputation:
                continue
            if max_price and c.price_kern > max_price:
                continue
            if region and c.region != region:
                continue
            if min_gpu_vram and c.gpu_vram_gb < min_gpu_vram:
                continue
            filtered.append(c)

        if not filtered:
            return []

        # Calcular precio máximo real para normalización relativa
        actual_max_price = max(c.price_kern for c in filtered) if filtered else self.MAX_PRICE_KERN

        # Puntuar cada candidato
        results = [self._score_candidate(c, category, actual_max_price) for c in filtered]

        # Ordenar por score descendente
        results.sort(key=lambda r: r.total_score, reverse=True)

        # Asignar ranking
        for i, result in enumerate(results):
            result.rank = i + 1

        return results[:top_n]
