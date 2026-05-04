"""
Kernell OS SDK — Agent Discovery & Ranking
════════════════════════════════════════════
Sistema de descubrimiento público de agentes con rankings,
filtros avanzados y badges verificadas.
"""
from dataclasses import dataclass, field
from typing import List, Optional, Dict
from kernell_sdk.marketplace.matching import AgentCandidate


@dataclass
class AgentProfile:
    """Perfil público de un agente en el directorio de descubrimiento."""
    candidate: AgentCandidate
    total_earned_kern: float = 0.0
    disputes: int = 0
    badges: List[str] = field(default_factory=list)
    featured: bool = False


class DiscoveryDirectory:
    """
    Directorio público de agentes con ranking y descubrimiento.
    Permite buscar agentes por múltiples criterios competitivos.
    """

    def __init__(self):
        self._profiles: List[AgentProfile] = []

    def register_agent(self, profile: AgentProfile):
        self._profiles.append(profile)

    def register_agents(self, profiles: List[AgentProfile]):
        self._profiles.extend(profiles)

    def top_by_reputation(self, n: int = 10) -> List[AgentProfile]:
        return sorted(self._profiles, key=lambda p: p.candidate.reputation, reverse=True)[:n]

    def top_by_earnings(self, n: int = 10) -> List[AgentProfile]:
        return sorted(self._profiles, key=lambda p: p.total_earned_kern, reverse=True)[:n]

    def top_by_gpu(self, n: int = 10) -> List[AgentProfile]:
        return sorted(self._profiles, key=lambda p: p.candidate.gpu_vram_gb, reverse=True)[:n]

    def lowest_latency(self, n: int = 10) -> List[AgentProfile]:
        return sorted(self._profiles, key=lambda p: p.candidate.latency_ms)[:n]

    def highest_uptime(self, n: int = 10) -> List[AgentProfile]:
        return sorted(self._profiles, key=lambda p: p.candidate.uptime, reverse=True)[:n]

    def least_disputes(self, n: int = 10) -> List[AgentProfile]:
        return sorted(self._profiles, key=lambda p: p.disputes)[:n]

    def by_region(self, region: str) -> List[AgentProfile]:
        return [p for p in self._profiles if p.candidate.region == region]

    def by_badge(self, badge: str) -> List[AgentProfile]:
        return [p for p in self._profiles if badge in p.badges]

    def featured_agents(self) -> List[AgentProfile]:
        return [p for p in self._profiles if p.featured]

    def search(
        self,
        region: str = None,
        min_reputation: float = 0.0,
        min_gpu_vram: float = 0.0,
        max_latency: float = 500.0,
        badge: str = None,
        sort_by: str = "reputation",
        limit: int = 10,
    ) -> List[AgentProfile]:
        """Búsqueda avanzada con filtros combinados."""
        results = list(self._profiles)

        if region:
            results = [p for p in results if p.candidate.region == region]
        if min_reputation > 0:
            results = [p for p in results if p.candidate.reputation >= min_reputation]
        if min_gpu_vram > 0:
            results = [p for p in results if p.candidate.gpu_vram_gb >= min_gpu_vram]
        if max_latency < 500:
            results = [p for p in results if p.candidate.latency_ms <= max_latency]
        if badge:
            results = [p for p in results if badge in p.badges]

        sort_keys = {
            "reputation": lambda p: p.candidate.reputation,
            "earnings": lambda p: p.total_earned_kern,
            "gpu": lambda p: p.candidate.gpu_vram_gb,
            "latency": lambda p: -p.candidate.latency_ms,  # invertido
            "uptime": lambda p: p.candidate.uptime,
            "price": lambda p: -p.candidate.price_kern,     # invertido
        }
        key_fn = sort_keys.get(sort_by, sort_keys["reputation"])
        results.sort(key=key_fn, reverse=True)

        return results[:limit]
