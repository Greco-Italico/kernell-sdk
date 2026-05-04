import time
from dataclasses import dataclass
from typing import Callable, List

@dataclass
class AgentEvent:
    type: str
    agent_id: str
    timestamp: float
    payload: dict

class EventBus:
    def __init__(self):
        self._subscribers: List[Callable[[AgentEvent], None]] = []
        
    def emit(self, event_type: str, agent_id: str, payload: dict):
        event = AgentEvent(
            type=event_type,
            agent_id=agent_id,
            timestamp=time.time(),
            payload=payload,
        )
        for sub in self._subscribers:
            try:
                sub(event)
            except Exception as e:
                # Silently catch to prevent observability bugs from crashing the agent
                pass

    def subscribe(self, fn: Callable[[AgentEvent], None]):
        self._subscribers.append(fn)

# Singleton MVP
GLOBAL_EVENT_BUS = EventBus()
