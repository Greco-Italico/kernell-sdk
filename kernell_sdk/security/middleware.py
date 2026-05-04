import json
from starlette.types import ASGIApp, Receive, Scope, Send
from starlette.requests import Request
from starlette.responses import JSONResponse
import urllib.parse
from .iam import IAMPolicyEngine

class IAMSecurityMiddleware:
    def __init__(self, app: ASGIApp, iam_engine: IAMPolicyEngine, exempt_paths: list[str] = None,
                 spend_guard=None, cost_estimator=None):
        self.app = app
        self.iam = iam_engine
        self.exempt_paths = exempt_paths or ["/health", "/docs", "/openapi.json"]
        self.spend_guard = spend_guard  # Optional: SpendGuard instance
        self.cost_estimator = cost_estimator  # Optional: callable(method, path, body) -> int (micro)

    async def __call__(self, scope: Scope, receive: Receive, send: Send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
            
        request = Request(scope, receive)
        
        # Resolve clean path to prevent directory traversal bypasses
        raw_path = request.url.path
        clean_path = urllib.parse.unquote(urllib.parse.urlparse(raw_path).path)
        clean_path = urllib.parse.urljoin("/", clean_path) # Collapse /../
        
        # Check exemption list
        if any(clean_path.startswith(p) for p in self.exempt_paths):
            return await self.app(scope, receive, send)

        # 1. Extract headers
        tenant_id = request.headers.get("X-Tenant-Id")
        agent_id = request.headers.get("X-Agent-Id")
        signature = request.headers.get("X-Signature")
        timestamp_str = request.headers.get("X-Timestamp")
        key_version = request.headers.get("X-Key-Version") # Optional but recommended for O(1) matching

        if not tenant_id or not agent_id or not signature or not timestamp_str:
            response = JSONResponse(status_code=401, content={"detail": "Missing auth headers"})
            return await response(scope, receive, send)

        try:
            timestamp = int(timestamp_str)
        except ValueError:
            response = JSONResponse(status_code=401, content={"detail": "Invalid timestamp"})
            return await response(scope, receive, send)

        # We must read the body to verify the signature. 
        # In ASGI, reading the body consumes the stream, so we must buffer it and mock the receive function for the downstream app.
        body_bytes = await request.body()
        body_str = body_bytes.decode('utf-8')
        
        # Deterministic action resolution
        action = self._resolve_action(request.method, clean_path)

        try:
            # 2. Verify signature + replay + optionally O(1) key check
            self.iam.verify_request(
                tenant_id,
                agent_id, 
                signature, 
                timestamp, 
                request.method,
                clean_path,
                body_str, 
                action=action, 
                key_version=key_version
            )
            
            # 3. Inject verified tenant_id into request state for downstream handlers
            scope.setdefault("state", {})
            request.state.tenant_id = tenant_id
            
            # 4. Spend enforcement (if configured)
            if self.spend_guard and self.cost_estimator:
                estimated_cost = self.cost_estimator(request.method, clean_path, body_str)
                if estimated_cost > 0:
                    decision = self.spend_guard.check_and_deduct(tenant_id, estimated_cost)
                    if not decision.allowed:
                        response = JSONResponse(
                            status_code=402, 
                            content={"detail": f"Insufficient balance: {decision.reason}", "balance": decision.balance_after}
                        )
                        return await response(scope, receive, send)
                
        except Exception as e:
            # Catch Unauthorized or other exceptions
            status = getattr(e, 'status_code', 401)
            msg = str(e)
            if "Deny" in msg or "not authorized" in msg:
                status = 403
            response = JSONResponse(status_code=status, content={"detail": msg})
            return await response(scope, receive, send)

        # Re-inject the body for the downstream application
        async def receive_mock() -> dict:
            return {"type": "http.request", "body": body_bytes, "more_body": False}

        return await self.app(scope, receive_mock, send)

    def _resolve_action(self, method: str, path: str) -> str:
        # e.g., POST /escrow/release -> execute:escrow.release
        # GET /vault/secret -> read:vault.secret
        
        # Normalize path
        parts = [p for p in path.strip("/").split("/") if p]
        if not parts:
            return f"{method.lower()}:root"
            
        resource = ".".join(parts)
        
        action_verb = "execute"
        if method == "GET":
            action_verb = "read"
        elif method == "POST":
            action_verb = "execute"
        elif method == "PUT" or method == "PATCH":
            action_verb = "write"
        elif method == "DELETE":
            action_verb = "delete"
            
        return f"{action_verb}:{resource}"
