"""
Kernell OS SDK — Structured Multimodal Perception (Phase 7a)
════════════════════════════════════════════════════════════
Provides a strongly-typed perception contract. 
Translates raw pixels (base64) into structured, grounded visual elements.
"""

import json
import logging
import time
from dataclasses import dataclass, field, asdict
from typing import Any, Dict, List, Optional, Protocol, Tuple

from kernell_sdk.observability.event_bus import GLOBAL_EVENT_BUS

logger = logging.getLogger("kernell.agent.perception")

# ══════════════════════════════════════════════════════════════════════
# PERCEPTION CONTRACT
# ══════════════════════════════════════════════════════════════════════

@dataclass
class VisualElement:
    """A bounded UI element or object detected in the visual field."""
    label: str
    bbox: Tuple[int, int, int, int]  # x_min, y_min, x_max, y_max
    confidence: float
    attributes: Dict[str, Any] = field(default_factory=dict)
    
    def center(self) -> Tuple[int, int]:
        """Get the center coordinates for clicking."""
        return (
            (self.bbox[0] + self.bbox[2]) // 2,
            (self.bbox[1] + self.bbox[3]) // 2
        )
    
    def to_dict(self) -> dict:
        return asdict(self)

@dataclass
class ScreenState:
    """The structured perception of an entire screen/frame."""
    screenshot_id: str
    resolution: Tuple[int, int]
    elements: List[VisualElement] = field(default_factory=list)
    raw_text: str = ""
    timestamp: float = field(default_factory=time.time)

    def find_element(self, query: str) -> Optional[VisualElement]:
        """Find an element by label (fuzzy/exact)."""
        query = query.lower()
        # Exact match
        for el in self.elements:
            if el.label.lower() == query:
                return el
        # Partial match
        for el in self.elements:
            if query in el.label.lower():
                return el
        return None

    def to_dict(self) -> dict:
        return asdict(self)


# ══════════════════════════════════════════════════════════════════════
# ADAPTER PROTOCOL
# ══════════════════════════════════════════════════════════════════════

class VisionAdapter(Protocol):
    def analyze(self, image_base64: str, prompt: str, resolution: Tuple[int, int]) -> ScreenState:
        ...


# ══════════════════════════════════════════════════════════════════════
# GEMINI VISION ADAPTER
# ══════════════════════════════════════════════════════════════════════

class GeminiVisionAdapter:
    """
    Adapter that wraps Gemini API for structured bounding box extraction.
    Forces strictly typed JSON output for visual perception.
    """
    def __init__(self, llm_registry):
        self._registry = llm_registry
        
    def analyze(self, image_base64: str, prompt: str, resolution: Tuple[int, int]) -> ScreenState:
        system_prompt = """You are a highly precise visual perception engine.
Analyze the provided image and extract interactive or relevant UI elements.
For EACH element, output its bounding box in absolute pixels based on the original resolution.

Your output MUST be ONLY valid JSON matching this exact schema:
{
  "elements": [
    {
      "label": "descriptive name (e.g. 'Submit Button', 'Username Input', 'Main Logo')",
      "bbox": [x_min, y_min, x_max, y_max],
      "confidence": 0.95,
      "attributes": {"type": "button", "text": "Submit"}
    }
  ],
  "raw_text": "Any globally visible text or context (optional)"
}
DO NOT wrap the JSON in markdown blocks. Output raw JSON only."""

        # We inject the resolution so the model grounds correctly
        user_prompt = f"Resolution: {resolution[0]}x{resolution[1]}\n{prompt}"
        
        try:
            # Here we assume llm_registry.complete can handle images via 'images' kwarg
            # For the mock/test, it will return a JSON string
            resp = self._registry.complete(
                messages=[{"role": "user", "content": user_prompt}],
                system_prompt=system_prompt,
                role="vision",
                images=[image_base64],
                temperature=0.0,
                max_tokens=2048
            )
            
            if not resp or not resp.content:
                return ScreenState(screenshot_id="error", resolution=resolution, elements=[])
                
            text = resp.content.strip()
            # Clean markdown if present
            if text.startswith("```json"):
                text = text.split("```json")[1].split("```")[0].strip()
            elif text.startswith("```"):
                text = text.split("```")[1].split("```")[0].strip()
                
            data = json.loads(text)
            
            elements = []
            for el_data in data.get("elements", []):
                elements.append(VisualElement(
                    label=el_data.get("label", "unknown"),
                    bbox=tuple(el_data.get("bbox", [0,0,0,0])),
                    confidence=float(el_data.get("confidence", 1.0)),
                    attributes=el_data.get("attributes", {})
                ))
                
            screen_state = ScreenState(
                screenshot_id=f"frame_{int(time.time())}",
                resolution=resolution,
                elements=elements,
                raw_text=data.get("raw_text", "")
            )
            
            GLOBAL_EVENT_BUS.emit("vision_updated", "current", {
                "elements": [e.to_dict() for e in elements],
                "screenshot": image_base64
            })
            
            return screen_state
            
        except Exception as e:
            logger.error(f"[GeminiVisionAdapter] Failed to analyze image: {e}")
            return ScreenState(screenshot_id="error", resolution=resolution, elements=[])
