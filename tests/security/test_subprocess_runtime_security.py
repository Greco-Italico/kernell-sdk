"""SubprocessRuntime production gate (H-07)."""
import os

import pytest

from kernell_sdk.identity import SecurityError
from kernell_sdk.runtime import SubprocessRuntime


def test_subprocess_forbidden_in_production():
    old = os.environ.get("KERNELL_ENV")
    os.environ["KERNELL_ENV"] = "production"
    try:
        with pytest.raises(SecurityError):
            SubprocessRuntime(allow_insecure_exec=True)
    finally:
        if old is None:
            os.environ.pop("KERNELL_ENV", None)
        else:
            os.environ["KERNELL_ENV"] = old
