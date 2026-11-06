"""Tests for the reservation ↔ stay-channel DynamoDB index."""

from moto import mock_aws

from stay_channels import StayChannels
from stay_models import STATUS_ACTIVE, STATUS_ARCHIVED


def _put(sc: StayChannels, **overrides) -> None:
    kwargs = dict(
        reservation_uuid="res-1",
        channel_id="C_STAY_1",
        channel_name="stay-palm-smith-20261120",
        property_id="prop-palm",
        property_name="The Palm Club",
        guest_name="Jane Smith",
        check_in="2026-11-20",
        check_out="2026-11-25",
        journal_s3_key="stays/res-1.json",
    )
    kwargs.update(overrides)
    sc.put(**kwargs)


@mock_aws
def test_put_and_get_by_reservation(stay_channels_table):
    sc = StayChannels()
    _put(sc)

    row = sc.get_by_reservation("res-1")
    assert row is not None
    assert row["channel_id"] == "C_STAY_1"
    assert row["channel_name"] == "stay-palm-smith-20261120"
    assert row["status"] == STATUS_ACTIVE
    assert row["journal_s3_key"] == "stays/res-1.json"
    assert "created_at" in row


@mock_aws
def test_get_by_reservation_missing_returns_none(stay_channels_table):
    assert StayChannels().get_by_reservation("nope") is None
    assert StayChannels().get_by_reservation("") is None


@mock_aws
def test_put_skips_when_keys_empty(stay_channels_table):
    sc = StayChannels()
    sc.put(
        reservation_uuid="",
        channel_id="C",
        channel_name="x",
        property_id="p",
        property_name="P",
        guest_name="G",
        check_in="2026-01-01",
        check_out="2026-01-02",
        journal_s3_key="stays/x.json",
    )
    # Nothing written; no row under "".
    assert sc.get_by_reservation("") is None


@mock_aws
def test_get_by_channel_uses_gsi(stay_channels_table):
    sc = StayChannels()
    _put(sc, reservation_uuid="res-a", channel_id="C_A")
    _put(sc, reservation_uuid="res-b", channel_id="C_B")

    row = sc.get_by_channel("C_B")
    assert row is not None
    assert row["reservation_uuid"] == "res-b"

    assert sc.get_by_channel("C_UNKNOWN") is None


@mock_aws
def test_update_last_mirrored(stay_channels_table):
    sc = StayChannels()
    _put(sc)
    sc.update_last_mirrored("res-1", "2026-11-22T10:15:00Z")

    row = sc.get_by_reservation("res-1")
    assert row["last_mirrored_hospitable_ts"] == "2026-11-22T10:15:00Z"


@mock_aws
def test_mark_archived_sets_status_and_timestamp(stay_channels_table):
    sc = StayChannels()
    _put(sc)
    sc.mark_archived("res-1")

    row = sc.get_by_reservation("res-1")
    assert row["status"] == STATUS_ARCHIVED
    assert "archived_at" in row and row["archived_at"]


@mock_aws
def test_mark_active_restores_status_and_clears_archived_at(stay_channels_table):
    sc = StayChannels()
    _put(sc)
    sc.mark_archived("res-1")
    sc.mark_active("res-1")

    row = sc.get_by_reservation("res-1")
    assert row["status"] == STATUS_ACTIVE
    assert "archived_at" not in row


@mock_aws
def test_list_active_scans_only_active_rows(stay_channels_table):
    sc = StayChannels()
    _put(sc, reservation_uuid="res-a", channel_id="C_A")
    _put(sc, reservation_uuid="res-b", channel_id="C_B")
    sc.mark_archived("res-b")

    active = sc.list_active()
    assert {r["reservation_uuid"] for r in active} == {"res-a"}


@mock_aws
def test_list_due_for_archival_filters_on_checkout(stay_channels_table):
    sc = StayChannels()
    _put(sc, reservation_uuid="old", channel_id="C_OLD", check_out="2026-10-01")
    _put(sc, reservation_uuid="new", channel_id="C_NEW", check_out="2026-12-01")

    due = sc.list_due_for_archival(before_date="2026-11-01")
    assert {r["reservation_uuid"] for r in due} == {"old"}


@mock_aws
def test_list_due_for_archival_excludes_archived(stay_channels_table):
    sc = StayChannels()
    _put(sc, reservation_uuid="old", channel_id="C_OLD", check_out="2026-10-01")
    sc.mark_archived("old")

    due = sc.list_due_for_archival(before_date="2026-11-01")
    assert due == []
