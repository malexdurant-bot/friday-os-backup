"""
Xcerebro 2.1 — Multi-Model Router

Routes agent tasks to the cheapest model that can handle them,
with automatic escalation to premium models on failure or high-stakes content.

Routing tiers:
- ECONOMY: DeepSeek primary, no fallback. High-volume, low-stakes.
- BALANCED: DeepSeek primary, Claude Sonnet fallback on failure.
- PREMIUM: Claude Opus only. High-stakes, customer-facing, legal/financial.

Cost savings: 70-90% vs all-Claude routing for typical workloads.
"""

from enum import Enum
from typing import Optional, List, Dict, Any
from dataclasses import dataclass, field
from datetime import datetime
import logging
import re

logger = logging.getLogger(__name__)


class RoutingTier(str, Enum):
    """Routing tier — determines which models are used for an agent."""
    ECONOMY = "economy"
    BALANCED = "balanced"
    PREMIUM = "premium"


class ModelProvider(str, Enum):
    """LLM providers supported by the router."""
    ANTHROPIC = "anthropic"
    OPENAI = "openai"
    DEEPSEEK = "deepseek"
    GROQ = "groq"
    GOOGLE = "google"


@dataclass
class ModelSpec:
    """Specification for a single model."""
    provider: ModelProvider
    model_id: str
    cost_per_1k_input: float   # USD per 1K input tokens
    cost_per_1k_output: float  # USD per 1K output tokens
    context_window: int
    supports_streaming: bool = True
    supports_tools: bool = True


# Model registry — pricing as of v2.1 ship date
# Update these from provider docs periodically
MODEL_REGISTRY: Dict[str, ModelSpec] = {
    # --- Anthropic ---
    "claude-opus-4-7": ModelSpec(
        provider=ModelProvider.ANTHROPIC,
        model_id="claude-opus-4-7",
        cost_per_1k_input=0.015,
        cost_per_1k_output=0.075,
        context_window=200_000,
    ),
    "claude-sonnet-4-6": ModelSpec(
        provider=ModelProvider.ANTHROPIC,
        model_id="claude-sonnet-4-6",
        cost_per_1k_input=0.003,
        cost_per_1k_output=0.015,
        context_window=200_000,
    ),
    "claude-haiku-4-5": ModelSpec(
        provider=ModelProvider.ANTHROPIC,
        model_id="claude-haiku-4-5-20251001",
        cost_per_1k_input=0.001,
        cost_per_1k_output=0.005,
        context_window=200_000,
    ),
    # --- OpenAI ---
    "gpt-5.5": ModelSpec(
        provider=ModelProvider.OPENAI,
        model_id="gpt-5.5",
        cost_per_1k_input=0.01,
        cost_per_1k_output=0.03,
        context_window=128_000,
    ),
    "gpt-5-mini": ModelSpec(
        provider=ModelProvider.OPENAI,
        model_id="gpt-5-mini",
        cost_per_1k_input=0.002,
        cost_per_1k_output=0.008,
        context_window=128_000,
    ),
    # --- DeepSeek (the cheap workhorses) ---
    "deepseek-v4-pro": ModelSpec(
        provider=ModelProvider.DEEPSEEK,
        model_id="deepseek-v4-pro",
        cost_per_1k_input=0.00027,
        cost_per_1k_output=0.0011,
        context_window=64_000,
    ),
    "deepseek-flash": ModelSpec(
        provider=ModelProvider.DEEPSEEK,
        model_id="deepseek-flash",
        cost_per_1k_input=0.00014,
        cost_per_1k_output=0.00028,
        context_window=32_000,
    ),
    # --- Groq (ultra-fast Llama inference) ---
    "groq-llama-3.3-70b": ModelSpec(
        provider=ModelProvider.GROQ,
        model_id="llama-3.3-70b-versatile",
        cost_per_1k_input=0.00059,
        cost_per_1k_output=0.00079,
        context_window=128_000,
    ),
}


# Registry model IDs → OpenRouter slugs. All execution goes through OpenRouter
# (one API key), so routing only decides WHICH slug _build_llm uses.
OPENROUTER_SLUGS: Dict[str, str] = {
    "claude-opus-4-7": "anthropic/claude-opus-4.5",
    "claude-sonnet-4-6": "anthropic/claude-sonnet-4.5",
    "claude-haiku-4-5": "anthropic/claude-haiku-4.5",
    "gpt-5.5": "openai/gpt-5.5",
    "gpt-5-mini": "openai/gpt-5-mini",
    "deepseek-v4-pro": "deepseek/deepseek-chat",
    "deepseek-flash": "deepseek/deepseek-chat",  # same OpenRouter slug; flash tier kept for pricing intent
    "groq-llama-3.3-70b": "meta-llama/llama-3.3-70b-instruct",
}


def to_openrouter_slug(model_id: str) -> str:
    """Map a registry model ID to its OpenRouter slug (pass through if unknown)."""
    return OPENROUTER_SLUGS.get(model_id, model_id)


def cost_for_slug(slug: str, input_tokens: int, output_tokens: int) -> float:
    """Estimate USD cost for a call by OpenRouter slug, using MODEL_REGISTRY pricing."""
    for model_id, s in OPENROUTER_SLUGS.items():
        if s == slug:
            spec = MODEL_REGISTRY.get(model_id)
            if spec:
                return (input_tokens / 1000) * spec.cost_per_1k_input + \
                       (output_tokens / 1000) * spec.cost_per_1k_output
    # Unknown slug — assume Sonnet pricing (conservative)
    spec = MODEL_REGISTRY["claude-sonnet-4-6"]
    return (input_tokens / 1000) * spec.cost_per_1k_input + \
           (output_tokens / 1000) * spec.cost_per_1k_output


@dataclass
class RoutingPolicy:
    """
    Defines how an agent routes its model calls.

    Loaded from agent YAML or set per-task at runtime.
    """
    tier: RoutingTier = RoutingTier.BALANCED
    primary_model: str = "deepseek-v4-pro"
    fallback_model: Optional[str] = "claude-sonnet-4-6"
    premium_model: str = "claude-opus-4-7"

    # Escalation triggers
    escalate_on_retry_count: int = 2
    # Real-estate ops language ("under contract", "legal description",
    # "contractor", "refund policy") must NOT escalate — only genuinely
    # high-stakes phrases do. Matched on word boundaries, not substrings.
    escalate_on_keywords: List[str] = field(default_factory=lambda: [
        "lawsuit", "litigation", "attorney", "subpoena",
        "legal advice", "legal review", "compliance violation",
        "chargeback", "fraud",
        "medical", "diagnosis", "prescription",
    ])
    escalate_on_categories: List[str] = field(default_factory=lambda: [
        "compliance", "finance_critical", "customer_facing_dispute",
    ])

    # Hard overrides (always use premium regardless of routing tier)
    force_premium: bool = False


# Pre-defined policies for the 3 tiers
TIER_POLICIES: Dict[RoutingTier, RoutingPolicy] = {
    RoutingTier.ECONOMY: RoutingPolicy(
        tier=RoutingTier.ECONOMY,
        primary_model="deepseek-flash",
        fallback_model=None,  # No fallback — accept failures
        premium_model="claude-opus-4-7",  # Still used if force_premium
    ),
    RoutingTier.BALANCED: RoutingPolicy(
        tier=RoutingTier.BALANCED,
        primary_model="deepseek-v4-pro",
        fallback_model="claude-sonnet-4-6",
        premium_model="claude-opus-4-7",
    ),
    RoutingTier.PREMIUM: RoutingPolicy(
        tier=RoutingTier.PREMIUM,
        primary_model="claude-opus-4-7",
        fallback_model=None,  # Already at premium
        premium_model="claude-opus-4-7",
    ),
}


@dataclass
class TaskContext:
    """Context for a single task — used to make routing decisions."""
    agent_id: str
    task_description: str
    category: Optional[str] = None
    retry_count: int = 0
    force_premium: bool = False
    last_failure_reason: Optional[str] = None


@dataclass
class RoutingDecision:
    """The result of a routing decision — which model to use and why."""
    model_id: str
    model_spec: ModelSpec
    reason: str
    is_escalation: bool = False


class ModelRouter:
    """
    Routes agent tasks to the appropriate model based on routing policy.

    Usage:
        router = ModelRouter()
        decision = router.route(policy, task_context)
        # decision.model_id, decision.model_spec, decision.reason
    """

    def __init__(self, available_providers: Optional[List[ModelProvider]] = None):
        """
        Args:
            available_providers: Which providers have API keys configured.
                If None, all are assumed available.
        """
        self.available_providers = available_providers or list(ModelProvider)
        self._call_log: List[Dict[str, Any]] = []

    def route(
        self,
        policy: RoutingPolicy,
        context: TaskContext,
    ) -> RoutingDecision:
        """
        Make a routing decision based on policy and current task context.
        """
        # 1. Check force-premium flag (overrides everything)
        if context.force_premium or policy.force_premium:
            spec = self._get_model(policy.premium_model)
            return RoutingDecision(
                model_id=policy.premium_model,
                model_spec=spec,
                reason="force_premium flag set",
                is_escalation=True,
            )

        # 2. Check category-based escalation
        if context.category and context.category in policy.escalate_on_categories:
            spec = self._get_model(policy.premium_model)
            return RoutingDecision(
                model_id=policy.premium_model,
                model_spec=spec,
                reason=f"category '{context.category}' requires premium",
                is_escalation=True,
            )

        # 3. Check keyword-based escalation (word boundaries — "contractor"
        # must not match "contract"-style substrings)
        task_lower = context.task_description.lower()
        triggered_keywords = [
            kw for kw in policy.escalate_on_keywords
            if re.search(rf"\b{re.escape(kw.lower())}\b", task_lower)
        ]
        if triggered_keywords:
            spec = self._get_model(policy.premium_model)
            return RoutingDecision(
                model_id=policy.premium_model,
                model_spec=spec,
                reason=f"keyword(s) triggered escalation: {triggered_keywords}",
                is_escalation=True,
            )

        # 4. Check retry-based escalation
        if context.retry_count >= policy.escalate_on_retry_count:
            target = policy.fallback_model or policy.premium_model
            spec = self._get_model(target)
            return RoutingDecision(
                model_id=target,
                model_spec=spec,
                reason=f"escalated after {context.retry_count} retries",
                is_escalation=True,
            )

        # 5. Default: use primary model for the tier
        spec = self._get_model(policy.primary_model)
        return RoutingDecision(
            model_id=policy.primary_model,
            model_spec=spec,
            reason=f"default {policy.tier.value} tier routing",
            is_escalation=False,
        )

    def _get_model(self, model_id: str) -> ModelSpec:
        """Get model spec, falling back to a sensible default if not found."""
        if model_id not in MODEL_REGISTRY:
            logger.warning(f"Model {model_id} not in registry, using claude-sonnet-4-6")
            return MODEL_REGISTRY["claude-sonnet-4-6"]
        spec = MODEL_REGISTRY[model_id]
        if spec.provider not in self.available_providers:
            logger.warning(
                f"Model {model_id} provider {spec.provider} not configured, "
                f"falling back to claude-sonnet-4-6"
            )
            return MODEL_REGISTRY["claude-sonnet-4-6"]
        return spec

    def estimate_cost(
        self,
        model_id: str,
        input_tokens: int,
        output_tokens: int,
    ) -> float:
        """Estimate cost in USD for a single call."""
        spec = self._get_model(model_id)
        input_cost = (input_tokens / 1000) * spec.cost_per_1k_input
        output_cost = (output_tokens / 1000) * spec.cost_per_1k_output
        return input_cost + output_cost

    def log_call(
        self,
        model_id: str,
        agent_id: str,
        input_tokens: int,
        output_tokens: int,
        duration_ms: int,
        success: bool,
    ) -> None:
        """Log a model call for cost tracking and analytics."""
        cost = self.estimate_cost(model_id, input_tokens, output_tokens)
        entry = {
            "timestamp": datetime.utcnow().isoformat(),
            "model_id": model_id,
            "agent_id": agent_id,
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "duration_ms": duration_ms,
            "success": success,
            "cost_usd": round(cost, 6),
        }
        self._call_log.append(entry)
        logger.info(
            f"[router] {agent_id} -> {model_id} "
            f"({input_tokens}+{output_tokens} toks, ${cost:.4f})"
        )

    def get_spend_summary(self, hours: int = 24) -> Dict[str, Any]:
        """Get a summary of spend over the last N hours."""
        from collections import defaultdict
        by_model = defaultdict(lambda: {"calls": 0, "cost": 0.0, "tokens": 0})
        total_cost = 0.0
        total_calls = 0

        for entry in self._call_log:
            model = entry["model_id"]
            by_model[model]["calls"] += 1
            by_model[model]["cost"] += entry["cost_usd"]
            by_model[model]["tokens"] += entry["input_tokens"] + entry["output_tokens"]
            total_cost += entry["cost_usd"]
            total_calls += 1

        return {
            "period_hours": hours,
            "total_calls": total_calls,
            "total_cost_usd": round(total_cost, 4),
            "by_model": {k: dict(v) for k, v in by_model.items()},
        }


def load_policy_from_yaml(yaml_data: Dict[str, Any]) -> RoutingPolicy:
    """
    Load a RoutingPolicy from agent YAML data.

    Expected YAML structure:
        model_routing:
          tier: balanced  # economy | balanced | premium
          primary: deepseek-v4-pro       # optional override
          fallback: claude-sonnet-4-6    # optional override
          premium: claude-opus-4-7       # optional override
          escalate_on_retry_count: 2
          force_premium: false
    """
    routing_data = yaml_data.get("model_routing", {})
    tier_str = routing_data.get("tier", "balanced")

    try:
        tier = RoutingTier(tier_str)
    except ValueError:
        logger.warning(f"Unknown tier '{tier_str}', defaulting to BALANCED")
        tier = RoutingTier.BALANCED

    # Start with the base policy for this tier
    base = TIER_POLICIES[tier]

    # Apply YAML overrides
    return RoutingPolicy(
        tier=tier,
        primary_model=routing_data.get("primary", base.primary_model),
        fallback_model=routing_data.get("fallback", base.fallback_model),
        premium_model=routing_data.get("premium", base.premium_model),
        escalate_on_retry_count=routing_data.get(
            "escalate_on_retry_count", base.escalate_on_retry_count
        ),
        escalate_on_keywords=routing_data.get(
            "escalate_on_keywords", base.escalate_on_keywords
        ),
        escalate_on_categories=routing_data.get(
            "escalate_on_categories", base.escalate_on_categories
        ),
        force_premium=routing_data.get("force_premium", False),
    )
