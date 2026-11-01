import asyncio
from unittest.mock import AsyncMock

from test_friday_slack import configured, envelope, send
from config import settings
from slack_socket import receive_envelope


def test_socket_durable_ack_and_duplicate(configured):
    _, records, inbox = configured
    ack = AsyncMock()
    item = {"type": "events_api", "envelope_id": "socket-1", "payload": envelope()}
    async def acknowledged(message):
        assert "EvTEST" in records
        await ack(message)
    asyncio.run(receive_envelope(item, inbox, acknowledged))
    asyncio.run(receive_envelope(item, inbox, acknowledged))
    assert len(records) == 1 and ack.await_count == 2


def test_socket_database_failure_no_ack(configured):
    _, records, inbox = configured
    inbox.enqueue.side_effect = RuntimeError("private database error")
    ack = AsyncMock()
    asyncio.run(receive_envelope({"type": "events_api", "envelope_id": "1", "payload": envelope()}, inbox, ack))
    ack.assert_not_awaited()
    assert not records


def test_socket_forbidden_owner_ack_without_enqueue(configured):
    _, records, inbox = configured
    body = envelope()
    body["event"]["user"] = "UOTHER"
    ack = AsyncMock()
    asyncio.run(receive_envelope({"type": "events_api", "envelope_id": "1", "payload": body}, inbox, ack))
    ack.assert_awaited_once()
    inbox.enqueue.assert_not_called()


def test_socket_mode_blocks_http(configured, monkeypatch):
    monkeypatch.setattr(settings, "slack_socket_mode", True)
    assert send(configured[0], envelope()).status_code == 403
