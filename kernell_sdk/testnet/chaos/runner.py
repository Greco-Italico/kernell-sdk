#!/usr/bin/env python3
"""
Kernell OS Chaos Engine — Runner.

Executable chaos test suite against the SwarmSimulator.
Runs all 7 directed scenarios + compound attack sequences.

Usage:
    python -m testnet.chaos.runner
    python -m testnet.chaos.runner --scenario latency_spike
    python -m testnet.chaos.runner --sequence "latency_spike,cartel_attack,partial_node_failure"
"""
import argparse
import logging
import sys
import json
import time

from kernell_sdk.marketplace.simulator import SwarmSimulator
from kernell_sdk.testnet.chaos.engine import ChaosEngine
from kernell_sdk.testnet.chaos.scenarios import (
    LatencySpike,
    PartialNodeFailure,
    CartelAttack,
    PriceDumpAttack,
    ByzantineOutput,
    CascadingFailure,
    SchedulerPoisoning,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("chaos.runner")

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Scenario registry
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
SCENARIO_REGISTRY = {
    "latency_spike": LatencySpike,
    "partial_node_failure": PartialNodeFailure,
    "cartel_attack": CartelAttack,
    "price_dump_attack": PriceDumpAttack,
    "byzantine_output": ByzantineOutput,
    "cascading_failure": CascadingFailure,
    "scheduler_poisoning": SchedulerPoisoning,
}


def build_simulator(num_nodes: int = 50) -> SwarmSimulator:
    """Create a fresh SwarmSimulator with a mixed node pool."""
    sim = SwarmSimulator()
    sim.generate_swarm(num_nodes)
    # Warm up: 2 epochs so controller has baseline state
    for _ in range(2):
        sim.run_epoch(200)
    # Reset metrics after warmup
    sim.metrics = {
        "total_tasks": 0, "fraud_detected": 0, "fraud_success": 0,
        "false_positives": 0, "cartel_assignments": 0, "economic_loss": 0.0,
        "slashed_amount": 0.0, "fraud_profit": 0.0, "avg_task_cost": 0.0,
    }
    return sim


def run_single(scenario_name: str, nodes: int, tasks: int, epochs: int):
    """Run a single named scenario."""
    if scenario_name not in SCENARIO_REGISTRY:
        logger.error(f"Unknown scenario: {scenario_name}")
        logger.info(f"Available: {list(SCENARIO_REGISTRY.keys())}")
        sys.exit(1)

    sim = build_simulator(nodes)
    scenario = SCENARIO_REGISTRY[scenario_name]()
    engine = ChaosEngine(scenarios=[scenario])
    verdict = engine.run_experiment(scenario, sim, tasks, epochs)
    print(engine.report())
    return verdict.all_passed


def run_sequence(sequence_str: str, nodes: int, tasks: int, epochs: int):
    """Run a compound attack sequence."""
    names = [s.strip() for s in sequence_str.split(",")]
    scenarios = []
    for name in names:
        if name not in SCENARIO_REGISTRY:
            logger.error(f"Unknown scenario in sequence: {name}")
            sys.exit(1)
        scenarios.append(SCENARIO_REGISTRY[name]())

    sim = build_simulator(nodes)
    engine = ChaosEngine()
    verdict = engine.run_sequence(scenarios, sim, tasks, epochs)
    print(engine.report())
    return verdict.all_passed


def run_full_suite(nodes: int, tasks: int, epochs: int):
    """Run ALL scenarios + compound sequences."""
    sim = build_simulator(nodes)

    # Build all scenarios
    all_scenarios = [cls() for cls in SCENARIO_REGISTRY.values()]
    engine = ChaosEngine(scenarios=all_scenarios)

    # Phase 1: Individual scenarios
    logger.info("\n" + "█" * 70)
    logger.info("  PHASE 1: INDIVIDUAL SCENARIO TESTING")
    logger.info("█" * 70)
    engine.run_all(sim, tasks, epochs)

    # Phase 2: Compound sequences (the real test)
    logger.info("\n" + "█" * 70)
    logger.info("  PHASE 2: COMPOUND ATTACK SEQUENCES")
    logger.info("█" * 70)

    sequences = [
        [LatencySpike(), CartelAttack(), PartialNodeFailure()],
        [PriceDumpAttack(), SchedulerPoisoning()],
        [ByzantineOutput(), CascadingFailure(), LatencySpike()],
    ]

    for seq in sequences:
        # Fresh sim for each sequence
        seq_sim = build_simulator(nodes)
        engine.run_sequence(seq, seq_sim, tasks, epochs)

    # Final report
    report = engine.report()
    print(report)

    # Return exit code based on results
    failures = sum(1 for v in engine.history if not v.all_passed)
    return failures == 0


def main():
    parser = argparse.ArgumentParser(description="Kernell OS Chaos Engine")
    parser.add_argument("--scenario", type=str, help="Run a single scenario by name")
    parser.add_argument("--sequence", type=str, help="Comma-separated scenario sequence")
    parser.add_argument("--nodes", type=int, default=50, help="Number of nodes (default: 50)")
    parser.add_argument("--tasks", type=int, default=500, help="Tasks per epoch (default: 500)")
    parser.add_argument("--epochs", type=int, default=5, help="Epochs per scenario (default: 5)")
    parser.add_argument("--json", action="store_true", help="Output results as JSON")

    args = parser.parse_args()

    start = time.time()

    if args.scenario:
        success = run_single(args.scenario, args.nodes, args.tasks, args.epochs)
    elif args.sequence:
        success = run_sequence(args.sequence, args.nodes, args.tasks, args.epochs)
    else:
        success = run_full_suite(args.nodes, args.tasks, args.epochs)

    elapsed = time.time() - start
    logger.info(f"\nTotal chaos runtime: {elapsed:.1f}s")

    sys.exit(0 if success else 1)


if __name__ == "__main__":
    main()
