"""
Kernell OS — Kernel Persistente Protegido (KPP) + Browser Runtime
=================================================================
Runtime Server interno del sandbox agéntico.
Arquitectura: Supervisor → Kernel Worker (proceso persistente) → Thread de ejecución.

Capacidades:
  - Python stateful (variables persisten entre llamadas)
  - Browser automation (Playwright, Chromium headless)
  - Timeout real (thread + nuclear kill)
  - __import__ controlado (defensa en profundidad)
  - Serialización inteligente (DataFrames, gráficas, primitivos)
"""

import time
import traceback
import multiprocessing
from multiprocessing.connection import Connection
from threading import Thread
import builtins
import io
import contextlib
import base64
import asyncio
from fastapi import FastAPI
from pydantic import BaseModel

# =========================
# 🔐 CONFIG
# =========================
EXEC_TIMEOUT = 6

FORBIDDEN_MODULES = {
    "os", "sys", "subprocess", "socket", "shutil"
}

FORBIDDEN_BUILTINS = {
    "exec", "eval", "compile", "open", "__import__"
}

_original_import = builtins.__import__


def safe_import(name, globals=None, locals=None, fromlist=(), level=0):
    if name.split(".")[0] in FORBIDDEN_MODULES:
        raise ImportError(f"Import of '{name}' is forbidden")
    return _original_import(name, globals, locals, fromlist, level)


# =========================
# 🧠 BROWSER MANAGER
# =========================
class BrowserManager:
    def __init__(self, loop):
        self.loop = loop
        self.playwright = None
        self.browser = None
        self.page = None

    async def init(self):
        if not self.playwright:
            from playwright.async_api import async_playwright
            self.playwright = await async_playwright().start()
            self.browser = await self.playwright.chromium.launch(
                headless=True,
                args=["--no-sandbox"]
            )
            self.page = await self.browser.new_page()

    async def goto(self, url):
        await self.init()
        if not url.startswith("http"):
            raise Exception("Invalid URL")
        await self.page.goto(url, timeout=5000)
        return {"status": "ok"}

    async def click(self, selector):
        await self.init()
        await self.page.click(selector)
        return {"status": "ok"}

    async def type(self, selector, text):
        await self.init()
        await self.page.fill(selector, text)
        return {"status": "ok"}

    async def screenshot(self):
        await self.init()
        img = await self.page.screenshot(full_page=True)
        return {
            "status": "ok",
            "image_base64": base64.b64encode(img).decode()
        }

    async def get_dom(self):
        await self.init()
        html = await self.page.content()
        return {"status": "ok", "html": html}


# =========================
# 🧠 KERNEL WORKER
# =========================
def kernel_worker(conn: Connection):
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    browser = BrowserManager(loop)

    # safe builtins
    safe_builtins = {
        k: v for k, v in builtins.__dict__.items()
        if k not in FORBIDDEN_BUILTINS
    }
    safe_builtins["__import__"] = safe_import
    globals_dict = {"__builtins__": safe_builtins}

    while True:
        try:
            msg = conn.recv()

            # =================
            # PYTHON EXECUTION
            # =================
            if msg["type"] == "execute":
                result = execute_code(msg["code"], globals_dict)
                conn.send(result)

            # =================
            # BROWSER ACTIONS
            # =================
            elif msg["type"] == "browser":
                action = msg["action"]
                params = msg.get("params", {})
                coro = getattr(browser, action)(**params)
                result = loop.run_until_complete(coro)
                conn.send(result)

            elif msg["type"] == "reset":
                globals_dict = {"__builtins__": safe_builtins}
                conn.send({"status": "ok"})

        except Exception:
            conn.send({
                "status": "error",
                "error_type": "KernelCrash",
                "error_message": traceback.format_exc()
            })


# =========================
# ⚙️ PYTHON EXECUTION
# =========================
def execute_code(code, globals_dict):
    stdout = io.StringIO()
    stderr = io.StringIO()
    result_container = {}

    def runner():
        try:
            local_vars = {}
            with contextlib.redirect_stdout(stdout):
                with contextlib.redirect_stderr(stderr):
                    exec(code, globals_dict, local_vars)

            result_container["status"] = "ok"
            result_container["stdout"] = stdout.getvalue()
            result_container["stderr"] = stderr.getvalue()

            if local_vars:
                last_value = list(local_vars.values())[-1]
                result_container["result"] = serialize(last_value)
            else:
                result_container["result"] = None
        except Exception as e:
            result_container["status"] = "error"
            result_container["error_type"] = type(e).__name__
            result_container["error_message"] = str(e)
            result_container["traceback"] = traceback.format_exc()

    thread = Thread(target=runner)
    thread.start()
    thread.join(EXEC_TIMEOUT)

    if thread.is_alive():
        return {
            "status": "timeout",
            "error_message": f"Execution exceeded {EXEC_TIMEOUT}s"
        }
    return result_container


# =========================
# 🔧 SERIALIZER
# =========================
def serialize(obj):
    try:
        import pandas as pd
        if isinstance(obj, pd.DataFrame):
            return {
                "type": "dataframe",
                "preview": obj.head().to_dict(orient="records")
            }
    except Exception:
        pass

    if isinstance(obj, (str, int, float, list, dict, bool)):
        return {"type": "primitive", "value": obj}

    return {"type": "repr", "value": repr(obj)}


# =========================
# 🧠 SUPERVISOR
# =========================
class KernelSupervisor:
    def __init__(self):
        self.parent_conn = None
        self.process = None
        self.start_kernel()

    def start_kernel(self):
        parent_conn, child_conn = multiprocessing.Pipe()
        self.process = multiprocessing.Process(
            target=kernel_worker,
            args=(child_conn,),
            daemon=True
        )
        self.process.start()
        self.parent_conn = parent_conn

    def restart_kernel(self):
        if self.process:
            self.process.terminate()
            self.process.join()
        self.start_kernel()

    def execute(self, msg):
        try:
            self.parent_conn.send(msg)
            if self.parent_conn.poll(EXEC_TIMEOUT + 2):
                result = self.parent_conn.recv()
                if result.get("status") == "timeout":
                    self.restart_kernel()
                return result
            else:
                self.restart_kernel()
                return {
                    "status": "error",
                    "error_type": "TimeoutError",
                    "error_message": "Kernel unresponsive, restarted"
                }
        except Exception:
            self.restart_kernel()
            return {
                "status": "error",
                "error_type": "KernelFailure",
                "error_message": "Kernel crashed and restarted"
            }


SUPERVISOR = KernelSupervisor()

# =========================
# 🌐 FASTAPI
# =========================
app = FastAPI()


class CodeRequest(BaseModel):
    code: str


class BrowserRequest(BaseModel):
    action: str
    params: dict = {}


@app.post("/python/execute")
def execute_code_api(req: CodeRequest):
    start = time.time()
    result = SUPERVISOR.execute({
        "type": "execute",
        "code": req.code
    })
    result["execution_time"] = round(time.time() - start, 4)
    return result


@app.post("/browser/action")
def browser_action(req: BrowserRequest):
    start = time.time()
    result = SUPERVISOR.execute({
        "type": "browser",
        "action": req.action,
        "params": req.params
    })
    result["execution_time"] = round(time.time() - start, 4)
    return result


@app.post("/python/reset")
def reset():
    SUPERVISOR.restart_kernel()
    return {"status": "ok"}


@app.get("/health")
def health():
    return {"status": "ok"}
