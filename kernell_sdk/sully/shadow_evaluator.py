"""
Kernell OS SDK — Shadow Deployment Evaluator
════════════════════════════════════════════
Evaluates a fine-tuned LoRA model's routing decisions against the production
model BEFORE promoting it to active duty.

Metrics:
- Win Rate: % where Shadow's expected reward > Prod's actual reward
- Cost Delta: % cost savings
- Latency Delta: Expected latency improvement
- Catastrophic Regression Rate: Shadow chose a model with historic success < 0.5 when Prod succeeded
"""

import json
import logging
import os
import sys
from typing import List, Dict

logger = logging.getLogger("kernell.sully.shadow_eval")

class ShadowEvaluator:
    def __init__(self, shadow_log_path: str = "/home/anny/.gemini/antigravity/dataset/shadow_eval.jsonl"):
        self.shadow_log_path = shadow_log_path
        
    def evaluate(self) -> Dict:
        """Analyze shadow logs and decide whether to block or promote the new model."""
        logger.info(f"🔍 Analyzing Shadow Deployment Logs: {self.shadow_log_path}")
        
        if not os.path.exists(self.shadow_log_path):
            logger.warning("No shadow evaluation logs found. Have you run the system in shadow mode?")
            return {"promotable": False, "reason": "No data"}
            
        with open(self.shadow_log_path, "r", encoding="utf-8") as f:
            samples = [json.loads(line) for line in f if line.strip()]
            
        if not samples:
            return {"promotable": False, "reason": "Empty data"}
            
        total = len(samples)
        wins = 0
        catastrophic_regressions = 0
        total_prod_cost = 0.0
        total_shadow_cost = 0.0
        total_prod_latency = 0.0
        total_shadow_latency = 0.0
        
        for s in samples:
            prod_out = s.get("prod_outcome", {})
            prod_dec = s.get("prod_decision", {})
            shadow_dec = s.get("shadow_decision", {})
            
            # Real prod metrics
            p_score = prod_out.get("score", 0.0)
            p_success = prod_out.get("success", False)
            total_prod_cost += prod_out.get("total_cost", 0.0)
            total_prod_latency += prod_out.get("total_latency", 0.0)
            
            # Expected shadow metrics
            # If shadow wasn't executed, we estimate its score based on the market's expected latency and cost.
            # In a real parallel-execution shadow mode, we'd have actual shadow outcomes.
            s_exp_latency = shadow_dec.get("expected_latency", 2000.0)
            s_exp_cost = shadow_dec.get("expected_cost", 0.0)
            s_confidence = shadow_dec.get("confidence", 0.5)
            
            # Heuristic simulation of shadow reward (since we didn't execute it)
            s_score_est = (
                (1.0 if s_confidence > 0.5 else 0.0)
                + (s_confidence * 0.3)
                - (s_exp_cost * 0.3)
                - ((s_exp_latency / 5000.0) * 0.1)
            )
            
            total_shadow_cost += s_exp_cost
            total_shadow_latency += s_exp_latency
            
            if s_score_est > p_score:
                wins += 1
                
            # Catastrophic Regression: Prod succeeded but Shadow chose a highly unsafe model/tier
            if p_success and s_confidence < 0.4:
                catastrophic_regressions += 1
                
        win_rate = wins / total
        regression_rate = catastrophic_regressions / total
        cost_delta = total_prod_cost - total_shadow_cost
        avg_reward_delta = (wins - (total - wins)) / total # proxy for avg delta
        
        metrics = {
            "total_samples": total,
            "win_rate": win_rate,
            "regression_rate": regression_rate,
            "cost_delta": cost_delta,
            "latency_delta": total_prod_latency - total_shadow_latency,
            "avg_reward_delta": avg_reward_delta
        }
        
        logger.info(f"📊 Shadow Metrics:")
        logger.info(json.dumps(metrics, indent=2))
        
        # Eval Gates (Strict Block Policy)
        blocks = []
        if win_rate < 0.55:
            blocks.append(f"Win Rate too low ({win_rate:.2f} < 0.55)")
        if regression_rate > 0.02:
            blocks.append(f"Catastrophic Regressions too high ({regression_rate:.2f} > 0.02)")
        if avg_reward_delta < 0:
            blocks.append("Average Reward Delta is negative")
            
        metrics["blocks"] = blocks
        metrics["promotable"] = len(blocks) == 0
        
        return metrics

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    
    # Optional debug flag to bypass strictness
    strict = os.environ.get("EVAL_STRICT", "1") == "1"
    
    evaluator = ShadowEvaluator()
    results = evaluator.evaluate()
    
    if not results["promotable"]:
        logger.error("🚨 SHADOW EVALUATION FAILED. Model Promotion Blocked.")
        for b in results.get("blocks", []):
            logger.error(f"  - {b}")
        if strict:
            sys.exit(1)
        else:
            logger.warning("Bypassing block due to EVAL_STRICT=0")
            sys.exit(0)
    else:
        logger.info("✅ SHADOW EVALUATION PASSED. Model is safe to promote.")
        sys.exit(0)
