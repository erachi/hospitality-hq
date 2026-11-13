"""Tests for the AppConfig feature-flag client."""

from __future__ import annotations

import io
import json
import urllib.error
from unittest.mock import patch

import pytest

import flags


@pytest.fixture(autouse=True)
def _reset_cache():
    flags._reset_cache_for_tests()
    yield
    flags._reset_cache_for_tests()


def _response(payload: dict) -> io.BytesIO:
    body = json.dumps(payload).encode("utf-8")

    class _Resp(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *_):
            return False

    return _Resp(body)


def test_enabled_flag_returns_true():
    with patch(
        "flags.urllib.request.urlopen",
        return_value=_response({"foo": {"enabled": True}}),
    ):
        assert flags.is_enabled("foo") is True


def test_disabled_flag_returns_false():
    with patch(
        "flags.urllib.request.urlopen",
        return_value=_response({"foo": {"enabled": False}}),
    ):
        assert flags.is_enabled("foo") is False


def test_missing_flag_returns_default():
    with patch("flags.urllib.request.urlopen", return_value=_response({})):
        assert flags.is_enabled("nope") is False
        assert flags.is_enabled("nope", default=True) is True


def test_malformed_entry_returns_default():
    with patch(
        "flags.urllib.request.urlopen", return_value=_response({"foo": "not-a-dict"})
    ):
        assert flags.is_enabled("foo") is False


def test_extension_unreachable_returns_default():
    err = urllib.error.URLError("connection refused")
    with patch("flags.urllib.request.urlopen", side_effect=err):
        assert flags.is_enabled("foo") is False
        assert flags.is_enabled("foo", default=True) is True


def test_malformed_json_returns_default():
    with patch(
        "flags.urllib.request.urlopen",
        side_effect=json.JSONDecodeError("bad", "doc", 0),
    ):
        assert flags.is_enabled("foo") is False


def test_cache_reused_within_ttl():
    with patch(
        "flags.urllib.request.urlopen",
        return_value=_response({"foo": {"enabled": True}}),
    ) as mocked:
        flags.is_enabled("foo")
        flags.is_enabled("foo")
        flags.is_enabled("foo")
        assert mocked.call_count == 1
