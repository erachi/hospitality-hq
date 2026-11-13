"""Tests for stay_provisioner — channel-name slug, provisioner, mirror."""

from unittest.mock import MagicMock, patch

import pytest
from moto import mock_aws

import stay_provisioner
from stay_channels import StayChannels
from stay_models import (
    SOURCE_HOSPITABLE_GUEST,
    SOURCE_HOSPITABLE_HOST,
    SOURCE_SYSTEM,
    STATUS_ACTIVE,
)
from stay_provisioner import (
    _slugify,
    _source_from_sender,
    build_stay_channel_name,
    mirror_message,
    provision_if_missing,
)
from stay_store import StayStore

# ─── Slug helpers ──────────────────────────────────────────────────────


def test_slugify_strips_and_collapses():
    assert _slugify("  Hello  World  ") == "hello-world"
    assert _slugify("a--b__c") == "a-b-c"
    assert _slugify("") == ""
    assert _slugify("---") == ""


def test_slugify_drops_non_ascii():
    # Non-ASCII collapses to empty string per plan.
    assert _slugify("李 伟") == ""


def test_build_channel_name_happy_path():
    name = build_stay_channel_name("palm", "Jane Smith", "2026-11-20")
    assert name == "stay-palm-smith-20261120"


def test_build_channel_name_missing_lastname_uses_fallback():
    assert build_stay_channel_name("palm", "", "2026-01-01") == "stay-palm-guest-20260101"
    assert build_stay_channel_name("palm", "Jane", "2026-01-01") == "stay-palm-jane-20260101"


def test_build_channel_name_non_ascii_guest_falls_back_to_guest():
    assert build_stay_channel_name("villa", "李 伟", "2026-12-05") == "stay-villa-guest-20261205"


def test_build_channel_name_strips_time_from_date():
    assert (
        build_stay_channel_name("palm", "Jane Smith", "2026-11-20T00:00:00Z")
        == "stay-palm-smith-20261120"
    )


def test_build_channel_name_handles_bad_date():
    assert build_stay_channel_name("palm", "Jane Smith", "") == "stay-palm-smith-nodate"


def test_build_channel_name_truncates_to_80_chars():
    name = build_stay_channel_name(
        "palm" * 30, "A very long guest name family-of-many", "2026-11-20"
    )
    assert len(name) <= 80


def test_build_channel_name_appends_collision_suffix():
    name_1 = build_stay_channel_name("palm", "Jane Smith", "2026-11-20", suffix=1)
    name_2 = build_stay_channel_name("palm", "Jane Smith", "2026-11-20", suffix=2)
    name_5 = build_stay_channel_name("palm", "Jane Smith", "2026-11-20", suffix=5)
    assert name_1 == "stay-palm-smith-20261120"
    assert name_2 == "stay-palm-smith-20261120-2"
    assert name_5 == "stay-palm-smith-20261120-5"


def test_source_from_sender_maps_correctly():
    assert _source_from_sender("guest") == SOURCE_HOSPITABLE_GUEST
    assert _source_from_sender("GUEST") == SOURCE_HOSPITABLE_GUEST
    assert _source_from_sender("host") == SOURCE_HOSPITABLE_HOST
    # Anything unknown defaults to host (the safer fallback since
    # agent context won't confuse it with a guest ask).
    assert _source_from_sender("") == SOURCE_HOSPITABLE_HOST
    assert _source_from_sender("system") == SOURCE_HOSPITABLE_HOST


# ─── Fixtures for the provisioner + mirror ─────────────────────────────


@pytest.fixture
def flag_on(monkeypatch):
    """Turn on the stay-channels feature flag for the provisioner module.

    Config resolves the flag at import time, so patching os.environ is
    too late — patch the module attribute directly.
    """
    monkeypatch.setattr(stay_provisioner, "STAY_CHANNELS_ENABLED", True)


@pytest.fixture
def flag_off(monkeypatch):
    monkeypatch.setattr(stay_provisioner, "STAY_CHANNELS_ENABLED", False)


@pytest.fixture
def no_invite_list(monkeypatch):
    """Avoid an extra Slack `conversations.invite` call in tests that
    don't care about it."""
    monkeypatch.setattr(stay_provisioner, "STAY_CHANNEL_INVITE_USER_IDS", [])


def _make_slack_client(create_responses=None, post_responses=None):
    """Mock Slack client with configurable sequences."""
    client = MagicMock()

    if create_responses is None:
        create_responses = [
            {"ok": True, "channel": {"id": "C_STAY_123", "name": "stay-palm-smith-20261120"}}
        ]
    client.create_private_channel.side_effect = create_responses

    if post_responses is None:
        # Any number of post_message calls return the same thing.
        client.post_message.return_value = {"ok": True, "ts": "1700000000.100"}
    else:
        client.post_message.side_effect = post_responses

    client.invite_users.return_value = {"ok": True}
    client.pin_message.return_value = {"ok": True}
    return client


def _make_hospitable_client(messages=None):
    client = MagicMock()
    client.get_reservation_messages.return_value = messages or []
    return client


def _reservation() -> dict:
    return {
        "id": "res-abc",
        "property_id": "prop-palm",
        "check_in": "2026-11-20",
        "check_out": "2026-11-25",
        "platform": "airbnb",
        "guest": {"full_name": "Jane Smith", "first_name": "Jane"},
    }


# ─── Provisioner — happy path + variants ────────────────────────────────


@mock_aws
def test_provisioner_happy_path(flag_on, no_invite_list, stay_channels_table, stay_journal_bucket):
    """Full flow: journal seeded, channel created, intro posted,
    transcript backfilled, mapping committed, row returned."""
    messages = [
        {
            "id": "m1",
            "body": "Hi, early check-in possible?",
            "sender_type": "guest",
            "source": "platform",
            "created_at": "2026-11-20T15:00:00Z",
        },
        {
            "id": "m2",
            "body": "Of course, see you at noon.",
            "sender_type": "host",
            "source": "manual",
            "created_at": "2026-11-20T15:05:00Z",
        },
    ]
    slack = _make_slack_client()
    hospitable = _make_hospitable_client(messages)

    row = provision_if_missing(
        reservation_uuid="res-abc",
        property_id="prop-palm",
        property_slug="palm",
        property_name="The Palm Club",
        guest_name="Jane Smith",
        check_in="2026-11-20",
        check_out="2026-11-25",
        reservation_snapshot=_reservation(),
        hospitable_client=hospitable,
        slack_client=slack,
    )

    assert row is not None
    assert row["channel_id"] == "C_STAY_123"
    assert row["status"] == STATUS_ACTIVE
    assert row["last_mirrored_hospitable_ts"] == "2026-11-20T15:05:00Z"

    # Journal exists in S3 with the reservation snapshot + system entry
    # + both Hospitable messages, in order.
    journal = StayStore().get("res-abc")
    assert journal is not None
    assert journal.reservation_snapshot["id"] == "res-abc"
    sources = [e.source for e in journal.entries]
    assert sources == [
        SOURCE_SYSTEM,
        SOURCE_HOSPITABLE_GUEST,
        SOURCE_HOSPITABLE_HOST,
    ]

    # Slack calls: 1 create + 1 intro + 2 backfill messages
    slack.create_private_channel.assert_called_once_with("stay-palm-smith-20261120")
    assert slack.post_message.call_count >= 3  # intro + 2 backfills


@mock_aws
def test_provisioner_is_idempotent(
    flag_on, no_invite_list, stay_channels_table, stay_journal_bucket
):
    """Second call short-circuits when a row already exists."""
    slack = _make_slack_client()
    hospitable = _make_hospitable_client()

    kwargs = dict(
        reservation_uuid="res-abc",
        property_id="prop-palm",
        property_slug="palm",
        property_name="The Palm Club",
        guest_name="Jane Smith",
        check_in="2026-11-20",
        check_out="2026-11-25",
        reservation_snapshot=_reservation(),
        hospitable_client=hospitable,
        slack_client=slack,
    )

    first = provision_if_missing(**kwargs)
    assert first is not None

    slack.reset_mock()
    hospitable.reset_mock()

    second = provision_if_missing(**kwargs)
    assert second is not None
    assert second["channel_id"] == first["channel_id"]

    # Second call makes zero Slack / Hospitable calls.
    slack.create_private_channel.assert_not_called()
    slack.post_message.assert_not_called()
    hospitable.get_reservation_messages.assert_not_called()


@mock_aws
def test_provisioner_feature_flag_off_is_noop(flag_off, stay_channels_table, stay_journal_bucket):
    slack = _make_slack_client()
    hospitable = _make_hospitable_client()

    result = provision_if_missing(
        reservation_uuid="res-abc",
        property_id="prop-palm",
        property_slug="palm",
        property_name="The Palm Club",
        guest_name="Jane Smith",
        check_in="2026-11-20",
        check_out="2026-11-25",
        reservation_snapshot=_reservation(),
        hospitable_client=hospitable,
        slack_client=slack,
    )
    assert result is None
    slack.create_private_channel.assert_not_called()
    hospitable.get_reservation_messages.assert_not_called()
    # And no journal row either.
    assert StayStore().get("res-abc") is None


@mock_aws
def test_provisioner_retries_on_name_taken(
    flag_on, no_invite_list, stay_channels_table, stay_journal_bucket
):
    """First two create attempts return name_taken; third succeeds."""
    slack = _make_slack_client(
        create_responses=[
            {"ok": False, "error": "name_taken"},
            {"ok": False, "error": "name_taken"},
            {"ok": True, "channel": {"id": "C_X", "name": "stay-palm-smith-20261120-3"}},
        ]
    )
    hospitable = _make_hospitable_client()

    row = provision_if_missing(
        reservation_uuid="res-abc",
        property_id="prop-palm",
        property_slug="palm",
        property_name="The Palm Club",
        guest_name="Jane Smith",
        check_in="2026-11-20",
        check_out="2026-11-25",
        reservation_snapshot=_reservation(),
        hospitable_client=hospitable,
        slack_client=slack,
    )

    assert row is not None
    assert row["channel_name"] == "stay-palm-smith-20261120-3"

    # Verify the sequence of names tried.
    attempted = [c.args[0] for c in slack.create_private_channel.call_args_list]
    assert attempted == [
        "stay-palm-smith-20261120",
        "stay-palm-smith-20261120-2",
        "stay-palm-smith-20261120-3",
    ]


@mock_aws
def test_provisioner_aborts_on_missing_scope(
    flag_on, no_invite_list, stay_channels_table, stay_journal_bucket
):
    """Missing Slack scope aborts cleanly with the journal still written."""
    slack = _make_slack_client(create_responses=[{"ok": False, "error": "missing_scope"}])
    hospitable = _make_hospitable_client()

    result = provision_if_missing(
        reservation_uuid="res-abc",
        property_id="prop-palm",
        property_slug="palm",
        property_name="The Palm Club",
        guest_name="Jane Smith",
        check_in="2026-11-20",
        check_out="2026-11-25",
        reservation_snapshot=_reservation(),
        hospitable_client=hospitable,
        slack_client=slack,
    )
    assert result is None
    # No retry burst — we bail immediately.
    slack.create_private_channel.assert_called_once()
    # Journal was still written (so the next poll can retry once scope fixed).
    assert StayStore().get("res-abc") is not None
    # No mapping row committed.
    assert StayChannels().get_by_reservation("res-abc") is None


# ─── Mirror ─────────────────────────────────────────────────────────────


def _seed_mapping(check_in="2026-11-20", channel_id="C_STAY_1"):
    """Helper: insert a stay-channel row by hand for mirror tests."""
    sc = StayChannels()
    sc.put(
        reservation_uuid="res-abc",
        channel_id=channel_id,
        channel_name="stay-palm-smith-20261120",
        property_id="prop-palm",
        property_name="The Palm Club",
        guest_name="Jane Smith",
        check_in=check_in,
        check_out="2026-11-25",
        journal_s3_key="stays/res-abc.json",
    )
    # Seed a minimal journal — append_entry needs it to exist.
    from stay_models import StayJournal

    store = StayStore()
    store.put(
        StayJournal.new(
            reservation_uuid="res-abc",
            property_id="prop-palm",
            property_name="The Palm Club",
            guest_name="Jane Smith",
            check_in=check_in,
            check_out="2026-11-25",
        )
    )


@mock_aws
def test_mirror_happy_path(flag_on, stay_channels_table, stay_journal_bucket):
    _seed_mapping()
    slack = _make_slack_client()
    msg = {
        "id": "m-new",
        "body": "Any late checkout?",
        "sender_type": "guest",
        "created_at": "2026-11-23T16:00:00Z",
    }

    with patch("stay_agent.handle_mirrored_guest_message") as mock_dispatch:
        posted_ts = mirror_message(
            reservation_uuid="res-abc",
            message=msg,
            slack_client=slack,
            guest_name="Jane Smith",
        )

    assert posted_ts == "1700000000.100"
    slack.post_message.assert_called_once()
    # Journal grew.
    journal = StayStore().get("res-abc")
    assert len(journal.entries) == 1
    assert journal.entries[0].source == SOURCE_HOSPITABLE_GUEST
    assert journal.entries[0].body == "Any late checkout?"

    # High-water mark advanced.
    row = StayChannels().get_by_reservation("res-abc")
    assert row["last_mirrored_hospitable_ts"] == "2026-11-23T16:00:00Z"

    # Guest mirror fired the auto-draft dispatcher with the fresh row.
    mock_dispatch.assert_called_once()
    dispatch_kwargs = mock_dispatch.call_args.kwargs
    assert dispatch_kwargs["reservation_uuid"] == "res-abc"
    assert dispatch_kwargs["message"] is msg
    assert dispatch_kwargs["stay_row"]["channel_id"] == "C_STAY_1"


@mock_aws
def test_mirror_host_message_does_not_trigger_auto_draft(
    flag_on, stay_channels_table, stay_journal_bucket
):
    """Host-sent mirrors (e.g. VJ replied in Hospitable directly) must
    not auto-draft — there's nothing to reply to."""
    _seed_mapping()
    slack = _make_slack_client()
    msg = {
        "id": "m-host",
        "body": "See you at 3.",
        "sender_type": "host",
        "created_at": "2026-11-23T16:00:00Z",
    }
    with patch("stay_agent.handle_mirrored_guest_message") as mock_dispatch:
        mirror_message(
            reservation_uuid="res-abc",
            message=msg,
            slack_client=slack,
        )
    mock_dispatch.assert_not_called()


@mock_aws
def test_mirror_dedup_skips_auto_draft(flag_on, stay_channels_table, stay_journal_bucket):
    """A dedup'd mirror returns before posting → dispatcher must not fire."""
    _seed_mapping()
    StayChannels().update_last_mirrored("res-abc", "2026-11-23T16:00:00Z")
    slack = _make_slack_client()
    msg = {
        "id": "m-dupe",
        "body": "already mirrored",
        "sender_type": "guest",
        "created_at": "2026-11-23T16:00:00Z",
    }
    with patch("stay_agent.handle_mirrored_guest_message") as mock_dispatch:
        result = mirror_message(
            reservation_uuid="res-abc",
            message=msg,
            slack_client=slack,
        )
    assert result is None
    mock_dispatch.assert_not_called()


@mock_aws
def test_mirror_auto_draft_failure_does_not_fail_mirror(
    flag_on, stay_channels_table, stay_journal_bucket
):
    """If the auto-draft handler blows up, the mirror still succeeds —
    the guest message is journaled and posted regardless."""
    _seed_mapping()
    slack = _make_slack_client()
    msg = {
        "id": "m-boom",
        "body": "fire alarm went off",
        "sender_type": "guest",
        "created_at": "2026-11-23T16:00:00Z",
    }
    with patch(
        "stay_agent.handle_mirrored_guest_message",
        side_effect=RuntimeError("Claude timeout"),
    ):
        posted_ts = mirror_message(
            reservation_uuid="res-abc",
            message=msg,
            slack_client=slack,
        )
    assert posted_ts == "1700000000.100"
    # Journal still grew.
    assert len(StayStore().get("res-abc").entries) == 1


@mock_aws
def test_mirror_dedups_on_high_water_mark(flag_on, stay_channels_table, stay_journal_bucket):
    """A message whose created_at is <= last_mirrored is a no-op."""
    _seed_mapping()
    sc = StayChannels()
    sc.update_last_mirrored("res-abc", "2026-11-23T16:00:00Z")

    slack = _make_slack_client()
    msg = {
        "id": "m-dupe",
        "body": "already mirrored",
        "sender_type": "guest",
        "created_at": "2026-11-23T16:00:00Z",
    }

    result = mirror_message(
        reservation_uuid="res-abc",
        message=msg,
        slack_client=slack,
    )
    assert result is None
    slack.post_message.assert_not_called()
    assert StayStore().get("res-abc").entries == []  # no append


@mock_aws
def test_mirror_without_mapping_is_noop(flag_on, stay_channels_table, stay_journal_bucket):
    """No mapping row → mirror silently returns (next poll provisions)."""
    slack = _make_slack_client()
    result = mirror_message(
        reservation_uuid="res-nomap",
        message={
            "id": "m",
            "body": "hi",
            "sender_type": "guest",
            "created_at": "2026-11-23T16:00:00Z",
        },
        slack_client=slack,
    )
    assert result is None
    slack.post_message.assert_not_called()


@mock_aws
def test_mirror_feature_flag_off_is_noop(flag_off, stay_channels_table, stay_journal_bucket):
    _seed_mapping()
    slack = _make_slack_client()
    result = mirror_message(
        reservation_uuid="res-abc",
        message={
            "id": "m",
            "body": "hi",
            "sender_type": "guest",
            "created_at": "2026-11-23T16:00:00Z",
        },
        slack_client=slack,
    )
    assert result is None
    slack.post_message.assert_not_called()


# ─── #guest-alerts footer in _build_blocks ──────────────────────────────


def test_build_blocks_adds_stay_channel_footer_when_id_present():
    from slack_notifier import _build_blocks

    blocks = _build_blocks(
        guest_name="Jane",
        property_name="Palm",
        checkin_date="2026-11-20",
        checkout_date="2026-11-25",
        guest_message="hi",
        classification={"urgency": "LOW", "category": "GENERAL"},
        draft_response="hi back",
        reservation_uuid="res-abc",
        booking_source="airbnb",
        reservation_status="accepted",
        is_repeat_guest=False,
        recent_messages=[],
        conversation_summary="",
        message_timestamp="2026-11-20T15:00:00Z",
        stay_channel_id="C_STAY_123",
    )
    # Footer is a context block whose text references the channel link.
    last = blocks[-1]
    assert last["type"] == "context"
    text = last["elements"][0]["text"]
    assert "Open stay channel" in text
    assert "<#C_STAY_123>" in text


def test_build_blocks_omits_stay_channel_footer_when_id_absent():
    from slack_notifier import _build_blocks

    blocks = _build_blocks(
        guest_name="Jane",
        property_name="Palm",
        checkin_date="2026-11-20",
        checkout_date="2026-11-25",
        guest_message="hi",
        classification={"urgency": "LOW", "category": "GENERAL"},
        draft_response="hi back",
        reservation_uuid="res-abc",
        booking_source="airbnb",
        reservation_status="accepted",
        is_repeat_guest=False,
        recent_messages=[],
        conversation_summary="",
        message_timestamp="2026-11-20T15:00:00Z",
    )
    for b in blocks:
        if b.get("type") != "context":
            continue
        text = b["elements"][0]["text"]
        assert "Open stay channel" not in text
