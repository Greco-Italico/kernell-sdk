"""
Kernell OS SDK — Agent Persistence & Recovery Engine (Phase 5.5)
════════════════════════════════════════════════════════════════
Provides robust state saving, resumption, and idempotency for agent runs.

Capabilities:
  - Task State Snapshot: captures memory, history, step count, and goal.
  - SQLite-backed Checkpoint Manager: ACID-compliant persistence.
  - Recovery Engine: loads a snapshot and reinstantiates the agent exactly
    where it left off (handling crashes, timeouts, or intentional pauses).
"""

import json
import logging
import sqlite3
import time
from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional

logger = logging.getLogger("kernell.agent.persistence")


class TaskStatus(str, Enum):
    RUNNING = "running"
    PAUSED = "paused"
    COMPLETED = "completed"
    FAILED = "failed"
    INTERRUPTED = "interrupted"


@dataclass
class AgentStateSnapshot:
    """A complete serialization of an Agent's state at a point in time."""
    session_id: str
    goal: str
    status: TaskStatus
    current_step: int
    memory_dump: Dict[str, Any]
    history: List[Dict]
    created_at: float
    updated_at: float
    metadata: Dict[str, Any] = field(default_factory=dict)
    world_model: Optional[Dict[str, Any]] = None

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "AgentStateSnapshot":
        if "status" in data and isinstance(data["status"], str):
            data["status"] = TaskStatus(data["status"])
        return cls(**data)


class CheckpointManager:
    """
    Manages saving and loading Agent state snapshots.
    Uses SQLite to ensure atomicity and prevent state corruption.
    """

    def __init__(self, db_path: str = "/var/lib/kernell/agent_checkpoints.db"):
        self.db_path = db_path
        self._init_db()

    def _init_db(self):
        """Initialize the SQLite database schema."""
        import os
        os.makedirs(os.path.dirname(self.db_path) or ".", exist_ok=True)
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS agent_checkpoints (
                    session_id TEXT PRIMARY KEY,
                    goal TEXT NOT NULL,
                    status TEXT NOT NULL,
                    current_step INTEGER NOT NULL,
                    memory_dump TEXT NOT NULL,
                    history TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    metadata TEXT NOT NULL,
                    world_model TEXT
                )
            """)
            
            # Simple migration for existing DBs
            try:
                conn.execute("ALTER TABLE agent_checkpoints ADD COLUMN world_model TEXT")
            except sqlite3.OperationalError:
                pass # Column exists
                
            conn.commit()

    def save_checkpoint(self, state: AgentStateSnapshot):
        """Save or update a checkpoint for a session."""
        state.updated_at = time.time()
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("""
                INSERT INTO agent_checkpoints (
                    session_id, goal, status, current_step, memory_dump, 
                    history, created_at, updated_at, metadata, world_model
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(session_id) DO UPDATE SET
                    status=excluded.status,
                    current_step=excluded.current_step,
                    memory_dump=excluded.memory_dump,
                    history=excluded.history,
                    updated_at=excluded.updated_at,
                    metadata=excluded.metadata,
                    world_model=excluded.world_model
            """, (
                state.session_id,
                state.goal,
                state.status.value,
                state.current_step,
                json.dumps(state.memory_dump),
                json.dumps(state.history),
                state.created_at,
                state.updated_at,
                json.dumps(state.metadata),
                json.dumps(state.world_model) if state.world_model else None
            ))
            conn.commit()
        logger.debug(f"[CheckpointManager] Saved checkpoint for {state.session_id} at step {state.current_step}")

    def load_checkpoint(self, session_id: str) -> Optional[AgentStateSnapshot]:
        """Load the latest checkpoint for a session."""
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                "SELECT * FROM agent_checkpoints WHERE session_id = ?", 
                (session_id,)
            ).fetchone()

            if not row:
                return None

            return AgentStateSnapshot(
                session_id=row["session_id"],
                goal=row["goal"],
                status=TaskStatus(row["status"]),
                current_step=row["current_step"],
                memory_dump=json.loads(row["memory_dump"]),
                history=json.loads(row["history"]),
                created_at=row["created_at"],
                updated_at=row["updated_at"],
                metadata=json.loads(row["metadata"]),
                world_model=json.loads(row["world_model"]) if row["world_model"] else None
            )

    def list_active_sessions(self) -> List[str]:
        """Return a list of session IDs that are running, paused, or interrupted."""
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.execute(
                "SELECT session_id FROM agent_checkpoints WHERE status IN (?, ?, ?)",
                (TaskStatus.RUNNING.value, TaskStatus.PAUSED.value, TaskStatus.INTERRUPTED.value)
            )
            return [row[0] for row in cursor.fetchall()]
