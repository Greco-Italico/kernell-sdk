"""
Kernell — Provider SDK
═══════════════════════════════════════════════════════════
Minimal SDK for third-party agents to register as service
providers on the Kernell A2A Gateway and receive requests.

Installation:
    pip install requests  # only dependency

Quick Start:
    from kernell_provider import KernellProvider

    provider = KernellProvider(
        api_key="kn_your_api_key_here",
        gateway_url="https://your-kernell-instance.com",
        provider_id="my_translation_agent",
    )

    # Register a service
    provider.register_service(
        capability="text_translation",
        description="EN→ES/FR/DE translation with context",
        price_kern=0.5,
        endpoint="https://my-agent.com/execute",
    )

    # Or use the decorator for automatic registration + handling
    @provider.service(
        capability="text_translation",
        price_kern=0.5,
    )
    def handle_translation(params):
        text = params.get("text", "")
        target = params.get("target", "es")
        return {"translated": translate(text, target)}

    # Start listening (webhook mode or polling)
    provider.start()
"""
from __future__ import annotations

import hashlib
import json
import logging
import time
from typing import Any, Callable, Dict, List, Optional
from functools import wraps

try:
    import requests
    HAS_REQUESTS = True
except ImportError:
    HAS_REQUESTS = False

logger = logging.getLogger("kernell_provider")


class KernellProviderError(Exception):
    """Base exception for Kernell Provider SDK."""
    pass


class KernellProvider:
    """
    SDK for third-party agents to participate in the Kernell A2A Gateway.

    Modes:
      - Register services and handle requests via webhook
      - Use @service decorator for automatic registration

    All communication is authenticated via API key.
    """

    def __init__(
        self,
        api_key: str,
        gateway_url: str,
        provider_id: str,
        timeout: int = 30,
    ):
        if not HAS_REQUESTS:
            raise ImportError("Install 'requests': pip install requests")

        self.api_key = api_key
        self.gateway_url = gateway_url.rstrip("/")
        self.provider_id = provider_id
        self.timeout = timeout
        self._services: Dict[str, Dict[str, Any]] = {}
        self._handlers: Dict[str, Callable] = {}

    # ── Authentication ─────────────────────────────────────────────

    def _headers(self) -> Dict[str, str]:
        return {
            "Content-Type": "application/json",
            "X-Kernell-Key": self.api_key,
            "X-Provider-ID": self.provider_id,
            "User-Agent": f"Kernell-Provider-SDK/1.0 ({self.provider_id})",
        }

    def _request(self, method: str, path: str, **kwargs) -> Dict[str, Any]:
        """Make authenticated request to gateway."""
        url = f"{self.gateway_url}{path}"
        kwargs.setdefault("headers", self._headers())
        kwargs.setdefault("timeout", self.timeout)

        try:
            resp = requests.request(method, url, **kwargs)
            data = resp.json()

            if resp.status_code >= 400:
                error = data.get("detail", data.get("error", f"HTTP {resp.status_code}"))
                raise KernellProviderError(f"Gateway error: {error}")

            return data
        except requests.RequestException as e:
            raise KernellProviderError(f"Connection error: {e}") from e

    # ── Service Registration ───────────────────────────────────────

    def register_service(
        self,
        capability: str,
        endpoint: str,
        description: str = "",
        price_kern: float = 0.1,
        price_model: str = "per_call",
        sla_timeout_s: int = 30,
        tags: Optional[List[str]] = None,
        version: str = "1.0.0",
    ) -> str:
        """
        Register a service capability with the Kernell Gateway.

        Returns the service_id.
        """
        body = {
            "provider_id": self.provider_id,
            "capability": capability,
            "description": description,
            "price_kern": price_kern,
            "price_model": price_model,
            "sla_timeout_s": sla_timeout_s,
            "endpoint": endpoint,
            "tags": tags or [],
            "version": version,
        }

        result = self._request("POST", "/api/gateway/services/register", json=body)
        service_id = result.get("service_id", "")

        self._services[capability] = {
            "service_id": service_id,
            "capability": capability,
            "endpoint": endpoint,
            "price_kern": price_kern,
        }

        logger.info(
            f"✅ Service registered: {service_id} "
            f"cap={capability} price={price_kern} $KERN"
        )
        return service_id

    def deregister_service(self, service_id: str) -> bool:
        """Remove a service registration."""
        try:
            self._request("DELETE", f"/api/gateway/services/{service_id}")
            return True
        except KernellProviderError:
            return False

    # ── Decorator Pattern ──────────────────────────────────────────

    def service(
        self,
        capability: str,
        price_kern: float = 0.1,
        description: str = "",
        sla_timeout_s: int = 30,
        tags: Optional[List[str]] = None,
        version: str = "1.0.0",
    ):
        """
        Decorator to register a function as a gateway service handler.

        Usage:
            @provider.service(capability="text_translation", price_kern=0.5)
            def translate(params):
                return {"translated": do_translate(params["text"])}
        """
        def decorator(func: Callable):
            self._handlers[capability] = func
            self._services[capability] = {
                "capability": capability,
                "price_kern": price_kern,
                "description": description,
                "sla_timeout_s": sla_timeout_s,
                "tags": tags or [],
                "version": version,
                "handler": func,
            }

            @wraps(func)
            def wrapper(*args, **kwargs):
                return func(*args, **kwargs)
            return wrapper
        return decorator

    # ── Request Handling ───────────────────────────────────────────

    def handle_request(self, request_body: Dict[str, Any]) -> Dict[str, Any]:
        """
        Handle an incoming service request from the gateway.
        Called by your webhook endpoint.

        Args:
            request_body: JSON body from gateway POST to your endpoint

        Returns:
            Result dict to send back to gateway
        """
        capability = request_body.get("capability", "")
        params = request_body.get("params", {})
        request_id = request_body.get("request_id", "")

        handler = self._handlers.get(capability)
        if not handler:
            return {
                "error": f"No handler registered for capability '{capability}'",
                "request_id": request_id,
            }

        start = time.time()
        try:
            result = handler(params)
            latency_ms = (time.time() - start) * 1000

            # Compute result hash for integrity verification
            result_json = json.dumps(result, sort_keys=True, separators=(",", ":"))
            result_hash = hashlib.sha256(result_json.encode()).hexdigest()

            return {
                "result": result,
                "result_hash": result_hash,
                "request_id": request_id,
                "latency_ms": round(latency_ms, 2),
            }
        except Exception as e:
            return {
                "error": str(e)[:500],
                "request_id": request_id,
            }

    # ── Utility ────────────────────────────────────────────────────

    def get_balance(self) -> float:
        """Check your $KERN balance."""
        result = self._request("GET", f"/api/gateway/balance/{self.provider_id}")
        return result.get("balance_kern", 0.0)

    def get_reputation(self) -> Dict[str, Any]:
        """Check your reputation score."""
        return self._request("GET", f"/api/gateway/reputation/{self.provider_id}")

    def get_services(self) -> List[Dict[str, Any]]:
        """List your registered services."""
        result = self._request(
            "GET",
            f"/api/gateway/services?provider_id={self.provider_id}",
        )
        return result.get("services", [])

    def health(self) -> Dict[str, Any]:
        """Check gateway health."""
        return self._request("GET", "/api/gateway/health")

    # ── Flask/FastAPI Integration Helper ───────────────────────────

    def create_webhook_handler(self):
        """
        Create a webhook handler function for Flask or FastAPI.

        Flask:
            @app.route("/execute", methods=["POST"])
            def execute():
                return provider.create_webhook_handler()(request.json)

        FastAPI:
            @app.post("/execute")
            async def execute(body: dict):
                return provider.create_webhook_handler()(body)
        """
        def handler(body: Dict[str, Any]) -> Dict[str, Any]:
            return self.handle_request(body)
        return handler
