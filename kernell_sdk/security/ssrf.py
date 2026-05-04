import socket
import ipaddress
import urllib.parse
from httpx import Request, TransportError, URL, RequestError, HTTPStatusError, TimeoutException
import httpx
import structlog

logger = structlog.get_logger("kernell.security.ssrf")

# Blocked IP ranges (RFC 1918, Link-local, Loopback, etc.)
BLOCKED_SUBNETS = [
    ipaddress.ip_network('127.0.0.0/8'),
    ipaddress.ip_network('10.0.0.0/8'),
    ipaddress.ip_network('172.16.0.0/12'),
    ipaddress.ip_network('192.168.0.0/16'),
    ipaddress.ip_network('169.254.0.0/16'),
    ipaddress.ip_network('::1/128'),
    ipaddress.ip_network('fc00::/7'),
    ipaddress.ip_network('0.0.0.0/8'),
]

# Sensitive ports often targeted by SSRF
BLOCKED_PORTS = {22, 2375, 2376, 3306, 5432, 6379, 11211, 27017}

def _is_port_allowed(port: int) -> bool:
    if port in BLOCKED_PORTS:
        return False
    if 8000 <= port <= 9000:
        return False
    return True

class SSRFViolation(Exception):
    """Raised when an SSRF attempt is detected."""
    pass

def _is_ip_allowed(ip_str: str) -> bool:
    try:
        ip = ipaddress.ip_address(ip_str)
        for subnet in BLOCKED_SUBNETS:
            if ip in subnet:
                return False
        return True
    except ValueError:
        return False

def _resolve_and_validate(hostname: str, agent_id: str = "unknown") -> str:
    """Resolve hostname and ensure it doesn't point to a private IP."""
    if hostname.lower() in ('localhost', 'localhost.localdomain'):
        logger.warning("ssrf_blocked_localhost_domain", host=hostname, agent_id=agent_id)
        raise SSRFViolation("Localhost domains are blocked.")

    try:
        # Resolve all IPs for the host
        addr_info = socket.getaddrinfo(hostname, None)
        ips = [info[4][0] for info in addr_info]
        
        if not ips:
            raise SSRFViolation(f"Could not resolve {hostname}")
            
        for ip in ips:
            if not _is_ip_allowed(ip):
                logger.warning("ssrf_blocked_dns_resolution", host=hostname, resolved_ip=ip, agent_id=agent_id)
                raise SSRFViolation(f"Hostname {hostname} resolves to blocked IP: {ip}")
                
        # Return the first resolved IP for binding (DNS rebinding protection)
        return ips[0]
    except socket.gaierror:
        raise SSRFViolation(f"DNS resolution failed for {hostname}")


class SSRFSafeTransport(httpx.HTTPTransport):
    """
    Custom HTTPX Transport that enforces SSRF protection.
    It resolves the DNS, validates the IP, and forces the request to connect to the validated IP.
    """
    def __init__(self, agent_id: str = "unknown", *args, **kwargs):
        self.agent_id = agent_id
        super().__init__(*args, **kwargs)
        
    def handle_request(self, request: Request) -> httpx.Response:
        url = request.url
        
        scheme = url.scheme
        if scheme not in ("http", "https"):
            logger.warning("ssrf_blocked_scheme", scheme=scheme, agent_id=self.agent_id)
            raise SSRFViolation(f"Blocked scheme: {scheme}. Only http/https are allowed.")
            
        port = url.port or (443 if url.scheme == "https" else 80)
        if not _is_port_allowed(port):
            logger.warning("ssrf_blocked_port", port=port, host=url.host, agent_id=self.agent_id)
            raise SSRFViolation(f"Blocked sensitive port: {port}")
            
        hostname = url.host
        
        # 1. Pre-validation (Check if the URL host itself is a blocked IP)
        if not _is_ip_allowed(hostname):
            # If it's an IP, it failed the check. If it's a hostname, we proceed to DNS resolution.
            try:
                ipaddress.ip_address(hostname)
                logger.warning("ssrf_blocked_direct_ip", host=hostname, agent_id=self.agent_id)
                raise SSRFViolation(f"Direct IP access to {hostname} is blocked.")
            except ValueError as e:
                import logging
                logging.warning(f'Suppressed error in {__name__}: {e}') # It's a hostname, proceed to resolution
                
        # 2. DNS Resolution and IP Validation
        safe_ip = _resolve_and_validate(hostname, self.agent_id)
        
        # 3. DNS Rebinding Protection: Force the transport to use the safe_ip we just resolved
        # We modify the request URL to use the IP, but keep the original Host header
        original_host = request.headers.get("Host", hostname)
        request.headers["Host"] = original_host
        
        # Create a new URL using the safe IP, but retaining the scheme, port, and path
        safe_url = url.copy_with(host=safe_ip)
        request.url = safe_url
        
        # 4. Perform the actual request
        response = super().handle_request(request)
        
        # 5. Check redirects (httpx handles redirects at the client level, but if transport handles it)
        # Note: If the client follows redirects, it will invoke handle_request again for the new URL.
        # This naturally protects against redirect-based SSRF because the new URL goes through the same checks.
        
        return response

class SSRFSafeAsyncTransport(httpx.AsyncHTTPTransport):
    """Async variant of the SSRF safe transport."""
    def __init__(self, agent_id: str = "unknown", *args, **kwargs):
        self.agent_id = agent_id
        super().__init__(*args, **kwargs)

    async def handle_async_request(self, request: Request) -> httpx.Response:
        url = request.url
        
        scheme = url.scheme
        if scheme not in ("http", "https"):
            logger.warning("ssrf_blocked_scheme", scheme=scheme, agent_id=self.agent_id)
            raise SSRFViolation(f"Blocked scheme: {scheme}. Only http/https are allowed.")
            
        port = url.port or (443 if url.scheme == "https" else 80)
        if not _is_port_allowed(port):
            logger.warning("ssrf_blocked_port", port=port, host=url.host, agent_id=self.agent_id)
            raise SSRFViolation(f"Blocked sensitive port: {port}")

        hostname = url.host
        
        if not _is_ip_allowed(hostname):
            try:
                ipaddress.ip_address(hostname)
                logger.warning("ssrf_blocked_direct_ip", host=hostname, agent_id=self.agent_id)
                raise SSRFViolation(f"Direct IP access to {hostname} is blocked.")
            except ValueError as e:
                import logging
                logging.warning(f'Suppressed error in {__name__}: {e}')
                
        safe_ip = _resolve_and_validate(hostname, self.agent_id)
        
        original_host = request.headers.get("Host", hostname)
        request.headers["Host"] = original_host
        
        safe_url = url.copy_with(host=safe_ip)
        request.url = safe_url
        
        response = await super().handle_async_request(request)
        return response

def create_safe_client(agent_id: str = "unknown", **kwargs) -> httpx.Client:
    """Creates a secure HTTP client protected against SSRF."""
    if kwargs.get("verify") is False:
        raise ValueError("Security Policy Violation: verify=False is strictly prohibited.")
    
    # Do not allow custom transports unless they are our safe ones
    if "transport" in kwargs and not isinstance(kwargs["transport"], (SSRFSafeTransport, getattr(httpx, "SSRFSafeUDSTransport", type("Dummy", (), {})))):
        pass # Will be handled below when we inject SSRFSafeTransport
        
    kwargs['transport'] = SSRFSafeTransport(agent_id=agent_id)
    kwargs['follow_redirects'] = True # Ensure redirects are followed safely
    return httpx.Client(**kwargs)

class SSRFSafeUDSTransport(httpx.HTTPTransport):
    """Transport strictly for UNIX Domain Sockets. Immune to IP SSRF."""
    def __init__(self, uds_path: str, agent_id: str = "system", *args, **kwargs):
        self.uds_path = uds_path
        self.agent_id = agent_id
        kwargs["uds"] = uds_path
        super().__init__(*args, **kwargs)

def create_uds_client(uds_path: str, agent_id: str = "system", **kwargs) -> httpx.Client:
    """Creates a secure HTTP client for UDS communication."""
    kwargs["transport"] = SSRFSafeUDSTransport(uds_path=uds_path, agent_id=agent_id)
    return httpx.Client(**kwargs)

def create_safe_async_client(agent_id: str = "unknown", **kwargs) -> httpx.AsyncClient:
    """Creates a secure Async HTTP client protected against SSRF."""
    if kwargs.get("verify") is False:
        raise ValueError("Security Policy Violation: verify=False is strictly prohibited.")
        
    if "transport" in kwargs and not isinstance(kwargs["transport"], SSRFSafeAsyncTransport):
        raise ValueError("Security Policy Violation: Custom HTTP transports are prohibited.")
        
    kwargs['transport'] = SSRFSafeAsyncTransport(agent_id=agent_id)
    kwargs['follow_redirects'] = True
    return httpx.AsyncClient(**kwargs)
