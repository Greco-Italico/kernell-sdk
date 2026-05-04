import sys

def notify_update_silently():
    try:
        from kernell_sdk.runtime.version_manager import VersionManager
        manager = VersionManager()
        if manager.has_update():
            print(f"\n🔔 Kernell update available ({manager.latest_version()})")
            print("👉 Run: kernell upgrade\n")
    except Exception:
        pass

def main():
    if len(sys.argv) < 2:
        print("Usage: kernell [init|doctor|demo|cloud|dashboard|upgrade|run]")
        notify_update_silently()
        return

    cmd = sys.argv[1]

    if cmd == "init":
        from kernell_sdk.cli.init import main as run
    elif cmd == "doctor":
        from kernell_sdk.cli.doctor import main as run
    elif cmd == "demo":
        from kernell_sdk.cli.demo import main as run
    elif cmd == "cloud":
        from kernell_sdk.cli.cloud import main as run
    elif cmd == "dashboard":
        from kernell_sdk.cli.dashboard import main as run
    elif cmd == "upgrade":
        from kernell_sdk.cli.upgrade import main as run
    elif cmd == "run":
        from kernell_sdk.cli.run import main as run
    elif cmd == "--help":
        print("Usage: kernell [init|doctor|demo|cloud|dashboard|upgrade|run]")
        notify_update_silently()
        return 0
    else:
        print(f"Unknown command: {cmd}")
        return 1

    code = run()
    if cmd != "upgrade":
        notify_update_silently()
    sys.exit(code)

if __name__ == "__main__":
    main()
