from __future__ import annotations
import subprocess
import time
from pathlib import Path
import sys

CONFIG_DIR = Path.home() / ".kernell"

def _run(cmd: list[str]) -> bool:
    try:
        subprocess.run(cmd, check=True)
        return True
    except Exception:
        return False

def _wait_for_service(url: str, timeout: int = 30):
    import urllib.request
    start = time.time()
    while time.time() - start < timeout:
        try:
            urllib.request.urlopen(url, timeout=2)
            return True
        except Exception:
            time.sleep(1)
    return False

def full_install():
    print("\n🐳 Starting full environment (Redis + Qdrant)...\n")

    ok = _run([
        "docker", "compose",
        "-f", "deploy/docker-compose.sdk-client.yml",
        "up", "-d", "redis", "qdrant"
    ])

    if not ok:
        print("❌ Failed to start Docker services")
        return 1

    print("⏳ Waiting for services...")

    qdrant_ok = _wait_for_service("http://localhost:6333/healthz")

    if not qdrant_ok:
        print("❌ Qdrant not healthy")
        return 2

    print("✅ Services ready\n")
    return 0

def main() -> int:
    full = "--full" in sys.argv

    print("\n🧠 Initializing Kernell OS...\n")

    CONFIG_DIR.mkdir(parents=True, exist_ok=True)

    if full:
        return full_install()

    print("✅ Basic init complete")
    return 0
