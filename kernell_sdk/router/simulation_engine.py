import heapq
import json
import hashlib
import logging
import copy
from typing import List, Dict, Any, Optional, Callable

try:
    import redis
except ImportError:
    pass

logger = logging.getLogger("kernell.runtime.simulation")

class ExecutionFingerprint:
    @staticmethod
    def from_history(history: list) -> str:
        canonical = [
            {
                "type": h["type"],
                "epoch": h["epoch"],
                "ts": round(h["ts"], 6)  # evitar ruido float
            }
            for h in history
        ]
        serialized = json.dumps(canonical, sort_keys=True)
        return hashlib.sha256(serialized.encode()).hexdigest()

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 1. Event Normalizer (Desacopla el WAL crudo de la semántica de simulación)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class NormalizedEvent:
    def __init__(self, request_id: str, epoch: int, event_type: str, ts: float, payload: Dict[str, Any]):
        self.request_id = request_id
        self.epoch = epoch
        self.type = event_type
        self.ts = ts
        self.payload = payload

    def __repr__(self):
        return f"<Event {self.type} ts={self.ts} epoch={self.epoch} req={self.request_id}>"


class WALEventAdapter:
    def __init__(self, redis_client, stream="kernell:wal"):
        self.r = redis_client
        self.stream = stream

    def fetch_range(self, start="0-0", end="+") -> List[NormalizedEvent]:
        entries = self.r.xrange(self.stream, min=start, max=end)
        return [self._normalize(eid, data) for eid, data in entries]

    def _normalize(self, eid: str, data: Dict[str, Any]) -> NormalizedEvent:
        # Extraer ID puro (si viene formateado)
        req_id = data["request_id"]
        if req_id.startswith("kernell:exec:"):
            req_id = req_id.split("kernell:exec:")[1]
            
        event_type = data.get("event") or data.get("state_after")
        return NormalizedEvent(
            request_id=req_id,
            epoch=int(data.get("epoch", 0)),
            event_type=event_type,
            ts=float(data["ts"]),
            payload=data
        )

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 2. SimulationClock (Tiempo Determinista)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class SimulationClock:
    def __init__(self):
        self._time = 0.0

    def set(self, t: float):
        self._time = t

    def now(self) -> float:
        return self._time

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 3. DeterministicScheduler (Orden total sin race conditions)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class DeterministicScheduler:
    def __init__(self):
        self.queue = []

    def schedule(self, ts: float, fn: Callable):
        heapq.heappush(self.queue, (ts, id(fn), fn))

    def run(self):
        while self.queue:
            ts, _, fn = heapq.heappop(self.queue)
            fn(ts)

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 4. Simulation State (Invariantes puras)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class SimulationState:
    def __init__(self):
        self.executions = {}

    def apply(self, event: NormalizedEvent):
        rid = event.request_id

        current = self.executions.get(rid, {
            "epoch": 0,
            "state": "NEW",
            "history": [],
            "committed": False,
            "last_ts": None,
            "started": False
        })

        if current["last_ts"] is not None and event.ts < current["last_ts"]:
            raise Exception(f"Time regression for {rid}: {event.ts} < {current['last_ts']}")

        if event.epoch < current["epoch"] and event.type not in ("FREEZE", "COMPENSATE"):
            raise Exception(f"Epoch regression {event.epoch} < {current['epoch']}")

        if not current["started"] and event.type not in ("START", "FORCE_SYNC", "FAILOVER"):
            raise Exception(f"Execution without START for {rid}: {event.type}")

        if event.type == "RECLAIM" and current["state"] != "IN_PROGRESS":
            raise Exception(f"Invalid RECLAIM without active execution for {rid}")

        if event.type == "COMMIT" and current["state"] != "IN_PROGRESS":
            raise Exception(f"COMMIT without IN_PROGRESS for {rid}")

        if event.type == "COMMIT":
            if current["committed"]:
                raise Exception(f"Double COMMIT detected for {rid}")
            current["committed"] = True

        if current["state"] in ("COMPLETED", "FROZEN") and event.type not in ("COMMIT", "FORCE_SYNC", "COMPENSATE", "FREEZE"):
            raise Exception(f"Mutation after {current['state']} for {rid}")

        if event.type == "START":
            current["started"] = True
            current["epoch"] = event.epoch
            current["state"] = "IN_PROGRESS"

        elif event.type == "RECLAIM":
            current["epoch"] = event.epoch
            current["state"] = "IN_PROGRESS"

        elif event.type == "COMMIT":
            current["epoch"] = event.epoch
            current["state"] = "COMPLETED"
            current["result_ptr"] = event.payload.get("result_ptr")
            
        elif event.type == "FREEZE":
            current["state"] = "FROZEN"
            current["epoch"] = max(current["epoch"], event.epoch)
            
        elif event.type == "FAILOVER":
            current["epoch"] = event.epoch
            # FAILOVER transfers leadership but doesn't change the underlying execution state
            
        elif event.type == "FORCE_SYNC":
            current["started"] = True
            current["epoch"] = event.epoch
            current["state"] = event.payload.get("state_after", current["state"])
            current["result_ptr"] = event.payload.get("result_ptr", current.get("result_ptr"))
            if current["state"] == "COMPLETED":
                current["committed"] = True
                
        elif event.type == "COMPENSATE":
            current["epoch"] = event.epoch
            current["state"] = "COMPENSATED"
            current["committed"] = False

        current["last_ts"] = event.ts
        current["history"].append({
            "type": event.type,
            "epoch": event.epoch,
            "ts": event.ts
        })

        self.executions[rid] = current

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 5. Simulation Engine (Motor de reproducción)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class TimelineFrame:
    def __init__(self, ts: float, event: NormalizedEvent, state_snapshot: Dict[str, Any], fingerprint: str):
        self.ts = ts
        self.event = event
        self.state = state_snapshot
        self.fingerprint = fingerprint

class SimulationEngine:
    def __init__(self, events: List[NormalizedEvent]):
        self.clock = SimulationClock()
        self.scheduler = DeterministicScheduler()
        self.state = SimulationState()
        self.events = sorted(events, key=lambda e: e.ts)
        self._ptr = 0
        self.timeline: List[TimelineFrame] = []

    def build(self):
        for event in self.events:
            # Capturamos la closure correctamente
            def make_handler(e):
                return lambda ts: self._apply_event(ts, e)
            self.scheduler.schedule(event.ts, make_handler(event))

        self.scheduler.run()

    def _apply_event(self, ts: float, event: NormalizedEvent):
        self.clock.set(ts)
        
        try:
            self.state.apply(event)
        except Exception as e:
            alert_manager.emit(
                "CORRUPTION",
                str(e),
                {"event": event.__dict__}
            )
            raise
        
        current = self.state.executions[event.request_id]
        fp = ExecutionFingerprint.from_history(current["history"])
        
        self.timeline.append(
            TimelineFrame(
                ts=ts,
                event=event,
                state_snapshot=copy.deepcopy(current),
                fingerprint=fp
            )
        )

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 6. Incident Replay & Verificación Formal
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class IncidentReplayer:
    def __init__(self, adapter: WALEventAdapter):
        self.adapter = adapter

    def replay_request(self, request_id: str) -> Optional[Dict[str, Any]]:
        events = self.adapter.fetch_range()
        filtered = [e for e in events if e.request_id == request_id]
        
        if not filtered:
            return None

        engine = SimulationEngine(filtered)
        engine.build()

        return engine.state.executions.get(request_id)

from kernell_sdk.router.execution_resilience import alert_manager

class StateValidator:
    def __init__(self, redis_client):
        self.r = redis_client

    def validate(self, request_id: str, simulated_state: Dict[str, Any]) -> bool:
        key = f"kernell:exec:{request_id}"
        real = self.r.hgetall(key)

        if not real:
            logger.warning(f"Missing real state in Redis for {request_id}")
            return False

        real_epoch = int(real.get("epoch", 0))
        real_state = real.get("state")
        real_fp = real.get("execution_fp") or real.get("fingerprint")
        
        sim_fp = ExecutionFingerprint.from_history(simulated_state.get("history", []))

        if real_epoch != simulated_state["epoch"]:
            raise Exception(f"Epoch mismatch: Real {real_epoch} != Sim {simulated_state['epoch']}")

        if real_state != simulated_state["state"]:
            raise Exception(f"State mismatch: Real {real_state} != Sim {simulated_state['state']}")

        if real_fp and real_fp != sim_fp:
            alert_manager.emit(
                "FINGERPRINT_MISMATCH",
                f"{request_id} diverged",
                {
                    "real": real_fp,
                    "sim": sim_fp
                }
            )
            raise Exception(f"Fingerprint mismatch for {request_id}. Real: {real_fp} != Sim: {sim_fp}")

        return True
