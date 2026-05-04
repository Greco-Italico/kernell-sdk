import os
import shutil
import pytest
from kernell_sdk.runtime import SubprocessRuntime, DockerRuntime, ExecutionRequest, SandboxViolation

def has_docker():
    return shutil.which("docker") is not None

def has_nsjail():
    return shutil.which("nsjail") is not None

@pytest.fixture(params=["subprocess", "docker"])
def runtime(request):
    if request.param == "subprocess":
        if not has_nsjail():
            pytest.skip("nsjail not installed, skipping SubprocessRuntime tests")
        old = os.environ.get("KERNELL_ALLOW_UNSAFE_SUBPROCESS_RUNTIME")
        os.environ["KERNELL_ALLOW_UNSAFE_SUBPROCESS_RUNTIME"] = "1"
        try:
            yield SubprocessRuntime(allow_insecure_exec=True)
        finally:
            if old is None:
                os.environ.pop("KERNELL_ALLOW_UNSAFE_SUBPROCESS_RUNTIME", None)
            else:
                os.environ["KERNELL_ALLOW_UNSAFE_SUBPROCESS_RUNTIME"] = old
    else:
        if not has_docker():
            pytest.skip("Docker not installed or not running, skipping DockerRuntime tests")
        yield DockerRuntime()

def test_safe_execution(runtime):
    """Positive test path: ensures the sandbox allows legitimate safe code."""
    code = "print(2 + 2)"
    req = ExecutionRequest(code=code, timeout=2)
    result = runtime.execute(req)
    assert result.exit_code == 0
    assert "4" in result.stdout

def test_bypass_import(runtime):
    code = "__builtins__.__dict__['__import__']('os').system('id')"
    req = ExecutionRequest(code=code, timeout=2)
    with pytest.raises(SandboxViolation) as exc:
        runtime.execute(req)
    assert exc.value is not None

def test_indirect_sys_access(runtime):
    code = "().__class__.__base__.__subclasses__()"
    req = ExecutionRequest(code=code, timeout=2)
    with pytest.raises(SandboxViolation) as exc:
        runtime.execute(req)
    assert exc.value is not None

def test_file_system_escape(runtime):
    code = "open('/etc/passwd').read()"
    req = ExecutionRequest(code=code, timeout=2)
    with pytest.raises(SandboxViolation) as exc:
        runtime.execute(req)
    assert exc.value is not None

def test_memory_bomb(runtime):
    code = "a = 'A' * (10**9)" # ~1GB
    req = ExecutionRequest(code=code, timeout=2, memory_limit_mb=128)
    try:
        result = runtime.execute(req)
        assert "MemoryError" in result.stderr or result.exit_code != 0
    except SandboxViolation:
        # If the AST statically blocks large literals or loops, that's also a PASS
        pass

def test_cpu_burn(runtime):
    code = "while True:\n    pass"
    req = ExecutionRequest(code=code, timeout=1) # 1 sec
    try:
        result = runtime.execute(req)
        assert result.timed_out is True
    except SandboxViolation:
        # If AST blocks infinite loops statically, that's also a PASS
        pass

def test_fork_bomb(runtime):
    code = "import os\nwhile True:\n    os.fork()"
    req = ExecutionRequest(code=code, timeout=2)
    with pytest.raises(SandboxViolation):
        runtime.execute(req)

def test_ast_import_block(runtime):
    code = "import subprocess"
    req = ExecutionRequest(code=code, timeout=2)
    with pytest.raises(SandboxViolation):
        runtime.execute(req)
