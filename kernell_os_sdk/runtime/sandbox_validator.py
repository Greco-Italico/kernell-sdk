"""
sandbox_validator.py — Kernell OS SDK
Fix #2: AST-based sandbox validator (replaces naive import-string matching).

Rechaza código antes de ejecutarlo si contiene:
  - Imports de módulos peligrosos (ast.Import / ast.ImportFrom)
  - Llamadas a builtins peligrosos (ast.Call con nombres prohibidos)
  - Acceso a atributos de doble guión (__class__, __subclasses__, etc.)
  - Código excesivamente grande (DoS por AST patológico)
"""

from __future__ import annotations

import ast
import textwrap
from dataclasses import dataclass, field
from typing import Optional

# ---------------------------------------------------------------------------
# Configuración de límites (Fix #6 — límites de payload)
# ---------------------------------------------------------------------------

MAX_CODE_BYTES: int = 100_000       # 100 KB máximo de código fuente
MAX_AST_NODES: int = 10_000        # Nodos AST máximos (evita árboles patológicos)
MAX_STRING_LITERAL: int = 1_000_000  # Evita 'a' * 10**9 en strings literales

# ---------------------------------------------------------------------------
# Listas de bloqueo
# ---------------------------------------------------------------------------

FORBIDDEN_NAMES: frozenset[str] = frozenset({
    # Ejecución dinámica de código
    "__import__",
    "eval",
    "exec",
    "compile",
    "__builtins__",
    # I/O del sistema
    "open",
    "input",
    # Introspección y acceso a internos
    "globals",
    "locals",
    "vars",
    "dir",
    "getattr",
    "setattr",
    "delattr",
    "hasattr",
    # Metaclases y manipulación de objetos
    "type",
    "object",
    "super",
    "classmethod",
    "staticmethod",
    "property",
    # Otros peligrosos
    "breakpoint",
    "memoryview",
})

FORBIDDEN_MODULES: frozenset[str] = frozenset({
    "os",
    "sys",
    "subprocess",
    "socket",
    "importlib",
    "pathlib",
    "shutil",
    "tempfile",
    "pty",
    "tty",
    "termios",
    "signal",
    "ctypes",
    "cffi",
    "mmap",
    "resource",
    "pickle",
    "marshal",
    "shelve",
    "dbm",
    "multiprocessing",
    "threading",
    "concurrent",
    "asyncio",
    "selectors",
    "select",
    "ssl",
    "http",
    "urllib",
    "ftplib",
    "smtplib",
    "telnetlib",
    "xmlrpc",
    "wsgiref",
    "email",
    "zipimport",
    "zipfile",
    "tarfile",
    "gzip",
    "bz2",
    "lzma",
    "zlib",
    "struct",
    "fcntl",
    "grp",
    "pwd",
    "spwd",
    "crypt",
    "gc",
    "inspect",
    "dis",
    "code",
    "codeop",
    "pdb",
    "traceback",
    "linecache",
    "tokenize",
    "ast",      # bloquear auto-introspección
    "builtins",
    "_thread",
    "atexit",
})

# Dunders EXPLÍCITAMENTE prohibidos: introspección de clases y acceso a internos.
# Esta lista es estricta (solo lo realmente peligroso) para no romper librerías legítimas.
FORBIDDEN_DUNDER_ATTRS: frozenset[str] = frozenset({
    # Introspección de jerarquía de clases (vector principal de escape)
    "__subclasses__",
    "__bases__",
    "__mro__",
    # Acceso a código y contexto de ejecución
    "__globals__",
    "__code__",
    "__closure__",
    "__builtins__",
    # Loaders e importación dinámica
    "__loader__",
    "__spec__",
    "__import__",
    "__class__",
})

# Dunders PERMITIDOS explícitamente (whitelist para uso en visit_Attribute).
# Librerías legítimas como dataclasses, typing, pytest los usan constantemente.
ALLOWED_DUNDER_ATTRS: frozenset[str] = frozenset({
    # Dunder methods estándar de Python (protocol)
    "__init__", "__new__", "__del__",
    "__repr__", "__str__", "__bytes__", "__format__",
    "__bool__", "__int__", "__float__", "__complex__", "__index__",
    "__len__", "__length_hint__", "__getitem__", "__setitem__", "__delitem__",
    "__missing__", "__iter__", "__reversed__", "__next__", "__contains__",
    "__add__", "__radd__", "__iadd__", "__sub__", "__rsub__", "__isub__",
    "__mul__", "__rmul__", "__imul__", "__truediv__", "__rtruediv__",
    "__floordiv__", "__rfloordiv__", "__mod__", "__rmod__",
    "__pow__", "__rpow__", "__neg__", "__pos__", "__abs__", "__invert__",
    "__lshift__", "__rshift__", "__and__", "__or__", "__xor__",
    "__eq__", "__ne__", "__lt__", "__le__", "__gt__", "__ge__", "__hash__",
    "__call__", "__enter__", "__exit__", "__await__", "__aiter__", "__anext__",
    "__aenter__", "__aexit__",
    # Atributos de instancia comunes (no peligrosos)
    "__name__", "__doc__", "__module__", "__qualname__", "__dict__",
    "__slots__", "__weakref__", "__annotations__",
    # Dataclasses / attrs
    "__dataclass_fields__", "__dataclass_params__",
    # Descriptores
    "__get__", "__set__", "__delete__", "__set_name__",
    # Gestión de contexto de excepción
    "__traceback__", "__cause__", "__context__", "__suppress_context__",
    # pytest y frameworks de test
    "__pytest_mark__",
    # Typing
    "__orig_bases__", "__type_params__",
})

# ---------------------------------------------------------------------------
# Resultado de validación
# ---------------------------------------------------------------------------

@dataclass
class ValidationResult:
    valid: bool
    violations: list[str] = field(default_factory=list)
    node_count: int = 0

    def add(self, msg: str) -> None:
        self.violations.append(msg)
        self.valid = False

    def __str__(self) -> str:
        if self.valid:
            return "OK"
        return "REJECTED:\n" + "\n".join(f"  - {v}" for v in self.violations)


# ---------------------------------------------------------------------------
# Visitor AST principal
# ---------------------------------------------------------------------------

class _ForbiddenNodeVisitor(ast.NodeVisitor):
    """Recorre el AST y acumula violaciones."""

    def __init__(self, result: ValidationResult) -> None:
        self._result = result
        self._node_count = 0

    @property
    def node_count(self) -> int:
        return self._node_count

    def generic_visit(self, node: ast.AST) -> None:
        self._node_count += 1
        if self._node_count > MAX_AST_NODES:
            self._result.add(
                f"AST excede el límite de {MAX_AST_NODES} nodos "
                f"(posible DoS por árbol patológico)"
            )
            raise _TooManyNodes  # abortar el traversal
        super().generic_visit(node)

    # --- Imports -----------------------------------------------------------

    def visit_Import(self, node: ast.Import) -> None:
        for alias in node.names:
            top = alias.name.split(".")[0]
            if top in FORBIDDEN_MODULES:
                self._result.add(
                    f"Línea {node.lineno}: import prohibido → '{alias.name}'"
                )
        self.generic_visit(node)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        module = node.module or ""
        top = module.split(".")[0]
        if top in FORBIDDEN_MODULES:
            self._result.add(
                f"Línea {node.lineno}: from-import prohibido → '{module}'"
            )
        self.generic_visit(node)

    # --- Llamadas a funciones ----------------------------------------------

    def visit_Call(self, node: ast.Call) -> None:
        name = _extract_call_name(node)
        if name and name in FORBIDDEN_NAMES:
            self._result.add(
                f"Línea {node.lineno}: llamada prohibida → {name}()"
            )
        self.generic_visit(node)

    # --- Acceso a atributos ------------------------------------------------

    def visit_Attribute(self, node: ast.Attribute) -> None:
        attr = node.attr

        # 1. Bloquear dunders explícitamente peligrosos (lista negra pequeña y precisa)
        if attr in FORBIDDEN_DUNDER_ATTRS:
            self._result.add(
                f"Línea {node.lineno}: acceso a atributo prohibido → .{attr}"
            )

        # 2. Para cualquier otro dunder: bloquear si NO está en la whitelist.
        #    Esto evita romper librerías legítimas que usan __init__, __len__, etc.
        elif attr.startswith("__") and attr.endswith("__"):
            if attr not in ALLOWED_DUNDER_ATTRS:
                self._result.add(
                    f"Línea {node.lineno}: acceso a dunder desconocido → .{attr} "
                    f"(agregar a ALLOWED_DUNDER_ATTRS si es legítimo)"
                )

        self.generic_visit(node)

    # --- Constantes (evitar strings gigantes) ------------------------------

    def visit_Constant(self, node: ast.Constant) -> None:
        if isinstance(node.value, (str, bytes)):
            size = len(node.value)
            if size > MAX_STRING_LITERAL:
                self._result.add(
                    f"Línea {node.lineno}: literal de string demasiado grande "
                    f"({size:,} bytes > {MAX_STRING_LITERAL:,})"
                )
        self.generic_visit(node)

    # --- Nombres sueltos ---------------------------------------------------

    def visit_Name(self, node: ast.Name) -> None:
        if node.id in FORBIDDEN_NAMES:
            self._result.add(
                f"Línea {node.lineno}: nombre prohibido → '{node.id}'"
            )
        self.generic_visit(node)


class _TooManyNodes(Exception):
    """Señal interna para abortar el traversal del AST."""


# ---------------------------------------------------------------------------
# Función pública principal
# ---------------------------------------------------------------------------

def validate_code(source: str, filename: str = "<sandbox>") -> ValidationResult:
    """
    Valida que `source` sea seguro para ejecutar dentro del sandbox.

    Raises:
        No lanza excepciones — devuelve ValidationResult con valid=False
        si encuentra problemas.

    Ejemplo:
        result = validate_code(user_code)
        if not result.valid:
            raise SandboxViolation(str(result))
    """
    result = ValidationResult(valid=True)

    # 1. Límite de tamaño de código fuente
    code_bytes = len(source.encode("utf-8"))
    if code_bytes > MAX_CODE_BYTES:
        result.add(
            f"Código demasiado grande: {code_bytes:,} bytes "
            f"(máximo {MAX_CODE_BYTES:,})"
        )
        return result  # no intentar parsear código gigante

    # 2. Parseo del AST
    try:
        tree = ast.parse(source, filename=filename)
    except SyntaxError as exc:
        result.add(f"Error de sintaxis: {exc}")
        return result

    # 3. Traversal con visitor
    visitor = _ForbiddenNodeVisitor(result)
    try:
        visitor.visit(tree)
    except _TooManyNodes as e:
        import logging
        logging.warning(f'Suppressed error in {__name__}: {e}')  # la violación ya fue registrada

    result.node_count = visitor.node_count
    return result


# ---------------------------------------------------------------------------
# Excepción pública
# ---------------------------------------------------------------------------

from .errors import SandboxViolation as BaseSandboxViolation

class SandboxViolation(BaseSandboxViolation):
    """Lanzada cuando validate_code() detecta código prohibido."""

    def __init__(self, result: ValidationResult) -> None:
        self.result = result
        super().__init__(str(result))


# ---------------------------------------------------------------------------
# Helpers internos
# ---------------------------------------------------------------------------

def _extract_call_name(node: ast.Call) -> Optional[str]:
    """Extrae el nombre de una llamada simple, ej. exec(...) → 'exec'."""
    func = node.func
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return None
