"""
KAP Escrow — Trustless Escrow Extension for the A2A Protocol Stack
===================================================================
The missing financial protection layer between AP2 (authorization)
and x402 (settlement).

  pip install kap-escrow

Plugs into:
  • A2A Agent Cards (agent discovery & identity)
  • AP2 Mandates (authorization triggers)
  • x402 / Solana (settlement rails)
  • ERC-8004 (reputation publishing)

Core guarantees:
  • HMAC-SHA256 TX signing (deterministic JSON)
  • Anti-replay nonces (48h window)
  • Write-Ahead Log with fsync + chained SHA-256
  • Atomic WATCH/MULTI/EXEC on all balance mutations
  • Merkle tree batch anchoring with prefix protection
"""
from kap_escrow.engine import EscrowEngine
from kap_escrow.merkle import MerkleTree, build_tx_merkle
from kap_escrow.signing import sign_tx, verify_tx
from kap_escrow.wal import TransactionWAL
from kap_escrow.a2a_compat import AgentCard, validate_agent_card
from kap_escrow.ap2_compat import Mandate, escrow_from_mandate

__version__ = "1.0.0"
__all__ = [
    "EscrowEngine",
    "MerkleTree",
    "build_tx_merkle",
    "sign_tx",
    "verify_tx",
    "TransactionWAL",
    "AgentCard",
    "validate_agent_card",
    "Mandate",
    "escrow_from_mandate",
]
