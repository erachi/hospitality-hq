"""Tests for stay_models."""

from stay_models import (
    SOURCE_HOSPITABLE_GUEST,
    SOURCE_SLACK_AGENT,
    SOURCE_SLACK_USER,
    SOURCE_SYSTEM,
    STATUS_ACTIVE,
    STATUS_ARCHIVED,
    StayEntry,
    StayJournal,
)


def _journal() -> StayJournal:
    return StayJournal.new(
        reservation_uuid="res-uuid-1",
        property_id="prop-palm",
        property_name="The Palm Club",
        guest_name="Jane Smith",
        check_in="2026-11-20",
        check_out="2026-11-25",
    )


def test_new_entry_has_id_and_ts():
    e = StayEntry.new(
        source=SOURCE_SLACK_USER,
        author="UVJ",
        body="should I offer an upgrade?",
    )
    assert e.id
    assert e.ts
    assert e.source == SOURCE_SLACK_USER
    assert e.metadata == {}


def test_new_entry_accepts_explicit_ts_and_metadata():
    e = StayEntry.new(
        source=SOURCE_HOSPITABLE_GUEST,
        author="guest",
        body="hi, early check-in possible?",
        ts="2026-11-20T15:30:00Z",
        metadata={"hospitable_message_id": "msg-42"},
    )
    assert e.ts == "2026-11-20T15:30:00Z"
    assert e.metadata["hospitable_message_id"] == "msg-42"


def test_entry_roundtrip_to_dict():
    e = StayEntry.new(
        source=SOURCE_SLACK_AGENT,
        author="agent",
        body="I'd suggest a $30 credit.",
        metadata={"draft_state": "proposed"},
    )
    data = e.to_dict()
    back = StayEntry.from_dict(data)
    assert back.id == e.id
    assert back.body == e.body
    assert back.metadata == {"draft_state": "proposed"}


def test_new_journal_has_timestamps_and_active_status():
    j = _journal()
    assert j.reservation_uuid == "res-uuid-1"
    assert j.status == STATUS_ACTIVE
    assert j.created_at == j.updated_at
    assert j.archived_at is None
    assert j.entries == []
    assert not j.is_archived()


def test_journal_append_touches_updated_at_and_grows_entries():
    j = _journal()
    original = j.updated_at

    j.append(StayEntry.new(source=SOURCE_SYSTEM, author="system", body="provisioned"))
    assert len(j.entries) == 1
    assert j.updated_at >= original

    j.append(StayEntry.new(source=SOURCE_SLACK_USER, author="UVJ", body="hello"))
    assert len(j.entries) == 2


def test_journal_mark_archived_flips_status_and_stamps():
    j = _journal()
    assert not j.is_archived()
    j.mark_archived()
    assert j.is_archived()
    assert j.status == STATUS_ARCHIVED
    assert j.archived_at is not None


def test_journal_roundtrip_preserves_entries():
    j = _journal()
    j.append(
        StayEntry.new(
            source=SOURCE_HOSPITABLE_GUEST,
            author="guest",
            body="can we check out an hour later?",
            ts="2026-11-24T18:00:00Z",
            metadata={"hospitable_message_id": "m1"},
        )
    )
    j.append(
        StayEntry.new(
            source=SOURCE_SLACK_AGENT,
            author="agent",
            body="Yes — let's reply yes to a 1-hour late checkout.",
        )
    )

    back = StayJournal.from_dict(j.to_dict())
    assert back.reservation_uuid == j.reservation_uuid
    assert back.property_name == "The Palm Club"
    assert back.status == STATUS_ACTIVE
    assert len(back.entries) == 2
    assert back.entries[0].metadata["hospitable_message_id"] == "m1"
    assert back.entries[1].source == SOURCE_SLACK_AGENT


def test_journal_from_dict_tolerates_missing_optional_fields():
    minimal = {
        "reservation_uuid": "res-x",
        "property_id": "p",
        "property_name": "P",
        "guest_name": "G",
        "check_in": "2026-01-01",
        "check_out": "2026-01-05",
        "created_at": "2026-01-01T00:00:00Z",
    }
    j = StayJournal.from_dict(minimal)
    assert j.reservation_uuid == "res-x"
    assert j.status == STATUS_ACTIVE
    assert j.entries == []
    assert j.updated_at == j.created_at
