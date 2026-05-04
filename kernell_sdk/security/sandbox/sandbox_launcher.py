import subprocess
import json
import os
import pathlib

def run_in_sandbox(plan, token):
    payload = json.dumps({
        "plan": plan.model_dump(),
        "token": token.model_dump()
    })
    
    worker_script = str(pathlib.Path(__file__).parent / "sandbox_worker.py")
    
    cmd = [
        "unshare",
        "--user",
        "--mount",
        "--pid",
        "--ipc",
        "--net",
        "--fork",
        "--map-root-user",
        "python3",
        worker_script
    ]
    
    proc = subprocess.Popen(
        cmd,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        close_fds=True
    )
    
    out, err = proc.communicate(payload, timeout=5)
    
    if proc.returncode != 0:
        raise RuntimeError(f"Sandbox failed: {err}")
        
    return json.loads(out)
