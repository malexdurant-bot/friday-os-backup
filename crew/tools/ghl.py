"""
Xcerebro 2.0 — GoHighLevel CRM Tools

Five CrewAI BaseTool subclasses that give agents direct read/write
access to GoHighLevel via the GHL API v2.

Credentials come from env vars already set on the Railway service:
  GHL_API_KEY      — Bearer token (private integration)
  GHL_LOCATION_ID  — Sub-account location ID
"""

import json
import os
from typing import Optional, Type

import requests
from crewai.tools import BaseTool
from loguru import logger
from pydantic import BaseModel, Field

GHL_BASE = "https://services.leadconnectorhq.com"
GHL_VERSION = "2021-07-28"


def _headers() -> dict:
    return {
        "Authorization": f"Bearer {os.getenv('GHL_API_KEY', '')}",
        "Version": GHL_VERSION,
        "Content-Type": "application/json",
    }


def _location_id() -> str:
    return os.getenv("GHL_LOCATION_ID", "")


def _safe_request(method: str, url: str, **kwargs) -> dict:
    """Make a GHL API request and return parsed JSON, or an error dict."""
    try:
        resp = requests.request(method, url, headers=_headers(), timeout=15, **kwargs)
        resp.raise_for_status()
        return resp.json()
    except requests.HTTPError as e:
        logger.error(f"GHL API error {e.response.status_code}: {e.response.text[:300]}")
        return {"error": str(e), "status_code": e.response.status_code}
    except Exception as e:
        logger.error(f"GHL request failed: {e}")
        return {"error": str(e)}


# ── Read Contacts ─────────────────────────────────────────────────────────────

class ReadContactsInput(BaseModel):
    query: str = Field(default="", description="Search term (name, email, or phone)")
    max_contacts: int = Field(default=200, description="Max contacts to return. Use 500+ for full-database audits. Fetches in pages of 100.")


class GHLReadContactsTool(BaseTool):
    name: str = "ghl_read_contacts"
    description: str = (
        "Search and list contacts in GoHighLevel CRM. Use to find contacts by "
        "name/email/phone, see recently updated leads, or pull the full contact list. "
        "Returns contact names, pipeline stages, tags, last activity, and contact IDs. "
        "Set max_contacts=500 or higher for full-database audits."
    )
    args_schema: Type[BaseModel] = ReadContactsInput

    def _run(self, query: str = "", max_contacts: int = 200) -> str:
        contacts: list = []
        page_size = 100
        # GHL v2 paginates with a cursor (meta.startAfterId/startAfter),
        # not offset — sending "skip" is a 422
        start_after_id = ""
        start_after = None

        while len(contacts) < max_contacts:
            params = {
                "locationId": _location_id(),
                "limit": page_size,
            }
            if query:
                params["query"] = query
            if start_after_id:
                params["startAfterId"] = start_after_id
                if start_after is not None:
                    params["startAfter"] = start_after

            data = _safe_request("GET", f"{GHL_BASE}/contacts/", params=params)
            if "error" in data:
                return f"GHL error: {data['error']}"

            page = data.get("contacts", [])
            contacts.extend(page)

            meta = data.get("meta", {}) or {}
            start_after_id = meta.get("startAfterId") or ""
            start_after = meta.get("startAfter")
            if len(page) < page_size or not start_after_id:
                break  # last page

        if not contacts:
            return "No contacts found matching that query."

        contacts = contacts[:max_contacts]
        rows = []
        for c in contacts:
            name = f"{c.get('firstName','')} {c.get('lastName','')}".strip() or "(no name)"
            email = c.get("email", "")
            tags = ", ".join(c.get("tags", [])) or "none"
            stage = c.get("pipelineStage", {}).get("name", "") if c.get("pipelineStage") else ""
            updated = (c.get("dateUpdated") or "")[:10]
            rows.append(
                f"ID:{c['id']} | {name} | {email} | stage:{stage or 'none'} | "
                f"tags:[{tags}] | updated:{updated}"
            )

        total_in_db = data.get("meta", {}).get("total", len(contacts))
        suffix = f" (showing {len(contacts)} of {total_in_db} total)" if total_in_db > len(contacts) else ""
        return f"Found {len(contacts)} contact(s){suffix}:\n" + "\n".join(rows)


# ── Update Contact ────────────────────────────────────────────────────────────

class UpdateContactInput(BaseModel):
    contact_id: str = Field(..., description="The GHL contact ID to update")
    fields: dict = Field(
        ...,
        description=(
            "Dict of fields to update. Common fields: firstName, lastName, email, "
            "phone, companyName, address1, city, state, postalCode, "
            "customFields (list of {id, value} objects)"
        ),
    )


class GHLUpdateContactTool(BaseTool):
    name: str = "ghl_update_contact"
    description: str = (
        "Update fields on an existing GHL contact. Pass the contact_id and a dict "
        "of field names to new values. Use this to fix data quality issues, update "
        "pipeline stages, or correct contact information."
    )
    args_schema: Type[BaseModel] = UpdateContactInput

    def _run(self, contact_id: str, fields: dict) -> str:
        data = _safe_request("PUT", f"{GHL_BASE}/contacts/{contact_id}", json=fields)
        if "error" in data:
            return f"GHL update failed: {data['error']}"
        name = data.get("contact", {})
        return f"Contact {contact_id} updated successfully."


# ── Add / Remove Tags ─────────────────────────────────────────────────────────

class TagContactInput(BaseModel):
    contact_id: str = Field(..., description="The GHL contact ID")
    tags: list[str] = Field(..., description="List of tag strings to add")


class GHLAddTagTool(BaseTool):
    name: str = "ghl_add_tag"
    description: str = (
        "Add one or more tags to a GHL contact. Use to label lead source, intent, "
        "follow-up status, or any CRM categorization. Tags are case-sensitive."
    )
    args_schema: Type[BaseModel] = TagContactInput

    def _run(self, contact_id: str, tags: list[str]) -> str:
        data = _safe_request(
            "POST",
            f"{GHL_BASE}/contacts/{contact_id}/tags",
            json={"tags": tags},
        )
        if "error" in data:
            return f"GHL tag failed: {data['error']}"
        return f"Tags {tags} added to contact {contact_id}."


# ── Search Pipeline / Opportunities ──────────────────────────────────────────

class SearchPipelineInput(BaseModel):
    pipeline_id: str = Field(default="", description="Filter to a specific pipeline ID (optional)")
    stage_id: str = Field(default="", description="Filter to a specific stage ID (optional)")
    query: str = Field(default="", description="Search term for opportunity name or contact")
    limit: int = Field(default=20, description="Max opportunities to return")
    status: str = Field(default="open", description="open, won, lost, or abandoned")


class GHLSearchPipelineTool(BaseTool):
    name: str = "ghl_search_pipeline"
    description: str = (
        "Search opportunities (deals) in GHL pipelines. Returns deal name, value, "
        "stage, assigned user, and last activity. Use to check pipeline health, "
        "find stale deals, or identify high-value opportunities."
    )
    args_schema: Type[BaseModel] = SearchPipelineInput

    def _run(
        self,
        pipeline_id: str = "",
        stage_id: str = "",
        query: str = "",
        limit: int = 20,
        status: str = "open",
    ) -> str:
        params = {
            "location_id": _location_id(),
            "limit": min(limit, 100),
            "status": status,
        }
        if pipeline_id:
            params["pipeline_id"] = pipeline_id
        if stage_id:
            params["pipeline_stage_id"] = stage_id
        if query:
            params["q"] = query  # v2 search param is "q" — "query" is a 422

        data = _safe_request("GET", f"{GHL_BASE}/opportunities/search", params=params)
        if "error" in data:
            return f"GHL pipeline search failed: {data['error']}"

        opps = data.get("opportunities", [])
        if not opps:
            return "No opportunities found."

        rows = []
        for o in opps:
            name = o.get("name", "(unnamed)")
            value = f"${o.get('monetaryValue', 0):,.0f}"
            stage = o.get("pipelineStage", {}).get("name", "?") if o.get("pipelineStage") else "?"
            updated = (o.get("lastStatusChangeAt") or o.get("updatedAt") or "")[:10]
            rows.append(f"ID:{o['id']} | {name} | {value} | stage:{stage} | updated:{updated}")

        return f"Found {len(opps)} opportunit(ies):\n" + "\n".join(rows)


# ── Merge Duplicate Contacts ──────────────────────────────────────────────────

class MergeContactsInput(BaseModel):
    primary_contact_id: str = Field(
        ..., description="The contact ID to KEEP (merge into)"
    )
    secondary_contact_id: str = Field(
        ..., description="The contact ID to MERGE AND DELETE"
    )


class GHLMergeContactsTool(BaseTool):
    name: str = "ghl_merge_contacts"
    description: str = (
        "Merge two duplicate GHL contacts. The secondary contact's data is merged "
        "into the primary, and the secondary is deleted. IRREVERSIBLE — confirm "
        "both IDs are correct before calling. Use ghl_read_contacts to verify first."
    )
    args_schema: Type[BaseModel] = MergeContactsInput

    def _run(self, primary_contact_id: str, secondary_contact_id: str) -> str:
        data = _safe_request(
            "POST",
            f"{GHL_BASE}/contacts/{primary_contact_id}/merge",
            json={"sourcContactId": secondary_contact_id},  # GHL API spelling
        )
        if "error" in data:
            return f"GHL merge failed: {data['error']}"
        return (
            f"Merged contact {secondary_contact_id} into {primary_contact_id}. "
            f"Secondary contact has been deleted."
        )


# ── Tool registry (maps YAML can_call_tools names to classes) ─────────────────

GHL_TOOL_REGISTRY: dict[str, type] = {
    "ghl.read_contacts": GHLReadContactsTool,
    "ghl.update_contact": GHLUpdateContactTool,
    "ghl.add_tag": GHLAddTagTool,
    "ghl.search_pipeline": GHLSearchPipelineTool,
    "ghl.merge_duplicates": GHLMergeContactsTool,
    # alias used in some YAMLs
    "ghl.read_pipeline": GHLSearchPipelineTool,
    "ghl.read_contact": GHLReadContactsTool,
}


def get_ghl_tools(tool_names: list[str]) -> list:
    """Instantiate GHL tools from a list of YAML can_call_tools names."""
    tools = []
    for name in tool_names:
        cls = GHL_TOOL_REGISTRY.get(name)
        if cls:
            tools.append(cls())
    return tools
