from __future__ import annotations
import subprocess
import sys
from kernell_sdk.runtime.version_manager import VersionManager

def main() -> int:
    manager = VersionManager()
    curr = manager.current_version()
    latest = manager.latest_version()

    if not manager.has_update():
        print(f"\n✅ You are on the latest version ({curr})\n")
        return 0

    print("\n🔔 Update available!")
    print(f"Current: {curr}")
    print(f"Latest:  {latest}")
    print("\n✨ Improvements:")
    for change in manager.get_changelog():
        print(f"- {change}")

    ans = input("\nRun update? (y/N): ").strip().lower()
    if ans == 'y':
        print("\n🚀 Upgrading kernell-os-sdk...")
        subprocess.run([
            sys.executable, "-m", "pip", "install", "--upgrade", "kernell-os-sdk"
        ])
        print("\n✅ Upgrade complete! Please restart your services.\n")
    else:
        print("\n❌ Upgrade cancelled.\n")
    
    return 0
