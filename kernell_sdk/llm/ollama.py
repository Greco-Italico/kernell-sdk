"""
Kernell OS SDK — Ollama Provider (Local-First)
═══════════════════════════════════════════════
Native connector for Ollama, enabling agents and sub-agents to run
locally at $0 cost using models like Gemma 4, Llama 3, and Mistral.
"""
import json
import logging
from typing import List, Any, AsyncGenerator

from .base import BaseLLMProvider, LLMResponse, LLMMessage
from ..token_estimator import estimate_messages_tokens, estimate_tokens
from ..security.ssrf import create_safe_client, create_safe_async_client, RequestError

logger = logging.getLogger("kernell.llm.ollama")


class OllamaProvider(BaseLLMProvider):
    """
    Connector for local Ollama instances.
    """

    def __init__(
        self,
        model: str = "gemma:7b",  # Default local model
        base_url: str = "http://localhost:11434",
        temperature: float = 0.7,
        max_tokens: int = 4096,
        timeout: float = 120.0
    ):
        super().__init__(model, temperature, max_tokens)
        self.base_url = base_url.rstrip("/")
        self.is_local = True
        self.timeout = timeout

    def _format_messages(self, messages: List[LLMMessage]) -> List[dict]:
        return [{"role": m.role, "content": m.content} for m in messages]

    def complete(self, messages: List[LLMMessage], **kwargs) -> LLMResponse:
        """Synchronous call to Ollama."""
        url = f"{self.base_url}/api/chat"
        payload = {
            "model": self.model,
            "messages": self._format_messages(messages),
            "stream": False,
            "options": {
                "temperature": kwargs.get("temperature", self.temperature),
                "num_predict": kwargs.get("max_tokens", self.max_tokens),
            }
        }
        
        logger.debug(f"Calling Ollama local model: {self.model}")
        try:
            with create_safe_client(agent_id=self.model, timeout=self.timeout) as client:
                response = client.post(url, json=payload)
                response.raise_for_status()
                data = response.json()
                
                content = data.get("message", {}).get("content", "")
                
                # Ollama provides prompt_eval_count and eval_count
                p_tokens = data.get("prompt_eval_count", estimate_messages_tokens(self._format_messages(messages)))
                c_tokens = data.get("eval_count", estimate_tokens(content))
                
                return LLMResponse(
                    content=content,
                    model_used=self.model,
                    prompt_tokens=p_tokens,
                    completion_tokens=c_tokens,
                    total_tokens=p_tokens + c_tokens,
                    raw_response=data
                )
        except httpx.RequestError as e:
            logger.error(f"Failed to connect to local Ollama at {self.base_url}: {e}")
            raise RuntimeError(f"Ollama connection error: {e}. Is Ollama running?") from e

    async def complete_async(self, messages: List[LLMMessage], **kwargs) -> LLMResponse:
        """Asynchronous call to Ollama."""
        url = f"{self.base_url}/api/chat"
        payload = {
            "model": self.model,
            "messages": self._format_messages(messages),
            "stream": False,
            "options": {
                "temperature": kwargs.get("temperature", self.temperature),
                "num_predict": kwargs.get("max_tokens", self.max_tokens),
            }
        }
        
        try:
            async with create_safe_async_client(agent_id=self.model, timeout=self.timeout) as client:
                response = await client.post(url, json=payload)
                response.raise_for_status()
                data = response.json()
                
                content = data.get("message", {}).get("content", "")
                
                p_tokens = data.get("prompt_eval_count", estimate_messages_tokens(self._format_messages(messages)))
                c_tokens = data.get("eval_count", estimate_tokens(content))
                
                return LLMResponse(
                    content=content,
                    model_used=self.model,
                    prompt_tokens=p_tokens,
                    completion_tokens=c_tokens,
                    total_tokens=p_tokens + c_tokens,
                    raw_response=data
                )
        except httpx.RequestError as e:
            logger.error(f"Failed to connect to local Ollama at {self.base_url}: {e}")
            raise RuntimeError(f"Ollama connection error: {e}") from e

    async def stream_async(self, messages: List[LLMMessage], **kwargs) -> AsyncGenerator[str, None]:
        """Streaming async call to Ollama."""
        url = f"{self.base_url}/api/chat"
        payload = {
            "model": self.model,
            "messages": self._format_messages(messages),
            "stream": True,
            "options": {
                "temperature": kwargs.get("temperature", self.temperature),
                "num_predict": kwargs.get("max_tokens", self.max_tokens),
            }
        }
        
        try:
            async with create_safe_async_client(agent_id=self.model, timeout=self.timeout) as client:
                async with client.stream("POST", url, json=payload) as response:
                    response.raise_for_status()
                    async for line in response.aiter_lines():
                        if not line:
                            continue
                        try:
                            data = json.loads(line)
                            yield data.get("message", {}).get("content", "")
                        except json.JSONDecodeError:
                            continue
        except httpx.RequestError as e:
            logger.error(f"Ollama streaming error: {e}")
            raise RuntimeError(f"Ollama connection error: {e}") from e

    def estimate_cost(self, prompt_tokens: int, completion_tokens: int) -> float:
        """Local execution is always free."""
        return 0.0
