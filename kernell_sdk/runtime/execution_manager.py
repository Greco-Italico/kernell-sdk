from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, asdict
from typing import Any, Callable, Dict, Optional, Protocol, Tuple


def input_adjustment_micro(task: Dict[str, Any], *, cap: int = 15_000) -> int:
    """
    Deterministic extra cost from prompt/input size (micro-KERN).
    Keeps estimate and executor aligned when the API passes task["input"].
    """
    n = len(str(task.get("input") or ""))
    return min(n * 20, cap)


class BudgetExceededError(Exception):
    """Raised when estimated execution exceeds the caller max budget."""


class PreAuthorizationError(Exception):
    """Raised when hold/pre-authorization fails."""


class SpendGuardPort(Protocol):
    def pre_authorize(self, tenant_id: str, estimated_cost_micro: int) -> Tuple[bool, Optional[str]]: ...
    def capture_usage(self, hold_id: str, amount_micro: int) -> None: ...
    def finalize_hold(self, hold_id: str) -> None: ...


class CostEstimatorPort(Protocol):
    def estimate_micro(self, task: Dict[str, Any]) -> int: ...
    def actual_micro(self, task: Dict[str, Any], usage: Dict[str, Any], estimated_micro: int) -> int: ...


@dataclass
class ExecutionLedgerEntry:
    execution_id: str
    tenant_id: str
    task_type: str
    estimated_micro: int
    reserved_micro: int
    captured_micro: int
    refunded_micro: int
    status: str
    duration_ms: int
    error: Optional[str] = None


class InMemoryLedger:
    def __init__(self) -> None:
        self._entries: list[ExecutionLedgerEntry] = []

    def record(self, entry: ExecutionLedgerEntry) -> None:
        self._entries.append(entry)

    def list_entries(self) -> list[dict]:
        return [asdict(e) for e in self._entries]


class DefaultCostEstimator:
    """
    Deterministic v1 estimator in micro-units.

    The mapping intentionally mirrors current task classes to avoid hidden pricing.
    """

    _TASK_ESTIMATE_MICRO = {
        "simple": 26_000,
        "multi_agent": 39_000,
        "financial": 65_000,
        "autonomous_loop": 78_000,
        "default": 26_000,
    }

    def estimate_micro(self, task: Dict[str, Any]) -> int:
        task_type = str(task.get("task_type", "default"))
        base = self._TASK_ESTIMATE_MICRO.get(task_type, self._TASK_ESTIMATE_MICRO["default"])
        return base + input_adjustment_micro(task)

    def actual_micro(self, task: Dict[str, Any], usage: Dict[str, Any], estimated_micro: int) -> int:
        # Priority 1: explicit usage cost from executor
        usage_cost = usage.get("cost_micro")
        if isinstance(usage_cost, int) and usage_cost >= 0:
            return usage_cost
        # Priority 2: fallback to estimated cost to preserve deterministic behavior
        return estimated_micro


class ExecutionManager:
    """
    Single entrypoint for economic execution.

    Flow:
      estimate -> max budget check -> HOLD -> execute -> CAPTURE -> RELEASE/refund -> ledger
    """

    def __init__(
        self,
        executor: Callable[[Dict[str, Any]], Tuple[Any, Dict[str, Any]]],
        spend_guard: SpendGuardPort,
        cost_estimator: Optional[CostEstimatorPort] = None,
        ledger: Optional[InMemoryLedger] = None,
    ) -> None:
        self._executor = executor
        self._spend_guard = spend_guard
        self._cost_estimator = cost_estimator or DefaultCostEstimator()
        self._ledger = ledger or InMemoryLedger()

    def execute(
        self,
        tenant_id: str,
        task: Dict[str, Any],
        *,
        max_budget_micro: Optional[int] = None,
        idempotency_key: Optional[str] = None,
    ) -> Dict[str, Any]:
        import os
        from core.security.locks import get_tenant_lock
        from core.security.events import record_security_event, emit_execution_event

        # Dual-mode idempotency: Postgres when USE_DB=1, Redis otherwise
        if os.getenv("USE_DB", "0") == "1":
            from core.payments.db_idempotency import DBIdempotencyManager as IdempotencyManager
        else:
            from core.security.idempotency import IdempotencyManager
        
        execution_id = str(uuid.uuid4())
        emit_execution_event("EXECUTION_STARTED", tenant_id, execution_id=execution_id, task_type=task.get("task_type", "simple"))
        
        idem = None
        # 1. Idempotency Check
        if idempotency_key:
            idem = IdempotencyManager(tenant_id, idempotency_key)
            existing = idem.get()
            if existing:
                if existing["status"] == "completed":
                    emit_execution_event("IDEMPOTENCY_HIT", tenant_id, execution_id=execution_id)
                    record_security_event("REPLAY_BLOCKED", f"Replay detected for idempotency_key: {idempotency_key}", "LOW", tenant_id, execution_id=execution_id)
                    # Feed risk engine
                    try:
                        from core.security.risk_engine import get_risk_engine
                        get_risk_engine().record_replay(tenant_id)
                    except Exception:
                        pass
                    return existing["response"]
                if existing["status"] == "processing":
                    emit_execution_event("IDEMPOTENCY_PROCESSING", tenant_id, execution_id=execution_id)
                    raise Exception("REQUEST_IN_PROGRESS")
            if not idem.start():
                raise Exception("IDEMPOTENCY_RACE")

        try:
            with get_tenant_lock(tenant_id):
                emit_execution_event("LOCK_ACQUIRED", tenant_id, execution_id=execution_id)
                task_type = str(task.get("task_type", "simple"))
                started = time.time()
                estimated_micro = self._cost_estimator.estimate_micro(task)

                if max_budget_micro is not None and estimated_micro > max_budget_micro:
                    raise BudgetExceededError(
                        f"estimated={estimated_micro} exceeds max_budget={max_budget_micro}"
                    )

                # ── RISK ENGINE GATE ──────────────────────────────
                try:
                    from core.security.risk_engine import get_risk_engine
                    risk = get_risk_engine().evaluate(tenant_id)
                    if risk.blocked:
                        emit_execution_event("RISK_BLOCKED", tenant_id, execution_id=execution_id, risk_score=risk.risk_score, flags=risk.flags)
                        record_security_event("RISK_BLOCKED", f"Risk score {risk.risk_score} exceeded threshold. Flags: {risk.flags}", "HIGH", tenant_id, execution_id=execution_id)
                        
                        try:
                            from core.alerts.alert_manager import get_alert_manager
                            get_alert_manager().send("CRITICAL", "RISK_BLOCKED", {
                                "risk_score": risk.risk_score,
                                "flags": ", ".join(risk.flags)
                            }, tenant_id)
                        except ImportError:
                            pass
                        
                        raise PreAuthorizationError(f"RISK_BLOCKED: score={risk.risk_score}")
                    elif risk.risk_score > 0.4:
                        emit_execution_event("RISK_ELEVATED", tenant_id, execution_id=execution_id, risk_score=risk.risk_score, flags=risk.flags)
                except ImportError:
                    pass  # Risk engine not available, skip
                except Exception as e:
                    import logging
                    logging.getLogger(__name__).warning(f"Risk engine failed (fail-open): {e}")
                # ──────────────────────────────────────────────────

                # ── ECONOMIC RATE LIMITER GATE ────────────────────
                try:
                    from core.security.economic_rate_limiter import get_rate_limiter
                    limiter = get_rate_limiter()
                    allowed, current_spend = limiter.check(tenant_id, estimated_micro)
                    if not allowed:
                        emit_execution_event("RATE_LIMIT_BLOCKED", tenant_id, execution_id=execution_id, spend=current_spend, limit=limiter._limit)
                        record_security_event("RATE_LIMIT_BLOCKED", f"Economic rate limit exceeded. Spend: {current_spend}μ, Limit: {limiter._limit}μ", "HIGH", tenant_id, execution_id=execution_id)
                        
                        try:
                            from core.alerts.alert_manager import get_alert_manager
                            get_alert_manager().send("CRITICAL", "RATE_LIMIT_BLOCKED", {
                                "attempted_micro": estimated_micro,
                                "current_spend_micro": current_spend,
                                "limit_micro": limiter._limit
                            }, tenant_id)
                        except ImportError:
                            pass
                        
                        raise PreAuthorizationError(f"RATE_LIMIT_BLOCKED: Exceeded economic rate limit.")
                except ImportError:
                    pass
                except Exception as e:
                    import logging
                    logging.getLogger(__name__).warning(f"Rate limiter failed (fail-open): {e}")
                # ──────────────────────────────────────────────────

                # Reserve with 20% headroom to avoid over-capture for small estimation drift.
                reserved_micro = int(estimated_micro * 1.2)
                allowed, hold_id = self._spend_guard.pre_authorize(tenant_id, reserved_micro)
                if not allowed or not hold_id:
                    raise PreAuthorizationError("spend_guard pre_authorize denied")

                emit_execution_event("HOLD_CREATED", tenant_id, execution_id=execution_id, amount=reserved_micro, estimated=estimated_micro)

                result: Any = None
                status = "failed"
                error: Optional[str] = None
                captured_micro = 0
                usage: Dict[str, Any] = {}

                try:
                    result, usage = self._executor(task)
                    actual_micro = self._cost_estimator.actual_micro(task, usage, estimated_micro)
                    captured_micro = min(max(actual_micro, 0), reserved_micro)
                    
                    refunded_micro = max(reserved_micro - captured_micro, 0)
                    from core.security.economic_invariants import EconomicInvariants
                    try:
                        EconomicInvariants.validate_transaction(
                            estimated_micro=estimated_micro,
                            actual_micro=captured_micro,
                            reserved_micro=reserved_micro,
                            refunded_micro=refunded_micro,
                        )
                    except Exception as invariant_err:
                        record_security_event(
                            "ECONOMIC_EXPLOIT",
                            f"Invariant check failed: {invariant_err}",
                            "CRITICAL",
                            tenant_id,
                            execution_id=execution_id
                        )
                        emit_execution_event("INVARIANT_FAILED", tenant_id, execution_id=execution_id)
                        raise

                    self._spend_guard.capture_usage(hold_id, captured_micro)
                    status = "completed"
                    emit_execution_event("EXECUTION_COMPLETED", tenant_id, execution_id=execution_id)
                except Exception as exc:  # noqa: BLE001
                    emit_execution_event("ROLLBACK", tenant_id, execution_id=execution_id)
                    emit_execution_event("REFUND", tenant_id, execution_id=execution_id)
                    error = str(exc)
                    raise
                finally:
                    self._spend_guard.finalize_hold(hold_id)
                    refunded_micro = max(reserved_micro - captured_micro, 0)
                    duration_ms = int((time.time() - started) * 1000)
                    self._ledger.record(
                        ExecutionLedgerEntry(
                            execution_id=execution_id,
                            tenant_id=tenant_id,
                            task_type=task_type,
                            estimated_micro=estimated_micro,
                            reserved_micro=reserved_micro,
                            captured_micro=captured_micro,
                            refunded_micro=refunded_micro,
                            status=status,
                            duration_ms=duration_ms,
                            error=error,
                        )
                    )

                    # ── Feed risk engine ──────────────────────────
                    try:
                        from core.security.risk_engine import get_risk_engine
                        engine = get_risk_engine()
                        engine.record_spend(tenant_id, captured_micro)
                        engine.record_execution(tenant_id, status == "completed")
                    except Exception:
                        pass
                    # ──────────────────────────────────────────────
                    
                    # ── Feed rate limiter ─────────────────────────
                    try:
                        from core.security.economic_rate_limiter import get_rate_limiter
                        get_rate_limiter().record(tenant_id, captured_micro)
                    except Exception:
                        pass
                    # ──────────────────────────────────────────────

                final_result = {
                    "execution_id": execution_id,
                    "status": status,
                    "result": result,
                    "cost_estimated_micro": estimated_micro,
                    "cost_actual_micro": captured_micro,
                    "refunded_micro": max(reserved_micro - captured_micro, 0),
                    "usage": usage,
                }
                if idem:
                    idem.complete(final_result)
                
                emit_execution_event("LOCK_RELEASED", tenant_id, execution_id=execution_id)
                return final_result

        except TimeoutError as te:
            emit_execution_event("LOCK_TIMEOUT", tenant_id, execution_id=execution_id)
            record_security_event("LOCK_TIMEOUT", f"Concurrent requests exceeded lock timeout for tenant: {tenant_id}", "HIGH", tenant_id, execution_id=execution_id)
            if idem:
                idem.fail(te)
            raise ValueError("Too many concurrent requests. Please try again later.") from te
        except Exception as e:
            if idem:
                idem.fail(e)
            raise

    def ledger_entries(self) -> list[dict]:
        return self._ledger.list_entries()
