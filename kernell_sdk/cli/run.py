from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request
from typing import Tuple


def _parse_args(argv: list[str]) -> Tuple[str, str, str, str, bool]:
    task_type = "simple"
    task_input = ""
    api_key = os.getenv("KERNELL_API_KEY", "")
    base_url = os.getenv("KERNELL_BASE_URL", "http://localhost:8000")
    legacy = False

    i = 0
    while i < len(argv):
        token = argv[i]
        if token == "--task-type" and i + 1 < len(argv):
            task_type = argv[i + 1]
            i += 2
            continue
        if token == "--input" and i + 1 < len(argv):
            task_input = argv[i + 1]
            i += 2
            continue
        if token == "--api-key" and i + 1 < len(argv):
            api_key = argv[i + 1]
            i += 2
            continue
        if token == "--base-url" and i + 1 < len(argv):
            base_url = argv[i + 1]
            i += 2
            continue
        if token == "--legacy":
            legacy = True
            i += 1
            continue
        if token in ("-h", "--help"):
            _print_help()
            raise SystemExit(0)
        i += 1

    return task_type, task_input, api_key, base_url.rstrip("/"), legacy


def _print_help() -> None:
    print(
        "Usage: kernell run [--task-type <simple|multi_agent|financial|autonomous_loop>] "
        "[--input <text>] [--api-key <key>] [--base-url <url>] [--legacy]"
    )
    print("Defaults: --task-type simple, --base-url http://localhost:8000")
    print("Env vars: KERNELL_API_KEY, KERNELL_BASE_URL")


def _validate_invocation() -> None:
    # Guard against accidental direct invocation with wrong argv shape.
    if len(sys.argv) >= 2 and sys.argv[1] not in ("run", "-h", "--help"):
        raise SystemExit("This module is intended for 'kernell run'.")


def _post_json(url: str, payload: dict, api_key: str) -> Tuple[int, dict]:
    req = urllib.request.Request(
        url=url,
        method="POST",
        headers={
            "Content-Type": "application/json",
            "X-API-Key": api_key,
        },
        data=json.dumps(payload).encode("utf-8"),
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as response:
            body = response.read().decode("utf-8")
            return response.status, json.loads(body)
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8") if exc.fp else "{}"
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            data = {"detail": raw or str(exc)}
        return exc.code, data


def main() -> int:
    _validate_invocation()
    task_type, task_input, api_key, base_url, legacy = _parse_args(sys.argv[2:])
    if not api_key:
        print("❌ Missing API key. Use --api-key or set KERNELL_API_KEY.")
        return 2

    endpoint = "/api/v1/sandbox/execute" if legacy else "/api/v1/sandbox/execute-v2"
    status, data = _post_json(
        f"{base_url}{endpoint}",
        {"task_type": task_type, "input": task_input},
        api_key,
    )

    if status >= 400:
        detail = data.get("detail", data)
        print(f"❌ Execution failed ({status}): {detail}")
        return 1

    if legacy:
        print("✅ Execution (legacy)")
        print(f"Cost: {data.get('cost_kern', 0)} KERN")
        print(f"Remaining: {data.get('remaining_kern', 0)} KERN")
        return 0

    print("✅ Execution (v2)")
    print(f"Execution ID: {data.get('execution_id')}")
    if data.get("input_used") is not None and str(data.get("input_used", "")).strip():
        print(f"Input used: {data.get('input_used')}")
    out = data.get("output")
    if isinstance(out, dict) and out:
        if out.get("analysis"):
            print("── Result (financial) ──")
            print(out.get("analysis"))
            print(f"Confidence: {out.get('confidence')} — {out.get('confidence_note', '')}")
        elif out.get("summary"):
            print("── Result ──")
            print(out.get("summary"))
    est = float(data.get("cost_estimated_kern", 0))
    act = float(data.get("cost_actual_kern", 0))
    refund = float(data.get("refund_kern", 0))
    remaining = float(data.get("remaining_kern", 0))
    saved = max(est - act, 0.0)
    print(f"[Estimate] {est:.6f} KERN")
    print(f"[Actual]   {act:.6f} KERN")
    print(f"[Refund]   {refund:.6f} KERN")
    print(f"[Remaining] {remaining:.6f} KERN")
    print(f"Saved vs estimate: {saved:.6f} KERN")
    print("────────────────────────────")
    print("✔ Economic loop settled")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
