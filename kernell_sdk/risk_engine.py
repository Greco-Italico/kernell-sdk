"""
Kernell OS SDK — Risk Engine & Behavioral Monitor
════════════════════════════════════════════════════
Defends against Goal Hijacking, Chained Low-Risk Actions,
and Data Exfiltration via Taint Tracking and Anomaly Detection.
"""
from enum import IntEnum
from typing import Any, Dict, List, Optional
import time
import structlog
from pydantic import BaseModel, Field

logger = structlog.get_logger("kernell.risk_engine")


class RiskLevel(IntEnum):
    LOW = 1
    MEDIUM = 2
    HIGH = 3
    CRITICAL = 4


class DataSensitivity(IntEnum):
    PUBLIC = 1
    INTERNAL = 2
    SECRET = 3


class ActionTag(BaseModel):
    command: str
    timestamp: float
    bytes_processed: int = 0
    sensitivity: DataSensitivity = DataSensitivity.PUBLIC


# Anti State-Bloat constants
MAX_CONTEXT_ACTIONS = 50
MAX_CUMULATIVE_BYTES = 5 * 1024 * 1024  # 5MB lifetime cap before forced cooldown
CONTEXT_RESET_INTERVAL_S = 300  # Reset cumulative counters every 5 minutes


class ExecutionContext(BaseModel):
    """Short-term memory of the agent's execution flow for Taint Tracking.
    
    Anti State-Bloat:
      - History capped at MAX_CONTEXT_ACTIONS entries
      - Cumulative bytes auto-reset every CONTEXT_RESET_INTERVAL_S
      - Hard cap at MAX_CUMULATIVE_BYTES triggers forced cooldown
    """
    history: List[ActionTag] = Field(default_factory=list)
    total_bytes_read: int = 0
    holds_sensitive_data: bool = False
    last_reset_ts: float = Field(default_factory=time.time)
    is_throttled: bool = False
    
    def record_action(self, tag: ActionTag):
        now = time.time()
        
        # Periodic state reset (anti state-bloat)
        if now - self.last_reset_ts > CONTEXT_RESET_INTERVAL_S:
            self.total_bytes_read = 0
            self.is_throttled = False
            self.last_reset_ts = now
        
        self.history.append(tag)
        self.total_bytes_read += tag.bytes_processed
        if tag.sensitivity >= DataSensitivity.INTERNAL:
            self.holds_sensitive_data = True
            
        # Hard cap: force throttle if cumulative bytes exceed limit
        if self.total_bytes_read > MAX_CUMULATIVE_BYTES:
            self.is_throttled = True
            
        # Keep bounded history (anti state-bloat)
        if len(self.history) > MAX_CONTEXT_ACTIONS:
            self.history = self.history[-MAX_CONTEXT_ACTIONS:]


class BehaviorMonitor:
    """Detects behavioral drift, chained anomalies, and griefing patterns."""
    
    def __init__(self, max_requests_per_min: int = 10, max_read_volume_kb: int = 500):
        self.max_requests_per_min = max_requests_per_min
        self.max_read_volume = max_read_volume_kb * 1024

    def detect_anomalies(self, context: ExecutionContext, current_cmd: str) -> List[str]:
        anomalies = []
        now = time.time()
        
        # 1. Rate Limiting Anomaly (per-agent, not global)
        recent_actions = [a for a in context.history if now - a.timestamp < 60]
        if len(recent_actions) >= self.max_requests_per_min:
            anomalies.append(f"Behavior Drift: {len(recent_actions)} requests/min exceeds baseline.")
            
        # 2. Chained Data Volume Anomaly (Slow Exfiltration)
        if context.total_bytes_read > self.max_read_volume:
            anomalies.append(f"Volume Drift: Read {context.total_bytes_read} bytes, exceeding threshold.")
        
        # 3. State Bloat Throttle (anti state-bloat attack)
        if context.is_throttled:
            anomalies.append(f"State Bloat: Agent throttled after {context.total_bytes_read} cumulative bytes.")

        # 4. Repetitive Command Pattern (griefing detection)
        if len(recent_actions) >= 5:
            recent_cmds = [a.command.split()[0] for a in recent_actions[-5:]]
            if len(set(recent_cmds)) == 1:
                anomalies.append(f"Griefing Pattern: {recent_cmds[0]} repeated {len(recent_actions)} times.")
            
        return anomalies


class RiskEngine:
    """
    Evaluates semantic risk dynamically based on action, context, and cross-layer state.
    
    Defends against:
      - Goal Hijacking (taint tracking)
      - Chained Low-Risk Actions (volume + rate monitoring)
      - Economic Griefing (repetitive pattern detection)
      - Cross-Layer Desync (consistency assertion)
      - State Bloat (cumulative byte cap + auto-reset)
    """
    def __init__(self):
        self.monitor = BehaviorMonitor()

    def evaluate(self, command: str, context: ExecutionContext) -> RiskLevel:
        base_risk = self._get_base_risk(command)
        risk_score = base_risk.value
        
        # Data Flow Control (Taint Tracking)
        # If agent read sensitive data previously, sending it to network is CRITICAL.
        if context.holds_sensitive_data and self._is_egress_command(command):
            logger.warning("risk_data_flow_violation", command=command)
            risk_score += 2  # Escalate immediately
            
        # Behavior Drift Detection (includes griefing + state bloat)
        anomalies = self.monitor.detect_anomalies(context, command)
        if anomalies:
            for anomaly in anomalies:
                logger.warning("risk_anomaly_detected", reason=anomaly)
            risk_score += len(anomalies)  # Each anomaly compounds risk
            
        # Cap at CRITICAL
        final_risk = min(risk_score, RiskLevel.CRITICAL.value)
        return RiskLevel(final_risk)

    def cross_layer_verify(self, policy_allowed: bool, risk: RiskLevel, command: str) -> bool:
        """
        Cross-Layer Consistency Check.
        
        Catches desync between PolicyEngine (allowed) and RiskEngine (should be blocked).
        If PolicyEngine says OK but RiskEngine says CRITICAL, something is wrong.
        This is the redundancy layer that prevents edge-case bypasses.
        """
        if policy_allowed and risk >= RiskLevel.CRITICAL:
            logger.critical(
                "cross_layer_desync_detected",
                command=command,
                policy="ALLOWED",
                risk=risk.name,
                action="BLOCKING — risk override takes precedence"
            )
            return False  # Risk override: block even if policy allowed
        return True

    def _get_base_risk(self, command: str) -> RiskLevel:
        """Static base risk assessment."""
        if "wallet.transfer" in command or "escrow" in command:
            return RiskLevel.CRITICAL
        if command.startswith("curl ") or command.startswith("wget ") or "git push" in command:
            return RiskLevel.HIGH
        if "cat /" in command or "python" in command:
            return RiskLevel.MEDIUM
        return RiskLevel.LOW

    def _is_egress_command(self, command: str) -> bool:
        """Returns True if the command sends data out of the system."""
        egress_cmds = ["curl", "wget", "git push", "scp", "rsync"]
        return any(command.startswith(cmd) for cmd in egress_cmds)
