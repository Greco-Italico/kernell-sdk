"""
Kernell OS SDK — Bounty Board
═════════════════════════════
A distributed task marketplace. Agents can post tasks with a $KERN reward,
and other agents in the cluster can claim and execute them.
"""
import uuid
import json
import time
import logging
from dataclasses import dataclass, asdict
from typing import List, Optional, Dict

import redis

logger = logging.getLogger("kernell.cluster.bounty")


@dataclass
class Bounty:
    id: str
    poster_node_id: str
    task_description: str
    reward_kern: float
    status: str  # "open", "claimed", "completed", "failed"
    created_at: float
    claimed_by_node_id: Optional[str] = None
    result: Optional[str] = None
    timeout_sec: float = 3600.0


class BountyBoard:
    """
    Manages the creation and claiming of distributed bounties across the cluster.
    """
    
    def __init__(self, redis_url: str, cluster_name: str = "default_swarm"):
        self.r = redis.Redis.from_url(redis_url, decode_responses=True)
        self._prefix = f"kernell:cluster:{cluster_name}:bounties"
        
    def post(self, poster_id: str, task: str, reward: float, timeout_sec: float = 3600.0) -> Bounty:
        """Posts a new bounty to the board."""
        bounty = Bounty(
            id=f"bounty_{uuid.uuid4().hex[:8]}",
            poster_node_id=poster_id,
            task_description=task,
            reward_kern=reward,
            status="open",
            created_at=time.time(),
            timeout_sec=timeout_sec
        )
        
        # Save to Redis
        key = f"{self._prefix}:{bounty.id}"
        # Expire automatically if not claimed/completed
        self.r.setex(key, int(timeout_sec), json.dumps(asdict(bounty)))
        
        # Publish event so other nodes know immediately
        self.r.publish(f"{self._prefix}_events", json.dumps({
            "event": "new_bounty",
            "bounty_id": bounty.id
        }))
        
        logger.info(f"Node {poster_id} posted bounty {bounty.id} for {reward} KERN")
        return bounty

    def get_open_bounties(self) -> List[Bounty]:
        """Returns all currently open bounties."""
        keys = self.r.keys(f"{self._prefix}:*")
        
        bounties = []
        for key in keys:
            data = self.r.get(key)
            if data:
                try:
                    b_dict = json.loads(data)
                    bounty = Bounty(**b_dict)
                    if bounty.status == "open":
                        bounties.append(bounty)
                except Exception as e:
                    logger.debug(f"Failed to parse bounty from {key}: {e}")
                    
        return bounties

    def claim(self, bounty_id: str, claimer_node_id: str) -> bool:
        """
        Attempts to claim a bounty. 
        Returns True if successful, False if already claimed.
        Uses Redis transactions (WATCH) to prevent race conditions.
        """
        key = f"{self._prefix}:{bounty_id}"
        
        with self.r.pipeline() as pipe:
            while True:
                try:
                    # Watch the key for changes
                    pipe.watch(key)
                    data = pipe.get(key)
                    
                    if not data:
                        pipe.unwatch()
                        return False
                        
                    bounty_dict = json.loads(data)
                    if bounty_dict.get("status") != "open":
                        pipe.unwatch()
                        return False
                        
                    # Prepare update
                    bounty_dict["status"] = "claimed"
                    bounty_dict["claimed_by_node_id"] = claimer_node_id
                    
                    # Execute transaction
                    pipe.multi()
                    pipe.set(key, json.dumps(bounty_dict), keepttl=True)
                    pipe.execute()
                    
                    logger.info(f"Node {claimer_node_id} claimed bounty {bounty_id}")
                    return True
                    
                except redis.WatchError:
                    # Another client modified the key between WATCH and EXEC
                    # Retry the loop
                    continue
                except Exception as e:
                    logger.error(f"Error claiming bounty {bounty_id}: {e}")
                    pipe.unwatch()
                    return False

    def submit_result(self, bounty_id: str, claimer_node_id: str, result: str) -> bool:
        """Submits the result of a claimed bounty (atomic con WATCH/MULTI/EXEC)."""
        key = f"{self._prefix}:{bounty_id}"

        with self.r.pipeline() as pipe:
            while True:
                try:
                    pipe.watch(key)
                    data = pipe.get(key)

                    if not data:
                        pipe.unwatch()
                        return False

                    bounty_dict = json.loads(data)
                    if (bounty_dict.get("status") != "claimed" or
                            bounty_dict.get("claimed_by_node_id") != claimer_node_id):
                        pipe.unwatch()
                        return False

                    bounty_dict["status"] = "completed"
                    bounty_dict["result"] = result[:50_000]  # Límite de tamaño del resultado

                    pipe.multi()
                    pipe.set(key, json.dumps(bounty_dict), keepttl=True)
                    pipe.execute()

                    logger.info(f"Node {claimer_node_id} completed bounty {bounty_id}")
                    return True

                except redis.WatchError:
                    continue  # Retry si otro cliente modificó la key
                except Exception as e:
                    logger.error(f"Error submitting result for bounty {bounty_id}: {e}")
                    pipe.unwatch()
                    return False
