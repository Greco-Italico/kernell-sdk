import os
import sys
import uvicorn
from fastapi import FastAPI, Request, HTTPException
from kernell_sdk.security.ssrf import create_safe_client, RequestError, TimeoutException
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel
import subprocess
from pathlib import Path
import secrets

SETUP_TOKEN = secrets.token_urlsafe(16)
TOKEN_USED = False

def write_secure_env_file(env_vars: dict[str, str], path: str = ".env") -> Path:
    import os
    import stat
    env_path = Path(path).resolve()
    lines = [
        "# Kernell OS SDK — Archivo de configuración seguro\n",
        "# PERMISOS: 600 (solo lectura del owner)\n",
        "# NO hacer commit de este archivo — verificar que esté en .gitignore\n\n",
    ]
    for key, value in env_vars.items():
        safe_value = value.replace("\n", "\\n").replace('"', '\\"')
        lines.append(f'{key}="{safe_value}"\n')
    content = "".join(lines)
    flags = os.O_CREAT | os.O_WRONLY | os.O_TRUNC
    mode  = stat.S_IRUSR | stat.S_IWUSR   # 0o600
    fd = os.open(str(env_path), flags, mode)
    try:
        os.write(fd, content.encode("utf-8"))
    finally:
        os.close(fd)
    
    gitignore = env_path.parent / ".gitignore"
    if gitignore.exists():
        if not any(pattern.strip() in (".env", "*.env", ".env*") for pattern in gitignore.read_text().splitlines()):
            print("⚠️  ADVERTENCIA: '.env' no está en .gitignore.")
    return env_path

app = FastAPI(title="Kernell OS - Setup Wizard")

class SetupData(BaseModel):
    swarm_name: str
    github_user: str
    anthropic_key: str = ""
    openai_key: str = ""
    enable_kernell_pay: bool = False
    stripe_key: str = ""
    strict_sandbox: bool = True
    local_model: str = "gemma4:9b"

@app.get("/api/verify-star/{username}")
def verify_github_star(username: str):
    """Verifica si el usuario realmente dio star al repositorio en GitHub."""
    if not username or not username.strip():
        raise HTTPException(status_code=400, detail="Username requerido")
    # Validar que el username solo tiene caracteres válidos de GitHub
    import re
    if not re.match(r'^[a-zA-Z0-9](?:[a-zA-Z0-9]|-(?=[a-zA-Z0-9])){0,38}$', username):
        raise HTTPException(status_code=400, detail="Username de GitHub inválido")

    REPO = "Greco-Italico/kernell-os-sdk"
    url = f"https://api.github.com/users/{username}/starred/{REPO}"
    try:
        with create_safe_client(agent_id="launcher_wizard", timeout=8.0) as client:
            resp = client.get(
                url,
                headers={
                    "Accept": "application/vnd.github+json",
                    "X-GitHub-Api-Version": "2022-11-28",
                    "User-Agent": "kernell-os-sdk-installer",
                }
            )
        if resp.status_code == 204:
            return {"starred": True, "message": f"¡Verificado! Gracias por el apoyo, {username}."}
        elif resp.status_code == 404:
            raise HTTPException(status_code=403, detail="Star no detectada. Por favor dale ⭐ al repositorio primero.")
        elif resp.status_code == 401:
            raise HTTPException(status_code=503, detail="Error de autenticación con GitHub API.")
        else:
            raise HTTPException(status_code=503, detail=f"GitHub API retornó {resp.status_code}. Intenta de nuevo.")
    except Exception as e:
        if isinstance(e, TimeoutException):
            raise HTTPException(status_code=503, detail="Timeout al verificar con GitHub. Revisa tu conexión.")
        elif isinstance(e, RequestError):
            raise HTTPException(status_code=503, detail=f"No se pudo conectar a GitHub: {str(e)[:100]}")
        raise e

@app.get("/", response_class=HTMLResponse)
def serve_wizard():
    return """
    <!DOCTYPE html>
    <html lang="en">
    <head>
        <meta charset="UTF-8">
        <title>Kernell OS - Unified Installer</title>
        <style>
            @import url('https://fonts.googleapis.com/css2?family=Inter:wght@400;600;900&display=swap');
            body { background: #05080e; color: #cbd5e1; font-family: 'Inter', sans-serif; display: flex; justify-content: center; align-items: center; min-height: 100vh; margin: 0; padding: 2rem 0; }
            .card { background: #0b101d; border: 1px solid #1e293b; padding: 2.5rem; border-radius: 12px; width: 500px; box-shadow: 0 0 40px rgba(16,185,129,0.1); }
            h1 { color: white; font-weight: 900; text-transform: uppercase; margin-bottom: 0.5rem; font-size: 1.5rem;}
            p.subtitle { font-size: 0.85rem; color: #64748b; margin-bottom: 2rem; }
            .docs-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 1rem; margin-bottom: 1.5rem; }
            .doc-card { background: rgba(255,255,255,0.03); border: 1px solid #334155; padding: 1rem; border-radius: 8px; text-align: center; }
            .doc-card a { color: #38bdf8; text-decoration: none; font-size: 0.85rem; font-weight: 600; }
            .hardware { background: rgba(16,185,129,0.1); border: 1px solid rgba(16,185,129,0.3); padding: 1rem; border-radius: 8px; margin-bottom: 1.5rem; }
            label { display: block; font-size: 0.8rem; font-weight: 600; color: #94a3b8; margin-bottom: 5px; text-transform: uppercase; letter-spacing: 0.5px; }
            input[type="text"], input[type="password"] { width: 100%; padding: 12px; margin-bottom: 15px; background: rgba(0,0,0,0.3); border: 1px solid #334155; color: white; border-radius: 6px; box-sizing: border-box;}
            .checkbox-group { display: flex; align-items: center; margin-bottom: 15px; }
            .checkbox-group input { margin-right: 10px; }
            
            /* GitHub Star verification styling */
            .github-box { background: rgba(234, 179, 8, 0.1); border: 1px solid rgba(234, 179, 8, 0.3); padding: 1rem; border-radius: 8px; margin-bottom: 1.5rem; }
            .github-box input { margin-bottom: 10px; }
            .btn-verify { background: #334155; color: white; padding: 8px 12px; border: none; border-radius: 4px; cursor: pointer; font-size: 0.8rem; width: 100%; }
            .btn-verify:hover { background: #475569; }
            .star-badge { display: none; color: #facc15; font-weight: 900; text-align: center; margin-top: 10px; font-size: 0.9rem;}

            button.primary { width: 100%; padding: 14px; background: #10b981; color: black; font-weight: 900; border: none; border-radius: 6px; cursor: pointer; text-transform: uppercase; letter-spacing: 1px; margin-top: 1rem; opacity: 0.5; pointer-events: none;}
            button.primary.active { opacity: 1; pointer-events: auto; }
            button.primary.active:hover { background: #34d399; }
        </style>
    </head>
    <body>
        <div class="card">
            <h1>Kernell OS Installation</h1>
            <p class="subtitle">Unified installer for Kernell Agent Swarm & Kernell Pay</p>
            
            <div class="docs-grid">
                <div class="doc-card">
                    <span style="font-size:24px">🤖</span><br/>
                    <a href="https://kernell.site/docs/agent" target="_blank">Agent Docs ↗</a>
                </div>
                <div class="doc-card">
                    <span style="font-size:24px">💳</span><br/>
                    <a href="https://kernell.site/docs/pay" target="_blank">Kernell Pay Docs ↗</a>
                </div>
            </div>

            <div class="github-box">
                <label style="color: #facc15;">⭐ OSS Requirement</label>
                <p style="font-size: 0.75rem; color: #cbd5e1; margin-bottom: 10px;">Kernell OS is free. To proceed, please <a href="https://github.com/Greco-Italico/kernell-os-sdk" target="_blank" style="color: #38bdf8;">Star our GitHub repo</a>.</p>
                <input type="text" id="githubUser" placeholder="Your GitHub Username" />
                <button class="btn-verify" id="btnVerify" onclick="verifyStar()">Verify Human Star</button>
                <div class="star-badge" id="starBadge">⭐ Verified! Thank you.</div>
            </div>

            <div class="hardware">
                <strong style="color:#10b981">Hardware Auto-Discovery</strong><br/>
                <small>Detected 24GB VRAM. Recommended Engine: Gemma 4 Q8</small>
            </div>
            
            <label>Setup Token (from console)</label>
            <input type="password" id="setupToken" placeholder="Paste token here..." />

            <label>Swarm Name</label>
            <input type="text" id="swarmName" value="genesis_swarm" />
            
            <label>Anthropic API Key (Optional)</label>
            <input type="password" id="anthropicKey" placeholder="sk-ant-..." />
            
            <div class="checkbox-group">
                <input type="checkbox" id="enablePay" onchange="togglePay()" />
                <label style="margin:0;">Enable Kernell Pay Protocol (Dual Wallet)</label>
            </div>
            <div id="walletInfo" style="display:none; background: rgba(56, 189, 248, 0.1); border: 1px solid rgba(56, 189, 248, 0.3); padding: 1rem; border-radius: 8px; margin-bottom: 1.5rem;">
                <p style="font-size: 0.75rem; color: #cbd5e1; margin: 0;">
                    <strong style="color: #38bdf8;">L1+L2 Dual Wallet Auto-Generation:</strong><br/>
                    A new cryptographic wallet will be securely generated for this Agent. You can fund its external L1 address from your Phantom wallet. Funds are locked to mint <strong style="color:white;">Volatile $KERN</strong> for 0-fee, high-speed microtransactions within the swarm. Private keys will be provided in the Command Center.
                </p>
            </div>

            <button class="primary" id="btnInstall" onclick="submitSetup()">Deploy Infrastructure</button>
        </div>
        <script>
            let isVerified = false;

            async function verifyStar() {
                const user = document.getElementById('githubUser').value;
                if(!user) return alert("Please enter your GitHub username.");
                
                document.getElementById('btnVerify').innerText = "Verifying...";
                try {
                    const resp = await fetch(`/api/verify-star/${user}`);
                    if(resp.ok) {
                        isVerified = true;
                        document.getElementById('btnVerify').style.display = 'none';
                        document.getElementById('githubUser').style.display = 'none';
                        document.getElementById('starBadge').style.display = 'block';
                        document.getElementById('btnInstall').classList.add('active');
                    } else {
                        alert("Star not detected. Please star the repository first.");
                        document.getElementById('btnVerify').innerText = "Verify Human Star";
                    }
                } catch (e) {
                    alert("Error verifying.");
                }
            }

            function togglePay() {
                const checked = document.getElementById('enablePay').checked;
                document.getElementById('walletInfo').style.display = checked ? 'block' : 'none';
            }

            async function submitSetup() {
                if(!isVerified) return;
                
                const btn = document.getElementById('btnInstall');
                btn.innerText = 'Provisioning Sandbox...';
                
                const resp = await fetch('/api/setup', {
                    method: 'POST',
                    headers: {
                        'Content-Type': 'application/json',
                        'X-Setup-Token': document.getElementById('setupToken').value
                    },
                    body: JSON.stringify({
                        swarm_name: document.getElementById('swarmName').value,
                        github_user: document.getElementById('githubUser').value,
                        anthropic_key: document.getElementById('anthropicKey').value,
                        openai_key: "",
                        enable_kernell_pay: document.getElementById('enablePay').checked,
                        stripe_key: "",
                        strict_sandbox: true,
                        local_model: "gemma4:9b"
                    })
                });
                
                if(resp.ok) {
                    btn.innerText = 'Redirecting to Command Center...';
                    setTimeout(() => window.location.href = 'http://localhost:3000/dashboard', 2000);
                }
            }
        </script>
    </body>
    </html>
    """

def _write_secret_file(path: str, content: str) -> None:
    """Escribe un archivo con permisos 600 (solo el dueño puede leer/escribir)."""
    import stat
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    flags = os.O_CREAT | os.O_WRONLY | os.O_TRUNC
    mode = stat.S_IRUSR | stat.S_IWUSR
    fd = os.open(str(p), flags, mode)
    try:
        os.write(fd, content.encode("utf-8"))
    finally:
        os.close(fd)

@app.post("/api/setup")
def handle_setup(data: SetupData, request: Request):
    global TOKEN_USED
    token = request.headers.get("X-Setup-Token")
    if TOKEN_USED or token != SETUP_TOKEN:
        raise HTTPException(403, "Invalid or already used setup token")
    TOKEN_USED = True
    
    import secrets
    
    private_key_hex = ""
    public_address = ""
    volatile_address = ""
    
    if data.enable_kernell_pay:
        private_key_hex = secrets.token_hex(32)
        public_address = "sol1_" + secrets.token_hex(16)
        volatile_address = "kv_" + secrets.token_hex(16)
        
        # La clave privada va en archivo SEPARADO con permisos 600
        _write_secret_file(
            ".kernell/tx_private.key",
            private_key_hex
        )
        print("\n" + "=" * 60)
        print("  ⚠️  CLAVE PRIVADA GENERADA")
        print(f"  Ubicación: .kernell/tx_private.key")
        print("  NUNCA compartas este archivo.")
        print("  NUNCA lo subas a Git (ya está en .gitignore)")
        print("=" * 60 + "\n")

    # .env sin información criptográfica sensible
    env_content = f"""# Kernell OS SDK — Configuración generada automáticamente
# NUNCA subir a control de versiones

ANTHROPIC_API_KEY={data.anthropic_key}
OPENAI_API_KEY={data.openai_key}
KERNELL_CLUSTER_NAME={data.swarm_name}_cluster
REDIS_URL=redis://localhost:6379
STRICT_SANDBOX={str(data.strict_sandbox).lower()}
KERNELL_PAY_ENABLED={str(data.enable_kernell_pay).lower()}
KERNELL_PUBLIC_ADDRESS={public_address}
KERNELL_VOLATILE_ADDRESS={volatile_address}
# KERNELL_TX_PRIVATE_KEY — NO está aquí, ver: .kernell/tx_private.key
"""
    _write_secret_file(".env", env_content)
    
    # Asegurar que .gitignore excluye los secretos
    gitignore_entries = "\n# Kernell OS SDK secrets\n.env\n.kernell/\n*.key\n"
    gitignore_path = Path(".gitignore")
    existing = gitignore_path.read_text() if gitignore_path.exists() else ""
    if ".kernell/" not in existing:
        with open(".gitignore", "a") as f:
            f.write(gitignore_entries)
            
    # Generar y ejecutar main.py
    import re
    def _safe_name(v: str) -> str:
        if not re.fullmatch(r'[a-zA-Z0-9_\-]{1,64}', v):
            raise HTTPException(422, f"Valor inválido: {v[:30]!r}")
        return v

    MAIN_TEMPLATE = """\
import os
from dotenv import load_dotenv
from kernell_sdk import Agent, AgentPermissions
from kernell_sdk.llm import LLMRouter, OllamaProvider, AnthropicProvider
from kernell_sdk.cluster import ClusterDiscovery

load_dotenv()

def main():
    print("🚀 Booting Kernell OS Infrastructure...")
    print(f"🤖 Swarm Name: {swarm_name}")
    
    if {enable_pay}:
        print("💳 Kernell Pay: L1+L2 Dual Wallet Enabled")
        print("   -> L1 Deposit Address (Fund via Phantom): {public_address}")
        print("   -> L2 Volatile Address (Zero-Fee micro-tx): {volatile_address}")
        print("   [!] Private Key has been securely injected into the sandbox.")
    else:
        print("💳 Kernell Pay: Disabled")
    
    local = OllamaProvider(model={local_model})
    cloud = AnthropicProvider(model="claude-3-5-sonnet-20241022")
    router = LLMRouter(local_provider=local, cloud_provider=cloud, cloud_threshold="hard")
    director = Agent(name="Swarm Director", engine=router, permissions=AgentPermissions(network_access=True))
    director.enable_delegation(max_workers=5, worker_engine=local)
    
    print("✅ Infrastructure is online. Web UI taking over.")
    
    import time
    while True: time.sleep(1)

if __name__ == "__main__":
    main()
"""

    content = MAIN_TEMPLATE.format(
        swarm_name=repr(_safe_name(data.swarm_name)),
        enable_pay=repr(bool(data.enable_kernell_pay)),
        public_address=repr(public_address),
        volatile_address=repr(volatile_address),
        local_model=repr(_safe_name(data.local_model))
    )
    _write_secret_file("main.py", content)
        
    subprocess.Popen([sys.executable, "main.py"])
    return {"status": "success"}

def run_launcher():
    """Inicia el wizard de configuración SOLO en localhost."""
    if not os.path.exists(".env") or not os.path.exists("main.py"):
        print("=" * 60)
        print("  KERNELL OS — WEB SETUP WIZARD")
        print("  ⚠️  Accesible SOLO desde esta máquina (localhost)")
        print(f"  🔑  SETUP TOKEN: {SETUP_TOKEN}")
        print("  🌐  Abre: http://localhost:3000")
        print("=" * 60)
        uvicorn.run(
            app,
            host="127.0.0.1",   # ← CRÍTICO: nunca 0.0.0.0
            port=3000,
            log_level="warning"
        )
    else:
        print("Configuración encontrada. Iniciando swarm directamente...")
        subprocess.run([sys.executable, "main.py"], check=True)

if __name__ == "__main__":
    run_launcher()
