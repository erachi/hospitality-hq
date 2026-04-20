"""Tests for the thread logs wrapper (notes, issues, resolutions)."""

from moto import mock_aws

from thread_logs import (
    ThreadLogs,
    TYPE_NOTE,
    TYPE_ISSUE,
    TYPE_RESOLUTION,
    format_for_claude,
)


@mock_aws
def test_append_and_get_note(thread_logs_table):
    tl = ThreadLogs()
    log_id = tl.append_log(
        reservation_uuid="res-1",
        log_type=TYPE_NOTE,
        text="Guest is a bachelorette party",
        author="U123",
        thread_ts="1713400000.000100",
    )
    assert log_id is not None

    items = tl.get_logs("res-1")
    assert len(items) == 1
    assert items[0]["type"] == TYPE_NOTE
    assert items[0]["text"] == "Guest is a bachelorette party"
    assert items[0]["author"] == "U123"
    assert items[0]["thread_ts"] == "1713400000.000100"


@mock_aws
def test_logs_are_sorted_chronologically(thread_logs_table):
    import time

    tl = ThreadLogs()
    tl.append_log("res-1", TYPE_NOTE, "first", "U1", "ts1")
    time.sleep(0.01)
    tl.append_log("res-1", TYPE_ISSUE, "second", "U1", "ts1")
    time.sleep(0.01)
    tl.append_log("res-1", TYPE_RESOLUTION, "third", "U1", "ts1")

    items = tl.get_logs("res-1")
    assert len(items) == 3
    assert [i["text"] for i in items] == ["first", "second", "third"]


@mock_aws
def test_different_reservations_are_isolated(thread_logs_table):
    tl = ThreadLogs()
    tl.append_log("res-A", TYPE_NOTE, "alpha note", "U1", "ts1")
    tl.append_log("res-B", TYPE_NOTE, "bravo note", "U1", "ts2")

    a = tl.get_logs("res-A")
    b = tl.get_logs("res-B")
    assert len(a) == 1 and a[0]["text"] == "alpha note"
    assert len(b) == 1 and b[0]["text"] == "bravo note"


@mock_aws
def test_append_invalid_type_returns_none(thread_logs_table):
    tl = ThreadLogs()
    assert tl.append_log("res-1", "bogus", "x", "U1", "ts") is None
    assert tl.get_logs("res-1") == []


@mock_aws
def test_get_logs_unknown_reservation_returns_empty(thread_logs_table):
    assert ThreadLogs().get_logs("nope") == []


def test_format_for_claude_empty_list_returns_empty_string():
    assert format_for_claude([]) == ""


def test_format_for_claude_includes_header_and_icons():
    logs = [
        {"type": TYPE_NOTE, "text": "extra towels requested", "author": "U1", "created_at": "2026-04-20T12:00:00+00:00"},
        {"type": TYPE_ISSUE, "text": "projector cord missing", "author": "U2", "created_at": "2026-04-20T13:00:00+00:00"},
        {"type": TYPE_RESOLUTION, "text": "delivered replacement", "author": "U2", "created_at": "2026-04-20T14:00:00+00:00"},
    ]
    rendered = format_for_claude(logs)
    assert "INTERNAL NOTES" in rendered
    assert "📝" in rendered
    assert "🔧" in rendered
    assert "✅" in rendered
    assert "extra towels" in rendered
    assert "projector cord" in rendered
    assert "delivered replacement" in rendered
