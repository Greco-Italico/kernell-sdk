import time
import threading
from dataclasses import dataclass
from typing import Dict, List, Optional

# Minimum cost floor to prevent micro-execution underpricing abuse
MIN_COST = 0.01

@dataclass
class Plan:
    """SaaS plan definition — controls limits and pricing per tenant tier."""
    name: str
    rate_limit: float        # req/sec
    burst: int
    max_concurrency: int
    max_memory_mb: int
    price_per_unit: float    # cost multiplier (lower = cheaper per unit)

PLANS: Dict[str, Plan] = {
    "free":       Plan("free",       rate_limit=5,   burst=10,   max_concurrency=2,  max_memory_mb=128,  price_per_unit=1.0),
    "pro":        Plan("pro",        rate_limit=50,  burst=100,  max_concurrency=10, max_memory_mb=512,  price_per_unit=0.8),
    "enterprise": Plan("enterprise", rate_limit=500, burst=1000, max_concurrency=50, max_memory_mb=2048, price_per_unit=0.5),
}

class UsageRecord:
    def __init__(self, tenant_id: str, duration: float, memory_mb: int, cost: float):
        self.tenant_id = tenant_id
        self.duration = duration
        self.memory_mb = memory_mb
        self.cost = cost
        self.timestamp = time.time()

class TenantAccount:
    def __init__(self, credits: float, plan: Plan):
        self.credits = credits
        self.plan = plan
        self.lock = threading.Lock()
        self.usage_history: List[UsageRecord] = []

class BillingManager:
    def __init__(self):
        self.accounts: Dict[str, TenantAccount] = {}
        self.lock = threading.Lock()

    def get_account(self, tenant_id: str, plan_name: str = "free") -> TenantAccount:
        with self.lock:
            if tenant_id not in self.accounts:
                plan = PLANS.get(plan_name, PLANS["free"])
                self.accounts[tenant_id] = TenantAccount(credits=1000.0, plan=plan)
            return self.accounts[tenant_id]

    def reserve(self, account: TenantAccount, amount: float = 1.0) -> bool:
        """Pre-charge a minimum amount before execution."""
        with account.lock:
            if account.credits < amount:
                return False
            account.credits -= amount
            return True

    def settle(self, account: TenantAccount, tenant_id: str, duration_sec: float, memory_mb: int, reserved: float = 1.0):
        """Calculate final cost with plan multiplier, enforce MIN_COST, adjust balance, log."""
        base_cost_per_ms = 0.0001
        mem_multiplier = max(1, memory_mb / 128)
        raw_cost = (duration_sec * 1000) * base_cost_per_ms * mem_multiplier
        
        # Apply plan discount
        actual_cost = max(MIN_COST, raw_cost * account.plan.price_per_unit)
        
        refund = reserved - actual_cost
        
        record = UsageRecord(tenant_id, duration_sec, memory_mb, actual_cost)
        
        with account.lock:
            account.credits += refund
            account.usage_history.append(record)
