import time
import uuid
import logging
from typing import Dict, Any

from kernell_sdk.network.peer_manager import PeerManager
from kernell_sdk.network.gossip import GossipLayer
from kernell_sdk.network.validator import ProtocolValidator
from kernell_sdk.network.event_log import EventLog, Event
from kernell_sdk.network.state_sync import StateSync, Metrics

logger = logging.getLogger("p2p.node")

class P2PNode:
    """
    The MVP P2P Node combining PeerManager, Gossip, Validator, and StateSync.
    """
    def __init__(self, pubkey: str, redis_url: str):
        self.node_id = f"node_{pubkey[:8]}"
        self.pubkey = pubkey
        
        self.peer_manager = PeerManager(self.node_id)
        self.gossip = GossipLayer(redis_url)
        self.validator = ProtocolValidator(self.get_current_epoch)
        
        self.event_log = EventLog()
        self.state_sync = StateSync(self.event_log, self.validator, self.node_id)
        self.quorum_size = 2  # MVP: assume 3 total nodes -> quorum = 2
        
    def get_current_epoch(self) -> int:
        return int(time.time() / 5)

    def start(self):
        logger.info(f"Starting P2P Node {self.node_id}")
        self.gossip.start(self._handle_gossip_message)
        
    def create_message(self, msg_type: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        msg = {
            "msg_id": uuid.uuid4().hex,
            "type": msg_type,
            "sender": self.node_id,
            "epoch": self.get_current_epoch(),
            "timestamp": time.time(),
            "payload": payload,
            "signature": "simulated_signature"
        }
        return msg

    def create_event(self, event_type: str, payload: Dict[str, Any]) -> Event:
        prev_hash = self.event_log.head()
        event = Event(
            event_id="", 
            event_type=event_type, 
            payload=payload, 
            epoch=self.get_current_epoch(), 
            sender=self.node_id, 
            signature="simulated_signature", 
            prev_hash=prev_hash
        )
        event.event_id = event.compute_hash()
        return event

    def broadcast_event(self, event_type: str, payload: Dict[str, Any]):
        event = self.create_event(event_type, payload)
        self.state_sync.process_incoming_event(event.__dict__) # Apply locally first
        
        # We wrap the event inside a standard gossip message for transport
        msg = self.create_message("STATE_UPDATE", {"event": event.__dict__})
        self.gossip.broadcast(msg)
        logger.debug(f"Broadcasted Event {event_type} (hash: {event.event_id})")

    def _handle_gossip_message(self, msg: Dict[str, Any]):
        sender = msg.get("sender")
        if not sender or sender == self.node_id:
            return
            
        self.peer_manager.add_peer(sender, "unknown", sender)
        self.peer_manager.update_last_seen(sender)
        
        result = self.validator.validate(msg, sender)
        
        if not result.valid:
            if result.penalty:
                self.peer_manager.penalize_peer(sender, result.penalty)
            return
            
        self._process_valid_message(msg)
        
    def _process_valid_message(self, msg: Dict[str, Any]):
        if msg["type"] == "STATE_UPDATE":
            event_data = msg["payload"].get("event")
            if event_data:
                # We need to preserve confirmations if they exist in incoming data
                # but for MVP, we only trust local confirmations.
                self.state_sync.process_incoming_event(event_data)
                
                # Automatically confirm any new event that is now our head
                head_event = self.event_log.head()
                if head_event == event_data["event_id"]:
                    self.broadcast_confirmation(head_event)
                    
        elif msg["type"] == "EVENT_CONFIRM":
            event_id = msg["payload"].get("event_id")
            confirmer = msg["payload"].get("node_id")
            if event_id in self.event_log.index:
                event = self.event_log.index[event_id]
                if confirmer not in event.confirmations:
                    event.confirmations.append(confirmer)
                logger.debug(f"Event {event_id} confirmed by {confirmer}")
                
        # Update finality status after processing
        self.event_log.update_finality(self.quorum_size, k_depth=2)

    def broadcast_confirmation(self, event_id: str):
        payload = {
            "event_id": event_id,
            "node_id": self.node_id
        }
        msg = self.create_message("EVENT_CONFIRM", payload)
        self.gossip.broadcast(msg)
