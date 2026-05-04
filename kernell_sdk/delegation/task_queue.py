"""
Kernell OS SDK — Task Queue
═══════════════════════════
Thread-safe task queue for distributing workloads across
multiple sub-agent workers dynamically.
"""
import queue
import logging
from typing import Optional, Any

logger = logging.getLogger("kernell.delegation.queue")


class TaskQueue:
    """
    A simple thread-safe queue wrapper for distributing tasks
    to worker sub-agents.
    """
    
    def __init__(self, maxsize: int = 0):
        self._queue = queue.Queue(maxsize=maxsize)
        self._total_added = 0
        self._total_processed = 0

    def add(self, task: Any) -> None:
        """Add a task to the queue."""
        self._queue.put(task)
        self._total_added += 1

    def get(self, block: bool = True, timeout: Optional[float] = None) -> Any:
        """Get the next task. Blocks if empty and block=True."""
        try:
            return self._queue.get(block=block, timeout=timeout)
        except queue.Empty:
            return None

    def mark_done(self) -> None:
        """Indicate that a formerly enqueued task is complete."""
        self._queue.task_done()
        self._total_processed += 1

    def wait_until_complete(self) -> None:
        """Block until all tasks in the queue have been processed."""
        self._queue.join()

    @property
    def size(self) -> int:
        """Current number of items in the queue."""
        return self._queue.qsize()

    @property
    def is_empty(self) -> bool:
        """Check if the queue is empty."""
        return self._queue.empty()
        
    def stats(self) -> dict:
        """Return queue statistics."""
        return {
            "current_size": self.size,
            "total_added": self._total_added,
            "total_processed": self._total_processed,
            "remaining": self._total_added - self._total_processed
        }
