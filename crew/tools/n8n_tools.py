"""
Xcerebro 2.0 — n8n read tools

Lets agents see the automation layer: which workflows exist and which are
active. Read-only. Uses the n8n public API with the same base URL + API key
the runtime already holds.
"""

import json
from typing import Type

import requests
from crewai.tools import BaseTool
from loguru import logger
from pydantic import BaseModel, Field

from config import settings


class ReadWorkflowsInput(BaseModel):
    active_only: bool = Field(default=False, description="Only return active (scheduled/armed) workflows")


class N8nReadWorkflowsTool(BaseTool):
    name: str = "n8n_read_workflows"
    description: str = (
        "List the automation workflows on the n8n instance: name, active status, "
        "and tags. Use to understand what is automated and what runs on schedules."
    )
    args_schema: Type[BaseModel] = ReadWorkflowsInput

    def _run(self, active_only: bool = False) -> str:
        base = (settings.n8n_base_url or "").rstrip("/")
        key = settings.n8n_api_key or ""
        if not base or not key:
            return json.dumps({"error": "n8n API not configured (N8N_BASE_URL / N8N_API_KEY)."})
        try:
            params = {"limit": 100}
            if active_only:
                params["active"] = "true"
            resp = requests.get(
                f"{base}/api/v1/workflows",
                headers={"X-N8N-API-KEY": key},
                params=params,
                timeout=20,
            )
            resp.raise_for_status()
            data = resp.json()
            workflows = [
                {
                    "name": w.get("name"),
                    "active": w.get("active"),
                    "tags": [t.get("name") for t in (w.get("tags") or [])],
                }
                for w in data.get("data", [])
            ]
            return json.dumps({"count": len(workflows), "workflows": workflows})
        except Exception as e:
            logger.error(f"n8n_read_workflows failed: {e}")
            return json.dumps({"error": str(e)[:300]})


def get_n8n_tools(tool_names: list[str]) -> list:
    tools = []
    if any(n.startswith("n8n.read_workflows") for n in tool_names):
        tools.append(N8nReadWorkflowsTool())
    return tools
