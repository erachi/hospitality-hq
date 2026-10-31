"""Tests for pre-booking inquiry support.

Inquiries are a separate Hospitable resource from reservations. Their
messages carry `reservation_id: null` and the inquiry UUID in
`conversation_id`, so before this support existed the webhook dropped them
as "missing ids" and the poller (reservations only) never saw them.

Fixture shapes mirror a real inquiry pulled from the Hospitable API.
"""

import json
from datetime import datetime, timezone
from unittest.mock import patch, MagicMock

import responses
from moto import mock_aws

from classifier import draft_response
from handler import lambda_handler, inquiry_guest_name, inquiry_messages
from hospitable_client import HospitableClient
from slack_notifier import post_guest_alert
from state_tracker import StateTracker, message_dedup_key
from webhook_handler import webhook_handler

INQ_ID = "inq-uuid-001"
PROP_ID = "test-prop-uuid-1"
GUEST_MSG_BODY = "I know the listing says no pets — could we bring 2 golden retrievers?"


def _guest_msg(created_at="2026-10-31T21:35:19Z", body=GUEST_MSG_BODY):
    return {
        "id": 1316026731,
        "conversation_id": INQ_ID,
        "reservation_id": None,
        "body": body,
        "sender_type": "guest",
        "sender_role": None,
        "source": "platform",
        "created_at": created_at,
    }


def _host_msg(created_at="2026-10-31T21:40:16Z"):
    return {
        "id": 1316028184,
        "conversation_id": INQ_ID,
        "reservation_id": None,
        "body": "Unfortunately we aren't able to allow pets.",
        "sender_type": "host",
        "sender_role": "host",
        "source": "AI",
        "created_at": created_at,
    }


def _inquiry_detail(messages):
    return {
        "id": INQ_ID,
        "platform": "airbnb",
        "arrival_date": "2026-11-23",
        "departure_date": "2026-11-29",
        "guests": {"total": 9, "adult_count": 9},
        "guest": {"first_name": "Reagan", "last_name": None},
        # Single object on inquiries — not an array as on reservations.
        "properties": {"id": PROP_ID, "name": "Newly Refreshed! Pool & Fire Pit"},
        # The API nests messages newest-first.
        "messages": list(reversed(messages)),
    }


def _mock_client_for_inquiry(mock_hospitable, messages):
    client = MagicMock()
    mock_hospitable.return_value = client
    client.get_inquiry_detail.return_value = _inquiry_detail(messages)
    client.get_property.return_value = {"description": "A beautiful property"}
    client.get_property_knowledge_hub.return_value = {"topics": []}
    return client


_CLASSIFICATION = {
    "category": "GENERAL",
    "urgency": "LOW",
    "summary": "Asks to bring two dogs",
    "descriptor": "Pet policy question",
}


# ─── Helpers ──────────────────────────────────────────────────────────────


def test_inquiry_guest_name_joins_available_parts():
    assert inquiry_guest_name({"guest": {"first_name": "Reagan", "last_name": None}}) == "Reagan"
    assert inquiry_guest_name({"guest": {"first_name": "Reagan", "last_name": "Lee"}}) == "Reagan Lee"
    assert inquiry_guest_name({}) == "Guest"


def test_inquiry_messages_are_sorted_oldest_first():
    detail = _inquiry_detail([_guest_msg(), _host_msg()])
    ordered = inquiry_messages(detail)
    assert [m["sender_type"] for m in ordered] == ["guest", "host"]


# ─── Webhook path ─────────────────────────────────────────────────────────


@mock_aws
@patch("webhook_handler.get_webhook_secret", return_value="")
@patch("webhook_handler.HospitableClient")
@patch("webhook_handler.classify_message", return_value=dict(_CLASSIFICATION))
@patch("webhook_handler.draft_response", return_value="Hi Reagan! Sadly no pets.")
@patch("webhook_handler.summarize_conversation", return_value="")
@patch("webhook_handler.post_guest_alert", return_value={"ok": True})
def test_webhook_processes_inquiry_message(
    mock_slack, mock_summary, mock_draft, mock_classify, mock_hospitable, mock_secret,
    dynamodb_table,
):
    """Regression: inquiry messages (reservation_id null) used to be dropped."""
    client = _mock_client_for_inquiry(mock_hospitable, [_guest_msg()])

    payload = {"id": "evt-1", "action": "message.created", "data": _guest_msg()}
    result = webhook_handler(
        {"body": json.dumps(payload), "isBase64Encoded": False, "headers": {}}, None
    )

    assert json.loads(result["body"])["status"] == "processed"
    client.get_inquiry_detail.assert_called_once_with(INQ_ID)
    client.get_reservation_detail.assert_not_called()

    slack_kwargs = mock_slack.call_args.kwargs
    assert slack_kwargs["is_inquiry"] is True
    assert slack_kwargs["guest_name"] == "Reagan"
    assert slack_kwargs["checkin_date"] == "2026-11-23"
    assert slack_kwargs["checkout_date"] == "2026-11-29"
    assert slack_kwargs["reservation_uuid"] == INQ_ID

    draft_kwargs = mock_draft.call_args.kwargs
    assert draft_kwargs["is_inquiry"] is True
    assert draft_kwargs["party_size"] == 9

    # Marked under the inquiry id so the poller won't re-alert it.
    assert StateTracker().is_message_processed(INQ_ID, message_dedup_key(INQ_ID, _guest_msg()))


@mock_aws
@patch("webhook_handler.get_webhook_secret", return_value="")
def test_webhook_still_ignores_message_with_no_ids(mock_secret, dynamodb_table):
    data = _guest_msg()
    data["conversation_id"] = None
    payload = {"id": "evt-2", "action": "message.created", "data": data}
    result = webhook_handler(
        {"body": json.dumps(payload), "isBase64Encoded": False, "headers": {}}, None
    )
    assert json.loads(result["body"])["reason"] == "missing ids"


# ─── Poller path ──────────────────────────────────────────────────────────


@mock_aws
@patch("handler.HospitableClient")
@patch("handler.classify_message", return_value=dict(_CLASSIFICATION))
@patch("handler.draft_response", return_value="Hi Reagan! Sadly no pets.")
@patch("handler.summarize_conversation", return_value="")
@patch("handler.post_guest_alert", return_value={"ok": True})
def test_poller_alerts_unanswered_inquiry(
    mock_slack, mock_summary, mock_draft, mock_classify, mock_hospitable, dynamodb_table
):
    client = _mock_client_for_inquiry(mock_hospitable, [_guest_msg()])
    client.get_active_reservations.return_value = []
    client.get_recent_inquiries.return_value = [{"id": INQ_ID, "property_id": PROP_ID}]

    body = json.loads(lambda_handler({}, None)["body"])

    assert body["inquiries_checked"] == 1
    assert body["alerts_posted"] == 1
    slack_kwargs = mock_slack.call_args.kwargs
    assert slack_kwargs["is_inquiry"] is True
    assert slack_kwargs["reservation_status"] == "inquiry"
    assert mock_draft.call_args.kwargs["is_inquiry"] is True


@mock_aws
@patch("handler.HospitableClient")
@patch("handler.post_guest_alert")
def test_poller_skips_inquiry_already_answered(mock_slack, mock_hospitable, dynamodb_table):
    """A host reply after the guest message (even Hospitable AI-sent) counts as answered."""
    client = _mock_client_for_inquiry(mock_hospitable, [_guest_msg(), _host_msg()])
    client.get_active_reservations.return_value = []
    client.get_recent_inquiries.return_value = [{"id": INQ_ID, "property_id": PROP_ID}]

    body = json.loads(lambda_handler({}, None)["body"])

    assert body["alerts_posted"] == 0
    mock_slack.assert_not_called()


@mock_aws
@patch("handler.HospitableClient")
@patch("handler.classify_message", return_value=dict(_CLASSIFICATION))
@patch("handler.draft_response", return_value="draft")
@patch("handler.summarize_conversation", return_value="")
@patch("handler.post_guest_alert", return_value={"ok": True})
def test_inquiry_fetch_failure_does_not_block_reservation_alerts(
    mock_slack, mock_summary, mock_draft, mock_classify, mock_hospitable, dynamodb_table
):
    client = MagicMock()
    mock_hospitable.return_value = client
    client.get_active_reservations.return_value = [
        {"id": "res-1", "property_id": PROP_ID, "guest": {"full_name": "Jane"}}
    ]
    client.get_reservation_messages.return_value = [
        {"body": "AC broken", "sender_type": "guest", "created_at": "2026-04-20T22:00:00Z"}
    ]
    client.get_recent_inquiries.side_effect = RuntimeError("Hospitable 500")

    body = json.loads(lambda_handler({}, None)["body"])

    assert body["alerts_posted"] == 1
    assert any("inquiries" in e for e in body["errors"])


@mock_aws
@patch("handler.HospitableClient")
@patch("handler.classify_message", return_value=dict(_CLASSIFICATION))
@patch("handler.draft_response", return_value="draft")
@patch("handler.summarize_conversation", return_value="")
@patch("handler.post_guest_alert", return_value={"ok": True})
@patch("webhook_handler.get_webhook_secret", return_value="")
@patch("webhook_handler.HospitableClient")
@patch("webhook_handler.classify_message", return_value=dict(_CLASSIFICATION))
@patch("webhook_handler.draft_response", return_value="draft")
@patch("webhook_handler.summarize_conversation", return_value="")
@patch("webhook_handler.post_guest_alert", return_value={"ok": True})
def test_inquiry_webhook_then_poller_posts_exactly_one_alert(
    wh_slack, wh_summary, wh_draft, wh_classify, wh_hospitable, wh_secret,
    p_slack, p_summary, p_draft, p_classify, p_hospitable,
    dynamodb_table,
):
    _mock_client_for_inquiry(wh_hospitable, [_guest_msg()])
    poll_client = _mock_client_for_inquiry(p_hospitable, [_guest_msg()])
    poll_client.get_active_reservations.return_value = []
    poll_client.get_recent_inquiries.return_value = [{"id": INQ_ID, "property_id": PROP_ID}]

    payload = {"id": "evt-1", "action": "message.created", "data": _guest_msg()}
    webhook_handler({"body": json.dumps(payload), "isBase64Encoded": False, "headers": {}}, None)
    lambda_handler({}, None)

    assert wh_slack.call_count == 1
    assert p_slack.call_count == 0


# ─── Hospitable client ────────────────────────────────────────────────────


@responses.activate
@patch("hospitable_client.get_hospitable_token", return_value="test-token")
def test_get_recent_inquiries_filters_by_activity_and_attaches_property(mock_token):
    responses.add(
        responses.GET,
        "https://public.api.hospitable.com/v2/inquiries",
        json={"data": [{"id": INQ_ID}], "meta": {"current_page": 1, "last_page": 1}},
    )

    since = datetime(2026, 10, 31, 15, 35, 0, tzinfo=timezone.utc)
    results = HospitableClient().get_recent_inquiries([PROP_ID], since)

    assert results == [{"id": INQ_ID, "property_id": PROP_ID}]
    params = responses.calls[0].request.params
    assert params["properties[]"] == PROP_ID
    assert params["last_message_at"] == "2026-10-31 15:35:00"


@responses.activate
@patch("hospitable_client.get_hospitable_token", return_value="test-token")
def test_get_inquiry_detail_includes_messages(mock_token):
    responses.add(
        responses.GET,
        f"https://public.api.hospitable.com/v2/inquiries/{INQ_ID}",
        json={"data": {"id": INQ_ID, "messages": []}},
    )

    detail = HospitableClient().get_inquiry_detail(INQ_ID)

    assert detail["id"] == INQ_ID
    assert "messages" in responses.calls[0].request.params["include"]


# ─── Slack alert + draft prompt ───────────────────────────────────────────


@responses.activate
@patch("slack_notifier.get_slack_bot_token", return_value="xoxb-test-token")
def test_alert_marks_inquiry_with_requested_dates(mock_token):
    responses.add(
        responses.POST, "https://slack.com/api/chat.postMessage", json={"ok": True, "ts": "1.2"}
    )

    post_guest_alert(
        guest_name="Reagan",
        property_name="Villa Bougainvillea",
        checkin_date="2026-11-23",
        checkout_date="2026-11-29",
        guest_message=GUEST_MSG_BODY,
        classification=dict(_CLASSIFICATION),
        draft_response="Hi Reagan!",
        reservation_uuid=INQ_ID,
        is_inquiry=True,
    )

    payload = json.loads(responses.calls[0].request.body)
    prefix = payload["blocks"][0]["elements"][0]["text"]
    assert "Inquiry — not booked" in prefix
    assert "2026-11-23 → 2026-11-29" in prefix
    assert payload["text"].startswith("🟢 Inquiry")


@patch("classifier._get_client")
def test_inquiry_draft_prompt_withholds_confirmed_guest_details(mock_client):
    mock_client.return_value.messages.create.return_value = MagicMock(
        content=[MagicMock(text="Hi Reagan!")]
    )

    draft_response(
        message_text=GUEST_MSG_BODY,
        property_name="Villa Bougainvillea",
        property_description="",
        knowledge_hub_context="",
        guest_name="Reagan",
        checkin_date="2026-11-23",
        checkout_date="2026-11-29",
        classification=dict(_CLASSIFICATION),
        is_inquiry=True,
        party_size=9,
    )

    content = mock_client.return_value.messages.create.call_args.kwargs["messages"][0]["content"]
    assert "INQUIRY — not booked yet" in content
    assert "Party size: 9" in content
    assert "door codes" in content


@patch("classifier._get_client")
def test_reservation_draft_prompt_has_no_inquiry_section(mock_client):
    mock_client.return_value.messages.create.return_value = MagicMock(
        content=[MagicMock(text="Hi Jane!")]
    )

    draft_response(
        message_text="AC broken",
        property_name="Villa Bougainvillea",
        property_description="",
        knowledge_hub_context="",
        guest_name="Jane",
        checkin_date="2026-04-20",
        checkout_date="2026-04-25",
        classification=dict(_CLASSIFICATION),
    )

    content = mock_client.return_value.messages.create.call_args.kwargs["messages"][0]["content"]
    assert "INQUIRY" not in content


# ─── Thread Q&A ───────────────────────────────────────────────────────────


@mock_aws
@patch("thread_handler.qa_answer", return_value="No pets, sorry.")
@patch("thread_handler.post_thread_reply")
@patch("thread_handler.HospitableClient")
def test_thread_question_on_inquiry_alert_reads_inquiry(
    mock_hospitable, mock_post, mock_qa, all_thread_tables
):
    from thread_handler import _handle_question

    client = _mock_client_for_inquiry(mock_hospitable, [_guest_msg()])

    _handle_question(
        question="how many guests?",
        thread_ts="1.3",
        channel="C_TEST_CHANNEL",
        mapping={
            "reservation_uuid": INQ_ID,
            "property_id": PROP_ID,
            "property_name": "Villa Bougainvillea",
            "guest_name": "Reagan",
            "conversation_type": "inquiry",
        },
    )

    client.get_inquiry_detail.assert_called_once_with(INQ_ID)
    client.get_reservation_detail.assert_not_called()
    qa_kwargs = mock_qa.call_args.kwargs
    assert "not booked yet" in qa_kwargs["reservation_summary"]
    assert "golden retrievers" in qa_kwargs["conversation_history"]
