import uuid
import time
from typing import List, Dict, Optional

# Mocks para imports asumiendo la arquitectura actual
from kernell_sdk.reputation.engine import ReputationEngine, AgentReputationMetrics
from kernell_sdk.marketplace.listings import Marketplace, JobCategory
from kernell_sdk.escrow.manager import EscrowManager
from kernell_sdk.benchmarks.suite import BenchmarkSuite
from kernell_sdk.dashboard_metrics import DashboardMetrics

# Simple mock classes for Identity and Telemetry since they are complex subsystems
class AgentIdentity:
    def __init__(self, agent_id, owner_id):
        self.agent_id = agent_id
        self.owner_id = owner_id

class WalletMock:
    def __init__(self):
        self.balance = 0.0
    def get_address(self):
        return "kern_v_test_address_mock"

class TelemetryMonitorMock:
    def get_current_metrics(self):
        return {"cpu": 15, "gpu": 15, "ram": 40, "temp": 45}

class NativeMarketplaceAgent:
    """
    Agente nativo diseñado para la economía M2M de Kernell OS.
    Nace 'instrumentado' con todas las herramientas para monetizarse y crecer.
    """
    def __init__(self, owner_id: str, name: str):
        self.id = str(uuid.uuid4())
        self.name = name
        self.owner_id = owner_id
        self.created_at = time.time()
        
        # Componentes Core
        self.wallet = WalletMock()
        self.identity = AgentIdentity(agent_id=self.id, owner_id=self.owner_id)
        
        # Economía y Marketplace
        self.marketplace = Marketplace()
        # Alpha-secure escrow requires actor key registry; this mock agent is non-cryptographic.
        # Keep escrow available but explicitly ephemeral unless keys are registered.
        self.escrow_manager = EscrowManager(db_path="/tmp/kernell_escrow.sqlite3")
        
        # Observabilidad y Métrica
        self.reputation_engine = ReputationEngine()
        self.benchmarks = BenchmarkSuite()
        self.dashboard = DashboardMetrics()
        self.telemetry = TelemetryMonitorMock()
        
        # Gamificación y Nivel
        self.xp = 0
        self.level = "Novato"
        self.badges: List[str] = []
        
        # Estado de la instancia
        self.verified_skills: List[str] = []
        self.enabled_categories: List[JobCategory] = []
        
    def show_profile(self) -> dict:
        """Devuelve el perfil estático y dinámico del agente"""
        rep = self.reputation_engine.get_reputation(self.id)
        
        return {
            "static_data": {
                "agent_id": self.id,
                "name": self.name,
                "owner": self.owner_id,
                "wallet_address": self.wallet.get_address(),
                "created_at": self.created_at,
                "verified_skills": self.verified_skills
            },
            "dynamic_data": {
                "global_reputation": rep,
                "kern_available": self.wallet.balance,
                "xp": self.xp,
                "level": self.level,
                "telemetry": self.telemetry.get_current_metrics()
            },
            "growth_recommendations": self.dashboard.get_growth_recommendations(
                hw_profile={"has_gpu": True}, # Simulado: extraer de identity/hardware
                global_reputation=rep
            )
        }

    def earn_xp(self, amount: int):
        self.xp += amount
        self._update_level()

    def _update_level(self):
        if self.xp > 10000: self.level = "Elite"
        elif self.xp > 5000: self.level = "Experto"
        elif self.xp > 1000: self.level = "Profesional"
        elif self.xp > 100: self.level = "Confiable"
