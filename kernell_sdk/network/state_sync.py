import logging
from typing import Dict, Any, List
from kernell_sdk.network.event_log import EventLog, Event

logger = logging.getLogger("p2p.state_sync")

class Metrics:
    state_divergence_count = 0

class StateSync:
    """Handles synchronization and conflict resolution between nodes."""
    def __init__(self, event_log: EventLog, validator, node_id: str):
        self.event_log = event_log
        self.validator = validator
        self.node_id = node_id

    def process_incoming_event(self, event_data: Dict[str, Any]):
        try:
            event = Event(**event_data)
        except Exception as e:
            logger.warning(f"Failed to deserialize event: {e}")
            return

        if event.event_id in self.event_log.index:
            return  # Already have it

        local_head = self.event_log.head()
        
        # If the incoming event links perfectly to our head
        if event.prev_hash == local_head:
            try:
                self.event_log.append(event)
                logger.debug(f"Appended event {event.event_id}")
            except ValueError:
                self.resolve_conflict(event)
        else:
            # Missing events or conflict
            logger.warning(f"Divergence detected. Local head: {local_head}, Event prev: {event.prev_hash}")
            self.resolve_conflict(event)

    def resolve_conflict(self, incoming_event: Event):
        Metrics.state_divergence_count += 1
        
        if not self.event_log.events:
            self.event_log.events.append(incoming_event)
            self.event_log.index[incoming_event.event_id] = incoming_event
            return

        local_last = self.event_log.events[-1]
        
        # CRITICAL: Never rollback a finalized event
        if local_last.finalized:
            logger.warning("Attempted to rollback a finalized event! Rejecting conflict.")
            return
            
        # Rule 1: Highest epoch wins
        if incoming_event.epoch > local_last.epoch:
            logger.info("Conflict resolved: incoming event has higher epoch.")
            self._replace_last(incoming_event)
            return
            
        # Rule 2: Deterministic tiebreaker on same epoch
        if incoming_event.epoch == local_last.epoch:
            winner = min([local_last, incoming_event], key=lambda e: (e.event_id, e.sender))
            if winner.event_id != local_last.event_id:
                logger.info("Conflict resolved: deterministic tiebreaker favored incoming.")
                self._replace_last(incoming_event)
            else:
                logger.info("Conflict resolved: deterministic tiebreaker favored local.")
            return

        logger.debug("Conflict resolved: incoming event is older, ignoring.")

    def _replace_last(self, event: Event):
        old_id = self.event_log.events[-1].event_id
        if old_id in self.event_log.index:
            del self.event_log.index[old_id]
        
        self.event_log.events[-1] = event
        self.event_log.index[event.event_id] = event
