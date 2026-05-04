import hashlib
import json
from dataclasses import dataclass, asdict, field
from typing import Dict, Any, Optional

@dataclass
class Event:
    event_id: str
    event_type: str
    payload: Dict[str, Any]
    epoch: int
    sender: str
    signature: str
    prev_hash: Optional[str]
    confirmations: list = field(default_factory=list)
    status: str = "PENDING"
    finalized: bool = False

    def compute_hash(self) -> str:
        raw = json.dumps({
            "event_type": self.event_type,
            "payload": self.payload,
            "epoch": self.epoch,
            "sender": self.sender,
            "prev_hash": self.prev_hash
        }, sort_keys=True)
        return hashlib.sha256(raw.encode()).hexdigest()

class EventLog:
    """Canonical local event log ensuring causal chain and deduplication."""
    def __init__(self):
        self.events = []
        self.index = {}

    def append(self, event: Event) -> bool:
        if event.event_id in self.index:
            return False  # dedupe

        if self.events:
            last_hash = self.events[-1].event_id
            if event.prev_hash != last_hash:
                raise ValueError("Broken chain")
                
        self.events.append(event)
        self.index[event.event_id] = event
        return True

    def head(self) -> Optional[str]:
        return self.events[-1].event_id if self.events else None

    def get_events_from(self, start_hash: Optional[str]) -> list:
        if not start_hash:
            return self.events
        for i, ev in enumerate(self.events):
            if ev.event_id == start_hash:
                return self.events[i+1:]
        return []

    def update_finality(self, quorum: int, k_depth: int = 2):
        for i, event in enumerate(self.events):
            if event.finalized:
                continue
                
            if len(event.confirmations) >= quorum:
                event.status = "CONFIRMED"
                
            depth = len(self.events) - i - 1
            if event.status == "CONFIRMED" and depth >= k_depth:
                event.finalized = True
