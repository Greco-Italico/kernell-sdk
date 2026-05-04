"""
KAP WAL — Crash-Recoverable Transaction Journal
=================================================
Append-only JSONL with chained SHA-256 hashes and fsync durability.
"""
from __future__ import annotations

import logging
import threading
from typing import Any, Dict, List, Tuple

logger = logging.getLogger("KAP_WAL")

try:
    from kap_escrow.kap_core import RustTransactionWAL
except ImportError as e:
    raise RuntimeError("HFT Error: kap_core Rust module not found. You must compile the bindings with Maturin.") from e

class TransactionWAL:
    """Zero-Copy Write-Ahead Log delegated to Rust via PyO3.
    Wrapped with a threading lock to prevent Rust RefCell 'Already borrowed' panics 
    under heavy multi-threading."""

    def __init__(self, path: str = "./kap_escrow_wal.bin"):
        self.path = path
        self._rust_wal = RustTransactionWAL(path)
        self._lock = threading.Lock()

    def append(self, record: Dict[str, Any]) -> Dict[str, Any]:
        with self._lock:
            return self._rust_wal.append(record)

    def verify_integrity(self) -> Tuple[bool, int]:
        return self._rust_wal.verify_integrity()

    def replay(self, since_seq: int = 0) -> List[Dict[str, Any]]:
        return self._rust_wal.replay(since_seq)
