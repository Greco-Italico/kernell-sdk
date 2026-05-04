from dataclasses import dataclass, field
from typing import List, Dict, Optional

@dataclass
class FinancialMetrics:
    total_revenue: float = 0.0
    revenue_by_category: Dict[str, float] = field(default_factory=dict)
    daily_revenue: float = 0.0
    weekly_revenue: float = 0.0
    monthly_revenue: float = 0.0
    kern_earned: float = 0.0
    kern_spent: float = 0.0
    active_escrows: int = 0
    disputed_escrows: int = 0
    estimated_roi: float = 0.0

@dataclass
class OperationalMetrics:
    completed_jobs: int = 0
    failed_jobs: int = 0
    avg_delivery_time_hours: float = 0.0
    sla_fulfilled: int = 0
    sla_breached: int = 0
    avg_time_per_task: float = 0.0
    cpu_usage_percent: float = 0.0
    gpu_usage_percent: float = 0.0
    ram_usage_percent: float = 0.0
    network_usage_percent: float = 0.0

@dataclass
class GrowthProfile:
    most_profitable_skills: List[str] = field(default_factory=list)
    best_margin_jobs: List[str] = field(default_factory=list)
    underutilized_hardware: List[str] = field(default_factory=list)
    pending_benchmarks: List[str] = field(default_factory=list)
    high_demand_categories: List[str] = field(default_factory=list)

class GrowthRecommender:
    """Genera recomendaciones activas para guiar al agente hacia mayor rentabilidad."""
    
    def generate_recommendations(self, ops: OperationalMetrics, hw_profile: dict, rep: float) -> List[str]:
        recommendations = []
        
        if ops.gpu_usage_percent < 30.0 and hw_profile.get("has_gpu", False):
            recommendations.append(f"Tu GPU está subutilizada al {100 - ops.gpu_usage_percent}%. Publica servicios de renderizado o Stable Diffusion.")
            
        if ops.sla_breached > 0:
            recommendations.append(f"Has incumplido {ops.sla_breached} SLAs recientemente. Tu reputación podría verse afectada.")
            
        if ops.network_usage_percent > 90.0:
            recommendations.append("Tu latencia y ancho de banda están al límite. Esto podría afectar tus servicios de proxy premium.")
            
        if rep > 80.0 and ops.completed_jobs < 5:
            recommendations.append("Tu reputación inicial es excelente, pero tienes poco volumen. Baja temporalmente los precios para ganar más cuota de mercado.")
            
        return recommendations

class DashboardMetrics:
    """Agrega todas las métricas para alimentar el panel de crecimiento."""
    def __init__(self):
        self.financial = FinancialMetrics()
        self.operational = OperationalMetrics()
        self.growth = GrowthProfile()
        self.recommender = GrowthRecommender()

    def get_growth_recommendations(self, hw_profile: dict, global_reputation: float) -> List[str]:
        return self.recommender.generate_recommendations(
            self.operational, 
            hw_profile, 
            global_reputation
        )
