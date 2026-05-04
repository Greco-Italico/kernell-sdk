"""
Kernell OS SDK — Telemetry & Hardware Fingerprinting
════════════════════════════════════════════════════
Binds the agent's execution to a specific physical or virtual machine.
Prevents cloning of agent passports to run botnets on stolen Kernell OS resources.
Collects UDID, IPs, MAC addresses, and Container IDs.
"""
import hashlib
import platform
import socket
import uuid
from typing import Dict, Any

try:
    import psutil
    HAS_PSUTIL = True
except ImportError:
    HAS_PSUTIL = False

class HardwareFingerprint:
    @staticmethod
    def get_mac_address() -> str:
        return ':'.join(['{:02x}'.format((uuid.getnode() >> ele) & 0xff) 
                        for ele in range(0,8*6,8)][::-1])

    @staticmethod
    def get_ip() -> str:
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.connect(("8.8.8.8", 80))
            ip = s.getsockname()[0]
            s.close()
            return ip
        except Exception:
            return "127.0.0.1"

    @staticmethod
    def get_system_udid() -> str:
        """Generates a unique hardware identifier based on immutable host metrics."""
        components = [
            platform.node(),
            platform.machine(),
            platform.processor(),
            HardwareFingerprint.get_mac_address()
        ]
        
        raw_id = "|".join(str(c) for c in components)
        return hashlib.sha256(raw_id.encode()).hexdigest()

    @staticmethod
    def get_telemetry_payload(container_id: str = "host", include_network_data: bool = False) -> Dict[str, Any]:
        """Returns the full telemetry binding data."""
        payload = {
            "udid": HardwareFingerprint.get_system_udid(),
            "os": platform.system(),
            "release": platform.release(),
            "container_id": container_id,
        }
        
        # Solo recolectar datos de red si el usuario lo consiente explícitamente
        if include_network_data:
            payload["ip_address"] = HardwareFingerprint.get_ip()
        
        if HAS_PSUTIL:
            payload["total_ram_gb"] = round(psutil.virtual_memory().total / (1024**3), 2)
            payload["cpu_cores"] = psutil.cpu_count()
            
        return payload
