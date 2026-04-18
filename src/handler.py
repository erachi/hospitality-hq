"""AWS Lambda handler for Hospitality HQ guest monitoring.

Runs every 15 minutes via EventBridge. Polls Hospitable for new guest
messages, classifies issues, drafts responses, and posts to Slack.

CRITICAL: Never sends messages to guests. All responses are drafts
for VJ or Maggie to review and send manually.
"""

import json
import logging
from datetime import datetime, timezone

from config import PROPERTY_UUIDS
from hospitable_client import HospitableClient
from state_tracker import StateTracker
from classifier import classify_message, draft_response
from slack_notifier import post_guest_alert
from knowledge_base_loader import load_kb, format_for_claude, get_property_name

logger = logging.getLogger()
logger.setLevel(logging.INFO)


def lambda_handler(event, context):
    """Main entry point for the Lambda function."""
    logger.info("Hospitality HQ monitor starting")

    hospitable = HospitableClient()
    tracker = StateTracker()

    # Track stats for this run
    stats = {
        "reservations_checked": 0,
        "new_messages_found": 0,
        "alerts_posted": 0,
        "errors": [],
    }

    try:
        # Step 1: Fetch active/upcoming reservations across all properties
        reservations = hospitable.get_active_reservations(PROPERTY_UUIDS)
        stats["reservations_checked"] = len(reservations)
        logger.info(f"Found {len(reservations)} active reservations")

        # Build a property cache for context
        property_cache = {}

        for reservation in reservations:
            try:
                process_reservation(
                    reservation, hospitable, tracker, property_cache, stats
                )
            except Exception as e:
                error_msg = f"Error processing reservation {reservation.get('id', 'unknown')}: {str(e)}"
                logger.error(error_msg)
                stats["errors"].append(error_msg)

    except Exception as e:
        error_msg = f"Fatal error in monitor: {str(e)}"
        logger.error(error_msg)
        stats["errors"].append(error_msg)

    logger.info(
        f"Monitor complete: {stats['reservations_checked']} reservations, "
        f"{stats['new_messages_found']} new messages, "
        f"{stats['alerts_posted']} alerts posted, "
        f"{len(stats['errors'])} errors"
    )

    return {
        "statusCode": 200,
        "body": json.dumps(stats),
    }


def process_reservation(
    reservation: dict,
    hospitable: HospitableClient,
    tracker: StateTracker,
    property_cache: dict,
    stats: dict,
) -> None:
    """Process a single reservation for new guest messages."""
    res_uuid = reservation.get("id", "")
    property_id = reservation.get("property_id", "")

    # Resolve property name: prefer local KB (canonical), then Hospitable's
    # reservation payload, then property cache, then a fallback string.
    # The Hospitable API doesn't reliably return property_name on reservations,
    # so we resolve it from our own KB first.
    property_name = (
        get_property_name(property_id)
        or reservation.get("property_name")
        or "Unknown Property"
    )

    # Extract guest info
    guest = reservation.get("guest", {})
    guest_name = guest.get("full_name", guest.get("first_name", "Guest"))

    # Extract dates
    checkin = reservation.get("checkin", "")
    checkout = reservation.get("checkout", "")

    # Fetch messages for this reservation
    messages = hospitable.get_reservation_messages(res_uuid)

    if not messages:
        return

    # Filter to only guest messages (not host or automated)
    new_guest_messages = []
    for msg in messages:
        msg_id = str(msg.get("id", ""))
        source = msg.get("source", "")
        sender_type = msg.get("sender_type", "")

        # Only process guest messages that haven't been seen
        if sender_type == "guest" and not tracker.is_message_processed(res_uuid, msg_id):
            new_guest_messages.append(msg)

    if not new_guest_messages:
        return

    stats["new_messages_found"] += len(new_guest_messages)
    logger.info(
        f"Found {len(new_guest_messages)} new messages for reservation {res_uuid} ({guest_name} at {property_name})"
    )

    # Load property context (cached per property)
    if property_id not in property_cache:
        property_cache[property_id] = load_property_context(hospitable, property_id)

    prop_context = property_cache[property_id]

    # Build conversation history for context
    conversation_history = build_conversation_summary(messages)

    # Process each new guest message
    for msg in new_guest_messages:
        msg_id = str(msg.get("id", ""))
        # Hospitable can return body: null for non-text messages (e.g. attachments)
        msg_text = msg.get("body") or ""
        msg_time = msg.get("created_at", "")

        if not msg_text.strip():
            # Skip empty messages, mark as processed
            tracker.mark_message_processed(res_uuid, msg_id, "EMPTY", "LOW", property_name)
            continue

        # Classify the message
        classification = classify_message(msg_text, property_name)
        logger.info(
            f"Classified message {msg_id}: {classification['category']} / {classification['urgency']}"
        )

        # Draft a response
        draft = draft_response(
            message_text=msg_text,
            property_name=property_name,
            property_description=prop_context.get("description", ""),
            knowledge_hub_context=prop_context.get("knowledge_hub", ""),
            local_kb_context=prop_context.get("local_kb", ""),
            guest_name=guest_name,
            checkin_date=checkin,
            checkout_date=checkout,
            classification=classification,
            conversation_history=conversation_history,
        )

        # Post to Slack
        slack_result = post_guest_alert(
            guest_name=guest_name,
            property_name=property_name,
            checkin_date=checkin,
            checkout_date=checkout,
            guest_message=msg_text,
            classification=classification,
            draft_response=draft,
            reservation_uuid=res_uuid,
        )

        if slack_result.get("ok"):
            stats["alerts_posted"] += 1
            logger.info(f"Posted Slack alert for message {msg_id}")
        else:
            logger.error(f"Slack post failed: {slack_result.get('error', 'unknown')}")

        # Mark as processed regardless (to avoid re-posting on failure)
        tracker.mark_message_processed(
            res_uuid, msg_id, classification["category"], classification["urgency"], property_name
        )


def load_property_context(hospitable: HospitableClient, property_uuid: str) -> dict:
    """Load property context for response drafting.

    Merges three sources, in decreasing order of authority:
      1. Our curated local KB (src/knowledge_base/<property>.yaml) — authoritative
      2. Hospitable's Knowledge Hub — supplemental, fills gaps
      3. Hospitable's property description — lowest priority
    """
    context = {"description": "", "knowledge_hub": "", "local_kb": ""}

    # Local KB (authoritative)
    try:
        local_kb = load_kb(property_uuid)
        context["local_kb"] = format_for_claude(local_kb)
    except Exception as e:
        logger.warning(f"Could not load local KB for {property_uuid}: {e}")

    # Hospitable property description
    try:
        prop = hospitable.get_property(property_uuid)
        context["description"] = prop.get("description", "")
    except Exception as e:
        logger.warning(f"Could not load property details for {property_uuid}: {e}")

    # Hospitable Knowledge Hub (supplemental)
    try:
        kb = hospitable.get_property_knowledge_hub(property_uuid)
        kb_parts = []
        topics = kb.get("topics", [])
        if isinstance(topics, list):
            for topic in topics:
                topic_name = topic.get("name", "General")
                items = topic.get("items", [])
                if isinstance(items, list):
                    for item in items:
                        content = item.get("content", "")
                        if content:
                            kb_parts.append(f"[{topic_name}] {content}")
        context["knowledge_hub"] = "\n".join(kb_parts)
    except Exception as e:
        logger.warning(f"Could not load Knowledge Hub for {property_uuid}: {e}")

    return context


def build_conversation_summary(messages: list[dict]) -> str:
    """Build a brief summary of the recent conversation thread."""
    recent = messages[-10:]  # Last 10 messages for context
    lines = []
    for msg in recent:
        sender = msg.get("sender_type", "unknown")
        body = msg.get("body") or ""
        if body:
            label = "GUEST" if sender == "guest" else "HOST"
            # Truncate long messages
            if len(body) > 300:
                body = body[:300] + "..."
            lines.append(f"[{label}] {body}")
    return "\n".join(lines)
