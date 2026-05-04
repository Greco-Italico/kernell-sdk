"""
Kernell OS SDK — Anthropic Provider
═══════════════════════════════════
Connector for Anthropic's Claude models via their Messages API.
Used for "HARD" and "CRITICAL" reasoning tasks in the hybrid cluster.
"""
import os
import json
import logging
from typing import List, AsyncGenerator

from .base import BaseLLMProvider, LLMResponse, LLMMessage
from ..security.ssrf import create_safe_client, create_safe_async_client, HTTPStatusError

logger = logging.getLogger("kernell.llm.anthropic")

# Fallback token estimator if the API doesn't return usage
from ..token_estimator import estimate_messages_tokens, estimate_tokens


class AnthropicProvider(BaseLLMProvider):
    """
    Connector for Anthropic API.
    """

    def __init__(
        self,
        model: str = "claude-3-5-sonnet-20241022",
        api_key: str = None,
        api_key_env: str = "ANTHROPIC_API_KEY",
        temperature: float = 0.7,
        max_tokens: int = 4096,
        timeout: float = 60.0
    ):
        super().__init__(model, temperature, max_tokens)
        self.is_local = False
        self.timeout = timeout
        
        # Load API key
        self.api_key = api_key or os.getenv(api_key_env)
        if not self.api_key:
            logger.warning(f"Anthropic API key not found in environment variable {api_key_env}")

        self.base_url = "https://api.anthropic.com/v1/messages"
        
    def _format_payload(self, messages: List[LLMMessage], **kwargs) -> dict:
        """Formats the messages according to the Anthropic Messages API."""
        system_prompt = ""
        anthropic_messages = []
        
        for msg in messages:
            if msg.role == "system":
                system_prompt += msg.content + "\n"
            else:
                anthropic_messages.append({"role": msg.role, "content": msg.content})
                
        payload = {
            "model": self.model,
            "max_tokens": kwargs.get("max_tokens", self.max_tokens),
            "temperature": kwargs.get("temperature", self.temperature),
            "messages": anthropic_messages,
        }
        if system_prompt:
            payload["system"] = system_prompt.strip()
            
        return payload

    def _get_headers(self) -> dict:
        return {
            "x-api-key": self.api_key,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        }

    def complete(self, messages: List[LLMMessage], **kwargs) -> LLMResponse:
        """Synchronous call to Anthropic."""
        if not self.api_key:
            raise ValueError("Anthropic API key is not configured.")
            
        payload = self._format_payload(messages, **kwargs)
        
        try:
            with create_safe_client(agent_id=self.model, timeout=self.timeout) as client:
                response = client.post(
                    self.base_url,
                    headers=self._get_headers(),
                    json=payload
                )
                response.raise_for_status()
                data = response.json()
                
                content = data["content"][0]["text"]
                usage = data.get("usage", {})
                
                p_tokens = usage.get("input_tokens", estimate_messages_tokens([{"role": m.role, "content": m.content} for m in messages]))
                c_tokens = usage.get("output_tokens", estimate_tokens(content))
                
                return LLMResponse(
                    content=content,
                    model_used=self.model,
                    prompt_tokens=p_tokens,
                    completion_tokens=c_tokens,
                    total_tokens=p_tokens + c_tokens,
                    raw_response=data
                )
        except httpx.HTTPStatusError as e:
            logger.error(f"Anthropic API error: {e.response.text}")
            raise
        except Exception as e:
            logger.error(f"Anthropic connection error: {e}")
            raise

    async def complete_async(self, messages: List[LLMMessage], **kwargs) -> LLMResponse:
        """Asynchronous call to Anthropic."""
        if not self.api_key:
            raise ValueError("Anthropic API key is not configured.")
            
        payload = self._format_payload(messages, **kwargs)
        
        try:
            async with create_safe_async_client(agent_id=self.model, timeout=self.timeout) as client:
                response = await client.post(
                    self.base_url,
                    headers=self._get_headers(),
                    json=payload
                )
                response.raise_for_status()
                data = response.json()
                
                content = data["content"][0]["text"]
                usage = data.get("usage", {})
                
                p_tokens = usage.get("input_tokens", estimate_messages_tokens([{"role": m.role, "content": m.content} for m in messages]))
                c_tokens = usage.get("output_tokens", estimate_tokens(content))
                
                return LLMResponse(
                    content=content,
                    model_used=self.model,
                    prompt_tokens=p_tokens,
                    completion_tokens=c_tokens,
                    total_tokens=p_tokens + c_tokens,
                    raw_response=data
                )
        except httpx.HTTPStatusError as e:
            logger.error(f"Anthropic API error: {e.response.text}")
            raise
        except Exception as e:
            logger.error(f"Anthropic connection error: {e}")
            raise

    async def stream_async(self, messages: List[LLMMessage], **kwargs) -> AsyncGenerator[str, None]:
        """Streaming async call to Anthropic."""
        if not self.api_key:
            raise ValueError("Anthropic API key is not configured.")
            
        payload = self._format_payload(messages, **kwargs)
        payload["stream"] = True
        
        try:
            async with create_safe_async_client(agent_id=self.model, timeout=self.timeout) as client:
                async with client.stream("POST", self.base_url, headers=self._get_headers(), json=payload) as response:
                    response.raise_for_status()
                    async for line in response.aiter_lines():
                        if line.startswith("data: "):
                            data_str = line[6:]
                            if data_str == "[DONE]":
                                break
                            try:
                                data = json.loads(data_str)
                                if data.get("type") == "content_block_delta":
                                    yield data["delta"].get("text", "")
                            except json.JSONDecodeError:
                                continue
        except Exception as e:
            logger.error(f"Anthropic streaming error: {e}")
            raise

    def estimate_cost(self, prompt_tokens: int, completion_tokens: int) -> float:
        """Estimate cost based on current Anthropic pricing."""
        # Claude 3.5 Sonnet pricing
        if "sonnet" in self.model:
            return (prompt_tokens * 3.0 / 1e6) + (completion_tokens * 15.0 / 1e6)
        # Claude 3 Opus
        elif "opus" in self.model:
            return (prompt_tokens * 15.0 / 1e6) + (completion_tokens * 75.0 / 1e6)
        # Claude 3 Haiku
        elif "haiku" in self.model:
            return (prompt_tokens * 0.25 / 1e6) + (completion_tokens * 1.25 / 1e6)
        return 0.0
