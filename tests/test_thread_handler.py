"""Tests for the Slack Events thread handler Lambda."""

import hashlib
import hmac
import json
import time
from unittest.mock import patch, MagicMock

from moto import mock_aws

from thread_handler import (
    slack_events_handler,
    _classify_intent,
    _verify_signature,
)


TEST_SIGNING_SECRET = "test-slack-signing-secret"
TEST_CHANNEL = "C_TEST_CHANNEL"  # matches conftest SLACK_CHANNEL_ID


def _signed_event(payload: dict, secret: str = TEST_SIGNING_SECRET, timestamp: int | None = None) -> dict:
    """Build an API Gateway proxy event with a valid Slack v0 signature."""
    body = json.dumps(payload)
    ts = str(timestamp or int(time.time()))
    basestring = f"v0:{ts}:{body}".encode("utf-8")
    sig = "v0=" + hmac.new(secret.encode("utf-8"), basestring, hashlib.sha256).hexdigest()
    return {
        "body": body,
        "isBase64Encoded": False,
        "headers": {
            "X-Slack-Request-Timestamp": ts,
            "X-Slack-Signature": sig,
        },
    }


def _message_event(text: str, thread_ts: str = "1713400000.000100", user: str = "U123", channel: str = TEST_CHANNEL) -> dict:
    return {
        "type": "event_callback",
        "event_id": "Ev12345",
        "event": {
            "type": "message",
            "channel": channel,
            "user": user,
            "text": text,
            "ts": "1713400005.000200",
            "thread_ts": thread_ts,
        },
    }


# ───────────────────────────────────────────────────────────────────
# Intent classification (pure function, no AWS/HTTP)
# ───────────────────────────────────────────────────────────────────

def test_classify_intent_note():
    assert _classify_intent("note: extra towels please") == ("note", "extra towels please")

def test_classify_intent_issue():
    assert _classify_intent("issue: projector cord missing") == ("issue", "projector cord missing")

def test_classify_intent_resolved_alone():
    assert _classify_intent("resolved") == ("resolution", "")

def test_classify_intent_resolved_with_trailing_text():
    assert _classify_intent("resolved — called the guest") == ("resolution", "called the guest")

def test_classify_intent_case_insensitive_prefix():
    assert _classify_intent("NOTE: hello") == ("note", "hello")

def test_classify_intent_question_by_default():
    assert _classify_intent("what is the wifi password?") == ("question", "what is the wifi password?")

def test_classify_intent_not_resolved_substring():
    """'resolvedly' or 'resolver' shouldn't trigger resolution."""
    assert _classify_intent("resolvedly something")[0] == "question"


# ───────────────────────────────────────────────────────────────────
# Signature verification
# ───────────────────────────────────────────────────────────────────

@patch("thread_handler.get_slack_signing_secret", return_value=TEST_SIGNING_SECRET)
def test_verify_signature_valid(mock_secret):
    body = '{"test": true}'
    ts = str(int(time.time()))
    basestring = f"v0:{ts}:{body}".encode()
    sig = "v0=" + hmac.new(TEST_SIGNING_SECRET.encode(), basestring, hashlib.sha256).hexdigest()
    assert _verify_signature(body, ts, sig) is True


@patch("thread_handler.get_slack_signing_secret", return_value=TEST_SIGNING_SECRET)
def test_verify_signature_wrong_sig(mock_secret):
    ts = str(int(time.time()))
    assert _verify_signature("body", ts, "v0=badsig") is False


@patch("thread_handler.get_slack_signing_secret", return_value=TEST_SIGNING_SECRET)
def test_verify_signature_stale_timestamp(mock_secret):
    body = '{"t": 1}'
    old_ts = str(int(time.time()) - 3600)  # 1 hour ago
    basestring = f"v0:{old_ts}:{body}".encode()
    sig = "v0=" + hmac.new(TEST_SIGNING_SECRET.encode(), basestring, hashlib.sha256).hexdigest()
    assert _verify_signature(body, old_ts, sig) is False


@patch("thread_handler.get_slack_signing_secret", return_value="")
def test_verify_signature_no_secret_configured(mock_secret):
    """No configured secret must reject everything — don't allow unauthenticated access."""
    ts = str(int(time.time()))
    assert _verify_signature("body", ts, "v0=anything") is False


# ───────────────────────────────────────────────────────────────────
# URL verification challenge
# ───────────────────────────────────────────────────────────────────

@patch("thread_handler.get_slack_signing_secret", return_value=TEST_SIGNING_SECRET)
def test_url_verification_echoes_challenge(mock_secret):
    event = _signed_event({"type": "url_verification", "challenge": "abc123"})
    result = slack_events_handler(event, None)
    assert result["statusCode"] == 200
    assert result["body"] == "abc123"


# ───────────────────────────────────────────────────────────────────
# Invalid signature → 401
# ───────────────────────────────────────────────────────────────────

@patch("thread_handler.get_slack_signing_secret", return_value=TEST_SIGNING_SECRET)
def test_invalid_signature_returns_401(mock_secret):
    event = {
        "body": json.dumps({"type": "url_verification", "challenge": "x"}),
        "isBase64Encoded": False,
        "headers": {
            "X-Slack-Request-Timestamp": str(int(time.time())),
            "X-Slack-Signature": "v0=bad",
        },
    }
    result = slack_events_handler(event, None)
    assert result["statusCode"] == 401


# ───────────────────────────────────────────────────────────────────
# Retry short-circuit
# ───────────────────────────────────────────────────────────────────

@patch("thread_handler.get_slack_signing_secret", return_value=TEST_SIGNING_SECRET)
def test_retry_header_acks_without_processing(mock_secret):
    event = _signed_event({"type": "event_callback", "event": {"type": "message", "thread_ts": "x"}})
    event["headers"]["X-Slack-Retry-Num"] = "1"
    event["headers"]["X-Slack-Retry-Reason"] = "http_timeout"
    result = slack_events_handler(event, None)
    assert result["statusCode"] == 200


# ───────────────────────────────────────────────────────────────────
# End-to-end: log actions + question routing
# ───────────────────────────────────────────────────────────────────

@mock_aws
@patch("thread_handler.post_thread_reply")
@patch("thread_handler.get_slack_signing_secret", return_value=TEST_SIGNING_SECRET)
def test_note_prefix_logs_and_confirms(mock_secret, mock_post, all_thread_tables):
    """'note: ...' appends a log entry and confirms in-thread."""
    from thread_mapping import ThreadMapping

    ThreadMapping().put_mapping(
        thread_ts="1713400000.000100",
        reservation_uuid="res-abc",
        property_id="prop-1",
        property_name="Villa",
        guest_name="Jane",
    )

    event = _signed_event(_message_event("note: guest is a bachelorette party"))
    result = slack_events_handler(event, None)
    assert result["statusCode"] == 200

    # Log was written
    from thread_logs import ThreadLogs
    logs = ThreadLogs().get_logs("res-abc")
    assert len(logs) == 1
    assert logs[0]["type"] == "note"
    assert "bachelorette" in logs[0]["text"]

    # Confirmation posted to thread
    mock_post.assert_called_once()
    args, kwargs = mock_post.call_args
    thread_ts, text = args[0], args[1]
    assert thread_ts == "1713400000.000100"
    assert "saved" in text.lower() or "note" in text.lower()


@mock_aws
@patch("thread_handler.post_thread_reply")
@patch("thread_handler.get_slack_signing_secret", return_value=TEST_SIGNING_SECRET)
def test_issue_prefix_logs(mock_secret, mock_post, all_thread_tables):
    from thread_mapping import ThreadMapping
    from thread_logs import ThreadLogs

    ThreadMapping().put_mapping("1.1", "res-X", "prop-1", "Villa", "Jane")
    event = _signed_event(_message_event("issue: cord missing", thread_ts="1.1"))
    slack_events_handler(event, None)

    logs = ThreadLogs().get_logs("res-X")
    assert len(logs) == 1
    assert logs[0]["type"] == "issue"


@mock_aws
@patch("thread_handler.post_thread_reply")
@patch("thread_handler.get_slack_signing_secret", return_value=TEST_SIGNING_SECRET)
def test_resolved_prefix_logs(mock_secret, mock_post, all_thread_tables):
    from thread_mapping import ThreadMapping
    from thread_logs import ThreadLogs

    ThreadMapping().put_mapping("1.2", "res-Y", "prop-1", "Villa", "Jane")
    event = _signed_event(_message_event("resolved", thread_ts="1.2"))
    slack_events_handler(event, None)

    logs = ThreadLogs().get_logs("res-Y")
    assert len(logs) == 1
    assert logs[0]["type"] == "resolution"


@mock_aws
@patch("thread_handler.qa_answer")
@patch("thread_handler.post_thread_reply")
@patch("thread_handler.HospitableClient")
@patch("thread_handler.get_slack_signing_secret", return_value=TEST_SIGNING_SECRET)
def test_question_is_answered_via_claude(mock_secret, mock_hospitable, mock_post, mock_qa, all_thread_tables):
    """Plain text → treated as a question → Claude answer posted in thread."""
    from thread_mapping import ThreadMapping

    # Hospitable is mocked; make every call safe
    mock_client = MagicMock()
    mock_hospitable.return_value = mock_client
    mock_client.get_reservation_detail.return_value = {}
    mock_client.get_reservation_messages.return_value = []
    mock_client.get_property.return_value = {}
    mock_client.get_property_knowledge_hub.return_value = {}

    mock_qa.return_value = "Check-in is at 4 PM."

    ThreadMapping().put_mapping("1.3", "res-Z", "prop-1", "Villa", "Jane")

    event = _signed_event(_message_event("what time is check-in?", thread_ts="1.3"))
    slack_events_handler(event, None)

    mock_qa.assert_called_once()
    mock_post.assert_called_once()
    args = mock_post.call_args.args
    assert args[0] == "1.3"
    assert "Check-in" in args[1]


# ───────────────────────────────────────────────────────────────────
# Filters
# ───────────────────────────────────────────────────────────────────

@mock_aws
@patch("thread_handler.post_thread_reply")
@patch("thread_handler.get_slack_signing_secret", return_value=TEST_SIGNING_SECRET)
def test_unknown_thread_posts_help(mock_secret, mock_post, all_thread_tables):
    """Message in a thread we didn't map should reply with 'thread not found' nudge."""
    event = _signed_event(_message_event("what's up?", thread_ts="never-seen-ts"))
    slack_events_handler(event, None)
    mock_post.assert_called_once()
    assert "mapping" in mock_post.call_args.args[1].lower() or "thread" in mock_post.call_args.args[1].lower()


@mock_aws
@patch("thread_handler.post_thread_reply")
@patch("thread_handler.get_slack_signing_secret", return_value=TEST_SIGNING_SECRET)
def test_bot_message_is_ignored(mock_secret, mock_post, all_thread_tables):
    """Messages from bots (our own replies) must not trigger processing."""
    inner = _message_event("resolved", thread_ts="1.1")
    inner["event"]["bot_id"] = "B123"
    event = _signed_event(inner)
    slack_events_handler(event, None)
    mock_post.assert_not_called()


@mock_aws
@patch("thread_handler.post_thread_reply")
@patch("thread_handler.get_slack_signing_secret", return_value=TEST_SIGNING_SECRET)
def test_wrong_channel_is_ignored(mock_secret, mock_post, all_thread_tables):
    event = _signed_event(_message_event("hi", channel="C_OTHER"))
    slack_events_handler(event, None)
    mock_post.assert_not_called()


@mock_aws
@patch("thread_handler.post_thread_reply")
@patch("thread_handler.get_slack_signing_secret", return_value=TEST_SIGNING_SECRET)
def test_top_level_message_is_ignored(mock_secret, mock_post, all_thread_tables):
    """Message with no thread_ts (or equal to ts) is a top-level channel message, not a thread reply."""
    inner = _message_event("hi there")
    inner["event"]["thread_ts"] = inner["event"]["ts"]  # top-level marker
    event = _signed_event(inner)
    slack_events_handler(event, None)
    mock_post.assert_not_called()
