"""Tests for the S3-backed StayStore."""

import json

import boto3

from stay_models import (
    SOURCE_HOSPITABLE_GUEST,
    SOURCE_SLACK_USER,
    SOURCE_SYSTEM,
    StayEntry,
    StayJournal,
)
from stay_store import StayStore


def _journal(res_uuid: str = "res-1") -> StayJournal:
    return StayJournal.new(
        reservation_uuid=res_uuid,
        property_id="prop-palm",
        property_name="The Palm Club",
        guest_name="Jane Smith",
        check_in="2026-11-20",
        check_out="2026-11-25",
    )


def test_put_and_get_roundtrip(stay_journal_bucket):
    store = StayStore()
    j = _journal()
    j.append(StayEntry.new(source=SOURCE_SYSTEM, author="system", body="provisioned"))

    store.put(j)
    back = store.get("res-1")

    assert back is not None
    assert back.reservation_uuid == "res-1"
    assert back.property_name == "The Palm Club"
    assert len(back.entries) == 1
    assert back.entries[0].body == "provisioned"


def test_get_missing_returns_none(stay_journal_bucket):
    assert StayStore().get("does-not-exist") is None
    assert StayStore().get("") is None


def test_put_touches_updated_at(stay_journal_bucket):
    store = StayStore()
    j = _journal()
    before = j.updated_at
    store.put(j)
    # put() mutates the object in place via touch()
    assert j.updated_at >= before
    assert store.get("res-1").updated_at == j.updated_at


def test_append_entry_grows_journal(stay_journal_bucket):
    store = StayStore()
    j = _journal()
    store.put(j)

    updated = store.append_entry(
        "res-1",
        StayEntry.new(
            source=SOURCE_HOSPITABLE_GUEST,
            author="guest",
            body="hi, can I check in at 1pm?",
        ),
    )
    assert updated is not None
    assert len(updated.entries) == 1

    # Second append persists alongside the first.
    store.append_entry(
        "res-1",
        StayEntry.new(source=SOURCE_SLACK_USER, author="UVJ", body="sure"),
    )

    back = store.get("res-1")
    assert [e.source for e in back.entries] == [
        SOURCE_HOSPITABLE_GUEST,
        SOURCE_SLACK_USER,
    ]


def test_append_entry_on_missing_journal_returns_none(stay_journal_bucket):
    """We don't auto-create journals — that's the provisioner's job."""
    store = StayStore()
    result = store.append_entry(
        "never-provisioned",
        StayEntry.new(source=SOURCE_SLACK_USER, author="UVJ", body="hi"),
    )
    assert result is None
    # Nothing should have been written.
    assert store.get("never-provisioned") is None


def test_list_all_returns_every_journal(stay_journal_bucket):
    store = StayStore()
    for rid in ("res-a", "res-b", "res-c"):
        store.put(_journal(rid))

    rows = store.list_all()
    assert sorted(j.reservation_uuid for j in rows) == ["res-a", "res-b", "res-c"]


def test_prefix_scoping_ignores_other_keys(stay_journal_bucket):
    """Keys outside the stays/ prefix must not pollute list_all."""
    s3 = boto3.client("s3", region_name="us-east-1")
    s3.put_object(
        Bucket="hospitality-hq-tasks-test",
        Key="tasks/unrelated.json",
        Body=json.dumps({"id": "t"}).encode("utf-8"),
    )
    s3.put_object(
        Bucket="hospitality-hq-tasks-test",
        Key="stays/not-json.txt",
        Body=b"ignore me",
    )

    store = StayStore()
    store.put(_journal("res-only"))

    rows = store.list_all()
    assert [j.reservation_uuid for j in rows] == ["res-only"]


def test_key_uses_prefix(stay_journal_bucket):
    """The journal object actually lands at stays/<uuid>.json."""
    store = StayStore()
    store.put(_journal("res-keyed"))

    s3 = boto3.client("s3", region_name="us-east-1")
    obj = s3.get_object(Bucket="hospitality-hq-tasks-test", Key="stays/res-keyed.json")
    data = json.loads(obj["Body"].read())
    assert data["reservation_uuid"] == "res-keyed"
