"""Thin Slack Web API wrapper for the task workflow.

Kept separate from slack_notifier so the two workflows don't accidentally
couple. Everything here speaks raw JSON to Slack over requests — no Bolt
framework, matches the rest of the codebase.
"""

import logging
import requests

from config import get_slack_bot_token


logger = logging.getLogger(__name__)

_SLACK_API = "https://slack.com/api"


def _headers() -> dict:
    return {
        "Authorization": f"Bearer {get_slack_bot_token()}",
        "Content-Type": "application/json; charset=utf-8",
    }


def post_message(
    *,
    channel: str,
    text: str,
    blocks: list[dict] | None = None,
    thread_ts: str | None = None,
) -> dict:
    """Post a new message to a channel (or a thread reply if thread_ts set)."""
    payload: dict = {
        "channel": channel,
        "text": text,
        "unfurl_links": False,
        "unfurl_media": False,
    }
    if blocks:
        payload["blocks"] = blocks
    if thread_ts:
        payload["thread_ts"] = thread_ts
    response = requests.post(
        f"{_SLACK_API}/chat.postMessage", headers=_headers(), json=payload
    )
    return response.json()


def update_message(
    *,
    channel: str,
    ts: str,
    text: str,
    blocks: list[dict] | None = None,
) -> dict:
    """Edit an existing message in place (used to refresh task cards)."""
    payload: dict = {"channel": channel, "ts": ts, "text": text}
    if blocks:
        payload["blocks"] = blocks
    response = requests.post(
        f"{_SLACK_API}/chat.update", headers=_headers(), json=payload
    )
    return response.json()


def post_ephemeral(
    *,
    channel: str,
    user: str,
    text: str,
    blocks: list[dict] | None = None,
) -> dict:
    """Post a message only visible to one user in a channel."""
    payload: dict = {
        "channel": channel,
        "user": user,
        "text": text,
        "unfurl_links": False,
    }
    if blocks:
        payload["blocks"] = blocks
    response = requests.post(
        f"{_SLACK_API}/chat.postEphemeral", headers=_headers(), json=payload
    )
    return response.json()


def open_view(*, trigger_id: str, view: dict) -> dict:
    """Open a modal. `trigger_id` comes from the slash command / interaction."""
    payload = {"trigger_id": trigger_id, "view": view}
    response = requests.post(
        f"{_SLACK_API}/views.open", headers=_headers(), json=payload
    )
    return response.json()


def open_dm(user_id: str) -> str | None:
    """Open an IM channel with a user and return its channel id."""
    response = requests.post(
        f"{_SLACK_API}/conversations.open",
        headers=_headers(),
        json={"users": user_id},
    )
    data = response.json()
    if not data.get("ok"):
        logger.warning(f"conversations.open failed: {data.get('error')}")
        return None
    return data.get("channel", {}).get("id")


def dm_user(user_id: str, text: str, blocks: list[dict] | None = None) -> dict:
    """Send a direct message to a user. Opens an IM if needed."""
    channel = open_dm(user_id)
    if not channel:
        return {"ok": False, "error": "could_not_open_dm"}
    return post_message(channel=channel, text=text, blocks=blocks)
