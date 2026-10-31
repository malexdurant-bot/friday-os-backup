"""
Xcerebro 2.0 — Approval Manager

Sends approval requests to Slack and tracks them in Postgres via the
GovernanceStore (survives restarts — an approval created before a deploy
can still be resolved after it).

The Slack message uses Block Kit buttons whose clicks POST to
/slack/interactions, which resolves the approval and resumes the agent.
"""

import uuid
from typing import Any, Optional

from loguru import logger
from slack_sdk.web.async_client import AsyncWebClient
from slack_sdk.errors import SlackApiError

from tools.governance import GovernanceStore


class ApprovalManager:
    """Posts approval requests to Slack; persistence lives in GovernanceStore."""

    def __init__(
        self,
        slack_token: Optional[str],
        approval_channel: Optional[str],
        store: Optional[GovernanceStore] = None,
    ):
        self.slack_token = slack_token
        self.approval_channel = approval_channel
        self.store = store
        self.slack_client: Optional[AsyncWebClient] = None

        if slack_token:
            try:
                self.slack_client = AsyncWebClient(token=slack_token)
                logger.info("Slack approval manager initialized")
            except Exception as e:
                logger.error(f"Failed to init Slack client: {e}")

    async def request(
        self,
        title: str,
        description: str,
        context: dict,
        invocation_id: str,
        agent_id: Optional[str] = None,
        action_type: Optional[str] = None,
    ) -> str:
        """
        Create a new approval request (persisted) and post it to Slack.
        Returns the approval_id.
        """
        if self.store:
            approval_id = self.store.create_approval(
                agent_id=agent_id or "",
                task=description,
                context=context,
                action_type=action_type,
                invocation_id=invocation_id,
                title=title,
            )
        else:
            approval_id = str(uuid.uuid4())
            logger.warning(f"No governance store — approval {approval_id} not persisted")

        record = {
            "id": approval_id,
            "title": title,
            "description": description,
            "action_type": action_type,
        }

        if self.slack_client and self.approval_channel:
            try:
                await self.slack_client.chat_postMessage(
                    channel=self.approval_channel,
                    text=f"🔔 Approval needed: {title}",
                    blocks=self._build_approval_blocks(record),
                )
                logger.info(f"Approval {approval_id} posted to Slack")
            except SlackApiError as e:
                logger.error(f"Slack post failed: {e}")
        else:
            logger.warning(f"Approval {approval_id} created but no Slack configured")

        return approval_id

    async def resolve(
        self,
        approval_id: str,
        decision: str,
        approver: str,
        note: Optional[str] = None,
    ) -> dict:
        """Mark an approval resolved (approve/reject). Returns the full record."""
        record = None
        if self.store:
            record = self.store.resolve_approval(approval_id, decision, approver, note)
        if not record:
            raise ValueError(f"Approval {approval_id} not found or already resolved")

        if self.slack_client and self.approval_channel:
            emoji = "✅" if decision == "approve" else "❌"
            try:
                await self.slack_client.chat_postMessage(
                    channel=self.approval_channel,
                    text=f"{emoji} {record['title']} → {decision} by {approver}",
                )
            except SlackApiError as e:
                logger.error(f"Slack resolution post failed: {e}")

        return record

    async def notify(self, text: str) -> None:
        """Post a plain alert to the approval channel (spend cap, rate limit, etc.)."""
        if self.slack_client and self.approval_channel:
            try:
                await self.slack_client.chat_postMessage(
                    channel=self.approval_channel, text=text
                )
            except SlackApiError as e:
                logger.error(f"Slack alert failed: {e}")

    def get_pending(self) -> list[dict]:
        return self.store.pending_approvals() if self.store else []

    @staticmethod
    def _build_approval_blocks(record: dict) -> list[dict]:
        """Slack Block Kit message with Approve/Reject buttons."""
        action_line = f"\n*Action type:* `{record['action_type']}`" if record.get("action_type") else ""
        return [
            {
                "type": "header",
                "text": {"type": "plain_text", "text": f"🔔 {record['title']}"[:150]},
            },
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": f"*Task:*\n{record['description'][:2500]}{action_line}",
                },
            },
            {
                "type": "context",
                "elements": [
                    {"type": "mrkdwn", "text": f"Approval ID: `{record['id']}`"},
                ],
            },
            {
                "type": "actions",
                "elements": [
                    {
                        "type": "button",
                        "text": {"type": "plain_text", "text": "Approve"},
                        "style": "primary",
                        "value": f"approve:{record['id']}",
                        "action_id": "approval_approve",
                    },
                    {
                        "type": "button",
                        "text": {"type": "plain_text", "text": "Reject"},
                        "style": "danger",
                        "value": f"reject:{record['id']}",
                        "action_id": "approval_reject",
                    },
                ],
            },
        ]
