"""Daily archival of per-stay Slack channels.

Rule: a stay channel whose `check_out + 7 days < today` AND whose
status is still `active` gets archived. The S3 journal is finalized
with a system entry, a farewell note is posted in the channel, and
`conversations.archive` is called; the DynamoDB row flips to
`archived`.

Archival is purely a UX cleanup — Slack free hides messages older
than ~90 days anyway, so restoring an archived channel means
replaying from the S3 journal (that path is deferred to a later PR).

Handler shape mirrors `task_escalator.lambda_handler`: EventBridge
fires once a day, the function scans the StayChannels table, archives
each candidate, and returns a stats dict.
"""

import json
import logging
import os
from datetime import date, datetime, timedelta, timezone
from typing import Optional

import requests

from config import (
    STAY_CHANNELS_ENABLED,
    STAY_JOURNAL_PREFIX,
    get_slack_bot_token,
)
from slack_notifier import StayChannelSlackClient
from stay_channels import StayChannels
from stay_models import SOURCE_SYSTEM, StayEntry
from stay_store import StayStore

logger = logging.getLogger()
logger.setLevel(logging.INFO)

# Grace period between check-out and archival. 7 days lets lingering
# review / tax questions land in the still-active channel.
_ARCHIVE_GRACE_DAYS = int(os.environ.get("STAY_ARCHIVE_GRACE_DAYS", "7"))


def lambda_handler(event, context):
    """Entry point for the daily EventBridge trigger."""
    if not STAY_CHANNELS_ENABLED:
        logger.info("STAY_CHANNELS_ENABLED=false — archiver is a no-op")
        return {"statusCode": 200, "body": json.dumps({"skipped": "flag_off"})}

    stats = {"candidates": 0, "archived": 0, "errors": []}
    stay_channels = StayChannels()
    stay_store = StayStore()
    slack_client = StayChannelSlackClient()

    # before_date is today minus the grace window — rows whose
    # check_out is earlier than that are due.
    cutoff = (date.today() - timedelta(days=_ARCHIVE_GRACE_DAYS)).isoformat()
    try:
        rows = stay_channels.list_due_for_archival(before_date=cutoff)
    except Exception as e:
        logger.exception("archiver: list_due_for_archival failed: %s", e)
        return {"statusCode": 500, "body": json.dumps({"error": str(e)})}

    stats["candidates"] = len(rows)
    logger.info(f"archiver: {len(rows)} channel(s) due for archival (cutoff {cutoff})")

    for row in rows:
        res_uuid = row.get("reservation_uuid", "")
        channel_id = row.get("channel_id", "")
        if not res_uuid or not channel_id:
            logger.warning("archiver: skipping malformed row: %s", row)
            continue
        try:
            _archive_one(
                row=row,
                stay_channels=stay_channels,
                stay_store=stay_store,
                slack_client=slack_client,
            )
            stats["archived"] += 1
        except Exception as e:
            msg = f"archiver: {res_uuid} failed: {e}"
            logger.exception(msg)
            stats["errors"].append(msg)

    return {"statusCode": 200, "body": json.dumps(stats)}


def _archive_one(
    *,
    row: dict,
    stay_channels: StayChannels,
    stay_store: StayStore,
    slack_client: StayChannelSlackClient,
) -> None:
    """Journal first, farewell post, archive the channel, flip the row."""
    res_uuid = row["reservation_uuid"]
    channel_id = row["channel_id"]
    journal_key = row.get("journal_s3_key") or (
        f"{STAY_JOURNAL_PREFIX.rstrip('/')}/{res_uuid}.json"
    )

    # 1. Journal entry — durable record that this stay was archived.
    try:
        stay_store.append_entry(
            res_uuid,
            StayEntry.new(
                source=SOURCE_SYSTEM,
                author="archiver",
                body="Stay channel archived by scheduled job",
                metadata={
                    "action": "archived",
                    "check_out": row.get("check_out", ""),
                    "grace_days": _ARCHIVE_GRACE_DAYS,
                },
            ),
        )
    except Exception as e:
        # Loud log but keep going — missing the journal entry is bad
        # but the channel still needs to close.
        logger.warning("archiver: journal append failed for %s: %s", res_uuid, e)

    # 2. Farewell post. Includes the journal S3 location so a
    #    retrospective reader knows where the full record lives even
    #    after Slack has aged out this channel's history.
    try:
        slack_client.post_message(
            channel_id,
            text=(
                f":package: Archiving this channel. Full record in S3: "
                f"`s3://.../{journal_key}`"
            ),
        )
    except Exception as e:
        logger.warning("archiver: farewell post failed for %s: %s", res_uuid, e)

    # 3. Archive on Slack. StayChannelSlackClient doesn't wrap this
    #    one call so we do it inline; small enough to not deserve its
    #    own helper.
    archive_resp = _slack_archive(channel_id)
    err = archive_resp.get("error", "")
    if not archive_resp.get("ok"):
        if err == "already_archived":
            logger.info(
                "archiver: Slack reports %s already archived — "
                "flipping row anyway",
                channel_id,
            )
        else:
            raise RuntimeError(f"conversations.archive failed: {err}")

    # 4. Flip the DynamoDB row — last so a failure above leaves us in
    #    a state the next run can retry.
    stay_channels.mark_archived(res_uuid)
    logger.info("archiver: archived stay channel %s (%s)", channel_id, res_uuid)


def _slack_archive(channel_id: str) -> dict:
    """`conversations.archive` — single Slack call, raw response."""
    r = requests.post(
        f"{StayChannelSlackClient._SLACK_BASE}/conversations.archive",
        headers={
            "Authorization": f"Bearer {get_slack_bot_token()}",
            "Content-Type": "application/json; charset=utf-8",
        },
        json={"channel": channel_id},
    )
    return r.json()


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
