"""
Kernell OS SDK — Base LLM Provider
═══════════════════════════════════
Defines the common interface that all LLM providers must implement.
This ensures the agent can switch between local (Ollama) and cloud
(Anthropic/OpenAI) engines seamlessly without changing core logic.
"""
import abc
from dataclasses import dataclass, field
from typing import List, Dict, Any, Optional, AsyncGenerator


@dataclass
class LLMMessage:
    """Standardized message format across all providers."""
    role: str  # "system", "user", or "assistant"
    content: str


@dataclass
class LLMResponse:
    """Standardized response format from any provider."""
    content: str
    model_used: str
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    raw_response: Dict[str, Any] = field(default_factory=dict)


class BaseLLMProvider(abc.ABC):
    """
    Abstract base class for all LLM connectors.
    Enforces a consistent interface for the Agent to interact with.
    """

    def __init__(self, model: str, temperature: float = 0.7, max_tokens: int = 4096):
        self.model = model
        self.temperature = temperature
        self.max_tokens = max_tokens
        self.is_local = False  # Subclasses should override this
        
    @abc.abstractmethod
    def complete(self, messages: List[LLMMessage], **kwargs) -> LLMResponse:
        """
        Synchronous completion.
        
        Args:
            messages: List of LLMMessages forming the conversation history.
            **kwargs: Provider-specific overrides.
            
        Returns:
            An LLMResponse containing the text and token usage.
        """
        pass

    @abc.abstractmethod
    async def complete_async(self, messages: List[LLMMessage], **kwargs) -> LLMResponse:
        """
        Asynchronous completion.
        """
        pass

    @abc.abstractmethod
    async def stream_async(self, messages: List[LLMMessage], **kwargs) -> AsyncGenerator[str, None]:
        """
        Asynchronous streaming completion. Yields text chunks as they arrive.
        """
        pass

    def estimate_cost(self, prompt_tokens: int, completion_tokens: int) -> float:
        """
        Estimate the cost of a completion in USD.
        Local providers will return 0.0.
        """
        return 0.0
