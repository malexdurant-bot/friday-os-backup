"""Outbound Socket Mode receiver; shares the durable, owner-only Slack inbox."""
import asyncio
import json
import os
from urllib.parse import urlparse

import httpx
import websocket
from fastapi import HTTPException
from loguru import logger

from config import settings
from friday_slack import accept_payload, require_slack_configuration


async def receive_envelope(envelope, inbox, acknowledge):
    if not isinstance(envelope, dict):
        return
    envelope_id = envelope.get("envelope_id")
    if not isinstance(envelope_id, str) or not envelope_id or len(envelope_id) > 256:
        return
    if envelope.get("type") == "events_api":
        try:
            await accept_payload(envelope.get("payload"), inbox)
        except HTTPException as error:
            if error.status_code >= 500:
                return  # No acknowledgement until storage succeeds; Slack may retry.
            # Permanently rejected actor/body: acknowledge without processing.
    await acknowledge(json.dumps({"envelope_id": envelope_id}))


def connect():
    # The app token is used only through the configured HTTPS proxy.
    with httpx.Client(timeout=15) as client:
        response = client.post("https://slack.com/api/apps.connections.open",
            headers={"Authorization": "Bearer " + settings.slack_app_token})
        response.raise_for_status()
        data = response.json()
    url = data.get("url", "")
    parsed = urlparse(url)
    if not data.get("ok") or parsed.scheme != "wss" or not (parsed.hostname or "").endswith(".slack.com"):
        raise RuntimeError("Socket Mode connection unavailable")
    proxy = urlparse(os.environ.get("HTTPS_PROXY") or os.environ.get("https_proxy") or "")
    options = {"timeout": 15, "enable_multithread": True}
    if proxy.hostname:
        options.update(http_proxy_host=proxy.hostname, http_proxy_port=proxy.port or 80,
                       proxy_type="http")
    if proxy.username:
        options["http_proxy_auth"] = (proxy.username, proxy.password or "")
    # Never log the temporary WebSocket URL: it contains authentication material.
    return websocket.create_connection(url, **options)


async def run(app):
    require_slack_configuration()
    if not settings.slack_socket_mode or not settings.slack_app_token:
        raise RuntimeError("Socket Mode requires SLACK_SOCKET_MODE and SLACK_APP_TOKEN")
    while True:
        connection = None
        try:
            connection = await asyncio.to_thread(connect)
            logger.info("Slack Socket Mode connected")
            async def acknowledge(message):
                await asyncio.to_thread(connection.send, message)
            while True:
                try:
                    raw = await asyncio.to_thread(connection.recv)
                except websocket.WebSocketTimeoutException:
                    await asyncio.to_thread(connection.ping)
                    continue
                if not raw:
                    break
                if len(raw) > 65536:
                    break
                envelope = json.loads(raw)
                if isinstance(envelope, dict) and envelope.get("type") == "disconnect":
                    break
                await receive_envelope(envelope, app.state.slack_inbox, acknowledge)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.warning("Socket Mode connection interrupted; inbox preserved")
        finally:
            if connection:
                await asyncio.to_thread(connection.close)
        await asyncio.sleep(5)
