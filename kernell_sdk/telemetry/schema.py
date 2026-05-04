from pydantic import BaseModel, Field, validator
from typing import Optional, Literal
import time

class Task(BaseModel):
    type: Literal["llm"]
    input_hash: str
    input_preview: Optional[str]

class Features(BaseModel):
    input_tokens: int
    expected_output_tokens: int
    complexity_score: float = Field(ge=0.0, le=1.0)
    priority: Literal["low", "normal", "high"]

class Decision(BaseModel):
    tier: Literal["ECONOMIC", "BALANCED", "PREMIUM"]
    model: str
    provider: str
    confidence: float = Field(ge=0.0, le=1.0)

class Execution(BaseModel):
    success: bool
    latency_ms: float
    cost_usd: float
    retries: int = Field(ge=0)

class Consensus(BaseModel):
    score: float = Field(ge=0.0, le=1.0)
    method: str

class ShadowDecision(BaseModel):
    tier: Literal["ECONOMIC", "BALANCED", "PREMIUM"]
    model: str
    confidence: float = Field(ge=0.0, le=1.0)

class Shadow(BaseModel):
    enabled: bool
    decision: Optional[ShadowDecision]

class Meta(BaseModel):
    sdk_version: str
    env: Literal["production", "staging", "dev"]
    client_id: str

class TelemetryEvent(BaseModel):
    trace_id: str
    timestamp: int
    task: Task
    features: Features
    decision: Decision
    execution: Execution
    consensus: Consensus
    shadow: Optional[Shadow]
    meta: Meta

    @validator("timestamp")
    def validate_timestamp(cls, v):
        if v > int(time.time()) + 60:
            raise ValueError("timestamp in future")
        return v
