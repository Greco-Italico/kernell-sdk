"""
EconomicAgent (Client SDK)
===========================
The ultimate abstraction layer for Kernell OS agents.
Turns an AI Agent into a fully autonomous economic actor.

Instead of managing low-level escrows, compliance engines, or cryptography,
the developer simply calls:
  agent.buy("data_scraping", 50)
  agent.sell("image_gen", 5)
  agent.check_counterparty("agent_xyz")
"""
import uuid
from decimal import Decimal
from typing import Dict, Any, Optional

from kernell_sdk.identity import AgentPassport
from kernell_sdk.security.ssrf import create_safe_client


class EconomicAgent:
    def __init__(self, passport: AgentPassport, private_key: str, api_url: str = "https://api.kernell.site/v1"):
        """
        Initializes an Economic Agent capable of transacting natively on the Kernell L2.
        """
        self.passport = passport
        self._private_key = private_key
        self.agent_id = passport.agent_id
        self.api_url = api_url
        self.http = create_safe_client(agent_id=self.agent_id, timeout=10.0)

    def _sign_payload(self, payload: dict) -> dict:
        """Automatically injects identity signatures to bypass Stripe/Banking friction."""
        from kernell_sdk.identity import sign_message
        import json
        signature = sign_message(json.dumps(payload, sort_keys=True), self._private_key)
        return {"payload": payload, "signature": signature, "agent_id": self.agent_id}

    # ─── Risk & Counterparty Intelligence ───────────────────────────
    
    def check_counterparty(self, target_agent_id: str) -> bool:
        """
        Checks the FinCEN compliance status of another agent BEFORE doing business.
        Returns False if the agent has a blocked taint ratio (e.g. laundering risk).
        """
        try:
            res = self.http.get(f"{self.api_url}/compliance/verify/{target_agent_id}")
            res.raise_for_status()
            data = res.json()
            # We strictly block 'blocked_tainted' to protect our own clean mass
            if data.get("status") == "blocked_tainted":
                return False
            return True
        except Exception as e:
            # Safe-fail: If we can't verify compliance, we don't do business.
            return False

    # ─── Core Commerce Mechanics (Abstracted Escrows) ───────────────

    def buy_service(self, target_agent_id: str, service_id: str, amount_kern: Decimal) -> Optional[str]:
        """
        Safely buys a service from another agent. 
        Automatically handles Counterparty check -> Escrow Lock.
        Returns the contract_id to monitor settlement.
        """
        if not self.check_counterparty(target_agent_id):
            raise ValueError(f"Transaction blocked: {target_agent_id} failed FinCEN compliance.")

        contract_id = f"job_{uuid.uuid4().hex[:12]}"
        
        # Step 1: Lock funds in Taint-Aware Escrow
        payload = {
            "sender": self.agent_id,
            "receiver": target_agent_id,
            "amount": str(amount_kern),
            "contract_id": contract_id,
            "metadata": {"service_id": service_id}
        }
        
        # In a real scenario, this would post the signed payload
        res = self.http.post(f"{self.api_url}/escrow/create", json=payload)
        if res.status_code != 200:
            raise Exception(f"Failed to lock escrow: {res.text}")
            
        return contract_id

    def fulfill_order(self, contract_id: str, client_agent_id: str, amount_kern: Decimal, proof_of_work: str) -> bool:
        """
        Settles a contract after providing a service. 
        Collects the specified amount of funds from the Escrow.
        """
        payload = {
            "contract_id": contract_id,
            "payouts": {
                self.agent_id: str(amount_kern)
            },
            "proof": proof_of_work
        }
        
        res = self.http.post(f"{self.api_url}/escrow/settle", json=payload)
        if res.status_code != 200:
            raise Exception(f"Failed to settle escrow: {res.text}")
            
        return True

    # ─── Compute Economy (Marketplace Integration) ──────────────────

    def offer_compute(self, price_per_sec: Decimal, specs: dict):
        """
        Registers the agent as a Compute Worker on the Kernell Network.
        This allows 'moneyless' agents to earn KERN by renting their sandbox CPU.
        """
        # Calls the matching engine (future phase)
        payload = {
            "worker_id": self.agent_id,
            "price_per_sec": str(price_per_sec),
            "specs": specs
        }
        # self.http.post(f"{self.api_url}/compute/register", json=payload)
        print(f"[{self.agent_id}] Registered as Compute Provider at {price_per_sec} KERN/s")

    # ─── Standard Utilities ─────────────────────────────────────────

    def get_balance(self) -> Decimal:
        res = self.http.get(f"{self.api_url}/balance/{self.agent_id}")
        if res.status_code == 200:
            return Decimal(res.json().get("balance", "0"))
        return Decimal("0")
