"""
Kernell OS SDK — Router Dashboard Bridge
══════════════════════════════════════════
Connects the Intelligent Router's metrics and controls
to the CommandCenter dashboard.

This module provides:
  1. FastAPI routes for the router control panel
  2. Dashboard HTML/JS for the Token Economy views
  3. API key management for inference providers (Groq, DeepSeek, etc.)
  4. Budget control endpoints
  5. Pre-execution cost estimation endpoint

It does NOT replace the existing dashboard — it EXTENDS it.
"""
from __future__ import annotations

import logging
import time
from typing import Any, Dict, List, Optional

logger = logging.getLogger("kernell.router.dashboard")

try:
    from fastapi import APIRouter, HTTPException, Request
    HAS_FASTAPI = True
except ImportError:
    HAS_FASTAPI = False
    APIRouter = object


def create_router_api(
    metrics_collector=None,
    entrypoint=None,
    cost_estimator=None,
    model_registry=None,
    hardware_config=None,
    token_budget=None,
) -> "APIRouter":
    """
    Create FastAPI sub-router for the Token Economy dashboard.
    
    Mount this on the existing CommandCenter:
        router_api = create_router_api(metrics, entrypoint, ...)
        app.include_router(router_api, prefix="/api/router")
    """
    if not HAS_FASTAPI:
        return None

    api = APIRouter(tags=["Token Economy"])

    # ── Provider API key storage ─────────────────────────────────────
    _provider_keys: Dict[str, Dict[str, str]] = {
        "cheap_api": {},    # e.g. {"groq": "gsk_...", "deepseek": "sk-..."}
        "premium_api": {},  # e.g. {"anthropic": "sk-ant-...", "openai": "sk-..."}
    }

    # ── 1. Cost Overview ─────────────────────────────────────────────

    @api.get("/metrics")
    def get_router_metrics(request: Request):
        """Full dashboard metrics for the Token Economy panel."""
        if not metrics_collector:
            return {"error": "Metrics collector not initialized"}
        return metrics_collector.get_dashboard_metrics()

    @api.get("/metrics/prometheus")
    def get_prometheus_metrics():
        """Prometheus text exposition format for /metrics scrape."""
        if not metrics_collector:
            return ""
        from fastapi.responses import PlainTextResponse
        return PlainTextResponse(
            content=metrics_collector.export_prometheus(),
            media_type="text/plain; version=0.0.4; charset=utf-8",
        )

    # ── 2. Router Status & Control ───────────────────────────────────

    @api.get("/status")
    def get_router_status():
        """Current router mode and counters."""
        if not entrypoint:
            return {"mode": "not_configured"}
        return entrypoint.get_status()

    @api.post("/config")
    def update_router_config(data: dict):
        """Update router feature flags (shadow mode, canary %, etc.)."""
        if not entrypoint:
            raise HTTPException(400, "Router entrypoint not configured")

        config = entrypoint._config
        if "enable_intelligent_router" in data:
            config.enable_intelligent_router = bool(data["enable_intelligent_router"])
        if "shadow_mode" in data:
            config.shadow_mode = bool(data["shadow_mode"])
        if "canary_percent" in data:
            pct = float(data["canary_percent"])
            config.canary_percent = max(0.0, min(1.0, pct))

        logger.info(f"Router config updated: mode={entrypoint._current_mode()}")
        return {"ok": True, "mode": entrypoint._current_mode()}

    # ── 3. Cost Estimation ───────────────────────────────────────────

    @api.post("/estimate")
    def estimate_cost(data: dict):
        """Pre-execution cost simulation."""
        if not cost_estimator:
            raise HTTPException(400, "Cost estimator not configured")
        task = data.get("task", "")
        if not task:
            raise HTTPException(400, "task field required")
        return cost_estimator.estimate(task)

    # ── 4. Local Models ──────────────────────────────────────────────

    @api.get("/models/local")
    def get_local_models():
        """List installed/installable local models based on hardware."""
        if not hardware_config:
            return {"models": [], "tier": "unknown"}
        return {
            "tier": hardware_config.tier_name,
            "available_ram_gb": hardware_config.available_ram_gb,
            "has_gpu": hardware_config.has_gpu,
            "vram_gb": hardware_config.vram_gb,
            "models": [
                {
                    "name": m.name,
                    "ollama_tag": m.ollama_tag,
                    "params_b": m.params_b,
                    "ram_q4_gb": m.ram_q4_gb,
                    "tier": m.tier.value,
                    "max_difficulty": m.max_difficulty,
                    "specialties": m.specialties,
                    "is_classifier": m.is_classifier,
                }
                for m in hardware_config.installable_models
            ],
            "tier_coverage": {
                k.value: (v.name if v else None)
                for k, v in hardware_config.tier_map.items()
            },
        }

    # ── 5. Inference Providers (Cheap + Premium) ─────────────────────

    @api.get("/providers")
    def get_providers():
        """List configured inference providers with masked keys."""
        result = {}
        for tier, providers in _provider_keys.items():
            result[tier] = {
                name: key[:4] + "..." + key[-4:] if len(key) > 8 else "****"
                for name, key in providers.items()
            }
        return result

    @api.post("/providers/{tier}/{provider_name}")
    def set_provider_key(tier: str, provider_name: str, data: dict):
        """Add/update an API key for an inference provider."""
        if tier not in _provider_keys:
            raise HTTPException(400, f"Invalid tier: {tier}. Use 'cheap_api' or 'premium_api'")
        key = data.get("api_key", "").strip()
        if not key:
            raise HTTPException(400, "api_key required")

        _provider_keys[tier][provider_name] = key
        masked = key[:4] + "..." + key[-4:] if len(key) > 8 else "****"
        logger.info(f"Provider key set: {tier}/{provider_name} ({masked})")
        return {"ok": True, "provider": provider_name, "tier": tier, "masked": masked}

    @api.delete("/providers/{tier}/{provider_name}")
    def remove_provider_key(tier: str, provider_name: str):
        """Remove an inference provider's API key."""
        if tier in _provider_keys:
            _provider_keys[tier].pop(provider_name, None)
        return {"ok": True}

    # ── 6. Budget Control ────────────────────────────────────────────

    @api.get("/budget")
    def get_budget():
        """Get current token budget status."""
        if not token_budget:
            return {"configured": False}
        snap = token_budget.snapshot()
        return {
            "configured": True,
            **snap.__dict__,
            "suggested_tier": token_budget.suggest_model_tier(),
        }

    @api.post("/budget")
    def update_budget(data: dict):
        """Update budget limits."""
        if not token_budget:
            raise HTTPException(400, "Token budget not configured")
        if "hourly_limit" in data:
            token_budget.hourly_limit = int(data["hourly_limit"])
        if "daily_limit" in data:
            token_budget.daily_limit = int(data["daily_limit"])
        return {"ok": True, "snapshot": token_budget.snapshot().__dict__}

    # ── 7. Shadow Diffs (Training Data) ──────────────────────────────

    @api.get("/shadow-diffs")
    def get_shadow_diffs():
        """Export shadow mode comparison data for analysis."""
        if not entrypoint:
            return {"diffs": []}
        return {"diffs": entrypoint.get_shadow_diffs()}

    # ── 8. Classifier Health (Fine-tuning readiness) ─────────────────

    @api.get("/classifier/health")
    def get_classifier_health():
        """Check if the classifier has enough data for fine-tuning."""
        if not metrics_collector:
            return {"ready": False}
        dashboard = metrics_collector.get_dashboard_metrics()
        return dashboard.get("classifier_health", {})

    @api.get("/classifier/training-data")
    def get_training_candidates():
        """Export misclassified events for fine-tuning dataset."""
        if not metrics_collector:
            return {"candidates": []}
        return {"candidates": metrics_collector.export_training_candidates()}

    return api


# ── Dashboard HTML for Token Economy Panel ───────────────────────────────

ROUTER_DASHBOARD_CARD_HTML = """
<!-- Token Economy Card -->
<div class="card" style="grid-column:span 2">
  <h2><span>💰</span> Token Economy</h2>
  <div class="metrics-grid" style="grid-template-columns:repeat(5,1fr)">
    <div class="metric"><div class="num" id="rCost">$0.00</div><div class="unit">Total Cost</div></div>
    <div class="metric"><div class="num" id="rSaved" style="background:linear-gradient(135deg,#22c55e,#4ade80);-webkit-background-clip:text;-webkit-text-fill-color:transparent">$0.00</div><div class="unit">Saved</div></div>
    <div class="metric"><div class="num" id="rLocal">0%</div><div class="unit">Local Rate</div></div>
    <div class="metric"><div class="num" id="rCache">0%</div><div class="unit">Cache Hits</div></div>
    <div class="metric"><div class="num" id="rMode">—</div><div class="unit">Router Mode</div></div>
  </div>
  <div style="display:grid;grid-template-columns:1fr 1fr;gap:1rem;margin-top:1rem">
    <div>
      <div class="lbl" style="color:#64748b;font-size:.65rem;margin-bottom:.5rem">TIER DISTRIBUTION</div>
      <div id="rTiers" style="font-size:.75rem"></div>
    </div>
    <div>
      <div class="lbl" style="color:#64748b;font-size:.65rem;margin-bottom:.5rem">CLASSIFIER HEALTH</div>
      <div id="rClassifier" style="font-size:.75rem"></div>
    </div>
  </div>
</div>
<!-- Local Models Card -->
<div class="card">
  <h2><span>🤖</span> Local Models</h2>
  <div id="rModels" style="font-size:.75rem"></div>
</div>
<!-- Inference Providers Card -->
<div class="card">
  <h2><span>⚡</span> Inference Providers</h2>
  <div id="rProviders" style="font-size:.75rem"></div>
  <div class="add-key" style="margin-top:.5rem">
    <select id="provTier" style="padding:6px;background:rgba(0,0,0,.3);border:1px solid rgba(255,255,255,.1);border-radius:6px;color:#fff;font-size:.7rem">
      <option value="cheap_api">💸 Económica</option>
      <option value="premium_api">💎 Premium</option>
    </select>
    <input id="provName" placeholder="groq, deepseek..." style="flex:1;padding:6px 10px;background:rgba(0,0,0,.3);border:1px solid rgba(255,255,255,.1);border-radius:6px;color:#fff;font-size:.75rem">
    <input id="provKey" type="password" placeholder="API key" style="flex:2;padding:6px 10px;background:rgba(0,0,0,.3);border:1px solid rgba(255,255,255,.1);border-radius:6px;color:#fff;font-size:.75rem">
    <button class="btn btn-purple" onclick="addProvider()">Add</button>
  </div>
</div>
"""

ROUTER_DASHBOARD_JS = """
// Token Economy refresh
async function refreshRouter() {
  if(!T()) return;
  try {
    // Metrics
    const mr = await fetch('/api/router/metrics', {headers: headers()});
    if(mr.ok) {
      const m = await mr.json();
      if(m.cost_overview) {
        document.getElementById('rCost').textContent = '$' + m.cost_overview.total_cost_usd.toFixed(4);
        document.getElementById('rSaved').textContent = '$' + m.cost_overview.savings_usd.toFixed(4);
      }
      if(m.tier_distribution) {
        document.getElementById('rLocal').textContent = m.tier_distribution.local_resolution_rate + '%';
        document.getElementById('rCache').textContent = m.tier_distribution.cache_hit_rate + '%';
        let th = '';
        for(const [tier, pct] of Object.entries(m.tier_distribution.by_tier_percent || {})) {
          const col = tier.startsWith('local') ? '#4ade80' : tier === 'cheap_api' ? '#fbbf24' : '#f87171';
          th += `<div class="field"><div class="lbl">${tier}</div><div class="meter"><div class="fill" style="width:${pct}%;background:${col}"></div></div></div>`;
        }
        document.getElementById('rTiers').innerHTML = th;
      }
      if(m.classifier_health) {
        const ch = m.classifier_health;
        const col = ch.ready_for_finetuning ? '#4ade80' : '#fbbf24';
        document.getElementById('rClassifier').innerHTML =
          `<div class="field"><div class="lbl">Misclassification</div><div class="val" style="color:${col}">${ch.misclassification_rate}%</div></div>` +
          `<div class="field" style="margin-top:.3rem"><div class="val" style="color:#94a3b8;font-size:.7rem">${ch.recommendation}</div></div>`;
      }
    }
    // Router status
    const sr = await fetch('/api/router/status', {headers: headers()});
    if(sr.ok) {
      const s = await sr.json();
      document.getElementById('rMode').textContent = s.mode || '—';
    }
    // Local models
    const lr = await fetch('/api/router/models/local', {headers: headers()});
    if(lr.ok) {
      const l = await lr.json();
      let mh = `<div class="field"><div class="lbl">Tier: ${l.tier || '?'} | RAM: ${l.available_ram_gb || '?'}GB</div></div>`;
      (l.models || []).forEach(m => {
        const icon = m.is_classifier ? '🧠' : '🤖';
        mh += `<div class="key-row"><span class="nm">${icon} ${m.name}</span><span class="mk">${m.ram_q4_gb}GB → ${m.tier}</span></div>`;
      });
      document.getElementById('rModels').innerHTML = mh;
    }
    // Providers
    const pr = await fetch('/api/router/providers', {headers: headers()});
    if(pr.ok) {
      const p = await pr.json();
      let ph = '';
      for(const [tier, providers] of Object.entries(p)) {
        const label = tier === 'cheap_api' ? '💸 Económica' : '💎 Premium';
        ph += `<div class="lbl" style="margin-top:.3rem">${label}</div>`;
        for(const [name, masked] of Object.entries(providers)) {
          ph += `<div class="key-row"><span class="nm">${name}</span><span class="mk">${masked}</span>
            <button class="btn btn-red btn-sm" onclick="delProvider('${tier}','${name}')">✕</button></div>`;
        }
        if(!Object.keys(providers).length) ph += '<div style="color:#64748b;font-size:.7rem">No keys configured</div>';
      }
      document.getElementById('rProviders').innerHTML = ph;
    }
  } catch(e) { console.error('Router refresh:', e); }
}

async function addProvider() {
  const tier = document.getElementById('provTier').value;
  const name = document.getElementById('provName').value.trim();
  const key = document.getElementById('provKey').value.trim();
  if(!name||!key||!T()) return;
  await fetch(`/api/router/providers/${tier}/${name}`, {method:'POST', headers:headers(), body:JSON.stringify({api_key:key})});
  document.getElementById('provName').value = '';
  document.getElementById('provKey').value = '';
  refreshRouter();
}

async function delProvider(tier, name) {
  await fetch(`/api/router/providers/${tier}/${name}`, {method:'DELETE', headers:headers()});
  refreshRouter();
}

setInterval(refreshRouter, 3000);
setTimeout(refreshRouter, 800);
"""
