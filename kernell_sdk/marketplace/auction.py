"""
Kernell OS SDK — Live Auction System
S = 0.35P + 0.25R + 0.20SLA + 0.20A
"""
from dataclasses import dataclass, field
from typing import List, Dict, Optional
import uuid, time

@dataclass
class AuctionBid:
    bid_id: str = field(default_factory=lambda: str(uuid.uuid4())[:10])
    bidder_id: str = ""
    bidder_name: str = ""
    price_kern: float = 0.0
    delivery_hours: int = 1
    reputation: float = 0.0
    availability: float = 100.0
    timestamp: float = field(default_factory=time.time)
    score: float = 0.0

@dataclass
class LiveAuction:
    auction_id: str = field(default_factory=lambda: str(uuid.uuid4())[:12])
    creator_id: str = ""
    title: str = ""
    category: str = ""
    max_budget_kern: float = 0.0
    min_bid_kern: float = 0.0
    required_sla_hours: int = 24
    min_reputation: float = 0.0
    deadline: float = 0.0
    bids: List[AuctionBid] = field(default_factory=list)
    winner: Optional[AuctionBid] = None
    status: str = "open"

class LiveAuctionEngine:
    W_PRICE = 0.35
    W_REPUTATION = 0.25
    W_SLA = 0.20
    W_AVAILABILITY = 0.20

    def __init__(self):
        self._auctions: Dict[str, LiveAuction] = {}

    def create_auction(self, creator_id, title, category, max_budget,
                       min_bid=0.0, sla_hours=24, min_reputation=0.0, duration_minutes=30):
        a = LiveAuction(creator_id=creator_id, title=title, category=category,
                        max_budget_kern=max_budget, min_bid_kern=min_bid,
                        required_sla_hours=sla_hours, min_reputation=min_reputation,
                        deadline=time.time() + duration_minutes * 60)
        self._auctions[a.auction_id] = a
        return a.auction_id

    def place_bid(self, auction_id, bid):
        a = self._auctions.get(auction_id)
        if not a or a.status != "open":
            return False
        if bid.price_kern > a.max_budget_kern or bid.price_kern < a.min_bid_kern:
            return False
        if bid.reputation < a.min_reputation:
            return False
        a.bids.append(bid)
        return True

    def _score_bid(self, bid, max_price, max_sla):
        p = max(0, (1 - bid.price_kern / max_price)) * 100 if max_price > 0 else 50
        r = bid.reputation
        sla = max(0, (1 - bid.delivery_hours / max_sla)) * 100 if max_sla > 0 else 50
        a = bid.availability
        return round(self.W_PRICE * p + self.W_REPUTATION * r + self.W_SLA * sla + self.W_AVAILABILITY * a, 2)

    def close_and_select_winner(self, auction_id):
        a = self._auctions.get(auction_id)
        if not a or not a.bids:
            return None
        max_p = max(b.price_kern for b in a.bids)
        max_s = max(b.delivery_hours for b in a.bids) or 1
        for b in a.bids:
            b.score = self._score_bid(b, max_p, max_s)
        a.bids.sort(key=lambda b: b.score, reverse=True)
        a.winner = a.bids[0]
        a.status = "closed"
        return a.winner

    def get_auction_board(self, auction_id):
        a = self._auctions.get(auction_id)
        if not a:
            return {}
        return {"auction_id": a.auction_id, "title": a.title, "status": a.status,
                "total_bids": len(a.bids), "budget": a.max_budget_kern,
                "winner": a.winner.bidder_name if a.winner else None}
