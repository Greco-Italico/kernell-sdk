"""Client-side smoke checks for SDK container install."""

from __future__ import annotations

import importlib.metadata as metadata
import json
import os
from pathlib import Path
import subprocess
import sys
from urllib import error as urlerror
from urllib import request as urlrequest


def _print(title: str, value: str) -> None:
    print(f"[smoke] {title}: {value}")


class _MockBackend:
    """Deterministic backend for router smoke tests."""

    def __init__(self, text: str = "ok") -> None:
        self._text = text

    def generate(self, prompt: str, system: str = "") -> str:
        return f"{self._text}:{prompt[:48]}"


def _bool_env(name: str, default: str = "1") -> bool:
    return os.environ.get(name, default).lower() not in {"0", "false", "no"}


def _probe_qdrant_health(url: str, timeout_s: float = 1.5) -> bool:
    health_url = url.rstrip("/") + "/healthz"
    try:
        with urlrequest.urlopen(health_url, timeout=timeout_s) as response:
            return response.status == 200
    except (urlerror.URLError, TimeoutError, ValueError):
        return False


def _probe_redis_tcp(host: str, port: int, timeout_s: float = 1.0) -> bool:
    import socket

    try:
        with socket.create_connection((host, port), timeout=timeout_s):
            return True
    except OSError:
        return False


def main() -> int:
    package_name = os.environ.get("SDK_DISTRIBUTION_NAME", "kernell-os-sdk")
    chaos_mode = _bool_env("SDK_CHAOS_MODE", "0")
    report = {
        "install": "fail",
        "import": "fail",
        "cli": "fail",
        "router": "fail",
        "telemetry": "fail",
        "policy": "fail",
        "failure_mode": "skip",
        "degraded": False,
    }

    try:
        version = metadata.version(package_name)
    except metadata.PackageNotFoundError:
        _print("distribution", f"{package_name} not installed")
        print(json.dumps(report, sort_keys=True))
        return 2

    _print("distribution", f"{package_name}=={version}")
    report["install"] = "ok"

    try:
        import kernell_sdk as sdk  # pylint: disable=import-outside-toplevel
    except Exception as exc:  # noqa: BLE001
        _print("import", f"FAILED ({exc})")
        print(json.dumps(report, sort_keys=True))
        return 3

    _print("import", f"OK ({getattr(sdk, '__file__', 'n/a')})")
    report["import"] = "ok"

    cli = subprocess.run(
        ["kernell", "--help"],
        check=False,
        capture_output=True,
        text=True,
    )
    _print("cli_exit_code", str(cli.returncode))
    if cli.returncode != 0:
        _print("cli_stderr", cli.stderr.strip() or "<empty>")
        print(json.dumps(report, sort_keys=True))
        return 4
    _print("cli_stdout_head", (cli.stdout.strip().splitlines()[:1] or ["<empty>"])[0])
    report["cli"] = "ok"

    try:
        from kernell_sdk.router import (  # pylint: disable=import-outside-toplevel
            IntelligentRouter,
            PolicyLiteClient,
            TelemetryCollector,
            TelemetryConfig,
        )

        local = _MockBackend("local")
        policy_model = _MockBackend(
            '{"route":"local","confidence":0.92,"needs_decomposition":false,'
            '"risk":"low","expected_cost_usd":0.0001,"expected_latency_s":0.2,'
            '"max_budget_usd":0.005}'
        )
        telemetry = TelemetryCollector(
            config=TelemetryConfig(
                enabled=True,
                consent_given=True,
                buffer_dir=os.environ.get("KERNELL_TELEMETRY_DIR", "/tmp/kernell_telemetry"),
                batch_size=1000,
                flush_interval_seconds=3600.0,
            )
        )
        policy_lite = PolicyLiteClient(model=policy_model)
        router = IntelligentRouter(
            classifier=local,
            local_models={"local_small": local},
            telemetry=telemetry,
            policy_lite=policy_lite,
        )
        results = router.execute("smoke test task")
        if results and any(r.success for r in results):
            report["router"] = "ok"
            _print("router_execution", f"OK ({len(results)} subtask(s))")
        else:
            _print("router_execution", "FAILED (no successful results)")
            print(json.dumps(report, sort_keys=True))
            return 5

        if telemetry.get_stats().get("total_collected", 0) > 0:
            report["telemetry"] = "ok"
            _print("telemetry", "OK (event(s) collected)")
        else:
            _print("telemetry", "FAILED (no events collected)")
            print(json.dumps(report, sort_keys=True))
            return 7

        telemetry_events = telemetry.inspect_buffer()
        if not telemetry_events:
            _print("policy_signal", "FAILED (telemetry buffer empty)")
            print(json.dumps(report, sort_keys=True))
            return 8
        required_policy_fields = {"policy_route_predicted", "final_route_used"}
        has_policy_signal = any(
            bool(getattr(r, "model_used", "")) and getattr(r, "success", False)
            for r in results
        ) and any(
            required_policy_fields.issubset(evt.keys())
            and bool(evt.get("policy_route_predicted"))
            and bool(evt.get("final_route_used"))
            for evt in telemetry_events
        )
        if has_policy_signal:
            report["policy"] = "ok"
            _print("policy_signal", "OK (policy metadata present in telemetry)")
        else:
            _print("policy_signal", "FAILED (missing policy metadata)")
            print(json.dumps(report, sort_keys=True))
            return 8

        telemetry_path_raw = os.environ.get("KERNELL_TELEMETRY_PATH", "").strip()
        if telemetry_path_raw:
            telemetry_path = Path(telemetry_path_raw)
            if telemetry_path.exists():
                _print("telemetry_path", f"OK ({telemetry_path})")
            else:
                _print("telemetry_path", f"MISSING ({telemetry_path})")
    except Exception as exc:  # noqa: BLE001
        _print("router_execution", f"FAILED ({exc})")
        print(json.dumps(report, sort_keys=True))
        return 5

    if _bool_env("SDK_SMOKE_FAILURE_MODE", "1"):
        try:
            from kernell_sdk.router import IntelligentRouter  # pylint: disable=import-outside-toplevel

            router = IntelligentRouter(classifier=_MockBackend("f"), local_models={})
            failure_results = router.execute("failure mode probe")
            if failure_results and all(not r.success for r in failure_results):
                report["failure_mode"] = "ok"
                _print("failure_mode", "OK (graceful no-local-model path)")
            else:
                _print("failure_mode", "FAILED (unexpected success)")
                print(json.dumps(report, sort_keys=True))
                return 6
        except Exception as exc:  # noqa: BLE001
            _print("failure_mode", f"FAILED ({exc})")
            print(json.dumps(report, sort_keys=True))
            return 6

    if chaos_mode:
        redis_host = os.environ.get("REDIS_HOST", "redis")
        redis_port = int(os.environ.get("REDIS_PORT", "6379"))
        qdrant_url = os.environ.get("QDRANT_URL", "http://qdrant:6333")

        redis_ok = _probe_redis_tcp(redis_host, redis_port)
        qdrant_ok = _probe_qdrant_health(qdrant_url)
        report["degraded"] = not (redis_ok and qdrant_ok)
        _print("chaos_mode", "ON")
        _print("chaos_probe_redis", "OK" if redis_ok else "DEGRADED")
        _print("chaos_probe_qdrant", "OK" if qdrant_ok else "DEGRADED")

    _print("status", "PASS")
    print(json.dumps(report, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
