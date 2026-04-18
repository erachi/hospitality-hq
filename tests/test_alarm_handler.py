"""Tests for the CloudWatch alarm → Slack notifier."""

import json
import os

import responses
from unittest.mock import patch

os.environ["OPS_SLACK_CHANNEL_ID"] = "C_TEST_OPS_CHANNEL"

from alarm_handler import alarm_handler, post_to_slack  # noqa: E402


def _sns_event(alarm_payload: dict) -> dict:
    """Build a fake SNS-triggered Lambda event wrapping a CloudWatch alarm."""
    return {
        "Records": [
            {
                "EventSource": "aws:sns",
                "Sns": {"Message": json.dumps(alarm_payload)},
            }
        ]
    }


SAMPLE_ALARM = {
    "AlarmName": "hospitality-hq-hospitable-auth-errors",
    "AlarmDescription": "Hospitable returning 401 — rotate the API token",
    "NewStateValue": "ALARM",
    "NewStateReason": "Threshold Crossed: 1 datapoint [5.0 (04/18/26 02:30:00)] was greater than or equal to the threshold (1.0).",
    "StateChangeTime": "2026-04-18T02:30:12.345+0000",
    "Region": "US East (N. Virginia)",
}


@responses.activate
@patch("alarm_handler.get_slack_bot_token", return_value="xoxb-test-token")
def test_alarm_handler_posts_firing_alarm(mock_token):
    """A new ALARM state should post to the ops Slack channel."""
    responses.add(
        responses.POST,
        "https://slack.com/api/chat.postMessage",
        json={"ok": True, "ts": "1234.5678"},
        status=200,
    )

    result = alarm_handler(_sns_event(SAMPLE_ALARM), None)

    assert result["statusCode"] == 200
    assert len(responses.calls) == 1

    body = responses.calls[0].request.body
    if isinstance(body, bytes):
        body = body.decode("utf-8")
    assert "FIRING" in body
    assert "hospitality-hq-hospitable-auth-errors" in body
    assert "Hospitable returning 401" in body
    # Should post to the ops channel, not #guest-alerts
    assert "C_TEST_OPS_CHANNEL" in body


@responses.activate
@patch("alarm_handler.get_slack_bot_token", return_value="xoxb-test-token")
def test_alarm_handler_posts_resolved_alarm(mock_token):
    """An OK transition should post as RESOLVED."""
    responses.add(
        responses.POST,
        "https://slack.com/api/chat.postMessage",
        json={"ok": True, "ts": "1234.5679"},
        status=200,
    )

    resolved = {**SAMPLE_ALARM, "NewStateValue": "OK"}
    alarm_handler(_sns_event(resolved), None)

    body = responses.calls[0].request.body
    if isinstance(body, bytes):
        body = body.decode("utf-8")
    assert "RESOLVED" in body


@responses.activate
@patch("alarm_handler.get_slack_bot_token", return_value="xoxb-test-token")
def test_alarm_handler_handles_multiple_records(mock_token):
    """SNS can deliver multiple records in one event — each should post."""
    responses.add(
        responses.POST,
        "https://slack.com/api/chat.postMessage",
        json={"ok": True, "ts": "1234"},
        status=200,
    )

    event = {
        "Records": [
            {"Sns": {"Message": json.dumps(SAMPLE_ALARM)}},
            {"Sns": {"Message": json.dumps({**SAMPLE_ALARM, "AlarmName": "other"})}},
        ]
    }
    alarm_handler(event, None)
    assert len(responses.calls) == 2


@responses.activate
@patch("alarm_handler.get_slack_bot_token", return_value="xoxb-test-token")
def test_alarm_handler_ignores_malformed_messages(mock_token):
    """Malformed SNS payloads should not raise — just log and continue."""
    event = {
        "Records": [
            {"Sns": {"Message": "not-json"}},
        ]
    }
    result = alarm_handler(event, None)
    assert result["statusCode"] == 200
    # No Slack call made
    assert len(responses.calls) == 0


@responses.activate
@patch("alarm_handler.get_slack_bot_token", return_value="xoxb-test-token")
def test_post_to_slack_uses_bearer_auth(mock_token):
    """Ensure Authorization header uses the bot token."""
    responses.add(
        responses.POST,
        "https://slack.com/api/chat.postMessage",
        json={"ok": True, "ts": "1"},
        status=200,
    )
    post_to_slack(SAMPLE_ALARM)
    assert responses.calls[0].request.headers["Authorization"] == "Bearer xoxb-test-token"
