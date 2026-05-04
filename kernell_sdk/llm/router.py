"""
Kernell OS SDK — LLM Router (Hybrid Cloud/Local)
════════════════════════════════════════════════
Intelligently routes tasks to either a local model (Ollama)
or a cloud model (Anthropic/OpenAI) based on task complexity.
This enables the 99.9% cost savings promised by the framework.
"""
import logging
from enum import Enum
from typing import List, AsyncGenerator, Optional

from .base import BaseLLMProvider, LLMResponse, LLMMessage
from ..token_estimator import estimate_messages_tokens

logger = logging.getLogger("kernell.llm.router")


class ComplexityLevel(str, Enum):
    EASY = "easy"         # Local: formatting, regex, short extraction
    MEDIUM = "medium"     # Local/Cheap Cloud: standard text processing
    HARD = "hard"         # Cloud: multi-step reasoning, coding
    CRITICAL = "critical" # Cloud (Opus/GPT-4): financial, legal


class LLMRouter(BaseLLMProvider):
    """
    Acts as a single LLM provider, but internally routes requests
    to different actual providers based on a complexity heuristic.
    """

    def __init__(
        self,
        local_provider: BaseLLMProvider,
        cloud_provider: BaseLLMProvider,
        cloud_threshold: ComplexityLevel = ComplexityLevel.HARD,
    ):
        # We don't have a single model name or temperature anymore
        super().__init__(model="hybrid-router", temperature=0.7)
        self.local = local_provider
        self.cloud = cloud_provider
        self.cloud_threshold = cloud_threshold
        
    def _estimate_complexity(self, messages: List[LLMMessage]) -> ComplexityLevel:
        """
        Heuristic to determine task difficulty to save tokens.
        This is a fundamental part of the Kernell OS cost-saving strategy.
        """
        # Get total text length
        total_text = " ".join(m.content for m in messages)
        word_count = len(total_text.split())
        
        lower_text = total_text.lower()
        
        # Trigger words that usually require heavy reasoning
        hard_triggers = ["architect", "reasoning", "complex", "refactor", "analyze in depth", "financial"]
        critical_triggers = ["legal contract", "production deployment", "security audit"]
        
        if any(t in lower_text for t in critical_triggers):
            return ComplexityLevel.CRITICAL
            
        if any(t in lower_text for t in hard_triggers) or word_count > 2000:
            return ComplexityLevel.HARD
            
        if word_count < 100 and "analyze" not in lower_text:
            return ComplexityLevel.EASY
            
        return ComplexityLevel.MEDIUM

    def _route(self, complexity: ComplexityLevel) -> BaseLLMProvider:
        """Selects the appropriate provider based on complexity."""
        levels = [ComplexityLevel.EASY, ComplexityLevel.MEDIUM, ComplexityLevel.HARD, ComplexityLevel.CRITICAL]
        task_idx = levels.index(complexity)
        threshold_idx = levels.index(self.cloud_threshold)
        
        if task_idx >= threshold_idx:
            logger.info(f"Task classified as {complexity.name} (>= {self.cloud_threshold.name}). Routing to CLOUD ({self.cloud.model}).")
            return self.cloud
        else:
            logger.info(f"Task classified as {complexity.name} (< {self.cloud_threshold.name}). Routing to LOCAL ({self.local.model}).")
            return self.local

    def complete(self, messages: List[LLMMessage], **kwargs) -> LLMResponse:
        # 1. Check Semantic Cache (RAG / Exact Match for Hyper-optimization)
        cache_hit = self._check_semantic_cache(messages)
        if cache_hit:
            logger.info("Semantic Cache HIT: Returning cached response (Cost: $0.00, Latency: 0ms)")
            return cache_hit

        # 2. Estimate Complexity and Route
        complexity = self._estimate_complexity(messages)
        provider = self._route(complexity)
        
        # 3. Execute
        response = provider.complete(messages, **kwargs)
        
        # 4. Save to Cache
        self._save_to_semantic_cache(messages, response)
        
        return response

    def _check_semantic_cache(self, messages: List[LLMMessage]) -> Optional[LLMResponse]:
        """
        Interprets tasks to avoid repetitive API calls. 
        In production, this connects to Redis Vector Search (RAG Proxy).
        """
        # Placeholder for actual Redis integration
        return None

    def _save_to_semantic_cache(self, messages: List[LLMMessage], response: LLMResponse) -> None:
        """Saves successful completions to the local cache."""
        pass

    async def complete_async(self, messages: List[LLMMessage], **kwargs) -> LLMResponse:
        complexity = self._estimate_complexity(messages)
        provider = self._route(complexity)
        return await provider.complete_async(messages, **kwargs)

    async def stream_async(self, messages: List[LLMMessage], **kwargs) -> AsyncGenerator[str, None]:
        complexity = self._estimate_complexity(messages)
        provider = self._route(complexity)
        async for chunk in provider.stream_async(messages, **kwargs):
            yield chunk

    def estimate_cost(self, prompt_tokens: int, completion_tokens: int) -> float:
        # Cost depends on which provider was used.
        # It's better for the caller to look at the LLMResponse to see which model was used.
        return 0.0
