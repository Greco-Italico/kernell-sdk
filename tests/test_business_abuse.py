import time
import json
import hmac
import hashlib
import sqlite3
import pytest
from fastapi.testclient import TestClient

import sys
from pathlib import Path
PROJECT_ROOT = Path(__file__).parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from agents.interface.api_server import app

api_client = TestClient(app)

STRIPE_SECRET = "whsec_test_secret123"

def sign_payload(payload_str: str, secret: str, timestamp: int) -> str:
    """Genera una firma Stripe válida para testing."""
    signed_payload = f"{timestamp}.{payload_str}"
    mac = hmac.new(secret.encode('utf-8'), signed_payload.encode('utf-8'), hashlib.sha256).hexdigest()
    return f"t={timestamp},v1={mac}"

@pytest.fixture
def mock_stripe_env(monkeypatch, tmp_path):
    """Fija el secreto del webhook en el entorno para el test."""
    monkeypatch.setenv("STRIPE_WEBHOOK_SECRET", STRIPE_SECRET)
    monkeypatch.setenv("LEDGER_DB_PATH", str(tmp_path / "test_ledger.sqlite3"))

def test_webhook_signature_and_replay_realistic(mock_stripe_env):
    event_id = f"evt_test_{int(time.time())}"
    payload_obj = {
        "id": event_id,
        "object": "event",
        "type": "payment_intent.succeeded",
        "data": {
            "object": {
                "id": "pi_test",
                "amount": 5000,
                "currency": "usd"
            }
        }
    }
    payload_str = json.dumps(payload_obj)
    
    # 1. Sin firma (Debe ser rechazado)
    res = api_client.post("/webhooks/stripe", content=payload_str)
    assert res.status_code in (400, 401, 403), f"Accepted payload without signature! Status: {res.status_code}"

    # 2. Firma mal formada (Debe ser rechazado por formato)
    headers = {"Stripe-Signature": "invalid_format"}
    res = api_client.post("/webhooks/stripe", content=payload_str, headers=headers)
    assert res.status_code in (400, 401, 403), "Accepted badly formatted signature!"

    # 3. Firma matemáticamente válida pero con secreto incorrecto (Ataque forjado)
    ts = int(time.time())
    forged_signature = sign_payload(payload_str, "whsec_HACKER_SECRET", ts)
    headers = {"Stripe-Signature": forged_signature}
    res = api_client.post("/webhooks/stripe", content=payload_str, headers=headers)
    assert res.status_code == 403, "VULNERABILIDAD: Se aceptó firma con secreto forjado!"

    # 4. Ataque de Replay real (MISMO event_id)
    valid_signature = sign_payload(payload_str, STRIPE_SECRET, ts)
    headers = {"Stripe-Signature": valid_signature}
    
    # Intento 1: Debe procesarse bien
    res1 = api_client.post("/webhooks/stripe", content=payload_str, headers=headers)
    assert res1.status_code == 200, f"Error en intento legítimo: {res1.text}"
    
    # Intento 2: REPLAY (Mismo evento) Debe rechazarse para evitar doble pago
    res2 = api_client.post("/webhooks/stripe", content=payload_str, headers=headers)
    assert res2.status_code == 409, "VULNERABILIDAD REPLAY: Se procesó el mismo evento dos veces!"

import threading
from kernell_os_sdk.escrow.manager import EscrowManager, EscrowState, EscrowError

def test_escrow_double_spend_race(monkeypatch, tmp_path):
    # Mock signature verification para centrarnos en la concurrencia
    monkeypatch.setattr("kernell_os_sdk.escrow.manager._verify_actor_signature", lambda pub, msg, sig: True)
    
    db_path = str(tmp_path / "escrow.sqlite3")
    
    # Setup del Escrow inicial
    setup_manager = EscrowManager(db_path)
    setup_manager.register_actor_key("buyer1", "0" * 64)
    setup_manager.register_actor_key("seller1", "1" * 64)
    
    contract_id = "esc_race_1"
    setup_manager.create_escrow("buyer1", "seller1", 100.0, contract_id=contract_id, nonce="nonce1", signature_hex="sig")
    setup_manager.fund_escrow(contract_id, actor_id="buyer1", expected_prev_state=EscrowState.CREATED, nonce="nonce2", signature_hex="sig")
    setup_manager.lock_escrow(contract_id, actor_id="buyer1", expected_prev_state=EscrowState.FUNDED, nonce="nonce3", signature_hex="sig")
    
    results = [None] * 10
    
    def attempt_release(idx):
        # Cada thread usa su propia conexión SQLite para simular peticiones concurrentes reales
        thread_manager = EscrowManager(db_path)
        thread_manager.register_actor_key("buyer1", "0" * 64)
        try:
            res = thread_manager.release_funds(
                contract_id, 
                actor_id="buyer1", 
                expected_prev_state=EscrowState.LOCKED, 
                nonce=f"nonce_release_{idx}", 
                signature_hex="sig"
            )
            results[idx] = res
        except Exception as e:
            # Puede lanzar InvalidTransition (TOCTOU bloqueado) o OperationalError (Database is locked)
            results[idx] = e
            
    threads = []
    for i in range(10):
        t = threading.Thread(target=attempt_release, args=(i,))
        threads.append(t)
        
    for t in threads:
        t.start()
    for t in threads:
        t.join()
        
    successful = [r for r in results if r is True]
    assert len(successful) == 1, f"¡Vulnerabilidad crítica: doble gasto permitido! {len(successful)} releases exitosos. Resultados completos: {results}"


import math

@pytest.mark.parametrize("amount", [
    -1,
    -1000,
    0,
    float("nan"),
    float("inf"),
    float("-inf"),
    1e309, # overflow
])
def test_escrow_numeric_abuse(monkeypatch, tmp_path, amount):
    monkeypatch.setattr("kernell_os_sdk.escrow.manager._verify_actor_signature", lambda pub, msg, sig: True)
    db_path = str(tmp_path / "escrow.sqlite3")
    manager = EscrowManager(db_path)
    manager.register_actor_key("buyer1", "0" * 64)
    manager.register_actor_key("seller1", "1" * 64)
    
    contract_id = f"esc_num_{id(amount)}"
    try:
        manager.create_escrow("buyer1", "seller1", amount, contract_id=contract_id, nonce="nonce1", signature_hex="sig")
    except Exception:
        # Válido: el sistema rechazó correctamente el valor
        return
        
    pytest.fail(f"¡Vulnerabilidad crítica! Se aceptó amount={amount}")


from core.audit.double_entry_ledger import DoubleEntryLedger, JournalLine, LedgerIntegrityError

def test_journal_must_balance(tmp_path):
    db_path = str(tmp_path / "ledger.sqlite3")
    ledger = DoubleEntryLedger(db_path)
    ledger.create_account("t1", "A", "asset")
    ledger.create_account("t1", "B", "asset")
    
    with pytest.raises(LedgerIntegrityError):
        ledger.create_journal_entry("t1", "ref1", "Unbalanced", [
            JournalLine("A", "debit", 100),
            JournalLine("B", "credit", 90)
        ])

def test_partial_journal_insertion(tmp_path, monkeypatch):
    db_path = str(tmp_path / "ledger.sqlite3")
    ledger = DoubleEntryLedger(db_path)
    ledger.create_account("t1", "A", "asset")
    ledger.create_account("t1", "B", "asset")
    
    original_connect = sqlite3.connect
    
    class CrashyConnection:
        def __init__(self, *args, **kwargs):
            self.conn = original_connect(*args, **kwargs)
            
        def execute(self, sql, parameters=None):
            if "journal_lines" in sql and parameters and len(parameters) > 5 and parameters[5] == "credit":
                raise RuntimeError("CRASH SIMULADO a mitad de la inserción")
            if parameters is not None:
                return self.conn.execute(sql, parameters)
            return self.conn.execute(sql)
            
        def __enter__(self):
            self.conn.__enter__()
            return self
            
        def __exit__(self, exc_type, exc_val, exc_tb):
            return self.conn.__exit__(exc_type, exc_val, exc_tb)

    monkeypatch.setattr(sqlite3, "connect", CrashyConnection)
    
    try:
        ledger.create_journal_entry("t1", "ref_crash", "Crash Test", [
            JournalLine("A", "debit", 100),
            JournalLine("B", "credit", 100) # Debería crashear aquí
        ])
    except RuntimeError:
        pass
    
    # Ahora quitamos el mock para verificar el estado de la BD
    monkeypatch.undo()
    
    # El account A no debería tener el debit de 100 guardado (atomicidad de ROLLBACK)
    balance_a = ledger.get_account_balance("t1", "A")
    assert balance_a == 0, f"Ledger corrupto: inserción parcial detectada. Balance de A es {balance_a}"

def test_escrow_matches_ledger_and_global_invariant(tmp_path, monkeypatch):
    monkeypatch.setattr("kernell_os_sdk.escrow.manager._verify_actor_signature", lambda pub, msg, sig: True)
    
    db_path = str(tmp_path / "escrow.sqlite3")
    ledger_path = str(tmp_path / "ledger.sqlite3")
    
    ledger = DoubleEntryLedger(ledger_path)
    
    # Añadimos unos fondos iniciales a la wallet del buyer usando el ledger directamente (simulando un depósito)
    ledger.create_account("system", "wallet_buyer_x", "asset")
    ledger.create_account("system", "bank_deposit", "asset") # cuenta externa
    ledger.create_journal_entry("system", "dep_1", "Initial deposit", [
        JournalLine("bank_deposit", "credit", 100_000_000), # 100 KERN (credit=salida)
        JournalLine("wallet_buyer_x", "debit", 100_000_000) # 100 KERN (debit=entrada)
    ])
    
    assert ledger.get_account_balance("system", "wallet_buyer_x") == 100_000_000
    
    manager = EscrowManager(db_path, ledger=ledger)
    manager.register_actor_key("buyer_x", "0" * 64)
    manager.register_actor_key("seller_x", "1" * 64)
    
    contract_id = "cross_test_1"
    
    # 1. Crear Escrow
    manager.create_escrow("buyer_x", "seller_x", 100.0, contract_id=contract_id, nonce="n1", signature_hex="sig")
    
    # 2. Fundear Escrow (Debe mover del wallet_buyer_x a escrow_locked)
    manager.fund_escrow(contract_id, actor_id="buyer_x", expected_prev_state=EscrowState.CREATED, nonce="n2", signature_hex="sig")
    
    # Verificamos balances intermedios
    assert ledger.get_account_balance("system", "wallet_buyer_x") == 0
    assert ledger.get_account_balance("system", "escrow_locked") == 100_000_000
    assert ledger.check_global_invariant() is True
    
    # 3. Lock
    manager.lock_escrow(contract_id, actor_id="buyer_x", expected_prev_state=EscrowState.FUNDED, nonce="n3", signature_hex="sig")
    
    # 4. Release (Debe mover de escrow_locked a wallet_seller_x)
    manager.release_funds(contract_id, actor_id="buyer_x", expected_prev_state=EscrowState.LOCKED, nonce="n4", signature_hex="sig")
    
    # Verificamos balances finales
    assert ledger.get_account_balance("system", "escrow_locked") == 0
    assert ledger.get_account_balance("system", "wallet_seller_x") == 100_000_000
    
    contract = manager.get_contract(contract_id)
    assert contract.state == EscrowState.RELEASED
    
    # El invariante más crítico (Big Four Invariant)
    assert ledger.check_global_invariant() is True

