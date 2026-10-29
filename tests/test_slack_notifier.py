"""Tests for Slack notification formatting and posting.

The alert layout was slimmed to 5 blocks: a prefix context line, the AI
situation summary, the quoted guest message, the draft reply, and a footer
context line. Booking grid, repeat/new badge, source/status, and full
conversation history are intentionally NOT rendered in the top message —
they're available via thread Q&A.
"""

import json
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import responses
from unittest.mock import patch

from slack_notifier import post_guest_alert, _format_message_time


def _request_payload(call) -> dict:
    body = call.request.body
    if isinstance(body, bytes):
        body = body.decode("utf-8")
    return json.loads(body)


def _blocks_text(payload: dict) -> str:
    """Concatenate every text value across all blocks for substring assertions."""
    parts = []
    for block in payload.get("blocks", []):
        text = block.get("text")
        if isinstance(text, dict):
            parts.append(text.get("text", ""))
        for field in block.get("fields", []) or []:
            parts.append(field.get("text", ""))
        for el in block.get("elements", []) or []:
            parts.append(el.get("text", ""))
    return "\n".join(parts)


# ─── Call defaults ────────────────────────────────────────────────────────

_BASE_KWARGS = dict(
    guest_name="Jane Smith",
    property_name="Villa Bougainvillea",
    checkin_date="2026-04-20",
    checkout_date="2026-04-25",
    guest_message="The AC isn't working and it's really hot!",
    classification={
        "category": "URGENT_MAINTENANCE",
        "urgency": "HIGH",
        "summary": "AC not working",
        "descriptor": "Urgent Maintenance",
    },
    draft_response="Hi Jane! Sending our tech now.",
    reservation_uuid="res-uuid-123",
)


def _call(**overrides) -> dict:
    kwargs = {**_BASE_KWARGS, **overrides}
    return post_guest_alert(**kwargs)


def _stub_slack():
    responses.add(
        responses.POST,
        "https://slack.com/api/chat.postMessage",
        json={"ok": True, "ts": "1234567890.123456"},
        status=200,
    )


# ─── Layout: block count + ordering ───────────────────────────────────────


@responses.activate
@patch("slack_notifier.get_slack_bot_token", return_value="xoxb-test-token")
def test_alert_renders_five_blocks_in_order(mock_token):
    """The lean layout is exactly 5 blocks: prefix, situation, guest msg,
    draft, footer. Guards against anyone re-adding the booking grid or
    history on top."""
    _stub_slack()
    _call()
    payload = _request_payload(responses.calls[0])
    blocks = payload["blocks"]
    assert len(blocks) == 5
    assert [b["type"] for b in blocks] == ["context", "section", "section", "section", "context"]


# ─── Prefix line (block 1) ────────────────────────────────────────────────


@responses.activate
@patch("slack_notifier.get_slack_bot_token", return_value="xoxb-test-token")
def test_prefix_shows_urgency_descriptor_property(mock_token):
    _stub_slack()
    _call()
    payload = _request_payload(responses.calls[0])
    prefix = payload["blocks"][0]["elements"][0]["text"]
    assert "🔴" in prefix
    assert "Urgent Maintenance" in prefix
    assert "Villa Bougainvillea" in prefix


@responses.activate
@patch("slack_notifier.get_slack_bot_token", return_value="xoxb-test-token")
def test_prefix_includes_local_message_time_when_provided(mock_token):
    _stub_slack()
    now_local = datetime.now(ZoneInfo("America/Phoenix"))
    iso = now_local.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    _call(message_timestamp=iso)
    payload = _request_payload(responses.calls[0])
    prefix = payload["blocks"][0]["elements"][0]["text"]
    # Today's message renders as "today H:MM AM/PM MST".
    assert "today" in prefix
    assert "MST" in prefix or "MDT" in prefix


@responses.activate
@patch("slack_notifier.get_slack_bot_token", return_value="xoxb-test-token")
def test_prefix_omits_time_clause_when_timestamp_missing(mock_token):
    """Missing message_timestamp must not add an empty " · " at the end."""
    _stub_slack()
    _call(message_timestamp="")
    payload = _request_payload(responses.calls[0])
    prefix = payload["blocks"][0]["elements"][0]["text"]
    assert not prefix.rstrip().endswith("·")


@responses.activate
@patch("slack_notifier.get_slack_bot_token", return_value="xoxb-test-token")
def test_low_urgency_uses_green_dot(mock_token):
    _stub_slack()
    _call(classification={
        "category": "PRE_ARRIVAL", "urgency": "LOW",
        "summary": "Early check-in", "descriptor": "Early Check-in Request",
    })
    payload = _request_payload(responses.calls[0])
    prefix = payload["blocks"][0]["elements"][0]["text"]
    assert "🟢" in prefix
    assert "🔴" not in prefix


@responses.activate
@patch("slack_notifier.get_slack_bot_token", return_value="xoxb-test-token")
def test_prefix_descriptor_falls_back_to_category_label(mock_token):
    """When the classifier omits `descriptor`, use the human category label."""
    _stub_slack()
    _call(classification={"category": "GENERAL", "urgency": "MEDIUM", "summary": "hi"})
    payload = _request_payload(responses.calls[0])
    prefix = payload["blocks"][0]["elements"][0]["text"]
    assert "General Inquiry" in prefix


@responses.activate
@patch("slack_notifier.get_slack_bot_token", return_value="xoxb-test-token")
def test_no_big_h1_header_block(mock_token):
    """Regression guard: the old layout opened with a `header` block. The lean
    layout opens with `context`."""
    _stub_slack()
    _call()
    payload = _request_payload(responses.calls[0])
    assert payload["blocks"][0]["type"] != "header"
    assert not any(b.get("type") == "header" for b in payload["blocks"])


# ─── Situation block (block 2) ────────────────────────────────────────────


@responses.activate
@patch("slack_notifier.get_slack_bot_token", return_value="xoxb-test-token")
def test_situation_renders_haiku_summary_when_provided(mock_token):
    _stub_slack()
    summary = (
        "• *Agreed so far:* 4pm check-in confirmed\n"
        "• *Still open:* AC not cooling"
    )
    _call(conversation_summary=summary)
    payload = _request_payload(responses.calls[0])
    situation = payload["blocks"][1]["text"]["text"]
    assert "Agreed so far" in situation
    assert "Still open" in situation


@responses.activate
@patch("slack_notifier.get_slack_bot_token", return_value="xoxb-test-token")
def test_situation_falls_back_to_classifier_summary(mock_token):
    """Haiku summary occasionally fails upstream (handler logs + passes "").
    The situation block must still carry some context — the classifier's
    one-line summary."""
    _stub_slack()
    _call(conversation_summary="")
    payload = _request_payload(responses.calls[0])
    situation = payload["blocks"][1]["text"]["text"]
    assert "AC not working" in situation


# ─── Guest message block (block 3) ────────────────────────────────────────


@responses.activate
@patch("slack_notifier.get_slack_bot_token", return_value="xoxb-test-token")
def test_guest_message_block_labels_and_quotes(mock_token):
    _stub_slack()
    _call()
    payload = _request_payload(responses.calls[0])
    guest_block = payload["blocks"][2]["text"]["text"]
    assert "Guest · Jane Smith" in guest_block
    # Blockquote prefix on the actual message.
    assert "> The AC isn't working and it's really hot!" in guest_block


# ─── Draft block (block 4) ────────────────────────────────────────────────


@responses.activate
@patch("slack_notifier.get_slack_bot_token", return_value="xoxb-test-token")
def test_draft_block_labels_and_quotes(mock_token):
    _stub_slack()
    _call()
    payload = _request_payload(responses.calls[0])
    draft_block = payload["blocks"][3]["text"]["text"]
    assert "Draft reply" in draft_block
    assert "> Hi Jane! Sending our tech now." in draft_block


@responses.activate
@patch("slack_notifier.get_slack_bot_token", return_value="xoxb-test-token")
def test_draft_block_renders_placeholder_when_draft_missing(mock_token):
    _stub_slack()
    _call(draft_response="")
    payload = _request_payload(responses.calls[0])
    draft_block = payload["blocks"][3]["text"]["text"]
    assert "no draft" in draft_block


# ─── Footer (block 5) ─────────────────────────────────────────────────────


@responses.activate
@patch("slack_notifier.get_slack_bot_token", return_value="xoxb-test-token")
def test_footer_shows_reservation_uuid_and_cta(mock_token):
    _stub_slack()
    _call()
    payload = _request_payload(responses.calls[0])
    footer = payload["blocks"][4]["elements"][0]["text"]
    assert "res-uuid-123" in footer
    assert "Copy draft" in footer


@responses.activate
@patch("slack_notifier.get_slack_bot_token", return_value="xoxb-test-token")
def test_footer_no_response_needed_hides_cta(mock_token):
    _stub_slack()
    _call(classification={
        "category": "POSITIVE", "urgency": "LOW",
        "summary": "Guest enjoyed stay", "response_needed": False,
    })
    payload = _request_payload(responses.calls[0])
    footer = payload["blocks"][4]["elements"][0]["text"]
    assert "No response needed" in footer
    assert "Copy draft" not in footer


@responses.activate
@patch("slack_notifier.get_slack_bot_token", return_value="xoxb-test-token")
def test_positive_with_response_needed_still_prompts(mock_token):
    """A thank-you that embeds another question (`response_needed=True`) must
    still show the Copy-draft CTA. Previously regressed when the footer
    switched on `category == POSITIVE` directly."""
    _stub_slack()
    _call(classification={
        "category": "POSITIVE", "urgency": "LOW",
        "summary": "Thanks + question", "response_needed": True,
    })
    payload = _request_payload(responses.calls[0])
    footer = payload["blocks"][4]["elements"][0]["text"]
    assert "Copy draft" in footer


@responses.activate
@patch("slack_notifier.get_slack_bot_token", return_value="xoxb-test-token")
def test_response_needed_defaults_to_true(mock_token):
    _stub_slack()
    _call(classification={"category": "GENERAL", "urgency": "LOW", "summary": "x"})
    payload = _request_payload(responses.calls[0])
    footer = payload["blocks"][4]["elements"][0]["text"]
    assert "Copy draft" in footer


# ─── Mobile fallback text ─────────────────────────────────────────────────


@responses.activate
@patch("slack_notifier.get_slack_bot_token", return_value="xoxb-test-token")
def test_mobile_fallback_text_is_terse_one_liner(mock_token):
    _stub_slack()
    _call()
    payload = _request_payload(responses.calls[0])
    assert "Urgent Maintenance" in payload["text"]
    assert "Villa Bougainvillea" in payload["text"]
    assert "AC not working" in payload["text"]


# ─── Regression guards: things that must NOT appear in the lean alert ────


@responses.activate
@patch("slack_notifier.get_slack_bot_token", return_value="xoxb-test-token")
def test_booking_grid_fields_are_not_rendered(mock_token):
    """Repeat/new badge, source platform, status, and stay dates all moved
    out of the top message. Each stays reachable via thread Q&A."""
    _stub_slack()
    _call(booking_source="airbnb", reservation_status="checkpoint", is_repeat_guest=True)
    payload = _request_payload(responses.calls[0])
    text = _blocks_text(payload)
    assert "Airbnb" not in text
    assert "Checked In" not in text
    assert "(Repeat)" not in text
    assert "(New)" not in text
    # Stay dates (as supplied via checkin_date/checkout_date) are not rendered either.
    assert "2026-04-20" not in text
    assert "2026-04-25" not in text


@responses.activate
@patch("slack_notifier.get_slack_bot_token", return_value="xoxb-test-token")
def test_conversation_history_is_not_rendered(mock_token):
    """`recent_messages` is accepted for backward-compat but must not appear
    on the top message — thread Q&A handles it on demand."""
    _stub_slack()
    history = [
        {"sender_type": "guest", "body": "Hi, checking in at 4?", "created_at": "2026-04-20T20:00:00Z"},
        {"sender_type": "host",  "body": "Yes, 4pm works!",        "created_at": "2026-04-20T20:05:00Z"},
    ]
    _call(recent_messages=history)
    payload = _request_payload(responses.calls[0])
    text = _blocks_text(payload)
    assert "Hi, checking in at 4?" not in text
    assert "Yes, 4pm works!" not in text
    # Old section labels are gone.
    assert "CONVERSATION HISTORY" not in text
    assert "CONVERSATION SUMMARY" not in text
    assert "NEW MESSAGE" not in text


# ─── Auth header (unchanged behavior) ─────────────────────────────────────


@responses.activate
@patch("slack_notifier.get_slack_bot_token", return_value="xoxb-test-token")
def test_slack_auth_header(mock_token):
    _stub_slack()
    _call()
    assert responses.calls[0].request.headers["Authorization"] == "Bearer xoxb-test-token"


# ─── _format_message_time unit tests ──────────────────────────────────────


def test_format_message_time_today():
    now_utc = datetime.now(timezone.utc)
    out = _format_message_time(now_utc.strftime("%Y-%m-%dT%H:%M:%SZ"))
    assert out.startswith("today ")
    assert "MST" in out or "MDT" in out


def test_format_message_time_yesterday():
    now_phx = datetime.now(ZoneInfo("America/Phoenix"))
    yesterday_phx = now_phx - timedelta(days=1)
    iso = yesterday_phx.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    out = _format_message_time(iso)
    assert out.startswith("yesterday ")


def test_format_message_time_old_message_uses_month_day():
    # 10 days ago — not today, not yesterday.
    old = datetime.now(timezone.utc) - timedelta(days=10)
    iso = old.strftime("%Y-%m-%dT%H:%M:%SZ")
    out = _format_message_time(iso)
    assert not out.startswith("today")
    assert not out.startswith("yesterday")
    # Should include a month abbreviation.
    assert any(mo in out for mo in ("Jan","Feb","Mar","Apr","May","Jun","Jul","Aug","Sep","Oct","Nov","Dec"))


def test_format_message_time_empty_returns_empty():
    assert _format_message_time("") == ""


def test_format_message_time_unparseable_returns_raw():
    assert _format_message_time("not-a-timestamp") == "not-a-timestamp"
