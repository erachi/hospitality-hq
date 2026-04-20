"""AWS Lambda handler for Slack Events API — thread reply interactions.

Flow:
  1. Verify Slack request signature (v0 HMAC scheme, 5-min replay window)
  2. Handle url_verification challenge (Slack app setup)
  3. For event_callback events:
     - Filter: only in-thread replies in #guest-alerts, not from a bot
     - Lookup thread_ts → reservation context
     - Classify intent (note/issue/resolved prefix, else question)
     - Dispatch: log action or Q&A
     - Post confirmation/answer in thread

Replies are acknowledged fast (<3s) to stay within Slack's retry window.
If Slack retries (X-Slack-Retry-Num header present), we ack without reprocessing.
"""

import base64
import hashlib
import hmac
import json
import logging
import time

from config import get_slack_signing_secret, SLACK_CHANNEL_ID, TASKS_CHANNEL_ID
from slack_notifier import post_thread_reply
from thread_mapping import ThreadMapping
from thread_logs import (
    ThreadLogs,
    TYPE_NOTE,
    TYPE_ISSUE,
    TYPE_RESOLUTION,
    format_for_claude as format_logs,
)
from knowledge_base_loader import load_kb, format_for_claude as format_kb
from hospitable_client import HospitableClient
from slack_qa import answer as qa_answer
from handler import build_conversation_summary, load_property_context

logger = logging.getLogger()
logger.setLevel(logging.INFO)

# Slack rejects its own timestamps older than 5 minutes for replay protection
_MAX_TIMESTAMP_SKEW_SECONDS = 60 * 5

# Stock responses
_HELP_NUDGE = (
    "Not sure what you need — you can ask me a question about this reservation "
    "or log a note. Try `what's checkout time?` or `note: guest requested extra towels`."
)
_THREAD_NOT_FOUND = (
    "I don't have this thread in my mapping — I can only answer questions "
    "in the thread of an alert I originally posted."
)


def slack_events_handler(event, context):
    """API Gateway Lambda proxy entry point for Slack Events."""
    logger.info("Slack event received")

    # Read raw body (used for both signature verification and parsing)
    raw_body = event.get("body", "")
    if event.get("isBase64Encoded"):
        raw_body = base64.b64decode(raw_body).decode("utf-8")

    headers = _lowercase_headers(event.get("headers", {}))

    # Retry? Ack immediately without doing work.
    if headers.get("x-slack-retry-num"):
        logger.info(
            f"Slack retry #{headers['x-slack-retry-num']} "
            f"reason={headers.get('x-slack-retry-reason')} — acking only"
        )
        return _ok()

    # Signature verification
    timestamp = headers.get("x-slack-request-timestamp", "")
    signature = headers.get("x-slack-signature", "")
    if not _verify_signature(raw_body, timestamp, signature):
        logger.warning("Invalid Slack signature")
        return {"statusCode": 401, "body": "Invalid signature"}

    # Parse payload
    try:
        payload = json.loads(raw_body)
    except (json.JSONDecodeError, TypeError):
        logger.error("Invalid JSON payload")
        return {"statusCode": 400, "body": "Invalid JSON"}

    payload_type = payload.get("type")

    # URL verification — Slack sends this once when you set the Request URL
    if payload_type == "url_verification":
        challenge = payload.get("challenge", "")
        logger.info("Responding to url_verification challenge")
        return {
            "statusCode": 200,
            "headers": {"Content-Type": "text/plain"},
            "body": challenge,
        }

    if payload_type != "event_callback":
        logger.info(f"Ignoring payload type: {payload_type}")
        return _ok()

    try:
        _process_event(payload)
    except Exception as e:
        # Never fail the HTTP response — Slack will retry, and retries are expensive.
        logger.exception(f"Error processing Slack event: {e}")

    return _ok()


def _process_event(payload: dict) -> None:
    inner = payload.get("event", {}) or {}

    if inner.get("type") != "message":
        logger.info(f"Ignoring inner event type: {inner.get('type')}")
        return

    # Filter out edits, deletions, and other subtypes
    subtype = inner.get("subtype")
    if subtype and subtype not in ("file_share",):
        logger.info(f"Ignoring message subtype: {subtype}")
        return

    # Must be a thread reply (thread_ts present AND != ts — a new top-level
    # message in a channel has no thread_ts yet)
    thread_ts = inner.get("thread_ts")
    ts = inner.get("ts")
    if not thread_ts or thread_ts == ts:
        logger.info("Ignoring non-thread message")
        return

    # Dispatch by channel. Slack's Events API has one Request URL per app, so
    # this single handler routes both workflows: guest-alerts threads stay
    # below, task threads go to task_handler.
    channel = inner.get("channel", "")
    if TASKS_CHANNEL_ID and channel == TASKS_CHANNEL_ID:
        # Lazy import to avoid circular references and to keep the guest-alerts
        # cold path lean.
        from task_handler import handle_task_thread_message

        handle_task_thread_message(inner)
        return

    # Guest-alerts channel only
    if SLACK_CHANNEL_ID and channel != SLACK_CHANNEL_ID:
        logger.info(f"Ignoring message in other channel: {channel}")
        return

    # Don't react to our own bot (would cause loops)
    if inner.get("bot_id") or inner.get("app_id"):
        logger.info("Ignoring bot message")
        return

    # Find the reservation context for this thread
    mapping = ThreadMapping().get_mapping(thread_ts)
    if not mapping:
        logger.info(f"No mapping found for thread_ts={thread_ts}")
        post_thread_reply(thread_ts, _THREAD_NOT_FOUND, channel=channel)
        return

    text = (inner.get("text") or "").strip()
    author = inner.get("user", "unknown")

    if not text:
        logger.info("Empty message text — skipping")
        return

    # Dispatch on intent
    intent, payload_text = _classify_intent(text)

    if intent in {TYPE_NOTE, TYPE_ISSUE, TYPE_RESOLUTION}:
        _handle_log_action(
            log_type=intent,
            text=payload_text,
            author=author,
            thread_ts=thread_ts,
            channel=channel,
            mapping=mapping,
        )
    elif intent == "question":
        _handle_question(
            question=text,
            thread_ts=thread_ts,
            channel=channel,
            mapping=mapping,
        )
    else:
        post_thread_reply(thread_ts, _HELP_NUDGE, channel=channel)


def _classify_intent(text: str) -> tuple[str, str]:
    """Return (intent, payload_text).

    intent ∈ {"note", "issue", "resolution", "question"}
    payload_text is the user's content with any prefix stripped.
    """
    lowered = text.lower().strip()

    if lowered.startswith("note:"):
        return TYPE_NOTE, text.split(":", 1)[1].strip()
    if lowered.startswith("issue:"):
        return TYPE_ISSUE, text.split(":", 1)[1].strip()

    # Resolution: "resolved" alone, or "resolved — <trailing note>", etc.
    # We accept any string that starts with "resolved" as a word.
    if lowered == "resolved" or lowered.startswith("resolved ") or lowered.startswith("resolved\t"):
        # Trailing text (e.g. "— called the guest") becomes the log text
        tail = text[len("resolved"):].strip().lstrip("—-: ").strip()
        return TYPE_RESOLUTION, tail

    return "question", text


def _handle_log_action(
    *,
    log_type: str,
    text: str,
    author: str,
    thread_ts: str,
    channel: str,
    mapping: dict,
) -> None:
    logs = ThreadLogs()
    reservation_uuid = mapping.get("reservation_uuid", "")
    log_id = logs.append_log(
        reservation_uuid=reservation_uuid,
        log_type=log_type,
        text=text,
        author=author,
        thread_ts=thread_ts,
    )
    if not log_id:
        post_thread_reply(
            thread_ts,
            "_I couldn't save that — something went wrong on my end._",
            channel=channel,
        )
        return

    confirmation = _format_confirmation(log_type, text, reservation_uuid)
    post_thread_reply(thread_ts, confirmation, channel=channel)


def _format_confirmation(log_type: str, text: str, reservation_uuid: str) -> str:
    short = (reservation_uuid or "")[:8]
    if log_type == TYPE_NOTE:
        return f"✏️ Note saved for reservation `{short}`."
    if log_type == TYPE_ISSUE:
        body = f'"{text}"' if text else "(no description)"
        return f"🔧 Issue logged: {body}\nFlagged for follow-up."
    if log_type == TYPE_RESOLUTION:
        msg = "✅ Marked as resolved."
        if text:
            msg += f"\nNote saved: \"{text}\""
        return msg
    return "_Logged._"


def _handle_question(
    *,
    question: str,
    thread_ts: str,
    channel: str,
    mapping: dict,
) -> None:
    reservation_uuid = mapping.get("reservation_uuid", "")
    property_id = mapping.get("property_id", "")
    property_name = mapping.get("property_name", "")
    guest_name = mapping.get("guest_name", "")

    # Local KB (authoritative)
    local_kb_text = format_kb(load_kb(property_id)) if property_id else ""

    # Hospitable supplemental data (best-effort)
    hospitable_kb_text = ""
    reservation_summary = ""
    conversation_history = ""
    try:
        hospitable = HospitableClient()
        prop_context = load_property_context(hospitable, property_id) if property_id else {}
        hospitable_kb_text = prop_context.get("knowledge_hub", "") or ""

        res = hospitable.get_reservation_detail(reservation_uuid) if reservation_uuid else {}
        reservation_summary = _summarize_reservation(res, fallback_guest=guest_name)

        if reservation_uuid:
            messages = hospitable.get_reservation_messages(reservation_uuid)
            conversation_history = build_conversation_summary(messages)
    except Exception as e:
        logger.warning(f"Hospitable enrichment failed: {e}")

    # Prior thread logs (so the bot can answer "any notes on this?" etc.)
    logs = ThreadLogs().get_logs(reservation_uuid)
    thread_logs_text = format_logs(logs)

    reply = qa_answer(
        question=question,
        property_name=property_name,
        local_kb_context=local_kb_text,
        hospitable_kb_context=hospitable_kb_text,
        reservation_summary=reservation_summary,
        conversation_history=conversation_history,
        thread_logs_context=thread_logs_text,
    )
    post_thread_reply(thread_ts, reply, channel=channel)


def _summarize_reservation(res: dict, fallback_guest: str = "") -> str:
    """Render the interesting fields of a reservation as plain text."""
    if not res:
        return ""
    guest = res.get("guest", {}) or {}
    guest_name = guest.get("full_name") or guest.get("first_name") or fallback_guest or "Guest"
    parts = [
        f"Guest: {guest_name}",
    ]
    for k_in, k_out in [
        ("check_in", "Check-in"),
        ("check_out", "Check-out"),
        ("arrival_date", "Arrival"),
        ("departure_date", "Departure"),
        ("nights", "Nights"),
        ("platform", "Platform"),
        ("status", "Status"),
        ("guests", "Party size"),
    ]:
        v = res.get(k_in)
        if v:
            parts.append(f"{k_out}: {v}")
    return "\n".join(parts)


def _verify_signature(raw_body: str, timestamp: str, signature: str) -> bool:
    """Slack v0 signing scheme. Also enforces a replay window."""
    secret = get_slack_signing_secret()
    if not secret:
        # Not configured — reject everything to avoid unauthenticated access.
        logger.error("Slack signing secret not configured — rejecting request")
        return False
    if not timestamp or not signature:
        return False
    try:
        ts_int = int(timestamp)
    except ValueError:
        return False
    if abs(time.time() - ts_int) > _MAX_TIMESTAMP_SKEW_SECONDS:
        logger.warning(f"Slack timestamp skew too large: {timestamp}")
        return False

    basestring = f"v0:{timestamp}:{raw_body}".encode("utf-8")
    computed = "v0=" + hmac.new(secret.encode("utf-8"), basestring, hashlib.sha256).hexdigest()
    return hmac.compare_digest(computed, signature)


def _lowercase_headers(headers: dict) -> dict:
    """Normalize HTTP header names to lowercase for case-insensitive lookups."""
    return {k.lower(): v for k, v in (headers or {}).items()}


def _ok() -> dict:
    return {"statusCode": 200, "body": ""}
