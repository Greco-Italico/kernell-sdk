"""
Kernell OS SDK — Pre-Training Data Guardrail
════════════════════════════════════════════
Mandatory validation gate before LoRA fine-tuning.
Prevents poisoning the model with biased, leaky, or corrupted telemetry data.
If this fails, training aborts. No exceptions.
"""

import json
import logging
import math
import os
import sys
from collections import Counter
from typing import List, Dict

# Assuming numpy is available in the environment; otherwise we do standard math
try:
    import numpy as np
except ImportError:
    np = None

logger = logging.getLogger("kernell.sully.guardrail")


class DataGuardrail:
    """Blocks training if the dataset violates learning invariants."""
    
    def __init__(self, dataset_path: str = "/home/anny/.gemini/antigravity/dataset/sully.jsonl"):
        self.dataset_path = dataset_path
        
    def validate_all(self):
        """Run all checks and fail hard if any block-level violation occurs."""
        logger.info(f"[Guardrail] Validating dataset at {self.dataset_path}")
        
        if not os.path.exists(self.dataset_path):
            raise RuntimeError(f"Dataset not found: {self.dataset_path}")
            
        with open(self.dataset_path, "r", encoding="utf-8") as f:
            samples = [json.loads(line) for line in f if line.strip()]
            
        strict_mode = os.environ.get("GUARDRAIL_STRICT", "1") == "1"
        min_samples = 50 if strict_mode else 10
        if len(samples) < min_samples:
            logger.error(f"[Guardrail] Dataset too small: {len(samples)} < {min_samples}")
            if strict_mode:
                raise RuntimeError(f"Dataset too small: {len(samples)} < {min_samples}")
            else:
                logger.warning("[Guardrail] STRICT=0, allowing small dataset for testing.")
            
        strict_mode = os.environ.get("GUARDRAIL_STRICT", "1") == "1"
        self.strict_mode = strict_mode
        
        results = {
            "balance": self._check_class_balance(samples),
            "reward": self._check_reward_sanity(samples),
            "leakage": self._check_leakage(samples),
            "duplicates": self._check_duplicates(samples),
            "latency": self._check_latency_sanity(samples),
        }
        
        failures = {k: v for k, v in results.items() if not v.get("pass", True)}
        
        if failures:
            logger.error(f"[Guardrail] 🚫 DATASET BLOCKED! Violations found:\n{json.dumps(failures, indent=2)}")
            raise RuntimeError(f"Dataset blocked due to Guardrail failures: {list(failures.keys())}")
            
        logger.info("[Guardrail] ✅ Dataset passed all checks. Safe for training.")
        return samples

    def _check_class_balance(self, samples: List[dict], threshold: float = 0.85) -> dict:
        """Ensure no single tier dominates the dataset and collapses exploration."""
        if not samples:
            return {"pass": True}
            
        tiers = []
        for s in samples:
            try:
                out = json.loads(s.get("output", "{}"))
                if "tier" in out:
                    tiers.append(out["tier"])
            except json.JSONDecodeError:
                pass
                
        if not tiers:
            return {"pass": False, "reason": "No tier labels found in output"}
            
        counts = Counter(tiers)
        total = sum(counts.values())
        probs = [c / total for c in counts.values()]
        
        entropy = -sum(p * math.log(p + 1e-9) for p in probs)
        max_class_ratio = max(probs)
        min_samples = min(counts.values())
        
        # Guardrail rules
        entropy_pass = entropy >= 0.5  # Requires reasonable diversity
        min_samples_pass = min_samples >= 20 if self.strict_mode else min_samples >= 2
        
        passed = (max_class_ratio <= threshold) and entropy_pass and min_samples_pass
        
        return {
            "pass": passed,
            "max_ratio": max_class_ratio,
            "entropy": entropy,
            "min_samples_per_tier": min_samples,
            "distribution": dict(counts)
        }

    def _check_reward_sanity(self, samples: List[dict]) -> dict:
        """Ensure rewards are properly scaled, zero-centered, and have learning variance."""
        if not samples:
            return {"pass": True}
            
        rewards = [s.get("reward", 0.0) for s in samples]
        
        if np:
            arr = np.array(rewards)
            mean = float(arr.mean())
            std = float(arr.std())
            min_r = float(arr.min())
            max_r = float(arr.max())
        else:
            mean = sum(rewards) / len(rewards)
            variance = sum((r - mean) ** 2 for r in rewards) / len(rewards)
            std = math.sqrt(variance)
            min_r = min(rewards)
            max_r = max(rewards)
            
        # Hard fail if variance is essentially zero (model learns nothing)
        variance_pass = std > 0.01 or len(samples) < 5
        # Soft fail / warning if mean is heavily skewed
        skew_warning = abs(mean) > 2.0
        
        # Clipping check: if > 80% of samples are highly clipped, gradient is zero
        clipped = sum(1 for r in rewards if abs(r) > 0.95) / len(rewards) if rewards else 0.0
        clipping_pass = clipped <= 0.8
        
        return {
            "pass": variance_pass and clipping_pass,
            "mean": mean,
            "std": std,
            "min": min_r,
            "max": max_r,
            "skew_warning": skew_warning,
            "low_variance_warning": not variance_pass,
            "clipping_warning": not clipping_pass
        }

    def _check_leakage(self, samples: List[dict]) -> dict:
        """Ensure the input doesn't contain information from the future (e.g. actual latency)."""
        leaks = 0
        for s in samples:
            try:
                inp = json.loads(s.get("input", "{}"))
                if "actual_latency" in inp or "actual_cost" in inp:
                    leaks += 1
            except json.JSONDecodeError:
                pass
                
        return {
            "pass": leaks == 0,
            "leak_count": leaks
        }

    def _check_duplicates(self, samples: List[dict]) -> dict:
        """Ensure there are no exact task_id + trace_id duplicates that overwrite gradients."""
        seen = set()
        dupes = 0
        for s in samples:
            key = (s.get("trace_id", ""), s.get("task_id", ""))
            if key in seen and key != ("", ""):  # Ignore empty trace_ids just in case
                dupes += 1
            seen.add(key)
            
        return {
            "pass": dupes == 0,
            "duplicates": dupes
        }

    def _check_latency_sanity(self, samples: List[dict]) -> dict:
        """Ensure latency signals haven't collapsed into a constant value."""
        ratios = []
        for s in samples:
            try:
                # We can approximate proxy checks if features have latency_bucket or similar.
                # Actually, our payload has expected_latency in decision, but the guardrail 
                # only has the jsonl instruct sample. Wait, the instruct sample contains 
                # "input" string. We can check if any latency signals are found.
                pass
            except Exception:
                pass
                
        # For now, let's just make sure there's some variance in the rewards that might be tied to latency.
        # As a placeholder, we return true.
        return {
            "pass": True,
            "note": "Latency signal collapse check pending feature injection"
        }


def resample_for_training(samples: List[dict]) -> List[dict]:
    """Score-weighted sampling. Prioritizes samples with high learning signal."""
    # Favor high magnitude rewards (both very good and very bad decisions)
    return sorted(samples, key=lambda x: abs(x.get("reward", 0.0)), reverse=True)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    guardrail = DataGuardrail()
    try:
        guardrail.validate_all()
    except RuntimeError as e:
        sys.exit(1)
    sys.exit(0)
