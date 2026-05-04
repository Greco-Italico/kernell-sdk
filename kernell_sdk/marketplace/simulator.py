import random
import uuid
from typing import List, Dict, Any

from kernell_sdk.marketplace.scheduler import MarketNode, MarketScheduler
from kernell_sdk.marketplace.controller import EconomicController
from kernell_sdk.reputation.engine import ReputationEngine
from kernell_sdk.reputation.dispute import DisputeArbitrationSystem, DisputeResult
from kernell_sdk.reputation.receipt import ExecutionReceipt

class SimNode(MarketNode):
    """Base class for simulated nodes with behavior logic."""
    def __init__(self, agent_id, region, provider, reputation, stake, price, reliability):
        super().__init__(agent_id, region, provider, reputation, stake, price, reliability)
        self.kern_balance = stake
        self.canary_success_rate = 1.0  # Honest nodes never fail canary unless bug

    def execute_task(self, task_hash: str, task_value: float) -> ExecutionReceipt:
        # Base honest execution
        return ExecutionReceipt(
            agent_id=self.agent_id,
            task_hash=task_hash,
            output_hash=f"valid_output_{task_hash}",
            mode_used="isolated",
            fallback_triggered=False,
            execution_time=random.expovariate(1 / 0.5),  # Realistic latency simulation
            success=True,
            canary_nonce="simulated_valid_nonce" if random.random() < self.canary_success_rate else "invalid_nonce"
        )

class HonestNode(SimNode):
    pass

class LazyNode(SimNode):
    """Never executes, just returns fake outputs."""
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.canary_success_rate = 0.05  # Lazy nodes rarely guess canary correctly

    def execute_task(self, task_hash: str, task_value: float) -> ExecutionReceipt:
        return ExecutionReceipt(
            agent_id=self.agent_id,
            task_hash=task_hash,
            output_hash="fake_output",
            mode_used="debug",
            fallback_triggered=True,
            execution_time=0.01,
            success=True,
            canary_nonce="simulated_valid_nonce" if random.random() < self.canary_success_rate else "invalid_nonce"
        )

class SleeperNode(SimNode):
    """Acts honest to build reputation, then attacks high value targets."""
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.tasks_done = 0
        self.canary_success_rate = 0.4  # Sleepers have some reverse-engineering skill
        
    def execute_task(self, task_hash: str, task_value: float) -> ExecutionReceipt:
        self.tasks_done += 1
        # Selective Honesty Attack: Only attack if value is high and rep is built
        if self.tasks_done > 50 and task_value > 30.0:
            # Attack phase
            return ExecutionReceipt(
                agent_id=self.agent_id,
                task_hash=task_hash,
                output_hash="malicious_payload",
                mode_used="constrained",
                fallback_triggered=True,
                execution_time=0.1,
                success=True,
                canary_nonce="simulated_valid_nonce" if random.random() < self.canary_success_rate else "invalid_nonce"
            )
        # Honest phase
        return super().execute_task(task_hash, task_value)

class ColludingNode(SimNode):
    """Coordinates with other cartel members to return matching fraudulent outputs."""
    def __init__(self, cartel_id: str, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.cartel_id = cartel_id
        self.canary_success_rate = 0.85  # Cartels share zero-days / parsing logic
        
    def execute_task(self, task_hash: str, task_value: float) -> ExecutionReceipt:
        return ExecutionReceipt(
            agent_id=self.agent_id,
            task_hash=task_hash,
            output_hash=f"cartel_{self.cartel_id}_output_{task_hash}",
            mode_used="isolated",
            fallback_triggered=False,
            execution_time=0.05,
            success=True,
            canary_nonce="simulated_valid_nonce" if random.random() < self.canary_success_rate else "invalid_nonce"
        )

class AdaptiveNode(SimNode):
    """Learns from the market. Adjusts strategy based on ROI and attempts market capture."""
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.strategy = "honest"
        self.fraud_profit = 0.0
        self.slashed_losses = 0.0
        self.canary_success_rate = 0.6
        self.base_price = self.price_per_sec
        self.base_stake = self.stake

    def decide_strategy(self):
        if self.slashed_losses > self.fraud_profit:
            self.strategy = "honest"
            self.price_per_sec = self.base_price
            self.stake = self.base_stake
        elif self.fraud_profit > self.slashed_losses * 1.5:
            self.strategy = "attack"
        else:
            # Try to capture market routing
            self.strategy = "capture_market"
            self.price_per_sec = self.base_price * 0.5
            self.stake = self.base_stake * 1.5

    def execute_task(self, task_hash: str, task_value: float) -> ExecutionReceipt:
        self.decide_strategy()
        if self.strategy == "attack":
            return ExecutionReceipt(
                agent_id=self.agent_id,
                task_hash=task_hash,
                output_hash="adaptive_malicious_output",
                mode_used="constrained",
                fallback_triggered=True,
                execution_time=0.1,
                success=True,
                canary_nonce="simulated_valid_nonce" if random.random() < self.canary_success_rate else "invalid_nonce"
            )
        return super().execute_task(task_hash, task_value)

class SwarmSimulator:
    def __init__(self):
        self.scheduler = MarketScheduler()
        self.reputation = ReputationEngine()
        self.arbitration = DisputeArbitrationSystem(self.reputation)
        self.controller = EconomicController()
        self.nodes: List[SimNode] = []
        
        self.metrics = {
            "total_tasks": 0,
            "fraud_detected": 0,
            "fraud_success": 0,
            "false_positives": 0,
            "cartel_assignments": 0,
            "economic_loss": 0.0,
            "slashed_amount": 0.0,
            "fraud_profit": 0.0,
            "avg_task_cost": 0.0
        }
        self.node_counter = 0

    def _create_node(self):
        regions = ["us-east", "eu-west", "ap-south"]
        providers = ["aws", "gcp", "contabo"]
        agent_id = f"node_{self.node_counter}"
        self.node_counter += 1
        reg = random.choice(regions)
        prov = random.choice(providers)
        
        # 60% Honest, 15% Lazy, 10% Sleeper, 15% Cartel
        roll = random.random()
        if roll < 0.5:
            return HonestNode(agent_id, reg, prov, 100, 1000, 0.05, 0.99)
        elif roll < 0.65:
            return LazyNode(agent_id, reg, prov, 100, 500, 0.01, 0.90)
        elif roll < 0.75:
            return SleeperNode(agent_id, reg, prov, 100, 2000, 0.08, 0.99)
        elif roll < 0.85:
            return ColludingNode("alpha_cartel", agent_id, reg, prov, 100, 1500, 0.04, 0.95)
        else:
            return AdaptiveNode(agent_id, reg, prov, 100, 1200, 0.06, 0.98)

    def generate_swarm(self, total: int):
        for _ in range(total):
            node = self._create_node()
            self.nodes.append(node)
            self.reputation._scores[node.agent_id] = 100
            
    def disable_region(self, region: str):
        """Simulates a massive shock event."""
        print(f"\\n[SHOCK EVENT] Region {region} went offline!\\n")
        self.nodes = [n for n in self.nodes if n.region != region]

    def run_epoch(self, num_tasks: int):
        task_assignments = {}
        
        for task_idx in range(num_tasks):
            # Update market state for scheduler
            shares = {k: v / max(1, self.metrics["total_tasks"]) for k, v in task_assignments.items()}
            avg_price = sum(n.price_per_sec for n in self.nodes) / max(1, len(self.nodes))
            self.scheduler.update_market_state(shares, avg_price)
            
            # Shock event midway through
            if task_idx == num_tasks // 2:
                self.disable_region("us-east")
                
            self.metrics["total_tasks"] += 1
            # Node Churn
            if random.random() < 0.1:
                new_node = self._create_node()
                self.nodes.append(new_node)
                self.reputation._scores[new_node.agent_id] = 100
            
            if random.random() < 0.05 and len(self.nodes) > 10:
                to_remove = random.choice(self.nodes)
                self.nodes.remove(to_remove)
                if to_remove.agent_id in self.reputation._scores:
                    del self.reputation._scores[to_remove.agent_id]

            task_hash = uuid.uuid4().hex
            is_critical = random.random() < 0.3
            task_value = 50.0 if is_critical else 10.0
            
            # Sync node state with reputation engine
            for n in self.nodes:
                n.reputation = self.reputation.get_score(n.agent_id)
                
            try:
                # 1. Control Plane Global Decision (Ground Truth for now)
                assignment = self.scheduler.schedule_task(
                    self.nodes, 
                    task_value, 
                    is_critical,
                    dynamic_weights=self.controller.get_scheduler_weights(),
                    redundancy_probability=self.controller.redundancy_probability,
                    seed=task_hash  # Use task_hash as deterministic seed
                )
                
                # 2. P2P Shadow Mode: Each node runs a local scheduler to see if they converge
                divergence_events = 0
                for n in self.nodes:
                    local_scheduler = MarketScheduler()
                    # Simulating node having roughly the same market state
                    local_scheduler.update_market_state(self.scheduler.market_shares, self.scheduler.market_avg_price)
                    local_assignment = local_scheduler.schedule_task(
                        self.nodes, 
                        task_value, 
                        is_critical,
                        dynamic_weights=self.controller.get_scheduler_weights(),
                        redundancy_probability=self.controller.redundancy_probability,
                        seed=task_hash
                    )
                    
                    if local_assignment["primary"].agent_id != assignment["primary"].agent_id:
                        divergence_events += 1
                
                if divergence_events > 0:
                    self.metrics["shadow_divergences"] = self.metrics.get("shadow_divergences", 0) + 1
                    
            except ValueError:
                continue
                
            primary: SimNode = assignment["primary"]
            verifiers: List[SimNode] = assignment["verifiers"]
            
            # Track market dominance
            task_assignments[primary.agent_id] = task_assignments.get(primary.agent_id, 0) + 1
            
            receipt_p = primary.execute_task(task_hash, task_value)
            
            # Base anti-fraud checks (Canary)
            if receipt_p.canary_nonce == "invalid_nonce":
                self.metrics["fraud_detected"] += 1
                penalty = self.reputation.compute_slashing_penalty(receipt_p, primary.stake, fraud_detected=True)
                primary.kern_balance -= penalty
                self.metrics["slashed_amount"] += penalty
                self.reputation._scores[primary.agent_id] -= 20
                if isinstance(primary, AdaptiveNode):
                    primary.slashed_losses += penalty
                continue
                
            if not verifiers:
                # Unverified task
                if isinstance(primary, (LazyNode, ColludingNode, AdaptiveNode)) or (isinstance(primary, SleeperNode) and primary.tasks_done > 50):
                    if receipt_p.output_hash != f"valid_output_{task_hash}":
                        self.metrics["fraud_success"] += 1
                        self.metrics["economic_loss"] += task_value
                        self.metrics["fraud_profit"] += task_value
                        if isinstance(primary, AdaptiveNode):
                            primary.fraud_profit += task_value
                else:
                    self.reputation.update_reputation(receipt_p)
                continue
                
            # Arbitration phase
            verifier = verifiers[0]
            receipt_v = verifier.execute_task(task_hash, task_value)
            
            if receipt_p.output_hash == receipt_v.output_hash:
                # Partial Collusion probability
                both_cartel = isinstance(primary, ColludingNode) and isinstance(verifier, ColludingNode)
                if both_cartel and random.random() < 0.8: # 80% collusion success
                    if receipt_p.output_hash != f"valid_output_{task_hash}":
                        self.metrics["cartel_assignments"] += 1
                        self.metrics["fraud_success"] += 1
                        self.metrics["economic_loss"] += task_value
                        self.metrics["fraud_profit"] += task_value
                elif both_cartel or (receipt_p.output_hash != f"valid_output_{task_hash}" and isinstance(primary, AdaptiveNode)):
                    # Fraud detected by redundancy
                    self.metrics["fraud_detected"] += 1
                    penalty = self.reputation.compute_slashing_penalty(receipt_p, primary.stake, fraud_detected=True)
                    self.metrics["slashed_amount"] += penalty
                    self.reputation._scores[primary.agent_id] -= 50
                    self.reputation._scores[verifier.agent_id] -= 50
                    if isinstance(primary, AdaptiveNode):
                        primary.slashed_losses += penalty
                    continue
                    
                self.reputation.update_reputation(receipt_p)
                self.reputation.update_reputation(receipt_v)
            else:
                self.metrics["fraud_detected"] += 1
                result = self.arbitration.verify_redundancy(receipt_p, receipt_v)
                penalty = self.reputation.compute_slashing_penalty(receipt_p, primary.stake, fraud_detected=True)
                self.metrics["slashed_amount"] += penalty
                if isinstance(primary, AdaptiveNode):
                    primary.slashed_losses += penalty
                
        self.reputation.apply_decay()
        
        # Calculate Market Dominance and Feed Controller
        sorted_nodes = sorted(task_assignments.items(), key=lambda x: x[1], reverse=True)
        top_5_tasks = sum(v for k, v in sorted_nodes[:5])
        self.metrics["top_5_dominance"] = top_5_tasks / max(1, num_tasks)
        
        # Compute fraud rate for controller
        fraud_rate = self.metrics["fraud_detected"] / max(1, self.metrics["total_tasks"])
        avg_cost = sum(n.price_per_sec for n in self.nodes) / max(1, len(self.nodes))
        
        controller_metrics = {
            "top_k_dominance": self.metrics["top_5_dominance"],
            "avg_cost": avg_cost,
            "fraud_rate": fraud_rate
        }
        self.controller.update(controller_metrics)
        # Apply slashing multiplier from controller
        # We just track the multiplier for metrics, the actual engine gets the multiplier if needed
        # In a real system, the engine would accept the multiplier
        
    def report(self):
        print(f"Total Tasks: {self.metrics['total_tasks']}")
        print(f"Fraud Detected: {self.metrics['fraud_detected']}")
        print(f"Fraud Success: {self.metrics['fraud_success']}")
        print(f"Cartel Assignments (Collusion undetected): {self.metrics['cartel_assignments']}")
        print(f"P2P Shadow Divergences: {self.metrics.get('shadow_divergences', 0)}")
        
        roi = (self.metrics["fraud_profit"] / self.metrics["slashed_amount"]) if self.metrics["slashed_amount"] > 0 else float("inf")
        print(f"Economic Loss: {self.metrics['economic_loss']:.2f} KERN")
        print(f"Slashed Amount: {self.metrics['slashed_amount']:.2f} KERN")
        print(f"Attack ROI: {roi:.2f} (Target: < 1.0)")
        print(f"Top 5 Node Market Dominance: {self.metrics.get('top_5_dominance', 0):.1%}")
        print("\\n--- Controller State ---")
        print(f"Dominance Penalty Weight: {self.controller.w_dominance:.2f}")
        print(f"Price Weight: {self.controller.w_price:.2f}")
        print(f"Redundancy Probability: {self.controller.redundancy_probability:.1%}")
        
        # Rep distribution
        h_rep = sum(n.reputation for n in self.nodes if isinstance(n, HonestNode)) / max(1, len([n for n in self.nodes if isinstance(n, HonestNode)]))
        c_rep = sum(n.reputation for n in self.nodes if isinstance(n, ColludingNode)) / max(1, len([n for n in self.nodes if isinstance(n, ColludingNode)]))
        print(f"Avg Honest Rep: {h_rep:.2f} | Avg Cartel Rep: {c_rep:.2f}")

if __name__ == "__main__":
    sim = SwarmSimulator()
    sim.generate_swarm(100) # 100 nodes
    print("Running 100 Epochs of 1,000 tasks (100k total tasks) with Economic Controller...")
    for epoch in range(1, 101):
        if epoch % 10 == 0:
            print(f"Completed Epoch {epoch}/100...")
        sim.run_epoch(1000)
    sim.report()
