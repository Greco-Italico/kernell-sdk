"""
Kernell OS SDK — Agent GUI & Control Panel
══════════════════════════════════════════════════════════════
A lightweight local web dashboard for the Agent.
Allows users to toggle permissions, adjust resources, and view
the Passport/Wallet details in a beautiful UI.

SECURITY:
  - Bearer token authentication on all API endpoints
  - Token generated at startup and printed to console only
  - CORS restricted to localhost only
  - Rate limiting on sensitive endpoints
"""
import logging
import secrets
import threading
import time
from typing import Any
from .agent import Agent

try:
    from fastapi import FastAPI, Depends, HTTPException, Request
    from fastapi.responses import HTMLResponse, JSONResponse
    from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
    from fastapi.middleware.cors import CORSMiddleware
    import uvicorn
    HAS_GUI = True
except ImportError:
    HAS_GUI = False

logger = logging.getLogger("kernell.gui")

# Import from single source of truth
from .constants import VALID_PERMISSIONS, RateLimiter  # noqa: F401


class AgentGUI:
    """Local Control Panel for the Kernell Agent."""
    def __init__(self, agent: Agent, port: int = 8500):
        self.agent = agent
        self.port = port
        self.auth_token = secrets.token_urlsafe(32)
        self._rate_limiter = RateLimiter(max_requests=30, window_seconds=60)
        self.app = FastAPI(title=f"Kernell Control Panel - {self.agent.name}")

        # Restrict CORS to exact localhost port
        self.app.add_middleware(
            CORSMiddleware,
            allow_origins=[
                f"http://127.0.0.1:{self.port}",
                f"http://localhost:{self.port}",
            ],
            allow_credentials=False,
            allow_methods=["GET", "POST"],
            allow_headers=["Authorization", "Content-Type"],
            max_age=600,
        )

        self._setup_routes()

    def _verify_token(self, request: "Any") -> bool:
        """Verify the bearer token from the Authorization header."""
        auth_header = request.headers.get("Authorization", "")
        if auth_header.startswith("Bearer "):
            token = auth_header[7:]
            if secrets.compare_digest(token, self.auth_token):
                return True
        raise HTTPException(status_code=401, detail="Unauthorized. Use the token printed at startup.")

    def _setup_routes(self):
        @self.app.get("/", response_class=HTMLResponse)
        def index():
            html = f"""
            <!DOCTYPE html>
            <html lang="en">
            <head>
                <meta charset="UTF-8">
                <meta name="viewport" content="width=device-width, initial-scale=1.0">
                <title>{self.agent.name} - Kernell OS Control Panel</title>
                <style>
                    @import url('https://fonts.googleapis.com/css2?family=Inter:wght@300;400;500;600;700&display=swap');
                    * {{ margin: 0; padding: 0; box-sizing: border-box; }}
                    body {{ background-color: #020509; color: #e2e8f0; font-family: 'Inter', sans-serif; padding: 2rem; }}
                    .glass {{ background: rgba(15, 23, 42, 0.7); backdrop-filter: blur(12px); border: 1px solid rgba(255, 255, 255, 0.1); border-radius: 12px; padding: 1.5rem; }}
                    .container {{ max-width: 900px; margin: 0 auto; }}
                    .header {{ display: flex; align-items: center; justify-content: space-between; margin-bottom: 2rem; }}
                    .header h1 {{ font-size: 1.75rem; font-weight: 700; color: white; }}
                    .status {{ padding: 4px 12px; border-radius: 999px; font-size: 0.7rem; font-weight: 700; text-transform: uppercase; letter-spacing: 0.05em; }}
                    .status.idle {{ background: rgba(34,197,94,0.2); color: #4ade80; }}
                    .status.working {{ background: rgba(168,85,247,0.2); color: #c084fc; }}
                    .grid {{ display: grid; grid-template-columns: 1fr 1fr; gap: 1.5rem; }}
                    .field {{ margin-bottom: 0.75rem; font-size: 0.875rem; }}
                    .field .label {{ color: #64748b; font-size: 0.75rem; text-transform: uppercase; letter-spacing: 0.05em; margin-bottom: 2px; }}
                    .field .value {{ color: #cbd5e1; word-break: break-all; }}
                    h2 {{ font-size: 1.125rem; font-weight: 600; margin-bottom: 1rem; color: white; }}
                    .perm-row {{ display: flex; align-items: center; justify-content: space-between; padding: 0.5rem 0; border-bottom: 1px solid rgba(255,255,255,0.05); }}
                    .perm-name {{ font-size: 0.875rem; font-weight: 500; text-transform: capitalize; }}
                    .switch {{ position: relative; display: inline-block; width: 44px; height: 24px; }}
                    .switch input {{ opacity: 0; width: 0; height: 0; }}
                    .slider {{ position: absolute; cursor: pointer; inset: 0; background-color: #334155; transition: .3s; border-radius: 24px; }}
                    .slider:before {{ position: absolute; content: ""; height: 18px; width: 18px; left: 3px; bottom: 3px; background: white; transition: .3s; border-radius: 50%; }}
                    input:checked + .slider {{ background-color: #a855f7; }}
                    input:checked + .slider:before {{ transform: translateX(20px); }}
                    .auth-box {{ background: rgba(234,179,8,0.1); border: 1px solid rgba(234,179,8,0.3); border-radius: 8px; padding: 1rem; margin-bottom: 1.5rem; }}
                    .auth-box input {{ width: 100%; padding: 8px 12px; background: rgba(0,0,0,0.3); border: 1px solid rgba(255,255,255,0.15); border-radius: 6px; color: white; font-family: monospace; font-size: 0.8rem; margin-top: 0.5rem; }}
                    .auth-box label {{ font-size: 0.8rem; color: #facc15; font-weight: 600; }}
                    @media (max-width: 640px) {{ .grid {{ grid-template-columns: 1fr; }} }}
                </style>
            </head>
            <body>
                <div class="container" id="app">
                    <div class="header">
                        <h1>🌌 {self.agent.name}</h1>
                        <span class="status idle" id="statusBadge">{self.agent.state.status}</span>
                    </div>

                    <div class="auth-box">
                        <label>🔒 Auth Token (paste the token from your terminal)</label>
                        <input type="password" id="authToken" placeholder="Enter your bearer token..." />
                    </div>

                    <div class="grid">
                        <div class="glass">
                            <h2>Cryptographic Passport</h2>
                            <div class="field"><div class="label">Agent ID</div><div class="value">{self.agent.id}</div></div>
                            <div class="field"><div class="label">KAP Address</div><div class="value">{self.agent.passport.kap_address}</div></div>
                            <div class="field"><div class="label">Volatile Wallet</div><div class="value">{self.agent.passport.kern_volatile_address}</div></div>
                            <div class="field"><div class="label">Hardware UDID</div><div class="value">{self.agent.passport.hardware_udid[:16]}...</div></div>
                        </div>

                        <div class="glass">
                            <h2>Security Boundaries</h2>
                            <div id="permissionsContainer"></div>
                        </div>
                    </div>
                </div>

                <script>
                    const permissions = {{
                        network_access: {str(self.agent.permissions.network_access).lower()},
                        file_system_read: {str(self.agent.permissions.file_system_read).lower()},
                        file_system_write: {str(self.agent.permissions.file_system_write).lower()},
                        execute_commands: {str(self.agent.permissions.execute_commands).lower()},
                        browser_control: {str(self.agent.permissions.browser_control).lower()},
                        gui_automation: {str(self.agent.permissions.gui_automation).lower()}
                    }};

                    function getToken() {{
                        return document.getElementById('authToken').value;
                    }}

                    function renderPermissions() {{
                        const container = document.getElementById('permissionsContainer');
                        container.innerHTML = '';
                        for (const [key, value] of Object.entries(permissions)) {{
                            const row = document.createElement('div');
                            row.className = 'perm-row';
                            const name = key.replace(/_/g, ' ');
                            row.innerHTML = `
                                <span class="perm-name">${{name}}</span>
                                <label class="switch">
                                    <input type="checkbox" ${{value ? 'checked' : ''}} onchange="togglePerm('${{key}}', this.checked)">
                                    <span class="slider"></span>
                                </label>
                            `;
                            container.appendChild(row);
                        }}
                    }}

                    async function togglePerm(key, value) {{
                        const token = getToken();
                        if (!token) {{ alert('Please enter your auth token first.'); return; }}
                        const resp = await fetch(`/api/permissions/${{key}}`, {{
                            method: 'POST',
                            headers: {{
                                'Content-Type': 'application/json',
                                'Authorization': `Bearer ${{token}}`
                            }},
                            body: JSON.stringify({{state: value}})
                        }});
                        if (resp.status === 401) {{ alert('Invalid token!'); }}
                        else {{ permissions[key] = value; }}
                    }}

                    renderPermissions();
                </script>
            </body>
            </html>
            """
            return html

        @self.app.post("/api/permissions/{{permission}}")
        def update_permission(permission: str, request: Request, data: dict):
            # Rate limiting
            client_ip = request.client.host if request.client else "unknown"
            if not self._rate_limiter.is_allowed(client_ip):
                raise HTTPException(status_code=429, detail="Rate limit exceeded")

            # Authentication
            self._verify_token(request)

            # Whitelist validation
            if permission not in VALID_PERMISSIONS:
                raise HTTPException(status_code=400, detail=f"Invalid permission: {{permission}}")

            state = data.get("state")
            if not isinstance(state, bool):
                raise HTTPException(status_code=400, detail="'state' must be a boolean")

            self.agent.toggle_permission(permission, state)
            logger.info(f"[AUDIT] Permission '{{permission}}' changed to {{state}} by {{client_ip}}")
            return {{"status": "updated", "permission": permission, "state": state}}

    def start(self):
        """Starts the GUI server in a non-blocking thread."""
        if not HAS_GUI:
            logger.error("FastAPI or Uvicorn not installed. Run: pip install kernell-os-sdk[gui]")
            return

        # Print the auth token ONCE to the console (never to logs)
        print(f"")
        print(f"  🔐 KERNELL CONTROL PANEL AUTH TOKEN")
        print(f"  ════════════════════════════════════")
        print(f"  {self.auth_token}")
        print(f"  ════════════════════════════════════")
        print(f"  Paste this token in the browser to manage permissions.")
        print(f"  Dashboard: http://127.0.0.1:{self.port}")
        print(f"")

        logger.info(f"Starting Agent Control Panel on http://127.0.0.1:{self.port}")

        def run_server():
            uvicorn.run(self.app, host="127.0.0.1", port=self.port, log_level="warning")

        thread = threading.Thread(target=run_server, daemon=True)
        thread.start()
        return thread
