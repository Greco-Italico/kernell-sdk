"""
Kernell OS SDK — Agent Federations
═══════════════════════════════════
Permite a múltiples agentes agruparse bajo una entidad federada,
compartiendo tesorería, reputación y carga de trabajo.
"""
from dataclasses import dataclass, field
from typing import List, Dict, Optional
import uuid
import time


@dataclass
class FederationMember:
    agent_id: str
    role: str = "member"  # admin, member, worker
    share_percent: float = 0.0
    joined_at: float = field(default_factory=time.time)


@dataclass
class Federation:
    federation_id: str = field(default_factory=lambda: str(uuid.uuid4())[:12])
    name: str = ""
    description: str = ""
    members: List[FederationMember] = field(default_factory=list)
    treasury_balance_kern: float = 0.0
    total_earned_kern: float = 0.0
    federated_reputation: float = 0.0
    created_at: float = field(default_factory=time.time)


class FederationManager:
    """Gestor de federaciones de agentes."""
    
    def __init__(self):
        self._federations: Dict[str, Federation] = {}

    def create_federation(self, name: str, founder_id: str, description: str = "") -> str:
        fed = Federation(name=name, description=description)
        fed.members.append(FederationMember(agent_id=founder_id, role="admin", share_percent=100.0))
        self._federations[fed.federation_id] = fed
        return fed.federation_id

    def add_member(self, federation_id: str, admin_id: str, new_member_id: str, share_percent: float) -> bool:
        fed = self._federations.get(federation_id)
        if not fed: return False
        
        # Verificar permisos
        admin = next((m for m in fed.members if m.agent_id == admin_id and m.role == "admin"), None)
        if not admin: return False

        # Recalcular shares equitativamente si se especifica share
        if share_percent > 0:
            total_current = sum(m.share_percent for m in fed.members)
            scale = (100.0 - share_percent) / total_current if total_current > 0 else 0
            for m in fed.members:
                m.share_percent *= scale
        
        fed.members.append(FederationMember(agent_id=new_member_id, share_percent=share_percent))
        return True

    def distribute_revenue(self, federation_id: str, amount_kern: float) -> Dict[str, float]:
        """Distribuye ingresos según el share_percent de cada miembro."""
        fed = self._federations.get(federation_id)
        if not fed: return {}

        fed.treasury_balance_kern += amount_kern
        fed.total_earned_kern += amount_kern
        
        payouts = {}
        for member in fed.members:
            payouts[member.agent_id] = round(amount_kern * (member.share_percent / 100.0), 2)
            
        return payouts
        
    def update_federated_reputation(self, federation_id: str, member_reputations: Dict[str, float]) -> float:
        """La reputación de la federación es un promedio ponderado por shares."""
        fed = self._federations.get(federation_id)
        if not fed: return 0.0
        
        total_rep = 0.0
        for m in fed.members:
            rep = member_reputations.get(m.agent_id, 0.0)
            total_rep += rep * (m.share_percent / 100.0)
            
        fed.federated_reputation = round(total_rep, 2)
        return fed.federated_reputation
