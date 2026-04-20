"""Tests for the Slack thread → reservation mapping wrapper."""

from moto import mock_aws

from thread_mapping import ThreadMapping


@mock_aws
def test_put_and_get_mapping(thread_mapping_table):
    """Round-trip: put a mapping, then read it back."""
    tm = ThreadMapping()
    tm.put_mapping(
        thread_ts="1713400000.000100",
        reservation_uuid="res-abc",
        property_id="prop-xyz",
        property_name="Villa Bougainvillea",
        guest_name="Jane Smith",
    )

    result = tm.get_mapping("1713400000.000100")
    assert result is not None
    assert result["reservation_uuid"] == "res-abc"
    assert result["property_id"] == "prop-xyz"
    assert result["property_name"] == "Villa Bougainvillea"
    assert result["guest_name"] == "Jane Smith"
    assert "created_at" in result
    assert "ttl" in result


@mock_aws
def test_get_mapping_unknown_thread_returns_none(thread_mapping_table):
    """Unknown thread_ts returns None, not an exception."""
    tm = ThreadMapping()
    assert tm.get_mapping("not-a-real-ts") is None


@mock_aws
def test_put_mapping_ignores_empty_thread_ts(thread_mapping_table):
    """Empty thread_ts should no-op, not raise."""
    tm = ThreadMapping()
    # Would throw ValidationException if actually written
    tm.put_mapping("", "res-1", "prop-1", "Villa", "Guest")
    # Nothing was written, so nothing to read
    assert tm.get_mapping("") is None


@mock_aws
def test_ttl_is_set_in_future(thread_mapping_table):
    """TTL should be roughly 90 days out (in unix epoch seconds)."""
    import time

    tm = ThreadMapping()
    tm.put_mapping("1.1", "res-1", "prop-1", "Villa", "Guest")
    item = tm.get_mapping("1.1")
    ttl = int(item["ttl"])
    now = int(time.time())
    # Should be between 89 and 91 days out
    assert ttl > now + (89 * 86400)
    assert ttl < now + (91 * 86400)


@mock_aws
def test_get_mapping_empty_key_returns_none(thread_mapping_table):
    """Empty key should short-circuit and return None."""
    assert ThreadMapping().get_mapping("") is None
