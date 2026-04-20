"""Slack Web API helpers for the expense-capture workflow.

Kept separate from task_slack_client so the two workflows don't
accidentally couple — matches the convention set by task_*.
"""

import logging
import requests

from config import get_slack_bot_token


logger = logging.getLogger(__name__)

_SLACK_API = "https://slack.com/api"
_FILE_DOWNLOAD_TIMEOUT = 15


def _headers_json() -> dict:
    return {
        "Authorization": f"Bearer {get_slack_bot_token()}",
        "Content-Type": "application/json; charset=utf-8",
    }


def _headers_auth_only() -> dict:
    return {"Authorization": f"Bearer {get_slack_bot_token()}"}


def post_message(
    *,
    channel: str,
    text: str,
    blocks: list[dict] | None = None,
    thread_ts: str | None = None,
) -> dict:
    """Post a new message, or a thread reply when thread_ts is given."""
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
        f"{_SLACK_API}/chat.postMessage",
        headers=_headers_json(),
        json=payload,
    )
    return response.json()


def update_message(
    *,
    channel: str,
    ts: str,
    text: str,
    blocks: list[dict] | None = None,
) -> dict:
    payload: dict = {
        "channel": channel,
        "ts": ts,
        "text": text,
    }
    if blocks:
        payload["blocks"] = blocks
    response = requests.post(
        f"{_SLACK_API}/chat.update",
        headers=_headers_json(),
        json=payload,
    )
    return response.json()


def add_reaction(*, channel: str, timestamp: str, name: str) -> dict:
    """Add an emoji reaction to a message. `name` has no colons."""
    response = requests.post(
        f"{_SLACK_API}/reactions.add",
        headers=_headers_json(),
        json={"channel": channel, "timestamp": timestamp, "name": name},
    )
    return response.json()


def files_info(file_id: str) -> dict:
    """Fetch metadata for an uploaded file."""
    response = requests.get(
        f"{_SLACK_API}/files.info",
        headers=_headers_auth_only(),
        params={"file": file_id},
    )
    return response.json()


def download_file(url: str) -> bytes:
    """Download a private Slack file using the bot token.

    Slack's `url_private_download` requires Bearer auth; an anonymous GET
    returns an HTML login page, not the image.
    """
    response = requests.get(
        url,
        headers=_headers_auth_only(),
        timeout=_FILE_DOWNLOAD_TIMEOUT,
    )
    response.raise_for_status()
    return response.content
