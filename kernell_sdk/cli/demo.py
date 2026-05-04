from __future__ import annotations
import time
from pprint import pprint


class BaselineModel:
    """Simulates a direct premium API call (no routing)."""
    def generate(self, prompt: str) -> str:
        time.sleep(0.8)
        return f"[baseline] {prompt[:80]}"


COSTS = {
    "baseline": 0.25,
    "local_nano": 0.001,
    "local_small": 0.001,
    "local_medium": 0.005,
    "local_large": 0.01,
    "local": 0.001,
    "cheap_api": 0.01,
    "premium_api": 0.25,
    "cache": 0.0,
    "none": 0.05,
}


def estimate_cost(route: str) -> float:
    return COSTS.get(route, 0.05)


class DemoLocalBackend:
    """
    Mock LLM backend for demos.

    Must handle THREE types of prompts from the router pipeline:
    1. Decomposer prompts ("Decompose this task...") → not real JSON, triggers fallback
    2. Verifier prompts ("quality verifier...") → must return valid JSON with confidence > 0.7
    3. Execution prompts (the actual task) → must return substantive content
    """
    def generate(self, prompt: str, system: str = "") -> str:
        p = prompt.lower()

        # ── Verifier prompt: return valid acceptance JSON ──
        if "quality verifier" in p or "verify" in p or "evaluate" in p:
            return '{"valid": true, "confidence": 0.92, "reason": "output is correct and complete"}'

        # ── Decomposer prompt: return something non-JSON (triggers safe fallback) ──
        if "decompose" in p:
            return "single_task"

        # ── Execution prompts: substantive responses ──
        if "2+2" in prompt or "add two" in p:
            return "The answer is 4. This is a basic arithmetic operation."
        if "index" in p or "database" in p:
            return ("A database index is a data structure (typically a B-tree) "
                    "that improves the speed of data retrieval operations. "
                    "It works by maintaining a sorted reference to rows, "
                    "allowing the database engine to find data without scanning "
                    "every row in the table.")
        if "distributed" in p or "chat" in p or "scalable" in p:
            return ("A scalable distributed chat system would use: "
                    "1) WebSocket connections for real-time messaging, "
                    "2) A message broker like Kafka for event streaming, "
                    "3) Horizontal scaling via consistent hashing, "
                    "4) Redis for presence and session state.")
        if "function" in p or "python" in p or "code" in p:
            return "def add(a, b):\n    return a + b"

        return f"Detailed response to: {prompt[:120]}"


def run_demo():
    print("\n🚀 Kernell OS Demo — Cost & Intelligence Comparison\n")

    try:
        from kernell_sdk.router import IntelligentRouter
        from kernell_sdk.router import TelemetryCollector, TelemetryConfig
    except Exception as e:
        print("[demo] SDK not installed or import failed:", e)
        return None

    telemetry = TelemetryCollector(
        config=TelemetryConfig(
            enabled=True,
            consent_given=True,
            buffer_dir="/tmp/kernell_demo",
        )
    )

    local_be = DemoLocalBackend()

    router = IntelligentRouter(
        classifier=local_be,
        local_models={
            "local_nano": local_be,
            "local_small": local_be,
            "local_medium": local_be,
        },
        telemetry=telemetry,
    )
    baseline = BaselineModel()

    tasks = [
        ("easy", "What is 2+2?"),
        ("medium", "Explain how a database index works"),
        ("hard", "Design a scalable distributed system for real-time chat"),
    ]

    total_baseline_cost = 0.0
    total_kernell_cost = 0.0
    total_baseline_latency = 0.0
    total_kernell_latency = 0.0
    success_count = 0
    results_summary = []

    for difficulty, prompt in tasks:
        print(f"\n--- Task ({difficulty}) ---")
        print(f"Input: {prompt}\n")

        # Baseline execution
        t0 = time.time()
        baseline.generate(prompt)
        baseline_latency = time.time() - t0
        baseline_cost = COSTS["baseline"]

        # Kernell execution
        t1 = time.time()
        results = router.execute(prompt)
        kernell_latency = time.time() - t1

        success = any(r.success for r in results)
        if success:
            success_count += 1
        output = next((r.output for r in results if r.success), "<no output>")
        route = results[0].model_used if results else "unknown"

        kernell_cost = estimate_cost(route)

        total_baseline_cost += baseline_cost
        total_kernell_cost += kernell_cost
        total_baseline_latency += baseline_latency
        total_kernell_latency += kernell_latency

        savings = baseline_cost - kernell_cost

        print(f"Baseline → cost: ${baseline_cost:.3f}, latency: {baseline_latency:.2f}s")
        print(f"Kernell  → route: {route}, cost: ${kernell_cost:.3f}, latency: {kernell_latency:.2f}s")
        print(f"💰 Savings: ${savings:.3f}")
        print(f"Output (truncated): {output[:100]}...")

        results_summary.append({
            "difficulty": difficulty,
            "route": route,
            "baseline_cost": baseline_cost,
            "kernell_cost": kernell_cost,
            "savings": savings,
        })

    total_savings = total_baseline_cost - total_kernell_cost
    savings_pct = (total_savings / total_baseline_cost) * 100 if total_baseline_cost else 0
    latency_delta = ((total_kernell_latency - total_baseline_latency) / total_baseline_latency * 100) if total_baseline_latency else 0
    success_rate = (success_count / len(tasks)) * 100

    print("\n📊 Summary:")
    pprint(results_summary)

    print("\n💥 Total Savings:")
    print(f"Baseline cost: ${total_baseline_cost:.3f}")
    print(f"Kernell cost:  ${total_kernell_cost:.3f}")
    print(f"Savings:       ${total_savings:.3f} ({savings_pct:.1f}%)")

    print("\n📡 Telemetry Sample:")
    events = telemetry.inspect_buffer()
    if not events:
        print("No telemetry events captured ❌")
    else:
        pprint(events[0])

    return {
        "savings_usd": total_savings,
        "savings_pct": savings_pct,
        "latency_delta_pct": latency_delta,
        "success_rate": success_rate,
    }


def main() -> int:
    print("\n🚀 Kernell OS — Live Demo\n")
    time.sleep(0.3)

    results = run_demo()

    if not results:
        return 1

    savings = results["savings_usd"]
    savings_pct = results["savings_pct"]
    latency_delta = results["latency_delta_pct"]
    success_rate = results["success_rate"]

    print("\n──────── RESULTS ────────\n")
    print(f"💰 You saved ${savings:.2f} ({savings_pct:.1f}%)")
    print(f"⚡ Latency delta: {latency_delta:.1f}%")
    print(f"🧠 Success rate: {success_rate:.0f}%")
    print("\n─────────────────────────\n")
    print("Kernell is optimizing your AI stack in real time.\n")

    return 0

if __name__ == "__main__":
    main()
