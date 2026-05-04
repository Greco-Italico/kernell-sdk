import os
import tempfile
import ast
from .errors import SandboxViolation

FORBIDDEN_PATHS = [
    "/etc",
    "/root",
    "/proc",
    "/sys",
    "/var/run/docker.sock"
]

class SandboxFS:
    def __init__(self):
        self._tmp = None

    def __enter__(self):
        self._tmp = tempfile.TemporaryDirectory()
        return self._tmp.name

    def __exit__(self, exc_type, exc, tb):
        if self._tmp:
            self._tmp.cleanup()

FORBIDDEN_NODES = (
    ast.Import,
    ast.ImportFrom,
)

FORBIDDEN_NAMES = {
    "__import__",
    "eval",
    "exec",
    "open",
    "compile",
    "globals",
    "locals",
    "vars",
    "dir",
    "getattr",
    "setattr",
    "delattr",
}

def validate_code(code: str):
    if "\x00" in code:
        raise SandboxViolation("Null byte detected")

    try:
        tree = ast.parse(code)
    except SyntaxError as e:
        raise SandboxViolation(f"Syntax error in submitted code: {e}")
        
    for node in ast.walk(tree):
        if isinstance(node, FORBIDDEN_NODES):
            raise SandboxViolation("Imports are not allowed")
        
        if isinstance(node, ast.Name):
            if node.id in FORBIDDEN_NAMES:
                raise SandboxViolation(f"Forbidden name: {node.id}")
        
        if isinstance(node, ast.Attribute):
            if node.attr.startswith('__') and node.attr.endswith('__'):
                raise SandboxViolation(f"Access to dunder attribute {node.attr} is not allowed")
