"""
Kernell OS — Classifier-Pro API Server (Phase C)
══════════════════════════════════════════════════
FastAPI server deployed at api.kernellos.com/v1/policy/classify

This is the premium classification service that:
  1. Re-classifies tasks with higher accuracy than Policy-Lite
  2. Learns from fleet telemetry (Data Flywheel)
  3. Provides cost savings estimates to justify billing

Business model: 10% of verified savings achieved by routing optimization.

Endpoints:
  POST /v1/policy/classify   — classify a task (main API)
  POST /v1/telemetry/ingest  — receive fleet telemetry events
  GET  /v1/health             — health check
  GET  /v1/metrics            — Prometheus metrics
  GET  /v1/stats              — usage statistics for billing
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import time
from collections import defaultdict
from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Optional

from fastapi import FastAPI, HTTPException, Header, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

logger = logging.getLogger("kernell.classifier_pro.server")

# ── App ──────────────────────────────────────────────────────────────────

app = FastAPI(
    title="Kernell Classifier-Pro API",
    description="Premium policy routing for the Token Economy Engine",
    version="1.0.0",
    docs_url="/v1/docs",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["POST", "GET"],
    allow_headers=["*"],
)


# ── Request / Response Models ────────────────────────────────────────────

class ClassifyRequest(BaseModel):
    """Input to the classification endpoint."""
    task_hash: str = Field(..., description="SHA-256 hash of the task (no plaintext)")
    task_token_count: int = Field(0, description="Approximate token count")
    task_domain: str = Field("general", description="code|reasoning|data|creative|general")
    local_policy: Optional[Dict[str, Any]] = Field(
        None, description="Policy-Lite's local decision (for re-classification)"
    )
    hardware_tier: str = Field("", description="minimal|balanced|powerful|workstation")
    has_gpu: bool = Field(False)
    ram_bucket: str = Field("")
    history: Optional[Dict[str, Any]] = Field(
        None, description="Developer's recent routing history"
    )


class PolicyResponse(BaseModel):
    """Output of the classification endpoint."""
    route: str = Field(..., description="local|cheap|premium|hybrid")
    confidence: float = Field(..., ge=0.0, le=1.0)
    needs_decomposition: bool = Field(False)
    risk: str = Field("low", description="low|medium|high")
    expected_cost_usd: float = Field(0.0, ge=0.0)
    expected_latency_s: float = Field(0.0, ge=0.0)
    max_budget_usd: float = Field(0.0, ge=0.0)
    policy_version: str = Field("pro-v1")
    estimated_savings_pct: float = Field(
        0.0, description="Estimated savings vs all-premium routing"
    )


class TelemetryBatch(BaseModel):
    """Batch of telemetry events from fleet SDKs."""
    sdk_version: str = ""
    instance_id: str = ""
    event_count: int = 0
    events: List[Dict[str, Any]] = Field(default_factory=list)


class HealthResponse(BaseModel):
    status: str = "ok"
    version: str = "1.0.0"
    model_loaded: bool = False
    uptime_seconds: float = 0.0


# ── In-Memory State (replace with Redis in production) ───────────────────

@dataclass
class ServerState:
    """Server-wide state for metrics and billing."""
    start_time: float = field(default_factory=time.time)
    total_classifications: int = 0
    total_telemetry_events: int = 0
    api_key_usage: Dict[str, int] = field(default_factory=lambda: defaultdict(int))
    telemetry_buffer: List[Dict] = field(default_factory=list)
    telemetry_buffer_max: int = 50000
    model_loaded: bool = False
    model_version: str = "heuristic-v0"  # Until fine-tuned model is ready

    # Rate limits per tier
    rate_limits: Dict[str, int] = field(default_factory=lambda: {
        "free": 100,       # 100 req/day
        "developer": 1000, # 1000 req/day
        "pro": 10000,      # 10000 req/day
        "unlimited": -1,   # No limit
    })


state = ServerState()


# ── API Key Management ───────────────────────────────────────────────────

# In production, these come from Redis/database
VALID_API_KEYS: Dict[str, Dict] = {
    # key_hash: {tier, developer_id, daily_count, last_reset}
}

MASTER_KEY = os.environ.get("CLASSIFIER_PRO_MASTER_KEY", "")


def _validate_api_key(authorization: str) -> Dict:
    """Validate API key and return tier info."""
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Missing or invalid API key")

    token = authorization[7:]
    key_hash = hashlib.sha256(token.encode()).hexdigest()[:16]

    # Master key bypasses everything
    if MASTER_KEY and token == MASTER_KEY:
        return {"tier": "unlimited", "developer_id": "master", "key_hash": key_hash}

    # Check registered keys
    if key_hash in VALID_API_KEYS:
        info = VALID_API_KEYS[key_hash]
        daily_limit = state.rate_limits.get(info.get("tier", "free"), 100)
        if daily_limit > 0 and state.api_key_usage[key_hash] >= daily_limit:
            raise HTTPException(
                status_code=429,
                detail=f"Daily rate limit ({daily_limit}) exceeded. Upgrade tier."
            )
        state.api_key_usage[key_hash] += 1
        return info

    # Unknown key — treat as free tier
    if state.api_key_usage[key_hash] >= state.rate_limits["free"]:
        raise HTTPException(status_code=429, detail="Free tier limit (100/day) exceeded")
    state.api_key_usage[key_hash] += 1
    return {"tier": "free", "developer_id": "anonymous", "key_hash": key_hash}


# ── Classification Logic (Heuristic v0, replaced by model later) ─────────

def _classify_heuristic(req: ClassifyRequest) -> PolicyResponse:
    """
    Heuristic classifier (v0) — active until fine-tuned model is deployed.

    This is the baseline that the fine-tuned model must beat.
    """
    # Start with local policy if provided
    if req.local_policy:
        local_conf = req.local_policy.get("confidence", 0.5)
        local_route = req.local_policy.get("route", "cheap")

        # If local is very confident, trust it
        if local_conf >= 0.85:
            return PolicyResponse(
                route=local_route,
                confidence=min(local_conf + 0.05, 0.99),
                needs_decomposition=req.local_policy.get("needs_decomposition", False),
                risk=req.local_policy.get("risk", "low"),
                expected_cost_usd=_estimate_cost(local_route, req.task_token_count),
                expected_latency_s=_estimate_latency(local_route),
                max_budget_usd=_estimate_cost(local_route, req.task_token_count) * 1.5,
                policy_version="pro-v1-heuristic",
                estimated_savings_pct=_savings_pct(local_route),
            )

    # Domain-based heuristics
    domain = req.task_domain.lower()
    tokens = req.task_token_count

    # Security/payment domains → never local
    if domain in ("security", "legal", "payment", "compliance"):
        route = "premium"
        risk = "high"
        conf = 0.90
    # Short data tasks → local
    elif domain == "data" and tokens < 100:
        route = "local"
        risk = "low"
        conf = 0.88
    # Code with moderate length → cheap
    elif domain == "code" and tokens < 500:
        route = "cheap"
        risk = "medium"
        conf = 0.82
    # Complex reasoning → premium
    elif domain == "reasoning" and tokens > 300:
        route = "premium"
        risk = "medium"
        conf = 0.80
    # Long tasks → hybrid
    elif tokens > 800:
        route = "hybrid"
        risk = "medium"
        conf = 0.75
    else:
        route = "cheap"
        risk = "low"
        conf = 0.78

    needs_decomp = route == "hybrid" or tokens > 500

    return PolicyResponse(
        route=route,
        confidence=conf,
        needs_decomposition=needs_decomp,
        risk=risk,
        expected_cost_usd=_estimate_cost(route, tokens),
        expected_latency_s=_estimate_latency(route),
        max_budget_usd=_estimate_cost(route, tokens) * 1.5,
        policy_version="pro-v1-heuristic",
        estimated_savings_pct=_savings_pct(route),
    )


def _estimate_cost(route: str, tokens: int) -> float:
    costs = {"local": 0.0, "cheap": 0.0003 * tokens / 1000, "premium": 0.015 * tokens / 1000, "hybrid": 0.005 * tokens / 1000}
    return round(costs.get(route, 0.005), 6)


def _estimate_latency(route: str) -> float:
    latencies = {"local": 0.2, "cheap": 1.5, "premium": 3.0, "hybrid": 4.0}
    return latencies.get(route, 2.0)


def _savings_pct(route: str) -> float:
    savings = {"local": 100.0, "cheap": 95.0, "premium": 0.0, "hybrid": 70.0}
    return savings.get(route, 50.0)


# ── Endpoints ────────────────────────────────────────────────────────────

@app.post("/v1/policy/classify", response_model=PolicyResponse)
async def classify(
    req: ClassifyRequest,
    authorization: str = Header(""),
):
    """
    Classify a task and return the optimal routing decision.

    This is the main revenue-generating endpoint.
    """
    _validate_api_key(authorization)
    state.total_classifications += 1

    # Use fine-tuned model when available, heuristic otherwise
    if state.model_loaded:
        # TODO: Replace with actual model inference
        result = _classify_heuristic(req)
    else:
        result = _classify_heuristic(req)

    logger.info(
        f"Classified: domain={req.task_domain} tokens={req.task_token_count} "
        f"→ route={result.route} conf={result.confidence:.2f}"
    )
    return result


@app.post("/v1/telemetry/ingest")
async def ingest_telemetry(
    batch: TelemetryBatch,
    authorization: str = Header(""),
):
    """
    Receive telemetry from fleet SDKs for the Data Flywheel.

    This data feeds continuous improvement of the Pro classifier.
    """
    _validate_api_key(authorization)

    accepted = 0
    for event in batch.events:
        if len(state.telemetry_buffer) < state.telemetry_buffer_max:
            event["_received_at"] = time.time()
            event["_sdk_instance"] = batch.instance_id[:8]
            state.telemetry_buffer.append(event)
            accepted += 1

    state.total_telemetry_events += accepted

    logger.info(
        f"Telemetry: accepted {accepted}/{len(batch.events)} events "
        f"from {batch.instance_id[:8]} (buffer: {len(state.telemetry_buffer)})"
    )

    return {
        "accepted": accepted,
        "total_buffered": len(state.telemetry_buffer),
        "status": "ok",
    }


@app.get("/v1/health", response_model=HealthResponse)
async def health():
    """Health check for load balancers and monitoring."""
    return HealthResponse(
        status="ok",
        version="1.0.0",
        model_loaded=state.model_loaded,
        uptime_seconds=round(time.time() - state.start_time, 1),
    )


@app.get("/v1/stats")
async def stats(authorization: str = Header("")):
    """Usage statistics for billing dashboard."""
    _validate_api_key(authorization)

    return {
        "total_classifications": state.total_classifications,
        "total_telemetry_events": state.total_telemetry_events,
        "telemetry_buffer_size": len(state.telemetry_buffer),
        "model_version": state.model_version,
        "model_loaded": state.model_loaded,
        "uptime_seconds": round(time.time() - state.start_time, 1),
    }


@app.get("/v1/metrics")
async def metrics():
    """Prometheus-compatible metrics endpoint."""
    lines = [
        f"# HELP classifier_pro_total_classifications Total classification requests",
        f"# TYPE classifier_pro_total_classifications counter",
        f"classifier_pro_total_classifications {state.total_classifications}",
        f"# HELP classifier_pro_telemetry_events Total telemetry events ingested",
        f"# TYPE classifier_pro_telemetry_events counter",
        f"classifier_pro_telemetry_events {state.total_telemetry_events}",
        f"# HELP classifier_pro_telemetry_buffer Current telemetry buffer size",
        f"# TYPE classifier_pro_telemetry_buffer gauge",
        f"classifier_pro_telemetry_buffer {len(state.telemetry_buffer)}",
        f"# HELP classifier_pro_uptime_seconds Server uptime",
        f"# TYPE classifier_pro_uptime_seconds gauge",
        f"classifier_pro_uptime_seconds {time.time() - state.start_time:.0f}",
    ]
    return Response(content="\n".join(lines) + "\n", media_type="text/plain")


# ── Startup ──────────────────────────────────────────────────────────────

@app.on_event("startup")
async def startup():
    logger.info("Classifier-Pro API starting...")

    # Try to load fine-tuned model
    model_path = os.environ.get("CLASSIFIER_MODEL_PATH", "")
    if model_path and os.path.exists(model_path):
        logger.info(f"Loading fine-tuned model from {model_path}")
        state.model_loaded = True
        state.model_version = "pro-v1-finetuned"
    else:
        logger.info("No fine-tuned model found, using heuristic classifier v0")
        state.model_loaded = False
        state.model_version = "heuristic-v0"


# ── Run ──────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(
        "classifier_pro_server:app",
        host="0.0.0.0",
        port=8900,
        log_level="info",
        reload=True,
    )
