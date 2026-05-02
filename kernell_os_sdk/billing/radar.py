import time
from core.db import query_all, query_one

class RadarDashboardEngine:
    def __init__(self, guard_db: str):
        self.guard_db = guard_db

    def system_health(self):
        # kill_switch_status
        row = query_one("SELECT value, updated_at FROM system_control WHERE key = 'kill_switch'", sqlite_fallback_path=self.guard_db)
        kill_switch_status = row['value'] if isinstance(row, dict) else (row[0] if row else "off")
        
        # latency p95
        # In SQLite, we can approximate p95 by sorting and picking the 95th percentile offset
        rows = query_all("SELECT latency_ms FROM spend_guard_logs WHERE created_at > ? ORDER BY latency_ms ASC", (time.time() - 300,), sqlite_fallback_path=self.guard_db)
        latencies = [r['latency_ms'] if isinstance(r, dict) else r[0] for r in rows]
        p95_latency = latencies[int(len(latencies) * 0.95)] if latencies else 0.0
        
        # requests per sec (last 5 min)
        total_requests = len(latencies)
        rps = total_requests / 300.0
        
        # allowed vs denied
        denied_rows = query_one("SELECT COUNT(*) FROM spend_guard_logs WHERE allowed = 0 AND created_at > ?", (time.time() - 300,), sqlite_fallback_path=self.guard_db)
        denied_count = denied_rows['COUNT(*)'] if isinstance(denied_rows, dict) else (denied_rows[0] if denied_rows else 0)
        deny_rate = (denied_count / total_requests) * 100 if total_requests > 0 else 0.0
        
        # gc holds per min
        gc_rows = query_one("SELECT COUNT(*) FROM spend_events WHERE event_type = 'refund' AND created_at > ?", (time.time() - 60,), sqlite_fallback_path=self.guard_db)
        gc_count = gc_rows['COUNT(*)'] if isinstance(gc_rows, dict) else (gc_rows[0] if gc_rows else 0)
        
        return {
            "kill_switch_status": kill_switch_status,
            "latency_p95_ms": round(p95_latency, 2),
            "requests_per_sec": round(rps, 2),
            "deny_rate_pct": round(deny_rate, 2),
            "gc_holds_per_min": gc_count
        }

    def spend_flow(self):
        # spend vs refund last hour by minute
        # For sqlite we approximate trunc to minute
        # We'll just group by (cast(created_at/60 as int) * 60)
        sql = """
            SELECT 
                CAST(created_at / 60 AS INT) * 60 as ts,
                SUM(CASE WHEN event_type='spend' THEN amount_micro ELSE 0 END) as spend,
                SUM(CASE WHEN event_type='refund' THEN amount_micro ELSE 0 END) as refund
            FROM spend_events
            WHERE created_at > ?
            GROUP BY ts
            ORDER BY ts
        """
        rows = query_all(sql, (time.time() - 3600,), sqlite_fallback_path=self.guard_db)
        
        timeline = []
        for r in rows:
            if isinstance(r, dict):
                timeline.append({"ts": r['ts'], "spend": r['spend'], "refund": r['refund']})
            else:
                timeline.append({"ts": r[0], "spend": r[1], "refund": r[2]})
                
        # Net burn rate (spend - refund per min over last hour)
        total_spend = sum(t['spend'] for t in timeline)
        total_refund = sum(t['refund'] for t in timeline)
        net_burn_rate = (total_spend - total_refund) / 60.0
        
        # active holds
        act_row = query_one("SELECT COUNT(*) FROM spend_holds WHERE status = 'active'", sqlite_fallback_path=self.guard_db)
        active_holds = act_row['COUNT(*)'] if isinstance(act_row, dict) else (act_row[0] if act_row else 0)
        
        # expired holds
        exp_row = query_one("SELECT COUNT(*) FROM spend_holds WHERE status = 'active' AND expires_at < ?", (time.time(),), sqlite_fallback_path=self.guard_db)
        expired_holds = exp_row['COUNT(*)'] if isinstance(exp_row, dict) else (exp_row[0] if exp_row else 0)
        
        return {
            "timeline": timeline,
            "net_burn_rate_micro_per_min": round(net_burn_rate, 2),
            "active_holds": active_holds,
            "expired_holds": expired_holds
        }

    def abuse_defense(self):
        # Denial rate overall
        rows = query_one("SELECT COUNT(*) as total, SUM(CASE WHEN allowed = 0 THEN 1 ELSE 0 END) as denied FROM spend_guard_logs WHERE created_at > ?", (time.time() - 300,), sqlite_fallback_path=self.guard_db)
        total = rows['total'] if isinstance(rows, dict) else (rows[0] if rows else 0)
        denied = rows['denied'] if isinstance(rows, dict) else (rows[1] if rows else 0)
        denial_rate = (denied / total * 100) if total > 0 else 0.0
        
        # Rate limit hits
        rl_rows = query_all("""
            SELECT tenant_id, COUNT(*) as hits
            FROM spend_guard_logs
            WHERE reason LIKE 'rate_limit%' AND created_at > ?
            GROUP BY tenant_id ORDER BY hits DESC LIMIT 10
        """, (time.time() - 300,), sqlite_fallback_path=self.guard_db)
        
        rate_hits = []
        for r in rl_rows:
            if isinstance(r, dict):
                rate_hits.append({"tenant_id": r['tenant_id'], "hits": r['hits']})
            else:
                rate_hits.append({"tenant_id": r[0], "hits": r[1]})
                
        # Suspended tenants
        susp_rows = query_all("SELECT tenant_id, updated_at FROM tenant_budgets WHERE status = 'suspended' ORDER BY updated_at DESC", sqlite_fallback_path=self.guard_db)
        suspended = []
        for r in susp_rows:
            if isinstance(r, dict):
                suspended.append({"tenant_id": r['tenant_id'], "updated_at": r['updated_at']})
            else:
                suspended.append({"tenant_id": r[0], "updated_at": r[1]})
                
        # Top spenders (bonus)
        sp_rows = query_all("""
            SELECT tenant_id, SUM(amount_micro) as spend_5m
            FROM spend_events
            WHERE event_type='spend' AND created_at > ?
            GROUP BY tenant_id ORDER BY spend_5m DESC LIMIT 5
        """, (time.time() - 300,), sqlite_fallback_path=self.guard_db)
        
        top_spenders = []
        for r in sp_rows:
            if isinstance(r, dict):
                top_spenders.append({"tenant_id": r['tenant_id'], "spend_5m": r['spend_5m']})
            else:
                top_spenders.append({"tenant_id": r[0], "spend_5m": r[1]})
                
        return {
            "denial_rate_pct": round(denial_rate, 2),
            "rate_limit_hits": rate_hits,
            "suspended_tenants": suspended,
            "top_spenders_5m": top_spenders
        }
        
    def tenant_drilldown(self, tenant_id: str):
        # Timeline
        tl_rows = query_all("SELECT id, amount_micro, event_type, created_at FROM spend_events WHERE tenant_id = ? ORDER BY created_at DESC LIMIT 100", (tenant_id,), sqlite_fallback_path=self.guard_db)
        timeline = []
        for r in tl_rows:
            if isinstance(r, dict):
                timeline.append(r)
            else:
                timeline.append({"id": r[0], "amount_micro": r[1], "event_type": r[2], "created_at": r[3]})
                
        # Holds
        h_rows = query_all("SELECT id, reserved_micro, consumed_micro, status, created_at, expires_at FROM spend_holds WHERE tenant_id = ? ORDER BY created_at DESC LIMIT 50", (tenant_id,), sqlite_fallback_path=self.guard_db)
        holds = []
        for r in h_rows:
            if isinstance(r, dict):
                holds.append(r)
            else:
                holds.append({"id": r[0], "reserved_micro": r[1], "consumed_micro": r[2], "status": r[3], "created_at": r[4], "expires_at": r[5]})
                
        # Balance
        bal_row = query_one("SELECT balance_micro, status FROM tenant_budgets WHERE tenant_id = ?", (tenant_id,), sqlite_fallback_path=self.guard_db)
        if bal_row:
            balance = bal_row['balance_micro'] if isinstance(bal_row, dict) else bal_row[0]
            status = bal_row['status'] if isinstance(bal_row, dict) else bal_row[1]
        else:
            balance = 0
            status = "not_found"
            
        return {
            "tenant_id": tenant_id,
            "balance_micro": balance,
            "status": status,
            "timeline": timeline,
            "holds": holds
        }
