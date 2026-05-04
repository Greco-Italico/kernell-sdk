import sqlite3
import time
import logging
from typing import Optional, List, Dict
from dataclasses import dataclass, asdict

logger = logging.getLogger(__name__)


@dataclass
class TenantFinancialSnapshot:
    tenant_id: str
    current_balance: int
    total_spend_alltime: int
    spend_last_hour: int
    spend_last_day: int
    burn_rate_per_minute: int  # micro per minute, derived from last 5 min
    pending_outbox_count: int
    pending_outbox_amount: int
    anomaly_flag: bool
    anomaly_reason: Optional[str]
    snapshot_at: float

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class RadarMetricsSnapshot:
    tenant_suspended: int
    deny_rate_pct: float
    rate_limit_spikes: int
    auto_refund_ratio: float
    partial_usage_pct: float
    expired_holds_captured: int
    drift_detected: bool
    kill_switch_status: str

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class SystemHealthSnapshot:
    total_tenants: int
    total_events_ingested: int
    events_pending_aggregation: int
    outbox_pending: int
    outbox_processing: int
    stuck_events: int
    ledger_entries_total: int
    drift_detected: bool
    snapshot_at: float

    def to_dict(self) -> dict:
        return asdict(self)


class FinancialObserver:
    """
    Read-only observation layer over the financial execution stack.
    
    Never mutates execution state. Derives all data from:
    - metering DB (events, aggregates, outbox)
    - spend_guard DB (shadow balances)
    - ledger DB (journal entries/lines)
    
    Designed for dashboards, alerting, and anomaly detection.
    """

    def __init__(self, metering_db_path: str, spend_guard_db_path: str, ledger_db_path: str,
                 anomaly_multiplier: float = 3.0):
        self.metering_db = metering_db_path
        self.guard_db = spend_guard_db_path
        self.ledger_db = ledger_db_path
        self.anomaly_multiplier = anomaly_multiplier

    def tenant_snapshot(self, tenant_id: str) -> TenantFinancialSnapshot:
        """Materialized financial view for a single tenant."""
        now = time.time()
        one_hour_ago = now - 3600
        one_day_ago = now - 86400
        five_min_ago = now - 300

        # Balance from spend guard
        balance = 0
        with sqlite3.connect(self.guard_db) as conn:
            row = conn.execute(
                "SELECT balance_micro FROM tenant_budgets WHERE tenant_id = ?",
                (tenant_id,)
            ).fetchone()
            if row:
                balance = row[0]

        # Spend metrics from metering
        with sqlite3.connect(self.metering_db) as conn:
            # All-time spend from committed/processed outbox
            alltime = conn.execute(
                "SELECT COALESCE(SUM(amount_micro), 0) FROM billing_outbox WHERE tenant_id = ? AND status IN ('processed', 'processing')",
                (tenant_id,)
            ).fetchone()[0]

            # Last hour: events ingested in the last hour
            hour_spend = conn.execute(
                "SELECT COALESCE(SUM(amount_micro), 0) FROM billing_outbox WHERE tenant_id = ? AND created_at >= ? AND status IN ('processed', 'processing')",
                (tenant_id, one_hour_ago)
            ).fetchone()[0]

            # Last day
            day_spend = conn.execute(
                "SELECT COALESCE(SUM(amount_micro), 0) FROM billing_outbox WHERE tenant_id = ? AND created_at >= ? AND status IN ('processed', 'processing')",
                (tenant_id, one_day_ago)
            ).fetchone()[0]

            # Burn rate: usage cost in last 5 min
            recent_cost = conn.execute(
                "SELECT COALESCE(SUM(amount_micro), 0) FROM billing_outbox WHERE tenant_id = ? AND created_at >= ?",
                (tenant_id, five_min_ago)
            ).fetchone()[0]
            # Approximate burn rate per minute (money/min)
            burn_rate = int(recent_cost / 5) if recent_cost > 0 else 0

            # Pending outbox
            pending = conn.execute(
                "SELECT COUNT(*), COALESCE(SUM(amount_micro), 0) FROM billing_outbox WHERE tenant_id = ? AND status = 'pending'",
                (tenant_id,)
            ).fetchone()
            pending_count, pending_amount = pending[0], pending[1]

        # Anomaly detection: compare last 5 min rate to last hour average
        avg_rate_hour = int(hour_spend / 60) if hour_spend > 0 else 0
        anomaly = False
        anomaly_reason = None
        if avg_rate_hour > 0 and burn_rate > avg_rate_hour * self.anomaly_multiplier:
            anomaly = True
            anomaly_reason = f"Burn rate {burn_rate}/min is {burn_rate / avg_rate_hour:.1f}x the hourly average {avg_rate_hour}/min"

        return TenantFinancialSnapshot(
            tenant_id=tenant_id,
            current_balance=balance,
            total_spend_alltime=alltime,
            spend_last_hour=hour_spend,
            spend_last_day=day_spend,
            burn_rate_per_minute=burn_rate,
            pending_outbox_count=pending_count,
            pending_outbox_amount=pending_amount,
            anomaly_flag=anomaly,
            anomaly_reason=anomaly_reason,
            snapshot_at=now,
        )

    def system_health(self) -> SystemHealthSnapshot:
        """Global system health snapshot."""
        now = time.time()
        stuck_threshold = now - 120  # events older than 2 min unprocessed

        with sqlite3.connect(self.metering_db) as conn:
            total_events = conn.execute("SELECT COUNT(*) FROM metering_events").fetchone()[0]

            # Events not yet aggregated: events whose aggregation_key has no aggregate row
            pending_agg = conn.execute("""
                SELECT COUNT(DISTINCT me.aggregation_key) FROM metering_events me
                LEFT JOIN metering_aggregates ma 
                ON me.tenant_id = ma.tenant_id AND me.metric_type = ma.metric_type 
                   AND me.aggregation_key = (ma.tenant_id || ':' || ma.metric_type || ':' || ma.window_start)
                WHERE ma.tenant_id IS NULL
            """).fetchone()[0]

            outbox_pending = conn.execute(
                "SELECT COUNT(*) FROM billing_outbox WHERE status = 'pending'"
            ).fetchone()[0]

            outbox_processing = conn.execute(
                "SELECT COUNT(*) FROM billing_outbox WHERE status = 'processing'"
            ).fetchone()[0]

        # Stuck events from ledger inbox
        stuck = 0
        try:
            with sqlite3.connect(self.ledger_db) as conn:
                stuck = conn.execute(
                    "SELECT COUNT(*) FROM incoming_events WHERE processed_at IS NULL AND received_at < ?",
                    (stuck_threshold,)
                ).fetchone()[0]
        except Exception:
            pass  # Table may not exist in test environments

        # Total ledger entries
        ledger_total = 0
        try:
            with sqlite3.connect(self.ledger_db) as conn:
                ledger_total = conn.execute("SELECT COUNT(*) FROM journal_entries").fetchone()[0]
        except Exception:
            pass

        # Total tenants
        total_tenants = 0
        with sqlite3.connect(self.guard_db) as conn:
            total_tenants = conn.execute("SELECT COUNT(*) FROM tenant_budgets").fetchone()[0]

        # Drift detection: compare shadow balances to ledger reality
        drift = self._detect_drift()

        return SystemHealthSnapshot(
            total_tenants=total_tenants,
            total_events_ingested=total_events,
            events_pending_aggregation=pending_agg,
            outbox_pending=outbox_pending,
            outbox_processing=outbox_processing,
            stuck_events=stuck,
            ledger_entries_total=ledger_total,
            drift_detected=drift,
            snapshot_at=now,
        )

    def _detect_drift(self) -> bool:
        """
        Compare shadow balances (spend_guard) against ledger truth.
        Returns True if any tenant has drifted beyond tolerance.
        """
        TOLERANCE = 1000  # 1000 micro = acceptable rounding/timing drift

        with sqlite3.connect(self.guard_db) as conn:
            tenants = conn.execute("SELECT tenant_id, balance_micro FROM tenant_budgets").fetchall()

        for tenant_id, shadow_balance in tenants:
            try:
                with sqlite3.connect(self.ledger_db) as conn:
                    # Get expense from ledger
                    expense_row = conn.execute(
                        "SELECT COALESCE(SUM(CASE WHEN direction='debit' THEN amount_micro ELSE 0 END), 0) FROM journal_lines WHERE tenant_id = ?",
                        (tenant_id,)
                    ).fetchone()
                    credit_row = conn.execute(
                        "SELECT COALESCE(SUM(CASE WHEN direction='credit' THEN amount_micro ELSE 0 END), 0) FROM journal_lines WHERE tenant_id = ?",
                        (tenant_id,)
                    ).fetchone()
                    ledger_net = (credit_row[0] or 0) - (expense_row[0] or 0)

                if abs(shadow_balance - ledger_net) > TOLERANCE:
                    logger.warning(
                        f"DRIFT DETECTED for tenant {tenant_id}: shadow={shadow_balance}, ledger={ledger_net}, delta={shadow_balance - ledger_net}"
                    )
                    return True
            except Exception:
                continue

        return False

    def cost_per_endpoint(self, tenant_id: str = None, hours: int = 24) -> List[Dict]:
        """
        Cost breakdown by metric_type.
        Enables true AWS-style Cost Explorer insights by showing money, not raw volume.
        """
        threshold = time.time() - (hours * 3600)

        with sqlite3.connect(self.metering_db) as conn:
            query = """
                SELECT metric_type,
                       COUNT(*) as window_count,
                       SUM(amount_micro) as total_cost_micro
                FROM billing_outbox
                WHERE created_at >= ? AND status IN ('processed', 'processing')
            """
            params = [threshold]

            if tenant_id:
                query += " AND tenant_id = ?"
                params.append(tenant_id)

            query += " GROUP BY metric_type ORDER BY total_cost_micro DESC"

            rows = conn.execute(query, tuple(params)).fetchall()
            return [
                {
                    "metric": r[0],
                    "cost_micro": r[2] or 0
                }
                for r in rows
            ]

    def radar_metrics(self) -> RadarMetricsSnapshot:
        """
        Radar Dashboard metrics:
        - SpendGuard Health: suspended tenants, rate limit spikes (approximated).
        - Holds & GC: auto_refund_ratio, partial_usage_pct, expired_holds_captured.
        - Drift Detector: boolean indicator.
        - Kill Switch status.
        """
        suspended = 0
        rate_limit_spikes = 0
        kill_switch = "off"
        
        with sqlite3.connect(self.guard_db) as conn:
            # Suspended tenants
            row = conn.execute("SELECT COUNT(*) FROM tenant_budgets WHERE status = 'suspended'").fetchone()
            if row:
                suspended = row[0]
                
            # Kill switch
            row = conn.execute("SELECT value FROM system_control WHERE key = 'kill_switch'").fetchone()
            if row:
                kill_switch = row[0]
                
            # Rate limit spikes (approximated from tenants near the 10s window limit of $5)
            # Or from tenant_rate_limit
            # We will use tenant_rate_limit requests near limits
            now_int = int(time.time())
            window_start = now_int - (now_int % 60)
            row = conn.execute(
                "SELECT COUNT(*) FROM tenant_rate_limit WHERE window_start >= ? AND request_count >= 50",
                (window_start - 3600,) # last hour spikes
            ).fetchone()
            if row:
                rate_limit_spikes = row[0]
                
            # Holds & GC
            # Auto refund ratio = sum(refund) / sum(reserved) for finalized
            # Partial usage = sum(consumed) / sum(reserved)
            # Expired holds captured = finalized with 0 consumption (approximate)
            auto_refund_ratio = 0.0
            partial_usage_pct = 0.0
            expired_captured = 0
            
            row = conn.execute("""
                SELECT SUM(reserved_micro), SUM(consumed_micro) 
                FROM spend_holds 
                WHERE status = 'finalized'
            """).fetchone()
            
            if row and row[0] and row[0] > 0:
                reserved = row[0]
                consumed = row[1] or 0
                partial_usage_pct = (consumed / reserved) * 100
                auto_refund_ratio = ((reserved - consumed) / reserved) * 100
                
            row = conn.execute("""
                SELECT COUNT(*) FROM spend_holds 
                WHERE status = 'finalized' AND consumed_micro = 0
            """).fetchone()
            if row:
                expired_captured = row[0]
                
            # Deny rate (approximated)
            # Assuming suspended tenants / total tenants
            row = conn.execute("SELECT COUNT(*) FROM tenant_budgets").fetchone()
            total_tenants = row[0] if row and row[0] > 0 else 1
            deny_rate_pct = (suspended / total_tenants) * 100
            
        drift = self._detect_drift()
        
        return RadarMetricsSnapshot(
            tenant_suspended=suspended,
            deny_rate_pct=deny_rate_pct,
            rate_limit_spikes=rate_limit_spikes,
            auto_refund_ratio=auto_refund_ratio,
            partial_usage_pct=partial_usage_pct,
            expired_holds_captured=expired_captured,
            drift_detected=drift,
            kill_switch_status=kill_switch
        )
