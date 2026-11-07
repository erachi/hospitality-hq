"""Tests for the daily stay-channel archiver."""

from datetime import date, timedelta
from unittest.mock import patch

import pytest
from moto import mock_aws

import stay_archiver
from stay_archiver import lambda_handler
from stay_channels import StayChannels
from stay_models import SOURCE_SYSTEM, STATUS_ACTIVE, STATUS_ARCHIVED, StayJournal
from stay_store import StayStore


@pytest.fixture
def flag_on(monkeypatch):
    monkeypatch.setattr(stay_archiver, "STAY_CHANNELS_ENABLED", True, raising=False)


@pytest.fixture
def flag_off(monkeypatch):
    monkeypatch.setattr(stay_archiver, "STAY_CHANNELS_ENABLED", False, raising=False)


def _seed_stay(reservation_uuid: str, *, check_out: str, channel_id: str) -> None:
    sc = StayChannels()
    sc.put(
        reservation_uuid=reservation_uuid,
        channel_id=channel_id,
        channel_name=f"stay-{reservation_uuid}",
        property_id="prop-palm",
        property_name="The Palm Club",
        guest_name="Jane",
        check_in="2026-10-01",
        check_out=check_out,
        journal_s3_key=f"stays/{reservation_uuid}.json",
    )
    store = StayStore()
    store.put(
        StayJournal.new(
            reservation_uuid=reservation_uuid,
            property_id="prop-palm",
            property_name="The Palm Club",
            guest_name="Jane",
            check_in="2026-10-01",
            check_out=check_out,
        )
    )


@mock_aws
def test_archiver_feature_flag_off_is_noop(
    flag_off, stay_channels_table, stay_journal_bucket
):
    _seed_stay("res-old", check_out="2026-01-01", channel_id="C_OLD")
    result = lambda_handler({}, None)
    assert result["statusCode"] == 200

    row = StayChannels().get_by_reservation("res-old")
    # Still active — archiver did nothing.
    assert row["status"] == STATUS_ACTIVE


@mock_aws
def test_archiver_archives_stays_past_grace_window(
    flag_on, stay_channels_table, stay_journal_bucket
):
    # One stay well past checkout, one still inside the grace window.
    yesterday = (date.today() - timedelta(days=1)).isoformat()
    long_ago = (date.today() - timedelta(days=30)).isoformat()
    _seed_stay("res-old", check_out=long_ago, channel_id="C_OLD")
    _seed_stay("res-recent", check_out=yesterday, channel_id="C_RECENT")

    with patch(
        "stay_archiver._slack_archive", return_value={"ok": True}
    ) as mock_archive, patch(
        "stay_archiver.StayChannelSlackClient"
    ) as mock_slack_cls:
        mock_slack_cls.return_value.post_message.return_value = {"ok": True}
        result = lambda_handler({}, None)

    import json as _json

    body = _json.loads(result["body"])
    assert body["candidates"] == 1
    assert body["archived"] == 1
    mock_archive.assert_called_once_with("C_OLD")

    # Row flipped to archived.
    old = StayChannels().get_by_reservation("res-old")
    assert old["status"] == STATUS_ARCHIVED
    assert "archived_at" in old

    # Recent stay untouched.
    recent = StayChannels().get_by_reservation("res-recent")
    assert recent["status"] == STATUS_ACTIVE

    # Journal got the system entry.
    old_journal = StayStore().get("res-old")
    assert any(
        e.source == SOURCE_SYSTEM and e.metadata.get("action") == "archived"
        for e in old_journal.entries
    )


@mock_aws
def test_archiver_treats_already_archived_as_success(
    flag_on, stay_channels_table, stay_journal_bucket
):
    """A Slack `already_archived` error must still flip the row —
    otherwise the archiver loops on the same stay forever."""
    long_ago = (date.today() - timedelta(days=30)).isoformat()
    _seed_stay("res-again", check_out=long_ago, channel_id="C_AGAIN")

    with patch(
        "stay_archiver._slack_archive",
        return_value={"ok": False, "error": "already_archived"},
    ), patch("stay_archiver.StayChannelSlackClient") as mock_slack_cls:
        mock_slack_cls.return_value.post_message.return_value = {"ok": True}
        lambda_handler({}, None)

    row = StayChannels().get_by_reservation("res-again")
    assert row["status"] == STATUS_ARCHIVED


@mock_aws
def test_archiver_records_errors_and_continues(
    flag_on, stay_channels_table, stay_journal_bucket
):
    long_ago = (date.today() - timedelta(days=30)).isoformat()
    _seed_stay("res-ok", check_out=long_ago, channel_id="C_OK")
    _seed_stay("res-bad", check_out=long_ago, channel_id="C_BAD")

    def archive_side_effect(channel_id):
        if channel_id == "C_BAD":
            return {"ok": False, "error": "channel_not_found"}
        return {"ok": True}

    with patch(
        "stay_archiver._slack_archive", side_effect=archive_side_effect
    ), patch("stay_archiver.StayChannelSlackClient") as mock_slack_cls:
        mock_slack_cls.return_value.post_message.return_value = {"ok": True}
        result = lambda_handler({}, None)

    import json as _json

    body = _json.loads(result["body"])
    assert body["candidates"] == 2
    assert body["archived"] == 1  # C_OK succeeded, C_BAD errored
    assert len(body["errors"]) == 1

    # C_OK flipped, C_BAD did not.
    assert StayChannels().get_by_reservation("res-ok")["status"] == STATUS_ARCHIVED
    assert StayChannels().get_by_reservation("res-bad")["status"] == STATUS_ACTIVE
