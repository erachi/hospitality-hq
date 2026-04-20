"""DynamoDB wrapper for thread-scoped notes, issues, and resolutions.

Every note/issue/resolution a host logs in an alert thread is stored here,
keyed by reservation. These logs feed into two places:
  - Future draft_response calls (so the bot "remembers" prior context)
  - Future Q&A replies (so "any issues on this res?" returns real answers)
"""

import boto3
import logging
import time
from datetime import datetime, timezone
from typing import List, Optional

from config import THREAD_LOGS_TABLE

logger = logging.getLogger(__name__)


# Valid log entry types — the handler maps user prefixes to these.
TYPE_NOTE = "note"
TYPE_ISSUE = "issue"
TYPE_RESOLUTION = "resolution"
VALID_TYPES = {TYPE_NOTE, TYPE_ISSUE, TYPE_RESOLUTION}


class ThreadLogs:
    """Read/write thread-scoped logs tied to a reservation."""

    def __init__(self):
        self.dynamodb = boto3.resource("dynamodb")
        self.table = self.dynamodb.Table(THREAD_LOGS_TABLE)

    def append_log(
        self,
        reservation_uuid: str,
        log_type: str,
        text: str,
        author: str,
        thread_ts: str,
    ) -> Optional[str]:
        """Append a note/issue/resolution. Returns the generated log_id or None on failure."""
        if log_type not in VALID_TYPES:
            logger.warning(f"Ignoring invalid log type: {log_type}")
            return None
        if not reservation_uuid:
            logger.warning("Missing reservation_uuid, skipping log")
            return None

        now = datetime.now(timezone.utc)
        # log_id is ISO timestamp + millisecond suffix for uniqueness + sortability
        log_id = f"{now.isoformat()}#{int(time.time() * 1000) % 1000:03d}"

        try:
            self.table.put_item(
                Item={
                    "reservation_uuid": reservation_uuid,
                    "log_id": log_id,
                    "type": log_type,
                    "text": text or "",
                    "author": author or "unknown",
                    "thread_ts": thread_ts or "",
                    "created_at": now.isoformat(),
                }
            )
            return log_id
        except Exception as e:
            logger.error(f"Failed to append log for {reservation_uuid}: {e}")
            return None

    def get_logs(self, reservation_uuid: str) -> List[dict]:
        """Return all logs for a reservation, oldest first."""
        if not reservation_uuid:
            return []
        try:
            response = self.table.query(
                KeyConditionExpression="reservation_uuid = :uuid",
                ExpressionAttributeValues={":uuid": reservation_uuid},
            )
            items = response.get("Items", [])
            # Sort by log_id (which is timestamp-prefixed) oldest first
            items.sort(key=lambda x: x.get("log_id", ""))
            return items
        except Exception as e:
            logger.error(f"Failed to query logs for {reservation_uuid}: {e}")
            return []


# Icon prefixes for human-readable rendering
_TYPE_ICONS = {
    TYPE_NOTE: "📝",
    TYPE_ISSUE: "🔧",
    TYPE_RESOLUTION: "✅",
}


def format_for_claude(logs: List[dict]) -> str:
    """Render a list of logs as a text block for inclusion in a Claude prompt.

    Returns empty string when there are no logs, so the caller can
    unconditionally include it without extra boilerplate.
    """
    if not logs:
        return ""

    lines = ["═══ INTERNAL NOTES / ISSUES / RESOLUTIONS (from past thread replies) ═══"]
    for log in logs:
        type_label = log.get("type", "note").upper()
        icon = _TYPE_ICONS.get(log.get("type", ""), "•")
        text = (log.get("text") or "").strip()
        author = log.get("author") or "unknown"
        when = log.get("created_at") or ""
        # Short date, not full ISO
        short_when = when.split("T")[0] if when else ""
        lines.append(f"{icon} [{type_label}] {text}  ({author}, {short_when})".rstrip())
    return "\n".join(lines)
