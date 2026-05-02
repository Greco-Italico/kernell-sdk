from __future__ import annotations
from fastapi import FastAPI, Request, Response
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from pathlib import Path
import json
import httpx
import subprocess
import sys
import os
import uuid
import threading
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

app = FastAPI()

TELEMETRY_FILE = Path("/tmp/kernell_telemetry/telemetry_buffer_latest.jsonl")
_static_dir = Path(__file__).parent / "static"

_TASK_TYPES = frozenset({"simple", "financial", "multi_agent", "autonomous_loop"})
_TASKS_LOCK = threading.Lock()
_TASKS_FILE = Path.home() / ".kernell" / "data" / "tasks.json"
_EXECUTIONS_LOCK = threading.Lock()
_EXECUTIONS_FILE = Path.home() / ".kernell" / "data" / "executions.json"
_MAX_EXECUTIONS = 2000
_CORE_HTTP_TIMEOUT_S = 30.0


def _core_api_base() -> str:
    return os.environ.get("KERNELL_CORE_URL", "http://127.0.0.1:8000").rstrip("/")


def _tasks_path() -> Path:
    return Path(os.environ.get("KERNELL_TASKS_PATH", str(_TASKS_FILE)))


def _load_tasks() -> List[Dict[str, Any]]:
    path = _tasks_path()
    if not path.exists():
        return []
    try:
        raw = path.read_text(encoding="utf-8")
        data = json.loads(raw) if raw.strip() else []
        return data if isinstance(data, list) else []
    except Exception:
        return []


def _save_tasks(tasks: List[Dict[str, Any]]) -> None:
    path = _tasks_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(tasks, indent=2), encoding="utf-8")


def _validate_task_type(task_type: str) -> str | None:
    if task_type not in _TASK_TYPES:
        return f"Invalid task_type. Allowed: {', '.join(sorted(_TASK_TYPES))}"
    return None


def _executions_path() -> Path:
    return Path(os.environ.get("KERNELL_EXECUTIONS_PATH", str(_EXECUTIONS_FILE)))


def _load_executions() -> List[Dict[str, Any]]:
    path = _executions_path()
    if not path.exists():
        return []
    try:
        raw = path.read_text(encoding="utf-8")
        data = json.loads(raw) if raw.strip() else []
        return data if isinstance(data, list) else []
    except Exception:
        return []


def _save_executions(rows: List[Dict[str, Any]]) -> None:
    path = _executions_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(rows, indent=2), encoding="utf-8")


def _append_execution(record: Dict[str, Any]) -> None:
    with _EXECUTIONS_LOCK:
        rows = _load_executions()
        rows.append(record)
        if len(rows) > _MAX_EXECUTIONS:
            rows = rows[-_MAX_EXECUTIONS:]
        _save_executions(rows)


def _short_detail(detail: Any, max_len: int = 280) -> str:
    if detail is None:
        return ""
    if isinstance(detail, str):
        s = detail
    else:
        try:
            s = json.dumps(detail, ensure_ascii=False)
        except Exception:
            s = str(detail)
    s = s.replace("\n", " ").strip()
    if len(s) > max_len:
        return s[: max_len - 1] + "…"
    return s


def _failure_reason(http_status: int, payload: Any) -> str:
    if http_status == 502:
        return "core_unreachable"
    if http_status == 401:
        return "unauthorized"
    if http_status == 402:
        return "payment_required"
    if http_status == 429:
        return "rate_limited"
    if http_status >= 500:
        return "server_error"
    if isinstance(payload, dict):
        st = payload.get("status")
        if st and st != "completed":
            return f"execution_{st}"
    return "request_failed"


def record_execution_outcome(
    *,
    http_status: int,
    payload: Any,
    source: str,
    task_id: Optional[str] = None,
    task_name: Optional[str] = None,
    task_type: Optional[str] = None,
    input_text: Optional[str] = None,
    core_url: Optional[str] = None,
    failure_reason: Optional[str] = None,
) -> None:
    """
    Append one history row for both successes and failures (dashboard-local log).
    """
    base: Dict[str, Any] = {
        "ok": False,
        "http_status": http_status,
        "task_id": task_id,
        "task_name": task_name,
        "task_type": task_type,
        "input": input_text or "",
        "source": source,
        "recorded_at": datetime.now(timezone.utc).isoformat(),
        "ts": time.time(),
    }
    if core_url:
        base["core_url"] = core_url

    if http_status == 200 and isinstance(payload, dict) and payload.get("status") == "completed":
        eid = payload.get("execution_id")
        if not eid:
            return
        try:
            est = float(payload.get("cost_estimated_kern", 0))
            act = float(payload.get("cost_actual_kern", 0))
            refund = float(payload.get("refund_kern", 0))
            remaining = float(payload.get("remaining_kern", 0))
        except (TypeError, ValueError):
            return
        base.update(
            {
                "ok": True,
                "execution_id": str(eid),
                "estimated_kern": est,
                "actual_kern": act,
                "refund_kern": refund,
                "remaining_kern": remaining,
            }
        )
        _append_execution(base)
        return

    detail: Any = None
    if isinstance(payload, dict):
        detail = payload.get("detail", payload)
    else:
        detail = payload
    base.update(
        {
            "ok": False,
            "reason": failure_reason or _failure_reason(http_status, payload),
            "error": _short_detail(detail),
        }
    )
    if isinstance(payload, dict) and payload.get("execution_id"):
        base["execution_id"] = str(payload.get("execution_id"))
    _append_execution(base)


@app.get("/health")
def health():
    return {"status": "ok"}

@app.get("/events")
def get_events(limit: int = 50):
    if not TELEMETRY_FILE.exists():
        return []
    lines = TELEMETRY_FILE.read_text().splitlines()[-limit:]
    return [json.loads(line) for line in lines if line.strip()]

@app.get("/benchmark/latest")
def latest_benchmark():
    runs = sorted(Path("benchmarks/runs").glob("*.jsonl"))
    if not runs:
        return {"status": "no_data"}
    rows = [json.loads(l) for l in open(runs[-1])]
    return {
        "tasks": len(rows),
        "avg_savings": sum(r.get("savings_pct", r.get("savings", 0)) for r in rows)/len(rows),
        "avg_quality_drop": sum(r["quality_drop"] for r in rows)/len(rows),
    }

from kernell_os_sdk.runtime.version_manager import VersionManager

@app.get("/version/status")
def version_status():
    manager = VersionManager()
    curr = manager.current_version()
    latest = manager.latest_version()
    return {
        "current": curr,
        "latest": latest,
        "has_update": manager.has_update(),
        "changelog": manager.get_changelog()
    }

@app.post("/version/upgrade")
def version_upgrade():
    subprocess.Popen([
        sys.executable, "-m", "pip", "install", "--upgrade", "kernell-os-sdk"
    ])
    return {"status": "updating"}


@app.get("/run", response_class=HTMLResponse)
def dashboard_run_v1():
    """
    Minimal Dashboard v1: same-origin UI for the economic execution loop.
    Proxies to Core API via POST /proxy/execute-v2 to avoid browser CORS.
    DevLayer v1: save/list/reuse tasks (JSON file under ~/.kernell/data).
    Execution history v1: append-only local log under ~/.kernell/data/executions.json.
    """
    core_hint = _core_api_base()
    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>Kernell — Run</title>
  <style>
    body {{ font-family: system-ui, sans-serif; max-width: 640px; margin: 2rem auto; padding: 0 1rem; }}
    h1 {{ font-size: 1.25rem; }}
    label {{ display: block; margin-top: 1rem; font-weight: 600; }}
    select, input[type=text], textarea {{ width: 100%; box-sizing: border-box; margin-top: 0.35rem; }}
    button {{ margin-top: 1rem; padding: 0.5rem 1rem; cursor: pointer; }}
    pre {{ background: #f4f4f5; padding: 1rem; border-radius: 6px; white-space: pre-wrap; word-break: break-word; }}
    .hint {{ color: #666; font-size: 0.85rem; margin-top: 0.25rem; }}
  </style>
</head>
<body>
  <h1>Kernell Dashboard — Run (v1)</h1>
  <p class="hint">Calls Core <code>POST /api/v1/sandbox/execute-v2</code> via same-origin proxy. Core default: <code>{core_hint}</code> (set <code>KERNELL_CORE_URL</code> to override).</p>

  <label for="apiKey">API key</label>
  <input id="apiKey" type="text" autocomplete="off" placeholder="sk_test_kernell_..." />

  <label for="taskType">Task type</label>
  <select id="taskType">
    <option value="simple">simple</option>
    <option value="financial">financial</option>
    <option value="multi_agent">multi_agent</option>
    <option value="autonomous_loop">autonomous_loop</option>
  </select>

  <label for="input">Task input</label>
  <p class="hint" style="margin-top:0.15rem;">Sent to Core as <code>input</code> on every run. Example (financial): <code>analyze NVDA momentum vs sector</code>. Simple tasks echo this text in the mock result.</p>
  <textarea id="input" rows="4" placeholder="e.g. analyze NVDA stock trend (financial) or any note for simple / multi_agent"></textarea>

  <button type="button" id="runBtn">Run</button>
  <button type="button" id="saveBtn">Save task</button>

  <pre id="output"></pre>

  <h2 style="margin-top:2rem;font-size:1.1rem;">Saved tasks (DevLayer v1)</h2>
  <p class="hint">Stored locally under <code>~/.kernell/data/tasks.json</code> (override with <code>KERNELL_TASKS_PATH</code>).</p>
  <div id="tasks"></div>

  <h2 style="margin-top:2rem;font-size:1.1rem;">Execution history</h2>
  <p class="hint">Recent runs (newest first in this list). File: <code>~/.kernell/data/executions.json</code> (<code>KERNELL_EXECUTIONS_PATH</code>).</p>
  <div id="executions"></div>

  <script>
    const output = document.getElementById("output");
    const RUN_TIMEOUT_MS = 30000;

    function formatRecordedAt(iso) {{
      if (!iso) return "";
      try {{
        return new Date(iso).toLocaleString(undefined, {{ dateStyle: "short", timeStyle: "medium" }});
      }} catch (e) {{
        return iso;
      }}
    }}

    function formatTaskResult(data) {{
      const iu = data.input_used;
      let block = "";
      if (iu !== undefined && String(iu).length > 0) {{
        block += "Input used:\\n" + String(iu) + "\\n\\n";
      }}
      const o = data.output;
      if (o && typeof o === "object") {{
        block += "Result:\\n";
        if (o.analysis) {{
          block += o.analysis + "\\n";
          if (o.confidence != null)
            block += "Confidence: " + o.confidence + (o.confidence_note ? " — " + o.confidence_note : "") + "\\n";
          if (Array.isArray(o.sources) && o.sources.length) {{
            block += "Sources:\\n";
            o.sources.forEach((s) => {{
              block += "  • " + (s.label || "") + " " + (s.ref || "") + "\\n";
            }});
          }}
        }} else if (o.summary) {{
          block += o.summary + "\\n";
        }} else {{
          block += JSON.stringify(o, null, 2) + "\\n";
        }}
      }}
      return block;
    }}

    function formatOutput(data) {{
      const est = Number(data.cost_estimated_kern || 0);
      const act = Number(data.cost_actual_kern || 0);
      const refund = Number(data.refund_kern || 0);
      const remaining = Number(data.remaining_kern || 0);
      const saved = Math.max(est - act, 0);
      const econ =
        "Execution ID: " + data.execution_id + "\\n" +
        "[Estimate]   " + est.toFixed(6) + " KERN\\n" +
        "[Actual]     " + act.toFixed(6) + " KERN\\n" +
        "[Refund]     " + refund.toFixed(6) + " KERN\\n" +
        "[Remaining]  " + remaining.toFixed(6) + " KERN\\n" +
        "Saved vs estimate: " + saved.toFixed(6) + " KERN\\n";
      const body = formatTaskResult(data);
      const tail = "────────────────────────────\\n✔ Economic loop settled\\n";
      return body ? econ + "────────────────────────────\\n" + body + tail : econ + tail;
    }}

    async function loadTasks() {{
      const container = document.getElementById("tasks");
      container.textContent = "";
      try {{
        const res = await fetch("/tasks");
        const tasks = await res.json();
        if (!Array.isArray(tasks) || tasks.length === 0) {{
          const p = document.createElement("p");
          p.className = "hint";
          p.textContent = "No saved tasks yet.";
          container.appendChild(p);
          return;
        }}
        tasks.forEach((t) => {{
          const row = document.createElement("div");
          row.style.marginTop = "0.5rem";
          const title = document.createElement("strong");
          title.textContent = t.name + " ";
          const meta = document.createElement("span");
          meta.className = "hint";
          meta.textContent = "(" + t.task_type + ")";
          const runBtn = document.createElement("button");
          runBtn.type = "button";
          runBtn.textContent = "Run";
          runBtn.style.marginLeft = "0.5rem";
          runBtn.addEventListener("click", () => runSaved(t.id));
          row.appendChild(title);
          row.appendChild(meta);
          row.appendChild(runBtn);
          container.appendChild(row);
        }});
      }} catch (e) {{
        container.textContent = "❌ Failed to load tasks: " + e.message;
      }}
    }}

    async function loadExecutions() {{
      const container = document.getElementById("executions");
      container.textContent = "";
      try {{
        const res = await fetch("/executions?limit=15");
        const rows = await res.json();
        if (!Array.isArray(rows) || rows.length === 0) {{
          const p = document.createElement("p");
          p.className = "hint";
          p.textContent = "No executions recorded yet.";
          container.appendChild(p);
          return;
        }}
        rows.forEach((r) => {{
          const line = document.createElement("div");
          line.style.marginTop = "0.35rem";
          const when = formatRecordedAt(r.recorded_at);
          const prefix = when ? when + " · " : "";
          const name = r.task_name ? r.task_name + " → " : "";
          const tt = r.task_type ? "(" + r.task_type + ") " : "";
          const ok =
            r.ok === true ||
            (r.ok === undefined && r.execution_id && r.http_status === undefined);
          if (ok) {{
            const act = Number(r.actual_kern || 0).toFixed(6);
            line.textContent = prefix + "✔ " + name + tt + act + " KERN";
          }} else {{
            const rs = r.reason || "failed";
            const hs = r.http_status !== undefined ? " HTTP " + r.http_status : "";
            const err = r.error ? " — " + r.error : "";
            line.textContent = prefix + "✖ " + name + tt + rs + hs + err;
          }}
          container.appendChild(line);
        }});
      }} catch (e) {{
        container.textContent = "❌ Failed to load history: " + e.message;
      }}
    }}

    async function saveTask() {{
      const name = prompt("Task name?");
      if (!name || !name.trim()) return;
      const taskType = document.getElementById("taskType").value;
      const input = document.getElementById("input").value;
      const err = validateTaskType(taskType);
      if (err) {{ alert(err); return; }}
      try {{
        const res = await fetch("/tasks", {{
          method: "POST",
          headers: {{ "Content-Type": "application/json" }},
          body: JSON.stringify({{ name: name.trim(), task_type: taskType, input: input }}),
        }});
        if (!res.ok) {{
          const d = await res.json().catch(() => ({{}}));
          throw new Error(d.detail || res.statusText);
        }}
        await loadTasks();
      }} catch (e) {{
        alert("Save failed: " + e.message);
      }}
    }}

    function validateTaskType(taskType) {{
      const allowed = ["simple", "financial", "multi_agent", "autonomous_loop"];
      if (!allowed.includes(taskType)) {{
        return "Invalid task type.";
      }}
      return null;
    }}

    async function runSaved(taskId) {{
      const apiKey = document.getElementById("apiKey").value.trim();
      if (!apiKey) {{
        output.textContent = "❌ Enter API key (sandbox key).";
        return;
      }}
      output.textContent = "Running saved task...\\n(" + (RUN_TIMEOUT_MS/1000) + "s timeout)\\n";
      const controller = new AbortController();
      const to = setTimeout(() => controller.abort(), RUN_TIMEOUT_MS);
      try {{
        const res = await fetch("/tasks/" + encodeURIComponent(taskId) + "/run", {{
          method: "POST",
          headers: {{ "X-API-Key": apiKey }},
          signal: controller.signal,
        }});
        clearTimeout(to);
        const data = await res.json().catch(() => ({{}}));
        if (!res.ok) {{
          const detail = data.detail !== undefined ? data.detail : JSON.stringify(data);
          throw new Error(typeof detail === "string" ? detail : JSON.stringify(detail));
        }}
        output.textContent = formatOutput(data);
        await loadExecutions();
      }} catch (err) {{
        clearTimeout(to);
        if (err.name === "AbortError") {{
          output.textContent = "❌ Execution timed out after " + (RUN_TIMEOUT_MS/1000) + "s. Check Core at KERNELL_CORE_URL.";
        }} else {{
          output.textContent = "❌ Execution failed: " + err.message;
        }}
      }}
    }}

    document.getElementById("saveBtn").addEventListener("click", saveTask);

    document.getElementById("runBtn").addEventListener("click", async () => {{
      const apiKey = document.getElementById("apiKey").value.trim();
      const taskType = document.getElementById("taskType").value;
      const input = document.getElementById("input").value;
      const err = validateTaskType(taskType);
      if (err) {{ output.textContent = "❌ " + err; return; }}
      output.textContent = "Running execution...\\n(" + (RUN_TIMEOUT_MS/1000) + "s timeout)\\n";
      if (!apiKey) {{
        output.textContent = "❌ Enter API key (sandbox key).";
        return;
      }}
      const controller = new AbortController();
      const to = setTimeout(() => controller.abort(), RUN_TIMEOUT_MS);
      try {{
        const res = await fetch("/proxy/execute-v2", {{
          method: "POST",
          headers: {{
            "Content-Type": "application/json",
            "X-API-Key": apiKey,
          }},
          body: JSON.stringify({{ task_type: taskType, input: input }}),
          signal: controller.signal,
        }});
        clearTimeout(to);
        const data = await res.json().catch(() => ({{}}));
        if (!res.ok) {{
          const detail = data.detail !== undefined ? data.detail : JSON.stringify(data);
          const msg = typeof detail === "string" ? detail : JSON.stringify(detail);
          if (res.status === 502 && data.core_url) {{
            throw new Error(msg + " — check KERNELL_CORE_URL (" + data.core_url + ")");
          }}
          throw new Error(msg);
        }}
        output.textContent = formatOutput(data);
        await loadExecutions();
      }} catch (err) {{
        clearTimeout(to);
        if (err.name === "AbortError") {{
          output.textContent = "❌ Execution timed out after " + (RUN_TIMEOUT_MS/1000) + "s. Check Core at KERNELL_CORE_URL.";
        }} else {{
          output.textContent = "❌ Execution failed: " + err.message;
        }}
      }}
    }});

    loadTasks();
    loadExecutions();
  </script>
</body>
</html>"""
    return HTMLResponse(content=html)


@app.post("/proxy/execute-v2")
async def proxy_execute_v2(request: Request):
    """
    Same-origin proxy: forwards to Core execute-v2 with caller's X-API-Key.
    """
    api_key = request.headers.get("X-API-Key", "")
    if not api_key:
        return JSONResponse(status_code=401, content={"detail": "Missing X-API-Key"})
    try:
        body = await request.json()
    except Exception:
        body = {}
    url = f"{_core_api_base()}/api/v1/sandbox/execute-v2"
    tt = body.get("task_type") if isinstance(body, dict) else None
    inp = body.get("input") if isinstance(body, dict) else None
    async with httpx.AsyncClient(timeout=_CORE_HTTP_TIMEOUT_S) as client:
        try:
            r = await client.post(
                url,
                json=body,
                headers={
                    "Content-Type": "application/json",
                    "X-API-Key": api_key,
                },
            )
        except Exception as e:
            fr = "timeout" if isinstance(e, httpx.TimeoutException) else "core_unreachable"
            record_execution_outcome(
                http_status=502,
                payload={"detail": str(e)},
                source="dashboard_proxy",
                task_type=str(tt) if tt is not None else None,
                input_text=str(inp) if inp is not None else "",
                core_url=url,
                failure_reason=fr,
            )
            return JSONResponse(
                status_code=502,
                content={"detail": f"Core unreachable: {e}", "core_url": url},
            )
    try:
        payload = r.json()
    except Exception:
        payload = {"detail": r.text}
    record_execution_outcome(
        http_status=r.status_code,
        payload=payload,
        source="dashboard_proxy",
        task_type=str(tt) if tt is not None else None,
        input_text=str(inp) if inp is not None else "",
        core_url=url,
    )
    return JSONResponse(status_code=r.status_code, content=payload)


@app.post("/tasks")
async def devlayer_create_task(request: Request):
    try:
        payload = await request.json()
    except Exception:
        return JSONResponse(status_code=400, content={"detail": "Invalid JSON body"})
    if not isinstance(payload, dict):
        return JSONResponse(status_code=400, content={"detail": "Body must be a JSON object"})
    name = str(payload.get("name", "unnamed")).strip() or "unnamed"
    task_type = str(payload.get("task_type", "simple"))
    err = _validate_task_type(task_type)
    if err:
        return JSONResponse(status_code=400, content={"detail": err})
    inp = payload.get("input", "")
    if inp is not None and not isinstance(inp, str):
        return JSONResponse(status_code=400, content={"detail": "input must be a string"})
    task = {
        "id": str(uuid.uuid4()),
        "name": name,
        "task_type": task_type,
        "input": inp or "",
    }
    with _TASKS_LOCK:
        tasks = _load_tasks()
        tasks.append(task)
        _save_tasks(tasks)
    return task


@app.get("/tasks")
def devlayer_list_tasks():
    with _TASKS_LOCK:
        return _load_tasks()


@app.post("/tasks/{task_id}/run")
async def devlayer_run_task(task_id: str, request: Request):
    api_key = request.headers.get("X-API-Key", "")
    if not api_key:
        return JSONResponse(status_code=401, content={"detail": "Missing X-API-Key"})
    with _TASKS_LOCK:
        tasks = _load_tasks()
    task = next((t for t in tasks if t.get("id") == task_id), None)
    if not task:
        return JSONResponse(status_code=404, content={"detail": "Task not found"})
    err = _validate_task_type(str(task.get("task_type", "")))
    if err:
        return JSONResponse(status_code=400, content={"detail": err})
    url = f"{_core_api_base()}/api/v1/sandbox/execute-v2"
    body = {"task_type": task["task_type"], "input": task.get("input", "")}
    async with httpx.AsyncClient(timeout=_CORE_HTTP_TIMEOUT_S) as client:
        try:
            r = await client.post(
                url,
                json=body,
                headers={
                    "Content-Type": "application/json",
                    "X-API-Key": api_key,
                },
            )
        except Exception as e:
            fr = "timeout" if isinstance(e, httpx.TimeoutException) else "core_unreachable"
            record_execution_outcome(
                http_status=502,
                payload={"detail": str(e)},
                source="saved_task",
                task_id=str(task.get("id")) if task.get("id") else None,
                task_name=str(task.get("name")) if task.get("name") is not None else None,
                task_type=str(task.get("task_type")) if task.get("task_type") is not None else None,
                input_text=str(task.get("input", "")),
                core_url=url,
                failure_reason=fr,
            )
            return JSONResponse(
                status_code=502,
                content={"detail": f"Core unreachable: {e}", "core_url": url},
            )
    try:
        payload = r.json()
    except Exception:
        payload = {"detail": r.text}
    record_execution_outcome(
        http_status=r.status_code,
        payload=payload,
        source="saved_task",
        task_id=str(task.get("id")) if task.get("id") else None,
        task_name=str(task.get("name")) if task.get("name") is not None else None,
        task_type=str(task.get("task_type")) if task.get("task_type") is not None else None,
        input_text=str(task.get("input", "")),
        core_url=url,
    )
    return JSONResponse(status_code=r.status_code, content=payload)


@app.get("/executions")
def list_executions(limit: int = 50):
    if limit < 1:
        limit = 1
    if limit > 500:
        limit = 500
    with _EXECUTIONS_LOCK:
        rows = _load_executions()
    tail = rows[-limit:]
    return list(reversed(tail))


@app.api_route("/api/{path:path}", methods=["GET", "POST", "PUT", "DELETE", "PATCH"])
async def proxy_api(request: Request, path: str):
    url = f"http://127.0.0.1:8502/api/{path}"
    async with httpx.AsyncClient() as client:
        body = await request.body()
        proxy_req = client.build_request(
            request.method,
            url,
            headers=request.headers.raw,
            content=body,
            params=request.query_params
        )
        try:
            proxy_res = await client.send(proxy_req)
            return Response(
                content=proxy_res.content,
                status_code=proxy_res.status_code,
                headers=dict(proxy_res.headers)
            )
        except Exception as e:
            return Response(content=json.dumps({"error": str(e), "message": "Kernell OS API Server not running on 8502"}), status_code=502)

app.mount("/assets", StaticFiles(directory=str(_static_dir / "assets")), name="assets")

@app.get("/{path:path}")
async def serve_spa(path: str):
    file_path = _static_dir / path
    if file_path.is_file() and file_path.exists():
        return FileResponse(file_path)
    return FileResponse(_static_dir / "index.html")
