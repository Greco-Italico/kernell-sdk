"""
Kernell OS SDK — Swarm Executor
═══════════════════════════════
Parallel task execution with budget-controlled, Sully-scheduled agents.

Features:
  - Concurrent execution with semaphore limiting
  - Budget reservation before every execution
  - Automatic tier escalation on failure
  - Full telemetry emission for Sully training
"""

import asyncio
import logging
import time
import uuid
from typing import List, Optional

from kernell_sdk.sully.types import (
    TaskFeatures, SullyDecision, ExecutionResult, FinalOutcome, Tier
)
from kernell_sdk.sully.engine import SullyEngine
from kernell_sdk.sully.market import ModelMarketRegistry
from kernell_sdk.swarm.budget import SwarmBudgetManager
from kernell_sdk.observability.event_bus import GLOBAL_EVENT_BUS

logger = logging.getLogger("kernell.swarm.executor")


class SwarmExecutor:
    """
    Orchestrates parallel agent execution with economic constraints.
    
    Flow per subtask:
        1. Sully decides optimal model/tier
        2. Budget Manager reserves funds
        3. Execute with LLM
        4. On failure: escalate tier, re-decide, retry
        5. Commit actual cost
        6. Emit telemetry
    """
    
    def __init__(
        self,
        sully: SullyEngine,
        market: ModelMarketRegistry,
        llm_registry,  # LLMProviderRegistry — actual execution
        max_concurrency: int = 10,
    ):
        self.sully = sully
        self.market = market
        self.llm = llm_registry
        self.max_concurrency = max_concurrency
    
    async def run_swarm(
        self,
        subtasks: List[dict],
        budget: float = 1.0,
    ) -> List[FinalOutcome]:
        """
        Execute multiple subtasks in parallel with budget control.
        
        Each subtask dict should have:
            - "id": str
            - "prompt": str (the actual task)
            - "features": TaskFeatures
        """
        budget_mgr = SwarmBudgetManager(total_budget=budget)
        semaphore = asyncio.Semaphore(self.max_concurrency)
        trace_id = str(uuid.uuid4())
        
        GLOBAL_EVENT_BUS.emit("swarm_started", trace_id, {
            "subtask_count": len(subtasks),
            "budget": budget,
            "max_concurrency": self.max_concurrency,
        })
        
        async def run_one(subtask: dict) -> FinalOutcome:
            async with semaphore:
                return await self._execute_with_escalation(
                    subtask, budget_mgr, trace_id
                )
        
        results = await asyncio.gather(
            *[run_one(st) for st in subtasks],
            return_exceptions=True,
        )
        
        # Convert exceptions to failed outcomes
        outcomes = []
        for r in results:
            if isinstance(r, Exception):
                outcomes.append(FinalOutcome(
                    success=False, total_cost=0, total_latency=0,
                    final_tier=Tier.LOCAL, steps=0,
                    score=-1.0,
                ))
            else:
                outcomes.append(r)
        
        # Emit aggregated telemetry
        successes = sum(1 for o in outcomes if o.success)
        total_cost = sum(o.total_cost for o in outcomes)
        
        GLOBAL_EVENT_BUS.emit("swarm_completed", trace_id, {
            "total_tasks": len(outcomes),
            "successes": successes,
            "success_rate": successes / max(len(outcomes), 1),
            "total_cost": total_cost,
            "budget_remaining": budget_mgr.state.available,
        })
        
        return outcomes
    
    async def _execute_with_escalation(
        self,
        subtask: dict,
        budget_mgr: SwarmBudgetManager,
        trace_id: str,
    ) -> FinalOutcome:
        """Execute a single subtask with automatic tier escalation."""
        features: TaskFeatures = subtask["features"]
        task_id = subtask.get("id", str(uuid.uuid4()))
        prompt = subtask["prompt"]
        
        steps = 0
        total_cost = 0.0
        total_latency = 0.0
        
        while True:
            steps += 1
            
            # 1. Sully decides
            decision = self.sully.decide(features, budget_cap=budget_mgr.state.available)
            
            GLOBAL_EVENT_BUS.emit("sully_subtask_decision", trace_id, {
                "task_id": task_id,
                "step": steps,
                "tier": decision.tier.value,
                "model": decision.model_id,
                "confidence": decision.confidence,
            })
            
            # 2. Reserve budget
            allowed = await budget_mgr.reserve(decision.expected_cost)
            if not allowed:
                return FinalOutcome(
                    success=False, total_cost=total_cost, total_latency=total_latency,
                    final_tier=decision.tier, steps=steps,
                    score=self._compute_score(False, total_cost, total_latency, steps),
                )
            
            # 3. Execute via LLM Registry
            t0 = time.time()
            try:
                # Map Sully's model choice to LLM Registry role
                role = self._tier_to_role(decision.tier)
                response = self.llm.complete(
                    messages=[{"role": "user", "content": prompt}],
                    role=role,
                )
                
                elapsed = (time.time() - t0) * 1000
                actual_cost = decision.expected_cost  # approximate for now
                
                if response and response.content:
                    await budget_mgr.commit(actual_cost, decision.expected_cost)
                    total_cost += actual_cost
                    total_latency += elapsed
                    
                    # Update market quality score
                    self.market.update_quality_score(decision.model_id, True)
                    
                    outcome = FinalOutcome(
                        success=True, total_cost=total_cost, total_latency=total_latency,
                        final_tier=decision.tier, steps=steps,
                        score=self._compute_score(True, total_cost, total_latency, steps),
                    )
                    
                    # Emit training sample
                    self._emit_training_sample(trace_id, task_id, features, decision, outcome)
                    
                    return outcome
                else:
                    # Response failed
                    await budget_mgr.release(decision.expected_cost)
                    total_latency += elapsed
                    self.market.update_quality_score(decision.model_id, False)
                    
            except Exception as e:
                elapsed = (time.time() - t0) * 1000
                await budget_mgr.release(decision.expected_cost)
                total_latency += elapsed
                logger.warning(f"[Swarm] Task {task_id} failed on {decision.model_id}: {e}")
            
            # 4. Escalate
            next_tier = self.sully.next_tier(decision.tier)
            if not next_tier:
                outcome = FinalOutcome(
                    success=False, total_cost=total_cost, total_latency=total_latency,
                    final_tier=decision.tier, steps=steps,
                    score=self._compute_score(False, total_cost, total_latency, steps),
                )
                self._emit_training_sample(trace_id, task_id, features, decision, outcome)
                return outcome
            
            GLOBAL_EVENT_BUS.emit("sully_escalation", trace_id, {
                "task_id": task_id,
                "from_tier": decision.tier.value,
                "to_tier": next_tier.value,
            })
            
            features.history_failures += 1
    
    def _tier_to_role(self, tier: Tier) -> str:
        """Map Sully tier to LLMProviderRegistry role."""
        return {
            Tier.LOCAL: "local_only",
            Tier.ECONOMIC: "economy",
            Tier.PREMIUM: "premium",
        }.get(tier, "default")
    
    def _compute_score(self, success: bool, cost: float, latency: float, retries: int) -> float:
        """Reward function for Sully training. Higher = better decision."""
        return (
            (1.0 if success else 0.0)
            - 0.3 * cost
            - 0.1 * (latency / 5000)  # normalize to ~1.0
            - 0.2 * (retries - 1)     # penalize escalations
        )
    
    def _emit_training_sample(
        self,
        trace_id: str,
        task_id: str,
        features: TaskFeatures,
        decision: SullyDecision,
        outcome: FinalOutcome,
    ):
        """Emit a structured training example for next Sully fine-tuning cycle."""
        GLOBAL_EVENT_BUS.emit("sully_training_sample", trace_id, {
            "task_id": task_id,
            "input": {
                "task_type": features.task_type,
                "complexity": features.ui_complexity,
                "estimated_tokens": features.estimated_tokens,
                "requires_auth": features.requires_auth,
                "history_failures": features.history_failures,
            },
            "decision": {
                "tier": decision.tier.value,
                "model": decision.model_id,
                "confidence": decision.confidence,
                "expected_latency": decision.expected_latency,
                "shadow_decision": decision.shadow_decision,
            },
            "outcome": {
                "success": outcome.success,
                "total_cost": outcome.total_cost,
                "total_latency": outcome.total_latency,
                "final_tier": outcome.final_tier.value,
                "steps": outcome.steps,
                "score": outcome.score,
            },
        })
