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


def test_handle_task_thread_message_appends_comment(tasks_bucket):
    """Direct call into the shared event handler used by thread_handler dispatch."""
    from task_handler import handle_task_thread_message
    from task_models import Task
    from task_store import TaskStore

    store = TaskStore()
    t = Task.new(
        title="dispatched task",
        description="",
        property_id="prop-palm",
        assignee_id="vj",
        created_by_id="vj",
    )
    t.slack_channel_id = "C_TEST_TASKS"
    t.slack_message_ts = "1700000000.000020"
    store.put(t)
    store.put_slack_index(thread_ts="1700000000.000020", task_id=t.id)

    inner = {
        "type": "message",
        "channel": "C_TEST_TASKS",
        "thread_ts": "1700000000.000020",
        "ts": "1700000000.000021",
        "user": "UMAGGIE",
        "text": "dispatched comment",
    }
    handle_task_thread_message(inner)

    updated = store.get(t.id)
    assert len(updated.comments) == 1
    assert updated.comments[0].body == "dispatched comment"


def test_handle_task_thread_message_ignores_unknown_thread(tasks_bucket):
    """If the thread_ts doesn't map to any task, do nothing — don't crash."""
    from task_handler import handle_task_thread_message

    inner = {
        "type": "message",
        "channel": "C_TEST_TASKS",
        "thread_ts": "1700000000.999999",
        "ts": "1700000000.999998",
        "user": "UVJ",
        "text": "orphan comment",
    }
    handle_task_thread_message(inner)  # should not raise


def test_quick_create_from_one_liner(tasks_bucket):
    """/task <one-liner> should parse and create a task without opening a modal."""
    from task_handler import slack_tasks_handler
    from task_store import TaskStore
    from unittest.mock import patch

    body = urlencode(
        {
            "command": "/task",
            "text": "maggie fix garbage disposal at palm urgent tomorrow",
            "user_id": "UVJ",
            "trigger_id": "T",
        }
    )

    with patch("task_handler.post_message") as post_mock, patch(
        "task_handler.dm_user"
    ) as dm_mock:
        post_mock.return_value = {"ok": True, "ts": "1700000000.000500", "channel": "C_TEST_TASKS"}
        dm_mock.return_value = {"ok": True}

        result = slack_tasks_handler(_event(body), None)

    assert result["statusCode"] == 200
    payload = json.loads(result["body"])
    assert payload["response_type"] == "ephemeral"
    assert "Created" in payload["text"]

    store = TaskStore()
    tasks = store.list_all()
    assert len(tasks) == 1
    t = tasks[0]
    assert t.title == "fix garbage disposal"
    assert t.assignee_id == "maggie"
    assert t.property_id == "prop-palm"
    assert t.priority == "urgent"
    assert t.due_date is not None

    # Assignee is not the creator → DM should have been sent
    dm_mock.assert_called_once()


def test_quick_create_defaults_when_parse_is_sparse(tasks_bucket):
    """When only a title is given, use sensible defaults (creator, business-wide, normal)."""
    from task_handler import slack_tasks_handler
    from task_store import TaskStore
    from unittest.mock import patch

    body = urlencode(
        {
            "command": "/task",
            "text": "renew scottsdale str registration",
            "user_id": "UVJ",
            "trigger_id": "T",
        }
    )

    with patch("task_handler.post_message") as post_mock, patch(
        "task_handler.dm_user"
    ) as dm_mock:
        post_mock.return_value = {"ok": True, "ts": "x", "channel": "C_TEST_TASKS"}
        dm_mock.return_value = {"ok": True}

        slack_tasks_handler(_event(body), None)

    store = TaskStore()
    tasks = store.list_all()
    assert len(tasks) == 1
    t = tasks[0]
    assert t.title == "renew scottsdale str registration"
    # Defaulted to creator (vj) since no assignee was parsed
    assert t.assignee_id == "vj"
    # No property match → business-wide
    assert t.property_id == "business"
    assert t.priority == "normal"


def test_quick_create_refuses_empty_title(tasks_bucket):
    """If everything parses as metadata, don't create a titleless task."""
    from task_handler import slack_tasks_handler

    body = urlencode(
        {
            "command": "/task",
            "text": "maggie palm urgent tomorrow",
            "user_id": "UVJ",
            "trigger_id": "T",
        }
    )
    result = slack_tasks_handler(_event(body), None)
    payload = json.loads(result["body"])
    assert "couldn't find a title" in payload["text"]


def test_thread_handler_dispatches_tasks_channel_to_task_handler(
    tasks_bucket, all_thread_tables, ssm_with_secrets
):
    """End-to-end: a thread reply in TASKS_CHANNEL_ID reaches the task handler
    via the existing /slack/events endpoint.
    """
    import os
    from unittest.mock import patch

    from task_models import Task
    from task_store import TaskStore

    # Seed a task
    store = TaskStore()
    t = Task.new(
        title="cross-handler task",
        description="",
        property_id="prop-palm",
        assignee_id="vj",
        created_by_id="vj",
    )
    t.slack_channel_id = os.environ["TASKS_CHANNEL_ID"]
    t.slack_message_ts = "1700000000.111111"
    store.put(t)
    store.put_slack_index(thread_ts="1700000000.111111", task_id=t.id)

    # Build a signed Slack event arriving at thread_handler
    event_payload = {
        "type": "event_callback",
        "event": {
            "type": "message",
            "channel": os.environ["TASKS_CHANNEL_ID"],
            "thread_ts": "1700000000.111111",
            "ts": "1700000000.111112",
            "user": "UMAGGIE",
            "text": "via /slack/events",
        },
    }
    body = json.dumps(event_payload)
    ts = str(int(time.time()))
    sig = _sign(body, ts)

    # thread_handler uses its own signing-secret getter — patch it
    with patch(
        "thread_handler.get_slack_signing_secret",
        lambda: SIGNING_SECRET,
    ):
        from thread_handler import slack_events_handler

        result = slack_events_handler(
            {
                "body": body,
                "headers": {
                    "content-type": "application/json",
                    "x-slack-request-timestamp": ts,
                    "x-slack-signature": sig,
                },
            },
            None,
        )

    assert result["statusCode"] == 200
    updated = store.get(t.id)
    assert len(updated.comments) == 1
    assert updated.comments[0].body == "via /slack/events"
