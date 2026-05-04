from enum import Enum
from dataclasses import dataclass, field
from typing import List, Dict, Optional
import uuid

class JobCategory(Enum):
    GPU_RENDER = "GPU_RENDER"
    PROXY_BANDWIDTH = "PROXY_BANDWIDTH"
    OCR = "OCR"
    TRANSLATION = "TRANSLATION"
    WEB_SCRAPING = "WEB_SCRAPING"
    STABLE_DIFFUSION = "STABLE_DIFFUSION"
    LLM_INFERENCE = "LLM_INFERENCE"
    BOT_HOSTING = "BOT_HOSTING"
    BLOCKCHAIN_INDEXING = "BLOCKCHAIN_INDEXING"
    CONTENT_MODERATION = "CONTENT_MODERATION"

@dataclass
class JobListing:
    id: str = field(default_factory=lambda: str(uuid.uuid4()))
    provider_id: str = ""
    title: str = ""
    category: JobCategory = JobCategory.GPU_RENDER
    pricing_kern: float = 0.0
    sla_hours: int = 24
    min_reputation_required: float = 0.0
    required_skills: List[str] = field(default_factory=list)
    active: bool = True

class Marketplace:
    """Gestión de publicaciones de servicios, búsqueda y matching"""
    
    def __init__(self):
        self._listings: List[JobListing] = []
        
    def publish_job(self, listing: JobListing) -> str:
        self._listings.append(listing)
        return listing.id
        
    def search_jobs(self, category: Optional[JobCategory] = None, min_reputation: float = 0.0) -> List[JobListing]:
        results = []
        for job in self._listings:
            if not job.active:
                continue
            if category and job.category != category:
                continue
            if min_reputation < job.min_reputation_required:
                continue
            results.append(job)
        return results
