"""Data model for the task-management workflow.

A Task is stored in S3 as one JSON object per task. Comments live inline
on the task (not in a separate store) to keep the read path simple:
one GET returns the full task with its history.

Fields align with the design doc — see TASKS_DESIGN.md if you ever write it.
"""

import uuid
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from typing import Optional


# Status values. A task's lifecycle walks through these left-to-right,
# with "blocked" as an optional detour and "cancelled" as an exit.
STATUS_OPEN = "open"
STATUS_IN_PROGRESS = "in_progress"
STATUS_BLOCKED = "blocked"
STATUS_DONE = "done"
STATUS_CANCELLED = "cancelled"

OPEN_STATUSES = (STATUS_OPEN, STATUS_IN_PROGRESS, STATUS_BLOCKED)
ALL_STATUSES = (
    STATUS_OPEN,
    STATUS_IN_PROGRESS,
    STATUS_BLOCKED,
    STATUS_DONE,
    STATUS_CANCELLED,
)

STATUS_LABEL = {
    STATUS_OPEN: "Open",
    STATUS_IN_PROGRESS: "In progress",
    STATUS_BLOCKED: "Blocked",
    STATUS_DONE: "Done",
    STATUS_CANCELLED: "Cancelled",
}

# Priority values
PRIORITY_LOW = "low"
PRIORITY_NORMAL = "normal"
PRIORITY_HIGH = "high"
PRIORITY_URGENT = "urgent"

ALL_PRIORITIES = (PRIORITY_LOW, PRIORITY_NORMAL, PRIORITY_HIGH, PRIORITY_URGENT)

PRIORITY_LABEL = {
    PRIORITY_LOW: "Low",
    PRIORITY_NORMAL: "Normal",
    PRIORITY_HIGH: "High",
    PRIORITY_URGENT: "Urgent",
}

PRIORITY_EMOJI = {
    PRIORITY_LOW: "⚪",
    PRIORITY_NORMAL: "🟢",
    PRIORITY_HIGH: "🟠",
    PRIORITY_URGENT: "🔴",
}

# Sentinel property_id for tasks that apply to the business as a whole
# (e.g. Scottsdale STR compliance) rather than a specific property.
# Using a string sentinel keeps list-by-property logic uniform.
PROPERTY_BUSINESS = "business"


@dataclass
class Comment:
    """A comment on a task. Comments are stored inline on the Task."""

    id: str
    user_id: str
    body: str
    created_at: str
    slack_message_ts: Optional[str] = None

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "Comment":
        return cls(
            id=data["id"],
            user_id=data["user_id"],
            body=data["body"],
            created_at=data["created_at"],
            slack_message_ts=data.get("slack_message_ts"),
        )

    @classmethod
    def new(cls, user_id: str, body: str, slack_message_ts: Optional[str] = None) -> "Comment":
        return cls(
            id=str(uuid.uuid4()),
            user_id=user_id,
            body=body,
            created_at=_now_iso(),
            slack_message_ts=slack_message_ts,
        )


@dataclass
class Task:
    """A unit of work assigned to VJ or Maggie.

    Stored in S3 at tasks/{id}.json as its full state. The Slack card is a
    view over the task — slack_channel_id + slack_message_ts let us update
    it in place when the task changes.
    """

    id: str
    title: str
    description: str
    status: str
    priority: str
    property_id: str  # a property UUID, or PROPERTY_BUSINESS sentinel
    assignee_id: str
    created_by_id: str
    created_at: str
    updated_at: str
    due_date: Optional[str] = None  # ISO date, YYYY-MM-DD
    completed_at: Optional[str] = None
    slack_channel_id: Optional[str] = None
    slack_message_ts: Optional[str] = None
    tags: list[str] = field(default_factory=list)
    comments: list[Comment] = field(default_factory=list)

    def to_dict(self) -> dict:
        data = asdict(self)
        # asdict already handles nested dataclasses, but be explicit.
        data["comments"] = [c.to_dict() for c in self.comments]
        return data

    @classmethod
    def from_dict(cls, data: dict) -> "Task":
        return cls(
            id=data["id"],
            title=data["title"],
            description=data.get("description", ""),
            status=data.get("status", STATUS_OPEN),
            priority=data.get("priority", PRIORITY_NORMAL),
            property_id=data.get("property_id", PROPERTY_BUSINESS),
            assignee_id=data["assignee_id"],
            created_by_id=data["created_by_id"],
            created_at=data["created_at"],
            updated_at=data.get("updated_at", data["created_at"]),
            due_date=data.get("due_date"),
            completed_at=data.get("completed_at"),
            slack_channel_id=data.get("slack_channel_id"),
            slack_message_ts=data.get("slack_message_ts"),
            tags=list(data.get("tags", []) or []),
            comments=[Comment.from_dict(c) for c in (data.get("comments") or [])],
        )

    @classmethod
    def new(
        cls,
        *,
        title: str,
        description: str,
        property_id: str,
        assignee_id: str,
        created_by_id: str,
        priority: str = PRIORITY_NORMAL,
        due_date: Optional[str] = None,
        tags: Optional[list[str]] = None,
    ) -> "Task":
        now = _now_iso()
        return cls(
            id=str(uuid.uuid4()),
            title=title.strip(),
            description=(description or "").strip(),
            status=STATUS_OPEN,
            priority=priority,
            property_id=property_id,
            assignee_id=assignee_id,
            created_by_id=created_by_id,
            created_at=now,
            updated_at=now,
            due_date=due_date,
            tags=list(tags or []),
        )

    def touch(self) -> None:
        """Stamp updated_at. Call on every mutation."""
        self.updated_at = _now_iso()

    def is_open(self) -> bool:
        return self.status in OPEN_STATUSES

    def is_overdue(self, today: Optional[str] = None) -> bool:
        """True if open and the due_date has passed.

        today is an ISO date (YYYY-MM-DD); defaults to today in UTC. The cron
        runs daily at 9am Phoenix — the tiny timezone offset is not worth
        modeling here since due_date is date-only anyway.
        """
        if not self.is_open() or not self.due_date:
            return False
        today = today or datetime.now(timezone.utc).date().isoformat()
        return self.due_date < today


def _now_iso() -> str:
    """Current UTC time as an ISO-8601 string with trailing Z."""
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
