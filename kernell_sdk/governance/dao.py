"""
Kernell OS SDK — Agent DAOs
════════════════════════════
Permite el gobierno descentralizado entre agentes mediante
votaciones ponderadas por reputación y stake.

Fórmula de poder de voto:
  V = 0.60R + 0.40S
  R: reputación (0-100)
  S: stake normalizado (0-100)
"""
from dataclasses import dataclass, field
from typing import List, Dict, Optional
import uuid
import time


@dataclass
class Proposal:
    proposal_id: str = field(default_factory=lambda: str(uuid.uuid4())[:10])
    dao_id: str = ""
    proposer_id: str = ""
    title: str = ""
    description: str = ""
    action_type: str = "buy_hardware" # buy_hardware, policy_change, distribute_funds
    action_payload: Dict = field(default_factory=dict)
    votes_for: float = 0.0
    votes_against: float = 0.0
    deadline: float = 0.0
    status: str = "active" # active, passed, rejected, executed

class AgentDAO:
    """Organización Autónoma Descentralizada para Agentes."""
    
    W_REPUTATION = 0.60
    W_STAKE = 0.40

    def __init__(self, dao_id: str):
        self.dao_id = dao_id
        self._proposals: Dict[str, Proposal] = {}
        self._members_stake: Dict[str, float] = {}
        self._members_rep: Dict[str, float] = {}

    def register_member(self, agent_id: str, stake_kern: float, reputation: float):
        self._members_stake[agent_id] = stake_kern
        self._members_rep[agent_id] = reputation

    def calculate_voting_power(self, agent_id: str) -> float:
        """V = 0.60R + 0.40S"""
        if agent_id not in self._members_stake: return 0.0
        
        r = self._members_rep.get(agent_id, 0.0)
        
        # Normalizar stake relativo al total de la DAO
        total_stake = sum(self._members_stake.values())
        s = (self._members_stake[agent_id] / total_stake * 100.0) if total_stake > 0 else 0.0
        
        return round(self.W_REPUTATION * r + self.W_STAKE * s, 2)

    def create_proposal(self, proposer_id: str, title: str, action_type: str, payload: Dict, duration_hours: int = 24) -> str:
        p = Proposal(
            dao_id=self.dao_id,
            proposer_id=proposer_id,
            title=title,
            action_type=action_type,
            action_payload=payload,
            deadline=time.time() + (duration_hours * 3600)
        )
        self._proposals[p.proposal_id] = p
        return p.proposal_id

    def cast_vote(self, proposal_id: str, voter_id: str, support: bool) -> bool:
        p = self._proposals.get(proposal_id)
        if not p or p.status != "active": return False
        
        power = self.calculate_voting_power(voter_id)
        if power <= 0: return False
        
        if support:
            p.votes_for += power
        else:
            p.votes_against += power
            
        return True

    def tally_votes(self, proposal_id: str) -> str:
        """Cierra la propuesta y evalúa el resultado."""
        p = self._proposals.get(proposal_id)
        if not p: return "error"
        
        if p.votes_for > p.votes_against:
            p.status = "passed"
        else:
            p.status = "rejected"
            
        return p.status
