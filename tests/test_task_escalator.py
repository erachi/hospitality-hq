"""Tests for the daily overdue-task escalator.

We use actual clock (not mocked) with far-past / far-future dates so the
tests don't drift as the real calendar moves forward. This tests the
routing and DM/channel decisions, not the date arithmetic itself (that
belongs in test_task_models).
"""

import json
from datetime import datetime, timezone
from unittest.mock import patch

from task_models import Task
from task_store import TaskStore


def _today() -> str:
    return datetime.now(timezone.utc).date().isoformat()


def test_escalator_dms_each_user_with_overdue(tasks_bucket):
    from task_escalator import lambda_handler

    store = TaskStore()

    # 20+ days overdue for vj — triggers channel escalation
    very_overdue_vj = Task.new(
        title="very overdue vj",
        description="",
        property_id="prop-palm",
        assignee_id="vj",
        created_by_id="vj",
        due_date="2020-01-01",
    )
    # Recently overdue for maggie (well within channel-escalation window)
    overdue_maggie = Task.new(
        title="slightly overdue maggie",
        description="",
        property_id="prop-villa",
        assignee_id="maggie",
        created_by_id="vj",
        due_date="2020-01-02",
    )
    # Due today for maggie
    due_today_maggie = Task.new(
        title="due today maggie",
        description="",
        property_id="prop-palm",
        assignee_id="maggie",
        created_by_id="vj",
        due_date=_today(),
    )
    # Future for vj (should be ignored)
    future_vj = Task.new(
        title="future vj",
        description="",
        property_id="prop-palm",
        assignee_id="vj",
        created_by_id="vj",
        due_date="2099-12-31",
    )
    for t in (very_overdue_vj, overdue_maggie, due_today_maggie, future_vj):
        store.put(t)

    with patch("task_escalator.dm_user") as dm_mock, patch(
        "task_escalator.post_message"
    ) as post_mock:
        dm_mock.return_value = {"ok": True}
        post_mock.return_value = {"ok": True}
        result = lambda_handler({}, None)

    stats = json.loads(result["body"])
    # vj and maggie both have overdue → one DM each
    assert stats["dms_sent"] == 2
    assert stats["overdue_total"] == 2
    assert stats["due_today_total"] == 1
    # Both overdue tasks are from 2020 → both are badly-overdue and get channel posted
    assert stats["badly_overdue_total"] == 2
    post_mock.assert_called_once()


def test_escalator_with_no_overdue_sends_nothing(tasks_bucket):
    from task_escalator import lambda_handler

    store = TaskStore()
    t = Task.new(
        title="future only",
        description="",
        property_id="prop-palm",
        assignee_id="vj",
        created_by_id="vj",
        due_date="2099-01-01",
    )
    store.put(t)

    with patch("task_escalator.dm_user") as dm_mock, patch(
        "task_escalator.post_message"
    ) as post_mock:
        dm_mock.return_value = {"ok": True}
        post_mock.return_value = {"ok": True}
        result = lambda_handler({}, None)

    stats = json.loads(result["body"])
    assert stats["dms_sent"] == 0
    assert stats["channel_posts"] == 0


def test_escalator_dms_user_with_only_due_today(tasks_bucket):
    """A user with no overdue but a due-today task should still get a DM."""
    from task_escalator import lambda_handler

    store = TaskStore()
    t = Task.new(
        title="due today",
        description="",
        property_id="prop-palm",
        assignee_id="maggie",
        created_by_id="vj",
        due_date=_today(),
    )
    store.put(t)

    with patch("task_escalator.dm_user") as dm_mock, patch(
        "task_escalator.post_message"
    ) as post_mock:
        dm_mock.return_value = {"ok": True}
        post_mock.return_value = {"ok": True}
        result = lambda_handler({}, None)

    stats = json.loads(result["body"])
    assert stats["dms_sent"] == 1
    assert stats["overdue_total"] == 0
