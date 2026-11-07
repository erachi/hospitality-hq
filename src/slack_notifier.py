"""Slack notification formatting and posting.

Posts a lean Block Kit guest alert to #guest-alerts. The alert carries only
the four things that drive triage — property, message time, situation so
far, draft response — because the Thread Interactions handler lets anyone
pull booking details or history from the thread on demand.

Never sends messages to guests. Drafts are for VJ or Maggie to review and
send manually.
"""

import re
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import requests

from config import get_slack_bot_token, SLACK_CHANNEL_ID


# `[HOST: ...]` notes are host-only annotations the drafter uses to flag
# uncertainty. They must not reach guests, so we split them out of the draft
# body and render them in a separate Slack block labeled accordingly.
_HOST_NOTE_RE = re.compile(r"\[HOST:\s*([^\]]+)\]")


def _split_host_notes(draft: str) -> tuple[str, list[str]]:
    """Return (cleaned_draft, [note_text, ...]). Non-greedy, single-line."""
    if not draft:
        return "", []
    notes = [m.group(1).strip() for m in _HOST_NOTE_RE.finditer(draft)]
    cleaned = _HOST_NOTE_RE.sub("", draft)
    # Removing a note mid-sentence leaves artefacts; collapse them.
    cleaned = re.sub(r"[ \t]{2,}", " ", cleaned)
    cleaned = re.sub(r"\n[ \t]+", "\n", cleaned)
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)
    return cleaned.strip(), notes


URGENCY_EMOJI = {
    "HIGH": "🔴",
    "MEDIUM": "🟡",
    "LOW": "🟢",
}

CATEGORY_LABEL = {
    "URGENT_MAINTENANCE": "Urgent Maintenance",
    "COMPLAINT": "Guest Complaint",
    "PRE_ARRIVAL": "Pre-Arrival Question",
    "GENERAL": "General Inquiry",
    "POSITIVE": "Positive Feedback",
}

# Retained for thread Q&A — not rendered in the lean top-level alert.
STATUS_DISPLAY = {
    "accepted": "Confirmed",
    "checkpoint": "Checked In",
    "inquiry": "Inquiry",
    "cancelled": "Cancelled",
    "declined": "Declined",
}
SOURCE_DISPLAY = {
    "airbnb": "Airbnb",
    "vrbo": "VRBO",
    "booking": "Booking.com",
    "direct": "Direct",
    "homeaway": "HomeAway",
}

# Slack Block Kit hard limit per text field.
_MAX_TEXT_LEN = 3000

# Both properties are in Scottsdale; move into per-property KB when a
# third property with a different tz comes online.
_DEFAULT_TZ = "America/Phoenix"

# The python3.12 Lambda runtime (Amazon Linux 2023) ships without the system
# tz database, so ZoneInfo() raises unless the `tzdata` package is bundled.
# Phoenix has no DST, so a fixed MST offset is an exact fallback.
_FALLBACK_TZ = timezone(timedelta(hours=-7), "MST")


def _get_tz(tz_name: str):
    try:
        return ZoneInfo(tz_name)
    except (ZoneInfoNotFoundError, ValueError):
        return _FALLBACK_TZ


def _truncate(text: str, limit: int = _MAX_TEXT_LEN) -> str:
    if len(text) <= limit:
        return text
    return text[: limit - 3] + "..."


def _format_source(source: str) -> str:
    if not source:
        return ""
    return SOURCE_DISPLAY.get(source.lower(), source)


def _format_status(status: str) -> str:
    if not status:
        return ""
    return STATUS_DISPLAY.get(status.lower(), status)


def _format_source_status(source: str, status: str) -> str:
    source_s = _format_source(source)
    status_s = _format_status(status)
    if source_s and status_s:
        return f"{source_s} · {status_s}"
    return source_s or status_s or "—"


def _format_message_time(iso_ts: str, tz_name: str = _DEFAULT_TZ) -> str:
    """Human-friendly local time for the triggering message.

    Returns strings like "today 3:12 PM MST", "yesterday 11:04 AM MST",
    or "Apr 18 10:22 AM MST" for older messages. Returns the raw input on
    parse failure so a weird timestamp never crashes the alert.
    """
    if not iso_ts:
        return ""
    try:
        s = iso_ts
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        tz = _get_tz(tz_name)
        local = dt.astimezone(tz)
    except (ValueError, TypeError):
        return iso_ts

    now_local = datetime.now(tz)
    time_part = local.strftime("%-I:%M %p %Z")
    today = now_local.date()
    msg_date = local.date()
    if msg_date == today:
        return f"today {time_part}"
    if (today - msg_date).days == 1:
        return f"yesterday {time_part}"
    return f"{local.strftime('%b %-d')} {time_part}"


def _build_blocks(
    *,
    guest_name: str,
    property_name: str,
    checkin_date: str,
    checkout_date: str,
    guest_message: str,
    classification: dict,
    draft_response: str,
    reservation_uuid: str,
    booking_source: str,
    reservation_status: str,
    is_repeat_guest: bool,
    recent_messages: list[dict],
    conversation_summary: str,
    message_timestamp: str,
    is_inquiry: bool = False,
    stay_channel_id: str = "",
) -> list[dict]:
    urgency = classification.get("urgency", "MEDIUM")
    category = classification.get("category", "GENERAL")
    summary = (classification.get("summary") or "").strip()
    descriptor = (classification.get("descriptor") or "").strip()
    if not descriptor:
        descriptor = CATEGORY_LABEL.get(category, category)

    urgency_icon = URGENCY_EMOJI.get(urgency, "🟡")

    # ---- 1. Prefix line: urgency · descriptor · property · local time ----
    prefix_parts = [f"{urgency_icon} *{descriptor}*", property_name]
    local_time = _format_message_time(message_timestamp)
    if local_time:
        prefix_parts.append(local_time)
    if is_inquiry:
        # Not booked yet — show the requested dates, since they decide
        # whether this lead is worth chasing.
        stay = f"{checkin_date} → {checkout_date}" if checkin_date and checkout_date else ""
        prefix_parts.append(f"📩 *Inquiry — not booked*{' · ' + stay if stay else ''}")
    prefix_text = " · ".join(prefix_parts)

    # ---- 2. Situation: AI 2-bullet summary, or fallback to classifier line ----
    situation_text = (conversation_summary or "").strip() or (summary or "_(no summary available)_")

    # ---- 3. New guest message, quoted under a `Guest · <name>:` label ----
    guest_label = f"*Guest · {guest_name}:*" if guest_name else "*Guest:*"
    guest_quote = guest_message.strip() or "_(no message body)_"
    guest_block_text = _truncate(f"{guest_label}\n> {guest_quote}")

    # ---- 4. Draft response (strip [HOST:...] notes so they can't leak) ----
    cleaned_draft, host_notes = _split_host_notes(draft_response or "")
    draft = cleaned_draft.strip()
    draft_block_text = _truncate(f"*Draft reply:*\n> {draft}") if draft else "*Draft reply:*\n> _(no draft)_"
    if host_notes:
        notes_bullets = "\n".join(f"• {n}" for n in host_notes)
        host_notes_block_text = _truncate(
            f"*Host notes (not sent to guest):*\n{notes_bullets}"
        )
    else:
        host_notes_block_text = ""

    # ---- 5. Footer: reservation id + action text ----
    response_needed = classification.get("response_needed", True)
    if response_needed:
        action_text = "Copy draft & send via Hospitable · React ❌ to skip"
    else:
        action_text = "No response needed unless you'd like to reply."
    footer_text = f"`{reservation_uuid}` · {action_text}"

    blocks = [
        {
            "type": "context",
            "elements": [{"type": "mrkdwn", "text": _truncate(prefix_text)}],
        },
        {
            "type": "section",
            "text": {"type": "mrkdwn", "text": _truncate(situation_text)},
        },
        {
            "type": "section",
            "text": {"type": "mrkdwn", "text": guest_block_text},
        },
        {
            "type": "section",
            "text": {"type": "mrkdwn", "text": draft_block_text},
        },
    ]
    if host_notes_block_text:
        blocks.append(
            {
                "type": "section",
                "text": {"type": "mrkdwn", "text": host_notes_block_text},
            }
        )
    blocks.append(
        {
            "type": "context",
            "elements": [{"type": "mrkdwn", "text": _truncate(footer_text)}],
        }
    )
    if stay_channel_id:
        blocks.append(
            {
                "type": "context",
                "elements": [
                    {
                        "type": "mrkdwn",
                        "text": f"🧵 Open stay channel → <#{stay_channel_id}>",
                    }
                ],
            }
        )
    return blocks


def post_guest_alert(
    guest_name: str,
    property_name: str,
    checkin_date: str,
    checkout_date: str,
    guest_message: str,
    classification: dict,
    draft_response: str,
    reservation_uuid: str,
    booking_source: str = "",
    reservation_status: str = "",
    is_repeat_guest: bool = False,
    recent_messages: list[dict] = None,
    conversation_summary: str = "",
    message_timestamp: str = "",
    is_inquiry: bool = False,
    stay_channel_id: str = "",
) -> dict:
    """Post a Block Kit guest alert to the Slack channel.

    `booking_source`, `reservation_status`, `is_repeat_guest`, and
    `recent_messages` are accepted for backward compatibility with the two
    callers but are not rendered in the lean top-level alert — thread Q&A
    exposes them on demand.

    Returns the Slack API response.
    """
    urgency = classification.get("urgency", "MEDIUM")
    category = classification.get("category", "GENERAL")
    summary = classification.get("summary", "")
    descriptor = (classification.get("descriptor") or "").strip() or CATEGORY_LABEL.get(category, category)

    urgency_icon = URGENCY_EMOJI.get(urgency, "🟡")

    # Mobile push-notification fallback — the terse one-liner the OS shows
    # on a locked phone before Block Kit renders.
    fallback_text = f"{urgency_icon} {descriptor} — {property_name}: {summary}"
    if is_inquiry:
        fallback_text = f"{urgency_icon} Inquiry · {descriptor} — {property_name}: {summary}"

    blocks = _build_blocks(
        guest_name=guest_name,
        property_name=property_name,
        checkin_date=checkin_date,
        checkout_date=checkout_date,
        guest_message=guest_message,
        classification=classification,
        draft_response=draft_response,
        reservation_uuid=reservation_uuid,
        booking_source=booking_source,
        reservation_status=reservation_status,
        is_repeat_guest=is_repeat_guest,
        recent_messages=recent_messages or [],
        conversation_summary=conversation_summary,
        message_timestamp=message_timestamp,
        is_inquiry=is_inquiry,
        stay_channel_id=stay_channel_id,
    )

    response = requests.post(
        "https://slack.com/api/chat.postMessage",
        headers={
            "Authorization": f"Bearer {get_slack_bot_token()}",
            "Content-Type": "application/json",
        },
        json={
            "channel": SLACK_CHANNEL_ID,
            "text": fallback_text,
            "blocks": blocks,
            "unfurl_links": False,
            "unfurl_media": False,
        },
    )

    return response.json()


class StayChannelSlackClient:
    """Thin Slack Web API wrapper for the stay-channel provisioner.

    Methods return the parsed Slack response dict so the provisioner can
    branch on `ok` / `error`. All calls include the bot token and use
    the same `requests.post` style as `post_guest_alert`.
    """

    _SLACK_BASE = "https://slack.com/api"

    def _headers(self) -> dict:
        return {
            "Authorization": f"Bearer {get_slack_bot_token()}",
            "Content-Type": "application/json; charset=utf-8",
        }

    def create_private_channel(self, name: str) -> dict:
        """`conversations.create` with is_private=True."""
        r = requests.post(
            f"{self._SLACK_BASE}/conversations.create",
            headers=self._headers(),
            json={"name": name, "is_private": True},
        )
        return r.json()

    def invite_users(self, channel_id: str, user_ids: list[str]) -> dict:
        """`conversations.invite`. Returns the raw response.

        Slack accepts up to 1000 users per call; our invite list is ≤ 5
        so no batching needed. `already_in_channel` errors are logged
        by the caller but not retried.
        """
        if not channel_id or not user_ids:
            return {"ok": True, "noop": True}
        r = requests.post(
            f"{self._SLACK_BASE}/conversations.invite",
            headers=self._headers(),
            json={"channel": channel_id, "users": ",".join(user_ids)},
        )
        return r.json()

    def post_message(
        self,
        channel_id: str,
        *,
        text: str,
        blocks: list[dict] | None = None,
    ) -> dict:
        """`chat.postMessage`. Returns the raw response (incl. `ts`)."""
        payload: dict = {
            "channel": channel_id,
            "text": text,
            "unfurl_links": False,
            "unfurl_media": False,
        }
        if blocks:
            payload["blocks"] = blocks
        r = requests.post(
            f"{self._SLACK_BASE}/chat.postMessage",
            headers=self._headers(),
            json=payload,
        )
        return r.json()

    def pin_message(self, channel_id: str, ts: str) -> dict:
        """`pins.add`. Non-fatal — caller shrugs on failure."""
        r = requests.post(
            f"{self._SLACK_BASE}/pins.add",
            headers=self._headers(),
            json={"channel": channel_id, "timestamp": ts},
        )
        return r.json()


def post_thread_reply(thread_ts: str, text: str, channel: str | None = None) -> dict:
    """Post a plain-text reply in an existing Slack thread.

    Used by the thread event handler to answer questions or confirm note/issue
    saves. Keeps formatting simple (markdown via Slack's mrkdwn) — rich Block Kit
    is reserved for the initial alert.
    """
    response = requests.post(
        "https://slack.com/api/chat.postMessage",
        headers={
            "Authorization": f"Bearer {get_slack_bot_token()}",
            "Content-Type": "application/json",
        },
        json={
            "channel": channel or SLACK_CHANNEL_ID,
            "thread_ts": thread_ts,
            "text": text,
            "unfurl_links": False,
            "unfurl_media": False,
        },
    )
    return response.json()
