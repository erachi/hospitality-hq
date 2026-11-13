"""Per-stay Slack channel provisioner + Hospitable-message mirror.

Two public entry points:

- `provision_if_missing(...)` — idempotent. For a given reservation it
  seeds the S3 stay journal, creates a private Slack channel, invites
  the humans, posts and pins an intro, backfills the recent Hospitable
  transcript into the channel, and commits a StayChannels row. Called
  once per reservation per poll from `MonitorFunction`.

- `mirror_message(...)` — for each new Hospitable guest or host
  message, append to the journal and post it into the stay channel as
  a top-level message. Called from the webhook handler and from the
  hourly safety-net poller.

Design invariants:

- Journal writes happen BEFORE Slack writes, always. The S3 journal is
  the source of truth; Slack is a view that can age out on free plans.
- Nothing calls these with `STAY_CHANNELS_ENABLED=False` — both entry
  points short-circuit on the flag so a caller cannot accidentally
  leak provisioning behavior.
- All Slack / S3 / DynamoDB failures log loudly and either raise (if
  the state is unrecoverable) or return `None` (if the next poll can
  retry safely). No partial Slack rollback: a bare channel left behind
  by a mid-flight crash is accepted for v1 — reconcile later via a
  cleanup script.
"""

import logging
import re
from datetime import UTC, datetime, timedelta

from config import (
    STAY_CHANNEL_INVITE_USER_IDS,
    STAY_CHANNEL_NAME_PREFIX,
    STAY_CHANNELS_ENABLED,
    STAY_JOURNAL_PREFIX,
    STAY_SLACK_BACKFILL_DAYS,
)
from stay_channels import StayChannels
from stay_models import (
    SOURCE_HOSPITABLE_GUEST,
    SOURCE_HOSPITABLE_HOST,
    SOURCE_SYSTEM,
    StayEntry,
    StayJournal,
)
from stay_store import StayStore

logger = logging.getLogger(__name__)

# Slack channel name limits — hard cap is 80 chars, lowercase, no spaces.
_MAX_CHANNEL_NAME_LEN = 80

# How many `-2`, `-3`, … collision suffixes to try before giving up.
_MAX_COLLISION_RETRIES = 5

# Hospitable message sender_type → journal source.
_SOURCE_BY_SENDER = {
    "guest": SOURCE_HOSPITABLE_GUEST,
    "host": SOURCE_HOSPITABLE_HOST,
}


# ─── Slug ──────────────────────────────────────────────────────────────


def _slugify(text: str) -> str:
    """Lowercase ASCII slug: keep [a-z0-9-], collapse repeats, trim.

    Non-ASCII characters collapse to empty — the caller supplies a
    fallback.
    """
    if not text:
        return ""
    s = text.lower()
    # Replace anything that isn't lowercase alphanumeric with a hyphen.
    s = re.sub(r"[^a-z0-9]+", "-", s)
    s = re.sub(r"-{2,}", "-", s).strip("-")
    return s


def build_stay_channel_name(
    property_slug: str,
    guest_full_name: str,
    checkin_date: str,
    suffix: int = 1,
) -> str:
    """Build a Slack channel name for a stay.

    Format: `{prefix}{property_slug}-{guest_lastname}-{YYYYMMDD}`,
    with a `-{suffix}` suffix appended when suffix > 1 to recover from
    a `name_taken` collision. Always ≤ 80 chars, lowercase, slug-safe.
    """
    prop = _slugify(property_slug) or "stay"

    # Guest "last name" = last whitespace token, slugified. Empty →
    # "guest". The real-world value is used only inside a slug so the
    # fallback doesn't risk leaking PII.
    last_token = (guest_full_name or "").strip().split()[-1:] or [""]
    last = _slugify(last_token[0]) or "guest"

    # YYYYMMDD. Tolerate either "2026-11-20" or "2026-11-20T00:00:00Z".
    date_part = (checkin_date or "").split("T", 1)[0].replace("-", "")
    if not re.fullmatch(r"\d{8}", date_part):
        date_part = "nodate"

    base = f"{STAY_CHANNEL_NAME_PREFIX}{prop}-{last}-{date_part}"
    if suffix > 1:
        base = f"{base}-{suffix}"

    # Slugify the full assembled name (prefix might carry hyphens and
    # the suffix is pure digits — both safe), then hard-truncate.
    base = _slugify(base)
    if len(base) > _MAX_CHANNEL_NAME_LEN:
        base = base[:_MAX_CHANNEL_NAME_LEN].rstrip("-")
    return base


# ─── Journal helpers ───────────────────────────────────────────────────


def _source_from_sender(sender_type: str) -> str:
    """Map Hospitable sender_type to a journal source."""
    return _SOURCE_BY_SENDER.get((sender_type or "").lower(), SOURCE_HOSPITABLE_HOST)


def _entry_from_hospitable_message(message: dict) -> StayEntry:
    """Build a journal entry from a Hospitable message object."""
    return StayEntry.new(
        source=_source_from_sender(message.get("sender_type")),
        author=message.get("sender_type") or "unknown",
        body=message.get("body") or "",
        ts=message.get("created_at") or None,
        metadata={
            "hospitable_message_id": str(message.get("id") or ""),
            "hospitable_created_at": message.get("created_at") or "",
            "hospitable_source": message.get("source") or "",
        },
    )


def _message_ts(message: dict) -> str:
    """Hospitable's authoritative message timestamp."""
    return message.get("created_at") or ""


# ─── Provisioner ───────────────────────────────────────────────────────


def provision_if_missing(
    *,
    reservation_uuid: str,
    property_id: str,
    property_slug: str,
    property_name: str,
    guest_name: str,
    check_in: str,
    check_out: str,
    reservation_snapshot: dict,
    hospitable_client,
    slack_client,
    stay_channels: StayChannels | None = None,
    stay_store: StayStore | None = None,
) -> dict | None:
    """Idempotently provision a per-stay Slack channel.

    Returns the `StayChannels` row dict on success (existing or new);
    returns `None` on feature-flag off or unrecoverable failure.

    Called from `MonitorFunction` once per in-window reservation.
    `slack_client` is a `StayChannelSlackClient` (see slack_notifier).
    """
    if not STAY_CHANNELS_ENABLED:
        return None
    if not reservation_uuid:
        logger.warning("provision_if_missing called with empty reservation_uuid")
        return None

    stay_channels = stay_channels or StayChannels()
    stay_store = stay_store or StayStore()

    existing = stay_channels.get_by_reservation(reservation_uuid)
    if existing:
        return existing

    # 1. Journal first — durable. Fetch the Hospitable transcript, then
    #    snapshot the reservation metadata, then append each existing
    #    message as a journal entry. If the Hospitable fetch fails we
    #    bail with a loud log: no channel without the record.
    try:
        messages = hospitable_client.get_reservation_messages(reservation_uuid) or []
    except Exception as e:
        logger.error(
            "provisioner: Hospitable message fetch failed for %s: %s",
            reservation_uuid,
            e,
        )
        return None

    journal = StayJournal.new(
        reservation_uuid=reservation_uuid,
        property_id=property_id or "",
        property_name=property_name or "",
        guest_name=guest_name or "",
        check_in=check_in or "",
        check_out=check_out or "",
        reservation_snapshot=reservation_snapshot or {},
    )
    journal.append(
        StayEntry.new(
            source=SOURCE_SYSTEM,
            author="system",
            body="Channel provisioning started",
        )
    )
    last_message_ts = ""
    for msg in messages:
        journal.append(_entry_from_hospitable_message(msg))
        ts = _message_ts(msg)
        if ts and (not last_message_ts or ts > last_message_ts):
            last_message_ts = ts

    try:
        stay_store.put(journal)
    except Exception as e:
        logger.error("provisioner: journal write failed for %s: %s", reservation_uuid, e)
        return None

    # 2. Create the Slack channel. Collisions are rare (property + guest
    #    + check-in date) but if someone double-books the same guest on
    #    the same date we try -2, -3, … up to 5.
    base_name = build_stay_channel_name(property_slug, guest_name, check_in)
    channel_id: str | None = None
    channel_name: str | None = None
    for attempt in range(1, _MAX_COLLISION_RETRIES + 1):
        candidate = (
            build_stay_channel_name(property_slug, guest_name, check_in, suffix=attempt)
            if attempt > 1
            else base_name
        )
        try:
            result = slack_client.create_private_channel(candidate)
        except Exception as e:
            logger.error(
                "provisioner: Slack create exploded for %s (%s): %s",
                reservation_uuid,
                candidate,
                e,
            )
            return None
        if result.get("ok"):
            channel = result.get("channel") or {}
            channel_id = channel.get("id")
            channel_name = channel.get("name") or candidate
            break
        err = result.get("error", "")
        if err == "name_taken":
            logger.info("provisioner: %s taken, trying -%d", candidate, attempt + 1)
            continue
        if err == "missing_scope":
            logger.error(
                "provisioner: Slack app missing scope for %s "
                "(need groups:write). Aborting; journal is kept.",
                reservation_uuid,
            )
            return None
        logger.error("provisioner: Slack create failed for %s: %s", reservation_uuid, err)
        return None

    if not channel_id:
        logger.error(
            "provisioner: ran out of collision retries for %s (base=%s)",
            reservation_uuid,
            base_name,
        )
        return None

    # 3. Invite humans. Best-effort — ignore errors per invitee.
    if STAY_CHANNEL_INVITE_USER_IDS:
        try:
            slack_client.invite_users(channel_id, STAY_CHANNEL_INVITE_USER_IDS)
        except Exception as e:
            logger.warning(
                "provisioner: invite failed for %s: %s (continuing)",
                reservation_uuid,
                e,
            )

    # 4. Intro block + pin. Non-fatal on either side.
    journal_s3_key = f"{STAY_JOURNAL_PREFIX.rstrip('/')}/{reservation_uuid}.json"
    intro_blocks = _build_intro_blocks(
        property_name=property_name,
        guest_name=guest_name,
        check_in=check_in,
        check_out=check_out,
        reservation_snapshot=reservation_snapshot,
        journal_s3_key=journal_s3_key,
    )
    try:
        intro_result = slack_client.post_message(
            channel_id,
            text=f"Stay: {guest_name} at {property_name} ({check_in} → {check_out})",
            blocks=intro_blocks,
        )
        intro_ts = intro_result.get("ts") or (intro_result.get("message") or {}).get("ts")
        if intro_ts:
            try:
                slack_client.pin_message(channel_id, intro_ts)
            except Exception as e:
                logger.warning(
                    "provisioner: pin failed for %s: %s (continuing)",
                    reservation_uuid,
                    e,
                )
    except Exception as e:
        logger.warning(
            "provisioner: intro post failed for %s: %s (continuing)",
            reservation_uuid,
            e,
        )

    # 5. Backfill recent transcript into Slack. Full record is in the
    #    journal; Slack gets only the last STAY_SLACK_BACKFILL_DAYS so
    #    free-plan retention doesn't matter.
    cutoff = datetime.now(UTC) - timedelta(days=STAY_SLACK_BACKFILL_DAYS)
    recent = []
    older_count = 0
    for msg in messages:
        parsed = _parse_iso(_message_ts(msg))
        if parsed is None:
            recent.append(msg)
            continue
        if parsed >= cutoff:
            recent.append(msg)
        else:
            older_count += 1
    if older_count:
        try:
            slack_client.post_message(
                channel_id,
                text=(
                    f":books: {older_count} older messages omitted — "
                    "full transcript in S3 journal (link above)."
                ),
            )
        except Exception as e:
            logger.warning("provisioner: older-omit note failed: %s", e)
    for msg in recent:
        text = _format_hospitable_message(msg, guest_name)
        try:
            slack_client.post_message(channel_id, text=text)
        except Exception as e:
            logger.warning(
                "provisioner: backfill post failed for msg %s: %s",
                msg.get("id"),
                e,
            )

    # 6. Commit the mapping — last step so a partial failure leaves no
    #    "done" marker and the next poll safely retries from step 1.
    try:
        stay_channels.put(
            reservation_uuid=reservation_uuid,
            channel_id=channel_id,
            channel_name=channel_name or "",
            property_id=property_id or "",
            property_name=property_name or "",
            guest_name=guest_name or "",
            check_in=check_in or "",
            check_out=check_out or "",
            journal_s3_key=journal_s3_key,
            last_mirrored_hospitable_ts=last_message_ts or None,
        )
    except Exception as e:
        logger.error(
            "provisioner: StayChannels.put failed for %s (channel %s already "
            "created — orphan risk): %s",
            reservation_uuid,
            channel_id,
            e,
        )
        return None

    logger.info(
        "provisioner: created stay channel %s for reservation %s",
        channel_name,
        reservation_uuid,
    )
    return stay_channels.get_by_reservation(reservation_uuid)


# ─── Mirror ─────────────────────────────────────────────────────────────


def mirror_message(
    *,
    reservation_uuid: str,
    message: dict,
    slack_client,
    stay_channels: StayChannels | None = None,
    stay_store: StayStore | None = None,
    guest_name: str = "",
) -> str | None:
    """Append a Hospitable message to the stay journal + stay channel.

    Returns the Slack message ts on success, None otherwise. Idempotent:
    a message whose `created_at` is `<=` the StayChannels row's
    `last_mirrored_hospitable_ts` is a no-op.

    Non-fatal: callers (webhook + monitor) wrap this in try/except so a
    mirror failure never kills the primary alert flow.
    """
    if not STAY_CHANNELS_ENABLED:
        return None
    if not reservation_uuid or not message:
        return None

    stay_channels = stay_channels or StayChannels()
    stay_store = stay_store or StayStore()

    row = stay_channels.get_by_reservation(reservation_uuid)
    if not row:
        return None

    message_ts = _message_ts(message)
    last_ts = row.get("last_mirrored_hospitable_ts") or ""
    if message_ts and last_ts and message_ts <= last_ts:
        # Already mirrored by the other entry point. No-op.
        return None

    # Journal first — durable.
    try:
        stay_store.append_entry(reservation_uuid, _entry_from_hospitable_message(message))
    except Exception as e:
        logger.error("mirror: journal append failed for %s: %s", reservation_uuid, e)
        return None

    # Then Slack.
    channel_id = row.get("channel_id")
    if not channel_id:
        logger.warning("mirror: no channel_id on row for %s", reservation_uuid)
        return None
    text = _format_hospitable_message(message, guest_name or row.get("guest_name", ""))
    try:
        result = slack_client.post_message(channel_id, text=text)
    except Exception as e:
        logger.warning("mirror: Slack post failed for %s: %s", reservation_uuid, e)
        return None

    posted_ts = result.get("ts") or (result.get("message") or {}).get("ts")

    # Advance high-water mark.
    if message_ts:
        try:
            stay_channels.update_last_mirrored(reservation_uuid, message_ts)
        except Exception as e:
            logger.warning(
                "mirror: update_last_mirrored failed for %s: %s",
                reservation_uuid,
                e,
            )

    # Auto-draft hand-off. Only for guest-source messages — host mirrors
    # don't need a reply. Guarded inside the handler on STAY_AUTO_DRAFT_ENABLED
    # and the in-stay window. Imported lazily to avoid a circular import.
    if (message.get("sender_type") or "").lower() == "guest" and posted_ts:
        try:
            from stay_agent import handle_mirrored_guest_message

            handle_mirrored_guest_message(
                reservation_uuid=reservation_uuid,
                stay_row=row,
                message=message,
            )
        except Exception as e:
            # A draft failure must never fail the mirror — the guest
            # message itself is already journaled and posted.
            logger.warning(
                "mirror: auto-draft hand-off failed for %s: %s",
                reservation_uuid,
                e,
            )

    return posted_ts


# ─── Formatting ────────────────────────────────────────────────────────


def _format_hospitable_message(message: dict, guest_name: str) -> str:
    """Attributed Slack-mrkdwn line for one Hospitable message."""
    sender = (message.get("sender_type") or "").lower()
    body = (message.get("body") or "").strip() or "_(no message body)_"
    ts = _message_ts(message)
    stamp = f" _[{ts}]_" if ts else ""
    if sender == "guest":
        label = f"*Guest · {guest_name}:*" if guest_name else "*Guest:*"
    else:
        label = "*Us (via Hospitable):*"
    return f"{label}{stamp}\n> {body}"


def _build_intro_blocks(
    *,
    property_name: str,
    guest_name: str,
    check_in: str,
    check_out: str,
    reservation_snapshot: dict,
    journal_s3_key: str,
) -> list[dict]:
    """Block Kit for the pinned intro message.

    Sections degrade gracefully: missing fields are simply omitted
    rather than aborting the whole intro.
    """
    rs = reservation_snapshot or {}
    guest = rs.get("guest") or {}

    header_text = f":house: {property_name}" if property_name else ":house: Stay"

    # Context line: dates + booking channel + confirmation code
    context_bits: list[str] = []
    if check_in and check_out:
        context_bits.append(f"{check_in} → {check_out}")
    source = rs.get("platform") or rs.get("source") or ""
    if source:
        context_bits.append(source.title())
    conf = rs.get("confirmation_code") or rs.get("reservation_code") or ""
    if conf:
        context_bits.append(f"`{conf}`")

    guest_bits: list[str] = []
    if guest_name:
        guest_bits.append(f"*Guest:* {guest_name}")
    party = (rs.get("guests") or {}).get("total") or rs.get("guests_total")
    if party:
        guest_bits.append(f"*Party:* {party}")
    phone = guest.get("phone") or guest.get("phone_number")
    if phone:
        guest_bits.append(f"*Phone:* {phone}")

    hospitable_url = (
        f"https://my.hospitable.com/inbox/conversation/{rs.get('id', '')}" if rs.get("id") else ""
    )

    blocks: list[dict] = [
        {"type": "header", "text": {"type": "plain_text", "text": header_text}},
    ]
    if context_bits:
        blocks.append(
            {
                "type": "context",
                "elements": [{"type": "mrkdwn", "text": " · ".join(context_bits)}],
            }
        )
    if guest_bits:
        blocks.append(
            {
                "type": "section",
                "text": {"type": "mrkdwn", "text": "\n".join(guest_bits)},
            }
        )

    footer_bits: list[str] = []
    if journal_s3_key:
        footer_bits.append(f"S3 journal: `s3://.../{journal_s3_key}`")
    if hospitable_url:
        footer_bits.append(f"<{hospitable_url}|Open in Hospitable>")
    if footer_bits:
        blocks.append({"type": "divider"})
        blocks.append(
            {
                "type": "context",
                "elements": [{"type": "mrkdwn", "text": " · ".join(footer_bits)}],
            }
        )
    return blocks


def _parse_iso(ts: str) -> datetime | None:
    """Parse an ISO-8601 timestamp into a UTC-aware datetime, or None."""
    if not ts:
        return None
    try:
        s = ts
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=UTC)
        return dt.astimezone(UTC)
    except (ValueError, TypeError):
        return None
