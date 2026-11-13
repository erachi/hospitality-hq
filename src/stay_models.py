"""Data model for the per-stay Slack channel workflow.

A StayJournal is the durable record of everything that happens for one
reservation: the mirrored Hospitable transcript, every top-level Slack
message the humans or the agent post in the stay channel, and the
system events (provisioning, send-draft, archive).

Stored as one JSON object per reservation in S3 (versioned). We treat
S3 as the source of truth because free Slack hides messages older than
~90 days from both the UI and conversations.history — so the Slack
channel is a view layer and the journal is the record.
"""

import uuid
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from typing import Any

# ─── Status ──────────────────────────────────────────────────────────────

STATUS_ACTIVE = "active"
STATUS_ARCHIVED = "archived"

ALL_STATUSES = (STATUS_ACTIVE, STATUS_ARCHIVED)


# ─── Entry source ────────────────────────────────────────────────────────
# Where this entry came from. Determines how the agent renders the entry
# when assembling multi-turn context.

SOURCE_HOSPITABLE_GUEST = "hospitable-guest"
SOURCE_HOSPITABLE_HOST = "hospitable-host"
SOURCE_SLACK_USER = "slack-user"
SOURCE_SLACK_AGENT = "slack-agent"
SOURCE_SYSTEM = "system"

ALL_SOURCES = (
    SOURCE_HOSPITABLE_GUEST,
    SOURCE_HOSPITABLE_HOST,
    SOURCE_SLACK_USER,
    SOURCE_SLACK_AGENT,
    SOURCE_SYSTEM,
)


@dataclass
class StayEntry:
    """A single entry in the stay journal.

    metadata is a free-form dict for carrying bits that matter to one
    source but not another: Hospitable message_id, Slack ts, draft-card
    state, etc. Keep it JSON-serializable.
    """

    id: str
    ts: str  # ISO-8601 UTC
    source: str
    author: str
    body: str
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "StayEntry":
        return cls(
            id=data["id"],
            ts=data["ts"],
            source=data["source"],
            author=data.get("author", ""),
            body=data.get("body", ""),
            metadata=dict(data.get("metadata") or {}),
        )

    @classmethod
    def new(
        cls,
        *,
        source: str,
        author: str,
        body: str,
        ts: str | None = None,
        metadata: dict | None = None,
    ) -> "StayEntry":
        return cls(
            id=str(uuid.uuid4()),
            ts=ts or _now_iso(),
            source=source,
            author=author,
            body=body,
            metadata=dict(metadata or {}),
        )


@dataclass
class StayJournal:
    """The full record for one reservation.

    Stored at s3://<journal_bucket>/<prefix>/<reservation_uuid>.json.

    `reservation_snapshot` is the full Hospitable reservation object as
    observed at provisioning time. It gives the agent reliable context
    even if the Hospitable API is temporarily unreachable.
    """

    reservation_uuid: str
    property_id: str
    property_name: str
    guest_name: str
    check_in: str  # ISO date, YYYY-MM-DD
    check_out: str  # ISO date, YYYY-MM-DD
    created_at: str
    updated_at: str
    status: str = STATUS_ACTIVE
    channel_id: str | None = None
    channel_name: str | None = None
    archived_at: str | None = None
    last_mirrored_hospitable_ts: str | None = None
    reservation_snapshot: dict = field(default_factory=dict)
    entries: list[StayEntry] = field(default_factory=list)

    def to_dict(self) -> dict:
        data = asdict(self)
        data["entries"] = [e.to_dict() for e in self.entries]
        return data

    @classmethod
    def from_dict(cls, data: dict) -> "StayJournal":
        return cls(
            reservation_uuid=data["reservation_uuid"],
            property_id=data.get("property_id", ""),
            property_name=data.get("property_name", ""),
            guest_name=data.get("guest_name", ""),
            check_in=data.get("check_in", ""),
            check_out=data.get("check_out", ""),
            created_at=data["created_at"],
            updated_at=data.get("updated_at", data["created_at"]),
            status=data.get("status", STATUS_ACTIVE),
            channel_id=data.get("channel_id"),
            channel_name=data.get("channel_name"),
            archived_at=data.get("archived_at"),
            last_mirrored_hospitable_ts=data.get("last_mirrored_hospitable_ts"),
            reservation_snapshot=dict(data.get("reservation_snapshot") or {}),
            entries=[StayEntry.from_dict(e) for e in (data.get("entries") or [])],
        )

    @classmethod
    def new(
        cls,
        *,
        reservation_uuid: str,
        property_id: str,
        property_name: str,
        guest_name: str,
        check_in: str,
        check_out: str,
        reservation_snapshot: dict | None = None,
    ) -> "StayJournal":
        now = _now_iso()
        return cls(
            reservation_uuid=reservation_uuid,
            property_id=property_id,
            property_name=property_name,
            guest_name=guest_name,
            check_in=check_in,
            check_out=check_out,
            created_at=now,
            updated_at=now,
            status=STATUS_ACTIVE,
            reservation_snapshot=dict(reservation_snapshot or {}),
        )

    def touch(self) -> None:
        """Stamp updated_at. Call on every mutation."""
        self.updated_at = _now_iso()

    def append(self, entry: StayEntry) -> None:
        """Append an entry and touch updated_at."""
        self.entries.append(entry)
        self.touch()

    def is_archived(self) -> bool:
        return self.status == STATUS_ARCHIVED

    def mark_archived(self) -> None:
        self.status = STATUS_ARCHIVED
        self.archived_at = _now_iso()
        self.touch()


def _now_iso() -> str:
    """Current UTC time as an ISO-8601 string with trailing Z."""
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")
