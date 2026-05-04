import json
import uuid

import pytest
from hypothesis import given, settings, strategies as st
from cryptography.hazmat.primitives.asymmetric import ed25519
from cryptography.hazmat.primitives import serialization

from kernell_sdk.escrow.manager import (
    EscrowManager,
    EscrowState,
    Unauthorized,
    InvalidTransition,
    ReplayDetected,
    InvalidSignature,
)


def _gen_pair():
    priv = ed25519.Ed25519PrivateKey.generate()
    pub = priv.public_key().public_bytes(
        serialization.Encoding.Raw, serialization.PublicFormat.Raw
    ).hex()
    return priv, pub


def _sign(priv, intent: dict) -> str:
    msg = json.dumps(intent, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return priv.sign(msg).hex()


@given(
    actions=st.lists(
        st.tuples(
            st.sampled_from(["fund", "lock", "release", "dispute", "refund"]),
            st.sampled_from(["buyer", "seller", "arb"]),
        ),
        min_size=1,
        max_size=25,
    )
)
@pytest.mark.invariant("S6")
@pytest.mark.invariant("S3")
@pytest.mark.invariant("S2")
@pytest.mark.invariant("S1")
@settings(max_examples=80, deadline=None)
def test_escrow_state_machine_invariants(actions):
    m = EscrowManager(db_path=":memory:")
    buyer_priv, buyer_pub = _gen_pair()
    seller_priv, seller_pub = _gen_pair()
    arb_priv, arb_pub = _gen_pair()

    m.register_actor_key("buyer", buyer_pub)
    m.register_actor_key("seller", seller_pub)
    m.register_actor_key("arb", arb_pub)

    contract_id = f"c-{uuid.uuid4().hex[:12]}"
    create_intent = {
        "action": "CREATE",
        "contract_id": contract_id,
        "buyer_id": "buyer",
        "seller_id": "seller",
        "arbitrator_id": "arb",
        "amount_kern": "10.0",
        "expected_prev_state": "NONE",
        "nonce": "n-create",
    }
    m.create_escrow(
        buyer_id="buyer",
        seller_id="seller",
        amount=10.0,
        contract_id=contract_id,
        arbitrator_id="arb",
        nonce="n-create",
        signature_hex=_sign(buyer_priv, create_intent),
    )

    terminal_seen = False
    nonce_i = 0

    for action, actor in actions:
        c = m.get_contract(contract_id)
        assert c is not None
        assert c.state in EscrowState
        if c.state in (EscrowState.RELEASED, EscrowState.REFUNDED):
            terminal_seen = True

        nonce_i += 1
        nonce = f"n-{nonce_i}"
        intent = {
            "action": action.upper(),
            "contract_id": contract_id,
            "expected_prev_state": c.state.value,
            "nonce": nonce,
            "ts": 1234.0,
        }
        key = {"buyer": buyer_priv, "seller": seller_priv, "arb": arb_priv}[actor]
        sig = _sign(key, intent)

        try:
            if action == "fund":
                m.fund_escrow(
                    contract_id,
                    actor_id=actor,
                    expected_prev_state=c.state,
                    nonce=nonce,
                    signature_hex=sig,
                )
            elif action == "lock":
                m.lock_escrow(
                    contract_id,
                    actor_id=actor,
                    expected_prev_state=c.state,
                    nonce=nonce,
                    signature_hex=sig,
                )
            elif action == "release":
                m.release_funds(
                    contract_id,
                    actor_id=actor,
                    expected_prev_state=c.state,
                    nonce=nonce,
                    signature_hex=sig,
                )
            elif action == "dispute":
                m.open_dispute(
                    contract_id,
                    actor_id=actor,
                    expected_prev_state=c.state,
                    nonce=nonce,
                    signature_hex=sig,
                )
            elif action == "refund":
                m.refund(
                    contract_id,
                    actor_id=actor,
                    expected_prev_state=c.state,
                    nonce=nonce,
                    signature_hex=sig,
                )
        except (Unauthorized, InvalidTransition, ReplayDetected, InvalidSignature):
            pass

        c2 = m.get_contract(contract_id)
        assert c2 is not None
        # Safety invariant: impossible states must never appear.
        assert c2.state in {
            EscrowState.CREATED,
            EscrowState.FUNDED,
            EscrowState.LOCKED,
            EscrowState.DISPUTED,
            EscrowState.EXPIRED,
            EscrowState.RELEASED,
            EscrowState.REFUNDED,
        }
        # Once terminal has been reached, state cannot go back to non-terminal.
        if terminal_seen:
            assert c2.state in (EscrowState.RELEASED, EscrowState.REFUNDED)

