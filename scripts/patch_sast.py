import os
import re
import shutil

SDK_DIR = "/home/anny/kernell-os/kernell-os-sdk/kernell_sdk"

def patch_shutil_which():
    """Patches subprocess calls to use shutil.which for absolute paths."""
    print("Patching Path Hijacking (shutil.which)...")
    
    # 1. Patch docker_runtime.py
    docker_rt = os.path.join(SDK_DIR, "runtime", "docker_runtime.py")
    if os.path.exists(docker_rt):
        with open(docker_rt, 'r') as f:
            content = f.read()
        
        if "import shutil" not in content:
            content = "import shutil\n" + content
        
        content = re.sub(
            r'subprocess\.run\(\s*\["docker"',
            'docker_bin = shutil.which("docker")\n        if not docker_bin: raise RuntimeError("docker not found in PATH")\n        subprocess.run([docker_bin',
            content
        )
        with open(docker_rt, 'w') as f:
            f.write(content)

    # 2. Patch firecracker/manager.py
    fc_mgr = os.path.join(SDK_DIR, "runtime", "firecracker", "manager.py")
    if os.path.exists(fc_mgr):
        with open(fc_mgr, 'r') as f:
            content = f.read()
        
        if "import shutil" not in content:
            content = "import shutil\n" + content
            
        content = re.sub(
            r'subprocess\.Popen\(\s*\["firecracker"',
            'fc_bin = shutil.which("firecracker")\n        if not fc_bin: raise RuntimeError("firecracker not found in PATH")\n        process = subprocess.Popen([fc_bin',
            content
        )
        with open(fc_mgr, 'w') as f:
            f.write(content)
            
    # 3. Patch sandbox.py
    sandbox_py = os.path.join(SDK_DIR, "sandbox.py")
    if os.path.exists(sandbox_py):
        with open(sandbox_py, 'r') as f:
            content = f.read()
        if "import shutil" not in content:
            content = "import shutil\n" + content
        content = re.sub(
            r'subprocess\.run\(\s*\["docker"',
            'docker_bin = shutil.which("docker")\n        if not docker_bin: raise RuntimeError("docker not found in PATH")\n        subprocess.run([docker_bin',
            content
        )
        with open(sandbox_py, 'w') as f:
            f.write(content)


def patch_except_pass():
    """Patches except Exception: pass to log errors."""
    print("Patching silent except blocks...")
    
    pattern = re.compile(r'except\s+([A-Za-z0-9_.]+)(?:\s+as\s+[a-zA-Z0-9_]+)?:\s*\n(\s*)pass')
    
    count = 0
    for root, _, files in os.walk(SDK_DIR):
        for file in files:
            if not file.endswith(".py"):
                continue
            path = os.path.join(root, file)
            with open(path, 'r') as f:
                content = f.read()
            
            if "except" in content and "pass" in content:
                new_content = content
                
                # Replace with logging
                def repl(match):
                    exc_type = match.group(1)
                    indent = match.group(2)
                    return f"except {exc_type} as e:\n{indent}import logging\n{indent}logging.warning(f'Suppressed error in {{__name__}}: {{e}}')"
                
                new_content, num = pattern.subn(repl, content)
                if num > 0:
                    with open(path, 'w') as f:
                        f.write(new_content)
                    count += num
    print(f"Patched {count} silent except blocks.")


def patch_tmp_sockets():
    """Patches /tmp/firecracker-*.sock usage"""
    print("Patching /tmp socket usage...")
    fc_mgr = os.path.join(SDK_DIR, "runtime", "firecracker", "manager.py")
    if os.path.exists(fc_mgr):
        with open(fc_mgr, 'r') as f:
            content = f.read()
            
        if 'socket_path = f"/tmp/firecracker-{vm_id}.sock"' in content:
            replacement = '''import tempfile
        import os
        runtime_dir = os.environ.get("XDG_RUNTIME_DIR", "/var/run/kernell")
        try:
            os.makedirs(runtime_dir, mode=0o700, exist_ok=True)
        except OSError:
            runtime_dir = tempfile.gettempdir()
        socket_path = os.path.join(runtime_dir, f"firecracker-{vm_id}.sock")'''
            content = content.replace('socket_path = f"/tmp/firecracker-{vm_id}.sock"', replacement)
            with open(fc_mgr, 'w') as f:
                f.write(content)

if __name__ == "__main__":
    patch_shutil_which()
    patch_except_pass()
    patch_tmp_sockets()
    print("✅ SAST patching complete.")
