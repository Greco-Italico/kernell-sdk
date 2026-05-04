"""
Kernell OS SDK — Command Center Dashboard
══════════════════════════════════════════
Full-featured local dashboard for SDK agents with:
  • Real-time metrics (tokens, budget, SLO, circuit breakers)
  • API key management (add/remove keys for tools)
  • Permission toggles with live state
  • Passport & wallet info
  • Skill registry viewer
  • Audit log viewer
"""
import json, logging, secrets, time, threading
from typing import Any, Dict, Optional
from pathlib import Path

logger = logging.getLogger("kernell.dashboard")

try:
    from fastapi import FastAPI, HTTPException, Request
    from fastapi.responses import HTMLResponse, JSONResponse
    from fastapi.middleware.cors import CORSMiddleware
    import uvicorn
    HAS_DEPS = True
except ImportError:
    HAS_DEPS = False
    Request = Any
    HTTPException = Exception
    def FastAPI(**kwargs):
        class DummyApp:
            def add_middleware(self, *a, **kw): pass
            def get(self, *a, **kw): return lambda f: f
            def post(self, *a, **kw): return lambda f: f
            def delete(self, *a, **kw): return lambda f: f
        return DummyApp()

# Import from single source of truth
from .constants import VALID_PERMISSIONS, RateLimiter  # noqa: F401

# Router dashboard bridge (Token Economy integration)
try:
    from .router.dashboard_bridge import (
        create_router_api, ROUTER_DASHBOARD_CARD_HTML, ROUTER_DASHBOARD_JS,
    )
    HAS_ROUTER_BRIDGE = True
except ImportError:
    HAS_ROUTER_BRIDGE = False
    ROUTER_DASHBOARD_CARD_HTML = ""
    ROUTER_DASHBOARD_JS = ""

def create_auth_middleware(app, tokens: dict):
    @app.middleware("http")
    async def auth_middleware(request: Request, call_next):
        if request.method == "OPTIONS":
            return await call_next(request)
        
        auth = request.headers.get("Authorization")
        if not auth or not auth.startswith("Bearer "):
            from fastapi.responses import JSONResponse
            return JSONResponse(status_code=401, content={"detail": "Missing token"})
            
        token = auth.split(" ", 1)[1]
        
        role = None
        for valid_token, assigned_role in tokens.items():
            if secrets.compare_digest(token, valid_token):
                role = assigned_role
                break
                
        if not role:
            from fastapi.responses import JSONResponse
            return JSONResponse(status_code=403, content={"detail": "Invalid token"})
            
        request.state.role = role
        return await call_next(request)

class CommandCenter:
    """Full Command Center dashboard for an SDK agent."""

    def __init__(self, agent, port: int = 8500,
                 router_metrics=None, router_entrypoint=None,
                 cost_estimator=None, hardware_config=None):
        self.agent = agent
        self.port = port
        self.auth_token = secrets.token_urlsafe(32)
        self.read_token = secrets.token_urlsafe(32)
        self.tokens = {
            self.auth_token: "admin",
            self.read_token: "read"
        }
        self._api_keys: Dict[str, str] = {}  # tool_name → masked key
        self._api_keys_raw: Dict[str, str] = {}  # tool_name → actual key
        self._audit_log: list = []
        self._rate_limiter = RateLimiter(max_requests=60, window_seconds=60)
        self.app = FastAPI(title=f"Kernell Command Center — {agent.name}")
        self.app.add_middleware(
            CORSMiddleware,
            allow_origins=[
                "https://app.kernell.ai",
                f"http://127.0.0.1:{self.port}",
                f"http://localhost:{self.port}",
            ],
            allow_credentials=True,
            allow_methods=["*"],
            allow_headers=["*"],
            max_age=600,
        )
        
        create_auth_middleware(self.app, self.tokens)
        self._setup()

        # Mount Token Economy router API (if available)
        if HAS_ROUTER_BRIDGE:
            token_budget = getattr(agent, 'budget', None)
            router_api = create_router_api(
                metrics_collector=router_metrics,
                entrypoint=router_entrypoint,
                cost_estimator=cost_estimator,
                hardware_config=hardware_config,
                token_budget=token_budget,
            )
            if router_api:
                self.app.include_router(router_api, prefix="/api/router")

    def _auth(self, request: Request):
        auth = request.headers.get("Authorization", "")
        if auth.startswith("Bearer ") and secrets.compare_digest(auth[7:], self.auth_token):
            return True
        raise HTTPException(401, "Unauthorized")

    def _audit(self, action: str, detail: str, ip: str = ""):
        entry = {"ts": time.time(), "action": action, "detail": detail, "ip": ip}
        self._audit_log.append(entry)
        if len(self._audit_log) > 500:
            self._audit_log = self._audit_log[-500:]
        logger.info(f"[AUDIT] {action}: {detail}")

    def _setup(self):
        @self.app.get("/", response_class=HTMLResponse)
        def dashboard():
            return self._render_html()

        @self.app.get("/api/status")
        def status(request: Request):
            self._auth(request)
            budget_snap = self.agent.budget.snapshot() if hasattr(self.agent, 'budget') and self.agent.budget else None
            slo_score = self.agent.slo.score() if hasattr(self.agent, 'slo') and self.agent.slo else None
            governor_snap = self.agent.governor.full_snapshot() if hasattr(self.agent, 'governor') and self.agent.governor else None
            return {
                "agent": self.agent.name,
                "id": self.agent.id,
                "state": self.agent.state.model_dump(),
                "permissions": self.agent.permissions.model_dump(),
                "budget": budget_snap.__dict__ if budget_snap else None,
                "slo": slo_score.__dict__ if slo_score else None,
                "governor": governor_snap,
                "passport": {
                    "kap": self.agent.passport.kap_address,
                    "volatile_wallet": self.agent.passport.kern_volatile_address,
                    "udid": self.agent.passport.hardware_udid[:16] + "...",
                },
                "api_keys": list(self._api_keys.keys()),
                "skills": list(self.agent._skills.keys()),
            }

        @self.app.post("/api/permissions/{perm}")
        def toggle_perm(perm: str, request: Request, data: dict):
            self._auth(request)
            ip = request.client.host if request.client else ""
            if not self._rate_limiter.is_allowed(ip): raise HTTPException(429)
            if perm not in VALID_PERMISSIONS: raise HTTPException(400, f"Invalid: {perm}")
            if not isinstance(data.get("state"), bool): raise HTTPException(400)
            self.agent.toggle_permission(perm, data["state"])
            self._audit("permission_change", f"{perm}={data['state']}", ip)
            return {"ok": True}

        @self.app.post("/api/keys")
        def add_key(request: Request, data: dict):
            self._auth(request)
            ip = request.client.host if request.client else ""
            name = data.get("name", "").strip()
            key = data.get("key", "").strip()
            if not name or not key: raise HTTPException(400, "name and key required")
            self._api_keys_raw[name] = key
            self._api_keys[name] = key[:4] + "..." + key[-4:]
            self._audit("api_key_added", f"{name} ({self._api_keys[name]})", ip)
            return {"ok": True, "masked": self._api_keys[name]}

        @self.app.delete("/api/keys/{name}")
        def remove_key(name: str, request: Request):
            self._auth(request)
            ip = request.client.host if request.client else ""
            self._api_keys_raw.pop(name, None)
            self._api_keys.pop(name, None)
            self._audit("api_key_removed", name, ip)
            return {"ok": True}

        @self.app.get("/api/keys/{name}")
        def get_key_value(name: str, request: Request):
            """SDK agents call this to retrieve an API key at runtime."""
            self._auth(request)
            val = self._api_keys_raw.get(name)
            if not val: raise HTTPException(404, f"Key '{name}' not found")
            return {"key": val}

        @self.app.get("/api/audit")
        def audit_log(request: Request):
            self._auth(request)
            return {"log": self._audit_log[-50:]}

    def get_api_key(self, name: str) -> Optional[str]:
        """Programmatic access to stored API keys (for agent skills)."""
        return self._api_keys_raw.get(name)

    def _render_html(self) -> str:
        return f"""<!DOCTYPE html>
<html lang="en"><head>
<meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{self.agent.name} — Command Center</title>
<style>
@import url('https://fonts.googleapis.com/css2?family=Inter:wght@300;400;500;600;700;800&display=swap');
*{{margin:0;padding:0;box-sizing:border-box}}
body{{background:#020509;color:#e2e8f0;font-family:'Inter',sans-serif;min-height:100vh}}
.top{{background:linear-gradient(135deg,#0f0a1e 0%,#1a0a2e 50%,#0a1628 100%);padding:1.5rem 2rem;border-bottom:1px solid rgba(139,92,246,0.2);display:flex;align-items:center;justify-content:space-between}}
.top h1{{font-size:1.5rem;font-weight:800;background:linear-gradient(135deg,#a855f7,#6366f1);-webkit-background-clip:text;-webkit-text-fill-color:transparent}}
.top .badge{{padding:4px 12px;border-radius:999px;font-size:.65rem;font-weight:700;text-transform:uppercase;letter-spacing:.08em}}
.badge.idle{{background:rgba(34,197,94,.15);color:#4ade80}}
.badge.working{{background:rgba(168,85,247,.15);color:#c084fc}}
.wrap{{max-width:1200px;margin:0 auto;padding:1.5rem}}
.auth-bar{{background:rgba(234,179,8,.08);border:1px solid rgba(234,179,8,.25);border-radius:10px;padding:1rem;margin-bottom:1.5rem;display:flex;gap:1rem;align-items:center}}
.auth-bar label{{font-size:.75rem;color:#facc15;font-weight:600;white-space:nowrap}}
.auth-bar input{{flex:1;padding:8px 12px;background:rgba(0,0,0,.4);border:1px solid rgba(255,255,255,.1);border-radius:6px;color:#fff;font-family:monospace;font-size:.8rem}}
.grid{{display:grid;grid-template-columns:repeat(auto-fit,minmax(340px,1fr));gap:1rem}}
.card{{background:rgba(15,23,42,.65);backdrop-filter:blur(16px);border:1px solid rgba(255,255,255,.07);border-radius:14px;padding:1.25rem}}
.card h2{{font-size:.95rem;font-weight:700;margin-bottom:1rem;color:#fff;display:flex;align-items:center;gap:.5rem}}
.card h2 span{{font-size:1.1rem}}
.field{{margin-bottom:.6rem;font-size:.8rem}}
.field .lbl{{color:#64748b;font-size:.65rem;text-transform:uppercase;letter-spacing:.05em}}
.field .val{{color:#cbd5e1;word-break:break-all}}
.meter{{height:6px;background:rgba(255,255,255,.05);border-radius:3px;overflow:hidden;margin-top:4px}}
.meter .fill{{height:100%;border-radius:3px;transition:width .5s}}
.perm-row{{display:flex;align-items:center;justify-content:space-between;padding:.4rem 0;border-bottom:1px solid rgba(255,255,255,.04)}}
.perm-name{{font-size:.8rem;font-weight:500;text-transform:capitalize}}
.sw{{position:relative;display:inline-block;width:40px;height:22px}}
.sw input{{opacity:0;width:0;height:0}}
.sl{{position:absolute;cursor:pointer;inset:0;background:#334155;transition:.3s;border-radius:22px}}
.sl:before{{position:absolute;content:"";height:16px;width:16px;left:3px;bottom:3px;background:#fff;transition:.3s;border-radius:50%}}
input:checked+.sl{{background:#a855f7}}
input:checked+.sl:before{{transform:translateX(18px)}}
.key-row{{display:flex;align-items:center;justify-content:space-between;padding:.35rem 0;border-bottom:1px solid rgba(255,255,255,.04);font-size:.8rem}}
.key-row .nm{{color:#a78bfa;font-weight:600}}
.key-row .mk{{color:#64748b;font-family:monospace;font-size:.7rem}}
.btn{{padding:6px 14px;border:none;border-radius:6px;font-size:.75rem;font-weight:600;cursor:pointer;transition:.2s}}
.btn-purple{{background:#7c3aed;color:#fff}}.btn-purple:hover{{background:#6d28d9}}
.btn-red{{background:rgba(239,68,68,.15);color:#f87171;border:1px solid rgba(239,68,68,.2)}}.btn-red:hover{{background:rgba(239,68,68,.25)}}
.btn-sm{{padding:3px 8px;font-size:.65rem}}
.add-key{{display:flex;gap:.5rem;margin-top:.75rem}}
.add-key input{{flex:1;padding:6px 10px;background:rgba(0,0,0,.3);border:1px solid rgba(255,255,255,.1);border-radius:6px;color:#fff;font-size:.75rem}}
.metric{{text-align:center;padding:.5rem}}
.metric .num{{font-size:1.75rem;font-weight:800;background:linear-gradient(135deg,#a855f7,#6366f1);-webkit-background-clip:text;-webkit-text-fill-color:transparent}}
.metric .unit{{font-size:.65rem;color:#64748b;text-transform:uppercase;letter-spacing:.05em}}
.metrics-grid{{display:grid;grid-template-columns:repeat(3,1fr);gap:.5rem}}
.log-entry{{font-size:.7rem;color:#94a3b8;padding:.2rem 0;border-bottom:1px solid rgba(255,255,255,.03);font-family:monospace}}
.skill-tag{{display:inline-block;padding:3px 10px;margin:2px;border-radius:6px;font-size:.7rem;font-weight:600;background:rgba(99,102,241,.15);color:#818cf8;border:1px solid rgba(99,102,241,.2)}}
</style>
</head>
<body>
<div class="top">
  <h1>🌌 {self.agent.name} — Command Center</h1>
  <span class="badge idle" id="sBadge">IDLE</span>
</div>
<div class="wrap">
  <div class="auth-bar">
    <label>🔒 Auth Token</label>
    <input type="password" id="tok" placeholder="Paste token from terminal...">
  </div>
  <div class="grid">
    <!-- Metrics -->
    <div class="card"><h2><span>📊</span> Live Metrics</h2>
      <div class="metrics-grid">
        <div class="metric"><div class="num" id="mTasks">0</div><div class="unit">Tasks</div></div>
        <div class="metric"><div class="num" id="mTokens">0</div><div class="unit">Tokens/hr</div></div>
        <div class="metric"><div class="num" id="mKern">0.00</div><div class="unit">$KERN</div></div>
      </div>
      <div style="margin-top:.75rem">
        <div class="field"><div class="lbl">Hourly Budget</div>
          <div class="meter"><div class="fill" id="budgetBar" style="width:0%;background:linear-gradient(90deg,#22c55e,#a855f7)"></div></div>
        </div>
        <div class="field" style="margin-top:.5rem"><div class="lbl">SLO Health</div>
          <div class="val" id="sloStatus" style="color:#4ade80;font-weight:600">HEALTHY</div>
        </div>
      </div>
    </div>
    <!-- Passport -->
    <div class="card"><h2><span>🪪</span> Cryptographic Passport</h2>
      <div class="field"><div class="lbl">Agent ID</div><div class="val" id="pId">{self.agent.id}</div></div>
      <div class="field"><div class="lbl">KAP Address</div><div class="val" id="pKap">{self.agent.passport.kap_address}</div></div>
      <div class="field"><div class="lbl">Volatile Wallet</div><div class="val">{self.agent.passport.kern_volatile_address}</div></div>
      <div class="field"><div class="lbl">Hardware UDID</div><div class="val">{self.agent.passport.hardware_udid[:16]}...</div></div>
    </div>
    <!-- Permissions -->
    <div class="card"><h2><span>🛡️</span> Capabilities Boundaries</h2><div id="permsBox"></div></div>
    <!-- Circuit Breakers -->
    <div class="card"><h2><span>⚡</span> Circuit Breakers</h2><div id="defenseBox"></div></div>
    <!-- API Keys -->
    <div class="card"><h2><span>🔑</span> API Keys</h2>
      <div id="keysBox"></div>
      <div class="add-key">
        <input id="kName" placeholder="Tool name (e.g. openai)">
        <input id="kVal" type="password" placeholder="API key value">
        <button class="btn btn-purple" onclick="addKey()">Add</button>
      </div>
    </div>
    {ROUTER_DASHBOARD_CARD_HTML}
    <!-- Skills -->
    <div class="card"><h2><span>⚙️</span> Registered Skills</h2><div id="skillsBox"></div></div>
    <!-- Audit Log -->
    <div class="card"><h2><span>📜</span> Audit Log</h2><div id="logBox" style="max-height:200px;overflow-y:auto"></div></div>
  </div>
</div>
<script>
const T=()=>document.getElementById('tok').value;
const H={{'Authorization':`Bearer ${{T()}}`,'Content-Type':'application/json'}};
function headers(){{return{{'Authorization':`Bearer ${{T()}}`,'Content-Type':'application/json'}}}}

const perms={{{','.join(f"'{k}':{str(v).lower()}" for k,v in self.agent.permissions.model_dump().items() if k!='allowed_paths')}}};

function renderPerms(){{
  let h='';
  for(const[k,v] of Object.entries(perms)){{
    const n=k.replace(/_/g,' ');
    h+=`<div class="perm-row"><span class="perm-name">${{n}}</span>
    <label class="sw"><input type="checkbox" ${{v?'checked':''}} onchange="toggleP('${{k}}',this.checked)"><span class="sl"></span></label></div>`;
  }}
  document.getElementById('permsBox').innerHTML=h;
}}

async function toggleP(k,v){{
  if(!T()){{alert('Enter auth token first');return}}
  const r=await fetch(`/api/permissions/${{k}}`,{{method:'POST',headers:headers(),body:JSON.stringify({{state:v}})}});
  if(r.status===401)alert('Invalid token');
  else perms[k]=v;
}}

let apiKeys={{}};
function renderKeys(){{
  let h='';
  for(const[n,m] of Object.entries(apiKeys)){{
    h+=`<div class="key-row"><span class="nm">${{n}}</span><span class="mk">${{m}}</span>
    <button class="btn btn-red btn-sm" onclick="delKey('${{n}}')">✕</button></div>`;
  }}
  if(!Object.keys(apiKeys).length)h='<div style="color:#64748b;font-size:.75rem">No API keys configured</div>';
  document.getElementById('keysBox').innerHTML=h;
}}

async function addKey(){{
  const n=document.getElementById('kName').value.trim();
  const k=document.getElementById('kVal').value.trim();
  if(!n||!k)return;if(!T()){{alert('Enter auth token');return}}
  const r=await fetch('/api/keys',{{method:'POST',headers:headers(),body:JSON.stringify({{name:n,key:k}})}});
  if(r.ok){{const d=await r.json();apiKeys[n]=d.masked;renderKeys();document.getElementById('kName').value='';document.getElementById('kVal').value='';}}
}}

async function delKey(n){{
  await fetch(`/api/keys/${{n}}`,{{method:'DELETE',headers:headers()}});
  delete apiKeys[n];renderKeys();
}}

async function refresh(){{
  if(!T())return;
  try{{
    const r=await fetch('/api/status',{{headers:headers()}});
    if(!r.ok)return;const d=await r.json();
    document.getElementById('sBadge').textContent=d.state.status.toUpperCase();
    document.getElementById('sBadge').className='badge '+d.state.status;
    document.getElementById('mTasks').textContent=d.state.tasks_completed;
    document.getElementById('mKern').textContent=d.state.kern_earned.toFixed(2);
    if(d.budget){{
      document.getElementById('mTokens').textContent=d.budget.hourly_used;
      const pct=Math.min(100,(d.budget.hourly_used/Math.max(d.budget.hourly_limit,1))*100);
      document.getElementById('budgetBar').style.width=pct+'%';
      if(pct>80)document.getElementById('budgetBar').style.background='linear-gradient(90deg,#f59e0b,#ef4444)';
    }}
    if(d.slo){{
      const el=document.getElementById('sloStatus');
      el.textContent=d.slo.status;
      el.style.color=d.slo.status==='HEALTHY'?'#4ade80':d.slo.status==='DEGRADED'?'#fbbf24':'#f87171';
    }}
    if(d.api_keys){{d.api_keys.forEach(k=>{{if(!apiKeys[k])apiKeys[k]='****'}});renderKeys();}}
    if(d.skills){{
      document.getElementById('skillsBox').innerHTML=d.skills.map(s=>`<span class="skill-tag">${{s}}</span>`).join('');
    }}
    if(d.governor){{
      let h='';
      for(const [name, cb] of Object.entries(d.governor.breakers)){{
         let col = cb.state === 'CLOSED' ? '#4ade80' : cb.state === 'HALF_OPEN' ? '#fbbf24' : '#ef4444';
         h += `<div class="field"><div class="lbl" style="text-transform:none">${{name}}</div><div class="val" style="color:${{col}};font-weight:600;font-size:0.75rem">${{cb.state}} <span style="font-size:0.85em;color:#64748b;font-weight:normal">(${{cb.recent_failures}}/${{cb.failure_threshold}} fails)</span></div></div>`;
      }}
      document.getElementById('defenseBox').innerHTML = h;
    }}
    const lr=await fetch('/api/audit',{{headers:headers()}});
    if(lr.ok){{const ld=await lr.json();
      document.getElementById('logBox').innerHTML=ld.log.reverse().slice(0,20).map(e=>
        `<div class="log-entry">${{new Date(e.ts*1000).toLocaleTimeString()}} ${{e.action}} — ${{e.detail}}</div>`
      ).join('');
    }}
  }}catch(e){{}}
}}

renderPerms();renderKeys();
setInterval(refresh,3000);
setTimeout(refresh,500);
{ROUTER_DASHBOARD_JS}
</script>
</body></html>"""

    def start(self):
        if not HAS_DEPS:
            logger.error("Install: pip install kernell-os-sdk[gui]")
            return
        print(f"\n  🎯 KERNELL COMMAND CENTER")
        print(f"  ════════════════════════════════════")
        print(f"  Auth Token: {self.auth_token}")
        print(f"  Dashboard:  http://127.0.0.1:{self.port}")
        print(f"  ════════════════════════════════════\n")
        def run():
            uvicorn.run(self.app, host="127.0.0.1", port=self.port, log_level="warning")
        t = threading.Thread(target=run, daemon=True)
        t.start()
        return t
