"""Slack notification formatting and posting."""

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


def post_guest_alert(
    guest_name: str,
    property_name: str,
    checkin_date: str,
    checkout_date: str,
    guest_message: str,
    classification: dict,
    draft_response: str,
    reservation_uuid: str,
) -> dict:
    """Post a formatted guest alert to the Slack channel.

    Returns the Slack API response.
    """
    urgency = classification.get("urgency", "MEDIUM")
    category = classification.get("category", "GENERAL")
    summary = classification.get("summary", "")

    urgency_icon = URGENCY_EMOJI.get(urgency, "🟡")
    category_icon = CATEGORY_EMOJI.get(category, "💬")
    category_label = CATEGORY_LABEL.get(category, category)

    # Build the header
    header = f"{urgency_icon} {category_icon} *{category_label}* — {property_name}"
    if urgency == "HIGH":
        header = f"🚨 {header} 🚨"

    # Build the message
    message_parts = [
        header,
        "",
        f"*Guest:* {guest_name}",
        f"*Dates:* {checkin_date} → {checkout_date}",
        f"*Summary:* {summary}",
        "",
        "━━━━━━━━━━━━━━━━━━━━━━━━",
        "",
        f"*Guest said:*",
        f"> {guest_message}",
        "",
        "━━━━━━━━━━━━━━━━━━━━━━━━",
        "",
        f"*Draft response:*",
        f"```{draft_response}```",
        "",
        f"_Reservation: `{reservation_uuid}`_",
    ]

    # Add action instructions based on urgency
    if category == "POSITIVE":
        message_parts.append("\n_No response needed unless you'd like to reply._")
    else:
        message_parts.append(
            "\n⚡ *To send this response:* Copy the draft above and send via Hospitable."
            "\n✏️ *To edit:* Modify the draft first, then send."
            "\n🚫 *To skip:* React with ❌ to mark as handled."
        )

    message = "\n".join(message_parts)

    # Post to Slack
    response = requests.post(
        "https://slack.com/api/chat.postMessage",
        headers={
            "Authorization": f"Bearer {get_slack_bot_token()}",
            "Content-Type": "application/json",
        },
        json={
            "channel": SLACK_CHANNEL_ID,
            "text": message,
            "unfurl_links": False,
            "unfurl_media": False,
        },
    )

    return response.json()
