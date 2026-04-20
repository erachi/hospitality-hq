"""Tests for task_models."""

from task_models import (
    Task,
    Comment,
    STATUS_OPEN,
    STATUS_DONE,
    PRIORITY_NORMAL,
    PRIORITY_HIGH,
    PROPERTY_BUSINESS,
    OPEN_STATUSES,
)


def test_new_task_has_uuid_and_timestamps():
    t = Task.new(
        title="Fix disposal",
        description="Guest reported it",
        property_id="prop-palm",
        assignee_id="vj",
        created_by_id="maggie",
    )
    assert t.id
    assert t.status == STATUS_OPEN
    assert t.priority == PRIORITY_NORMAL
    assert t.created_at == t.updated_at
    assert t.is_open()
    assert t.comments == []


def test_task_roundtrip_to_dict():
    t = Task.new(
        title="Restock",
        description="",
        property_id="prop-villa",
        assignee_id="maggie",
        created_by_id="vj",
        priority=PRIORITY_HIGH,
        due_date="2026-05-01",
        tags=["consumables", "pre-arrival"],
    )
    t.comments.append(Comment.new(user_id="vj", body="checked pantry"))

    data = t.to_dict()
    round = Task.from_dict(data)

    assert round.id == t.id
    assert round.title == t.title
    assert round.priority == PRIORITY_HIGH
    assert round.due_date == "2026-05-01"
    assert round.tags == ["consumables", "pre-arrival"]
    assert len(round.comments) == 1
    assert round.comments[0].body == "checked pantry"


def test_touch_updates_updated_at():
    t = Task.new(
        title="t",
        description="",
        property_id=PROPERTY_BUSINESS,
        assignee_id="vj",
        created_by_id="vj",
    )
    original = t.updated_at
    t.title = "t2"
    t.touch()
    assert t.updated_at >= original


def test_is_overdue_only_for_open_tasks():
    t = Task.new(
        title="overdue",
        description="",
        property_id=PROPERTY_BUSINESS,
        assignee_id="vj",
        created_by_id="vj",
        due_date="2025-01-01",
    )
    assert t.is_overdue(today="2026-04-20")

    t.status = STATUS_DONE
    assert not t.is_overdue(today="2026-04-20")


def test_is_overdue_false_without_due_date():
    t = Task.new(
        title="no deadline",
        description="",
        property_id=PROPERTY_BUSINESS,
        assignee_id="vj",
        created_by_id="vj",
    )
    assert not t.is_overdue(today="2099-01-01")


def test_open_statuses_covers_non_terminal_states():
    assert STATUS_OPEN in OPEN_STATUSES
    assert STATUS_DONE not in OPEN_STATUSES
