"""
Kernell OS SDK — Agnostic LLM Engine
═════════════════════════════════════
This module provides a unified interface for multiple LLM providers,
enabling the "Local-First" architecture of Kernell OS.

Exports:
    BaseLLMProvider: Abstract base class for all providers.
    OllamaProvider: Native connector for local open-source models.
    AnthropicProvider: Connector for Claude models.
    OpenAIProvider: Connector for GPT models (and vLLM).
    LLMRouter: Hybrid router that delegates tasks based on complexity.
"""
from .base import BaseLLMProvider, LLMResponse, LLMMessage
from .ollama import OllamaProvider
from .anthropic import AnthropicProvider
from .openai import OpenAIProvider
from .router import LLMRouter, ComplexityLevel

__all__ = [
    "BaseLLMProvider",
    "LLMResponse",
    "LLMMessage",
    "OllamaProvider",
    "AnthropicProvider",
    "OpenAIProvider",
    "LLMRouter",
    "ComplexityLevel",
]
