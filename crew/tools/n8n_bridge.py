"""
Xcerebro 2.0 — n8n Bridge Tools (email + calendar)

Google OAuth lives in n8n (which already holds working credentials), not in
this Python runtime. These tools POST to small n8n webhook workflows that do
the actual Gmail/Calendar work and return the result.

Security layers:
- A shared secret header (N8N_BRIDGE_SECRET) that the n8n workflows verify.
- The approval gate: email.send / calendar.create_event are classified as
  outbound action types in registry.ACTION_TYPE_TOOLS, so gated agents pause
  for Slack approval BEFORE these tools can ever fire.

Webhook contract (n8n side): POST {N8N_WEBHOOK_URL}/agent-<action>
with JSON body; responds with JSON.
"""

import json
import os
from typing import Type

import requests
from crewai.tools import BaseTool
from loguru import logger
from pydantic import BaseModel, Field

from config import settings


def _bridge_post(action: str, payload: dict) -> str:
    base = (settings.n8n_webhook_url or "").rstrip("/")
    if not base:
        return json.dumps({"error": "N8N_WEBHOOK_URL not configured."})
    secret = os.getenv("N8N_BRIDGE_SECRET", "")
    try:
        resp = requests.post(
            f"{base}/agent-{action}",
            json=payload,
            headers={"X-Bridge-Secret": secret, "Content-Type": "application/json"},
            timeout=30,
        )
        resp.raise_for_status()
        try:
            return json.dumps(resp.json())
        except ValueError:
            return json.dumps({"ok": True, "raw": resp.text[:500]})
    except requests.HTTPError as e:
        logger.error(f"n8n bridge {action} error {e.response.status_code}: {e.response.text[:300]}")
        return json.dumps({"error": str(e), "status_code": e.response.status_code})
    except Exception as e:
        logger.error(f"n8n bridge {action} failed: {e}")
        return json.dumps({"error": str(e)})


# ── email ─────────────────────────────────────────────────────────────────────

class EmailSendInput(BaseModel):
    to: str = Field(description="Recipient email address")
    subject: str = Field(description="Email subject line")
    body: str = Field(description="Email body (plain text or simple HTML)")


class EmailSendTool(BaseTool):
    name: str = "email_send"
    description: str = (
        "Send an email via the business Gmail account. Outbound-gated: for most "
        "agents this only runs after human approval in Slack."
    )
    args_schema: Type[BaseModel] = EmailSendInput

    def _run(self, to: str, subject: str, body: str) -> str:
        return _bridge_post("email-send", {"to": to, "subject": subject, "body": body})


# ── calendar ──────────────────────────────────────────────────────────────────

class CalendarReadInput(BaseModel):
    start: str = Field(description="Window start, ISO 8601 (e.g. 2026-07-06T09:00:00-05:00)")
    end: str = Field(description="Window end, ISO 8601")


class CalendarReadEventsTool(BaseTool):
    name: str = "calendar_read_events"
    description: str = "List calendar events in a time window (also used to check availability — gaps between events are free)."
    args_schema: Type[BaseModel] = CalendarReadInput

    def _run(self, start: str, end: str) -> str:
        return _bridge_post("calendar-read", {"start": start, "end": end})


class CalendarCreateInput(BaseModel):
    title: str = Field(description="Event title")
    start: str = Field(description="Event start, ISO 8601")
    end: str = Field(description="Event end, ISO 8601")
    attendee_email: str = Field(default="", description="Optional attendee to invite")
    description: str = Field(default="", description="Optional event description")


class CalendarCreateEventTool(BaseTool):
    name: str = "calendar_create_event"
    description: str = (
        "Create a calendar event / book a meeting. Outbound-gated: for most agents "
        "this only runs after human approval in Slack."
    )
    args_schema: Type[BaseModel] = CalendarCreateInput

    def _run(self, title: str, start: str, end: str, attendee_email: str = "", description: str = "") -> str:
        return _bridge_post("calendar-create", {
            "title": title, "start": start, "end": end,
            "attendee_email": attendee_email, "description": description,
        })


# ── google drive ──────────────────────────────────────────────────────────────

class DriveSearchInput(BaseModel):
    query: str = Field(description="File name or partial name to search for in Google Drive")


class DriveSearchTool(BaseTool):
    name: str = "drive_search_files"
    description: str = "Search Google Drive for files by name. Returns file IDs, names, and types."
    args_schema: Type[BaseModel] = DriveSearchInput

    def _run(self, query: str) -> str:
        return _bridge_post("drive-search", {"query": query})


class DriveReadInput(BaseModel):
    file_id: str = Field(description="Google Drive file ID (from drive_search_files)")


class DriveReadTool(BaseTool):
    name: str = "drive_read_file"
    description: str = (
        "Read a Google Drive file's text content (Google Docs are exported as plain text). "
        "Use for the EOS V/TO, SOPs, and other business documents."
    )
    args_schema: Type[BaseModel] = DriveReadInput

    def _run(self, file_id: str) -> str:
        return _bridge_post("drive-read", {"file_id": file_id})


def get_n8n_bridge_tools(tool_names: list[str]) -> list:
    """Loader for email.*, calendar.*, and drive.* declared names."""
    tools = []
    if any(n.startswith("email.send") for n in tool_names):
        tools.append(EmailSendTool())
    if any(n.startswith(("calendar.read", "calendar.update")) for n in tool_names):
        # read_availability / read_events / update all get the read tool;
        # update-in-place can come later — reading + creating covers the declared flows
        tools.append(CalendarReadEventsTool())
    if any(n.startswith("calendar.create") for n in tool_names):
        tools.append(CalendarCreateEventTool())
    if any(n.startswith("drive.search") for n in tool_names):
        tools.append(DriveSearchTool())
    if any(n.startswith("drive.read") for n in tool_names):
        tools.append(DriveReadTool())
    return tools
