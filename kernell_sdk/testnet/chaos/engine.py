"""
Kernell OS Chaos Engine — Orchestrator.

Runs directed chaos scenarios against the swarm simulator,
validates protocol invariants after each experiment, and
produces structured verdicts.
"""
import time
import logging
import json
from typing import List, Dict, Any, Optional
from dataclasses import dataclass, field, asdict

from kernell_sdk.testnet.chaos.scenarios import ChaosScenario, ChaosResult

logger = logging.getLogger("chaos.engine")


@dataclass
class InvariantCheck:
    """A single protocol invariant to validate post-chaos."""
    name: str
    description: str
    threshold: float
    metric_key: str
    comparator: str  # "lt", "gt", "eq", "lte", "gte"

    def evaluate(self, value: float) -> bool:
        ops = {
            "lt": lambda v, t: v < t,
            "gt": lambda v, t: v > t,
            "lte": lambda v, t: v <= t,
            "gte": lambda v, t: v >= t,
            "eq": lambda v, t: abs(v - t) < 0.001,
        }
        return ops[self.comparator](value, self.threshold)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Default protocol invariants — the system MUST satisfy these
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
DEFAULT_INVARIANTS = [
    InvariantCheck(
        name="attack_roi",
        description="Attacking the system must be unprofitable (ROI < 1.0)",
        threshold=1.0,
        metric_key="attack_roi",
        comparator="lt",
    ),
    InvariantCheck(
        name="top_k_dominance",
        description="No top-5 cartel captures >50% of routing",
        threshold=0.5,
        metric_key="top_k_dominance",
        comparator="lt",
    ),
    InvariantCheck(
        name="fraud_detection_rate",
        description="Fraud detection rate must be >60%",
        threshold=0.6,
        metric_key="fraud_detection_rate",
        comparator="gt",
    ),
    InvariantCheck(
        name="system_liveness",
        description="System must successfully route >80% of tasks",
        threshold=0.8,
        metric_key="task_success_rate",
        comparator="gt",
    ),
]


@dataclass
class ExperimentVerdict:
    """Full verdict for one chaos experiment run."""
    scenario_name: str
    hypothesis: str
    chaos_result: Dict[str, Any]
    invariant_results: Dict[str, bool]
    all_passed: bool
    metrics_snapshot: Dict[str, float]
    duration_sec: float
    timestamp: float = field(default_factory=time.time)

    def to_json(self) -> str:
        return json.dumps(asdict(self), indent=2, default=str)


class ChaosEngine:
    """
    Protocol-level adversarial testing orchestrator.

    Not a random fault injector — a directed hypothesis tester that:
    1. Applies surgical perturbations via ChaosScenario
    2. Runs the economic simulation under stress
    3. Validates protocol invariants post-chaos
    4. Produces structured pass/fail verdicts
    """

    def __init__(
        self,
        scenarios: Optional[List[ChaosScenario]] = None,
        invariants: Optional[List[InvariantCheck]] = None,
        isolation_mode: bool = True,
    ):
        self.scenarios = scenarios or []
        self.invariants = invariants or DEFAULT_INVARIANTS
        self.isolation_mode = isolation_mode  # 1 scenario at a time
        self.history: List[ExperimentVerdict] = []

    def add_scenario(self, scenario: ChaosScenario):
        self.scenarios.append(scenario)

    def add_invariant(self, invariant: InvariantCheck):
        self.invariants.append(invariant)

    def _extract_metrics(self, simulator) -> Dict[str, float]:
        """Pull metrics from the SwarmSimulator after a chaos run."""
        m = simulator.metrics
        total = max(1, m["total_tasks"])
        fraud_detected = m.get("fraud_detected", 0)
        fraud_success = m.get("fraud_success", 0)
        total_fraud = fraud_detected + fraud_success
        slashed = m.get("slashed_amount", 0.0)
        fraud_profit = m.get("fraud_profit", 0.0)

        return {
            "attack_roi": (fraud_profit / slashed) if slashed > 0 else float("inf") if fraud_profit > 0 else 0.0,
            "top_k_dominance": m.get("top_5_dominance", 0.0),
            "fraud_detection_rate": (fraud_detected / total_fraud) if total_fraud > 0 else 1.0,
            "task_success_rate": (total - fraud_success) / total,
            "economic_loss": m.get("economic_loss", 0.0),
            "slashed_amount": slashed,
            "fraud_profit": fraud_profit,
            "total_tasks": total,
            "fraud_detected": fraud_detected,
            "fraud_success": fraud_success,
            "controller_dominance_w": getattr(simulator.controller, "w_dominance", 0),
            "controller_redundancy": getattr(simulator.controller, "redundancy_probability", 0),
            "controller_slashing_mult": getattr(simulator.controller, "slashing_multiplier", 0),
        }

    def _validate_invariants(self, metrics: Dict[str, float]) -> Dict[str, bool]:
        """Check all protocol invariants against current metrics."""
        results = {}
        for inv in self.invariants:
            value = metrics.get(inv.metric_key)
            if value is None:
                results[inv.name] = False
                logger.warning(f"[INVARIANT] {inv.name}: metric '{inv.metric_key}' missing")
                continue
            passed = inv.evaluate(value)
            results[inv.name] = passed
            status = "✅ PASS" if passed else "❌ FAIL"
            logger.info(f"[INVARIANT] {inv.name}: {value:.4f} {inv.comparator} {inv.threshold} → {status}")
        return results

    def run_experiment(
        self,
        scenario: ChaosScenario,
        simulator,
        tasks_per_epoch: int = 500,
        epochs: int = 5,
    ) -> ExperimentVerdict:
        """
        Execute a single chaos experiment:
        1. Apply scenario to the live node pool
        2. Run N epochs of the simulator under perturbation
        3. Cleanup the perturbation
        4. Validate invariants
        5. Return structured verdict
        """
        logger.info(f"\n{'='*60}")
        logger.info(f"🔥 CHAOS: {scenario.name}")
        logger.info(f"📐 Hypothesis: {scenario.hypothesis}")
        logger.info(f"{'='*60}")

        start = time.time()

        # 1. Apply perturbation
        chaos_result = scenario.apply(simulator.nodes)
        logger.info(f"[APPLY] Affected {chaos_result.nodes_affected} nodes")

        # 2. Run simulation under stress
        for epoch in range(1, epochs + 1):
            simulator.run_epoch(tasks_per_epoch)
            logger.info(f"  Epoch {epoch}/{epochs} complete")

        duration = time.time() - start

        # 3. Extract metrics
        metrics = self._extract_metrics(simulator)

        # 4. Cleanup
        scenario.cleanup()

        # 5. Validate invariants
        inv_results = self._validate_invariants(metrics)
        all_passed = all(inv_results.values())

        verdict = ExperimentVerdict(
            scenario_name=scenario.name,
            hypothesis=scenario.hypothesis,
            chaos_result=asdict(chaos_result) if hasattr(chaos_result, '__dataclass_fields__') else {"raw": str(chaos_result)},
            invariant_results=inv_results,
            all_passed=all_passed,
            metrics_snapshot=metrics,
            duration_sec=duration,
        )

        self.history.append(verdict)

        status = "✅ SYSTEM SURVIVED" if all_passed else "🚨 INVARIANT VIOLATION"
        logger.info(f"\n{'='*60}")
        logger.info(f"{status} — {scenario.name}")
        logger.info(f"{'='*60}\n")

        return verdict

    def run_all(
        self,
        simulator,
        tasks_per_epoch: int = 500,
        epochs_per_scenario: int = 5,
        reset_between: bool = True,
    ) -> List[ExperimentVerdict]:
        """Run all registered scenarios sequentially (isolation mode)."""
        verdicts = []
        for scenario in self.scenarios:
            if reset_between:
                # Reset simulator metrics between scenarios for clean measurement
                simulator.metrics = {
                    "total_tasks": 0, "fraud_detected": 0, "fraud_success": 0,
                    "false_positives": 0, "cartel_assignments": 0, "economic_loss": 0.0,
                    "slashed_amount": 0.0, "fraud_profit": 0.0, "avg_task_cost": 0.0,
                }
            verdict = self.run_experiment(scenario, simulator, tasks_per_epoch, epochs_per_scenario)
            verdicts.append(verdict)
        return verdicts

    def run_sequence(
        self,
        sequence: List[ChaosScenario],
        simulator,
        tasks_per_epoch: int = 500,
        epochs_per_scenario: int = 3,
    ) -> ExperimentVerdict:
        """
        Run a SEQUENCE of scenarios without cleanup between them.
        Simulates compound real-world attacks:
          [LatencySpike → CartelAttack → NodeFailure]
        """
        logger.info(f"\n{'='*60}")
        logger.info(f"⚡ CHAOS SEQUENCE: {' → '.join(s.name for s in sequence)}")
        logger.info(f"{'='*60}")

        start = time.time()
        total_affected = 0

        # Apply all perturbations
        for scenario in sequence:
            result = scenario.apply(simulator.nodes)
            total_affected += result.nodes_affected

        # Run under compound stress
        for epoch in range(1, epochs_per_scenario + 1):
            simulator.run_epoch(tasks_per_epoch)
            logger.info(f"  Sequence epoch {epoch}/{epochs_per_scenario}")

        duration = time.time() - start
        metrics = self._extract_metrics(simulator)

        # Cleanup all
        for scenario in sequence:
            scenario.cleanup()

        inv_results = self._validate_invariants(metrics)
        all_passed = all(inv_results.values())

        verdict = ExperimentVerdict(
            scenario_name=f"SEQUENCE[{'+'.join(s.name for s in sequence)}]",
            hypothesis="System survives compound adversarial attack",
            chaos_result={"sequence": [s.name for s in sequence], "total_affected": total_affected},
            invariant_results=inv_results,
            all_passed=all_passed,
            metrics_snapshot=metrics,
            duration_sec=duration,
        )
        self.history.append(verdict)
        return verdict

    def report(self) -> str:
        """Generate a full chaos test report."""
        lines = [
            "\n" + "═" * 70,
            "  KERNELL OS — CHAOS ENGINE REPORT",
            "═" * 70,
            "",
        ]
        passed = sum(1 for v in self.history if v.all_passed)
        failed = len(self.history) - passed

        lines.append(f"  Total Experiments: {len(self.history)}")
        lines.append(f"  Passed: {passed}  |  Failed: {failed}")
        lines.append("")

        for i, v in enumerate(self.history, 1):
            icon = "✅" if v.all_passed else "🚨"
            lines.append(f"  {icon} [{i}] {v.scenario_name}")
            lines.append(f"     Hypothesis: {v.hypothesis}")
            lines.append(f"     Duration: {v.duration_sec:.1f}s")

            for inv_name, inv_passed in v.invariant_results.items():
                inv_icon = "✓" if inv_passed else "✗"
                metric_val = v.metrics_snapshot.get(inv_name, "N/A")
                if isinstance(metric_val, float):
                    metric_val = f"{metric_val:.4f}"
                lines.append(f"       {inv_icon} {inv_name} = {metric_val}")

            # Key metrics
            m = v.metrics_snapshot
            lines.append(f"     Attack ROI: {m.get('attack_roi', 0):.3f}")
            lines.append(f"     Dominance: {m.get('top_k_dominance', 0):.1%}")
            lines.append(f"     Fraud detected: {m.get('fraud_detected', 0):.0f}")
            lines.append(f"     Economic loss: {m.get('economic_loss', 0):.2f} KERN")
            lines.append("")

        lines.append("═" * 70)
        verdict = "PROTOCOL RESILIENT ✅" if failed == 0 else f"PROTOCOL VULNERABLE 🚨 ({failed} failures)"
        lines.append(f"  FINAL VERDICT: {verdict}")
        lines.append("═" * 70)

        report_text = "\n".join(lines)
        logger.info(report_text)
        return report_text
