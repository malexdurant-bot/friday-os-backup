"""
Xcerebro 2.1 — Provider Adapters

Thin adapters that normalize calls across Anthropic, OpenAI, DeepSeek, Groq.
Each adapter takes a unified input format and returns a unified output.

The router picks WHICH provider; the adapter handles HOW to call it.
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Optional, List, Dict, Any
import logging
import os

logger = logging.getLogger(__name__)


@dataclass
class CallRequest:
    """Unified request format for any LLM call."""
    model_id: str
    messages: List[Dict[str, str]]  # [{role: "user"|"assistant"|"system", content: "..."}]
    max_tokens: int = 4000
    temperature: float = 0.7
    system: Optional[str] = None


@dataclass
class CallResponse:
    """Unified response format from any LLM call."""
    content: str
    input_tokens: int
    output_tokens: int
    model_id: str
    provider: str
    finish_reason: str  # "stop" | "length" | "error"
    raw_response: Optional[Dict[str, Any]] = None


class ProviderAdapter(ABC):
    """Base class for LLM provider adapters."""

    @abstractmethod
    def call(self, request: CallRequest) -> CallResponse:
        """Make a synchronous LLM call."""
        pass

    @abstractmethod
    def is_configured(self) -> bool:
        """Check if this provider has the required env vars."""
        pass


class AnthropicAdapter(ProviderAdapter):
    """Adapter for Anthropic Claude models."""

    def __init__(self, api_key: Optional[str] = None):
        self.api_key = api_key or os.getenv("ANTHROPIC_API_KEY")
        self._client = None

    def is_configured(self) -> bool:
        return bool(self.api_key)

    def _get_client(self):
        if self._client is None:
            try:
                from anthropic import Anthropic
                self._client = Anthropic(api_key=self.api_key)
            except ImportError:
                raise RuntimeError("anthropic package not installed. Run: pip install anthropic")
        return self._client

    def call(self, request: CallRequest) -> CallResponse:
        client = self._get_client()

        # Anthropic uses separate `system` parameter, not in messages
        system = request.system
        messages = [m for m in request.messages if m["role"] != "system"]
        if not system:
            system_msgs = [m["content"] for m in request.messages if m["role"] == "system"]
            system = "\n".join(system_msgs) if system_msgs else None

        kwargs = {
            "model": request.model_id,
            "max_tokens": request.max_tokens,
            "temperature": request.temperature,
            "messages": messages,
        }
        if system:
            kwargs["system"] = system

        response = client.messages.create(**kwargs)

        return CallResponse(
            content=response.content[0].text if response.content else "",
            input_tokens=response.usage.input_tokens,
            output_tokens=response.usage.output_tokens,
            model_id=request.model_id,
            provider="anthropic",
            finish_reason=response.stop_reason or "stop",
            raw_response=response.model_dump() if hasattr(response, 'model_dump') else None,
        )


class OpenAIAdapter(ProviderAdapter):
    """Adapter for OpenAI GPT models."""

    def __init__(self, api_key: Optional[str] = None):
        self.api_key = api_key or os.getenv("OPENAI_API_KEY")
        self._client = None

    def is_configured(self) -> bool:
        return bool(self.api_key)

    def _get_client(self):
        if self._client is None:
            try:
                from openai import OpenAI
                self._client = OpenAI(api_key=self.api_key)
            except ImportError:
                raise RuntimeError("openai package not installed. Run: pip install openai")
        return self._client

    def call(self, request: CallRequest) -> CallResponse:
        client = self._get_client()

        messages = list(request.messages)
        if request.system:
            messages = [{"role": "system", "content": request.system}] + messages

        response = client.chat.completions.create(
            model=request.model_id,
            messages=messages,
            max_tokens=request.max_tokens,
            temperature=request.temperature,
        )

        return CallResponse(
            content=response.choices[0].message.content or "",
            input_tokens=response.usage.prompt_tokens,
            output_tokens=response.usage.completion_tokens,
            model_id=request.model_id,
            provider="openai",
            finish_reason=response.choices[0].finish_reason or "stop",
        )


class DeepSeekAdapter(ProviderAdapter):
    """
    Adapter for DeepSeek models.

    DeepSeek uses an OpenAI-compatible API, so we reuse the OpenAI SDK
    with a different base_url and api_key.
    """

    def __init__(self, api_key: Optional[str] = None):
        self.api_key = api_key or os.getenv("DEEPSEEK_API_KEY")
        self.base_url = os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com/v1")
        self._client = None

    def is_configured(self) -> bool:
        return bool(self.api_key)

    def _get_client(self):
        if self._client is None:
            try:
                from openai import OpenAI
                self._client = OpenAI(api_key=self.api_key, base_url=self.base_url)
            except ImportError:
                raise RuntimeError("openai package not installed. Run: pip install openai")
        return self._client

    def call(self, request: CallRequest) -> CallResponse:
        client = self._get_client()

        messages = list(request.messages)
        if request.system:
            messages = [{"role": "system", "content": request.system}] + messages

        response = client.chat.completions.create(
            model=request.model_id,
            messages=messages,
            max_tokens=request.max_tokens,
            temperature=request.temperature,
        )

        return CallResponse(
            content=response.choices[0].message.content or "",
            input_tokens=response.usage.prompt_tokens,
            output_tokens=response.usage.completion_tokens,
            model_id=request.model_id,
            provider="deepseek",
            finish_reason=response.choices[0].finish_reason or "stop",
        )


class GroqAdapter(ProviderAdapter):
    """Adapter for Groq (ultra-fast Llama inference)."""

    def __init__(self, api_key: Optional[str] = None):
        self.api_key = api_key or os.getenv("GROQ_API_KEY")
        self._client = None

    def is_configured(self) -> bool:
        return bool(self.api_key)

    def _get_client(self):
        if self._client is None:
            try:
                from groq import Groq
                self._client = Groq(api_key=self.api_key)
            except ImportError:
                raise RuntimeError("groq package not installed. Run: pip install groq")
        return self._client

    def call(self, request: CallRequest) -> CallResponse:
        client = self._get_client()

        messages = list(request.messages)
        if request.system:
            messages = [{"role": "system", "content": request.system}] + messages

        response = client.chat.completions.create(
            model=request.model_id,
            messages=messages,
            max_tokens=request.max_tokens,
            temperature=request.temperature,
        )

        return CallResponse(
            content=response.choices[0].message.content or "",
            input_tokens=response.usage.prompt_tokens,
            output_tokens=response.usage.completion_tokens,
            model_id=request.model_id,
            provider="groq",
            finish_reason=response.choices[0].finish_reason or "stop",
        )


# Provider registry — used by the router to dispatch calls
PROVIDER_ADAPTERS: Dict[str, type] = {
    "anthropic": AnthropicAdapter,
    "openai": OpenAIAdapter,
    "deepseek": DeepSeekAdapter,
    "groq": GroqAdapter,
}


def get_adapter(provider: str) -> ProviderAdapter:
    """Factory: get the adapter instance for a provider."""
    if provider not in PROVIDER_ADAPTERS:
        raise ValueError(f"Unknown provider: {provider}")
    return PROVIDER_ADAPTERS[provider]()


def get_configured_providers() -> List[str]:
    """Return list of providers that have valid API keys configured."""
    configured = []
    for provider_name, adapter_cls in PROVIDER_ADAPTERS.items():
        adapter = adapter_cls()
        if adapter.is_configured():
            configured.append(provider_name)
    return configured
