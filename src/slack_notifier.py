"""Slack notification formatting and posting.

Uses Slack Block Kit to render rich guest alerts with booking context, recent
message history, and a draft response. Never sends messages to guests —
drafts are for VJ or Maggie to review and send manually.
"""

import requests
from config import get_slack_bot_token, SLACK_CHANNEL_ID


URGENCY_EMOJI = {
    "HIGH": "🔴",
    "MEDIUM": "🟡",
    "LOW": "🟢",
}

CATEGORY_EMOJI = {
    "URGENT_MAINTENANCE": "🔧",
    "COMPLAINT": "😤",
    "PRE_ARRIVAL": "✈️",
    "GENERAL": "💬",
    "POSITIVE": "⭐",
}

CATEGORY_LABEL = {
    "URGENT_MAINTENANCE": "Urgent Maintenance",
    "COMPLAINT": "Guest Complaint",
    "PRE_ARRIVAL": "Pre-Arrival Question",
    "GENERAL": "General Inquiry",
    "POSITIVE": "Positive Feedback",
}

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


def _truncate(text: str, limit: int = _MAX_TEXT_LEN) -> str:
    if len(text) <= limit:
        return text
    return text[: limit - 3] + "..."


def _format_message_history(recent_messages: list[dict], trigger_message: str) -> str:
    """Format prior conversation messages for Block Kit display.

    Excludes the triggering message (shown separately in its own block) and
    sorts chronologically so the conversation reads top-to-bottom.
    """
    if not recent_messages:
        return "_No prior messages_"

    trigger = (trigger_message or "").strip()

    # Sort chronologically (oldest first) so the thread reads naturally
    sorted_msgs = sorted(recent_messages, key=lambda m: m.get("created_at", ""))

    lines = []
    for msg in sorted_msgs:
        sender = msg.get("sender_type", msg.get("sender", ""))
        body = (msg.get("body") or "").strip()
        if not body:
            continue
        # Skip the triggering message — it gets its own prominent section
        if body == trigger and sender == "guest":
            continue
        timestamp = msg.get("created_at", "")
        label = "GUEST" if sender == "guest" else "HOST"
        lines.append(f"*[{label}]* {body}  _({timestamp})_")

    if not lines:
        return "_No prior messages_"

    return _truncate("\n".join(lines))


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
) -> list[dict]:
    urgency = classification.get("urgency", "MEDIUM")
    category = classification.get("category", "GENERAL")
    summary = classification.get("summary", "")

    urgency_icon = URGENCY_EMOJI.get(urgency, "🟡")
    category_icon = CATEGORY_EMOJI.get(category, "💬")
    category_label = CATEGORY_LABEL.get(category, category)

    if urgency == "HIGH":
        header_text = f"🚨 {urgency_icon} {category_icon} {category_label} 🚨"
    else:
        header_text = f"{urgency_icon} {category_icon} {category_label}"

    guest_badge = "_(Repeat)_" if is_repeat_guest else "_(New)_"
    source_status = _format_source_status(booking_source, reservation_status)

    if category == "POSITIVE":
        action_text = "No response needed unless you'd like to reply."
    else:
        action_text = "Copy draft & send via Hospitable · React ❌ to skip"

    history_text = _format_message_history(recent_messages, guest_message)
    draft_block_text = _truncate(f">>> {draft_response}") if draft_response else ">>> _(no draft)_"

    blocks = [
        {
            "type": "header",
            "text": {"type": "plain_text", "text": header_text, "emoji": True},
        },
        {
            "type": "context",
            "elements": [{"type": "mrkdwn", "text": _truncate(f"_{summary}_")}],
        },
        {
            "type": "section",
            "fields": [
                {"type": "mrkdwn", "text": f"*Property*\n{property_name}"},
                {"type": "mrkdwn", "text": f"*Guest*\n{guest_name} {guest_badge}"},
                {"type": "mrkdwn", "text": f"*Dates*\n{checkin_date} → {checkout_date}"},
                {"type": "mrkdwn", "text": f"*Source / Status*\n{source_status}"},
            ],
        },
        {"type": "divider"},
        # ---- New guest message (the trigger) shown prominently ----
        {
            "type": "context",
            "elements": [{"type": "mrkdwn", "text": "*💬 NEW MESSAGE*"}],
        },
        {
            "type": "section",
            "text": {"type": "mrkdwn", "text": _truncate(f"> {guest_message}")},
        },
        {"type": "divider"},
        # ---- Prior conversation for context ----
        {
            "type": "context",
            "elements": [{"type": "mrkdwn", "text": "*CONVERSATION HISTORY*"}],
        },
        {
            "type": "section",
            "text": {"type": "mrkdwn", "text": history_text},
        },
        {"type": "divider"},
        {
            "type": "context",
            "elements": [{"type": "mrkdwn", "text": "*DRAFT RESPONSE*"}],
        },
        {
            "type": "section",
            "text": {"type": "mrkdwn", "text": draft_block_text},
        },
        {"type": "divider"},
        {
            "type": "context",
            "elements": [
                {
                    "type": "mrkdwn",
                    "text": f"`{reservation_uuid}` · {action_text}",
                },
            ],
        },
    ]

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
) -> dict:
    """Post a Block Kit guest alert to the Slack channel.

    Returns the Slack API response.
    """
    urgency = classification.get("urgency", "MEDIUM")
    category = classification.get("category", "GENERAL")
    summary = classification.get("summary", "")

    urgency_icon = URGENCY_EMOJI.get(urgency, "🟡")
    category_label = CATEGORY_LABEL.get(category, category)

    fallback_text = f"{urgency_icon} {category_label} — {property_name}: {summary}"

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
