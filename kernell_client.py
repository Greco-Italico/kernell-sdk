import requests
import webbrowser
import time
import sys

class KernellClient:
    """
    Official SDK for Kernell Core.
    Handles agent execution requests and automatically manages 402 Payment Required states
    to ensure operational continuity with minimal friction.
    """
    def __init__(self, api_key: str, base_url: str = "http://localhost:8000"):
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")

    def execute(self, task_type: str = "simple", auto_retry: bool = True, max_retries: int = 3):
        """
        Execute a task through the Kernell secure execution layer.
        """
        payload = {"task_type": task_type}
        headers = {"X-API-Key": self.api_key}

        for attempt in range(max_retries):
            try:
                res = requests.post(f"{self.base_url}/api/v1/sandbox/execute", json=payload, headers=headers)
                
                # Payment Required
                if res.status_code == 402:
                    error_data = res.json().get("detail", {})
                    self._handle_payment_required(error_data)
                    
                    if not auto_retry:
                        return None
                        
                    estimated_cost = float(error_data.get("estimated_cost_next_task", 0.025))
                    print("\n[Kernell] Waiting for payment confirmation...")
                    self._wait_for_balance(estimated_cost)
                    print("\n[Kernell] Retrying execution after payment clearance...\n")
                    continue
                
                res.raise_for_status()
                return res.json()
                
            except requests.exceptions.RequestException as e:
                print(f"[Kernell Error] Execution failed: {str(e)}")
                return None

        print("\n[Kernell Error] Max retries exceeded. Execution aborted.")
        return None

    def _wait_for_balance(self, min_required: float, timeout=15):
        for _ in range(timeout):
            try:
                res = requests.get(f"{self.base_url}/api/v1/sandbox/balance", headers={"X-API-Key": self.api_key})
                if res.status_code == 200:
                    balance = res.json().get("balance_kern", 0)
                    if balance >= min_required:
                        return True
            except Exception:
                pass
            time.sleep(1)
        return False

    def _handle_payment_required(self, payload: dict):
        """
        Interrupts execution and safely injects the Stripe checkout flow into the terminal,
        blocking the thread until the user confirms the top-up.
        """
        recommended_plan = payload.get("recommended_plan", "growth").lower()
        estimated_cost = payload.get("estimated_cost_next_task", "0.025")
        
        # Fetch the real checkout URL from our backend to avoid passing API keys in query params
        try:
            res = requests.post(
                f"{self.base_url}/api/v1/billing/checkout?plan_id={recommended_plan}",
                headers={"X-API-Key": self.api_key}
            )
            if res.status_code == 200:
                upgrade_url = res.json().get("checkout_url")
            else:
                upgrade_url = payload.get("upgrade_url", "https://kernell.ai/pricing")
        except Exception:
            upgrade_url = payload.get("upgrade_url", "https://kernell.ai/pricing")

        print("\n" + "═"*60)
        print(" ⚠️  KERNELL — EXECUTION LIMIT REACHED")
        print("═"*60)
        print("\nYour agents have been paused to prevent uncontrolled spend.")
        print(f"Next task cost: {estimated_cost} KERN")
        print(f"Recommended plan: {recommended_plan}")
        
        print("\n→ Continue running your agents instantly:")
        print(f"  {upgrade_url}")
        
        print("\nOpening secure browser session in 3 seconds...")
        time.sleep(3)
        
        try:
            webbrowser.open(upgrade_url)
        except Exception:
            print("\n⚠️ Could not open browser automatically.")
            print("Please open the link manually in your browser.")
            
        print("\n" + "─"*60)
        print("Press [ENTER] after upgrading to resume execution...")
        input()
        
        
if __name__ == "__main__":
    # Test script usage
    print("Initializing SDK test...")
    # NOTE: You'll need to use a valid API key generated from the local server to run this
    client = KernellClient(api_key="sk_test_kernell_dummy", base_url="http://localhost:8000")
    # result = client.execute("financial")
    # print(result)
