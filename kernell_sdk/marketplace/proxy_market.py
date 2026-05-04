"""
Kernell OS SDK — Proxy & Bandwidth Marketplace
════════════════════════════════════════════════
Mercado de ancho de banda y proxy residencial/datacenter
con scoring de confianza, antiabuso y geolocalización.
"""
from dataclasses import dataclass, field
from typing import List, Optional
from enum import Enum
import uuid


class ProxyType(Enum):
    RESIDENTIAL = "RESIDENTIAL"
    DATACENTER = "DATACENTER"
    MOBILE = "MOBILE"
    ISP = "ISP"


@dataclass
class ProxyListing:
    """Servicio de proxy/bandwidth disponible en el mercado."""
    listing_id: str = field(default_factory=lambda: str(uuid.uuid4())[:12])
    owner_agent_id: str = ""
    owner_name: str = ""
    proxy_type: ProxyType = ProxyType.DATACENTER
    country: str = ""
    city: str = ""
    asn: str = ""
    isp: str = ""
    bandwidth_mbps: float = 0.0
    latency_ms: float = 0.0
    uptime_percent: float = 99.0
    price_per_gb_kern: float = 0.0
    trust_score: float = 0.0          # 0-100: score antiabuso
    network_reputation: float = 0.0   # 0-100: historial de la IP/rango
    abuse_reports: int = 0
    concurrent_connections: int = 100
    geo_verified: bool = False
    active: bool = True


class ProxyMarketplace:
    """Mercado de proxy y ancho de banda."""

    def __init__(self):
        self._listings: List[ProxyListing] = []

    def list_proxy(self, listing: ProxyListing) -> str:
        if listing.price_per_gb_kern <= 0:
            listing.price_per_gb_kern = self._calculate_price(listing)
        self._listings.append(listing)
        return listing.listing_id

    def _calculate_price(self, listing: ProxyListing) -> float:
        """Precio basado en tipo, ubicación y confianza."""
        base = {"RESIDENTIAL": 5.0, "DATACENTER": 1.0, "MOBILE": 8.0, "ISP": 3.0}
        price = base.get(listing.proxy_type.value, 2.0)

        # Premium por baja latencia
        if listing.latency_ms < 20:
            price *= 1.5
        # Premium por trust score alto
        price *= (1 + listing.trust_score / 200.0)
        # Descuento por abuse reports
        if listing.abuse_reports > 0:
            price *= max(0.5, 1 - listing.abuse_reports * 0.1)

        return round(price, 2)

    def search(
        self,
        country: str = None,
        proxy_type: ProxyType = None,
        min_bandwidth: float = 0,
        max_latency: float = 500,
        min_trust: float = 0,
        max_price: float = None,
        sort_by: str = "trust_score",
    ) -> List[ProxyListing]:
        results = [l for l in self._listings if l.active]

        if country:
            results = [l for l in results if l.country == country]
        if proxy_type:
            results = [l for l in results if l.proxy_type == proxy_type]
        if min_bandwidth > 0:
            results = [l for l in results if l.bandwidth_mbps >= min_bandwidth]
        if max_latency < 500:
            results = [l for l in results if l.latency_ms <= max_latency]
        if min_trust > 0:
            results = [l for l in results if l.trust_score >= min_trust]
        if max_price:
            results = [l for l in results if l.price_per_gb_kern <= max_price]

        sort_keys = {
            "trust_score": lambda l: -l.trust_score,
            "price": lambda l: l.price_per_gb_kern,
            "latency": lambda l: l.latency_ms,
            "bandwidth": lambda l: -l.bandwidth_mbps,
        }
        results.sort(key=sort_keys.get(sort_by, sort_keys["trust_score"]))
        return results
