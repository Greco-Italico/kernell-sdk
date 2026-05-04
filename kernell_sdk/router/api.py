from fastapi import FastAPI, HTTPException
from typing import List, Dict, Any
import redis
import copy
from pydantic import BaseModel

from kernell_sdk.router.simulation_engine import SimulationEngine, WALEventAdapter
from kernell_sdk.router.reconciler import reconcile_from_timelines
from kernell_sdk.router.reconciliation_executor import ReconciliationExecutor

app = FastAPI(title="Kernell Incident Time Travel UI API")

try:
    redis_client = redis.Redis(host="localhost", port=6379, decode_responses=True)
    adapter = WALEventAdapter(redis_client)
except Exception:
    redis_client = None
    adapter = None


@app.get("/timeline/{request_id}")
def get_timeline(request_id: str, region: str = "A") -> List[Dict[str, Any]]:
    if not adapter:
        raise HTTPException(status_code=500, detail="Redis connection failed")
        
    events = adapter.fetch_range()
    filtered = [e for e in events if e.request_id == request_id]

    if not filtered:
        raise HTTPException(status_code=404, detail="Request not found in WAL")

    engine = SimulationEngine(filtered)
    try:
        engine.build()
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Simulation failed: {e}")

    timeline = [
        {
            "ts": f.ts,
            "event": f.event.type,
            "epoch": f.event.epoch,
            "state": f.state["state"],
            "fingerprint": f.fingerprint,
            "history_len": len(f.state.get("history", [])),
            "state_snapshot": f.state
        }
        for f in engine.timeline
    ]
    
    if region == "B" and len(timeline) > 1:
        # Mock region B divergence for testing
        timeline = copy.deepcopy(timeline)
        timeline[-1]["state"] = "FAILED"
        timeline[-1]["state_snapshot"]["state"] = "FAILED"
        timeline[-1]["fingerprint"] = "0000000000000000000000000000000000000000000000000000000000000000"

    return timeline


@app.get("/diff/{request_id}")
def diff_execution(request_id: str) -> Dict[str, Any]:
    timeline = get_timeline(request_id)
    fingerprints = [f["fingerprint"] for f in timeline]

    return {
        "final_fingerprint": fingerprints[-1] if fingerprints else None,
        "unique_fingerprints": len(set(fingerprints)),
        "divergence": len(set(fingerprints)) > 1,
        "timeline_length": len(timeline)
    }


@app.get("/reconcile/{request_id}")
def reconcile_request(request_id: str):
    try:
        timeline_a = get_timeline(request_id, region="A")
        timeline_b = get_timeline(request_id, region="B")
    except Exception as e:
        return {"error": str(e)}

    result = reconcile_from_timelines(timeline_a, timeline_b)
    return result.to_dict()


class ExecuteDecision(BaseModel):
    action: str
    winner: str
    reason: str

@app.post("/reconcile/{request_id}/execute")
def execute_reconciliation(request_id: str, decision: ExecuteDecision):
    executor = ReconciliationExecutor(redis_a=redis_client, redis_b=redis_client)
    try:
        result = executor.execute(request_id, decision.dict())
        return result
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Kernell Pay — Financial Ledger API
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

from kernell_sdk.router.kernell_pay import (
    Ledger, LedgerEntry, SettlementEngine, PaymentHook, InsufficientFundsError
)

ledger = Ledger(redis_client) if redis_client else None
settlement = SettlementEngine(ledger) if ledger else None


class CreditRequest(BaseModel):
    account_id: str
    amount: int
    memo: str = "Manual credit"

class HoldRequest(BaseModel):
    account_id: str
    amount: int
    request_id: str

class CaptureRequest(BaseModel):
    account_id: str
    amount: int
    request_id: str

class ReleaseRequest(BaseModel):
    account_id: str
    amount: int
    request_id: str


@app.get("/pay/balance/{account_id}")
def get_balance(account_id: str):
    if not ledger:
        raise HTTPException(status_code=500, detail="Ledger not available")
    balance = ledger.get_balance(account_id)
    available = ledger.get_available_balance(account_id)
    return {
        "account_id": account_id,
        "balance": balance,
        "available": available,
        "currency": "KERN"
    }


@app.get("/pay/ledger/{account_id}")
def get_ledger_entries(account_id: str):
    if not ledger:
        raise HTTPException(status_code=500, detail="Ledger not available")
    entries = ledger.get_entries(account_id=account_id)
    return {"account_id": account_id, "entries": entries, "count": len(entries)}


@app.post("/pay/credit")
def credit_account(req: CreditRequest):
    if not ledger:
        raise HTTPException(status_code=500, detail="Ledger not available")
    ledger.append(LedgerEntry(
        account_id=req.account_id,
        delta=req.amount,
        entry_type="CREDIT",
        request_id="manual",
        memo=req.memo
    ))
    return {
        "status": "credited",
        "account_id": req.account_id,
        "amount": req.amount,
        "new_balance": ledger.get_balance(req.account_id)
    }


@app.post("/pay/hold")
def hold_funds(req: HoldRequest):
    if not settlement:
        raise HTTPException(status_code=500, detail="Settlement not available")
    try:
        result = settlement.hold(req.account_id, req.amount, req.request_id)
        return result
    except InsufficientFundsError as e:
        raise HTTPException(status_code=402, detail=str(e))


@app.post("/pay/capture")
def capture_funds(req: CaptureRequest):
    if not settlement:
        raise HTTPException(status_code=500, detail="Settlement not available")
    result = settlement.capture(req.account_id, req.amount, req.request_id)
    return result


@app.post("/pay/release")
def release_funds(req: ReleaseRequest):
    if not settlement:
        raise HTTPException(status_code=500, detail="Settlement not available")
    result = settlement.release(req.account_id, req.amount, req.request_id)
    return result


@app.get("/pay/wallets")
def get_wallet_balances():
    if not ledger:
        raise HTTPException(status_code=500, detail="Ledger not available")
    return {
        "system": ledger.get_balance("system"),
        "fee": ledger.get_balance("fee"),
        "treasury": ledger.get_balance("treasury"),
        "currency": "KERN"
    }


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Marketplace API
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

from kernell_sdk.router.marketplace import (
    Skill, SkillRegistry, PurchaseEngine, SkillNotFoundError
)

skill_registry = SkillRegistry(redis_client) if redis_client else None
purchase_engine = PurchaseEngine(ledger, skill_registry) if ledger and skill_registry else None


class RegisterSkillRequest(BaseModel):
    skill_id: str
    creator_id: str
    name: str
    price: int
    description: str = ""
    category: str = "general"

class PurchaseRequest(BaseModel):
    buyer_id: str
    skill_id: str

class CompleteRequest(BaseModel):
    purchase_id: str
    buyer_id: str
    skill_id: str
    creator_id: str
    price: int


@app.get("/marketplace/skills")
def list_skills():
    if not skill_registry:
        raise HTTPException(status_code=500, detail="Registry not available")
    return {"skills": skill_registry.list_all()}


@app.post("/marketplace/skills")
def register_skill(req: RegisterSkillRequest):
    if not skill_registry:
        raise HTTPException(status_code=500, detail="Registry not available")
    skill = Skill(req.skill_id, req.creator_id, req.name, req.price,
                  description=req.description, category=req.category)
    skill_registry.register(skill)
    return {"status": "registered", "skill": skill.to_dict()}


@app.post("/marketplace/purchase")
def purchase_skill(req: PurchaseRequest):
    if not purchase_engine:
        raise HTTPException(status_code=500, detail="Engine not available")
    try:
        result = purchase_engine.purchase(req.buyer_id, req.skill_id)
        return result
    except InsufficientFundsError as e:
        raise HTTPException(status_code=402, detail=str(e))
    except SkillNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))


@app.post("/marketplace/complete")
def complete_purchase(req: CompleteRequest):
    if not purchase_engine:
        raise HTTPException(status_code=500, detail="Engine not available")
    result = purchase_engine.complete(
        req.purchase_id, req.buyer_id, req.skill_id, req.creator_id, req.price
    )
    return result


@app.get("/marketplace/earnings/{creator_id}")
def creator_earnings(creator_id: str):
    if not purchase_engine:
        raise HTTPException(status_code=500, detail="Engine not available")
    return purchase_engine.get_creator_earnings(creator_id)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Hardening Pack API
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

from kernell_sdk.router.hardening import (
    HoldExpirationEngine, ExportEngine, DurabilityManager
)

expiration_engine = HoldExpirationEngine(redis_client, settlement) if redis_client and settlement else None
export_engine = ExportEngine(redis_client) if redis_client else None
durability_manager = DurabilityManager(redis_client) if redis_client else None


@app.post("/system/holds/sweep")
def sweep_expired_holds():
    if not expiration_engine:
        raise HTTPException(status_code=500, detail="Expiration Engine not available")
    released = expiration_engine.sweep_expired()
    return {"status": "success", "swept": len(released), "details": released}

@app.get("/system/holds/active")
def get_active_holds():
    if not expiration_engine:
        raise HTTPException(status_code=500, detail="Expiration Engine not available")
    active = expiration_engine.get_active_holds()
    return {"status": "success", "count": len(active), "holds": active}


@app.get("/export/ledger")
def export_full_ledger(account_id: str = None, since: float = None):
    if not export_engine:
        raise HTTPException(status_code=500, detail="Export Engine not available")
    return export_engine.export_ledger(account_id, since)

@app.get("/export/execution/{request_id}")
def export_execution_log(request_id: str):
    if not export_engine:
        raise HTTPException(status_code=500, detail="Export Engine not available")
    return export_engine.export_execution(request_id)

@app.get("/export/reconciliation/{request_id}")
def export_reconciliation_log(request_id: str):
    if not export_engine:
        raise HTTPException(status_code=500, detail="Export Engine not available")
    return export_engine.export_reconciliation(request_id)


@app.get("/system/durability/verify")
def verify_durability():
    if not durability_manager:
        raise HTTPException(status_code=500, detail="Durability Manager not available")
    return durability_manager.verify()

@app.post("/system/durability/harden")
def harden_durability():
    if not durability_manager:
        raise HTTPException(status_code=500, detail="Durability Manager not available")
    return durability_manager.harden()


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Failover & Leasing API
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

from kernell_sdk.router.lease_manager import LeaseManager
import time

REGION = os.getenv("KERNELL_REGION", "A")
lease_manager = LeaseManager(redis_client, REGION) if redis_client else None

@app.post("/system/failover/{request_id}")
def force_failover(request_id: str, ttl: float = 300.0):
    if not lease_manager:
        raise HTTPException(status_code=500, detail="Lease Manager not available")
    
    current = lease_manager.get(request_id)
    old_holder = current["holder"] if current else "UNKNOWN"
    
    new_lease = lease_manager.takeover(request_id, ttl=ttl)
    
    # Append the FAILOVER event to the WAL
    if redis_client:
        event = {
            "type": "FAILOVER",
            "request_id": request_id,
            "epoch": new_lease["epoch"],
            "from": old_holder,
            "to": REGION,
            "ts": time.time()
        }
        redis_client.xadd("kernell:wal", event)

    return {
        "status": "takeover_complete",
        "request_id": request_id,
        "new_epoch": new_lease["epoch"],
        "holder": new_lease["holder"]
    }


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
