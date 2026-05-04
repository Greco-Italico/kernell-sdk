import asyncio
import time
from kernell_sdk.runtime.firecracker_runtime import FirecrackerRuntime, ExecutionRequest

class RuntimeAdapter:
    def __init__(self, firecracker_runtime: FirecrackerRuntime):
        self.runtime = firecracker_runtime

    async def execute(self, code: str):
        start = time.time()

        # FirecrackerRuntime is sync, so we wrap it
        loop = asyncio.get_running_loop()
        request = ExecutionRequest(code=code)
        
        result = await loop.run_in_executor(None, self.runtime.execute, request)

        elapsed = (time.time() - start) * 1000

        return {
            "stdout": result.stdout,
            "stderr": result.stderr,
            "exit_code": result.exit_code,
            "execution_time_ms": elapsed
        }
