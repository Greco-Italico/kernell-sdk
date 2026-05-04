import json
import math
import random
from collections import defaultdict
from kernell_sdk.telemetry.schema import TelemetryEvent

def compute_reward(event: TelemetryEvent) -> float:
    s = 1.0 if event.execution.success else 0.0
    a = event.consensus.score
    c = event.decision.confidence
    k = event.execution.cost_usd * 10
    
    latency_ratio = event.execution.latency_ms / max(1.0, event.features.input_tokens)
    latency_penalty = min(latency_ratio, 2.0) * 0.1
    r = event.execution.retries
    
    return (
        s +
        (0.5 * a) +
        (0.3 * c) -
        (0.3 * k) -
        latency_penalty -
        (0.2 * r)
    )

def format_instruction(event: TelemetryEvent) -> str:
    return (
        f"Task: {event.task.input_preview}\n"
        f"Input tokens: {event.features.input_tokens}\n"
        f"Expected output tokens: {event.features.expected_output_tokens}\n"
        f"Complexity: {event.features.complexity_score}\n"
        f"Priority: {event.features.priority}"
    )

def format_output(event: TelemetryEvent) -> str:
    return (
        f"Tier: {event.decision.tier}\n"
        f"Model: {event.decision.model}"
    )

def normalize_rewards(rewards):
    if not rewards:
        return []
    mean = sum(rewards) / len(rewards)
    var = sum((r - mean) ** 2 for r in rewards) / len(rewards)
    std = max(var ** 0.5, 1e-6)
    return [(r - mean) / std for r in rewards]

def compute_weight(r_norm):
    return math.exp(abs(r_norm))

def stratify_by_tier(dataset):
    buckets = defaultdict(list)
    for sample in dataset:
        try:
            tier = sample["output"].split("\n")[0].split(": ")[1]
            buckets[tier].append(sample)
        except Exception:
            continue
    return buckets

def resample_dataset(dataset):
    if not dataset:
        return []
        
    rewards = [s["reward"] for s in dataset]
    norm_rewards = normalize_rewards(rewards)
    
    for i, sample in enumerate(dataset):
        sample["weight"] = compute_weight(norm_rewards[i])
        
    buckets = stratify_by_tier(dataset)
    if not buckets:
        return dataset
        
    # anti-collapse: target balance
    target_per_tier = min(len(b) for b in buckets.values())
    if target_per_tier == 0:
        return dataset
        
    final_dataset = []
    for tier, samples in buckets.items():
        weights = [s["weight"] for s in samples]
        chosen = random.choices(
            samples,
            weights=weights,
            k=target_per_tier
        )
        final_dataset.extend(chosen)
        
    random.shuffle(final_dataset)
    return final_dataset

def build_dataset(jsonl_path: str, output_path: str):
    dataset = []
    with open(jsonl_path, "r") as f:
        for line in f:
            try:
                raw = json.loads(line)
                event = TelemetryEvent(**raw)
            except Exception:
                continue  # Skip corrupt/invalid lines silently
            
            reward = compute_reward(event)
            sample = {
                "instruction": format_instruction(event),
                "input": "",
                "output": format_output(event),
                "reward": reward
            }
            dataset.append(sample)
            
    # Apply score-weighted and stratified resampling
    dataset = resample_dataset(dataset)
            
    with open(output_path, "w") as f:
        json.dump(dataset, f, indent=2)
        
    print(f"✅ Dataset built: {len(dataset)} samples")
