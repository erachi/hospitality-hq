"""DynamoDB wrapper for the Slack thread → reservation mapping.

When the bot posts an alert, we record the thread_ts (Slack message timestamp
that starts the thread) with the reservation context. When users later reply
in that thread, the thread handler uses this mapping to know which reservation
they're talking about — no IDs needed in the message.
"""

import boto3
import logging
import time
from datetime import datetime, timezone
from typing import Optional

from config import THREAD_MAPPING_TABLE

logger = logging.getLogger(__name__)

# Mappings expire after 90 days to keep the table small. Threads older than
# that are unlikely to receive meaningful replies.
_TTL_SECONDS = 90 * 24 * 60 * 60


class ThreadMapping:
    """Reads and writes the thread_ts → reservation mapping."""

    def __init__(self):
        self.dynamodb = boto3.resource("dynamodb")
        self.table = self.dynamodb.Table(THREAD_MAPPING_TABLE)

    def put_mapping(
        self,
        thread_ts: str,
        reservation_uuid: str,
        property_id: str,
        property_name: str,
        guest_name: str,
    ) -> None:
        """Record a new thread → reservation mapping."""
        if not thread_ts or not reservation_uuid:
            logger.warning(
                f"Skipping mapping put: thread_ts={thread_ts!r}, "
                f"reservation_uuid={reservation_uuid!r}"
            )
            return
        try:
            self.table.put_item(
                Item={
                    "thread_ts": str(thread_ts),
                    "reservation_uuid": reservation_uuid,
                    "property_id": property_id or "",
                    "property_name": property_name or "",
                    "guest_name": guest_name or "",
                    "created_at": datetime.now(timezone.utc).isoformat(),
                    "ttl": int(time.time()) + _TTL_SECONDS,
                }
            )
        except Exception as e:
            # Don't block alert posting on mapping failures — log and move on.
            logger.error(f"Failed to save thread mapping for {thread_ts}: {e}")

    def get_mapping(self, thread_ts: str) -> Optional[dict]:
        """Look up reservation context for a Slack thread. Returns None if unknown."""
        if not thread_ts:
            return None
        try:
            response = self.table.get_item(Key={"thread_ts": str(thread_ts)})
            return response.get("Item")
        except Exception as e:
            logger.error(f"Failed to read thread mapping for {thread_ts}: {e}")
            return None
