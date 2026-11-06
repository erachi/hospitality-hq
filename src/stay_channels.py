"""DynamoDB wrapper for the reservation ↔ stay-channel mapping.

For each reservation that has a provisioned Slack channel we keep a
small pointer row:

    reservation_uuid (PK) → channel_id, channel_name, property/guest
    metadata, check-in/out, status (active|archived), archived_at,
    last_mirrored_hospitable_ts

A GSI on `channel_id` lets the Slack event router resolve an incoming
channel event back to a reservation in one query.

The journal itself lives in S3 (`stay_store.py`). This table is just
the index and a few operational attributes we want to query on without
touching S3.
"""

import boto3
import logging
from datetime import datetime, timezone
from typing import Optional

from boto3.dynamodb.conditions import Key
from botocore.exceptions import ClientError

from config import STAY_CHANNELS_TABLE
from stay_models import STATUS_ACTIVE, STATUS_ARCHIVED

logger = logging.getLogger(__name__)

# GSI defined in template.yaml. The name is referenced in a couple of
# places so pin it here.
GSI_BY_CHANNEL = "by_channel"


class StayChannels:
    """Reads and writes the reservation ↔ stay-channel mapping."""

    def __init__(self):
        self.dynamodb = boto3.resource("dynamodb")
        self.table = self.dynamodb.Table(STAY_CHANNELS_TABLE)

    # ─── Writes ──────────────────────────────────────────────────────

    def put(
        self,
        *,
        reservation_uuid: str,
        channel_id: str,
        channel_name: str,
        property_id: str,
        property_name: str,
        guest_name: str,
        check_in: str,
        check_out: str,
        journal_s3_key: str,
        last_mirrored_hospitable_ts: Optional[str] = None,
    ) -> None:
        """Record a newly provisioned stay channel.

        Idempotent: callers should check get_by_reservation first.
        Writing over an existing row resets archived_at to NULL and
        status to active — useful on /stay-restore.
        """
        if not reservation_uuid or not channel_id:
            logger.warning(
                "Skipping StayChannels.put: res=%r chan=%r",
                reservation_uuid,
                channel_id,
            )
            return
        item = {
            "reservation_uuid": reservation_uuid,
            "channel_id": channel_id,
            "channel_name": channel_name or "",
            "property_id": property_id or "",
            "property_name": property_name or "",
            "guest_name": guest_name or "",
            "check_in": check_in or "",
            "check_out": check_out or "",
            "status": STATUS_ACTIVE,
            "created_at": _now_iso(),
            "journal_s3_key": journal_s3_key or "",
        }
        if last_mirrored_hospitable_ts:
            item["last_mirrored_hospitable_ts"] = last_mirrored_hospitable_ts
        try:
            self.table.put_item(Item=item)
        except ClientError as e:
            logger.error("Failed to put stay-channel row for %s: %s", reservation_uuid, e)

    def update_last_mirrored(
        self, reservation_uuid: str, hospitable_ts: str
    ) -> None:
        """Advance the high-water mark of mirrored Hospitable messages."""
        if not reservation_uuid or not hospitable_ts:
            return
        try:
            self.table.update_item(
                Key={"reservation_uuid": reservation_uuid},
                UpdateExpression="SET last_mirrored_hospitable_ts = :t",
                ExpressionAttributeValues={":t": hospitable_ts},
            )
        except ClientError as e:
            logger.error(
                "Failed to update last_mirrored for %s: %s", reservation_uuid, e
            )

    def mark_archived(self, reservation_uuid: str) -> None:
        """Flip status to archived and stamp archived_at."""
        if not reservation_uuid:
            return
        try:
            self.table.update_item(
                Key={"reservation_uuid": reservation_uuid},
                UpdateExpression="SET #s = :s, archived_at = :a",
                ExpressionAttributeNames={"#s": "status"},
                ExpressionAttributeValues={
                    ":s": STATUS_ARCHIVED,
                    ":a": _now_iso(),
                },
            )
        except ClientError as e:
            logger.error(
                "Failed to mark archived for %s: %s", reservation_uuid, e
            )

    def mark_active(self, reservation_uuid: str) -> None:
        """Flip status back to active and clear archived_at. For restore."""
        if not reservation_uuid:
            return
        try:
            self.table.update_item(
                Key={"reservation_uuid": reservation_uuid},
                UpdateExpression="SET #s = :s REMOVE archived_at",
                ExpressionAttributeNames={"#s": "status"},
                ExpressionAttributeValues={":s": STATUS_ACTIVE},
            )
        except ClientError as e:
            logger.error(
                "Failed to mark active for %s: %s", reservation_uuid, e
            )

    # ─── Reads ───────────────────────────────────────────────────────

    def get_by_reservation(self, reservation_uuid: str) -> Optional[dict]:
        """Fetch the channel row for a reservation, or None."""
        if not reservation_uuid:
            return None
        try:
            resp = self.table.get_item(Key={"reservation_uuid": reservation_uuid})
            return resp.get("Item")
        except ClientError as e:
            logger.error(
                "Failed to read stay-channel row for %s: %s",
                reservation_uuid,
                e,
            )
            return None

    def get_by_channel(self, channel_id: str) -> Optional[dict]:
        """Resolve a Slack channel_id back to a reservation row via GSI."""
        if not channel_id:
            return None
        try:
            resp = self.table.query(
                IndexName=GSI_BY_CHANNEL,
                KeyConditionExpression=Key("channel_id").eq(channel_id),
                Limit=1,
            )
            items = resp.get("Items") or []
            return items[0] if items else None
        except ClientError as e:
            logger.error(
                "Failed to query by channel %s: %s", channel_id, e
            )
            return None

    def list_active(self) -> list[dict]:
        """Scan for every active stay channel. Low volume; scan is fine."""
        return self._scan_by_status(STATUS_ACTIVE)

    def list_due_for_archival(self, before_date: str) -> list[dict]:
        """Active stays whose check_out is strictly before `before_date`.

        `before_date` is an ISO date (YYYY-MM-DD). Caller controls the
        grace period — e.g. pass today - 7 days for a 7-day grace.
        """
        rows = self.list_active()
        return [r for r in rows if r.get("check_out") and r["check_out"] < before_date]

    def _scan_by_status(self, status: str) -> list[dict]:
        rows = []
        kwargs = {
            "FilterExpression": "#s = :s",
            "ExpressionAttributeNames": {"#s": "status"},
            "ExpressionAttributeValues": {":s": status},
        }
        while True:
            resp = self.table.scan(**kwargs)
            rows.extend(resp.get("Items") or [])
            last = resp.get("LastEvaluatedKey")
            if not last:
                break
            kwargs["ExclusiveStartKey"] = last
        return rows


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
