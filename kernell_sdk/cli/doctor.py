from __future__ import annotations
import subprocess
import urllib.request
import sys

def check_http(url: str) -> bool:
    try:
        urllib.request.urlopen(url, timeout=2)
        return True
    except Exception:
        return False

def check_router():
    try:
        from kernell_sdk.router import IntelligentRouter
        return True
    except Exception:
        return False

def main() -> int:
    deep = "--deep" in sys.argv

    print("\n🩺 Kernell Doctor\n")

    checks = {
        "CLI": subprocess.run(["kernell", "--help"]).returncode == 0,
    }

    if deep:
        checks.update({
            "Router import": check_router(),
            "Qdrant": check_http("http://localhost:6333/healthz"),
        })

    all_ok = True

    for name, status in checks.items():
        icon = "✅" if status else "❌"
        print(f"{icon} {name}")
        if not status:
            all_ok = False

    print()
    return 0 if all_ok else 1
