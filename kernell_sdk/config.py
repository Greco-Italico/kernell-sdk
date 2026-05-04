"""
Kernell OS SDK — Configuration
════════════════════════════════
SECURITY:
  - Sensitive fields (api_key, wallet_private_key) are loaded from
    environment variables and NOT serialized by default.
  - wallet_private_key is excluded from JSON/dict serialization.
"""
import os
from typing import Optional
from pydantic import BaseModel, ConfigDict, Field, field_validator


class KernellConfig(BaseModel):
    """Configuration for the Kernell OS SDK."""

    model_config = ConfigDict(env_prefix="KERNELL_")

    api_key: str = Field(default_factory=lambda: os.getenv("KERNELL_API_KEY", ""))
    gateway_url: str = Field(default_factory=lambda: os.getenv("KERNELL_GATEWAY_URL", "https://api.kernell.site"))
    
    @field_validator("gateway_url")
    @classmethod
    def validate_gateway_url(cls, v: str) -> str:
        from urllib.parse import urlparse
        import os
        parsed = urlparse(v)
        
        # Producción: SOLO el dominio oficial
        if os.getenv("KERNELL_ENV", "development") == "production":
            if parsed.hostname != "api.kernell.site":
                raise ValueError("Production only allows api.kernell.site")
            if parsed.scheme != "https":
                raise ValueError("Production requires HTTPS")
        else:
            # Desarrollo: localhost permitido pero documentado
            allowed_hosts = ["api.kernell.site", "localhost", "127.0.0.1"]
            if parsed.hostname not in allowed_hosts:
                raise ValueError(f"SSRF: hostname '{parsed.hostname}' no permitido")
        return v
        
    redis_url: Optional[str] = Field(default_factory=lambda: os.getenv("KERNELL_REDIS_URL", None))
    environment: str = Field(default_factory=lambda: os.getenv("KERNELL_ENV", "development"))

    # Multi-tenant binding for signed channels (A2A, audit); default is single-tenant alpha.
    tenant_id: str = Field(default_factory=lambda: os.getenv("KERNELL_TENANT_ID", "default"))

    # Wallet / Escrow configuration
    wallet_address: Optional[str] = Field(default_factory=lambda: os.getenv("KERNELL_WALLET_ADDRESS", None))
    # SECURITY: This field is EXCLUDED from serialization
    wallet_private_key: Optional[str] = Field(
        default_factory=lambda: os.getenv("KERNELL_WALLET_KEY", None),
        exclude=True,  # Never serialize this field
    )

    # LLM Defaults
    default_model: str = Field(default="claude-3-5-sonnet-20241022")
    fallback_model: str = Field(default="gpt-4o-mini")

    def model_dump(self, **kwargs):
        """Override to always exclude sensitive fields."""
        kwargs.setdefault("exclude", set())
        if isinstance(kwargs["exclude"], set):
            kwargs["exclude"].add("wallet_private_key")
            kwargs["exclude"].add("api_key")
        return super().model_dump(**kwargs)


# ── Lazy singleton ────────────────────────────────────────────────────────────
# Instantiating KernellConfig() at module import time means any env vars set
# AFTER the first import are silently ignored.  Use get_default_config() to
# always receive a config that reflects the current environment.
#
# For backwards compatibility, `default_config` remains as a module-level
# attribute but is now lazy: it is only created on first access.

import functools as _functools


@_functools.lru_cache(maxsize=1)
def get_default_config() -> "KernellConfig":
    """Return the process-wide default KernellConfig (lazy, cached).

    Call ``get_default_config.cache_clear()`` in tests to reset between runs.
    """
    return KernellConfig()


class _LazyConfig:
    """Proxy that behaves like KernellConfig but defers construction."""

    def __getattr__(self, name: str):
        return getattr(get_default_config(), name)

    def __repr__(self) -> str:
        return repr(get_default_config())


# Keep the name so existing `from kernell_sdk.config import default_config` works.
default_config: "KernellConfig" = _LazyConfig()  # type: ignore[assignment]
