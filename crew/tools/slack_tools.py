"""
Xcerebro 2.0 — Slack Tools

Lets agents post messages to Slack channels. Uses the sync WebClient
(CrewAI tools run synchronously) with the same bot token as the
approval/notification system.
"""

import json
from typing import Type

from crewai.tools import BaseTool
from loguru import logger
from pydantic import BaseModel, Field
from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError

from config import settings


class PostMessageInput(BaseModel):
    text: str = Field(description="The message to post (Slack mrkdwn supported)")
    channel: str = Field(
        default="",
        description="Channel ID to post to. Leave empty for the default notifications channel.",
    )


class SlackPostMessageTool(BaseTool):
    name: str = "slack_post_message"
    description: str = (
        "Post a message to a Slack channel. Defaults to the team notifications "
        "channel; pass a channel ID to post elsewhere."
    )
    args_schema: Type[BaseModel] = PostMessageInput

    def _run(self, text: str, channel: str = "") -> str:
        token = settings.slack_bot_token
        target = channel or settings.slack_notifications_channel_id
        if not token or not target:
            return json.dumps({"error": "Slack not configured (missing bot token or channel)."})
        try:
            client = WebClient(token=token)
            resp = client.chat_postMessage(channel=target, text=text)
            return json.dumps({"ok": True, "ts": resp["ts"], "channel": resp["channel"]})
        except SlackApiError as e:
            logger.error(f"slack_post_message failed: {e}")
            return json.dumps({"error": str(e)[:300]})


def get_slack_tools(tool_names: list[str]) -> list:
    tools = []
    if any(n.startswith("slack.post_message") for n in tool_names):
        tools.append(SlackPostMessageTool())
    return tools
