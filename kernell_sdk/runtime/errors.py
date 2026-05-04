class RuntimeErrorBase(Exception):
    pass

class SandboxViolation(RuntimeErrorBase):
    pass

class ExecutionTimeout(RuntimeErrorBase):
    pass
