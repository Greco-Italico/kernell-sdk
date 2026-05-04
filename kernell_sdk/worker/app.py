from fastapi import FastAPI, Depends
from fastapi.responses import JSONResponse
import asyncio

from .schemas import ExecuteRequest
from .auth import verify_token
from .runtime_adapter import RuntimeAdapter
from .config import MAX_CONCURRENCY, EXECUTION_TIMEOUT

from kernell_sdk.runtime.firecracker_runtime import FirecrackerRuntime

app = FastAPI()

# Note: FirecrackerRuntime requires proper parameters in production.
# Ensure paths like /vmlinux and /rootfs.ext4 exist.
try:
    runtime = FirecrackerRuntime("/opt/kernell/vmlinux", "/opt/kernell/rootfs.ext4")
    adapter = RuntimeAdapter(runtime)
except Exception as e:
    import logging
    logging.error(f"Failed to init FirecrackerRuntime: {e}")
    runtime = None
    adapter = None

# Concurrency limit to protect the node
semaphore = asyncio.Semaphore(MAX_CONCURRENCY)

current_inflight = 0
inflight_lock = asyncio.Lock()

async def inc_inflight():
    global current_inflight
    async with inflight_lock:
        current_inflight += 1

async def dec_inflight():
    global current_inflight
    async with inflight_lock:
        current_inflight -= 1

@app.get("/health")
async def health(_=Depends(verify_token)):
    global current_inflight
    return JSONResponse(content={
        "status": "ok",
        "inflight": current_inflight,
        "max_concurrency": MAX_CONCURRENCY
    })

@app.post("/execute")
async def execute(
    req: ExecuteRequest,
    _=Depends(verify_token)
):
    if not adapter:
        return JSONResponse(
            status_code=503,
            content={
                "stdout": "",
                "stderr": "Runtime not initialized",
                "exit_code": -1
            }
        )

    async with semaphore:
        await inc_inflight()
        try:
            result = await asyncio.wait_for(
                adapter.execute(req.code),
                timeout=EXECUTION_TIMEOUT
            )
            return JSONResponse(content=result)

        except asyncio.TimeoutError:
            return JSONResponse(
                status_code=408,
                content={
                    "stdout": "",
                    "stderr": "Execution timeout",
                    "exit_code": -1
                }
            )

        except Exception as e:
            return JSONResponse(
                status_code=500,
                content={
                    "stdout": "",
                    "stderr": str(e),
                    "exit_code": -1
                }
            )
        finally:
            await dec_inflight()
