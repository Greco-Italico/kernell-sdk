"""
Economic Orchestrator
=====================
Transforms a basic EconomicAgent into an autonomous multi-agent economic system.
This Orchestrator manages specialized sub-agents to handle Finances, Commerce,
Compute reselling, and Strategy.
"""
from typing import Dict, Any, Optional
from decimal import Decimal
import time

from kernell_sdk.economic_agent import EconomicAgent

class SubAgent:
    """Base class for specialized economic modules."""
    def __init__(self, orchestrator: "EconomicOrchestrator"):
        self.orchestrator = orchestrator
        self.core = orchestrator.core_agent

class FinanceAgent(SubAgent):
    """Manages the treasury, processes payments, and evaluates compliance risk."""
    
    def get_balance(self) -> Decimal:
        return self.core.get_balance()
        
    def check_risk(self, target_agent_id: str) -> bool:
        """Evaluates counterparty compliance (FinCEN grade check)."""
        return self.core.check_counterparty(target_agent_id)
        
    def execute_payment(self, target_agent_id: str, service_id: str, amount: Decimal) -> str:
        """Executes a Taint-Aware Escrow and returns the contract ID."""
        return self.core.buy_service(target_agent_id, service_id, amount)

class ComputeAgent(SubAgent):
    """Manages system resources and sells idle compute to the network."""
    
    def is_idle(self) -> bool:
        # Placeholder for actual hardware utilization metrics
        return True
        
    def sell_compute_time(self, expected_kern: Decimal):
        """Offers compute time to the network to earn KERN."""
        print(f"[{self.core.agent_id[:8]}] ⚡ ComputeAgent: Selling idle CPU to earn {expected_kern} KERN...")
        self.core.offer_compute(price_per_sec=Decimal("0.05"), specs={"cpu": 4, "ram": "8GB"})
        # In a real scenario, this would block until a job is received and settled
        # For the SDK, we simulate receiving the funds via the dev mint endpoint
        self.core.http.post(f"{self.core.api_url}/dev/mint", json={
            "agent_id": self.core.agent_id, 
            "amount": str(expected_kern), 
            "is_tainted": False
        })
        time.sleep(1)
        print(f"[{self.core.agent_id[:8]}] ⚡ ComputeAgent: Earned {expected_kern} KERN!")

class CommerceAgent(SubAgent):
    """Manages pricing, negotiation, and service delivery."""
    
    def deliver_service(self, contract_id: str, client_id: str, amount: Decimal, result_data: str):
        """Delivers the work and settles the escrow to get paid."""
        print(f"[{self.core.agent_id[:8]}] 🛒 CommerceAgent: Delivering service for {amount} KERN...")
        self.core.fulfill_order(contract_id, client_id, amount, result_data)

class StrategyAgent(SubAgent):
    """The brain. Decides when to work, when to buy, and when to sell compute."""
    
    def solve_objective(self, task: str, required_service: str, target_agent: str, cost: Decimal):
        """Attempts to solve a task. If broke, it sells compute first to fund the purchase."""
        print(f"\n[{self.core.agent_id[:8]}] 🧠 StrategyAgent: Objective -> '{task}'")
        
        balance = self.orchestrator.finance.get_balance()
        if balance < cost:
            shortfall = cost - balance
            print(f"[{self.core.agent_id[:8]}] 🧠 StrategyAgent: Insufficient funds (Balance: {balance}, Cost: {cost}). Initiating fallback plan...")
            self.orchestrator.compute.sell_compute_time(shortfall)
            
        print(f"[{self.core.agent_id[:8]}] 🧠 StrategyAgent: Funds secured. Proceeding with purchase.")
        
        if not self.orchestrator.finance.check_risk(target_agent):
            print(f"[{self.core.agent_id[:8]}] 🧠 StrategyAgent: ABORT! Counterparty {target_agent} failed Compliance check.")
            return False
            
        contract_id = self.orchestrator.finance.execute_payment(target_agent, required_service, cost)
        print(f"[{self.core.agent_id[:8]}] 🧠 StrategyAgent: Contract {contract_id} locked. Awaiting service delivery...")
        return contract_id

class EconomicOrchestrator:
    """
    The orchestrator wrapper.
    It encapsulates the low-level EconomicAgent and splits its intelligence 
    into specialized sub-agents.
    """
    def __init__(self, core_agent: EconomicAgent):
        self.core_agent = core_agent
        
        # Initialize Sub-agents
        self.finance = FinanceAgent(self)
        self.compute = ComputeAgent(self)
        self.commerce = CommerceAgent(self)
        self.strategy = StrategyAgent(self)
        
    def run_task(self, task_name: str, required_service: str, provider_id: str, cost: Decimal):
        """High-level entry point for developers."""
        return self.strategy.solve_objective(task_name, required_service, provider_id, cost)
