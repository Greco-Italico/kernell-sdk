"""
Kernell OS SDK — Sully Training Pipeline
════════════════════════════════════════
Closes the learning loop by automatically dumping telemetry events into
ready-to-train JSONL datasets for LoRA fine-tuning.

Pipeline:
  Collector (Event Bus) → Buffer → Curator (Filter/Balance) → Formatter (Instruct) → JSONL
"""

import json
import logging
import os
import time
from typing import Dict, Any, List

from kernell_sdk.observability.event_bus import GLOBAL_EVENT_BUS

logger = logging.getLogger("kernell.sully.training")


class TrainingPipeline:
    """
    Listens to execution events, computes Reward V2, and dumps structured datasets.
    """
    
    def __init__(self, output_dir: str = "/home/anny/.gemini/antigravity/dataset"):
        self.output_dir = output_dir
        os.makedirs(self.output_dir, exist_ok=True)
        
        self.sully_file = os.path.join(self.output_dir, "sully.jsonl")
        self.consensus_file = os.path.join(self.output_dir, "consensus.jsonl")
        self.decomposer_file = os.path.join(self.output_dir, "decomposer.jsonl")
        
        # Buffer for events to combine them (e.g., waiting for consensus)
        self._pending_decisions: Dict[str, Dict[str, Any]] = {}
        
        self._subscribe()
        logger.info(f"[TrainingPipeline] Initialized. Dumping to {self.output_dir}")

    def _subscribe(self):
        GLOBAL_EVENT_BUS.subscribe(self._handle_event)

    def _handle_event(self, event):
        """Route events to their respective collectors."""
        self._flush_timeouts()
        if event.type == "sully_training_sample":
            self._handle_sully_sample(event.agent_id, event.payload)
        elif event.type == "consensus_resolved":
            self._handle_consensus(event.agent_id, event.payload)
        elif event.type == "decomposition_training_sample":
            self._handle_decomposer(event.agent_id, event.payload)

    # ── Reward V2 & Sully Collector ──────────────────────────────────
    
    def _handle_sully_sample(self, trace_id: str, payload: dict):
        """Buffer the sample to wait for potential consensus feedback."""
        task_id = payload.get("task_id", trace_id)
        
        # Store basic sample with timestamp
        self._pending_decisions[task_id] = {
            "payload": payload,
            "timestamp": time.time(),
            "trace_id": trace_id
        }
        
        # Clean up stale buffers
        self._flush_timeouts()

    def _handle_consensus(self, trace_id: str, consensus_payload: dict):
        """Update recent decisions with consensus reward and dump to consensus.jsonl."""
        # Dump to consensus.jsonl
        self._append_jsonl(self.consensus_file, consensus_payload)
        
        # Apply Reward V2 to any pending sully samples from this trace
        # trace_id is usually the same for all subtasks in a swarm run.
        # But task_id might be different. Let's just update all pending for this trace_id.
        # For simplicity, we can calculate the consensus reward bonus here.
        method = consensus_payload.get("method", "")
        agreement = consensus_payload.get("agreement_score", 0.0)
        confidence = consensus_payload.get("confidence", 0.0)
        
        # Find all matching tasks
        for task_id, data in list(self._pending_decisions.items()):
            if data["trace_id"] == trace_id:
                payload = data["payload"]
                
                # Apply Reward V2
                outcome = payload.get("outcome", {})
                success = outcome.get("success", False)
                cost = outcome.get("total_cost", 0.0)
                latency = outcome.get("total_latency", 0.0)
                steps = outcome.get("steps", 1)
                
                decision = payload.get("decision", {})
                expected_latency = decision.get("expected_latency", 5000.0)
                latency_norm = min(latency / max(expected_latency, 1.0), 2.0)
                
                # REWARD V2 formula:
                reward = (
                    (1.0 if success else 0.0)
                    + (agreement * 0.5)
                    + (confidence * 0.3)
                    - (cost * 0.3)
                    - (latency_norm * 0.1)
                    - ((steps - 1) * 0.2)
                )
                
                if method == "unanimous":
                    reward += 0.2
                elif method == "judge":
                    reward -= 0.1
                    
                payload["outcome"]["score"] = reward
                payload["outcome"]["reward_version"] = "v2"
                
                # Overwrite the sample with V2 reward
                self._flush_sully(task_id, payload)
                
                # Clean up
                del self._pending_decisions[task_id]
                
    def _flush_timeouts(self, timeout=2.0):
        """Flush samples that never received consensus."""
        now = time.time()
        for task_id, data in list(self._pending_decisions.items()):
            if now - data["timestamp"] > timeout:
                payload = data["payload"]
                
                # Fallback Reward v1 calculation
                outcome = payload.get("outcome", {})
                success = outcome.get("success", False)
                cost = outcome.get("total_cost", 0.0)
                latency = outcome.get("total_latency", 0.0)
                steps = outcome.get("steps", 1)
                
                decision = payload.get("decision", {})
                expected_latency = decision.get("expected_latency", 5000.0)
                latency_norm = min(latency / max(expected_latency, 1.0), 2.0)
                
                reward = (
                    (1.0 if success else 0.0)
                    - (cost * 0.3)
                    - (latency_norm * 0.1)
                    - ((steps - 1) * 0.2)
                )
                
                payload["outcome"]["score"] = reward
                payload["outcome"]["reward_version"] = "v1_fallback"
                
                self._flush_sully(task_id, payload)
                del self._pending_decisions[task_id]

    def _flush_sully(self, task_id: str, payload: dict, override: bool = False):
        """Curate and format Sully samples."""
        # Curate: Filter garbage
        outcome = payload.get("outcome", {})
        score = outcome.get("score", -1.0)
        
        # Allow failures through so the model learns what NOT to do, 
        # but filter out absolute garbage/aborts without signal.
        if score < -2.0:
            return  # Total failure without learning signal
            
        # Format for Instruct (LoRA format)
        features = payload.get("input", {})
        decision = payload.get("decision", {})
        
        instruct_sample = {
            "instruction": "Route the following task to the optimal model tier.",
            "input": json.dumps(features),
            "output": json.dumps({
                "tier": decision.get("tier"),
                "model": decision.get("model")
            }),
            "reward": round(score, 4),
            "timestamp": time.time(),
            "trace_id": payload.get("trace_id", ""),
            "task_id": task_id
        }
        
        self._append_jsonl(self.sully_file, instruct_sample)
        
        # --- Shadow Deployment Logging ---
        if decision.get("shadow_decision"):
            shadow_sample = {
                "input": features,
                "prod_decision": {
                    "tier": decision.get("tier"),
                    "model": decision.get("model"),
                    "expected_latency": decision.get("expected_latency")
                },
                "shadow_decision": decision.get("shadow_decision"),
                "prod_outcome": {
                    "score": score,
                    "success": outcome.get("success"),
                    "total_cost": outcome.get("total_cost"),
                    "total_latency": outcome.get("total_latency"),
                },
                "timestamp": time.time(),
                "trace_id": payload.get("trace_id", ""),
                "task_id": task_id
            }
            shadow_file = os.path.join(self.output_dir, "shadow_eval.jsonl")
            self._append_jsonl(shadow_file, shadow_sample)

    # ── Decomposer Collector ─────────────────────────────────────────

    def _handle_decomposer(self, trace_id: str, payload: dict):
        """Curate and format Decomposer samples."""
        instruct_sample = {
            "instruction": "Decompose the following task into a DAG.",
            "input": json.dumps(payload.get("input", {})),
            "output": "DAG layout hidden in v0, used for success tracking.",
            "reward": payload.get("outcome", {}).get("success_rate", 0.0),
            "timestamp": time.time()
        }
        self._append_jsonl(self.decomposer_file, instruct_sample)

    # ── Utilities ────────────────────────────────────────────────────

    def _append_jsonl(self, filepath: str, data: dict):
        try:
            with open(filepath, "a", encoding="utf-8") as f:
                f.write(json.dumps(data) + "\n")
        except Exception as e:
            logger.error(f"[TrainingPipeline] Error writing to {filepath}: {e}")

# Automatically initialize
pipeline = TrainingPipeline()
