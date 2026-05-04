"""
Kernell OS SDK — Hybrid Interaction Router (Phase 7c)
═════════════════════════════════════════════════════
Deterministic policy layer that maps abstract agent intentions 
(e.g., "click 'Buy'") into physical or DOM-based execution paths.

Capabilities:
  - Decouples Intent from Implementation.
  - Fallback logic: DOM (reliable) -> Vision/OS (flexible) -> Replan.
  - Confidence scoring to prevent wild guesses.
"""

import logging
from dataclasses import dataclass
from typing import Dict, Any, Optional

from kernell_sdk.observability.event_bus import GLOBAL_EVENT_BUS

logger = logging.getLogger("kernell.agent.interaction")

@dataclass
class RoutedAction:
    tool_name: str
    args: Dict[str, Any]
    confidence: float
    fallback_strategy: str
    reasoning: str

class InteractionRouter:
    """
    Decides HOW to execute a UI interaction based on the World Model and active tools.
    """
    def __init__(self, browser_controller=None, os_controller=None):
        self.browser = browser_controller
        self.os = os_controller

    def route(self, intent: str, target: str, text: str = "", world_model=None) -> RoutedAction:
        """
        Routes an abstract intent (click, type) to a concrete tool (browser_*, os_*).
        Returns a RoutedAction with confidence score.
        """
        intent = intent.lower()
        target = target.lower()

        # 1. Attempt DOM Mapping (Highest Confidence)
        if self.browser:
            # Check if Playwright has the element in its known DOM map (simulated check)
            # In a real impl, we'd check the browser's active element tree or state.
            dom_element_found = False
            selector = ""
            
            # Simple heuristic: if the world_model entities explicitly list this as a DOM node
            if world_model and "dom_nodes" in world_model.entities:
                for node in world_model.entities["dom_nodes"]:
                    if target in node.get("label", "").lower() or target in node.get("id", "").lower():
                        dom_element_found = True
                        selector = node.get("selector", f"text={target}")
                        break
            else:
                # If we have browser, we assume we can attempt a text selector
                dom_element_found = True
                selector = f"text={target}"

            if dom_element_found:
                if intent == "click":
                    route = RoutedAction(
                        tool_name="browser_click",
                        args={"selector": selector},
                        confidence=0.95,
                        fallback_strategy="vision_os",
                        reasoning="DOM selector available, highly reliable."
                    )
                elif intent == "type":
                    route = RoutedAction(
                        tool_name="browser_type",
                        args={"selector": selector, "text": text},
                        confidence=0.95,
                        fallback_strategy="vision_os",
                        reasoning="DOM input available, highly reliable."
                    )
                    
                if route:
                    GLOBAL_EVENT_BUS.emit("interaction_routed", "current", {
                        "intent": intent, "target": target, "route": route.tool_name, 
                        "confidence": route.confidence, "dom_match": True, "vision_match": False
                    })
                    return route

        # 2. Attempt Vision + OS Mapping (Medium Confidence)
        if self.os and world_model and world_model.visual_context:
            visual_el = world_model.visual_context.find_element(target)
            if visual_el:
                cx, cy = visual_el.center()
                conf = visual_el.confidence * 0.9  # OS is inherently slightly less reliable than DOM
                
                if intent == "click":
                    route = RoutedAction(
                        tool_name="os_click",
                        args={"x": cx, "y": cy},
                        confidence=conf,
                        fallback_strategy="replan",
                        reasoning=f"Found visual element '{visual_el.label}' via Vision."
                    )
                elif intent == "type":
                    route = RoutedAction(
                        tool_name="os_type",
                        args={"text": text},
                        confidence=conf,
                        fallback_strategy="replan",
                        reasoning="Using OS native typing."
                    )
                    
                if route:
                    GLOBAL_EVENT_BUS.emit("interaction_routed", "current", {
                        "intent": intent, "target": target, "route": route.tool_name, 
                        "confidence": route.confidence, "dom_match": False, "vision_match": True
                    })
                    return route

        # 3. Fallback: Cannot resolve
        route = RoutedAction(
            tool_name="unknown",
            args={},
            confidence=0.0,
            fallback_strategy="abort",
            reasoning=f"Could not map intent '{intent}' on target '{target}' to DOM or Vision."
        )
        GLOBAL_EVENT_BUS.emit("interaction_routed", "current", {
            "intent": intent, "target": target, "route": route.tool_name, 
            "confidence": route.confidence, "dom_match": False, "vision_match": False
        })
        return route
