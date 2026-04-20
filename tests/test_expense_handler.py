"""Tests for the Slack-events Lambda handler for the expense workflow."""

import hashlib
import hmac
import json
import time
from unittest.mock import MagicMock, patch

import boto3
import pytest

import expense_handler
from expense_handler import (
    _match_property_from_caption,
    _retention_until_iso,
    _verify_signature,
    ingest_file,
    slack_expenses_handler,
)
from expense_models import CONFIDENCE_HIGH, CONFIDENCE_LOW


_EXPENSES_CHANNEL = "C_TEST_EXPENSES"
_EVENTS_PROPERTIES = [
    {"id": "3278e9cb-9239-487f-aa51-cbfbaf4b7570", "name": "The Palm Club", "slug": "palm"},
    {"id": "f8236d9d-988a-4192-9d16-2927b0b9ad8e", "name": "Villa Bougainvillea", "slug": "villa"},
]


@pytest.fixture
def expenses_env(expenses_bucket):
    """Expenses bucket fixture + task bucket pre-seeded with properties,
    so _match_property_from_caption and _post_confirmation_card both work.
    """
    s3 = boto3.client("s3", region_name="us-east-1")
    s3.create_bucket(Bucket="hospitality-hq-tasks-test")
    s3.put_object(
        Bucket="hospitality-hq-tasks-test",
        Key="config/properties.json",
        Body=json.dumps(_EVENTS_PROPERTIES).encode("utf-8"),
    )
    s3.put_object(
        Bucket="hospitality-hq-tasks-test",
        Key="config/users.json",
        Body=json.dumps([]).encode("utf-8"),
    )
    # Clear the task_store property cache so the fixture's properties win.
    try:
        from task_store import TaskStore

        for attr in ("load_properties", "load_users"):
            fn = getattr(TaskStore, attr, None)
            if fn and hasattr(fn, "cache_clear"):
                fn.cache_clear()
    except ImportError:
        pass
    yield expenses_bucket


# ─── Signature verification ──────────────────────────────────────────────


def _sign(body: str, secret: str, timestamp: str) -> str:
    base = f"v0:{timestamp}:{body}".encode("utf-8")
    return "v0=" + hmac.new(secret.encode("utf-8"), base, hashlib.sha256).hexdigest()


@patch("expense_handler.get_slack_signing_secret", return_value="shh")
def test_verify_signature_accepts_valid_request(_secret):
    body = '{"type":"event_callback"}'
    ts = str(int(time.time()))
    sig = _sign(body, "shh", ts)
    assert _verify_signature(body, ts, sig) is True


@patch("expense_handler.get_slack_signing_secret", return_value="shh")
def test_verify_signature_rejects_old_timestamp(_secret):
    body = "{}"
    ts = str(int(time.time()) - 60 * 10)  # 10 minutes old
    sig = _sign(body, "shh", ts)
    assert _verify_signature(body, ts, sig) is False


@patch("expense_handler.get_slack_signing_secret", return_value="shh")
def test_verify_signature_rejects_bad_signature(_secret):
    body = "{}"
    ts = str(int(time.time()))
    assert _verify_signature(body, ts, "v0=deadbeef") is False


# ─── Handler routing ─────────────────────────────────────────────────────


def _make_event(body: str, secret: str = "shh", ts: str | None = None, retry: str | None = None) -> dict:
    ts = ts or str(int(time.time()))
    headers = {
        "x-slack-request-timestamp": ts,
        "x-slack-signature": _sign(body, secret, ts),
        "content-type": "application/json",
    }
    if retry:
        headers["x-slack-retry-num"] = retry
    return {"body": body, "headers": headers}


@patch("expense_handler.get_slack_signing_secret", return_value="shh")
def test_handler_returns_challenge_for_url_verification(_secret):
    body = json.dumps({"type": "url_verification", "challenge": "xyz123"})
    result = slack_expenses_handler(_make_event(body), None)
    assert result["statusCode"] == 200
    assert result["body"] == "xyz123"


@patch("expense_handler.get_slack_signing_secret", return_value="shh")
def test_handler_rejects_invalid_signature(_secret):
    result = slack_expenses_handler(
        {"body": "{}", "headers": {"x-slack-signature": "v0=bad", "x-slack-request-timestamp": str(int(time.time()))}},
        None,
    )
    assert result["statusCode"] == 401


@patch("expense_handler.get_slack_signing_secret", return_value="shh")
@patch("expense_handler._route_event")
def test_handler_short_circuits_retries(mock_route, _secret):
    body = json.dumps({"type": "event_callback", "event": {}})
    event = _make_event(body, retry="1")
    result = slack_expenses_handler(event, None)
    assert result["statusCode"] == 200
    mock_route.assert_not_called()


@patch("expense_handler.get_slack_signing_secret", return_value="shh")
@patch("expense_handler._route_event")
def test_handler_swallows_inner_exceptions(mock_route, _secret):
    mock_route.side_effect = RuntimeError("boom")
    body = json.dumps({"type": "event_callback", "event": {}})
    result = slack_expenses_handler(_make_event(body), None)
    # Never 500 to Slack — always 200.
    assert result["statusCode"] == 200


@patch("expense_handler.get_slack_signing_secret", return_value="shh")
def test_handler_ignores_non_json_body(_secret):
    result = slack_expenses_handler(_make_event("not-json"), None)
    assert result["statusCode"] == 200


# ─── Event routing ───────────────────────────────────────────────────────


def _file_share_envelope(*, channel=_EXPENSES_CHANNEL, text="", user="UVJ", ts="1745000000.000100", files=None) -> dict:
    return {
        "type": "event_callback",
        "event": {
            "type": "message",
            "subtype": "file_share",
            "channel": channel,
            "text": text,
            "user": user,
            "ts": ts,
            "files": files or [],
        },
    }


@patch("expense_handler.ingest_file")
def test_route_event_skips_wrong_channel(mock_ingest):
    envelope = _file_share_envelope(
        channel="C_ANOTHER_CHANNEL",
        files=[{"id": "F1", "mimetype": "image/jpeg"}],
    )
    expense_handler._route_event(envelope)
    mock_ingest.assert_not_called()


@patch("expense_handler.ingest_file")
def test_route_event_skips_non_file_share(mock_ingest):
    envelope = {
        "type": "event_callback",
        "event": {"type": "message", "subtype": "bot_message", "channel": _EXPENSES_CHANNEL},
    }
    expense_handler._route_event(envelope)
    mock_ingest.assert_not_called()


@patch("expense_handler.ingest_file")
def test_route_event_calls_ingest_for_each_file(mock_ingest):
    envelope = _file_share_envelope(
        files=[
            {"id": "F1", "mimetype": "image/jpeg"},
            {"id": "F2", "mimetype": "image/png"},
        ]
    )
    expense_handler._route_event(envelope)
    assert mock_ingest.call_count == 2


# ─── Ingest pipeline ─────────────────────────────────────────────────────


@patch("expense_handler.add_reaction")
@patch("expense_handler.post_message")
@patch("expense_handler.download_file")
@patch("expense_handler.extract_receipt")
def test_ingest_happy_path(mock_ocr, mock_download, mock_post, mock_react, expenses_env):
    mock_download.return_value = b"image-bytes"
    mock_ocr.return_value = {
        "merchant_name": "Home Depot",
        "transaction_date": "2026-04-18",
        "total": "311.97",
        "subtotal": "287.42",
        "tax": "24.55",
        "suggested_category": "supplies",
        "extraction_confidence": "high",
        "category_confidence": "high",
    }
    mock_post.return_value = {"ok": True}
    mock_react.return_value = {"ok": True}

    message = {
        "channel": _EXPENSES_CHANNEL,
        "ts": "1745000000.000100",
        "user": "UVJ",
        "text": "palm — new garbage disposal",
    }
    file_obj = {
        "id": "F1",
        "mimetype": "image/jpeg",
        "url_private_download": "https://files.slack.com/...",
    }

    expense = ingest_file(message, file_obj)

    assert expense is not None
    assert expense.id.startswith("EXP-2026-")
    assert expense.merchant_name == "Home Depot"
    assert expense.total == "311.97"
    assert expense.category_id == "supplies"  # from rule or suggestion
    assert expense.property_id == "3278e9cb-9239-487f-aa51-cbfbaf4b7570"  # "palm" in caption
    assert expense.needs_review is False
    assert expense.slack_thread_ts == "1745000000.000100"
    assert expense.ocr_model  # stamped
    assert expense.notes == "palm — new garbage disposal"
    assert len(expense.allocations) == 1
    assert expense.allocations[0].percent == "100.00"

    mock_react.assert_called_once()
    mock_post.assert_called_once()
    # The card goes in-thread on the original message
    assert mock_post.call_args.kwargs["thread_ts"] == "1745000000.000100"
    assert mock_post.call_args.kwargs["channel"] == _EXPENSES_CHANNEL


@patch("expense_handler.add_reaction", return_value={"ok": True})
@patch("expense_handler.post_message", return_value={"ok": True})
@patch("expense_handler.download_file")
@patch("expense_handler.extract_receipt")
def test_ingest_flags_low_confidence(mock_ocr, mock_download, mock_post, mock_react, expenses_env):
    mock_download.return_value = b"bytes"
    mock_ocr.return_value = {
        "merchant_name": "???",
        "transaction_date": "2026-04-18",
        "total": "42.00",
        "extraction_confidence": "low",
        "category_confidence": "low",
        "needs_review_reason": "total was smudged",
    }
    message = {"channel": _EXPENSES_CHANNEL, "ts": "1.2", "user": "UVJ", "text": ""}
    file_obj = {"id": "F1", "mimetype": "image/jpeg", "url_private_download": "http://x"}

    expense = ingest_file(message, file_obj)
    assert expense is not None
    assert expense.needs_review is True
    assert expense.review_reason == "total was smudged"
    assert expense.ocr_extraction_confidence == CONFIDENCE_LOW


@patch("expense_handler.add_reaction", return_value={"ok": True})
@patch("expense_handler.post_message", return_value={"ok": True})
@patch("expense_handler.download_file")
@patch("expense_handler.extract_receipt")
def test_ingest_without_caption_leaves_property_unset(
    mock_ocr, mock_download, mock_post, mock_react, expenses_env
):
    mock_download.return_value = b"bytes"
    mock_ocr.return_value = {
        "merchant_name": "Costco",
        "transaction_date": "2026-04-18",
        "total": "100.00",
        "suggested_category": "supplies",
        "extraction_confidence": "high",
        "category_confidence": "high",
    }
    message = {"channel": _EXPENSES_CHANNEL, "ts": "1.3", "user": "UVJ", "text": ""}
    file_obj = {"id": "F2", "mimetype": "image/jpeg", "url_private_download": "http://x"}

    expense = ingest_file(message, file_obj)
    assert expense is not None
    assert expense.property_id is None
    assert expense.allocations == []


@patch("expense_handler.add_reaction", return_value={"ok": True})
@patch("expense_handler.post_message", return_value={"ok": True})
@patch("expense_handler.download_file")
def test_ingest_skips_non_image_files(mock_download, mock_post, mock_react, expenses_env):
    message = {"channel": _EXPENSES_CHANNEL, "ts": "1.4", "user": "UVJ", "text": ""}
    file_obj = {"id": "F3", "mimetype": "application/pdf", "url_private_download": "http://x"}

    assert ingest_file(message, file_obj) is None
    mock_download.assert_not_called()
    mock_post.assert_not_called()


@patch("expense_handler.add_reaction", return_value={"ok": True})
@patch("expense_handler.post_message", return_value={"ok": True})
@patch("expense_handler.download_file")
@patch("expense_handler.extract_receipt")
def test_ingest_is_idempotent_on_same_message_ts(
    mock_ocr, mock_download, mock_post, mock_react, expenses_env
):
    mock_download.return_value = b"bytes"
    mock_ocr.return_value = {
        "merchant_name": "X",
        "transaction_date": "2026-04-18",
        "total": "1.00",
        "suggested_category": "other",
        "extraction_confidence": "high",
        "category_confidence": "high",
    }
    message = {"channel": _EXPENSES_CHANNEL, "ts": "1.5", "user": "UVJ", "text": ""}
    file_obj = {"id": "F4", "mimetype": "image/jpeg", "url_private_download": "http://x"}

    first = ingest_file(message, file_obj)
    second = ingest_file(message, file_obj)

    assert first is not None
    assert second is None  # idempotent — already ingested


@patch("expense_handler.add_reaction", return_value={"ok": True})
@patch("expense_handler.post_message", return_value={"ok": True})
@patch("expense_handler.download_file", side_effect=RuntimeError("net down"))
def test_ingest_posts_error_card_on_download_failure(
    mock_download, mock_post, mock_react, expenses_env
):
    message = {"channel": _EXPENSES_CHANNEL, "ts": "1.6", "user": "UVJ", "text": ""}
    file_obj = {"id": "F5", "mimetype": "image/jpeg", "url_private_download": "http://x"}

    assert ingest_file(message, file_obj) is None
    # Error card posted in thread
    mock_post.assert_called_once()
    assert mock_post.call_args.kwargs["thread_ts"] == "1.6"


@patch("expense_handler.add_reaction", return_value={"ok": True})
@patch("expense_handler.post_message", return_value={"ok": True})
@patch("expense_handler.download_file", return_value=b"bytes")
@patch("expense_handler.extract_receipt", side_effect=RuntimeError("ocr down"))
def test_ingest_posts_error_card_on_ocr_failure(
    mock_ocr, mock_download, mock_post, mock_react, expenses_env
):
    message = {"channel": _EXPENSES_CHANNEL, "ts": "1.7", "user": "UVJ", "text": ""}
    file_obj = {"id": "F6", "mimetype": "image/jpeg", "url_private_download": "http://x"}

    assert ingest_file(message, file_obj) is None
    mock_post.assert_called_once()


# ─── Caption → property matching ────────────────────────────────────────


def test_match_property_exact_slug(expenses_env):
    assert (
        _match_property_from_caption("palm — $30 bulbs")
        == "3278e9cb-9239-487f-aa51-cbfbaf4b7570"
    )
    assert (
        _match_property_from_caption("for villa")
        == "f8236d9d-988a-4192-9d16-2927b0b9ad8e"
    )


def test_match_property_name_substring(expenses_env):
    assert (
        _match_property_from_caption("Home Depot for The Palm Club")
        == "3278e9cb-9239-487f-aa51-cbfbaf4b7570"
    )


def test_match_property_none_when_no_match(expenses_env):
    assert _match_property_from_caption("random note") is None
    assert _match_property_from_caption("") is None
    assert _match_property_from_caption(None) is None


# ─── Retention timestamp ─────────────────────────────────────────────────


def test_retention_until_iso_is_in_the_future():
    from datetime import datetime, timezone

    value = _retention_until_iso()
    # Parse back — should be parseable and clearly in the future (>1 year).
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    delta = parsed - datetime.now(timezone.utc)
    # Default retention is 2555 days (7 years) — allow a wide floor here.
    assert delta.days > 365
    assert value.endswith("Z")
