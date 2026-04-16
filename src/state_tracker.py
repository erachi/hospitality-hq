"""DynamoDB-backed state tracker for processed messages."""

import boto3
from datetime import datetime, timezone
from config import DYNAMODB_TABLE


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
