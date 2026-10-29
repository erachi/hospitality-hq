"""DynamoDB-backed state tracker for processed messages."""

import hashlib
from datetime import datetime, timezone

import boto3

from config import DYNAMODB_TABLE


def _normalize_iso(ts: str) -> str:
    """Normalize an ISO-8601 timestamp to UTC at second precision.

    Returns the input unchanged if it can't be parsed, so a weird-but-stable
    timestamp still produces a stable dedup key.
    """
    if not ts:
        return ""
    try:
        s = ts
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        else:
            dt = dt.astimezone(timezone.utc)
        return dt.strftime("%Y-%m-%dT%H:%M:%SZ")
    except (ValueError, TypeError):
        return ts


def message_dedup_key(reservation_id: str, msg: dict) -> str:
    """Shared dedup key for a guest message.

    Produced identically from the Hospitable `message.created` webhook `data`
    object and from a message dict returned by `GET /reservations/{id}/messages`,
    so the webhook path and the hourly poller mark and detect the same key.

    The webhook payload's top-level `id` is the *webhook event id*, not a
    message id, so we don't use it. The key is derived from fields both
    payloads carry: `created_at` normalized to UTC at second precision, plus
    a truncated sha256 of the stripped body.

    `reservation_id` is accepted for symmetry with the StateTracker API and
    for log context; it is the DynamoDB partition key and isn't folded into
    the returned sort key.
    """
    created_at = _normalize_iso(msg.get("created_at", ""))
    body = (msg.get("body") or "").strip()
    body_hash = hashlib.sha256(body.encode("utf-8")).hexdigest()[:16]
    return f"ts:{created_at}#b:{body_hash}"


class StateTracker:
    """Tracks which messages have been processed to avoid duplicates."""

    def __init__(self):
        self.dynamodb = boto3.resource("dynamodb")
        self.table = self.dynamodb.Table(DYNAMODB_TABLE)

    def is_message_processed(self, reservation_uuid: str, message_id: str) -> bool:
        """Check if a message has already been processed."""
        try:
            response = self.table.get_item(
                Key={
                    "reservation_uuid": reservation_uuid,
                    "message_id": message_id,
                }
            )
            return "Item" in response
        except Exception:
            return False

    def mark_message_processed(
        self,
        reservation_uuid: str,
        message_id: str,
        classification: str,
        urgency: str,
        property_name: str,
    ) -> None:
        """Mark a message as processed with its classification."""
        self.table.put_item(
            Item={
                "reservation_uuid": reservation_uuid,
                "message_id": message_id,
                "classification": classification,
                "urgency": urgency,
                "property_name": property_name,
                "processed_at": datetime.now(timezone.utc).isoformat(),
            }
        )

    def get_reservation_history(self, reservation_uuid: str) -> list[dict]:
        """Get all processed messages for a reservation (for context)."""
        try:
            response = self.table.query(
                KeyConditionExpression="reservation_uuid = :uuid",
                ExpressionAttributeValues={":uuid": reservation_uuid},
            )
            return response.get("Items", [])
        except Exception:
            return []
