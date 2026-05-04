import sys

def install_seccomp():
    try:
        import seccomp
    except ImportError:
        raise RuntimeError("CRITICAL: python3-seccomp is missing. Seccomp isolation cannot be disabled in production. Install python3-seccomp.")

    # Default: KILL process
    filt = seccomp.SyscallFilter(defaction=seccomp.KILL)
    
    # --- ALLOW LIST (mínimo viable) ---
    allowed_syscalls = [
        "read",
        "write",
        "exit",
        "exit_group",
        "brk",
        "mmap",
        "munmap",
        "close",
        "fstat",
        "rt_sigreturn",
        "rt_sigaction",
        "lseek",
        "getpid",
        "gettid",
        "clock_gettime",
    ]
    
    for sc in allowed_syscalls:
        filt.add_rule(seccomp.ALLOW, sc)

    # --- openat con restricciones básicas ---
    filt.add_rule(seccomp.ALLOW, "openat")

    # --- DENY explícito (defensa en profundidad) ---
    blocked = [
        "execve", "socket", "connect", "accept", "accept4",
        "bind", "listen", "ptrace", "clone", "fork", "vfork",
        "kill", "mount", "umount2", "chmod", "chown",
    ]
    
    for sc in blocked:
        filt.add_rule(seccomp.KILL, sc)
            
    filt.load()
    
    # Verificación de que seccomp está activo usando prctl
    try:
        import ctypes
        import os
        libc = ctypes.CDLL(None)
        PR_GET_SECCOMP = 21
        SECCOMP_MODE_FILTER = 2
        mode = libc.prctl(PR_GET_SECCOMP, 0, 0, 0, 0)
        if mode != SECCOMP_MODE_FILTER:
            print("CRITICAL: Seccomp profile was loaded but prctl reports it is not active. Halting.")
            sys.exit(1)
    except Exception as e:
        # Fallback si no podemos usar prctl, pero sabemos que load() pasó.
        # Lo ideal es fallar cerrado si no podemos verificar.
        print(f"CRITICAL: Cannot verify seccomp state: {e}")
        sys.exit(1)
