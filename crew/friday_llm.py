"""CrewAI-compatible, bounded OpenRouter adapter for the tool-free foundation."""
import json
import re

import httpx
from crewai import LLM

from friday_limits import estimated_cost


class FridayLLM(LLM):
    def __init__(self, model: str, api_key: str, max_tokens: int, budget: float):
        super().__init__(model=f"openrouter/{model}", api_key=api_key,
                         max_tokens=max_tokens, temperature=0, timeout=45)
        self.slug = model
        self.budget = budget
        self.calls = 0
        self.reserved = 0.0
        self.prompt_tokens = 0
        self.completion_tokens = 0
        self.reported_cost = 0.0

    def call(self, messages, tools=None, callbacks=None, available_functions=None):
        if tools or available_functions:
            raise RuntimeError("Friday cannot call tools")
        if self.calls >= 2:
            raise RuntimeError("Friday model call limit reached")
        if isinstance(messages, str):
            messages = [{"role": "user", "content": messages}]
        # UTF-8 byte count plus framing margin is a deliberately conservative
        # input-token estimate. Actual billing still depends on provider prices.
        size = len(json.dumps(messages, ensure_ascii=False).encode("utf-8")) + 1024
        if size > 34000:
            raise RuntimeError("Friday model input size limit reached")
        upper = estimated_cost(self.slug, size, self.max_tokens)
        if self.reserved + upper > self.budget:
            raise RuntimeError("Friday per-run estimated budget exceeded")
        self.reserved += upper
        self.calls += 1
        try:
            # No retry loop; no LiteLLM fallback or provider substitution.
            with httpx.Client(timeout=45) as client:
                response = client.post(
                    "https://openrouter.ai/api/v1/chat/completions",
                    headers={"Authorization": f"Bearer {self.api_key}"},
                    json={"model": self.slug, "messages": messages,
                          "max_tokens": self.max_tokens, "temperature": 0,
                          "provider": {"allow_fallbacks": False},
                          **({"reasoning": {"enabled": False}}
                             if self.slug.startswith("google/gemma-") else {})},
                )
            if response.status_code != 200:
                # Keep only the provider's brief error message, never raw request
                # metadata, headers or nested upstream error payloads.
                detail = ""
                try:
                    detail = str(response.json().get("error", {}).get("message", ""))
                except (ValueError, AttributeError):
                    pass
                if self.api_key:
                    detail = detail.replace(self.api_key, "[REDACTED]")
                detail = re.sub(r"(?i)Bearer\s+\S+|sk-[\w-]+", "[REDACTED]", detail)
                raise RuntimeError(
                    f"OpenRouter HTTP {response.status_code}; no retry performed. {detail[:300]}"
                )
            data = response.json()
            usage = data.get("usage", {})
            tin, tout = usage.get("prompt_tokens", 0), usage.get("completion_tokens", 0)
            if not isinstance(tin, int) or not isinstance(tout, int) or tin <= 0 or tout <= 0:
                raise RuntimeError("OpenRouter usage missing; reservation retained")
            self.prompt_tokens += tin
            self.completion_tokens += tout
            cost = usage.get("cost")
            if isinstance(cost, (int, float)) and cost >= 0:
                self.reported_cost += cost
            message = data["choices"][0]["message"]
            if message.get("tool_calls"):
                raise RuntimeError("Unexpected tool call refused")
            reply = message.get("content")
            if not isinstance(reply, str) or not reply.strip():
                raise RuntimeError("OpenRouter returned no text")
            return reply
        except httpx.HTTPError:
            # Never include headers, request objects or provider response bodies.
            raise RuntimeError("OpenRouter transport failure; no retry performed") from None
        except (KeyError, TypeError, ValueError):
            raise RuntimeError("Invalid OpenRouter response; reservation retained") from None

    def supports_function_calling(self):
        return False
