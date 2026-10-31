"""
Xcerebro 2.0 — CrewAI Agent Runtime
====================================
FastAPI service that runs the autonomous agent workforce.

Endpoints:
    GET  /health                     — health check (used by Railway, Docker)
    POST /agents/{agent_id}/invoke   — invoke a single agent
    POST /crews/{crew_id}/run        — run a coordinated crew
    POST /workflows/trigger          — generic webhook trigger from n8n
    GET  /agents                     — list all available agents
    GET  /crews                      — list all available crews
    POST /approvals/{approval_id}    — human approval response (manual/API)
    POST /slack/interactions         — Slack button clicks (approve/reject)
    POST /chat                       — channel-agnostic chat (n8n adapters post here)
    POST /slack/events               — Slack Events API (chat channel, DMs, mentions)
    GET  /approvals                  — pending approvals
    GET  /spend                      — LLM spend summary + daily cap
    GET  /governance/trust           — graduated-autonomy counters + flip candidates
    POST /governance/trust/flip      — human flips auto-approve for (agent, action)
    GET  /audit                      — recent agent activity log

Architecture:
    n8n triggers → POST /workflows/trigger → CrewAI agents run →
    sensitive actions hit LangGraph approval gate → Slack message →
    human approves/rejects → action executes or aborts
"""

import asyncio
import json
import os
import re
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, date
from pathlib import Path
from typing import Any, Optional

import httpx

import yaml
from fastapi import BackgroundTasks, FastAPI, HTTPException, Header, Depends, Request
from fastapi.responses import JSONResponse
from loguru import logger
from pydantic import BaseModel, Field

from comms import handle_chat
from crews.registry import CrewRegistry
from tools.audit import AuditLogger
from tools.approval import ApprovalManager
from tools.governance import GovernanceStore
from config import settings

# ============================================================
# LIFESPAN — Startup / Shutdown
# ============================================================

@asynccontextmanager
async def lifespan(app: FastAPI):
    """Initialize agent registry and shared services on startup."""
    logger.info("Xcerebro 2.0 Agent Runtime starting up...")

    # Restricted profile loads only the Friday definition.
    app.state.registry = CrewRegistry(agents_path=settings.agents_path)
    app.state.registry.load_all()

    app.state.audit = AuditLogger(database_url=settings.database_url)
    app.state.governance = GovernanceStore(database_url=settings.database_url)
    if settings.runtime_profile == "friday" and (
        not app.state.audit.enabled or not app.state.governance.enabled
    ):
        raise RuntimeError("Friday requires working audit and governance storage")
    app.state.registry.set_governance(app.state.governance)
    app.state.approval = ApprovalManager(
        slack_token=None if settings.runtime_profile == "friday" else settings.slack_bot_token,
        approval_channel=settings.slack_approval_channel_id,
        store=app.state.governance,
    )
    app.state.last_cap_alert_date = None  # dedupe daily spend-cap Slack alerts

    # n8n watchdog: the scheduler died silently twice (Jul 1, Jul 8) and nobody
    # noticed for days. This always-on service now checks it every 10 minutes.
    app.state.n8n_watchdog = None
    if settings.runtime_profile != "friday":
        app.state.n8n_watchdog = asyncio.create_task(_n8n_watchdog(app))

    logger.info(f"Loaded {len(app.state.registry.agents)} agents, "
                f"{len(app.state.registry.crews)} crews")
    app.state.slack_worker = None
    if settings.slack_enabled:
        import threading
        from friday_slack import require_slack_configuration, run_worker
        from slack_inbox import SlackInbox
        require_slack_configuration()
        app.state.slack_inbox = SlackInbox(settings.database_url)
        app.state.slack_stop = threading.Event()
        app.state.slack_worker = asyncio.create_task(
            asyncio.to_thread(run_worker, app, app.state.slack_stop))
    try:
        yield
    finally:
        if app.state.slack_worker:
            app.state.slack_stop.set()
            await app.state.slack_worker
            app.state.slack_inbox.engine.dispose()
        logger.info("Xcerebro 2.0 Agent Runtime shutting down...")


async def _n8n_watchdog(app: FastAPI) -> None:
    """
    Ping n8n /healthz every 10 minutes. Alert Slack once per outage and once
    on recovery. n8n's own DB has frozen silently twice — this is the alarm.
    """
    if not settings.n8n_base_url:
        logger.warning("n8n watchdog disabled: N8N_BASE_URL not set")
        return
    url = settings.n8n_base_url.rstrip("/") + "/healthz"
    down = False
    while True:
        try:
            async with httpx.AsyncClient(timeout=15) as client:
                resp = await client.get(url)
            healthy = resp.status_code == 200
        except Exception:
            healthy = False

        if not healthy and not down:
            down = True
            logger.error("n8n watchdog: n8n is DOWN")
            await app.state.approval.notify(
                "🚨 *n8n is DOWN* — scheduled workflows (daily brief, CRM crons, "
                "email/calendar bridges) are not running. Check the n8n project on Railway "
                "(its Postgres has a history of freezing — restart Postgres, then n8n)."
            )
        elif healthy and down:
            down = False
            logger.info("n8n watchdog: n8n recovered")
            await app.state.approval.notify("✅ n8n is back up — scheduled workflows resumed.")

        await asyncio.sleep(600)


app = FastAPI(
    title="Xcerebro 2.0 Agent Runtime",
    description="Autonomous AI workforce powered by CrewAI + LangGraph",
    version="2.0.0",
    lifespan=lifespan,
)


# The foundation exposes only its review/local-invocation surface. Slack stays
# disabled until signature verification, deduplication and owner restrictions pass.
@app.middleware("http")
async def friday_surface(request: Request, call_next):
    if settings.runtime_profile == "friday":
        allowed = {
            ("GET", "/"), ("GET", "/health"), ("GET", "/agents"),
            ("GET", "/crews"), ("GET", "/spend"), ("GET", "/audit"),
            ("GET", "/docs"), ("GET", "/openapi.json"), ("GET", "/redoc"),
            ("GET", "/docs/oauth2-redirect"),
            ("POST", "/agents/friday-operations/invoke"), ("POST", "/chat"),
        }
        if settings.slack_enabled:
            allowed.add(("POST", "/slack/events"))
        if (request.method, request.url.path) not in allowed:
            return JSONResponse(status_code=403, content={"detail": "Disabled in the Friday foundation"})
    return await call_next(request)


# ============================================================
# AUTH
# ============================================================

async def verify_api_key(x_api_key: Optional[str] = Header(None)):
    """Verify the runtime API key. n8n and other services pass this."""
    if not settings.agent_runtime_api_key:
        if settings.runtime_profile == "friday":
            raise HTTPException(status_code=503, detail="Runtime API key is not configured")
        return True
    if x_api_key != settings.agent_runtime_api_key:
        raise HTTPException(status_code=401, detail="Invalid API key")
    return True


# ============================================================
# HEALTH
# ============================================================

@app.get("/health")
async def health():
    """Health check endpoint (Railway + Docker use this)."""
    return {
        "status": "ok",
        "service": "xcerebro-2.0-agent-runtime",
        "version": "2.0.0",
        "agents_loaded": len(app.state.registry.agents) if hasattr(app.state, "registry") else 0,
        "crews_loaded": len(app.state.registry.crews) if hasattr(app.state, "registry") else 0,
        "timestamp": datetime.utcnow().isoformat(),
    }


# ============================================================
# AGENT INVOCATION
# ============================================================

class AgentInvokeRequest(BaseModel):
    """Request to invoke a single agent."""
    task: str = Field(..., description="The task description for the agent")
    context: dict[str, Any] = Field(default_factory=dict, description="Additional context")
    require_approval: Optional[bool] = Field(default=None, description="Override default approval requirement")
    callback_url: Optional[str] = Field(default=None, description="Webhook URL to POST result to")


class AgentInvokeResponse(BaseModel):
    invocation_id: str
    agent_id: str
    status: str
    result: Optional[Any] = None
    error: Optional[str] = None
    requires_approval: bool = False
    approval_id: Optional[str] = None


@app.post("/agents/{agent_id}/invoke", response_model=AgentInvokeResponse)
async def invoke_agent(
    agent_id: str,
    req: AgentInvokeRequest,
    request: Request,
    _auth: bool = Depends(verify_api_key),
):
    """
    Invoke a single agent by ID.

    Example:
        POST /agents/auto-cmo/invoke
        Body: {"task": "Plan content for this week", "context": {"audience": "AI"}}
    """
    invocation_id = str(uuid.uuid4())
    registry: CrewRegistry = request.app.state.registry
    audit: AuditLogger = request.app.state.audit

    agent = registry.get_agent(agent_id)
    if not agent:
        raise HTTPException(404, f"Agent '{agent_id}' not found")

    audit.log_event(
        event_type="agent.invoke.start",
        agent_id=agent_id,
        invocation_id=invocation_id,
        payload={"task": req.task, "context": req.context},
    )

    try:
        # Determine if this action requires human approval
        # (agent YAML permissions + action-type defaults + earned auto-approve)
        gate_needed, action_type = registry.requires_approval(agent_id, req.task)
        needs_approval = (
            req.require_approval if req.require_approval is not None else gate_needed
        )

        if needs_approval:
            # Send approval request to Slack and pause
            approval: ApprovalManager = request.app.state.approval
            approval_id = await approval.request(
                title=f"Approve {agent_id} action",
                description=req.task,
                context=req.context,
                invocation_id=invocation_id,
                agent_id=agent_id,
                action_type=action_type,
            )
            audit.log_event(
                event_type="agent.approval.requested",
                agent_id=agent_id,
                invocation_id=invocation_id,
                payload={"approval_id": approval_id},
            )
            return AgentInvokeResponse(
                invocation_id=invocation_id,
                agent_id=agent_id,
                status="awaiting_approval",
                requires_approval=True,
                approval_id=approval_id,
            )

        # Otherwise, execute directly
        result = await registry.invoke(
            agent_id=agent_id,
            task=req.task,
            context=req.context,
        )

        audit.log_event(
            event_type="agent.invoke.complete",
            agent_id=agent_id,
            invocation_id=invocation_id,
            payload={"result_preview": str(result)[:500]},
        )

        return AgentInvokeResponse(
            invocation_id=invocation_id,
            agent_id=agent_id,
            status="complete",
            result=result,
        )

    except Exception as e:
        logger.error("Friday invocation failed; inspect sanitized audit event")
        audit.log_event(
            event_type="agent.invoke.error",
            agent_id=agent_id,
            invocation_id=invocation_id,
            payload={"error_type": type(e).__name__},
        )
        if settings.runtime_profile != "friday":
            await _maybe_alert_cap(request.app, str(e))
        return AgentInvokeResponse(
            invocation_id=invocation_id,
            agent_id=agent_id,
            status="error",
            error="Invocation failed; see runtime audit status",
        )


async def _maybe_alert_cap(app: FastAPI, error: str) -> None:
    """Slack-alert spend-cap / rate-limit refusals, once per day to avoid cron spam."""
    if "SPEND_CAP" not in error and "RATE_LIMIT" not in error:
        return
    if app.state.last_cap_alert_date == date.today():
        return
    app.state.last_cap_alert_date = date.today()
    await app.state.approval.notify(f"🛑 Agent runtime kill switch tripped: {error}")


# ============================================================
# CREW EXECUTION
# ============================================================

class CrewRunRequest(BaseModel):
    """Request to run a coordinated crew."""
    inputs: dict[str, Any] = Field(default_factory=dict)
    callback_url: Optional[str] = None


@app.post("/crews/{crew_id}/run")
async def run_crew(
    crew_id: str,
    req: CrewRunRequest,
    request: Request,
    _auth: bool = Depends(verify_api_key),
):
    """
    Run a coordinated crew (multiple agents working together).

    Example:
        POST /crews/daily-kpi-brief/run
        Body: {"inputs": {"date": "2026-04-30"}}
    """
    registry: CrewRegistry = request.app.state.registry
    audit: AuditLogger = request.app.state.audit

    crew = registry.get_crew(crew_id)
    if not crew:
        raise HTTPException(404, f"Crew '{crew_id}' not found")

    invocation_id = str(uuid.uuid4())
    audit.log_event(
        event_type="crew.run.start",
        agent_id=crew_id,
        invocation_id=invocation_id,
        payload={"inputs": req.inputs},
    )

    try:
        result = await registry.run_crew(crew_id, req.inputs)
        audit.log_event(
            event_type="crew.run.complete",
            agent_id=crew_id,
            invocation_id=invocation_id,
            payload={"result_preview": str(result)[:500]},
        )
        return {
            "invocation_id": invocation_id,
            "crew_id": crew_id,
            "status": "complete",
            "result": result,
        }
    except Exception as e:
        logger.exception(f"Error running crew {crew_id}")
        audit.log_event(
            event_type="crew.run.error",
            agent_id=crew_id,
            invocation_id=invocation_id,
            payload={"error_type": type(e).__name__},
        )
        await _maybe_alert_cap(request.app, str(e))
        raise HTTPException(500, f"Crew execution failed: {e}")


# ============================================================
# WORKFLOW TRIGGER (n8n calls this)
# ============================================================

class WorkflowTriggerRequest(BaseModel):
    """Generic trigger from n8n. Routes to the right agent or crew."""
    workflow_name: str
    payload: dict[str, Any] = Field(default_factory=dict)
    source: Optional[str] = None  # e.g., "comment_keyword", "schedule", "manual"


@app.post("/workflows/trigger")
async def trigger_workflow(
    req: WorkflowTriggerRequest,
    request: Request,
    _auth: bool = Depends(verify_api_key),
):
    """
    Generic workflow trigger entrypoint. n8n posts to this when a
    scheduled flow fires or a webhook event happens.
    """
    registry: CrewRegistry = request.app.state.registry
    invocation_id = str(uuid.uuid4())

    logger.info(f"Workflow triggered: {req.workflow_name} from {req.source}")

    # Look up the workflow → crew mapping
    crew_id = registry.workflow_to_crew(req.workflow_name)
    if not crew_id:
        raise HTTPException(404, f"No crew mapped to workflow '{req.workflow_name}'")

    result = await registry.run_crew(crew_id, req.payload)
    return {
        "invocation_id": invocation_id,
        "workflow": req.workflow_name,
        "crew_id": crew_id,
        "status": "complete",
        "result": result,
    }


# ============================================================
# DISCOVERY ENDPOINTS
# ============================================================

@app.get("/agents")
async def list_agents(request: Request, _auth: bool = Depends(verify_api_key)):
    """List all loaded agents (Tier A + Tier B)."""
    registry: CrewRegistry = request.app.state.registry
    return {
        "friday": registry.list_tier("friday"),
        "tier_a": registry.list_tier("a"),
        "tier_b": registry.list_tier("b"),
        "total": len(registry.agents),
    }


@app.get("/crews")
async def list_crews(request: Request, _auth: bool = Depends(verify_api_key)):
    """List all defined crews."""
    registry: CrewRegistry = request.app.state.registry
    return {
        "crews": registry.list_crews(),
        "total": len(registry.crews),
    }


# ============================================================
# APPROVAL CALLBACK (Slack hits this)
# ============================================================

class ApprovalDecision(BaseModel):
    decision: str  # "approve" | "reject"
    approver: str
    note: Optional[str] = None


@app.post("/approvals/{approval_id}")
async def handle_approval(
    approval_id: str,
    decision: ApprovalDecision,
    request: Request,
    _auth: bool = Depends(verify_api_key),
):
    """Receive a human approval decision (from Slack or admin UI)."""
    approval: ApprovalManager = request.app.state.approval
    audit: AuditLogger = request.app.state.audit

    record = await approval.resolve(
        approval_id=approval_id,
        decision=decision.decision,
        approver=decision.approver,
        note=decision.note,
    )

    audit.log_event(
        event_type=f"approval.{decision.decision}",
        agent_id=record.get("agent_id"),
        invocation_id=record.get("invocation_id"),
        payload={"approver": decision.approver, "note": decision.note},
    )

    # Graduated autonomy: approve increments the clean counter, reject resets it
    gov: GovernanceStore = request.app.state.governance
    gov.record_decision(
        agent_id=record.get("agent_id", ""),
        action_type=record.get("action_type"),
        approved=decision.decision == "approve",
    )

    if decision.decision == "approve":
        # Resume execution of the paused agent invocation
        registry: CrewRegistry = request.app.state.registry
        result = await registry.resume_after_approval(record)
        return {"approval_id": approval_id, "status": "resumed", "result": result}

    return {"approval_id": approval_id, "status": "rejected"}


# ============================================================
# SLACK INTERACTIVITY (button clicks land here)
# ============================================================

async def _resume_approved(app: FastAPI, record: dict) -> None:
    """Background task: run the approved invocation and audit the outcome."""
    registry: CrewRegistry = app.state.registry
    audit: AuditLogger = app.state.audit
    try:
        result = await registry.resume_after_approval(record)
        audit.log_event(
            event_type="agent.invoke.complete",
            agent_id=record.get("agent_id"),
            invocation_id=record.get("invocation_id"),
            payload={"result_preview": str(result)[:500], "resumed_from": record.get("id")},
        )
        await app.state.approval.notify(
            f"✅ `{record.get('agent_id')}` finished approved task:\n{str(result)[:1500]}"
        )
    except Exception as e:
        logger.exception("Resume after approval failed")
        audit.log_event(
            event_type="agent.invoke.error",
            agent_id=record.get("agent_id"),
            invocation_id=record.get("invocation_id"),
            payload={"error": str(e), "resumed_from": record.get("id")},
        )
        await app.state.approval.notify(
            f"⚠️ `{record.get('agent_id')}` approved task FAILED: {str(e)[:500]}"
        )


@app.post("/slack/interactions")
async def slack_interactions(request: Request, background: BackgroundTasks):
    """
    Slack interactivity endpoint. Approve/Reject button clicks POST here as
    form-encoded `payload=<json>`. Verified with the Slack signing secret —
    NOT the runtime API key (Slack can't send custom headers).
    """
    body = await request.body()

    if settings.slack_signing_secret:
        from slack_sdk.signature import SignatureVerifier
        verifier = SignatureVerifier(signing_secret=settings.slack_signing_secret)
        if not verifier.is_valid_request(body, dict(request.headers)):
            raise HTTPException(401, "Invalid Slack signature")
    else:
        raise HTTPException(503, "Slack signing secret is required")

    form = await request.form()
    payload = json.loads(form.get("payload", "{}"))

    actions = payload.get("actions", [])
    if not actions:
        return {"ok": True}

    value = actions[0].get("value", "")  # "approve:<id>" | "reject:<id>"
    if ":" not in value:
        return {"ok": True}
    decision_word, approval_id = value.split(":", 1)
    decision = "approve" if decision_word == "approve" else "reject"
    approver = (payload.get("user") or {}).get("username") or (payload.get("user") or {}).get("id", "slack-user")

    approval: ApprovalManager = request.app.state.approval
    audit: AuditLogger = request.app.state.audit
    gov: GovernanceStore = request.app.state.governance

    try:
        record = await approval.resolve(
            approval_id=approval_id, decision=decision, approver=approver
        )
    except ValueError:
        # Already resolved (double click) — acknowledge quietly
        return {"ok": True, "note": "already resolved"}

    audit.log_event(
        event_type=f"approval.{decision}",
        agent_id=record.get("agent_id"),
        invocation_id=record.get("invocation_id"),
        payload={"approver": approver, "via": "slack_button"},
    )
    gov.record_decision(
        agent_id=record.get("agent_id", ""),
        action_type=record.get("action_type"),
        approved=decision == "approve",
    )

    if decision == "approve":
        # Slack needs a response within 3s — run the agent in the background
        background.add_task(_resume_approved, request.app, record)

    return {"ok": True}


# ============================================================
# CHAT — talk to the team from Slack / Telegram / WhatsApp / ClickUp
# ============================================================

class ChatRequest(BaseModel):
    channel: str = Field(..., description="slack | telegram | whatsapp | clickup")
    conversation_id: str = Field(..., description="Stable id for the conversation thread")
    sender: str = Field(default="", description="Who is talking, e.g. kenny | angel")
    text: str = Field(..., description="The message")


@app.post("/chat")
async def chat(req: ChatRequest, request: Request, _auth: bool = Depends(verify_api_key)):
    """
    Channel-agnostic chat entrypoint. n8n channel adapters (Telegram,
    WhatsApp, ClickUp) POST here and deliver the reply back themselves.
    '@<agent-id> ...' targets a specific agent; default is the CEO.
    """
    try:
        return await handle_chat(
            request.app, req.channel, req.conversation_id, req.sender, req.text
        )
    except ValueError:
        raise HTTPException(400, "Invalid chat request or unavailable agent")


def _slack_verified(body: bytes, headers: dict) -> bool:
    if not settings.slack_signing_secret:
        return False
    from slack_sdk.signature import SignatureVerifier
    return SignatureVerifier(
        signing_secret=settings.slack_signing_secret
    ).is_valid_request(body, headers)


async def _slack_chat_reply(app: FastAPI, channel_id: str, thread_ts: str,
                            sender: str, text_in: str) -> None:
    """Background task: run the chat and post the reply into the thread."""
    try:
        out = await handle_chat(app, "slack", f"slack:{channel_id}:{thread_ts}", sender, text_in)
        from slack_sdk import WebClient
        WebClient(token=settings.slack_bot_token).chat_postMessage(
            channel=channel_id, thread_ts=thread_ts, text=out["reply"]
        )
    except Exception:
        logger.exception("slack chat reply failed")


@app.post("/slack/events")
async def slack_events(request: Request, background: BackgroundTasks):
    """
    Slack Events API. Answers every message in the dedicated chat channel
    (SLACK_CHAT_CHANNEL_ID), bot DMs, and @mentions anywhere. Verified with
    the Slack signing secret; ack within 3s, agent runs in the background.
    """
    if settings.runtime_profile == "friday":
        from friday_slack import accept_event
        return await accept_event(request)
    body = await request.body()
    if not _slack_verified(body, dict(request.headers)):
        raise HTTPException(401, "Invalid Slack signature")

    data = json.loads(body)
    if data.get("type") == "url_verification":
        return {"challenge": data.get("challenge")}

    event = data.get("event", {})
    etype = event.get("type")
    # Ignore our own posts, edits, joins, etc. — only fresh human messages
    if event.get("bot_id") or event.get("subtype"):
        return {"ok": True}

    channel_id = event.get("channel", "")
    text_in = (event.get("text") or "").strip()
    is_chat_channel = bool(settings.slack_chat_channel_id) and channel_id == settings.slack_chat_channel_id

    if etype == "app_mention":
        if is_chat_channel:
            return {"ok": True}  # the message event already handles this channel
        text_in = re.sub(r"<@[A-Z0-9]+>\s*", "", text_in).strip()
    elif etype == "message":
        if event.get("channel_type") != "im" and not is_chat_channel:
            return {"ok": True}
    else:
        return {"ok": True}

    if not text_in:
        return {"ok": True}

    thread_ts = event.get("thread_ts") or event.get("ts", "")
    sender = event.get("user", "slack-user")
    background.add_task(_slack_chat_reply, request.app, channel_id, thread_ts, sender, text_in)
    return {"ok": True}


# ============================================================
# GOVERNANCE — spend + trust visibility (weekly digest reads these)
# ============================================================

@app.get("/spend")
async def get_spend(request: Request, days: int = 7, _auth: bool = Depends(verify_api_key)):
    """LLM spend summary: today's total (vs cap), by agent, by model."""
    gov: GovernanceStore = request.app.state.governance
    summary = gov.spend_summary(days=days)
    summary["daily_cap_usd"] = settings.max_daily_llm_spend_usd
    return summary


@app.get("/governance/trust")
async def get_trust(request: Request, _auth: bool = Depends(verify_api_key)):
    """Trust counters per (agent, action_type) + flip candidates at threshold."""
    gov: GovernanceStore = request.app.state.governance
    return gov.trust_summary(threshold=settings.trust_auto_approve_threshold)


class TrustFlipRequest(BaseModel):
    agent_id: str
    action_type: str
    auto_approve: bool


@app.post("/governance/trust/flip")
async def flip_trust(
    req: TrustFlipRequest, request: Request, _auth: bool = Depends(verify_api_key)
):
    """Human-initiated auto-approve flip. The system never calls this itself."""
    gov: GovernanceStore = request.app.state.governance
    ok = gov.set_auto_approve(req.agent_id, req.action_type, req.auto_approve)
    if not ok:
        raise HTTPException(404, f"No trust record for {req.agent_id}/{req.action_type}")
    request.app.state.audit.log_event(
        event_type="trust.flip",
        agent_id=req.agent_id,
        payload={"action_type": req.action_type, "auto_approve": req.auto_approve},
    )
    return {"agent_id": req.agent_id, "action_type": req.action_type, "auto_approve": req.auto_approve}


@app.get("/approvals")
async def list_pending_approvals(request: Request, _auth: bool = Depends(verify_api_key)):
    """All pending approvals (also visible as Slack cards)."""
    approval: ApprovalManager = request.app.state.approval
    return {"pending": approval.get_pending()}


# ============================================================
# AUDIT LOG
# ============================================================

@app.get("/audit")
async def get_audit_log(
    request: Request,
    limit: int = 100,
    agent_id: Optional[str] = None,
    _auth: bool = Depends(verify_api_key),
):
    """Recent agent activity log."""
    audit: AuditLogger = request.app.state.audit
    return {"events": audit.recent(limit=limit, agent_id=agent_id)}


# ============================================================
# ROOT
# ============================================================

@app.get("/")
async def root():
    return {
        "service": "Xcerebro 2.0 Agent Runtime",
        "docs": "/docs",
        "health": "/health",
    }
