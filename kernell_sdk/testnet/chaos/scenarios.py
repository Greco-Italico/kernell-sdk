"""
Kernell OS Chaos Engine — Directed Attack Scenarios.

Each scenario targets a specific protocol hypothesis.
Not random noise — surgical perturbations with measurable impact.
"""
import random
import time
import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import List, Dict, Any, Optional

logger = logging.getLogger("chaos.scenarios")


@dataclass
class ChaosResult:
    """Outcome of a single chaos scenario execution."""
    scenario_name: str
    hypothesis: str
    duration_sec: float
    nodes_affected: int
    mutations_applied: Dict[str, Any] = field(default_factory=dict)
    invariants_checked: Dict[str, bool] = field(default_factory=dict)
    passed: bool = True
    error: Optional[str] = None


class ChaosScenario(ABC):
    """Base class for all chaos scenarios."""

    def __init__(self, name: str, hypothesis: str):
        self.name = name
        self.hypothesis = hypothesis
        self._original_state: Dict[str, Any] = {}
        self._affected_nodes: List[Any] = []

    @abstractmethod
    def apply(self, nodes: list) -> ChaosResult:
        """Inject the perturbation into the cluster."""
        ...

    def cleanup(self):
        """Restore all mutated nodes to their pre-chaos state."""
        for node in self._affected_nodes:
            agent_id = node.agent_id
            if agent_id in self._original_state:
                for attr, val in self._original_state[agent_id].items():
                    setattr(node, attr, val)
        self._original_state.clear()
        self._affected_nodes.clear()
        logger.info(f"[CLEANUP] {self.name} — state restored")

    def _snapshot(self, node, attrs: List[str]):
        """Save original attribute values before mutation."""
        self._original_state[node.agent_id] = {
            a: getattr(node, a) for a in attrs if hasattr(node, a)
        }
        if node not in self._affected_nodes:
            self._affected_nodes.append(node)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 💣 1. LATENCY SPIKE — tests scheduler bias & fallback rate
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
class LatencySpike(ChaosScenario):
    """
    Injects artificial latency into 30% of nodes.
    HYPOTHESIS: Scheduler should NOT over-route to unaffected nodes
    (dominance must stay < 0.5) and fallback rate must stay bounded.
    """

    def __init__(self, probability: float = 0.3, min_delay: float = 1.0, max_delay: float = 5.0):
        super().__init__(
            name="latency_spike",
            hypothesis="Scheduler distributes load fairly under latency; dominance < 0.5"
        )
        self.probability = probability
        self.min_delay = min_delay
        self.max_delay = max_delay

    def apply(self, nodes: list) -> ChaosResult:
        affected = 0
        for node in nodes:
            if random.random() < self.probability:
                self._snapshot(node, ["reliability", "price_per_sec"])
                delay = random.uniform(self.min_delay, self.max_delay)
                # Latency degrades reliability score proportionally
                node.reliability = max(0.1, node.reliability - (delay * 0.15))
                affected += 1
                logger.info(f"[LATENCY] {node.agent_id} += {delay:.1f}s delay, reliability → {node.reliability:.2f}")

        return ChaosResult(
            scenario_name=self.name,
            hypothesis=self.hypothesis,
            duration_sec=0,
            nodes_affected=affected,
            mutations_applied={"type": "latency_degradation", "probability": self.probability},
        )


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 💣 2. PARTIAL NODE FAILURE — tests resilience under degradation
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
class PartialNodeFailure(ChaosScenario):
    """
    20% of nodes start failing 50% of their tasks.
    HYPOTHESIS: System detects degraded nodes and routes around them
    without collapsing the remaining healthy pool.
    """

    def __init__(self, failure_probability: float = 0.2, task_failure_rate: float = 0.5):
        super().__init__(
            name="partial_node_failure",
            hypothesis="Reputation engine penalizes failing nodes; healthy nodes absorb load"
        )
        self.failure_probability = failure_probability
        self.task_failure_rate = task_failure_rate

    def apply(self, nodes: list) -> ChaosResult:
        affected = 0
        for node in nodes:
            if random.random() < self.failure_probability:
                self._snapshot(node, ["reliability", "reputation"])
                node.reliability = max(0.1, node.reliability * (1.0 - self.task_failure_rate))
                node.reputation = max(10.0, node.reputation - 30.0)
                affected += 1
                logger.info(f"[FAILURE] {node.agent_id} reliability → {node.reliability:.2f}")

        return ChaosResult(
            scenario_name=self.name,
            hypothesis=self.hypothesis,
            duration_sec=0,
            nodes_affected=affected,
            mutations_applied={"type": "partial_failure", "rate": self.task_failure_rate},
        )


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 💣 3. CARTEL ATTACK — tests anti-correlation & arbitration
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
class CartelAttack(ChaosScenario):
    """
    3 nodes coordinate: same region, same provider, colluding strategy.
    HYPOTHESIS: Scheduler's correlation risk blocks co-assignment;
    arbitration catches matching fraudulent outputs.
    """

    def __init__(self, cartel_size: int = 3):
        super().__init__(
            name="cartel_attack",
            hypothesis="Anti-collusion scheduler blocks cartel co-assignment (correlation > 0.5)"
        )
        self.cartel_size = cartel_size

    def apply(self, nodes: list) -> ChaosResult:
        if len(nodes) < self.cartel_size:
            return ChaosResult(
                scenario_name=self.name, hypothesis=self.hypothesis,
                duration_sec=0, nodes_affected=0, passed=False,
                error="Not enough nodes for cartel formation"
            )

        cartel = random.sample(nodes, k=self.cartel_size)
        cartel_region = "cartel-zone"
        cartel_provider = "cartel-infra"

        for node in cartel:
            self._snapshot(node, ["region", "provider", "stake", "price_per_sec"])
            node.region = cartel_region
            node.provider = cartel_provider
            # Cartels boost stake to game routing
            node.stake = node.stake * 2.0
            node.price_per_sec = node.price_per_sec * 0.5

        cartel_ids = [n.agent_id for n in cartel]
        logger.info(f"[CARTEL] Formed cartel: {cartel_ids}")

        return ChaosResult(
            scenario_name=self.name,
            hypothesis=self.hypothesis,
            duration_sec=0,
            nodes_affected=self.cartel_size,
            mutations_applied={"type": "cartel_formation", "members": cartel_ids},
        )


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 💣 4. PRICE DUMPING — tests market invariants
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
class PriceDumpAttack(ChaosScenario):
    """
    All adaptive nodes slash prices to 20% of their base.
    HYPOTHESIS: Scheduler's anti-dumping penalty prevents routing
    to predatory pricers; market avg price stays > 50% of baseline.
    """

    def __init__(self, dump_factor: float = 0.2):
        super().__init__(
            name="price_dump_attack",
            hypothesis="Anti-dumping penalty blocks predatory pricing; market price floor holds"
        )
        self.dump_factor = dump_factor

    def apply(self, nodes: list) -> ChaosResult:
        affected = 0
        for node in nodes:
            self._snapshot(node, ["price_per_sec"])
            node.price_per_sec = node.price_per_sec * self.dump_factor
            affected += 1
            logger.info(f"[DUMP] {node.agent_id} price → {node.price_per_sec:.4f}")

        return ChaosResult(
            scenario_name=self.name,
            hypothesis=self.hypothesis,
            duration_sec=0,
            nodes_affected=affected,
            mutations_applied={"type": "price_dump", "factor": self.dump_factor},
        )


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 💣 5. BYZANTINE OUTPUT — tests verification pipeline
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
class ByzantineOutput(ChaosScenario):
    """
    A random node is forced to produce invalid outputs.
    HYPOTHESIS: Redundancy verification catches the mismatch;
    slashing is applied and reputation drops.
    """

    def __init__(self, targets: int = 1):
        super().__init__(
            name="byzantine_output",
            hypothesis="Verification detects invalid outputs; slashing activates"
        )
        self.targets = targets

    def apply(self, nodes: list) -> ChaosResult:
        targets = random.sample(nodes, k=min(self.targets, len(nodes)))
        for node in targets:
            self._snapshot(node, ["reliability", "reputation"])
            # Simulate byzantine by cratering reliability — the node
            # "succeeds" but its output hash will not match verifiers
            node.reliability = 0.05
            node.reputation = max(5.0, node.reputation - 50.0)
            logger.info(f"[BYZANTINE] {node.agent_id} → forced invalid outputs")

        return ChaosResult(
            scenario_name=self.name,
            hypothesis=self.hypothesis,
            duration_sec=0,
            nodes_affected=len(targets),
            mutations_applied={"type": "byzantine", "targets": [n.agent_id for n in targets]},
        )


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 💣 6. CASCADING FAILURE — tests system under chain reaction
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
class CascadingFailure(ChaosScenario):
    """
    Kill 1 node → remaining nodes get overloaded → cascade.
    HYPOTHESIS: System absorbs single-node loss without triggering
    a dominance spiral; controller stabilizes within 3 epochs.
    """

    def __init__(self, initial_kills: int = 1, overload_factor: float = 0.3):
        super().__init__(
            name="cascading_failure",
            hypothesis="Single-node death does not cascade; controller stabilizes in ≤3 epochs"
        )
        self.initial_kills = initial_kills
        self.overload_factor = overload_factor

    def apply(self, nodes: list) -> ChaosResult:
        if len(nodes) <= self.initial_kills:
            return ChaosResult(
                scenario_name=self.name, hypothesis=self.hypothesis,
                duration_sec=0, nodes_affected=0, passed=False,
                error="Not enough nodes to simulate cascade"
            )

        # Kill initial nodes
        killed = random.sample(nodes, k=self.initial_kills)
        for node in killed:
            self._snapshot(node, ["reliability", "reputation", "stake"])
            node.reliability = 0.0
            node.reputation = 0.0
            logger.info(f"[CASCADE-KILL] {node.agent_id} → dead")

        # Overload survivors proportionally
        survivors = [n for n in nodes if n not in killed]
        overloaded = 0
        for node in survivors:
            self._snapshot(node, ["reliability"])
            degradation = self.overload_factor * (self.initial_kills / len(survivors))
            node.reliability = max(0.2, node.reliability - degradation)
            overloaded += 1

        logger.info(f"[CASCADE] {self.initial_kills} killed, {overloaded} survivors degraded")

        return ChaosResult(
            scenario_name=self.name,
            hypothesis=self.hypothesis,
            duration_sec=0,
            nodes_affected=self.initial_kills + overloaded,
            mutations_applied={"killed": [n.agent_id for n in killed], "overloaded": overloaded},
        )


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 💣 7. SCHEDULER POISONING — nodes report false metrics
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
class SchedulerPoisoning(ChaosScenario):
    """
    Nodes inflate their reputation and stake to game the scheduler.
    HYPOTHESIS: sqrt(stake) diminishing returns + reputation decay
    prevent inflated nodes from capturing >50% of routing.
    """

    def __init__(self, poisoners: int = 3, inflation_factor: float = 5.0):
        super().__init__(
            name="scheduler_poisoning",
            hypothesis="Inflated metrics do not capture >50% routing due to sqrt-stake + decay"
        )
        self.poisoners = poisoners
        self.inflation_factor = inflation_factor

    def apply(self, nodes: list) -> ChaosResult:
        targets = random.sample(nodes, k=min(self.poisoners, len(nodes)))
        for node in targets:
            self._snapshot(node, ["reputation", "stake", "reliability"])
            node.reputation = min(100.0, node.reputation * self.inflation_factor)
            node.stake = node.stake * self.inflation_factor
            node.reliability = 1.0  # Perfect reported reliability
            logger.info(f"[POISON] {node.agent_id} rep→{node.reputation:.0f} stake→{node.stake:.0f}")

        return ChaosResult(
            scenario_name=self.name,
            hypothesis=self.hypothesis,
            duration_sec=0,
            nodes_affected=len(targets),
            mutations_applied={
                "type": "scheduler_poisoning",
                "targets": [n.agent_id for n in targets],
                "inflation": self.inflation_factor,
            },
        )
