"""Tests for the task Slack handler. We stub Slack HTTP calls and exercise
the routing + persistence paths."""

import base64
import hashlib
import hmac
import json
import time
from unittest.mock import patch
from urllib.parse import urlencode

import pytest


SIGNING_SECRET = "test-slack-signing-secret"


@pytest.fixture(autouse=True)
def configured_slack_secret(monkeypatch):
    """Patch the signing-secret fetch to return a known test value."""
    from config import get_slack_signing_secret

    monkeypatch.setattr(
        "task_handler.get_slack_signing_secret",
        lambda: SIGNING_SECRET,
    )


def _sign(raw_body: str, timestamp: str, secret: str = SIGNING_SECRET) -> str:
    base = f"v0:{timestamp}:{raw_body}".encode("utf-8")
    digest = hmac.new(secret.encode("utf-8"), base, hashlib.sha256).hexdigest()
    return f"v0={digest}"


def _event(raw_body: str, content_type: str = "application/x-www-form-urlencoded", path: str = "/slack/tasks/commands"):
    ts = str(int(time.time()))
    sig = _sign(raw_body, ts)
    return {
        "body": raw_body,
        "isBase64Encoded": False,
        "rawPath": path,
        "headers": {
            "content-type": content_type,
            "x-slack-request-timestamp": ts,
            "x-slack-signature": sig,
        },
    }


def test_rejects_invalid_signature(tasks_bucket):
    from task_handler import slack_tasks_handler

    event = {
        "body": "command=/task",
        "headers": {
            "content-type": "application/x-www-form-urlencoded",
            "x-slack-request-timestamp": str(int(time.time())),
            "x-slack-signature": "v0=wrong",
        },
    }
    result = slack_tasks_handler(event, None)
    assert result["statusCode"] == 401


def test_slash_command_help(tasks_bucket):
    from task_handler import slack_tasks_handler

    body = urlencode({"command": "/task", "text": "help", "user_id": "UVJ", "trigger_id": "T"})
    result = slack_tasks_handler(_event(body), None)

    assert result["statusCode"] == 200
    payload = json.loads(result["body"])
    assert payload["response_type"] == "ephemeral"
    assert "/task" in payload["text"]


def test_slash_command_mine_returns_assignee_tasks(tasks_bucket):
    from task_handler import slack_tasks_handler
    from task_models import Task
    from task_store import TaskStore

    store = TaskStore()
    mine = Task.new(
        title="my task",
        description="",
        property_id="prop-palm",
        assignee_id="vj",
        created_by_id="maggie",
    )
    hers = Task.new(
        title="her task",
        description="",
        property_id="prop-palm",
        assignee_id="maggie",
        created_by_id="vj",
    )
    store.put(mine)
    store.put(hers)

    body = urlencode({"command": "/task", "text": "mine", "user_id": "UVJ", "trigger_id": "T"})
    result = slack_tasks_handler(_event(body), None)

    assert result["statusCode"] == 200
    payload = json.loads(result["body"])
    flat = json.dumps(payload)
    assert "my task" in flat
    assert "her task" not in flat


def test_slash_command_list_by_property_slug(tasks_bucket):
    from task_handler import slack_tasks_handler
    from task_models import Task
    from task_store import TaskStore

    store = TaskStore()
    palm_t = Task.new(
        title="palm task",
        description="",
        property_id="prop-palm",
        assignee_id="vj",
        created_by_id="vj",
    )
    villa_t = Task.new(
        title="villa task",
        description="",
        property_id="prop-villa",
        assignee_id="vj",
        created_by_id="vj",
    )
    store.put(palm_t)
    store.put(villa_t)

    body = urlencode({"command": "/task", "text": "list palm", "user_id": "UVJ", "trigger_id": "T"})
    result = slack_tasks_handler(_event(body), None)

    payload = json.loads(result["body"])
    flat = json.dumps(payload)
    assert "palm task" in flat
    assert "villa task" not in flat


def test_slash_command_unknown_property(tasks_bucket):
    from task_handler import slack_tasks_handler

    body = urlencode({"command": "/task", "text": "list moreland", "user_id": "UVJ", "trigger_id": "T"})
    result = slack_tasks_handler(_event(body), None)

    payload = json.loads(result["body"])
    assert "don't know" in payload["text"]


def test_view_submission_creates_task_and_posts_card(tasks_bucket):
    from task_handler import slack_tasks_handler
    from task_store import TaskStore
    from task_slack_ui import CREATE_MODAL_CALLBACK_ID

    submission = {
        "type": "view_submission",
        "user": {"id": "UVJ"},
        "view": {
            "callback_id": CREATE_MODAL_CALLBACK_ID,
            "state": {
                "values": {
                    "title_block": {"title": {"value": "Fix garbage disposal"}},
                    "description_block": {"description": {"value": "Guest reported it"}},
                    "property_block": {
                        "property": {"selected_option": {"value": "prop-palm"}}
                    },
                    "assignee_block": {
                        "assignee": {"selected_option": {"value": "maggie"}}
                    },
                    "priority_block": {
                        "priority": {"selected_option": {"value": "high"}}
                    },
                    "due_date_block": {"due_date": {"selected_date": "2026-05-01"}},
                }
            },
        },
    }

    body = urlencode({"payload": json.dumps(submission)})

    with patch("task_handler.post_message") as post_mock, patch(
        "task_handler.dm_user"
    ) as dm_mock:
        post_mock.return_value = {"ok": True, "ts": "1700000000.000123", "channel": "C_TEST_TASKS"}
        dm_mock.return_value = {"ok": True}

        result = slack_tasks_handler(
            _event(body, path="/slack/tasks/interactions"), None
        )

    assert result["statusCode"] == 200
    post_mock.assert_called_once()

    store = TaskStore()
    tasks = store.list_all()
    assert len(tasks) == 1
    t = tasks[0]
    assert t.title == "Fix garbage disposal"
    assert t.assignee_id == "maggie"
    assert t.priority == "high"
    assert t.due_date == "2026-05-01"
    assert t.slack_message_ts == "1700000000.000123"

    # Assignee is not the creator → DM sent
    dm_mock.assert_called_once()


def test_view_submission_requires_title(tasks_bucket):
    from task_handler import slack_tasks_handler
    from task_slack_ui import CREATE_MODAL_CALLBACK_ID

    submission = {
        "type": "view_submission",
        "user": {"id": "UVJ"},
        "view": {
            "callback_id": CREATE_MODAL_CALLBACK_ID,
            "state": {
                "values": {
                    "title_block": {"title": {"value": "  "}},
                    "description_block": {"description": {"value": ""}},
                    "property_block": {
                        "property": {"selected_option": {"value": "prop-palm"}}
                    },
                    "assignee_block": {
                        "assignee": {"selected_option": {"value": "vj"}}
                    },
                    "priority_block": {
                        "priority": {"selected_option": {"value": "normal"}}
                    },
                    "due_date_block": {"due_date": {"selected_date": ""}},
                }
            },
        },
    }
    body = urlencode({"payload": json.dumps(submission)})

    result = slack_tasks_handler(
        _event(body, path="/slack/tasks/interactions"), None
    )
    payload = json.loads(result["body"])
    assert payload["response_action"] == "errors"
    assert "title_block" in payload["errors"]


def test_mark_done_button_closes_task_and_refreshes_card(tasks_bucket):
    from task_handler import slack_tasks_handler
    from task_models import Task
    from task_store import TaskStore

    store = TaskStore()
    t = Task.new(
        title="to close",
        description="",
        property_id="prop-palm",
        assignee_id="vj",
        created_by_id="vj",
    )
    t.slack_channel_id = "C_TEST_TASKS"
    t.slack_message_ts = "1700000000.000001"
    store.put(t)

    payload = {
        "type": "block_actions",
        "user": {"id": "UVJ"},
        "actions": [{"action_id": "task_mark_done", "value": t.id}],
        "response_url": "https://hooks.slack.test/response",
    }
    body = urlencode({"payload": json.dumps(payload)})

    with patch("task_handler.update_message") as update_mock, patch(
        "task_handler.post_message"
    ) as post_mock:
        update_mock.return_value = {"ok": True}
        post_mock.return_value = {"ok": True, "ts": "x", "channel": "C_TEST_TASKS"}

        result = slack_tasks_handler(
            _event(body, path="/slack/tasks/interactions"), None
        )

    assert result["statusCode"] == 200
    updated = store.get(t.id)
    assert updated.status == "done"
    assert updated.completed_at is not None
    update_mock.assert_called_once()


def test_swap_assignee_flips_between_two_users(tasks_bucket):
    from task_handler import slack_tasks_handler
    from task_models import Task
    from task_store import TaskStore

    store = TaskStore()
    t = Task.new(
        title="swap me",
        description="",
        property_id="prop-palm",
        assignee_id="vj",
        created_by_id="vj",
    )
    t.slack_channel_id = "C_TEST_TASKS"
    t.slack_message_ts = "1700000000.000002"
    store.put(t)

    payload = {
        "type": "block_actions",
        "user": {"id": "UVJ"},
        "actions": [{"action_id": "task_swap_assignee", "value": t.id}],
    }
    body = urlencode({"payload": json.dumps(payload)})

    with patch("task_handler.update_message") as _update_mock, patch(
        "task_handler.post_message"
    ) as _post_mock, patch("task_handler.dm_user") as _dm_mock:
        _update_mock.return_value = {"ok": True}
        _post_mock.return_value = {"ok": True}
        _dm_mock.return_value = {"ok": True}
        slack_tasks_handler(_event(body, path="/slack/tasks/interactions"), None)

    updated = store.get(t.id)
    assert updated.assignee_id == "maggie"


def test_thread_reply_appends_comment(tasks_bucket):
    from task_handler import slack_tasks_handler
    from task_models import Task
    from task_store import TaskStore

    store = TaskStore()
    t = Task.new(
        title="with comments",
        description="",
        property_id="prop-palm",
        assignee_id="vj",
        created_by_id="vj",
    )
    t.slack_channel_id = "C_TEST_TASKS"
    t.slack_message_ts = "1700000000.000010"
    store.put(t)
    store.put_slack_index(thread_ts="1700000000.000010", task_id=t.id)

    event_payload = {
        "type": "event_callback",
        "event": {
            "type": "message",
            "channel": "C_TEST_TASKS",
            "thread_ts": "1700000000.000010",
            "ts": "1700000000.000011",
            "user": "UMAGGIE",
            "text": "checked the disposal — looks jammed, will call plumber",
        },
    }
    body = json.dumps(event_payload)

    result = slack_tasks_handler(
        _event(body, content_type="application/json", path="/slack/tasks/events"),
        None,
    )
    assert result["statusCode"] == 200

    updated = store.get(t.id)
    assert len(updated.comments) == 1
    assert updated.comments[0].user_id == "maggie"
    assert "plumber" in updated.comments[0].body


def test_events_url_verification(tasks_bucket):
    from task_handler import slack_tasks_handler

    body = json.dumps({"type": "url_verification", "challenge": "abc123"})
    result = slack_tasks_handler(
        _event(body, content_type="application/json", path="/slack/tasks/events"),
        None,
    )
    assert result["statusCode"] == 200
    assert result["body"] == "abc123"


def test_retry_header_acks_without_processing(tasks_bucket):
    from task_handler import slack_tasks_handler

    event = {
        "body": "anything",
        "headers": {"x-slack-retry-num": "3", "x-slack-retry-reason": "http_timeout"},
    }
    result = slack_tasks_handler(event, None)
    assert result["statusCode"] == 200
