"""
Kernell OS SDK — Dynamic Pricing Engine
════════════════════════════════════════
Precios dinámicos, subastas y recomendaciones automáticas
de pricing basadas en oferta/demanda del marketplace.
"""
from dataclasses import dataclass, field
from typing import List, Dict, Optional
import time
import uuid


@dataclass
class PriceSignal:
    """Señal de mercado para una categoría."""
    category: str
    avg_price: float = 0.0
    min_price: float = 0.0
    max_price: float = 0.0
    demand_count: int = 0      # Trabajos solicitados en las últimas 24h
    supply_count: int = 0      # Agentes ofreciendo en esa categoría
    demand_trend: str = "stable"  # "rising", "falling", "stable"


@dataclass
class Auction:
    """Subasta para un trabajo específico."""
    auction_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    category: str = ""
    buyer_id: str = ""
    description: str = ""
    max_budget_kern: float = 0.0
    deadline_timestamp: float = 0.0
    bids: List[Dict] = field(default_factory=list)
    winner_id: Optional[str] = None
    closed: bool = False


class DynamicPricingEngine:
    """Motor de precios dinámicos y subastas para el marketplace."""

    def __init__(self):
        self._price_history: Dict[str, List[float]] = {}
        self._auctions: Dict[str, Auction] = {}

    def record_transaction(self, category: str, price: float):
        """Registra una transacción para alimentar las señales de mercado."""
        if category not in self._price_history:
            self._price_history[category] = []
        self._price_history[category].append(price)

    def get_price_signal(self, category: str) -> PriceSignal:
        """Genera señal de mercado actual para una categoría."""
        prices = self._price_history.get(category, [])
        if not prices:
            return PriceSignal(category=category)

        recent = prices[-50:]  # Últimas 50 transacciones
        avg = sum(recent) / len(recent)

        # Tendencia simple: comparar primera y segunda mitad
        trend = "stable"
        if len(recent) >= 4:
            first_half = sum(recent[:len(recent)//2]) / (len(recent)//2)
            second_half = sum(recent[len(recent)//2:]) / (len(recent) - len(recent)//2)
            if second_half > first_half * 1.1:
                trend = "rising"
            elif second_half < first_half * 0.9:
                trend = "falling"

        return PriceSignal(
            category=category,
            avg_price=round(avg, 2),
            min_price=round(min(recent), 2),
            max_price=round(max(recent), 2),
            demand_count=len(recent),
            demand_trend=trend,
        )

    def recommend_price(self, category: str, agent_reputation: float) -> float:
        """Recomienda un precio óptimo basado en mercado y reputación."""
        signal = self.get_price_signal(category)
        if signal.avg_price == 0:
            return 100.0  # Precio base por defecto

        # Agentes con mejor reputación pueden cobrar más
        rep_multiplier = 1.0 + (agent_reputation - 50) / 200.0  # Rango ~0.75 a 1.25
        recommended = signal.avg_price * rep_multiplier

        # Ajuste por tendencia
        if signal.demand_trend == "rising":
            recommended *= 1.10
        elif signal.demand_trend == "falling":
            recommended *= 0.90

        return round(max(10.0, recommended), 2)

    def create_auction(self, buyer_id: str, category: str, description: str, max_budget: float, hours: int = 1) -> str:
        """Crea una subasta donde los workers pueden pujar."""
        auction = Auction(
            buyer_id=buyer_id,
            category=category,
            description=description,
            max_budget_kern=max_budget,
            deadline_timestamp=time.time() + (hours * 3600),
        )
        self._auctions[auction.auction_id] = auction
        return auction.auction_id

    def place_bid(self, auction_id: str, bidder_id: str, price: float, delivery_hours: int = 1) -> bool:
        """Un worker puja en una subasta."""
        auction = self._auctions.get(auction_id)
        if not auction or auction.closed:
            return False
        if price > auction.max_budget_kern:
            return False

        auction.bids.append({
            "bidder_id": bidder_id,
            "price": price,
            "delivery_hours": delivery_hours,
            "timestamp": time.time(),
        })
        return True

    def close_auction(self, auction_id: str) -> Optional[Dict]:
        """Cierra la subasta y selecciona al ganador (menor precio)."""
        auction = self._auctions.get(auction_id)
        if not auction or not auction.bids:
            return None

        # Ganador: menor precio
        winner = min(auction.bids, key=lambda b: b["price"])
        auction.winner_id = winner["bidder_id"]
        auction.closed = True
        return winner
