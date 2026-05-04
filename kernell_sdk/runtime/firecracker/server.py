"""
Kernell OS Firecracker Control Plane — HTTP server.

uvicorn and fastapi are intentionally imported lazily (inside functions)
so that importing this module does NOT require the [gui] extras to be
installed.  Only calling ``main()`` or accessing ``app`` requires them.
"""
from __future__ import annotations

import os
import secrets as secrets_module
from typing import TYPE_CHECKING, Dict, Optional

# ── Lazy-load heavy web framework deps ──────────────────────────────────────
# This prevents ImportError when kernell_sdk is imported in environments
# where uvicorn/fastapi are not installed (e.g. pure agent deployments).
try:
    import uvicorn
    from contextlib import asynccontextmanager
    from fastapi import FastAPI, BackgroundTasks, HTTPException, Request
    from pydantic import BaseModel
    _WEB_AVAILABLE = True
except ImportError:
    _WEB_AVAILABLE = False
    uvicorn = None  # type: ignore[assignment]

    def asynccontextmanager(f): return f  # type: ignore[assignment]

    class FastAPI:  # type: ignore[no-redef]  # noqa: N801
        """Stub so the module is importable without fastapi installed."""
        def __init__(self, **_): pass
        def get(self, *a, **kw): return lambda f: f
        def post(self, *a, **kw): return lambda f: f
        def on_event(self, *a, **kw): return lambda f: f

    class Request:  # type: ignore[no-redef]
        headers: dict = {}

    class HTTPException(Exception):  # type: ignore[no-redef]
        def __init__(self, status_code=500, detail=""): pass

    class BaseModel:  # type: ignore[no-redef]
        pass
# ─────────────────────────────────────────────────────────────────────────────

# Load Prometheus metrics early so they register before the server starts
from . import metrics as prom
from .orchestrator import RuntimeOrchestrator
from ..firecracker_runtime import FirecrackerRuntime
from ..models import ExecutionRequest

# Global instances — managed by lifespan context
runtime = None
orchestrator = None


@asynccontextmanager
async def _lifespan(application):
    """FastAPI lifespan: replaces deprecated on_event('startup'/'shutdown')."""
    global runtime, orchestrator

    # ── Startup ──────────────────────────────────────────────────────────────
    kernel_path = os.getenv("FC_KERNEL", "/var/lib/kernell/vmlinux")
    rootfs_path = os.getenv("FC_ROOTFS", "/var/lib/kernell/rootfs.ext4")

    if not os.path.exists(kernel_path):
        print(f"WARN: Kernel not found at {kernel_path} (safe to ignore in mock/test mode)")

    runtime = FirecrackerRuntime(kernel_path, rootfs_path)

    # 50 worker threads — Enterprise Max Concurrency tier
    orchestrator = RuntimeOrchestrator(runtime, num_workers=50)
    orchestrator.start()

    # Prometheus metrics on a separate port
    prom.start_metrics_server(port=9090)
    print("Prometheus metrics exposed on port 9090")

    yield  # ← server runs here

    # ── Shutdown ─────────────────────────────────────────────────────────────
    if orchestrator:
        orchestrator.stop()
    if runtime and hasattr(runtime.pool, "running"):
        runtime.pool.running = False


app = FastAPI(title="Kernell OS Firecracker Control Plane", lifespan=_lifespan)


class ExecutePayload(BaseModel):
    code: str
    timeout: int = 2
    memory_limit_mb: int = 128
    tenant_id: str = "default_tenant"
    request_id: Optional[str] = None


def _require_control_token(request) -> None:
    token = os.getenv("FC_CONTROL_TOKEN", "").strip()
    if not token:
        raise HTTPException(status_code=503, detail="ControlPlaneUnavailable: FC_CONTROL_TOKEN not configured")
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Unauthorized")
    presented = auth[7:].strip()
    if not secrets_module.compare_digest(presented, token):
        raise HTTPException(status_code=401, detail="Unauthorized")


@app.get("/healthz")
async def healthz():
    return {"status": "ok"}


@app.post("/execute")
async def execute(payload: ExecutePayload, request: Request):
    _require_control_token(request)
    req = ExecutionRequest(
        code=payload.code,
        timeout=payload.timeout,
        memory_limit_mb=payload.memory_limit_mb,
        tenant_id=payload.tenant_id,
        request_id=payload.request_id
    )
    
    # Use the orchestrator (Fair Queuing + Async Worker Pool) instead of direct execution
    future = orchestrator.submit(req, request_id=payload.request_id)
    result = future.result()  # Blocks until execution completes or fails
    
    if result.exit_code == 402:
        raise HTTPException(status_code=402, detail=result.stderr)
    if result.exit_code == 429:
        raise HTTPException(status_code=429, detail=result.stderr)
    if result.exit_code == 503:
        raise HTTPException(status_code=503, detail=result.stderr)
        
    return {
        "stdout": result.stdout,
        "stderr": result.stderr,
        "exit_code": result.exit_code,
        "timed_out": result.timed_out
    }


def main():
    if not _WEB_AVAILABLE:
        raise ImportError(
            "uvicorn and fastapi are required to run the Firecracker control plane. "
            "Install them with: pip install 'kernell-os[gui]'"
        )
    host = os.getenv("FC_CONTROL_PLANE_HOST", "127.0.0.1")
    port = int(os.getenv("FC_CONTROL_PLANE_PORT", "8080"))
    if host == "0.0.0.0" and not os.getenv("FC_ALLOW_PUBLIC_BIND", "").strip():
        raise RuntimeError("Refusing to bind 0.0.0.0 without FC_ALLOW_PUBLIC_BIND=1")
    uvicorn.run("kernell_sdk.runtime.firecracker.server:app", host=host, port=port, log_level="warning")


if __name__ == "__main__":
    main()
