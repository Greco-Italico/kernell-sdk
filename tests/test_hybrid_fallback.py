import pytest
import time
from unittest.mock import patch, MagicMock
from kernell_sdk.runtime import HybridRuntime, HybridRuntimeConfig, ExecutionMode
from kernell_sdk.runtime.models import ExecutionRequest, ExecutionResult
from kernell_sdk.runtime.errors import ExecutionTimeout

@pytest.fixture
def mock_runtimes():
    with patch('kernell_sdk.runtime.hybrid_runtime.DockerRuntime') as MockDocker, \
         patch('kernell_sdk.runtime.hybrid_runtime.FirecrackerRuntime') as MockFC, \
         patch('kernell_sdk.runtime.hybrid_runtime.SubprocessRuntime') as MockSub:
        
        # Configure standard success mocks
        fc_instance = MockFC.return_value
        fc_instance.execute.return_value = ExecutionResult(stdout="fc_success", stderr="", exit_code=0)
        
        docker_instance = MockDocker.return_value
        docker_instance.execute.return_value = ExecutionResult(stdout="docker_success", stderr="", exit_code=0)
        
        sub_instance = MockSub.return_value
        sub_instance.execute.return_value = ExecutionResult(stdout="sub_success", stderr="", exit_code=0)
        
        yield {"fc": fc_instance, "docker": docker_instance, "sub": sub_instance}

def test_case_1_firecracker_fails_hard_fallback(mock_runtimes):
    """Test Case 1: Firecracker fails, fallback to constrained."""
    config = HybridRuntimeConfig(
        target_mode=ExecutionMode.ISOLATED,
        fallback_on_failure=True,
        min_required_mode=ExecutionMode.DEBUG
    )
    runtime = HybridRuntime(config)
    
    # Simulate FC crashing
    mock_runtimes["fc"].execute.side_effect = RuntimeError("Firecracker daemon unreachable")
    
    req = ExecutionRequest(code="echo test")
    result = runtime.execute(req)
    
    assert result.stdout == "docker_success"
    assert result._execution_context["fallback_triggered"] is True
    assert result._execution_context["mode"] == ExecutionMode.CONSTRAINED.value

def test_case_2_firecracker_fails_no_fallback(mock_runtimes):
    """Test Case 2: Firecracker fails, fallback is disabled."""
    config = HybridRuntimeConfig(
        target_mode=ExecutionMode.ISOLATED,
        fallback_on_failure=False
    )
    runtime = HybridRuntime(config)
    
    mock_runtimes["fc"].execute.side_effect = RuntimeError("Timeout")
    
    req = ExecutionRequest(code="echo test")
    with pytest.raises(RuntimeError, match="Execution failed in isolated mode: Timeout"):
        runtime.execute(req)

def test_case_3_constrained_memory_limit(mock_runtimes):
    """Test Case 3: Security block on fallback due to min_required_mode."""
    config = HybridRuntimeConfig(
        target_mode=ExecutionMode.ISOLATED,
        fallback_on_failure=True,
        min_required_mode=ExecutionMode.ISOLATED  # Strict crypto config
    )
    runtime = HybridRuntime(config)
    
    # Simulate FC failure
    mock_runtimes["fc"].execute.side_effect = RuntimeError("FC Crash")
    
    # Should NOT fallback to constrained because min is ISOLATED
    req = ExecutionRequest(code="echo test")
    with pytest.raises(RuntimeError, match="FC Crash"):
        runtime.execute(req)

def test_case_4_recursive_fallback(mock_runtimes):
    """Test Case 4: Both FC and Docker fail, fallback to DEBUG."""
    config = HybridRuntimeConfig(
        target_mode=ExecutionMode.ISOLATED,
        fallback_on_failure=True,
        min_required_mode=ExecutionMode.DEBUG
    )
    runtime = HybridRuntime(config)
    
    mock_runtimes["fc"].execute.side_effect = RuntimeError("FC Crash")
    mock_runtimes["docker"].execute.side_effect = RuntimeError("Docker Crash")
    
    req = ExecutionRequest(code="echo test")
    result = runtime.execute(req)
    
    assert result.stdout == "sub_success"
    assert result._execution_context["mode"] == ExecutionMode.DEBUG.value
    assert result._execution_context["fallback_triggered"] is True

def test_case_5_success_path_no_fallback(mock_runtimes):
    """Test Case 5: Happy path execution logic."""
    config = HybridRuntimeConfig(target_mode=ExecutionMode.ISOLATED)
    runtime = HybridRuntime(config)
    
    req = ExecutionRequest(code="echo test")
    result = runtime.execute(req)
    
    assert result.stdout == "fc_success"
    assert result._execution_context["mode"] == ExecutionMode.ISOLATED.value
    assert result._execution_context["fallback_triggered"] is False
