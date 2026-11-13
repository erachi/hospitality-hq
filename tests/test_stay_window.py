"""Tests for the in-stay window predicate."""

from datetime import date

from stay_window import is_in_stay_window


def test_in_stay_window_mid_stay():
    assert is_in_stay_window("2026-11-20", "2026-11-25", date(2026, 11, 22))


def test_in_stay_window_check_in_day():
    assert is_in_stay_window("2026-11-20", "2026-11-25", date(2026, 11, 20))


def test_in_stay_window_check_out_day_inclusive():
    assert is_in_stay_window("2026-11-20", "2026-11-25", date(2026, 11, 25))


def test_in_stay_window_day_before_check_in_included():
    """Early-arrival pad: day before check-in still counts."""
    assert is_in_stay_window("2026-11-20", "2026-11-25", date(2026, 11, 19))


def test_in_stay_window_two_days_before_check_in_excluded():
    assert not is_in_stay_window("2026-11-20", "2026-11-25", date(2026, 11, 18))


def test_in_stay_window_day_after_check_out_excluded():
    assert not is_in_stay_window("2026-11-20", "2026-11-25", date(2026, 11, 26))


def test_in_stay_window_empty_strings_false():
    assert not is_in_stay_window("", "2026-11-25", date(2026, 11, 22))
    assert not is_in_stay_window("2026-11-20", "", date(2026, 11, 22))
    assert not is_in_stay_window("", "", date(2026, 11, 22))


def test_in_stay_window_malformed_strings_false():
    assert not is_in_stay_window("not-a-date", "2026-11-25", date(2026, 11, 22))
    assert not is_in_stay_window("2026-11-20", "nope", date(2026, 11, 22))


def test_in_stay_window_tolerates_iso_8601_with_time():
    """Reservation snapshots occasionally carry full ISO timestamps."""
    assert is_in_stay_window(
        "2026-11-20T00:00:00Z", "2026-11-25T12:00:00Z", date(2026, 11, 22)
    )


def test_in_stay_window_default_today(monkeypatch):
    """Default `today` is derived from the UTC clock."""
    from datetime import datetime, timezone

    import stay_window as sw

    class _FakeDatetime:
        @classmethod
        def now(cls, tz=None):
            return datetime(2026, 11, 22, 10, 0, 0, tzinfo=tz or timezone.utc)

    monkeypatch.setattr(sw, "datetime", _FakeDatetime)
    assert sw.is_in_stay_window("2026-11-20", "2026-11-25")
    assert not sw.is_in_stay_window("2026-12-01", "2026-12-05")
