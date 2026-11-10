"""Tests for the unified Slack events + interactivity dispatch in
`thread_handler`.

The Hospitality HQ workspace runs a single Slack app, so every Events
API delivery and every block_actions click flows through the one
`thread_handler.slack_events_handler` Lambda. Dispatch happens on
channel ID (events) and action_id (interactions). These tests cover
the three new branches:

1. `file_share` in #receipts → `expense_handler.process_file_share`
2. Expense-card button clicks → `expense_handler.process_interaction`
3. Non-matching events/clicks are not accidentally routed to the
   expense pipeline.
"""

import hashlib
import hmac
import json
import time
from unittest.mock import patch
from urllib.parse import urlencode

from thread_handler import _process_event, _process_interaction, slack_events_handler


_EXPENSES_CHANNEL = "C_TEST_EXPENSES"  # matches conftest EXPENSES_CHANNEL_ID
_STAY_CHANNEL = "C_TEST_CHANNEL"       # matches conftest SLACK_CHANNEL_ID (guest-alerts)
_SIGNING_SECRET = "test-slack-signing-secret"


# ─── Direct unit tests on _process_event / _process_interaction ────────


def _file_share_envelope(channel: str, user: str = "UVJ") -> dict:
    """Shape of a Slack Events file_share callback."""
    return {
        "type": "event_callback",
        "event": {
            "type": "message",
            "subtype": "file_share",
            "channel": channel,
            "user": user,
            "ts": "1700000000.000100",
            "text": "receipt from home depot",
            "files": [
                {
                    "id": "F_ABC",
                    "mimetype": "image/jpeg",
                    "url_private_download": "https://files.slack.com/foo.jpg",
                }
            ],
        },
    }


def test_file_share_in_receipts_channel_routes_to_expense_handler():
    envelope = _file_share_envelope(_EXPENSES_CHANNEL)
    with patch("expense_handler.process_file_share") as mock_process:
        _process_event(envelope)
    mock_process.assert_called_once()
    (inner_arg,) = mock_process.call_args.args
    assert inner_arg["channel"] == _EXPENSES_CHANNEL
    assert inner_arg["subtype"] == "file_share"
    assert inner_arg["files"][0]["id"] == "F_ABC"


def test_file_share_in_wrong_channel_is_not_routed_to_expenses():
    """A file_share in a stay channel or elsewhere must not trigger the
    expense pipeline (which would attempt to OCR random uploads)."""
    envelope = _file_share_envelope("C_SOMEWHERE_ELSE")
    with patch("expense_handler.process_file_share") as mock_process:
        _process_event(envelope)
    mock_process.assert_not_called()


def test_non_file_share_message_in_receipts_channel_is_ignored():
    """A plain text message in #receipts is not a receipt upload."""
    envelope = _file_share_envelope(_EXPENSES_CHANNEL)
    envelope["event"].pop("subtype")
    envelope["event"].pop("files")
    with patch("expense_handler.process_file_share") as mock_process:
        _process_event(envelope)
    mock_process.assert_not_called()


def test_bot_file_share_is_ignored():
    """Our own bot posting an image (hypothetical) must not loop."""
    envelope = _file_share_envelope(_EXPENSES_CHANNEL)
    envelope["event"]["bot_id"] = "B_SELF"
    with patch("expense_handler.process_file_share") as mock_process:
        _process_event(envelope)
    mock_process.assert_not_called()


# ─── Interactivity dispatch ─────────────────────────────────────────────


def _block_actions_payload(action_id: str, block_id: str = "x") -> str:
    """Serialize a block_actions payload as Slack sends it: urlencoded
    with a single `payload` field containing JSON."""
    inner = {
        "type": "block_actions",
        "user": {"id": "UVJ", "name": "vj"},
        "container": {"channel_id": "C_X", "message_ts": "1.0"},
        "actions": [{"action_id": action_id, "block_id": block_id, "value": "v"}],
    }
    return urlencode({"payload": json.dumps(inner)})


def test_expense_file_it_action_dispatches_to_expense_handler():
    body = _block_actions_payload("expense_file_it")
    with patch("expense_handler.process_interaction") as mock_process:
        _process_interaction(body)
    mock_process.assert_called_once()
    payload = mock_process.call_args.args[0]
    assert payload["actions"][0]["action_id"] == "expense_file_it"


def test_expense_skip_action_dispatches():
    body = _block_actions_payload("expense_skip")
    with patch("expense_handler.process_interaction") as mock_process:
        _process_interaction(body)
    mock_process.assert_called_once()


def test_expense_category_select_dispatches():
    body = _block_actions_payload("expense_category_select")
    with patch("expense_handler.process_interaction") as mock_process:
        _process_interaction(body)
    mock_process.assert_called_once()


def test_stay_send_draft_does_not_reach_expense_handler():
    """Guardrail — stay-channel actions must not accidentally dispatch
    to the expense handler."""
    body = _block_actions_payload("stay_send_draft")
    with patch("expense_handler.process_interaction") as mock_exp, patch(
        "stay_agent.handle_send_draft"
    ) as mock_stay:
        _process_interaction(body)
    mock_exp.assert_not_called()
    mock_stay.assert_called_once()


def test_unknown_action_is_ignored_silently():
    body = _block_actions_payload("totally_fake_action_id")
    with patch("expense_handler.process_interaction") as mock_exp, patch(
        "stay_agent.handle_send_draft"
    ) as mock_stay:
        _process_interaction(body)
    mock_exp.assert_not_called()
    mock_stay.assert_not_called()


# ─── End-to-end: signed POST through slack_events_handler ──────────────


def _signed_event(body: str, content_type: str = "application/json") -> dict:
    ts = str(int(time.time()))
    basestring = f"v0:{ts}:{body}".encode("utf-8")
    sig = "v0=" + hmac.new(_SIGNING_SECRET.encode("utf-8"), basestring, hashlib.sha256).hexdigest()
    return {
        "body": body,
        "isBase64Encoded": False,
        "headers": {
            "Content-Type": content_type,
            "X-Slack-Request-Timestamp": ts,
            "X-Slack-Signature": sig,
        },
    }


@patch("thread_handler.get_slack_signing_secret", return_value=_SIGNING_SECRET)
def test_signed_receipt_file_share_reaches_expense_handler(_secret):
    envelope = _file_share_envelope(_EXPENSES_CHANNEL)
    event = _signed_event(json.dumps(envelope))
    with patch("expense_handler.process_file_share") as mock_process:
        response = slack_events_handler(event, None)
    assert response["statusCode"] == 200
    mock_process.assert_called_once()


@patch("thread_handler.get_slack_signing_secret", return_value=_SIGNING_SECRET)
def test_signed_expense_interaction_reaches_expense_handler(_secret):
    body = _block_actions_payload("expense_file_it")
    event = _signed_event(body, content_type="application/x-www-form-urlencoded")
    with patch("expense_handler.process_interaction") as mock_process:
        response = slack_events_handler(event, None)
    assert response["statusCode"] == 200
    mock_process.assert_called_once()
