"""Tests for block_actions handling on the expense card."""

import hashlib
import hmac
import json
import time
from unittest.mock import patch
from urllib.parse import urlencode

import boto3
import pytest

from expense_handler import _route_interaction, slack_expenses_handler
from expense_models import (
    Allocation,
    CONFIDENCE_HIGH,
    CONFIDENCE_LOW,
    Expense,
)
from expense_slack_ui import (
    ACTION_CATEGORY_SELECT,
    ACTION_FILE_IT,
    ACTION_PROPERTY_SELECT,
    ACTION_SKIP,
    ACTION_SPLIT,
    BLOCK_CATEGORY,
    BLOCK_PROPERTY,
    block_id_with_expense,
)
from expense_store import ExpenseStore


_PALM_ID = "3278e9cb-9239-487f-aa51-cbfbaf4b7570"
_VILLA_ID = "f8236d9d-988a-4192-9d16-2927b0b9ad8e"

_PROPERTIES = [
    {"id": _PALM_ID, "name": "The Palm Club", "slug": "palm"},
    {"id": _VILLA_ID, "name": "Villa Bougainvillea", "slug": "villa"},
]


@pytest.fixture
def env(expenses_bucket):
    """Expenses bucket + tasks bucket seeded with properties."""
    s3 = boto3.client("s3", region_name="us-east-1")
    s3.create_bucket(Bucket="hospitality-hq-tasks-test")
    s3.put_object(
        Bucket="hospitality-hq-tasks-test",
        Key="config/properties.json",
        Body=json.dumps(_PROPERTIES).encode("utf-8"),
    )
    s3.put_object(
        Bucket="hospitality-hq-tasks-test",
        Key="config/users.json",
        Body=json.dumps([]).encode("utf-8"),
    )
    try:
        from task_store import TaskStore

        for attr in ("load_properties", "load_users"):
            fn = getattr(TaskStore, attr, None)
            if fn and hasattr(fn, "cache_clear"):
                fn.cache_clear()
    except ImportError:
        pass
    yield expenses_bucket


def _seed_expense(**overrides) -> Expense:
    """Persist an Expense in the store and return it."""
    defaults = dict(
        id="EXP-2026-0042",
        submitter_slack_id="UVJ",
        merchant_name="Home Depot #0438",
        transaction_date="2026-04-18",
        total="311.97",
        currency="USD",
        image_s3_key="receipts/2026/EXP-2026-0042.jpg",
        image_sha256="abc",
        ocr_payload={},
        ocr_model="claude-sonnet-4-6",
        slack_channel_id="C_EXPENSES",
        slack_thread_ts="1745000000.000100",
        created_at="2026-04-18T15:00:00Z",
        updated_at="2026-04-18T15:00:00Z",
        category_id="supplies",
        property_id=_PALM_ID,
        ocr_extraction_confidence=CONFIDENCE_HIGH,
        ocr_category_confidence=CONFIDENCE_HIGH,
        allocations=[Allocation.single(_PALM_ID, "311.97")],
    )
    defaults.update(overrides)
    expense = Expense(**defaults)
    ExpenseStore().put(expense)
    return expense


def _button_payload(*, action_id: str, expense_id: str, response_url: str = "https://hooks.slack.test/1") -> dict:
    return {
        "type": "block_actions",
        "user": {"id": "UVJ"},
        "response_url": response_url,
        "actions": [
            {
                "type": "button",
                "action_id": action_id,
                "value": expense_id,
                "block_id": "expense_actions_block",
            }
        ],
    }


def _select_payload(
    *,
    action_id: str,
    block_prefix: str,
    expense_id: str,
    selected_value: str,
    response_url: str = "https://hooks.slack.test/1",
) -> dict:
    return {
        "type": "block_actions",
        "user": {"id": "UVJ"},
        "response_url": response_url,
        "actions": [
            {
                "type": "static_select",
                "action_id": action_id,
                "block_id": block_id_with_expense(block_prefix, expense_id),
                "selected_option": {
                    "text": {"type": "plain_text", "text": selected_value, "emoji": True},
                    "value": selected_value,
                },
            }
        ],
    }


# ─── File it ─────────────────────────────────────────────────────────────


@patch("expense_handler.post_response_url", return_value={"ok": True})
def test_file_it_updates_card_and_clears_needs_review(mock_post, env):
    expense = _seed_expense(needs_review=True, review_reason="was smudged")
    payload = _button_payload(action_id=ACTION_FILE_IT, expense_id=expense.id)

    _route_interaction(payload)

    refreshed = ExpenseStore().get(expense.id)
    assert refreshed.needs_review is False
    assert refreshed.review_reason is None

    # response_url was used with replace_original to update the card
    call_args = mock_post.call_args
    assert call_args.args[0] == "https://hooks.slack.test/1"
    body = call_args.args[1]
    assert body["replace_original"] is True
    text = body["blocks"][0]["text"]["text"]
    assert "Filed" in text
    assert expense.id in text


@patch("expense_handler.post_response_url", return_value={"ok": True})
def test_file_it_rejects_when_property_not_set(mock_post, env):
    expense = _seed_expense(property_id=None, allocations=[])
    payload = _button_payload(action_id=ACTION_FILE_IT, expense_id=expense.id)

    _route_interaction(payload)

    # Expense unchanged
    refreshed = ExpenseStore().get(expense.id)
    assert refreshed.property_id is None
    assert refreshed.needs_review is False  # unchanged

    # Ephemeral nudge posted back to submitter
    body = mock_post.call_args.args[1]
    assert body.get("response_type") == "ephemeral"
    assert "property" in body["text"].lower()


@patch("expense_handler.post_response_url", return_value={"ok": True})
def test_file_it_updates_allocation_to_match_property(mock_post, env):
    expense = _seed_expense(property_id=_PALM_ID, allocations=[])
    payload = _button_payload(action_id=ACTION_FILE_IT, expense_id=expense.id)

    _route_interaction(payload)

    refreshed = ExpenseStore().get(expense.id)
    assert len(refreshed.allocations) == 1
    assert refreshed.allocations[0].property_id == _PALM_ID
    assert refreshed.allocations[0].percent == "100.00"
    assert refreshed.allocations[0].amount == "311.97"


# ─── Skip ────────────────────────────────────────────────────────────────


@patch("expense_handler.post_response_url", return_value={"ok": True})
def test_skip_marks_personal_and_renders_skipped_card(mock_post, env):
    expense = _seed_expense()
    payload = _button_payload(action_id=ACTION_SKIP, expense_id=expense.id)

    _route_interaction(payload)

    refreshed = ExpenseStore().get(expense.id)
    assert refreshed.is_personal is True
    assert refreshed.needs_review is False

    body = mock_post.call_args.args[1]
    text = body["blocks"][0]["text"]["text"]
    assert "Skipped" in text
    assert refreshed.id in text


# ─── Split placeholder ──────────────────────────────────────────────────


@patch("expense_handler.post_response_url", return_value={"ok": True})
def test_split_shows_v2_ephemeral_notice(mock_post, env):
    expense = _seed_expense()
    payload = _button_payload(action_id=ACTION_SPLIT, expense_id=expense.id)

    _route_interaction(payload)

    # Nothing on the expense changes
    refreshed = ExpenseStore().get(expense.id)
    assert refreshed.to_dict() == expense.to_dict()

    body = mock_post.call_args.args[1]
    assert body.get("response_type") == "ephemeral"
    assert "v2" in body["text"].lower()


# ─── Property select ─────────────────────────────────────────────────────


@patch("expense_handler.post_response_url", return_value={"ok": True})
def test_property_select_updates_expense_and_re_renders(mock_post, env):
    expense = _seed_expense(property_id=_PALM_ID)
    payload = _select_payload(
        action_id=ACTION_PROPERTY_SELECT,
        block_prefix=BLOCK_PROPERTY,
        expense_id=expense.id,
        selected_value=_VILLA_ID,
    )

    _route_interaction(payload)

    refreshed = ExpenseStore().get(expense.id)
    assert refreshed.property_id == _VILLA_ID
    assert refreshed.allocations[0].property_id == _VILLA_ID

    body = mock_post.call_args.args[1]
    assert body["replace_original"] is True


@patch("expense_handler.post_response_url", return_value={"ok": True})
def test_property_select_same_value_is_noop(mock_post, env):
    """Slack sometimes re-fires on page refresh — don't thrash S3."""
    expense = _seed_expense(property_id=_PALM_ID)
    payload = _select_payload(
        action_id=ACTION_PROPERTY_SELECT,
        block_prefix=BLOCK_PROPERTY,
        expense_id=expense.id,
        selected_value=_PALM_ID,
    )

    _route_interaction(payload)

    # No re-render posted
    mock_post.assert_not_called()


# ─── Category select ─────────────────────────────────────────────────────


@patch("expense_handler.post_response_url", return_value={"ok": True})
def test_category_select_updates_and_clears_low_confidence_flag(mock_post, env):
    expense = _seed_expense(
        category_id="supplies", ocr_category_confidence=CONFIDENCE_LOW
    )
    payload = _select_payload(
        action_id=ACTION_CATEGORY_SELECT,
        block_prefix=BLOCK_CATEGORY,
        expense_id=expense.id,
        selected_value="repairs",
    )

    _route_interaction(payload)

    refreshed = ExpenseStore().get(expense.id)
    assert refreshed.category_id == "repairs"
    # User-confirmed → red dot goes away on re-render
    assert refreshed.ocr_category_confidence is None


@patch("expense_handler.post_response_url", return_value={"ok": True})
def test_category_select_rejects_unknown_category(mock_post, env):
    expense = _seed_expense(category_id="supplies")
    payload = _select_payload(
        action_id=ACTION_CATEGORY_SELECT,
        block_prefix=BLOCK_CATEGORY,
        expense_id=expense.id,
        selected_value="bogus_category_id",
    )

    _route_interaction(payload)

    refreshed = ExpenseStore().get(expense.id)
    assert refreshed.category_id == "supplies"  # unchanged
    mock_post.assert_not_called()


# ─── Routing edge cases ──────────────────────────────────────────────────


@patch("expense_handler.post_response_url", return_value={"ok": True})
def test_unknown_expense_id_surfaces_ephemeral(mock_post, env):
    payload = _button_payload(action_id=ACTION_FILE_IT, expense_id="EXP-2099-9999")

    _route_interaction(payload)

    body = mock_post.call_args.args[1]
    assert body.get("response_type") == "ephemeral"
    assert "no longer exists" in body["text"]


@patch("expense_handler.post_response_url", return_value={"ok": True})
def test_missing_expense_id_short_circuits(mock_post, env):
    payload = {
        "type": "block_actions",
        "user": {"id": "UVJ"},
        "response_url": "https://hooks.slack.test/1",
        "actions": [{"action_id": ACTION_FILE_IT, "value": "", "block_id": ""}],
    }

    _route_interaction(payload)

    body = mock_post.call_args.args[1]
    assert body.get("response_type") == "ephemeral"


def test_non_block_actions_type_is_ignored(env):
    # view_submission is handled differently in the task workflow; we
    # don't use modals in expenses, so just make sure the type is ignored.
    payload = {"type": "view_submission", "user": {"id": "UVJ"}}
    result = _route_interaction(payload)
    assert result["statusCode"] == 200


# ─── End-to-end through slack_expenses_handler ──────────────────────────


def _sign(body: str, secret: str, ts: str) -> str:
    base = f"v0:{ts}:{body}".encode("utf-8")
    return "v0=" + hmac.new(secret.encode("utf-8"), base, hashlib.sha256).hexdigest()


@patch("expense_handler.get_slack_signing_secret", return_value="shh")
@patch("expense_handler.post_response_url", return_value={"ok": True})
def test_handler_routes_form_urlencoded_interaction(mock_post, _secret, env):
    expense = _seed_expense()
    interaction_payload = _button_payload(
        action_id=ACTION_FILE_IT, expense_id=expense.id
    )
    body = urlencode({"payload": json.dumps(interaction_payload)})
    ts = str(int(time.time()))
    sig = _sign(body, "shh", ts)

    event = {
        "body": body,
        "headers": {
            "x-slack-request-timestamp": ts,
            "x-slack-signature": sig,
            "content-type": "application/x-www-form-urlencoded",
        },
    }

    result = slack_expenses_handler(event, None)
    assert result["statusCode"] == 200
    # Card replaced via response_url
    mock_post.assert_called_once()
    assert mock_post.call_args.args[1]["replace_original"] is True


@patch("expense_handler.get_slack_signing_secret", return_value="shh")
@patch("expense_handler.post_response_url", return_value={"ok": True})
def test_handler_form_body_without_payload_is_ignored(mock_post, _secret, env):
    body = urlencode({"command": "/nothing"})
    ts = str(int(time.time()))
    sig = _sign(body, "shh", ts)

    event = {
        "body": body,
        "headers": {
            "x-slack-request-timestamp": ts,
            "x-slack-signature": sig,
            "content-type": "application/x-www-form-urlencoded",
        },
    }

    result = slack_expenses_handler(event, None)
    assert result["statusCode"] == 200
    mock_post.assert_not_called()
