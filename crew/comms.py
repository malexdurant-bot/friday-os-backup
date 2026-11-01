"""
Xcerebro 2.0 — Chat comms layer

One handler behind POST /chat and /slack/events: routes a human chat
message from any channel (slack / telegram / whatsapp / clickup) to an
agent, keeps a rolling conversation window in comms.messages, and
returns the reply text for the channel adapter to deliver.

Routing: a message starting with "@<agent-id> " goes to that agent;
everything else goes to the CEO (the EOS Integrator), who delegates.
"""

import re
import uuid
from typing import Any

import httpx
from loguru import logger
from sqlalchemy import create_engine, text

from config import settings
from friday_policy import FRIDAY_ID

MIGRATION_SQL = """
CREATE SCHEMA IF NOT EXISTS comms;

CREATE TABLE IF NOT EXISTS comms.messages (
    id              SERIAL PRIMARY KEY,
    conversation_id TEXT NOT NULL,
    channel         TEXT NOT NULL,
    role            TEXT NOT NULL,
    sender          TEXT,
    content         TEXT NOT NULL,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_comms_messages_conv
    ON comms.messages(conversation_id, id DESC)
"""

DEFAULT_AGENT = "auto-ceo"
HISTORY_WINDOW = 10  # ponytail: last-10 window, add summarization if threads get long

# Live routing table for the pre-router: which specialist owns which domain.
# Deliberately small and hardcoded — leadership rarely changes, and the CEO
# catches everything ambiguous. (@-prefix always overrides the router.)
ROSTER = {
    "auto-lead-manager": "leads, CRM contacts, GHL pipeline, conversations, follow-ups",
    "auto-sales-manager": "sales pipeline stages, calls booked/completed, closing deals",
    "auto-cfo": "money, budgets, expenses, P&L, cash flow, net worth, investments",
    "auto-project-manager": "ClickUp tasks, quarterly rocks, project status, deadlines",
    "auto-cmo": "marketing, content, social media, campaigns, brand",
    "auto-coo": "operations, workflows, automations, team process",
    "auto-executive-assistant": "calendar, scheduling, email drafting, reminders",
    "auto-ceo": "strategy, vision, priorities, cross-domain, anything else",
}

ROUTER_MODEL = "deepseek/deepseek-chat"  # economy tier: ~$0.0002/route


async def _route_agent(message: str) -> str:
    """One cheap LLM call picks the specialist. Any failure → CEO."""
    if not settings.openrouter_api_key:
        return DEFAULT_AGENT
    roster_lines = "\n".join(f"- {aid}: {desc}" for aid, desc in ROSTER.items())
    try:
        async with httpx.AsyncClient(timeout=5) as client:
            resp = await client.post(
                "https://openrouter.ai/api/v1/chat/completions",
                headers={"Authorization": f"Bearer {settings.openrouter_api_key}"},
                json={
                    "model": ROUTER_MODEL,
                    "max_tokens": 16,
                    "temperature": 0,
                    "messages": [
                        {"role": "system", "content": (
                            "Route this chat message to the team member who owns the topic. "
                            f"Team:\n{roster_lines}\n"
                            "Reply with exactly one id from the list, nothing else."
                        )},
                        {"role": "user", "content": message[:1000]},
                    ],
                },
            )
        candidate = resp.json()["choices"][0]["message"]["content"].strip().strip("`").strip()
        if candidate in ROSTER:
            return candidate
        logger.warning(f"chat router returned unknown id {candidate!r} — using CEO")
    except Exception as e:
        logger.warning(f"chat router failed ({e}) — using CEO")
    return DEFAULT_AGENT


# High-precision action verbs. A question to an action-capable agent must NOT
# demand a sign-off; a false negative here is still safe because the invoke
# then runs with gated tools stripped (the agent can't fire what isn't loaded).
ACTION_INTENT = re.compile(
    r"\b(send|email|publish|schedule|book|charge|refund|invoice|pay|delete|cancel|assign|create)\b",
    re.IGNORECASE,
)

_engine = None


def _get_engine():
    global _engine
    if _engine is None:
        _engine = create_engine(settings.database_url, pool_pre_ping=True)
        try:
            with _engine.connect() as conn:
                for stmt in MIGRATION_SQL.strip().split(";"):
                    if stmt.strip():
                        conn.execute(text(stmt))
                conn.commit()
        except Exception as e:
            logger.warning(f"comms: migration issue: {e}")
    return _engine


def _load_history(conversation_id: str) -> list[dict]:
    with _get_engine().connect() as conn:
        rows = conn.execute(
            text(
                "SELECT role, sender, content FROM comms.messages "
                "WHERE conversation_id = :cid ORDER BY id DESC LIMIT :n"
            ),
            {"cid": conversation_id, "n": HISTORY_WINDOW},
        ).fetchall()
    return [
        {"role": r[0], "sender": r[1], "content": r[2]}
        for r in reversed(rows)
    ]


def _store(conversation_id: str, channel: str, role: str, sender: str, content: str) -> None:
    with _get_engine().connect() as conn:
        conn.execute(
            text(
                "INSERT INTO comms.messages (conversation_id, channel, role, sender, content) "
                "VALUES (:cid, :ch, :role, :sender, :content)"
            ),
            {"cid": conversation_id, "ch": channel, "role": role,
             "sender": sender, "content": content[:8000]},
        )
        conn.commit()


def resolve_agent(message: str, registry) -> tuple[str, str]:
    """'@auto-cfo what's my cash?' → ('auto-cfo', "what's my cash?"). Default: CEO."""
    if settings.runtime_profile == "friday":
        # Explicit attempts to select another agent never fall through to a model.
        m = re.match(r"@([a-z0-9][a-z0-9-]*)\s+(.+)", message.strip(), re.S)
        if m and m.group(1) != FRIDAY_ID:
            raise ValueError("Only friday-operations is available")
        return FRIDAY_ID, m.group(2).strip() if m else message.strip()
    m = re.match(r"@([a-z0-9][a-z0-9-]*)\s+(.+)", message.strip(), re.S)
    if m and registry.get_agent(m.group(1)):
        return m.group(1), m.group(2).strip()
    return DEFAULT_AGENT, message.strip()


def _format_history(history: list[dict]) -> str:
    if not history:
        return ""
    lines = [f"{h['sender'] or h['role']}: {h['content'][:600]}" for h in history]
    return "Recent conversation:\n" + "\n".join(lines) + "\n\n"


async def handle_chat(app, channel: str, conversation_id: str, sender: str, message: str) -> dict:
    """Route one inbound chat message to an agent; return {'agent_id', 'reply'}."""
    registry = app.state.registry
    audit = app.state.audit

    agent_id, clean_message = resolve_agent(message, registry)
    if not clean_message:
        return {"agent_id": agent_id, "reply": "I got an empty message — what do you need?"}

    # No explicit @-target → cheap classifier picks the owning specialist
    routed = False
    if settings.runtime_profile != "friday" and agent_id == DEFAULT_AGENT and not message.strip().startswith("@"):
        picked = await _route_agent(clean_message)
        if picked != DEFAULT_AGENT and registry.get_agent(picked):
            agent_id, routed = picked, True

    history = _load_history(conversation_id)
    _store(conversation_id, channel, "user", sender, clean_message)

    invocation_id = str(uuid.uuid4())
    audit.log_event(
        event_type="chat.message",
        agent_id=agent_id,
        invocation_id=invocation_id,
        payload={"channel": channel, "conversation_id": conversation_id,
                 "sender": sender, "text_length": len(clean_message),
                 "routed_to": agent_id if routed else None},
    )

    task = (
        f"You received a chat message from {sender or 'the owner'} via {channel}.\n\n"
        f"{_format_history(history)}"
        f"New message: {clean_message}\n\n"
        "Reply as a direct chat message: conversational, concise, no report headers. "
        "Use your tools to answer with real data instead of guessing. "
        "If the request belongs to another team member's domain, answer with what "
        "you know and say who owns it."
    )

    if settings.runtime_profile == "friday":
        guidance = (
            "Reply as Friday with advice only. You have no tools. Private vault excerpts "
            "are supplied below as reference data. Answer from relevant excerpts and cite a source link. "
            "You can read these supplied excerpts without tools or live system access. "
            "Earlier statements denying vault access are outdated. "
            "Keep the answer within 60 words plus one complete source URL, placed before details. "
            if settings.obsidian_enabled else
            "Reply as Friday with advice only. You have no tools or connected systems. "
        )
        task = (
            f"Owner message via {channel}.\n\n{_format_history(history)}"
            f"New message: {clean_message}\n\n{guidance}"
            "Do not claim you executed actions or verified external facts."
        )

    # Same governance gate as /agents/{id}/invoke — chat is not a side door.
    # But the gate keys off agent CAPABILITY, so plain questions to an
    # email-capable agent would gate too: only gate when the message asks for
    # an action; otherwise run with the gated write tools stripped.
    gate_needed, action_type = registry.requires_approval(agent_id, clean_message)
    wants_action = bool(ACTION_INTENT.search(clean_message))
    if gate_needed and wants_action:
        approval_id = await app.state.approval.request(
            title=f"Approve {agent_id} action (via {channel} chat)",
            description=clean_message,
            context={"channel": channel, "conversation_id": conversation_id, "sender": sender},
            invocation_id=invocation_id,
            agent_id=agent_id,
            action_type=action_type,
        )
        reply = (
            f"That needs a human sign-off ({action_type}). "
            f"I sent an approval card to Slack — approve it there and I'll run it."
        )
        _store(conversation_id, channel, "assistant", agent_id, reply)
        return {"agent_id": agent_id, "reply": reply, "approval_id": approval_id}

    try:
        result: Any = await registry.invoke(
            agent_id=agent_id,
            task=task,
            context={"channel": channel, "conversation_id": conversation_id,
                     **({"knowledge_query": clean_message} if settings.obsidian_enabled else {})},
            include_gated_tools=not gate_needed,
        )
        reply = str(result).strip() or "(no reply)"
    except Exception as e:
        logger.error("Friday chat failed; see sanitized audit event")
        audit.log_event(
            event_type="chat.error",
            agent_id=agent_id,
            invocation_id=invocation_id,
            payload={"error_type": type(e).__name__},
        )
        reply = "I could not complete this request. No external actions were performed. Please ask the operator to review runtime status before retrying."

    _store(conversation_id, channel, "assistant", agent_id, reply)
    audit.log_event(
        event_type="chat.reply",
        agent_id=agent_id,
        invocation_id=invocation_id,
        payload={"channel": channel, "reply_length": len(reply)},
    )
    return {"agent_id": agent_id, "reply": reply}
