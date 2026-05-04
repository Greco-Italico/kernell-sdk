"""
Kernell OS SDK — Hardware-Aware Model Registry
════════════════════════════════════════════════
Maps detected hardware capabilities to the optimal set of local models.
This is what runs at SDK installation time to recommend how much
compute power the user should cede to the agentic environment.

The registry answers: "Given THIS hardware, which models can I run,
and what difficulty level can each handle?"
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from .types import ModelTier, DifficultyLevel

logger = logging.getLogger("kernell.router.registry")


@dataclass
class LocalModelSpec:
    """Specification for a local model that can be installed."""
    name: str                       # e.g. "qwen3-1.7b"
    ollama_tag: str                 # e.g. "qwen3:1.7b-q4_K_M"
    params_b: float                 # billions of parameters
    ram_q4_gb: float                # RAM needed at Q4 quantization
    ram_q8_gb: float                # RAM needed at Q8 quantization
    ram_fp16_gb: float              # RAM needed at FP16
    tier: ModelTier                 # which tier this serves
    max_difficulty: DifficultyLevel # highest difficulty it can handle well
    specialties: List[str] = field(default_factory=list)  # e.g. ["code", "math"]
    is_classifier: bool = False     # True if this model is the fine-tuned classifier


# ── Default Model Catalog ──────────────────────────────────────────────────
# These are the models the SDK knows about out of the box.
# Users can extend this via config.

DEFAULT_CATALOG: List[LocalModelSpec] = [
    # ── Classifier (INDISPENSABLE — always installed) ──
    LocalModelSpec(
        name="kernell-classifier",
        ollama_tag="qwen3:1.7b-q4_K_M",
        params_b=1.7, ram_q4_gb=1.1, ram_q8_gb=2.0, ram_fp16_gb=3.5,
        tier=ModelTier.LOCAL_SMALL,
        max_difficulty=DifficultyLevel.MEDIUM,
        specialties=["classification", "decomposition"],
        is_classifier=True,
    ),
    # ── Tier: NANO ──
    LocalModelSpec(
        name="qwen3-0.6b",
        ollama_tag="qwen3:0.6b-q4_K_M",
        params_b=0.6, ram_q4_gb=0.42, ram_q8_gb=0.7, ram_fp16_gb=1.2,
        tier=ModelTier.LOCAL_NANO,
        max_difficulty=DifficultyLevel.TRIVIAL,
        specialties=["formatting", "extraction"],
    ),
    LocalModelSpec(
        name="gemma3-1b",
        ollama_tag="gemma3:1b-q4_K_M",
        params_b=1.0, ram_q4_gb=0.7, ram_q8_gb=1.2, ram_fp16_gb=2.0,
        tier=ModelTier.LOCAL_NANO,
        max_difficulty=DifficultyLevel.EASY,
        specialties=["general"],
    ),
    # ── Tier: SMALL ──
    LocalModelSpec(
        name="qwen3-1.7b",
        ollama_tag="qwen3:1.7b-q4_K_M",
        params_b=1.7, ram_q4_gb=1.1, ram_q8_gb=2.0, ram_fp16_gb=3.5,
        tier=ModelTier.LOCAL_SMALL,
        max_difficulty=DifficultyLevel.EASY,
        specialties=["general", "code"],
    ),
    LocalModelSpec(
        name="phi-4-mini",
        ollama_tag="phi4-mini:3.8b-q4_K_M",
        params_b=3.8, ram_q4_gb=2.3, ram_q8_gb=4.2, ram_fp16_gb=7.6,
        tier=ModelTier.LOCAL_SMALL,
        max_difficulty=DifficultyLevel.MEDIUM,
        specialties=["reasoning", "code"],
    ),
    # ── Tier: MEDIUM ──
    LocalModelSpec(
        name="qwen3-4b",
        ollama_tag="qwen3:4b-q4_K_M",
        params_b=4.0, ram_q4_gb=2.5, ram_q8_gb=4.5, ram_fp16_gb=8.0,
        tier=ModelTier.LOCAL_MEDIUM,
        max_difficulty=DifficultyLevel.MEDIUM,
        specialties=["code", "reasoning"],
    ),
    LocalModelSpec(
        name="gemma3-4b",
        ollama_tag="gemma3:4b-q4_K_M",
        params_b=4.0, ram_q4_gb=2.6, ram_q8_gb=4.6, ram_fp16_gb=8.0,
        tier=ModelTier.LOCAL_MEDIUM,
        max_difficulty=DifficultyLevel.MEDIUM,
        specialties=["general", "creative"],
    ),
    # ── Tier: LARGE ──
    LocalModelSpec(
        name="qwen3-8b",
        ollama_tag="qwen3:8b-q4_K_M",
        params_b=8.0, ram_q4_gb=5.0, ram_q8_gb=9.0, ram_fp16_gb=16.0,
        tier=ModelTier.LOCAL_LARGE,
        max_difficulty=DifficultyLevel.HARD,
        specialties=["code", "reasoning", "math"],
    ),
    LocalModelSpec(
        name="gemma3-12b",
        ollama_tag="gemma3:12b-q4_K_M",
        params_b=12.0, ram_q4_gb=7.5, ram_q8_gb=13.0, ram_fp16_gb=24.0,
        tier=ModelTier.LOCAL_LARGE,
        max_difficulty=DifficultyLevel.HARD,
        specialties=["reasoning", "creative", "general"],
    ),
]


@dataclass
class HardwareTierConfig:
    """The result of hardware profiling — what tiers are available."""
    tier_name: str                        # "minimal", "balanced", "powerful", "maximum"
    available_ram_gb: float
    has_gpu: bool
    vram_gb: float
    installable_models: List[LocalModelSpec]
    classifier_model: LocalModelSpec
    tier_map: Dict[ModelTier, Optional[LocalModelSpec]]  # best model per tier


class ModelRegistry:
    """
    Hardware-aware model registry.
    
    At install time:
      1. Scans hardware (RAM, GPU, VRAM)
      2. Asks user how much power to cede
      3. Selects optimal model set
      4. Builds tier→model mapping for the router
    """

    def __init__(self, catalog: Optional[List[LocalModelSpec]] = None):
        self._catalog = catalog or DEFAULT_CATALOG

    def build_config(
        self,
        total_ram_gb: float,
        vram_gb: float = 0.0,
        has_gpu: bool = False,
        power_level: str = "balanced",  # "minimal", "balanced", "powerful", "maximum"
    ) -> HardwareTierConfig:
        """
        Build the optimal model configuration for this hardware.
        
        Args:
            total_ram_gb: Total system RAM
            vram_gb: GPU VRAM (0 if no GPU)
            has_gpu: Whether a GPU is present
            power_level: How much compute the user wants to cede
        """
        # Determine usable memory pool
        if has_gpu and vram_gb > 0:
            memory_pool = vram_gb
        else:
            # CPU-only: use fraction of RAM based on power level
            fractions = {
                "minimal": 0.25,
                "balanced": 0.40,
                "powerful": 0.55,
                "maximum": 0.70,
            }
            memory_pool = total_ram_gb * fractions.get(power_level, 0.40)

        # Filter models that fit in memory (Q4 quantization)
        installable = [m for m in self._catalog if m.ram_q4_gb <= memory_pool]

        # Always include the classifier (it's tiny)
        classifier = next((m for m in self._catalog if m.is_classifier), self._catalog[0])
        if classifier not in installable:
            installable.insert(0, classifier)

        # Build tier map: pick the BEST (largest) model per tier
        tier_map: Dict[ModelTier, Optional[LocalModelSpec]] = {
            ModelTier.LOCAL_NANO: None,
            ModelTier.LOCAL_SMALL: None,
            ModelTier.LOCAL_MEDIUM: None,
            ModelTier.LOCAL_LARGE: None,
        }

        for model in sorted(installable, key=lambda m: m.params_b, reverse=True):
            if model.is_classifier:
                continue
            if tier_map[model.tier] is None:
                tier_map[model.tier] = model

        # Determine tier name
        if memory_pool < 2:
            tier_name = "minimal"
        elif memory_pool < 6:
            tier_name = "balanced"
        elif memory_pool < 12:
            tier_name = "powerful"
        else:
            tier_name = "maximum"

        config = HardwareTierConfig(
            tier_name=tier_name,
            available_ram_gb=total_ram_gb,
            has_gpu=has_gpu,
            vram_gb=vram_gb,
            installable_models=installable,
            classifier_model=classifier,
            tier_map=tier_map,
        )

        logger.info(
            f"ModelRegistry: tier={tier_name}, pool={memory_pool:.1f}GB, "
            f"models={len(installable)}, gpu={has_gpu}"
        )
        return config

    def get_model_for_difficulty(
        self,
        config: HardwareTierConfig,
        difficulty: DifficultyLevel,
    ) -> Optional[LocalModelSpec]:
        """Given a difficulty level, return the cheapest local model that can handle it."""
        # Map difficulty to minimum tier
        difficulty_to_tier = {
            DifficultyLevel.TRIVIAL: ModelTier.LOCAL_NANO,
            DifficultyLevel.EASY: ModelTier.LOCAL_SMALL,
            DifficultyLevel.MEDIUM: ModelTier.LOCAL_MEDIUM,
            DifficultyLevel.HARD: ModelTier.LOCAL_LARGE,
            DifficultyLevel.EXPERT: None,  # requires external API
        }

        min_tier = difficulty_to_tier.get(difficulty)
        if min_tier is None:
            return None  # Needs premium

        # Try the minimum tier first, then escalate
        tier_order = [ModelTier.LOCAL_NANO, ModelTier.LOCAL_SMALL,
                      ModelTier.LOCAL_MEDIUM, ModelTier.LOCAL_LARGE]
        start_idx = tier_order.index(min_tier)

        for tier in tier_order[start_idx:]:
            model = config.tier_map.get(tier)
            if model is not None:
                return model

        return None  # No local model available, needs API

    def format_install_suggestion(self, config: HardwareTierConfig) -> str:
        """Generate human-readable installation suggestion."""
        lines = [
            f"╔══ Kernell OS — Hardware Profile ══╗",
            f"║  RAM: {config.available_ram_gb:.0f} GB",
            f"║  GPU: {'✅ ' + str(config.vram_gb) + ' GB VRAM' if config.has_gpu else '❌ CPU-only'}",
            f"║  Tier: {config.tier_name.upper()}",
            f"╠══ Models to Install ══╣",
        ]
        for m in config.installable_models:
            marker = "🧠" if m.is_classifier else "🤖"
            lines.append(f"║  {marker} {m.name} ({m.ram_q4_gb}GB Q4) → {m.tier.value}")
        
        coverage = sum(1 for t in config.tier_map.values() if t is not None)
        lines.append(f"╠══ Coverage ══╣")
        lines.append(f"║  Local tiers covered: {coverage}/4")
        lines.append(f"║  Estimated local resolution: {min(95, coverage * 22 + 10)}%")
        lines.append(f"╚{'═' * 34}╝")
        return "\n".join(lines)
