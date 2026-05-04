import structlog
from typing import Dict, Any, List, Optional
from .base import BaseAdapter

logger = structlog.get_logger("kernell.adapters.router")

class CapabilityRouter:
    """
    The Brain of the Capability Layer.
    Orchestrates execution by finding the most optimal path to solve a task.
    Hierarchy: API (Fast, Reliable) -> Terminal (Powerful) -> GUI (Fallback) -> M2M (Delegation)
    """
    def __init__(self, adapters: Dict[str, BaseAdapter], agent_wallet=None):
        self.adapters = adapters
        self.wallet = agent_wallet

    def _score_adapters(self, task: str) -> List[str]:
        """
        Determines the execution order based on the task semantics.
        This is a heuristic implementation. In a full system, an LLM evaluates this.
        """
        task_lower = task.lower()
        
        # 1. Strong signals for specific layers
        if "click" in task_lower or "screen" in task_lower or "browser" in task_lower:
            # Requires visual context, but we still prefer APIs if possible, though GUI is strongly hinted
            return ["gui", "terminal", "m2m"]
            
        if "pay" in task_lower or "delegate" in task_lower or "ask peer" in task_lower:
            return ["m2m", "terminal", "gui"]

        # 2. Default Optimal Hierarchy:
        # API/Terminal (Code) -> GUI -> M2M
        return ["terminal", "gui", "m2m"]

    def route_and_execute(self, task: str, context: Dict[str, Any]) -> Dict[str, Any]:
        """
        Executes the task using the optimal adapter. Implements intelligent fallback.
        """
        logger.info("capability_routing_started", task=task[:50])
        
        execution_order = self._score_adapters(task)
        
        for adapter_key in execution_order:
            adapter = self.adapters.get(adapter_key)
            if not adapter:
                continue
                
            logger.info("attempting_adapter", adapter=adapter.capability_name)
            
            try:
                result = adapter.execute(task, context)
                
                # Check if execution was successful
                if result.get("status") == "success":
                    logger.info("task_completed_successfully", adapter=adapter.capability_name)
                    
                    # Inject the used adapter into the result for observability (Moltbook Feed)
                    result["used_adapter"] = adapter.capability_name
                    return result
                    
                else:
                    logger.warning("adapter_failed_fallback_triggered", 
                                   adapter=adapter.capability_name, 
                                   reason=result.get("reason", "unknown"))
                    continue # Try the next adapter in the hierarchy
                    
            except Exception as e:
                logger.error("adapter_crash", adapter=adapter.capability_name, error=str(e))
                continue
                
        # If all local and remote fallbacks fail
        return {
            "status": "error",
            "reason": "exhausted_all_capabilities",
            "used_adapter": "none"
        }
