"""
log_redactor.py — Kernell OS SDK
Fix #7: Procesador de structlog que redacta automáticamente secretos en logs.

Integración:
    import structlog
    from kernell_sdk.log_redactor import build_redacting_processor, configure_logging

    configure_logging()   # una sola llamada en el arranque de la app

El redactor:
  - Inspecciona recursivamente dicts y strings en el evento de log.
  - Reemplaza valores que parecen secretos con "***REDACTED***".
  - Trunca payloads de código (>500 chars) para evitar fugas por logs de debug.
  - Nunca lanza excepciones — si algo falla, registra un warning y sigue.
"""

from __future__ import annotations

import re
import os
import structlog
from typing import Any

# ---------------------------------------------------------------------------
# Configuración
# ---------------------------------------------------------------------------

# En dev, se puede deshabilitar el truncado para facilitar debugging.
# En producción siempre está activo.
_ENV = os.environ.get("KERNELL_ENV", "production").lower()
_IS_DEV = _ENV in ("dev", "development")

# Límite de truncado de payloads de código en logs.
# En dev: sin límite (None). En producción: 500 chars.
_MAX_CODE_LOG_CHARS: int | None = None if _IS_DEV else 500

# ---------------------------------------------------------------------------
# Patrones de detección de secretos
# ---------------------------------------------------------------------------

# Claves de dict cuyo VALOR debe ser redactado (case-insensitive)
_SECRET_KEY_PATTERNS: tuple[re.Pattern, ...] = tuple(
    re.compile(pattern, re.IGNORECASE)
    for pattern in [
        r"api[_\-]?key",
        r"api[_\-]?secret",
        r"secret[_\-]?key",
        r"access[_\-]?token",
        r"bearer[_\-]?token",
        r"auth[_\-]?token",
        r"jwt",
        r"password",
        r"passphrase",
        r"passwd",
        r"private[_\-]?key",
        r"priv[_\-]?key",
        r"seed[_\-]?phrase",
        r"mnemonic",
        r"wallet[_\-]?key",
        r"hmac[_\-]?secret",
        r"signing[_\-]?key",
        r"encryption[_\-]?key",
        r"crypto[_\-]?key",
        r"database[_\-]?url",
        r"db[_\-]?password",
        r"redis[_\-]?password",
    ]
)

# Patrones en el CONTENIDO de strings que indican un secreto (valor directo)
_SECRET_VALUE_PATTERNS: tuple[re.Pattern, ...] = tuple(
    re.compile(pattern, re.IGNORECASE)
    for pattern in [
        r"^sk[-_][a-zA-Z0-9\-_]{20,}$",          # OpenAI/Anthropic API keys
        r"^Bearer\s+[A-Za-z0-9\-_\.=]+$",         # Bearer tokens
        r"^eyJ[A-Za-z0-9\-_]+\.[A-Za-z0-9\-_]+", # JWT
        r"^[0-9a-fA-F]{64}$",                      # Hexstrings de 64 chars (claves)
        r"^[0-9a-fA-F]{128}$",                     # Hexstrings de 128 chars
        r"-----BEGIN (EC |RSA |OPENSSH )?PRIVATE KEY-----",  # PEM keys
    ]
)

_REDACTED = "***REDACTED***"

# ---------------------------------------------------------------------------
# Procesador principal
# ---------------------------------------------------------------------------

def redacting_processor(
    logger: Any,
    method: str,
    event_dict: dict[str, Any],
) -> dict[str, Any]:
    """
    Procesador de structlog. Redacta secretos en todos los campos del evento.

    Registrar en la chain de structlog:
        structlog.configure(processors=[..., redacting_processor, ...])
    """
    try:
        _redact_dict(event_dict)
    except Exception as exc:  # noqa: BLE001
        # Nunca fallar el logging por culpa del redactor
        event_dict["_redactor_error"] = str(exc)
    return event_dict


def _redact_dict(d: dict[str, Any]) -> None:
    """Redacta in-place un dict de structlog."""
    for key, value in list(d.items()):
        if isinstance(key, str) and _is_secret_key(key):
            d[key] = _REDACTED
        elif isinstance(value, dict):
            _redact_dict(value)
        elif isinstance(value, list):
            d[key] = [_redact_value(v) for v in value]
        elif isinstance(value, str):
            d[key] = _redact_string(key, value)


def _redact_value(value: Any) -> Any:
    if isinstance(value, dict):
        _redact_dict(value)
        return value
    if isinstance(value, str):
        return _redact_string("", value)
    return value


def _redact_string(key: str, value: str) -> str:
    # Truncar payloads de código (solo en producción; en dev se preservan para debugging)
    if (
        _MAX_CODE_LOG_CHARS is not None
        and key in ("source", "code", "payload", "script")
        and len(value) > _MAX_CODE_LOG_CHARS
    ):
        return (
            value[:_MAX_CODE_LOG_CHARS]
            + f"…[+{len(value) - _MAX_CODE_LOG_CHARS} chars — "
            + "set KERNELL_ENV=dev para ver completo]"
        )

    # Detectar valores que parecen secretos directamente
    for pattern in _SECRET_VALUE_PATTERNS:
        if pattern.search(value):
            return _REDACTED

    return value


def _is_secret_key(key: str) -> bool:
    return any(p.search(key) for p in _SECRET_KEY_PATTERNS)


# ---------------------------------------------------------------------------
# Configuración de structlog recomendada
# ---------------------------------------------------------------------------

def configure_logging(level: str = "INFO", as_json: bool = True) -> None:
    """
    Configura structlog con el redactor integrado y formato recomendado.

    Llamar UNA VEZ al arranque de la aplicación, antes de cualquier log.

    Args:
        level:    nivel mínimo de log ("DEBUG", "INFO", "WARNING", "ERROR").
        as_json:  True → JSON renderer (producción); False → console (dev).
    """
    import logging

    shared_processors: list[Any] = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_logger_name,
        structlog.stdlib.add_log_level,
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.processors.StackInfoRenderer(),
        redacting_processor,                     # ← redactor aquí
    ]

    if as_json:
        renderer = structlog.processors.JSONRenderer()
    else:
        renderer = structlog.dev.ConsoleRenderer()

    structlog.configure(
        processors=shared_processors + [renderer],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=True,
    )

    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(message)s",
    )


# ---------------------------------------------------------------------------
# Utilidad: logger pre-configurado
# ---------------------------------------------------------------------------

def get_logger(name: str) -> structlog.stdlib.BoundLogger:
    """
    Retorna un logger structlog con el redactor activo.

    Uso:
        from kernell_sdk.log_redactor import get_logger
        log = get_logger(__name__)
        log.info("wallet_loaded", wallet_id="abc123", api_key="sk-xxxx")
        # → api_key aparece como "***REDACTED***" en el output
    """
    return structlog.get_logger(name)
