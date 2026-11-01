"""
Xcerebro 2.0 — Configuration

Loads environment variables via Pydantic settings.
"""

import os

# Set before any lazy CrewAI import. Do not export task content as telemetry.
os.environ["OTEL_SDK_DISABLED"] = "true"
os.environ["CREWAI_TELEMETRY_DISABLED"] = "true"
os.environ["LITELLM_LOCAL_MODEL_COST_MAP"] = "true"

from pydantic_settings import BaseSettings, SettingsConfigDict
from typing import Optional, Literal
from pathlib import Path
from pydantic import Field


_SOURCE_DIR = Path(__file__).resolve().parent
_DEFAULT_AGENTS = (
    _SOURCE_DIR / "agents"
    if (_SOURCE_DIR / "agents").is_dir()
    else _SOURCE_DIR.parent / "agents"
)


class Settings(BaseSettings):
    """Application settings loaded from environment / .env."""

    # This foundation cannot be switched to the workforce through environment settings.
    runtime_profile: Literal["friday"] = "friday"
    agents_path: Path = _DEFAULT_AGENTS
    # Docker places crew source at /app and definitions at /app/agents.
    friday_model: Optional[str] = None  # explicitly select and verify in Milestone 4

    # ---- Private Obsidian snapshot (explicit refresh; disabled by default) ----
    obsidian_enabled: bool = False
    obsidian_snapshot_path: Path = _SOURCE_DIR.parent / ".friday-local" / "obsidian-snapshot.json"

    # ---- LLM Provider ----
    anthropic_api_key: Optional[str] = None
    openai_api_key: Optional[str] = None
    openrouter_api_key: Optional[str] = None  # used for LLM + embeddings
    deepseek_api_key: Optional[str] = None
    groq_api_key: Optional[str] = None
    deepseek_base_url: str = "https://api.deepseek.com/v1"
    default_llm_model: str = "anthropic/claude-sonnet-4-5-20250929"
    default_llm_provider: str = "anthropic"

    # ---- Multi-Model Routing (v2.1+) ----
    # Tier: economy (cheapest), balanced (smart routing), premium (Claude only)
    routing_default_tier: str = "balanced"
    routing_enabled: bool = False  # Off by default in v2.0; flip to True for v2.1
    routing_max_retries: int = 2

    # ---- Database ----
    database_url: str = "postgresql://xcerebro:changeme@postgres:5432/crew"
    redis_url: str = "redis://redis:6379"

    # ---- This service ----
    port: int = 8000
    agent_runtime_url: str = "http://localhost:8000"
    agent_runtime_api_key: Optional[str] = None

    # ---- n8n integration ----
    n8n_base_url: Optional[str] = None
    n8n_api_key: Optional[str] = None
    n8n_webhook_url: Optional[str] = None
    n8n_bridge_secret: Optional[str] = None  # shared secret for agent email/calendar webhooks

    # ---- ClickUp ----
    clickup_api_key: Optional[str] = None
    clickup_team_id: Optional[str] = None

    # ---- Dify integration ----
    dify_base_url: Optional[str] = None
    dify_api_key: Optional[str] = None

    # ---- Postiz integration ----
    postiz_base_url: Optional[str] = None
    postiz_api_key: Optional[str] = None

    # ---- Slack approval ----
    slack_bot_token: Optional[str] = None
    slack_signing_secret: Optional[str] = None  # verifies /slack/interactions payloads
    slack_approval_channel_id: Optional[str] = None
    slack_notifications_channel_id: Optional[str] = None
    slack_chat_channel_id: Optional[str] = None  # dedicated #xcerebro chat channel; bot answers everything there
    slack_enabled: bool = False
    slack_socket_mode: bool = False
    slack_app_token: Optional[str] = None
    slack_team_id: Optional[str] = None
    slack_owner_user_id: Optional[str] = None
    slack_app_id: Optional[str] = None
    slack_bot_user_id: Optional[str] = None

    # ---- Graduated autonomy ----
    # Clean approvals per (agent, action_type) before the weekly digest
    # proposes flipping auto_approve. A human flips it, never the system.
    trust_auto_approve_threshold: int = 10

    # ---- Cost controls ----
    max_tokens_per_task: int = Field(default=256, ge=32, le=512)
    max_tasks_per_hour: int = Field(default=10, ge=1, le=30)
    max_daily_llm_spend_usd: float = Field(default=0.05, ge=0.01, le=1.0)
    friday_run_reservation_usd: float = Field(default=0.01, ge=0.01, le=0.01)
    friday_max_input_chars: int = Field(default=8000, ge=256, le=8000)

    # ---- Approval defaults ----
    approval_required_for_dm: bool = True
    approval_required_for_email: bool = True
    approval_required_for_public_post: bool = True
    approval_required_for_crm_update: bool = False
    approval_required_for_calendar_booking: bool = False
    approval_required_for_refund: bool = True
    approval_required_for_payment: bool = True
    approval_required_for_task_management: bool = True  # ClickUp create/delete/assignee changes

    # ---- Logging ----
    log_level: str = "info"

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )


settings = Settings()
