from collections import deque
import threading
from typing import Optional, Dict

class TenantQueue:
    def __init__(self, weight: int = 1):
        self.queue = deque()
        self.weight = weight
        self.deficit = 0

class Scheduler:
    def __init__(self):
        self.queues: Dict[str, TenantQueue] = {}
        self.lock = threading.Lock()
        self.active_tenants = []
        self.current_index = 0

    def register_tenant(self, tenant_id: str, weight: int = 1):
        with self.lock:
            if tenant_id not in self.queues:
                self.queues[tenant_id] = TenantQueue(weight)
                self.active_tenants.append(tenant_id)

    def submit(self, request) -> bool:
        tenant_id = request.tenant_id
        with self.lock:
            if tenant_id not in self.queues:
                # Default registration if not pre-registered
                self.queues[tenant_id] = TenantQueue(weight=1)
                self.active_tenants.append(tenant_id)
                
            self.queues[tenant_id].queue.append(request)
            return True

    def next(self) -> Optional[object]:
        with self.lock:
            if not self.active_tenants:
                return None

            # Deficit Round Robin (DRR) implementation
            start_index = self.current_index
            
            while True:
                tenant_id = self.active_tenants[self.current_index]
                q = self.queues[tenant_id]

                # If queue has items, give it its quantum and check deficit
                if q.queue:
                    q.deficit += q.weight
                    
                    if q.deficit >= 1:
                        q.deficit -= 1
                        
                        # Move to next for the subsequent turn to ensure round-robin fairness
                        self.current_index = (self.current_index + 1) % len(self.active_tenants)
                        return q.queue.popleft()
                else:
                    # Reset deficit if queue is empty to prevent hoarding
                    q.deficit = 0
                
                # Move to next tenant
                self.current_index = (self.current_index + 1) % len(self.active_tenants)
                
                # If we've checked everyone and found nothing, return None
                if self.current_index == start_index:
                    return None
