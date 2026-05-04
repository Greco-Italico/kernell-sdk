import json
import uuid
from pathlib import Path

import pytest
from hypothesis import given, settings, HealthCheck, strategies as st

from kernell_sdk.runtime.firecracker.ledger import AuditLedger
from kernell_sdk.security.kms import LocalKMS


@given(
    n=st.integers(min_value=1, max_value=50),
    tenant=st.text(alphabet=st.characters(whitelist_categories=('Lu', 'Ll', 'Nd')), min_size=1, max_size=12),
    action=st.text(alphabet=st.characters(whitelist_categories=('Lu', 'Ll', 'Nd')), min_size=1, max_size=12),
    request_id=st.text(alphabet=st.characters(whitelist_categories=('Lu', 'Ll', 'Nd')), min_size=1, max_size=12),
)
@pytest.mark.invariant("L5")
@pytest.mark.invariant("L1")
@settings(max_examples=80, deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture])
def test_ledger_chain_verifies_after_appends(tmp_path: Path, n: int, tenant: str, action: str, request_id: str):
    kms = LocalKMS()
    kms.create_key(tenant)
    ledger_path = str(tmp_path / f"audit-{uuid.uuid4().hex}.ledger")
    led = AuditLedger(kms=kms, ledger_path=ledger_path)

    for i in range(n):
        led.append(tenant_id=tenant, request_id=f"{request_id}-{i}", action=action, code=f"print({i})", details={"i": i})

    ok, checked, err = led.verify_chain()
    assert ok is True
    assert checked == n
    assert err == ""


@given(
    n=st.integers(min_value=2, max_value=30),
)
@pytest.mark.invariant("L2")
@settings(max_examples=60, deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture])
def test_ledger_detects_tampering(tmp_path: Path, n: int):
    tenant = "t1"
    kms = LocalKMS()
    kms.create_key(tenant)
    ledger_path = tmp_path / f"audit-{uuid.uuid4().hex}.ledger"
    led = AuditLedger(kms=kms, ledger_path=str(ledger_path))

    for i in range(n):
        led.append(tenant_id=tenant, request_id=f"r{i}", action="EXEC", code=f"x={i}", details={"i": i})

    # Tamper with one entry_json (first line)
    lines = ledger_path.read_text().splitlines()
    assert len(lines) == n
    parts = lines[0].split("|")
    entry_json = json.loads(parts[0])
    entry_json["details"]["i"] = 999999
    parts[0] = json.dumps(entry_json, sort_keys=True)
    lines[0] = "|".join(parts)
    ledger_path.write_text("\n".join(lines) + "\n")

    ok, checked, err = led.verify_chain()
    assert ok is False
    assert "Hash mismatch" in err or "Invalid JSON" in err or "Chain broken" in err


@pytest.mark.invariant("L3")
def test_ledger_detects_invalid_signature(tmp_path: Path):
    tenant = "t1"
    kms = LocalKMS()
    kms.create_key(tenant)
    ledger_path = tmp_path / "audit.ledger"
    led = AuditLedger(kms=kms, ledger_path=str(ledger_path))
    led.append(tenant_id=tenant, request_id="r1", action="EXEC", code="print(1)", details={})

    # Corrupt signature field (third pipe-delimited segment)
    lines = ledger_path.read_text().splitlines()
    parts = lines[0].split("|")
    parts[2] = "00" * 64
    lines[0] = "|".join(parts)
    ledger_path.write_text("\n".join(lines) + "\n")

    ok, checked, err = led.verify_chain(verify_signatures=True)
    assert ok is False
    assert "Invalid signature" in err or "Malformed signature" in err

