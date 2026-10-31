"""
Xcerebro 2.1 — Routing Executor

The high-level entry point for agent task execution.
Combines the router (decision-making) with provider adapters (execution).

Usage:
    executor = RoutingExecutor()
    result = executor.execute(
        agent_id="copy-writer",
        policy=policy,
        messages=[{"role": "user", "content": "Write a hook"}],
    )
"""

from typing import List, Dict, Optional
import logging
import time

from .router import (
    ModelRouter,
    RoutingPolicy,
    TaskContext,
    RoutingDecision,
    MODEL_REGISTRY,
)
from .providers import (
    ProviderAdapter,
    CallRequest,
    CallResponse,
    get_adapter,
    get_configured_providers,
)

logger = logging.getLogger(__name__)


class RoutingExecutor:
    """
    Executes agent tasks using the routed model.
    Handles automatic retry/escalation on failure.
    """

    def __init__(self):
        self.router = ModelRouter()
        self._adapters: Dict[str, ProviderAdapter] = {}
        self.configured_providers = get_configured_providers()
        logger.info(f"[executor] configured providers: {self.configured_providers}")

    def _get_adapter(self, provider: str) -> ProviderAdapter:
        """Cache adapters per provider."""
        if provider not in self._adapters:
            self._adapters[provider] = get_adapter(provider)
        return self._adapters[provider]

    def execute(
        self,
        agent_id: str,
        policy: RoutingPolicy,
        messages: List[Dict[str, str]],
        system: Optional[str] = None,
        category: Optional[str] = None,
        max_tokens: int = 4000,
        temperature: float = 0.7,
        force_premium: bool = False,
        max_retries: int = 2,
    ) -> CallResponse:
        """
        Execute a task with automatic routing and escalation.

        Returns the response from whichever model succeeded.
        Raises RuntimeError if all retries fail.
        """
        task_description = " ".join([m.get("content", "") for m in messages])[:500]

        for attempt in range(max_retries + 1):
            context = TaskContext(
                agent_id=agent_id,
                task_description=task_description,
                category=category,
                retry_count=attempt,
                force_premium=force_premium,
            )

            decision = self.router.route(policy, context)
            logger.info(
                f"[executor] {agent_id} attempt {attempt+1}: "
                f"using {decision.model_id} ({decision.reason})"
            )

            try:
                adapter = self._get_adapter(decision.model_spec.provider.value)

                if not adapter.is_configured():
                    logger.warning(
                        f"[executor] {decision.model_spec.provider} not configured, "
                        f"will escalate"
                    )
                    continue

                request = CallRequest(
                    model_id=decision.model_spec.model_id,
                    messages=messages,
                    max_tokens=max_tokens,
                    temperature=temperature,
                    system=system,
                )

                start = time.time()
                response = adapter.call(request)
                duration_ms = int((time.time() - start) * 1000)

                self.router.log_call(
                    model_id=decision.model_id,
                    agent_id=agent_id,
                    input_tokens=response.input_tokens,
                    output_tokens=response.output_tokens,
                    duration_ms=duration_ms,
                    success=True,
                )

                return response

            except Exception as e:
                logger.error(
                    f"[executor] {agent_id} call failed on {decision.model_id}: {e}"
                )
                self.router.log_call(
                    model_id=decision.model_id,
                    agent_id=agent_id,
                    input_tokens=0,
                    output_tokens=0,
                    duration_ms=0,
                    success=False,
                )
                if attempt >= max_retries:
                    raise RuntimeError(
                        f"All {max_retries+1} attempts failed for {agent_id}. "
                        f"Last error: {e}"
                    )
                # Continue to next iteration — context.retry_count will trigger escalation

        raise RuntimeError(f"Exhausted retries for {agent_id}")

    def get_spend_summary(self, hours: int = 24) -> dict:
        """Return spend summary for the dashboard."""
        return self.router.get_spend_summary(hours=hours)


# Convenience: a default singleton for simple use cases
_default_executor: Optional[RoutingExecutor] = None


def get_default_executor() -> RoutingExecutor:
    """Get or create the default executor."""
    global _default_executor
    if _default_executor is None:
        _default_executor = RoutingExecutor()
    return _default_executor
