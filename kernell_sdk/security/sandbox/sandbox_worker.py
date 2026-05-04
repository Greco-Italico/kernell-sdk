import os
import sys
import json
from kernell_sdk.security.sandbox.seccomp_profile import install_seccomp

def close_fds():
    max_fd = 1024
    for fd in range(3, max_fd):
        try:
            os.close(fd)
        except OSError as e:
            import logging
            logging.warning(f'Suppressed error in {__name__}: {e}')

def drop_privileges():
    if getattr(os, 'getuid', lambda: -1)() == 0:
        try:
            os.setgid(65534)
            os.setuid(65534)
        except Exception as e:
            print(json.dumps({"error": f"Failed to drop privileges: {e}"}))
            sys.exit(1)

def main():
    close_fds()
    drop_privileges()
    
    install_seccomp()
    
    # Timeout duro de ejecución (CPU/wall clock escape mitigation)
    import signal
    signal.alarm(5)
    
    MAX_INPUT_SIZE = 1024 * 1024 * 5 # 5MB max
    raw = sys.stdin.read(MAX_INPUT_SIZE + 1)
    if not raw:
        sys.exit(1)
    if len(raw) > MAX_INPUT_SIZE:
        print(json.dumps({"error": "Payload too large"}))
        sys.exit(1)
        
    payload = json.loads(raw)
    
    from kernell_sdk.security.intent_firewall import OrchestratorStub, PlanIR, CapabilityToken
    
    plan = PlanIR(**payload["plan"])
    token = CapabilityToken(**payload["token"])
    
    auth_key_b64 = os.environ.get("KERNELL_SANDBOX_AUTH_KEY")
    if not auth_key_b64:
        print(json.dumps({"error": "KERNELL_SANDBOX_AUTH_KEY environment variable is required"}))
        sys.exit(1)
        
    try:
        from cryptography.hazmat.primitives.asymmetric import ed25519
        import base64
        key_bytes = base64.b64decode(auth_key_b64)
        private_key = ed25519.Ed25519PrivateKey.from_private_bytes(key_bytes)
        from kernell_sdk.security.intent_firewall import TokenAuthority
        authority = TokenAuthority(private_key)
    except Exception as e:
        print(json.dumps({"error": f"Invalid KERNELL_SANDBOX_AUTH_KEY: {e}"}))
        sys.exit(1)

    orchestrator = OrchestratorStub(authority=authority)
    result = orchestrator.execute(plan, token)
    
    print(json.dumps(result))

if __name__ == "__main__":
    main()
