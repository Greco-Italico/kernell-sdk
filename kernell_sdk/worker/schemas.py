from pydantic import BaseModel

class ExecuteRequest(BaseModel):
    code: str

class ExecuteResponse(BaseModel):
    stdout: str
    stderr: str
    exit_code: int
    execution_time_ms: float
