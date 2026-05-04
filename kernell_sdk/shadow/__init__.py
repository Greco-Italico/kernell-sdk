"""
Kernell OS SDK — Shadow Mode Package
═════════════════════════════════════
Zero-risk observation layer that intercepts LLM API calls,
lets them pass through unchanged, and records counterfactual
routing decisions for the FinOps Dashboard.

The user's production is NEVER affected. We only observe.
"""
from .proxy import ShadowProxy, ShadowConfig, ShadowEvent

__all__ = ["ShadowProxy", "ShadowConfig", "ShadowEvent"]
