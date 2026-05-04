"""
Kernell OS SDK — Hardware Discovery
═══════════════════════════════════
Scans the host system to determine available RAM, VRAM, and GPU architecture.
Used to recommend the optimal local LLM (e.g., Gemma 4 vs Llama 3) to act
as the "squire" for the expensive cloud subscription.
"""
import platform
import subprocess
import logging
from dataclasses import dataclass
from typing import Optional

logger = logging.getLogger("kernell.hardware")

@dataclass
class HardwareProfile:
    os_name: str
    arch: str
    total_ram_gb: float
    gpu_vendor: Optional[str] = None
    vram_gb: Optional[float] = None
    
    @property
    def has_gpu(self) -> bool:
        return self.vram_gb is not None and self.vram_gb > 0


class HardwareScanner:
    """Scans the system to build a hardware profile and recommend local LLMs."""
    
    @classmethod
    def scan(cls) -> HardwareProfile:
        """Runs the hardware scan."""
        import psutil
        
        os_name = platform.system().lower()
        arch = platform.machine().lower()
        ram_gb = psutil.virtual_memory().total / (1024**3)
        
        gpu_vendor = None
        vram_gb = None
        
        # 1. Check for Apple Silicon (Unified Memory)
        if os_name == "darwin" and arch == "arm64":
            gpu_vendor = "apple"
            # Apple Silicon uses unified memory, so VRAM ≈ Total RAM (minus system reserve)
            vram_gb = max(0, ram_gb - 2.0)
            
        # 2. Check for NVIDIA (Linux/Windows)
        elif cls._has_command("nvidia-smi"):
            try:
                output = subprocess.check_output(
                    ["nvidia-smi", "--query-gpu=memory.total", "--format=csv,noheader,nounits"],
                    text=True
                ).strip()
                vram_mb = sum(int(x) for x in output.split('\n'))
                vram_gb = vram_mb / 1024
                gpu_vendor = "nvidia"
            except Exception as e:
                logger.debug(f"Failed to query nvidia-smi: {e}")
                
        # 3. Check for AMD (Linux)
        elif os_name == "linux" and cls._has_command("rocm-smi"):
            try:
                output = subprocess.check_output(
                    ["rocm-smi", "--showmeminfo", "vram"],
                    text=True
                )
                # Simplified parsing for rocm-smi output
                if "VRAM" in output:
                    gpu_vendor = "amd"
                    vram_gb = 16.0 # Fallback estimate if parsing fails
            except Exception as e:
                logger.debug(f"Failed to query rocm-smi: {e}")
                
        return HardwareProfile(
            os_name=os_name,
            arch=arch,
            total_ram_gb=round(ram_gb, 1),
            gpu_vendor=gpu_vendor,
            vram_gb=round(vram_gb, 1) if vram_gb else None
        )
        
    @classmethod
    def recommend_local_llm(cls, profile: HardwareProfile) -> dict:
        """Recommends a local LLM based on available VRAM/RAM."""
        
        # The agent uses this to avoid spending cloud tokens on tasks 
        # that the local hardware can handle efficiently.
        
        memory_pool = profile.vram_gb if profile.has_gpu else (profile.total_ram_gb * 0.5)
        
        if memory_pool >= 22.0:
            return {
                "model": "gemma-4:27b-q8_0",
                "reason": f"Suficiente VRAM/RAM ({memory_pool}GB) para correr modelos frontera locales en alta precisión (Q8).",
                "capabilities": ["complex_reasoning", "coding", "agent_router"]
            }
        elif memory_pool >= 14.0:
            return {
                "model": "llama-3:8b-fp16",
                "reason": f"Capacidad media ({memory_pool}GB). Ideal para Llama 3 8B sin cuantizar, balance perfecto de latencia y calidad.",
                "capabilities": ["data_extraction", "summarization", "medium_reasoning"]
            }
        elif memory_pool >= 6.0:
            return {
                "model": "gemma-4:9b-q4_K_M",
                "reason": f"Recursos limitados ({memory_pool}GB). Se recomienda cuantización agresiva Q4 para mantener la velocidad.",
                "capabilities": ["data_extraction", "formatting", "simple_chat"]
            }
        else:
            return {
                "model": "phi-3-mini:3.8b-q4",
                "reason": f"Hardware de entrada ({memory_pool}GB). Phi-3 es el modelo más capaz para este segmento.",
                "capabilities": ["basic_formatting", "short_qa"]
            }

    @staticmethod
    def _has_command(cmd: str) -> bool:
        import shutil
        return shutil.which(cmd) is not None
