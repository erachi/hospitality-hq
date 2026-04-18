"""CloudWatch alarm → Slack notifier.

Subscribed to the hospitality-hq-alarms SNS topic. For each alarm event,
parses the CloudWatch alarm payload and posts a formatted message to the
dedicated ops Slack channel using the same bot token used for guest alerts.
"""

import json
import logging
import os

import requests

from config import get_slack_bot_token

logger = logging.getLogger()
logger.setLevel(logging.INFO)

OPS_SLACK_CHANNEL_ID = os.environ.get("OPS_SLACK_CHANNEL_ID", "")

# Map CloudWatch alarm state → emoji + color
STATE_STYLE = {
    "ALARM": {"emoji": "🚨", "label": "FIRING"},
    "OK": {"emoji": "✅", "label": "RESOLVED"},
    "INSUFFICIENT_DATA": {"emoji": "⚠️", "label": "NO DATA"},
}


def alarm_handler(event, context):
    """SNS-triggered handler that forwards CloudWatch alarms to Slack."""
    records = event.get("Records", [])
    logger.info(f"Processing {len(records)} SNS records")

    for record in records:
        try:
            sns_message = record.get("Sns", {}).get("Message", "")
            if not sns_message:
                logger.warning("Empty SNS Message")
                continue
            payload = json.loads(sns_message)
            post_to_slack(payload)
        except json.JSONDecodeError as e:
            logger.error(f"Could not parse SNS message as JSON: {e}")
        except Exception as e:
            logger.error(f"Error forwarding alarm to Slack: {e}")

    return {"statusCode": 200}


def post_to_slack(payload: dict) -> dict:
    """Format a CloudWatch alarm payload and post to the ops Slack channel."""
    alarm_name = payload.get("AlarmName", "Unknown alarm")
    new_state = payload.get("NewStateValue", "UNKNOWN")
    reason = payload.get("NewStateReason", "")
    description = payload.get("AlarmDescription", "")
    region = payload.get("Region", "")
    timestamp = payload.get("StateChangeTime", "")

    style = STATE_STYLE.get(new_state, {"emoji": "❓", "label": new_state})

    lines = [
        f"{style['emoji']} *{style['label']}:* `{alarm_name}`",
    ]
    if description:
        lines.append(f"_{description}_")
    lines.append("")
    lines.append(f"*State change:* {reason}")
    if region:
        lines.append(f"*Region:* {region}")
    if timestamp:
        lines.append(f"*At:* {timestamp}")

    text = "\n".join(lines)

    response = requests.post(
        "https://slack.com/api/chat.postMessage",
        headers={
            "Authorization": f"Bearer {get_slack_bot_token()}",
            "Content-Type": "application/json",
        },
        json={
            "channel": OPS_SLACK_CHANNEL_ID,
            "text": text,
            "unfurl_links": False,
            "unfurl_media": False,
        },
        timeout=10,
    )
    body = response.json()
    if not body.get("ok"):
        logger.error(f"Slack post failed: {body.get('error', 'unknown')}")
    return body
