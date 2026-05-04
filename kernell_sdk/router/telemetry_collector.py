"""
Kernell OS SDK — Distributed Telemetry Collector (Data Flywheel)
═══════════════════════════════════════════════════════════════════
Every SDK instance is a sensor. This module collects anonymized
routing decisions and ships them to Kernell Cloud for continuous
Classifier-Pro training.

Pattern: Tesla Fleet Learning
  - Each car (SDK) captures driving data (routing decisions)
  - Data is anonymized and batched
  - Central model improves with every fleet mile
  - Better model → better car → more data → stronger moat

Privacy guarantees:
  ✅ Opt-in only (disabled by default)
  ✅ No PII — task content is SHA-256 hashed
  ✅ No prompt text — only structural metadata
  ✅ Batched + compressed uploads (no real-time streaming)
  ✅ Local buffer persists if network is down
  ✅ User can inspect/delete buffer at any time
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import threading
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger("kernell.router.telemetry")


# ── Telemetry Event Schema ───────────────────────────────────────────────

@dataclass
class TelemetryEvent:
    """
    Single anonymized routing decision event.
    
    This is the training data unit for Classifier-Pro.
    NO task content or prompt text is ever included.
    """
    # ── Identity (anonymized) ────────────────────────────────────
    sdk_instance_id: str          # Random UUID generated at install
    event_id: str                 # Unique event hash
    timestamp: float              # Unix timestamp
    
    # ── Task Metadata (anonymized) ───────────────────────────────
    task_hash: str                # SHA-256 of the original task (irreversible)
    task_token_count: int         # Approximate token count of original task
    task_domain: str              # "code", "reasoning", "data", etc.
    
    # ── Routing Decision ─────────────────────────────────────────
    predicted_difficulty: int     # 1-5, what the local classifier guessed
    predicted_tier: str           # "local_nano", "cheap_api", etc.
    classifier_confidence: float  # 0.0-1.0
    
    # ── Actual Outcome ───────────────────────────────────────────
    actual_tier_used: str         # What tier actually executed successfully
    was_escalated: bool           # Did we need to go higher?
    escalation_chain: List[str] = field(default_factory=list)  # e.g. ["local_small", "cheap_api"]
    
    # ── Performance Signals ──────────────────────────────────────
    success: bool = True
    tokens_in: int = 0
    tokens_out: int = 0
    cost_usd: float = 0.0
    latency_ms: float = 0.0
    was_cached: bool = False
    verifier_accepted: bool = True
    verifier_confidence: float = 0.0
    
    # ── Hardware Context (anonymized) ────────────────────────────
    hardware_tier: str = ""       # "minimal", "balanced", "powerful", "workstation"
    has_gpu: bool = False
    ram_bucket: str = ""          # "4gb", "8gb", "16gb", "32gb", "64gb+" (bucketed, not exact)
    
    # ── Model Info ───────────────────────────────────────────────
    local_model_used: str = ""    # e.g. "qwen3:1.7b" (public model name, not sensitive)
    api_provider: str = ""        # "groq", "deepseek", "anthropic" (provider, not key)

    # ── Policy Model v2 (training signal) ────────────────────────
    policy_route_predicted: str = ""   # "local", "cheap", "premium", "hybrid"
    policy_confidence: float = 0.0     # Policy model's own confidence
    policy_expected_cost: float = 0.0  # What policy estimated cost would be
    policy_expected_latency: float = 0.0  # What policy estimated latency would be
    policy_risk: str = ""              # "low", "medium", "high"
    policy_version: str = ""           # "v0", "v1", etc. for A/B tracking
    final_route_used: str = ""         # Actual route after all fallbacks
    fallback_trigger: str = ""         # Why fallback happened: "verification_fail", "timeout", etc.

    def to_dict(self) -> dict:
        return asdict(self)


# ── Telemetry Config ─────────────────────────────────────────────────────

@dataclass
class TelemetryConfig:
    """Configuration for the data flywheel."""
    enabled: bool = False                    # OPT-IN ONLY. Never enabled by default.
    endpoint: str = "https://telemetry.kernellos.com/v1/events"
    api_key: str = ""                        # SDK telemetry key (not user's LLM key)
    
    # Batching
    batch_size: int = 50                     # Events per upload
    flush_interval_seconds: float = 300.0    # Flush every 5 minutes
    max_buffer_size: int = 10000             # Max events in memory
    
    # Privacy
    anonymize_tasks: bool = True             # SHA-256 hash all task content
    include_hardware_info: bool = True       # Include bucketed hardware info
    include_model_names: bool = True         # Include public model names
    
    # Persistence
    buffer_dir: str = ""                     # Directory for offline buffer
    
    # Consent
    consent_given: bool = False              # Explicit user consent flag
    consent_timestamp: float = 0.0           # When consent was given


# ── The Collector ────────────────────────────────────────────────────────

class TelemetryCollector:
    """
    Distributed data collection agent embedded in every SDK instance.
    
    This is NOT spyware. It is:
      - Opt-in only (disabled by default)
      - Fully transparent (user can inspect buffer)
      - Anonymized (no PII, no prompt text)
      - Deletable (user can purge at any time)
      
    It IS the competitive advantage:
      - Every SDK instance feeds routing decisions to Kernell Cloud
      - Classifier-Pro trains on this data continuously
      - Better model → cheaper routing → more adoption → more data
      - This is the Tesla fleet learning pattern
    """
    
    def __init__(self, config: Optional[TelemetryConfig] = None,
                 instance_id: str = ""):
        self._config = config or TelemetryConfig()
        self._instance_id = instance_id or self._generate_instance_id()
        self._buffer: List[TelemetryEvent] = []
        self._lock = threading.Lock()
        self._total_collected = 0
        self._total_shipped = 0
        self._last_flush = time.time()
        self._flush_errors = 0
        
        # Load persisted buffer if exists
        if self._config.buffer_dir:
            self._load_buffer()
        
        # Start background flush thread if enabled
        if self._config.enabled and self._config.consent_given:
            self._start_flush_thread()
    
    # ── Public API ───────────────────────────────────────────────────

    def record(self, event: TelemetryEvent) -> None:
        """Record a routing decision event."""
        if not self._config.enabled or not self._config.consent_given:
            return
        
        # Anonymize if configured
        if self._config.anonymize_tasks:
            event.task_hash = hashlib.sha256(
                event.task_hash.encode()
            ).hexdigest()[:16]
        
        event.sdk_instance_id = self._instance_id
        
        with self._lock:
            self._buffer.append(event)
            self._total_collected += 1
            
            # Enforce max buffer
            if len(self._buffer) > self._config.max_buffer_size:
                self._buffer = self._buffer[-self._config.max_buffer_size:]
            
            # Auto-flush if batch is full
            if len(self._buffer) >= self._config.batch_size:
                self._flush_async()

    def record_from_result(self, task: str, subtask_desc: str,
                           predicted_difficulty: int, predicted_tier: str,
                           confidence: float, result: Any,
                           hardware_tier: str = "", has_gpu: bool = False,
                           ram_gb: int = 0,
                           policy_decision: Optional[Any] = None,
                           final_route_used: str = "",
                           fallback_trigger: str = "") -> None:
        """
        Convenience method to record from an ExecutionResult.
        
        Called by the IntelligentRouter after each subtask execution.
        """
        event = TelemetryEvent(
            sdk_instance_id=self._instance_id,
            event_id=hashlib.sha256(
                f"{time.time()}{task}{subtask_desc}".encode()
            ).hexdigest()[:16],
            timestamp=time.time(),
            task_hash=task,
            task_token_count=len(task.split()),  # Rough estimate
            task_domain=getattr(result, 'domain', 'general') if hasattr(result, 'domain') else 'general',
            predicted_difficulty=predicted_difficulty,
            predicted_tier=predicted_tier,
            classifier_confidence=confidence,
            actual_tier_used=result.tier_used.value if hasattr(result, 'tier_used') else predicted_tier,
            was_escalated=result.escalated_from is not None if hasattr(result, 'escalated_from') else False,
            escalation_chain=(
                [result.escalated_from.value, result.tier_used.value]
                if hasattr(result, 'escalated_from') and result.escalated_from
                else []
            ),
            success=result.success if hasattr(result, 'success') else True,
            tokens_in=result.tokens_in if hasattr(result, 'tokens_in') else 0,
            tokens_out=result.tokens_out if hasattr(result, 'tokens_out') else 0,
            cost_usd=result.cost_usd if hasattr(result, 'cost_usd') else 0.0,
            latency_ms=result.latency_ms if hasattr(result, 'latency_ms') else 0.0,
            was_cached=result.was_cached if hasattr(result, 'was_cached') else False,
            hardware_tier=hardware_tier,
            has_gpu=has_gpu,
            ram_bucket=self._bucket_ram(ram_gb),
            local_model_used=result.model_used if hasattr(result, 'model_used') else "",
            policy_route_predicted=(
                getattr(getattr(policy_decision, "route", None), "value", "")
                if policy_decision else ""
            ),
            policy_confidence=float(getattr(policy_decision, "confidence", 0.0)) if policy_decision else 0.0,
            policy_expected_cost=float(getattr(policy_decision, "expected_cost_usd", 0.0)) if policy_decision else 0.0,
            policy_expected_latency=float(getattr(policy_decision, "expected_latency_s", 0.0)) if policy_decision else 0.0,
            policy_risk=(
                getattr(getattr(policy_decision, "risk", None), "value", "")
                if policy_decision else ""
            ),
            policy_version=str(getattr(policy_decision, "policy_version", "")) if policy_decision else "",
            final_route_used=final_route_used,
            fallback_trigger=fallback_trigger,
        )
        self.record(event)

    def enable(self, consent: bool = True) -> None:
        """Explicitly enable telemetry with user consent."""
        self._config.enabled = True
        self._config.consent_given = consent
        self._config.consent_timestamp = time.time()
        if consent:
            self._start_flush_thread()
            logger.info(
                "Telemetry enabled with user consent. "
                "Anonymized routing data will be sent to Kernell Cloud "
                "to improve the Classifier-Pro model. "
                "You can disable this at any time with collector.disable()"
            )

    def disable(self) -> None:
        """Disable telemetry collection."""
        self._config.enabled = False
        self._config.consent_given = False
        logger.info("Telemetry disabled. No data will be collected or sent.")

    def inspect_buffer(self) -> List[dict]:
        """Let the user see exactly what data is being collected."""
        with self._lock:
            return [e.to_dict() for e in self._buffer]

    def purge_buffer(self) -> int:
        """Delete all buffered data. Returns count of purged events."""
        with self._lock:
            count = len(self._buffer)
            self._buffer.clear()
        self._purge_persisted()
        logger.info(f"Telemetry buffer purged: {count} events deleted")
        return count

    def get_stats(self) -> dict:
        """Telemetry collection statistics."""
        return {
            "enabled": self._config.enabled,
            "consent_given": self._config.consent_given,
            "total_collected": self._total_collected,
            "total_shipped": self._total_shipped,
            "buffer_size": len(self._buffer),
            "flush_errors": self._flush_errors,
            "instance_id": self._instance_id[:8] + "...",
        }

    # ── Flush Logic ──────────────────────────────────────────────────

    def _flush_async(self) -> None:
        """Flush buffer in a background thread."""
        thread = threading.Thread(target=self._flush, daemon=True)
        thread.start()

    def _flush(self) -> None:
        """Ship buffered events to Kernell Cloud."""
        with self._lock:
            if not self._buffer:
                return
            batch = self._buffer[:self._config.batch_size]
            self._buffer = self._buffer[self._config.batch_size:]

        payload = {
            "sdk_version": "1.0.0",
            "instance_id": self._instance_id,
            "event_count": len(batch),
            "events": [e.to_dict() for e in batch],
        }

        try:
            # Use safe HTTP client if available, otherwise httpx
            self._ship_payload(payload)
            self._total_shipped += len(batch)
            self._last_flush = time.time()
            logger.debug(f"Telemetry: shipped {len(batch)} events")
        except Exception as e:
            # Put events back in buffer (don't lose data)
            with self._lock:
                self._buffer = batch + self._buffer
            self._flush_errors += 1
            logger.debug(f"Telemetry flush failed (will retry): {e}")
            # Persist to disk as backup
            self._persist_buffer(batch)

    def _ship_payload(self, payload: dict) -> None:
        """Send payload to the telemetry endpoint."""
        try:
            import httpx
            resp = httpx.post(
                self._config.endpoint,
                json=payload,
                headers={
                    "Authorization": f"Bearer {self._config.api_key}",
                    "Content-Type": "application/json",
                    "X-SDK-Instance": self._instance_id[:8],
                },
                timeout=10.0,
            )
            resp.raise_for_status()
        except ImportError:
            # httpx not available — persist locally
            self._persist_buffer(payload.get("events", []))

    def _start_flush_thread(self) -> None:
        """Background thread that flushes at intervals."""
        def _loop():
            while self._config.enabled and self._config.consent_given:
                time.sleep(self._config.flush_interval_seconds)
                if self._buffer:
                    self._flush()
        
        thread = threading.Thread(target=_loop, daemon=True, name="kernell-telemetry")
        thread.start()

    # ── Persistence ──────────────────────────────────────────────────

    def _persist_buffer(self, events) -> None:
        """Save events to disk when network is unavailable."""
        if not self._config.buffer_dir:
            return
        try:
            path = Path(self._config.buffer_dir)
            path.mkdir(parents=True, exist_ok=True)
            
            buf_file = path / f"telemetry_buffer_{int(time.time())}.jsonl"
            with open(buf_file, "a") as f:
                for evt in events:
                    data = evt.to_dict() if hasattr(evt, 'to_dict') else evt
                    f.write(json.dumps(data) + "\n")
        except Exception as e:
            logger.debug(f"Failed to persist telemetry buffer: {e}")

    def _load_buffer(self) -> None:
        """Load persisted buffer from disk on startup."""
        if not self._config.buffer_dir:
            return
        try:
            path = Path(self._config.buffer_dir)
            if not path.exists():
                return
            
            for buf_file in sorted(path.glob("telemetry_buffer_*.jsonl")):
                with open(buf_file) as f:
                    for line in f:
                        try:
                            data = json.loads(line.strip())
                            evt = TelemetryEvent(**{
                                k: v for k, v in data.items()
                                if k in TelemetryEvent.__dataclass_fields__
                            })
                            self._buffer.append(evt)
                        except (json.JSONDecodeError, TypeError):
                            continue
                # Delete file after loading
                buf_file.unlink(missing_ok=True)
            
            if self._buffer:
                logger.info(f"Loaded {len(self._buffer)} persisted telemetry events")
        except Exception as e:
            logger.debug(f"Failed to load telemetry buffer: {e}")

    def _purge_persisted(self) -> None:
        """Delete persisted buffer files."""
        if not self._config.buffer_dir:
            return
        try:
            path = Path(self._config.buffer_dir)
            for f in path.glob("telemetry_buffer_*.jsonl"):
                f.unlink(missing_ok=True)
        except Exception as e:
            import logging
            logging.warning(f'Suppressed error in {__name__}: {e}')

    # ── Helpers ───────────────────────────────────────────────────────

    @staticmethod
    def _generate_instance_id() -> str:
        """Generate a random, persistent instance ID."""
        return hashlib.sha256(
            f"{os.getpid()}{time.time()}{os.urandom(16).hex()}".encode()
        ).hexdigest()[:24]

    @staticmethod
    def _bucket_ram(ram_gb: int) -> str:
        """Bucket RAM into privacy-safe categories."""
        if ram_gb <= 4:
            return "4gb"
        elif ram_gb <= 8:
            return "8gb"
        elif ram_gb <= 16:
            return "16gb"
        elif ram_gb <= 32:
            return "32gb"
        else:
            return "64gb+"
