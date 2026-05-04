"""
Kernell OS SDK — OpenAI Provider
════════════════════════════════
Connector for OpenAI's API. Also highly compatible with vLLM
and other OpenAI-compatible local/remote endpoints.
"""
import os
import json
import logging
from typing import List, AsyncGenerator

from .base import BaseLLMProvider, LLMResponse, LLMMessage
from ..token_estimator import estimate_messages_tokens, estimate_tokens
from ..security.ssrf import create_safe_client, create_safe_async_client, HTTPStatusError

logger = logging.getLogger("kernell.llm.openai")


class OpenAIProvider(BaseLLMProvider):
    """
    Connector for OpenAI API and compatible endpoints (like vLLM).
    """

    def __init__(
        self,
        model: str = "gpt-4o",
        api_key: str = None,
        api_key_env: str = "OPENAI_API_KEY",
        base_url: str = "https://api.openai.com/v1",
        temperature: float = 0.7,
        max_tokens: int = 4096,
        timeout: float = 60.0
    ):
        super().__init__(model, temperature, max_tokens)
        self.timeout = timeout
        self.base_url = base_url.rstrip("/")
        
        # If base_url is not OpenAI, it might be a local vLLM instance
        self.is_local = "api.openai.com" not in self.base_url
        
        # Load API key
        self.api_key = api_key or os.getenv(api_key_env)
        if not self.api_key and not self.is_local:
            logger.warning(f"OpenAI API key not found in environment variable {api_key_env}")
            # If local (vLLM), dummy key is fine
            self.api_key = "dummy" if self.is_local else ""

    def _format_payload(self, messages: List[LLMMessage], **kwargs) -> dict:
        return {
            "model": self.model,
            "max_tokens": kwargs.get("max_tokens", self.max_tokens),
            "temperature": kwargs.get("temperature", self.temperature),
            "messages": [{"role": m.role, "content": m.content} for m in messages],
        }

    def _get_headers(self) -> dict:
        return {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }

    def complete(self, messages: List[LLMMessage], **kwargs) -> LLMResponse:
        """Synchronous call to OpenAI."""
        if not self.api_key:
            raise ValueError("OpenAI API key is not configured.")
            
        payload = self._format_payload(messages, **kwargs)
        url = f"{self.base_url}/chat/completions"
        
        try:
            with create_safe_client(agent_id=self.model, timeout=self.timeout) as client:
                response = client.post(url, headers=self._get_headers(), json=payload)
                response.raise_for_status()
                data = response.json()
                
                content = data["choices"][0]["message"]["content"]
                usage = data.get("usage", {})
                
                p_tokens = usage.get("prompt_tokens", estimate_messages_tokens([{"role": m.role, "content": m.content} for m in messages]))
                c_tokens = usage.get("completion_tokens", estimate_tokens(content))
                
                return LLMResponse(
                    content=content,
                    model_used=self.model,
                    prompt_tokens=p_tokens,
                    completion_tokens=c_tokens,
                    total_tokens=p_tokens + c_tokens,
                    raw_response=data
                )
        except httpx.HTTPStatusError as e:
            logger.error(f"OpenAI API error: {e.response.text}")
            raise
        except Exception as e:
            logger.error(f"OpenAI connection error: {e}")
            raise

    async def complete_async(self, messages: List[LLMMessage], **kwargs) -> LLMResponse:
        """Asynchronous call to OpenAI."""
        if not self.api_key:
            raise ValueError("OpenAI API key is not configured.")
            
        payload = self._format_payload(messages, **kwargs)
        url = f"{self.base_url}/chat/completions"
        
        try:
            async with create_safe_async_client(agent_id=self.model, timeout=self.timeout) as client:
                response = await client.post(url, headers=self._get_headers(), json=payload)
                response.raise_for_status()
                data = response.json()
                
                content = data["choices"][0]["message"]["content"]
                usage = data.get("usage", {})
                
                p_tokens = usage.get("prompt_tokens", estimate_messages_tokens([{"role": m.role, "content": m.content} for m in messages]))
                c_tokens = usage.get("completion_tokens", estimate_tokens(content))
                
                return LLMResponse(
                    content=content,
                    model_used=self.model,
                    prompt_tokens=p_tokens,
                    completion_tokens=c_tokens,
                    total_tokens=p_tokens + c_tokens,
                    raw_response=data
                )
        except httpx.HTTPStatusError as e:
            logger.error(f"OpenAI API error: {e.response.text}")
            raise
        except Exception as e:
            logger.error(f"OpenAI connection error: {e}")
            raise

    async def stream_async(self, messages: List[LLMMessage], **kwargs) -> AsyncGenerator[str, None]:
        """Streaming async call to OpenAI."""
        if not self.api_key:
            raise ValueError("OpenAI API key is not configured.")
            
        payload = self._format_payload(messages, **kwargs)
        payload["stream"] = True
        url = f"{self.base_url}/chat/completions"
        
        try:
            async with create_safe_async_client(agent_id=self.model, timeout=self.timeout) as client:
                async with client.stream("POST", url, headers=self._get_headers(), json=payload) as response:
                    response.raise_for_status()
                    async for line in response.aiter_lines():
                        if line.startswith("data: "):
                            data_str = line[6:]
                            if data_str == "[DONE]":
                                break
                            try:
                                data = json.loads(data_str)
                                choices = data.get("choices", [])
                                if choices and "delta" in choices[0]:
                                    content = choices[0]["delta"].get("content")
                                    if content:
                                        yield content
                            except json.JSONDecodeError:
                                continue
        except Exception as e:
            logger.error(f"OpenAI streaming error: {e}")
            raise

    def estimate_cost(self, prompt_tokens: int, completion_tokens: int) -> float:
        """Estimate cost based on current OpenAI pricing."""
        if self.is_local:
            return 0.0
            
        # GPT-4o
        if self.model == "gpt-4o" or self.model.startswith("gpt-4o-2024"):
            return (prompt_tokens * 5.0 / 1e6) + (completion_tokens * 15.0 / 1e6)
        # GPT-4o-mini
        elif "mini" in self.model:
            return (prompt_tokens * 0.15 / 1e6) + (completion_tokens * 0.60 / 1e6)
        return 0.0
