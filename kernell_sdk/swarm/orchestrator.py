"""
Kernell OS SDK — Swarm Orchestrator
═══════════════════════════════════
The unified entry point for autonomous task execution.

This is the "Manus-killer": a single call that decomposes, schedules,
executes in parallel waves, resolves consensus, and returns truth.

Pipeline:
    Task → Decompose → Schedule (Sully) → Execute (Waves) → Consensus → Output

Features:
  - DAG-aware wave execution (respects dependencies)
  - Per-subtask Sully routing (independent tier decisions)
  - Per-wave consensus resolution
  - Global budget enforcement
  - Full telemetry for Sully training
"""

import asyncio
import logging
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from kernell_sdk.sully.types import TaskFeatures, FinalOutcome, Tier
from kernell_sdk.sully.engine import SullyEngine
from kernell_sdk.sully.market import ModelMarketRegistry
from kernell_sdk.swarm.decomposer import TaskDecomposer, TaskDAG, SubTask
from kernell_sdk.swarm.executor import SwarmExecutor
from kernell_sdk.swarm.consensus import ConsensusEngine, Candidate
from kernell_sdk.swarm.budget import SwarmBudgetManager
from kernell_sdk.observability.event_bus import GLOBAL_EVENT_BUS

logger = logging.getLogger("kernell.swarm.orchestrator")


@dataclass
class OrchestratorResult:
    """Final result from the full orchestration pipeline."""
    success: bool
    output: str
    confidence: float
    total_cost: float
    total_latency: float
    subtasks_total: int
    subtasks_succeeded: int
    waves_executed: int
    consensus_method: str
    trace_id: str = ""


class SwarmOrchestrator:
    """
    The complete autonomous execution pipeline.
    
    One call: task in, trustworthy answer out.
    
    Usage:
        orchestrator = SwarmOrchestrator(sully, market, llm_registry)
        result = await orchestrator.execute(
            "Scrape pricing from 3 competitors and compare tiers",
            features=TaskFeatures(task_type="web_scraping", ...),
            budget=0.10,
        )
        print(result.output, result.confidence)
    """
    
    def __init__(
        self,
        sully: SullyEngine,
        market: ModelMarketRegistry,
        llm_registry,
        decomposer: Optional[TaskDecomposer] = None,
        consensus: Optional[ConsensusEngine] = None,
        max_concurrency: int = 10,
    ):
        self.sully = sully
        self.market = market
        self.llm = llm_registry
        self.decomposer = decomposer or TaskDecomposer()
        self.consensus = consensus or ConsensusEngine(llm_registry=llm_registry)
        self.max_concurrency = max_concurrency
    
    async def execute(
        self,
        task: str,
        features: TaskFeatures,
        budget: float = 1.0,
    ) -> OrchestratorResult:
        """
        Full autonomous pipeline:
            1. Decompose task into DAG
            2. Execute waves in dependency order
            3. Resolve consensus per wave
            4. Return unified result
        """
        trace_id = str(uuid.uuid4())
        t0 = time.time()
        
        GLOBAL_EVENT_BUS.emit("orchestrator_started", trace_id, {
            "task": task[:200],
            "task_type": features.task_type,
            "budget": budget,
        })
        
        # ── Step 1: Decompose ────────────────────────────────────────
        dag = self.decomposer.decompose(task, features, budget)
        waves = dag.execution_waves
        
        # ── Step 2: Execute waves ────────────────────────────────────
        budget_mgr = SwarmBudgetManager(total_budget=budget)
        semaphore = asyncio.Semaphore(self.max_concurrency)
        
        wave_results: Dict[str, str] = {}  # subtask_id → output
        all_outcomes: List[FinalOutcome] = []
        waves_executed = 0
        
        for wave_idx, wave in enumerate(waves):
            waves_executed += 1
            
            GLOBAL_EVENT_BUS.emit("orchestrator_wave_start", trace_id, {
                "wave": wave_idx,
                "subtask_count": len(wave),
                "subtask_ids": [st.id for st in wave],
            })
            
            # Inject context from previous waves into prompts
            enriched_wave = self._enrich_with_context(wave, wave_results)
            
            # Execute wave in parallel
            wave_outcomes = await self._execute_wave(
                enriched_wave, budget_mgr, semaphore, trace_id
            )
            
            # Collect results
            for st, outcome in zip(wave, wave_outcomes):
                all_outcomes.append(outcome)
                if outcome.success and outcome.output:
                    wave_results[st.id] = str(outcome.output)
            
            GLOBAL_EVENT_BUS.emit("orchestrator_wave_complete", trace_id, {
                "wave": wave_idx,
                "successes": sum(1 for o in wave_outcomes if o.success),
                "total": len(wave_outcomes),
            })
        
        # ── Step 3: Final consensus ──────────────────────────────────
        candidates = [
            Candidate(
                output=str(o.output or ""),
                confidence=max(o.score, 0.1),
                cost=o.total_cost,
                latency=o.total_latency,
                task_id=st.id,
            )
            for st, o in zip(dag.subtasks, all_outcomes)
            if o.success and o.output
        ]
        
        if candidates:
            # For multi-step tasks, concatenate in order rather than voting
            if len(waves) > 1:
                # Sequential DAG: combine outputs in execution order
                final_output = self._synthesize_outputs(dag, wave_results)
                consensus_result_method = "sequential_synthesis"
                consensus_confidence = sum(c.confidence for c in candidates) / len(candidates)
            else:
                # Single wave: use voting consensus
                consensus_result = self.consensus.resolve(
                    candidates, budget_remaining=budget_mgr.state.available
                )
                final_output = consensus_result.output
                consensus_result_method = consensus_result.method
                consensus_confidence = consensus_result.confidence
        else:
            final_output = ""
            consensus_result_method = "no_candidates"
            consensus_confidence = 0.0
        
        # ── Step 4: Build result ─────────────────────────────────────
        elapsed = (time.time() - t0) * 1000
        successes = sum(1 for o in all_outcomes if o.success)
        total_cost = sum(o.total_cost for o in all_outcomes)
        
        result = OrchestratorResult(
            success=successes > 0,
            output=final_output,
            confidence=consensus_confidence,
            total_cost=total_cost,
            total_latency=elapsed,
            subtasks_total=len(dag.subtasks),
            subtasks_succeeded=successes,
            waves_executed=waves_executed,
            consensus_method=consensus_result_method,
            trace_id=trace_id,
        )
        
        GLOBAL_EVENT_BUS.emit("orchestrator_completed", trace_id, {
            "success": result.success,
            "subtasks": f"{successes}/{len(dag.subtasks)}",
            "waves": waves_executed,
            "total_cost": total_cost,
            "confidence": consensus_confidence,
            "consensus_method": consensus_result_method,
            "latency_ms": elapsed,
        })
        
        # Emit training sample for the decomposition quality
        GLOBAL_EVENT_BUS.emit("decomposition_training_sample", trace_id, {
            "input": {
                "task_type": features.task_type,
                "complexity": features.ui_complexity,
                "subtask_count": len(dag.subtasks),
                "wave_count": len(waves),
            },
            "outcome": {
                "success_rate": successes / max(len(dag.subtasks), 1),
                "total_cost": total_cost,
                "confidence": consensus_confidence,
                "consensus_method": consensus_result_method,
            },
        })
        
        return result
    
    # ── Wave Execution ───────────────────────────────────────────────
    
    async def _execute_wave(
        self,
        subtasks: List[SubTask],
        budget_mgr: SwarmBudgetManager,
        semaphore: asyncio.Semaphore,
        trace_id: str,
    ) -> List[FinalOutcome]:
        """Execute all subtasks in a wave concurrently."""
        
        async def run_one(st: SubTask) -> FinalOutcome:
            async with semaphore:
                return await self._execute_subtask(st, budget_mgr, trace_id)
        
        results = await asyncio.gather(
            *[run_one(st) for st in subtasks],
            return_exceptions=True,
        )
        
        outcomes = []
        for r in results:
            if isinstance(r, Exception):
                logger.error(f"[Orchestrator] Subtask exception: {r}")
                outcomes.append(FinalOutcome(
                    success=False, total_cost=0, total_latency=0,
                    final_tier=Tier.LOCAL, steps=0, score=-1.0,
                ))
            else:
                outcomes.append(r)
        
        return outcomes
    
    async def _execute_subtask(
        self,
        subtask: SubTask,
        budget_mgr: SwarmBudgetManager,
        trace_id: str,
    ) -> FinalOutcome:
        """Execute a single subtask with Sully routing and escalation."""
        features = subtask.features
        
        # Sully decides routing for this specific subtask
        decision = self.sully.decide(features, budget_cap=budget_mgr.state.available)
        
        # Reserve budget
        allowed = await budget_mgr.reserve(decision.expected_cost)
        if not allowed:
            return FinalOutcome(
                success=False, total_cost=0, total_latency=0,
                final_tier=decision.tier, steps=0, score=-0.5,
            )
        
        # Execute
        t0 = time.time()
        role = {Tier.LOCAL: "local_only", Tier.ECONOMIC: "economy", Tier.PREMIUM: "premium"}.get(decision.tier, "default")
        
        try:
            response = self.llm.complete(
                messages=[{"role": "user", "content": subtask.prompt}],
                role=role,
            )
            
            elapsed = (time.time() - t0) * 1000
            
            if response and response.content:
                await budget_mgr.commit(decision.expected_cost, decision.expected_cost)
                self.market.update_quality_score(decision.model_id, True)
                
                return FinalOutcome(
                    success=True,
                    total_cost=decision.expected_cost,
                    total_latency=elapsed,
                    final_tier=decision.tier,
                    steps=1,
                    score=1.0 - 0.3 * decision.expected_cost - 0.1 * (elapsed / 5000),
                    output=response.content,
                )
            else:
                await budget_mgr.release(decision.expected_cost)
                self.market.update_quality_score(decision.model_id, False)
                
                return FinalOutcome(
                    success=False, total_cost=0, total_latency=elapsed,
                    final_tier=decision.tier, steps=1, score=-0.3,
                )
                
        except Exception as e:
            elapsed = (time.time() - t0) * 1000
            await budget_mgr.release(decision.expected_cost)
            logger.warning(f"[Orchestrator] Subtask {subtask.id} failed: {e}")
            return FinalOutcome(
                success=False, total_cost=0, total_latency=elapsed,
                final_tier=decision.tier, steps=1, score=-0.5,
            )
    
    # ── Context Enrichment ───────────────────────────────────────────
    
    def _enrich_with_context(
        self,
        wave: List[SubTask],
        previous_results: Dict[str, str],
    ) -> List[SubTask]:
        """Inject outputs from completed dependencies into subtask prompts."""
        for st in wave:
            if st.dependencies:
                context_parts = []
                for dep_id in st.dependencies:
                    if dep_id in previous_results:
                        context_parts.append(
                            f"[Result from {dep_id}]:\n{previous_results[dep_id][:2000]}"
                        )
                
                if context_parts:
                    context = "\n\n".join(context_parts)
                    st.prompt = f"{st.prompt}\n\n[Context from previous steps]:\n{context}"
        
        return wave
    
    # ── Output Synthesis ─────────────────────────────────────────────
    
    def _synthesize_outputs(
        self,
        dag: TaskDAG,
        results: Dict[str, str],
    ) -> str:
        """Combine outputs from multi-wave execution in dependency order."""
        parts = []
        for st in dag.subtasks:
            if st.id in results:
                parts.append(results[st.id])
        
        return "\n\n---\n\n".join(parts)
