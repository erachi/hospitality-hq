"""Tests for the Hospitable API client."""

import responses
from unittest.mock import patch
from hospitable_client import HospitableClient


@responses.activate
@patch("hospitable_client.get_hospitable_token", return_value="test-token")
def test_get_active_reservations_attaches_property_id(mock_token):
    """Hospitable's list-reservations endpoint doesn't return property_id,
    so the client must attach it from the query context."""
    prop_a = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
    prop_b = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"

    responses.add(
        responses.GET,
        "https://public.api.hospitable.com/v2/reservations",
        json={
            "data": [
                {"id": "res-1", "guest": {"first_name": "A"}},
                {"id": "res-2", "guest": {"first_name": "B"}},
            ],
            "meta": {"current_page": 1, "last_page": 1},
        },
        status=200,
    )
    responses.add(
        responses.GET,
        "https://public.api.hospitable.com/v2/reservations",
        json={
            "data": [
                {"id": "res-3", "guest": {"first_name": "C"}},
            ],
            "meta": {"current_page": 1, "last_page": 1},
        },
        status=200,
    )

    client = HospitableClient()
    results = client.get_active_reservations([prop_a, prop_b])

    assert len(results) == 3
    # First two reservations came from property A's query
    assert results[0]["property_id"] == prop_a
    assert results[1]["property_id"] == prop_a
    # Third from property B's query
    assert results[2]["property_id"] == prop_b
