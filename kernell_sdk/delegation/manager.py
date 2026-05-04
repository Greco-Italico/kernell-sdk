"""
Kernell OS SDK — Sub-Agent Manager
══════════════════════════════════
Manages the dynamic creation ("spawn") and destruction of local worker
sub-agents. This is the core of the "Hybrid Swarm" architecture.
"""
import uuid
import time
import logging
import threading
from typing import List, Dict, Any, Optional, Callable
from concurrent.futures import ThreadPoolExecutor, as_completed

from ..agent import Agent, AgentPermissions
from ..llm.base import BaseLLMProvider
from ..memory import Memory

logger = logging.getLogger("kernell.delegation.manager")


class SubAgentWorker:
    """A lightweight worker agent created dynamically for specific tasks."""
    
    def __init__(
        self,
        parent_id: str,
        engine: BaseLLMProvider,
        memory: Memory,
        timeout: float = 60.0
    ):
        self.id = f"{parent_id}_worker_{uuid.uuid4().hex[:8]}"
        self.engine = engine
        self.memory = memory
        self.timeout = timeout
        self.is_busy = False
        
    def execute(self, task: str) -> str:
        """Execute a single task using the assigned engine."""
        self.is_busy = True
        logger.debug(f"Worker {self.id} starting task: {task[:50]}...")
        
        start_time = time.time()
        
        from ..llm import LLMMessage
        messages = [
            LLMMessage(role="system", content="You are a specialized Kernell OS worker sub-agent. Complete the task accurately and concisely. Return ONLY the requested data without conversational filler."),
            LLMMessage(role="user", content=task)
        ]
        
        try:
            # We use synchronous execution here since we'll wrap it in a ThreadPoolExecutor
            response = self.engine.complete(messages, max_tokens=2048)
            result = response.content
            
            # Store result in shared Cortex Memory
            # We use the parent's memory instance so the main agent can read it
            duration = time.time() - start_time
            self.memory.add_episodic("worker_completed", {
                "worker_id": self.id,
                "task": task[:100],
                "result_preview": result[:200],
                "duration_sec": round(duration, 2),
                "model": response.model_used
            })
            
            return result
            
        except Exception as e:
            logger.error(f"Worker {self.id} failed: {e}")
            self.memory.add_episodic("worker_failed", {"worker_id": self.id, "error": str(e)})
            return f"Error executing task: {e}"
        finally:
            self.is_busy = False


class SubAgentManager:
    """
    Manages a pool of lightweight sub-agents for executing delegated tasks.
    """
    
    def __init__(self, parent_agent: Agent):
        self.parent = parent_agent
        self.workers: List[SubAgentWorker] = []
        self._max_workers = 0
        self._engine: Optional[BaseLLMProvider] = None
        self._is_enabled = False
        
    def enable(self, max_workers: int, worker_engine: BaseLLMProvider, timeout: float = 60.0):
        """Enable delegation and initialize the worker pool."""
        self._max_workers = max_workers
        self._engine = worker_engine
        self._is_enabled = True
        
        # Pre-spawn workers to avoid latency during execution
        logger.info(f"Spawning {max_workers} local workers using engine: {worker_engine.model}")
        for _ in range(max_workers):
            worker = SubAgentWorker(
                parent_id=self.parent.id,
                engine=self._engine,
                memory=self.parent.memory,
                timeout=timeout
            )
            self.workers.append(worker)
            
    def disable(self):
        """Destroy all workers and disable delegation."""
        self.workers.clear()
        self._is_enabled = False
        logger.info("Sub-agent delegation disabled. Workers destroyed.")
        
    def is_enabled(self) -> bool:
        return self._is_enabled

    def execute_batch(self, tasks: List[str], max_concurrent: int = None) -> List[str]:
        """
        Execute a batch of tasks using the worker pool in parallel.
        Returns the list of results in the same order as the input tasks.
        """
        if not self._is_enabled:
            raise RuntimeError("Sub-agent delegation is not enabled. Call enable() first.")
            
        if not tasks:
            return []
            
        concurrent = min(len(tasks), max_concurrent or self._max_workers)
        logger.info(f"Executing {len(tasks)} tasks using {concurrent} concurrent workers.")
        
        # Map tasks to their original index so we can return results in order
        task_map = {i: task for i, task in enumerate(tasks)}
        results = [None] * len(tasks)
        
        def _process_task(index, task):
            # Find an available worker (simple round-robin/first-available)
            # In a ThreadPool, we don't strictly bind to specific worker instances
            # to avoid complex locking, we just use them as configuration templates.
            worker = self.workers[index % len(self.workers)]
            return index, worker.execute(task)

        with ThreadPoolExecutor(max_workers=concurrent) as executor:
            # Submit all tasks
            futures = [executor.submit(_process_task, idx, task) for idx, task in task_map.items()]
            
            # Wait for completion
            for future in as_completed(futures):
                try:
                    idx, result = future.result()
                    results[idx] = result
                except Exception as e:
                    logger.error(f"Batch execution error: {e}")
                    # Find which future failed (harder with as_completed, but we know something failed)
                    
        return results
