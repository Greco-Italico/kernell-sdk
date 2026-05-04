"""
Kernell OS SDK — GPU Marketplace
═════════════════════════════════
Mercado especializado de GPUs con inventario, pricing dinámico,
colas de trabajo y benchmarks verificados.

Fórmula de pricing:
  P = B_gpu × V × U × R
  B_gpu: benchmark score (0-1 normalizado)
  V: VRAM factor
  U: utilización disponible (0-1)
  R: reputación factor (0-1)
"""
from dataclasses import dataclass, field
from typing import List, Dict, Optional
from enum import Enum
import uuid
import time


class GPUModel(Enum):
    RTX_4090 = "RTX_4090"
    RTX_4080 = "RTX_4080"
    RTX_3090 = "RTX_3090"
    A100 = "A100"
    H100 = "H100"
    L40S = "L40S"
    T4 = "T4"
    V100 = "V100"
    UNKNOWN = "UNKNOWN"


# Specs de referencia por modelo
GPU_SPECS = {
    GPUModel.H100:     {"vram_gb": 80, "cuda_cores": 16896, "base_score": 100, "tdp_watts": 700},
    GPUModel.A100:     {"vram_gb": 80, "cuda_cores": 6912,  "base_score": 90,  "tdp_watts": 400},
    GPUModel.L40S:     {"vram_gb": 48, "cuda_cores": 18176, "base_score": 85,  "tdp_watts": 350},
    GPUModel.RTX_4090: {"vram_gb": 24, "cuda_cores": 16384, "base_score": 82,  "tdp_watts": 450},
    GPUModel.RTX_4080: {"vram_gb": 16, "cuda_cores": 9728,  "base_score": 70,  "tdp_watts": 320},
    GPUModel.RTX_3090: {"vram_gb": 24, "cuda_cores": 10496, "base_score": 65,  "tdp_watts": 350},
    GPUModel.V100:     {"vram_gb": 32, "cuda_cores": 5120,  "base_score": 55,  "tdp_watts": 300},
    GPUModel.T4:       {"vram_gb": 16, "cuda_cores": 2560,  "base_score": 40,  "tdp_watts": 70},
}


@dataclass
class GPUListing:
    """Una GPU disponible en el mercado."""
    listing_id: str = field(default_factory=lambda: str(uuid.uuid4())[:12])
    owner_agent_id: str = ""
    owner_name: str = ""
    gpu_model: GPUModel = GPUModel.UNKNOWN
    vram_gb: float = 0.0
    cuda_cores: int = 0
    benchmark_score: float = 0.0
    availability_percent: float = 100.0
    price_per_hour_kern: float = 0.0
    region: str = "unknown"
    reputation: float = 0.0
    uptime: float = 99.0
    tdp_watts: int = 0
    queue_depth: int = 0
    sla_hours: int = 4
    compatible_jobs: List[str] = field(default_factory=lambda: ["render", "inference", "training", "stable_diffusion"])
    active: bool = True


@dataclass
class GPURental:
    """Contrato de alquiler de GPU."""
    rental_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    listing_id: str = ""
    buyer_id: str = ""
    seller_id: str = ""
    hours: int = 1
    total_kern: float = 0.0
    started_at: float = 0.0
    status: str = "active"  # active, completed, failed


class GPUMarketplace:
    """Mercado especializado de GPUs."""

    def __init__(self):
        self._listings: List[GPUListing] = []
        self._rentals: List[GPURental] = []

    def list_gpu(self, listing: GPUListing) -> str:
        """Publica una GPU en el mercado."""
        # Auto-calcular precio si no se proporcionó
        if listing.price_per_hour_kern <= 0:
            listing.price_per_hour_kern = self.calculate_fair_price(listing)
        self._listings.append(listing)
        return listing.listing_id

    def calculate_fair_price(self, listing: GPUListing) -> float:
        """
        P = B_gpu × V × U × R
        Normalizado para dar un precio razonable en KERN/hora.
        """
        b = listing.benchmark_score / 100.0           # 0-1
        v = min(listing.vram_gb / 80.0, 1.0)           # normalizado vs H100
        u = listing.availability_percent / 100.0        # 0-1
        r = max(listing.reputation / 100.0, 0.3)        # mínimo 0.3

        base_price = 10.0  # Base en KERN/hora
        price = base_price * (1 + b) * (1 + v) * u * (1 + r * 0.5)
        return round(price, 2)

    def search(
        self,
        min_vram: float = 0,
        gpu_model: GPUModel = None,
        region: str = None,
        max_price: float = None,
        job_type: str = None,
        sort_by: str = "price",
    ) -> List[GPUListing]:
        """Búsqueda avanzada de GPUs."""
        results = [l for l in self._listings if l.active]

        if min_vram > 0:
            results = [l for l in results if l.vram_gb >= min_vram]
        if gpu_model:
            results = [l for l in results if l.gpu_model == gpu_model]
        if region:
            results = [l for l in results if l.region == region]
        if max_price:
            results = [l for l in results if l.price_per_hour_kern <= max_price]
        if job_type:
            results = [l for l in results if job_type in l.compatible_jobs]

        sort_keys = {
            "price": lambda l: l.price_per_hour_kern,
            "vram": lambda l: -l.vram_gb,
            "benchmark": lambda l: -l.benchmark_score,
            "reputation": lambda l: -l.reputation,
        }
        results.sort(key=sort_keys.get(sort_by, sort_keys["price"]))
        return results

    def rent_gpu(self, listing_id: str, buyer_id: str, hours: int = 1) -> Optional[GPURental]:
        """Alquila una GPU del mercado."""
        listing = next((l for l in self._listings if l.listing_id == listing_id and l.active), None)
        if not listing:
            return None

        rental = GPURental(
            listing_id=listing_id,
            buyer_id=buyer_id,
            seller_id=listing.owner_agent_id,
            hours=hours,
            total_kern=listing.price_per_hour_kern * hours,
            started_at=time.time(),
        )
        listing.queue_depth += 1
        self._rentals.append(rental)
        return rental
