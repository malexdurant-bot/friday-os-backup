"""
Xcerebro 2.1 — Multi-Model Routing

Routes agent tasks to cheap models by default, escalates to premium
when needed. Cuts LLM costs 70-90% for typical workloads.
"""

from .router import (
    ModelRouter,
    RoutingPolicy,
    RoutingTier,
    TaskContext,
    RoutingDecision,
    ModelSpec,
    ModelProvider,
    MODEL_REGISTRY,
    TIER_POLICIES,
    load_policy_from_yaml,
)
from .providers import (
    ProviderAdapter,
    AnthropicAdapter,
    OpenAIAdapter,
    DeepSeekAdapter,
    GroqAdapter,
    CallRequest,
    CallResponse,
    get_adapter,
    get_configured_providers,
    PROVIDER_ADAPTERS,
)
from .executor import (
    RoutingExecutor,
    get_default_executor,
)

__all__ = [
    # Router
    "ModelRouter",
    "RoutingPolicy",
    "RoutingTier",
    "TaskContext",
    "RoutingDecision",
    "ModelSpec",
    "ModelProvider",
    "MODEL_REGISTRY",
    "TIER_POLICIES",
    "load_policy_from_yaml",
    # Providers
    "ProviderAdapter",
    "AnthropicAdapter",
    "OpenAIAdapter",
    "DeepSeekAdapter",
    "GroqAdapter",
    "CallRequest",
    "CallResponse",
    "get_adapter",
    "get_configured_providers",
    "PROVIDER_ADAPTERS",
    # Executor
    "RoutingExecutor",
    "get_default_executor",
]
