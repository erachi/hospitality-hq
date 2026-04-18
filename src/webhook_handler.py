"""AWS Lambda handler for Hospitable webhook events.

Receives real-time message.created webhooks from Hospitable via API Gateway.
Classifies guest messages, drafts responses, and posts to Slack for human approval.

Signature verification is optional — if a webhook signing secret is configured
in SSM, requests are verified with HMAC-SHA256. Otherwise, all requests are accepted.
Hospitable does not currently provide signing secrets via their dashboard.

CRITICAL: Never sends messages to guests. All responses are drafts
for VJ or Maggie to review and send manually.
"""

import hashlib
import hmac
import json
import logging
import base64

from config import get_webhook_secret
from hospitable_client import HospitableClient
from state_tracker import StateTracker
from classifier import classify_message, draft_response
from slack_notifier import post_guest_alert
from handler import load_property_context, build_conversation_summary, _date_only
from knowledge_base_loader import get_property_name

logger = logging.getLogger()
logger.setLevel(logging.INFO)


def webhook_handler(event, context):
    """API Gateway Lambda proxy handler for Hospitable webhooks."""
    logger.info("Webhook received")

    # Step 1: Parse the raw body
    raw_body = event.get("body", "")
    is_base64 = event.get("isBase64Encoded", False)
    if is_base64:
        raw_body = base64.b64decode(raw_body).decode("utf-8")

    # Step 1b: Verify signature if a signing secret is configured
    headers = event.get("headers", {})
    signature = headers.get("signature", headers.get("Signature", ""))
    sig_result = verify_signature(raw_body, signature)

    if sig_result == "rejected":
        logger.warning("Invalid webhook signature")
        return {"statusCode": 401, "body": json.dumps({"error": "Invalid signature"})}

    # Step 2: Parse payload
    try:
        payload = json.loads(raw_body)
    except (json.JSONDecodeError, TypeError):
        logger.error("Invalid JSON payload")
        return {"statusCode": 400, "body": json.dumps({"error": "Invalid JSON"})}

    # Step 3: Filter — only process message.created events
    action = payload.get("action", "")
    if action != "message.created":
        logger.info(f"Ignoring webhook action: {action}")
        return {"statusCode": 200, "body": json.dumps({"status": "ignored", "reason": "not message.created"})}

    data = payload.get("data", {})

    # Step 4: Filter — only process guest messages
    sender_type = data.get("sender_type", "")
    if sender_type != "guest":
        logger.info(f"Ignoring non-guest message (sender_type: {sender_type})")
        return {"statusCode": 200, "body": json.dumps({"status": "ignored", "reason": "not guest message"})}

    # Step 5: Dedup — check if already processed
    webhook_id = payload.get("id", "")
    reservation_id = data.get("reservation_id", "")

    if not webhook_id or not reservation_id:
        logger.warning("Missing webhook id or reservation_id")
        return {"statusCode": 200, "body": json.dumps({"status": "ignored", "reason": "missing ids"})}

    tracker = StateTracker()
    if tracker.is_message_processed(reservation_id, webhook_id):
        logger.info(f"Webhook {webhook_id} already processed, skipping")
        return {"statusCode": 200, "body": json.dumps({"status": "duplicate"})}

    # Step 6: Enrich — fetch reservation details
    try:
        result = process_webhook_message(data, payload, tracker, reservation_id, webhook_id)
        return result
    except Exception as e:
        logger.error(f"Error processing webhook: {e}")
        # Still return 200 to prevent Hospitable retries on transient errors
        # Mark as processed to avoid re-processing on retry
        tracker.mark_message_processed(
            reservation_id, webhook_id, "ERROR", "MEDIUM", "unknown"
        )
        return {"statusCode": 200, "body": json.dumps({"status": "error", "error": str(e)})}


def process_webhook_message(
    data: dict, payload: dict, tracker: StateTracker,
    reservation_id: str, webhook_id: str,
) -> dict:
    """Process a single webhook guest message through the classify/draft/notify pipeline."""
    hospitable = HospitableClient()

    # Fetch reservation details for context
    reservation = hospitable.get_reservation_detail(reservation_id)
    guest = reservation.get("guest", {})
    guest_name = guest.get("full_name", guest.get("first_name", "Guest"))

    # Hospitable's /reservations/{id} response doesn't carry property_id or
    # property_name at the top level. With `include=properties` we get a
    # `properties` array — take the first entry. Then prefer our local KB's
    # canonical name (same logic the polling handler uses) so the Slack alert
    # shows "Villa Bougainvillea" rather than an Airbnb public name.
    properties = reservation.get("properties") or []
    first_property = properties[0] if properties else {}
    property_id = first_property.get("id", "")
    property_name = (
        get_property_name(property_id)
        or first_property.get("name")
        or "Unknown Property"
    )

    # Dates live under check_in/check_out as ISO-8601 timestamps.
    checkin = _date_only(reservation.get("check_in") or reservation.get("arrival_date", ""))
    checkout = _date_only(reservation.get("check_out") or reservation.get("departure_date", ""))
    booking_source = (
        reservation.get("platform", "")
        or reservation.get("source", "")
        or data.get("platform", "")
    )
    reservation_status = reservation.get("status", "")

    # Load property context
    prop_context = load_property_context(hospitable, property_id)

    # Build conversation history
    messages = hospitable.get_reservation_messages(reservation_id)
    conversation_history = build_conversation_summary(messages)

    # Get the message text from the webhook payload
    msg_text = data.get("body") or ""
    if not msg_text.strip():
        tracker.mark_message_processed(
            reservation_id, webhook_id, "EMPTY", "LOW", property_name
        )
        return {"statusCode": 200, "body": json.dumps({"status": "skipped", "reason": "empty message"})}

    # Classify
    classification = classify_message(msg_text, property_name)
    logger.info(
        f"Classified webhook message: {classification['category']} / {classification['urgency']}"
    )

    # Draft response
    draft = draft_response(
        message_text=msg_text,
        property_name=property_name,
        property_description=prop_context.get("description", ""),
        knowledge_hub_context=prop_context.get("knowledge_hub", ""),
        guest_name=guest_name,
        checkin_date=checkin,
        checkout_date=checkout,
        classification=classification,
        conversation_history=conversation_history,
    )

    # Last 5 messages as context in the alert
    recent_messages = messages[-5:] if messages else []

    # Post to Slack
    slack_result = post_guest_alert(
        guest_name=guest_name,
        property_name=property_name,
        checkin_date=checkin,
        checkout_date=checkout,
        guest_message=msg_text,
        classification=classification,
        draft_response=draft,
        reservation_uuid=reservation_id,
        booking_source=booking_source,
        reservation_status=reservation_status,
        is_repeat_guest=False,  # TODO: implement repeat guest detection
        recent_messages=recent_messages,
    )

    if slack_result.get("ok"):
        logger.info(f"Posted Slack alert for webhook {webhook_id}")
    else:
        logger.error(f"Slack post failed: {slack_result.get('error', 'unknown')}")

    # Mark as processed
    tracker.mark_message_processed(
        reservation_id, webhook_id,
        classification["category"], classification["urgency"],
        property_name,
    )

    return {"statusCode": 200, "body": json.dumps({"status": "processed"})}


def verify_signature(raw_body: str, signature: str) -> str:
    """Verify HMAC-SHA256 signature from Hospitable webhook.

    Returns:
        "accepted" — signature valid, or no secret configured (skip verification)
        "rejected" — secret configured but signature doesn't match
    """
    secret = get_webhook_secret()

    if not secret:
        # No signing secret configured — accept all requests.
        # Hospitable doesn't currently provide signing secrets.
        logger.info("No webhook signing secret configured, skipping verification")
        return "accepted"

    if not signature:
        logger.warning("Signing secret configured but no Signature header received")
        return "rejected"

    expected = hmac.new(
        secret.encode("utf-8"),
        raw_body.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()

    if hmac.compare_digest(expected, signature):
        return "accepted"

    return "rejected"
