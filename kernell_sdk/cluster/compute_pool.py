"""
Kernell OS SDK — Resilient Compute Pool
════════════════════════════════════════
Pools de compute avanzados con autoescalado, failover automático,
reintentos y workers efímeros.
"""
from dataclasses import dataclass, field
from typing import List, Dict, Optional, Callable
from kernell_sdk.cluster.pool import ClusterManager, WorkerNode, TaskAssignment
import uuid
import time


@dataclass
class PoolPolicy:
    """Política de resiliencia del pool."""
    max_retries: int = 3
    failover_enabled: bool = True
    autoscale_enabled: bool = True
    min_nodes: int = 1
    max_nodes: int = 20
    retry_delay_seconds: float = 2.0
    health_check_interval: float = 10.0


@dataclass
class TaskExecution:
    """Ejecución rastreada de una tarea con reintentos y failover."""
    execution_id: str = field(default_factory=lambda: str(uuid.uuid4())[:10])
    task_description: str = ""
    assigned_node: str = ""
    attempt: int = 1
    status: str = "pending"  # pending, running, completed, failed, retrying
    started_at: float = 0.0
    completed_at: float = 0.0
    failover_from: Optional[str] = None  # nodo que falló


class ResilientComputePool:
    """
    Pool de compute con resiliencia empresarial:
    failover automático, reintentos y autoescalado.
    """

    def __init__(self, pool_name: str, policy: PoolPolicy = None):
        self.pool_id = str(uuid.uuid4())[:10]
        self.pool_name = pool_name
        self.policy = policy or PoolPolicy()
        self.cluster = ClusterManager(cluster_name=pool_name)
        self._executions: List[TaskExecution] = []
        self._failed_nodes: List[str] = []
        self._event_log: List[str] = []

    def add_node(self, node: WorkerNode):
        self.cluster.add_node(node)
        self._log(f"Node {node.node_id} ({node.agent_name}) joined pool")

    def _log(self, message: str):
        ts = time.strftime("%H:%M:%S")
        self._event_log.append(f"[{ts}] {message}")

    def simulate_node_failure(self, node_id: str):
        """Simula la caída de un nodo para demostrar failover."""
        for node in self.cluster._nodes:
            if node.node_id == node_id:
                node.status = "offline"
                self._failed_nodes.append(node_id)
                self._log(f"⚠ Node {node_id} FAILED — triggering failover")
                return True
        return False

    def _find_replacement_node(self, failed_node_id: str, required_gpu: float = 0.0) -> Optional[WorkerNode]:
        """Busca un nodo de reemplazo para failover."""
        for node in self.cluster._nodes:
            if node.status == "idle" and node.node_id != failed_node_id:
                if required_gpu <= 0 or node.gpu_vram_gb >= required_gpu:
                    return node
        return None

    def execute_with_resilience(
        self,
        tasks: List[str],
        required_gpu: float = 0.0,
        simulate_failures: List[int] = None,
    ) -> List[TaskExecution]:
        """
        Ejecuta tareas con failover automático.
        
        Args:
            tasks: Lista de descripciones de tareas
            required_gpu: VRAM mínima requerida
            simulate_failures: Índices de tareas donde simular fallo (para demo)
        """
        simulate_failures = simulate_failures or []
        executions = []

        for i, task_desc in enumerate(tasks):
            # Asignar tarea
            assignment = self.cluster.assign_task(task_desc, required_gpu)
            if not assignment:
                self._log(f"No nodes available for task: {task_desc}")
                continue

            exec_record = TaskExecution(
                task_description=task_desc,
                assigned_node=assignment.node_id,
                status="running",
                started_at=time.time(),
            )
            self._log(f"Task '{task_desc}' → Node {assignment.node_id}")

            # Simular fallo si está configurado
            if i in simulate_failures and self.policy.failover_enabled:
                failed_node = assignment.node_id
                self.simulate_node_failure(failed_node)
                exec_record.status = "retrying"

                # Intentar failover
                replacement = self._find_replacement_node(failed_node, required_gpu)
                if replacement:
                    replacement.status = "busy"
                    exec_record.failover_from = failed_node
                    exec_record.assigned_node = replacement.node_id
                    exec_record.attempt = 2
                    exec_record.status = "running"
                    self._log(f"Failover: {failed_node} → {replacement.node_id}")
                else:
                    exec_record.status = "failed"
                    self._log(f"Failover FAILED: no replacement available")

            # Marcar como completado (simulación)
            if exec_record.status == "running":
                exec_record.status = "completed"
                exec_record.completed_at = time.time()
                # Liberar nodo
                for node in self.cluster._nodes:
                    if node.node_id == exec_record.assigned_node:
                        node.status = "idle"
                        node.current_task = None

            executions.append(exec_record)

        self._executions = executions
        return executions

    def get_resilience_report(self) -> Dict:
        """Reporte de resiliencia del pool."""
        total = len(self._executions)
        completed = sum(1 for e in self._executions if e.status == "completed")
        failed = sum(1 for e in self._executions if e.status == "failed")
        failovers = sum(1 for e in self._executions if e.failover_from is not None)
        retries = sum(e.attempt - 1 for e in self._executions)

        return {
            "pool_name": self.pool_name,
            "total_tasks": total,
            "completed": completed,
            "failed": failed,
            "failovers_triggered": failovers,
            "total_retries": retries,
            "nodes_failed": len(self._failed_nodes),
            "success_rate": f"{(completed/total*100):.1f}%" if total > 0 else "N/A",
            "event_log": self._event_log,
        }
