"""
Kernell OS SDK — Agent World Model (Phase 5.7)
══════════════════════════════════════════════
Maintains an abstract, structured representation of the environment state.
Prevents the agent from re-reasoning the entire world from scratch every step.

Capabilities:
  - State Abstraction Layer (compiles raw observations into structured beliefs)
  - Belief Updating (modifies the world model post-action)
"""

import json
import logging
from dataclasses import dataclass, field, asdict
from typing import Any, Dict, Optional

# Phase 7a: Import perception contract
try:
    from kernell_sdk.agent_perception import ScreenState
except ImportError:
    ScreenState = Any

logger = logging.getLogger("kernell.agent.world_model")

@dataclass
class WorldModelState:
    """Explicit model of the agent's environment and beliefs."""
    current_context: str = "unknown"  # e.g., 'amazon_checkout', 'terminal_bash'
    entities: Dict[str, Any] = field(default_factory=dict)  # Key elements/objects
    task_phase: str = "exploration"   # e.g., 'exploration', 'execution', 'verification'
    known_facts: Dict[str, Any] = field(default_factory=dict) # Confirmed facts
    visual_context: Optional[ScreenState] = None # Phase 7a: Grounded visual state

    def to_dict(self) -> dict:
        d = asdict(self)
        if self.visual_context and hasattr(self.visual_context, 'to_dict'):
            d['visual_context'] = self.visual_context.to_dict()
        return d

    @classmethod
    def from_dict(cls, data: dict) -> "WorldModelState":
        return cls(**data)


UPDATER_PROMPT = """You are the Belief Updating Engine for an autonomous agent.
The agent maintains a structured WORLD MODEL of its environment to avoid re-reasoning.
Below is the CURRENT WORLD MODEL and the RECENT ACTION OUTCOME (Observation).

Your job is to update the World Model based on this new observation.
- Update 'current_context' if the agent navigated to a new page/environment.
- Update 'entities' if new interactive elements or data objects were discovered or changed state.
- Update 'task_phase' based on progress.
- Update 'known_facts' with any newly confirmed information.

Respond strictly in JSON format representing the fully updated World Model:
{
  "current_context": "string",
  "entities": {"key": "value"},
  "task_phase": "string",
  "known_facts": {"fact1": "value1"}
}
"""

class WorldModelUpdater:
    """Updates the agent's world model using an LLM to process observations."""
    
    def __init__(self, llm_registry):
        self._registry = llm_registry

    def update_beliefs(self, current_model: WorldModelState, action: str, observation: str) -> WorldModelState:
        """Process an action and observation to update the world model."""
        if not observation or not observation.strip():
            return current_model

        context = (
            f"CURRENT WORLD MODEL:\n{json.dumps(current_model.to_dict(), indent=2)}\n\n"
            f"RECENT ACTION:\n{action}\n\n"
            f"OBSERVATION:\n{observation[:3000]}\n"
        )

        try:
            resp = self._registry.complete(
                messages=[{"role": "user", "content": context}],
                system_prompt=UPDATER_PROMPT,
                role="reasoning",
                max_tokens=512,
                temperature=0.1,
            )
            if not resp or not resp.content:
                return current_model
            
            # Parse JSON
            text = resp.content.strip()
            if "```json" in text:
                text = text.split("```json")[1].split("```")[0].strip()
            elif "```" in text:
                text = text.split("```")[1].split("```")[0].strip()

            start = text.find("{")
            end = text.rfind("}") + 1
            if start >= 0 and end > start:
                text = text[start:end]

            data = json.loads(text)
            
            # Merge conservatively
            return WorldModelState(
                current_context=data.get("current_context", current_model.current_context),
                entities=data.get("entities", current_model.entities),
                task_phase=data.get("task_phase", current_model.task_phase),
                known_facts=data.get("known_facts", current_model.known_facts),
            )
        except Exception as e:
            logger.warning(f"[WorldModelUpdater] Failed to update beliefs: {e}")
            return current_model
