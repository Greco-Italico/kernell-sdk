"""
Kernell OS SDK — Wallet & M2M Commerce
═══════════════════════════════════════
Handles $KERN payments between agents via the Kernell Agent Protocol (KAP).
Agents can receive payments for completed tasks and pay other agents for services.

Usage:
    wallet = Wallet(config=my_config)

    balance = wallet.get_balance()
    escrow_id = wallet.request_payment_escrow(amount=50.0, task_id="t1", payer_id="agent_2")
    wallet.release_escrow(escrow_id)
"""
import re
from decimal import Decimal, InvalidOperation, ROUND_DOWN
from typing import Optional, Union

import structlog
from kernell_sdk.security.ssrf import create_safe_client, RequestError, HTTPStatusError
from kernell_sdk.security.rate_limiter import RateLimitGovernor, RateLimitExceeded
from pydantic import BaseModel, Field, field_validator
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type

from .config import default_config, KernellConfig

logger = structlog.get_logger("kernell.wallet")

# Default timeout for all HTTP operations (seconds)
REQUEST_TIMEOUT = 10.0

# Patrón para direcciones de wallet válidas (alfanumérico + guiones/underscore)
_WALLET_ADDR_RE = re.compile(r'^[a-zA-Z0-9_\-]{8,128}$')


class EscrowRequest(BaseModel):
    amount: float = Field(..., gt=0.0, description="Amount must be strictly positive")
    task_id: str = Field(..., min_length=1, max_length=128)
    payer: str = Field(..., min_length=1, max_length=128)
    payee: str
    ttl: int = Field(default=3600, ge=60, le=86400 * 30)

    @field_validator("payee")
    def validate_payee(cls, v):
        if not _WALLET_ADDR_RE.match(str(v)):
            raise ValueError("Invalid payee wallet address format")
        return v


def _to_decimal(value: Union[float, int, str, Decimal]) -> Decimal:
    """Coerce numeric input to Decimal. Rejects NaN/Inf."""
    if isinstance(value, Decimal):
        d = value
    elif isinstance(value, float):
        # Use str() to avoid float-precision artefacts: Decimal(0.1) ≠ Decimal('0.1')
        d = Decimal(str(value))
    else:
        d = Decimal(value)
    if not d.is_finite():
        raise ValueError(f"Non-finite value rejected: {value!r}")
    return d


class Wallet:
    """
    Handles M2M commerce via the Kernell Agent Protocol (KAP).

    Each agent has a volatile $KERN wallet for internal transactions.
    """

    def __init__(self, config: Optional[KernellConfig] = None):
        import threading
        self._governor = RateLimitGovernor()
        self.config = config or default_config
        self._client = create_safe_client(
            agent_id="wallet_service",
            base_url=self.config.gateway_url,
            headers={"Authorization": f"Bearer {self.config.api_key}"},
            timeout=REQUEST_TIMEOUT,
        )
        self._balance_lock = threading.Lock()
        self._local_balance: Decimal = Decimal("0")

    @property
    def balance(self) -> Decimal:
        """Current volatile balance (Decimal, thread-safe read)."""
        with self._balance_lock:
            return self._local_balance

    def credit(self, amount: Union[float, int, str, Decimal]) -> Decimal:
        """Add funds. Returns new balance as Decimal.

        Accepts float for backward compatibility but stores as Decimal internally
        to prevent rounding errors in financial arithmetic (C-11 fix).
        """
        d = _to_decimal(amount)
        if d <= 0:
            raise ValueError("credit amount must be > 0")
        with self._balance_lock:
            self._local_balance += d
            return self._local_balance

    def debit(self, amount: Union[float, int, str, Decimal]) -> bool:
        """Deduct funds. Returns True if sufficient balance, False otherwise."""
        d = _to_decimal(amount)
        if d <= 0:
            raise ValueError("debit amount must be > 0")
        with self._balance_lock:
            if self._local_balance < d:
                return False
            self._local_balance -= d
            return True

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=2, max=10),
        retry=retry_if_exception_type((RequestError, HTTPStatusError)),
        reraise=True
    )
    def _get_balance_with_retry(self, endpoint: str) -> float:
        response = self._client.get(endpoint)
        response.raise_for_status()
        return float(response.json().get("balance", 0.0))

    def get_balance(self) -> float:
        """Fetch the current $KERN balance from the gateway.

        Returns 0.0 if no wallet is configured or the gateway is unreachable.
        """
        addr = self.config.wallet_address

        # Validar formato antes de usar en URL
        if not addr or not _WALLET_ADDR_RE.match(str(addr)):
            logger.debug("invalid_wallet_address_or_missing", addr=addr)
            return 0.0

        try:
            endpoint = f"/api/v1/wallet/{addr}/balance"
            return self._get_balance_with_retry(endpoint)
        except HTTPStatusError as error:
            logger.warning("balance_check_failed_http", status=error.response.status_code)
            return 0.0
        except (RequestError, ValueError) as error:
            logger.warning("balance_check_failed_network", error=str(error))
            return 0.0

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=2, max=10),
        retry=retry_if_exception_type(RequestError),
        reraise=True
    )
    def _post_escrow_with_retry(self, payload: dict):
        response = self._client.post("/api/v1/escrow/create", json=payload)
        response.raise_for_status()
        return response

    def request_payment_escrow(
        self,
        amount: float,
        task_id: str,
        payer_id: str,
        ttl_seconds: int = 3600,
    ) -> str:
        """Request funds to be held in escrow for a specific task.

        Args:
            amount: Number of $KERN tokens to escrow.
            task_id: Unique identifier for the task being paid for.
            payer_id: Agent ID of the entity funding the escrow.
            ttl_seconds: Time-to-live for the escrow in seconds (default: 3600).

        Returns:
            The escrow ID string.

        Raises:
            HTTPStatusError: If the gateway rejects the request.
            ValueError: If input validation fails.
        """
        # Rate limit escrow creation
        self._governor.check_escrow_create(self.config.wallet_address or "unknown")

        # Validate using Pydantic
        req = EscrowRequest(
            amount=amount,
            task_id=task_id,
            payer=payer_id,
            payee=self.config.wallet_address,
            ttl=ttl_seconds
        )

        response = self._post_escrow_with_retry(req.model_dump())
        escrow_id = response.json().get("escrow_id", "")
        
        logger.info("escrow_created", escrow_id=escrow_id, amount=amount, task_id=task_id, payer=payer_id)
        return escrow_id

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=2, max=10),
        retry=retry_if_exception_type(RequestError),
        reraise=True
    )
    def _release_escrow_with_retry(self, escrow_id: str):
        return self._client.post(f"/api/v1/escrow/{escrow_id}/release")

    def release_escrow(self, escrow_id: str) -> bool:
        """Release escrowed funds upon task completion.

        Args:
            escrow_id: The escrow ID returned by request_payment_escrow().

        Returns:
            True if the release was successful, False otherwise.
        """
        try:
            response = self._release_escrow_with_retry(escrow_id)
            is_success = response.status_code == 200
            if is_success:
                logger.info("escrow_released", escrow_id=escrow_id)
            else:
                logger.warning("escrow_release_failed_http", escrow_id=escrow_id, status=response.status_code)
            return is_success
        except RequestError as error:
            logger.error("escrow_release_failed_network", escrow_id=escrow_id, error=str(error))
            return False

    def close(self):
        """Close the HTTP client and release connections."""
        self._client.close()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.close()
        return False
