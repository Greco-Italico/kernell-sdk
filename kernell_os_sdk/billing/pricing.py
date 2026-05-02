import sqlite3
import time
import uuid
import json
import os
from typing import Optional, Tuple

class PricingEngine:
    def __init__(self, db_path: str = "/var/lib/kernell/pricing.sqlite3"):
        self.db_path = db_path
        self._ensure_db()
        
    def _ensure_db(self):
        if self.db_path != ":memory:":
            os.makedirs(os.path.dirname(self.db_path) or ".", exist_ok=True)
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("PRAGMA journal_mode=WAL;")
            conn.execute('''
                CREATE TABLE IF NOT EXISTS pricing_catalog (
                    id TEXT PRIMARY KEY,
                    tenant_id TEXT,
                    metric_type TEXT NOT NULL,
                    pricing_model TEXT NOT NULL,
                    unit_price_micro BIGINT NOT NULL,
                    currency TEXT NOT NULL,
                    version INT NOT NULL,
                    valid_from REAL NOT NULL,
                    valid_to REAL,
                    metadata TEXT
                )
            ''')
            conn.execute('''
                CREATE INDEX IF NOT EXISTS idx_pricing_lookup 
                ON pricing_catalog(metric_type, tenant_id, valid_from, valid_to)
            ''')

    def add_price_rule(self, metric_type: str, pricing_model: str, unit_price_micro: int, 
                       tenant_id: str = None, metadata: dict = None, currency: str = "usd", 
                       timestamp: float = None):
        """
        Creates a new version of the pricing rule. The old rule (if any) is expired.
        """
        now = timestamp if timestamp is not None else time.time()
        meta_str = json.dumps(metadata) if metadata else None
        
        with sqlite3.connect(self.db_path) as conn:
            # Check for existing rule to expire it
            query = "SELECT id, version FROM pricing_catalog WHERE metric_type = ? AND valid_to IS NULL"
            params = [metric_type]
            if tenant_id:
                query += " AND tenant_id = ?"
                params.append(tenant_id)
            else:
                query += " AND tenant_id IS NULL"
                
            existing = conn.execute(query, tuple(params)).fetchone()
            
            version = 1
            if existing:
                old_id, old_version = existing
                conn.execute("UPDATE pricing_catalog SET valid_to = ? WHERE id = ?", (now, old_id))
                version = old_version + 1
                
            new_id = uuid.uuid4().hex
            conn.execute('''
                INSERT INTO pricing_catalog 
                (id, tenant_id, metric_type, pricing_model, unit_price_micro, currency, version, valid_from, metadata)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ''', (new_id, tenant_id, metric_type, pricing_model, unit_price_micro, currency, version, now, meta_str))
            
    def calculate(self, tenant_id: str, metric_type: str, quantity: int, timestamp: float) -> Tuple[int, int, str]:
        """
        Pure, deterministic pricing function.
        Output: amount_micro, pricing_version, pricing_model
        """
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            # Find tenant specific first, then global
            row = None
            for tid in [tenant_id, None]:
                query = """
                    SELECT * FROM pricing_catalog 
                    WHERE metric_type = ? 
                    AND (tenant_id = ? OR (? IS NULL AND tenant_id IS NULL))
                    AND valid_from <= ? 
                    AND (valid_to IS NULL OR valid_to > ?)
                    ORDER BY version DESC LIMIT 1
                """
                row = conn.execute(query, (metric_type, tid, tid, timestamp, timestamp)).fetchone()
                if row:
                    break
                    
            if not row:
                raise ValueError(f"No pricing rule found for metric {metric_type} at time {timestamp}")
                
            pricing_model = row['pricing_model']
            unit_price_micro = row['unit_price_micro']
            version = row['version']
            metadata = json.loads(row['metadata']) if row['metadata'] else {}
            
            if pricing_model == 'flat':
                amount = quantity * unit_price_micro
            elif pricing_model == 'tiered':
                tiers = metadata.get("tiers", [])
                amount = 0
                remaining = quantity
                for t in tiers:
                    up_to = t.get("up_to")
                    tier_price = t.get("price")
                    if up_to is None:
                        amount += remaining * tier_price
                        break
                    
                    in_tier = min(remaining, up_to)
                    amount += in_tier * tier_price
                    remaining -= in_tier
                    if remaining <= 0:
                        break
            elif pricing_model == 'volume':
                tiers = metadata.get("tiers", [])
                selected_price = unit_price_micro
                for t in tiers:
                    if quantity <= (t.get("up_to") or float('inf')):
                        selected_price = t.get("price")
                        break
                amount = quantity * selected_price
            else:
                # default to flat if unknown
                amount = quantity * unit_price_micro
                
            return amount, version, pricing_model
