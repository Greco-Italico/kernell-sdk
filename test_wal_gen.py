from kap_escrow.wal import TransactionWAL
w = TransactionWAL("test.bin")
w.append({"type": "CREDIT", "amount": 500})
w.append({"type": "DEBIT", "amount": 100})
w.append({"type": "SETTLEMENT", "success": True})
