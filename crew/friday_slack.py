"""Owner-only signed Slack ingress, durable processing and bounded reply delivery."""
import asyncio
import html
import json
import re
import uuid

import httpx
from fastapi import HTTPException
from loguru import logger
from slack_sdk.signature import SignatureVerifier

from config import settings
from comms import handle_chat


def require_slack_configuration():
    if not settings.slack_enabled:
        raise ValueError("Slack is disabled")
    for field in ("slack_bot_token", "slack_signing_secret", "slack_team_id",
                  "slack_owner_user_id", "slack_app_id", "slack_bot_user_id",
                  "slack_chat_channel_id"):
        if not getattr(settings, field):
            raise ValueError(f"Slack requires {field.upper()}")


async def accept_event(request):
    if settings.slack_socket_mode:
        raise HTTPException(403, "HTTP Slack ingress is disabled in Socket Mode")
    try:
        require_slack_configuration()
    except ValueError:
        raise HTTPException(503, "Slack configuration is incomplete")
    body = bytearray()
    async for chunk in request.stream():
        body.extend(chunk)
        if len(body) > 65536:
            raise HTTPException(413, "Slack event is too large")
    if not SignatureVerifier(settings.slack_signing_secret).is_valid_request(
        bytes(body), dict(request.headers)
    ):
        raise HTTPException(401, "Invalid or expired Slack signature")
    try:
        data = json.loads(body)
    except (ValueError, UnicodeDecodeError):
        raise HTTPException(400, "Invalid Slack JSON")
    if not isinstance(data, dict):
        raise HTTPException(400, "Invalid Slack envelope")
    if data.get("type") == "url_verification":
        challenge = data.get("challenge")
        if not isinstance(challenge, str) or not 0 < len(challenge) <= 1024:
            raise HTTPException(400, "Invalid Slack challenge")
        return {"challenge": challenge}
    return await accept_payload(data, request.app.state.slack_inbox)


async def accept_payload(data, inbox):
    """Authorize a transport-authenticated envelope and persist before acknowledgement."""
    require_slack_configuration()
    if not isinstance(data, dict):
        raise HTTPException(400, "Invalid Slack envelope")
    if data.get("type") != "event_callback":
        return {"ok": True, "ignored": True}
    if data.get("team_id") != settings.slack_team_id or data.get("api_app_id") != settings.slack_app_id:
        raise HTTPException(403, "Slack workspace or app is not allowed")
    event = data.get("event")
    if not isinstance(event, dict):
        raise HTTPException(400, "Invalid Slack event")
    if event.get("bot_id") or event.get("subtype") or event.get("type") != "app_mention":
        return {"ok": True, "ignored": True}
    if event.get("user") != settings.slack_owner_user_id or event.get("channel") != settings.slack_chat_channel_id:
        raise HTTPException(403, "Slack sender or channel is not allowed")
    if event.get("user") == settings.slack_bot_user_id:
        return {"ok": True, "ignored": True}
    message = event.get("text")
    if not isinstance(message, str):
        raise HTTPException(400, "Slack message must be text")
    mention = f"<@{settings.slack_bot_user_id}>"
    if mention not in message:
        return {"ok": True, "ignored": True}
    message = message.replace(mention, "").strip()
    if not message:
        return {"ok": True, "ignored": True}
    if len(message) > 4000:
        raise HTTPException(413, "Slack message is too long")
    event_id, ts = data.get("event_id"), event.get("ts")
    thread = event.get("thread_ts") or ts
    if not isinstance(event_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", event_id):
        raise HTTPException(400, "Invalid Slack event ID")
    if any(not isinstance(v, str) or not re.fullmatch(r"\d{1,20}\.\d{1,10}", v) for v in (ts, thread)):
        raise HTTPException(400, "Invalid Slack timestamp")
    record = {"event_id": event_id, "team_id": data["team_id"], "app_id": data["api_app_id"],
              "channel_id": event["channel"], "owner_id": event["user"],
              "event_ts": ts, "thread_ts": thread, "message": message}
    try:
        inserted = await asyncio.wait_for(
            asyncio.to_thread(inbox.enqueue, record), timeout=2,
        )
    except Exception:
        # A timed-out write might have committed: Slack may retry, and the
        # unique event/message keys still prevent double processing.
        raise HTTPException(503, "Slack event could not be acknowledged durably")
    return {"ok": True, "duplicate": not inserted}


class SlackDeliveryError(RuntimeError):
    def __init__(self, code, uncertain=False):
        self.code = code
        self.uncertain = uncertain
        super().__init__(code)


def post_reply(record):
    require_slack_configuration()
    if (record["team_id"] != settings.slack_team_id
            or record["app_id"] != settings.slack_app_id
            or record["channel_id"] != settings.slack_chat_channel_id
            or record["owner_id"] != settings.slack_owner_user_id):
        raise SlackDeliveryError("destination_not_allowed")
    try:
        with httpx.Client(timeout=15) as client:
            response = client.post("https://slack.com/api/chat.postMessage",
                headers={"Authorization": f"Bearer {settings.slack_bot_token}"},
                json={"channel": record["channel_id"], "thread_ts": record["thread_ts"],
                      "text": html.escape(record["reply"][:12000], quote=False),
                      "client_msg_id": str(uuid.uuid5(uuid.NAMESPACE_URL, record["team_id"] + record["event_id"])),
                      "unfurl_links": False, "unfurl_media": False, "parse": "none"})
        if response.status_code == 429:
            raise SlackDeliveryError("slack_rate_limited")
        if response.status_code != 200:
            raise SlackDeliveryError("slack_http_error", uncertain=True)
        data = response.json()
        if not data.get("ok"):
            raise SlackDeliveryError("slack_rejected_message")
        if not data.get("ts") or data.get("channel") != record["channel_id"]:
            raise SlackDeliveryError("slack_invalid_receipt", uncertain=True)
        return data["ts"]
    except (httpx.HTTPError, ValueError, AttributeError):
        raise SlackDeliveryError("slack_delivery_unknown", uncertain=True) from None


async def process_record(app, record):
    store = app.state.slack_inbox
    event_id = record["event_id"]
    # Recheck persisted destinations before generation as well as delivery.
    if (record["team_id"] != settings.slack_team_id
            or record["app_id"] != settings.slack_app_id
            or record["channel_id"] != settings.slack_chat_channel_id
            or record["owner_id"] != settings.slack_owner_user_id):
        store.transition(event_id, record["state"], "failed", error_code="destination_not_allowed")
        return
    if record["state"] == "queued":
        store.transition(event_id, "queued", "processing")
        try:
            out = await handle_chat(app, "slack",
                f"slack:{record['team_id']}:{record['channel_id']}:{record['thread_ts']}",
                record["owner_id"], record["message"])
            reply = out["reply"]
        except Exception:
            store.transition(event_id, "processing", "failed", error_code="chat_failed")
            return
        store.transition(event_id, "processing", "reply_ready", reply=reply)
        record = {**record, "state": "reply_ready", "reply": reply}
    store.transition(event_id, "reply_ready", "delivering")
    try:
        ts = await asyncio.to_thread(post_reply, record)
    except SlackDeliveryError as error:
        store.transition(event_id, "delivering", "uncertain" if error.uncertain else "failed",
                         error_code=error.code)
        return
    store.transition(event_id, "delivering", "delivered", reply_ts=ts)
    app.state.audit.log_event(event_type="slack.reply.delivered", agent_id="friday-operations",
                             payload={"event_id": event_id, "reply_ts": ts})


def run_worker(app, stop):
    # This only drains owner-triggered inbox events; it never schedules agents.
    while not stop.is_set():
        try:
            with app.state.slack_inbox.worker_lock() as acquired:
                if acquired:
                    app.state.slack_inbox.recover_interrupted()
                    record = app.state.slack_inbox.next_ready()
                    if record:
                        asyncio.run(process_record(app, record))
        except Exception:
            logger.error("Slack worker failed; durable state retained for review")
        stop.wait(0.5)
