from collections import defaultdict
from kernell_sdk.observability.event_bus import AgentEvent, GLOBAL_EVENT_BUS

class AgentState:
    def __init__(self):
        self.current_step = None
        self.last_action = None
        self.world_model = {}
        self.visual_elements = []
        self.screenshot = None
        self.timeline = []

AGENT_STATES = defaultdict(AgentState)

def handle_event(event: AgentEvent):
    state = AGENT_STATES[event.agent_id]
    
    state.timeline.append({
        "type": event.type,
        "timestamp": event.timestamp,
        "payload": event.payload
    })
    
    if event.type == "step_started":
        state.current_step = event.payload
    elif event.type == "interaction_routed":
        state.last_action = event.payload
    elif event.type == "vision_updated":
        state.visual_elements = event.payload.get("elements", [])
        state.screenshot = event.payload.get("screenshot", "")
    elif event.type == "world_model_updated":
        state.world_model = event.payload

GLOBAL_EVENT_BUS.subscribe(handle_event)
