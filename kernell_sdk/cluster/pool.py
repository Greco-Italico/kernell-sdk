"""
Kernell OS SDK — Cluster Manager
═════════════════════════════════
Permite a un agente agrupar múltiples nodos/workers en clústeres,
combinar recursos (CPU, GPU, bandwidth), balancear carga y
ofrecer servicios como capacidad agregada.
"""
from dataclasses import dataclass, field
from typing import List, Dict, Optional
import uuid


@dataclass
class WorkerNode:
    """Nodo individual dentro de un clúster."""
    node_id: str = field(default_factory=lambda: str(uuid.uuid4())[:8])
    agent_id: str = ""
    agent_name: str = ""
    cpu_cores: int = 0
    gpu_vram_gb: float = 0.0
    ram_gb: float = 0.0
    bandwidth_mbps: float = 0.0
    region: str = "unknown"
    status: str = "idle"       # idle, busy, offline
    current_task: Optional[str] = None
    load_percent: float = 0.0


@dataclass
class ClusterCapacity:
    """Capacidad agregada de un clúster."""
    total_cpu_cores: int = 0
    total_gpu_vram_gb: float = 0.0
    total_ram_gb: float = 0.0
    total_bandwidth_mbps: float = 0.0
    active_nodes: int = 0
    idle_nodes: int = 0
    busy_nodes: int = 0
    offline_nodes: int = 0


@dataclass
class TaskAssignment:
    """Asignación de una subtarea a un nodo del clúster."""
    task_id: str = field(default_factory=lambda: str(uuid.uuid4())[:8])
    node_id: str = ""
    description: str = ""
    status: str = "assigned"  # assigned, running, completed, failed
    escrow_id: Optional[str] = None


class ClusterManager:
    """
    Gestor de clústeres multiagente.
    Un agente coordinador puede crear pools de workers,
    distribuir trabajo y balancear carga.
    """

    def __init__(self, cluster_name: str = "default"):
        self.cluster_id = str(uuid.uuid4())
        self.cluster_name = cluster_name
        self._nodes: List[WorkerNode] = []
        self._task_assignments: List[TaskAssignment] = []

    def add_node(self, node: WorkerNode):
        """Agrega un nodo worker al clúster."""
        self._nodes.append(node)

    def remove_node(self, node_id: str) -> bool:
        """Retira un nodo del clúster."""
        for i, node in enumerate(self._nodes):
            if node.node_id == node_id:
                self._nodes.pop(i)
                return True
        return False

    def get_capacity(self) -> ClusterCapacity:
        """Calcula la capacidad agregada del clúster."""
        cap = ClusterCapacity()
        for node in self._nodes:
            cap.total_cpu_cores += node.cpu_cores
            cap.total_gpu_vram_gb += node.gpu_vram_gb
            cap.total_ram_gb += node.ram_gb
            cap.total_bandwidth_mbps += node.bandwidth_mbps

            if node.status == "idle":
                cap.idle_nodes += 1
            elif node.status == "busy":
                cap.busy_nodes += 1
            else:
                cap.offline_nodes += 1
            cap.active_nodes = cap.idle_nodes + cap.busy_nodes

        return cap

    def get_idle_nodes(self) -> List[WorkerNode]:
        """Devuelve los nodos disponibles para recibir trabajo."""
        return [n for n in self._nodes if n.status == "idle"]

    def assign_task(self, description: str, required_gpu: float = 0.0) -> Optional[TaskAssignment]:
        """
        Asigna una tarea al nodo idle más adecuado (load balancing simple).
        Prioriza nodos con menor carga y GPU suficiente.
        """
        idle = self.get_idle_nodes()
        if not idle:
            return None

        # Filtrar por GPU si es necesario
        if required_gpu > 0:
            idle = [n for n in idle if n.gpu_vram_gb >= required_gpu]
            if not idle:
                return None

        # Seleccionar el nodo con menor carga
        best = min(idle, key=lambda n: n.load_percent)
        best.status = "busy"

        assignment = TaskAssignment(
            node_id=best.node_id,
            description=description,
            status="running",
        )
        best.current_task = assignment.task_id
        self._task_assignments.append(assignment)
        return assignment

    def distribute_tasks(self, tasks: List[str], required_gpu: float = 0.0) -> List[TaskAssignment]:
        """
        Distribuye múltiples tareas entre los nodos idle del clúster.
        Retorna la lista de asignaciones realizadas.
        """
        assignments = []
        for task_desc in tasks:
            assignment = self.assign_task(task_desc, required_gpu)
            if assignment:
                assignments.append(assignment)
            else:
                break  # No hay más nodos disponibles
        return assignments

    def complete_task(self, task_id: str):
        """Marca una tarea como completada y libera el nodo."""
        for assignment in self._task_assignments:
            if assignment.task_id == task_id:
                assignment.status = "completed"
                # Liberar el nodo
                for node in self._nodes:
                    if node.node_id == assignment.node_id:
                        node.status = "idle"
                        node.current_task = None
                        node.load_percent = max(0, node.load_percent - 25)
                break

    def get_dashboard(self) -> Dict:
        """Genera el dashboard del clúster con todas las métricas."""
        cap = self.get_capacity()
        completed = sum(1 for t in self._task_assignments if t.status == "completed")
        running = sum(1 for t in self._task_assignments if t.status == "running")
        failed = sum(1 for t in self._task_assignments if t.status == "failed")

        return {
            "cluster_id": self.cluster_id,
            "cluster_name": self.cluster_name,
            "total_nodes": len(self._nodes),
            "capacity": {
                "cpu_cores": cap.total_cpu_cores,
                "gpu_vram_gb": cap.total_gpu_vram_gb,
                "ram_gb": cap.total_ram_gb,
                "bandwidth_mbps": cap.total_bandwidth_mbps,
            },
            "node_status": {
                "idle": cap.idle_nodes,
                "busy": cap.busy_nodes,
                "offline": cap.offline_nodes,
            },
            "tasks": {
                "completed": completed,
                "running": running,
                "failed": failed,
            },
        }
