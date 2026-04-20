"""Tests for the Claude-vision receipt OCR wrapper."""

from unittest.mock import MagicMock, patch

import pytest

from expense_ocr import OCR_MODEL, _first_tool_input, _normalize_media_type, extract_receipt


def _mock_response(tool_input: dict):
    """Build a MagicMock that mimics anthropic.types.Message with one tool_use block."""
    block = MagicMock()
    block.type = "tool_use"
    block.input = tool_input
    response = MagicMock()
    response.content = [block]
    return response


def _no_tool_response():
    block = MagicMock()
    block.type = "text"
    block.text = "not a tool call"
    response = MagicMock()
    response.content = [block]
    return response


@patch("expense_ocr.Anthropic")
@patch("expense_ocr.get_anthropic_key", return_value="sk-test")
def test_extract_receipt_returns_tool_input(mock_key, mock_anthropic_cls):
    """Happy path: Claude calls the tool and we get back the input dict."""
    expected = {
        "merchant_name": "Home Depot #0438",
        "transaction_date": "2026-04-18",
        "total": "311.97",
        "suggested_category": "supplies",
        "extraction_confidence": "high",
        "category_confidence": "high",
    }

    mock_client = MagicMock()
    mock_client.messages.create.return_value = _mock_response(expected)
    mock_anthropic_cls.return_value = mock_client

    result = extract_receipt(b"fake-jpeg-bytes", "image/jpeg")

    assert result == expected
    # Verify we actually called the API with the right model and forced
    # the tool choice.
    args, kwargs = mock_client.messages.create.call_args
    assert kwargs["model"] == OCR_MODEL
    assert kwargs["tool_choice"] == {"type": "tool", "name": "extract_receipt"}
    assert len(kwargs["tools"]) == 1
    assert kwargs["tools"][0]["name"] == "extract_receipt"


@patch("expense_ocr.Anthropic")
@patch("expense_ocr.get_anthropic_key", return_value="sk-test")
def test_extract_receipt_raises_when_tool_not_called(mock_key, mock_anthropic_cls):
    mock_client = MagicMock()
    mock_client.messages.create.return_value = _no_tool_response()
    mock_anthropic_cls.return_value = mock_client

    with pytest.raises(RuntimeError, match="did not call"):
        extract_receipt(b"bytes", "image/jpeg")


def test_extract_receipt_rejects_empty_bytes():
    with pytest.raises(ValueError, match="empty"):
        extract_receipt(b"", "image/jpeg")


def test_extract_receipt_rejects_unsupported_media_type():
    with pytest.raises(ValueError, match="unsupported"):
        extract_receipt(b"bytes", "image/tiff")


def test_normalize_media_type_canonicalizes_jpg():
    assert _normalize_media_type("image/jpg") == "image/jpeg"
    assert _normalize_media_type("IMAGE/JPEG") == "image/jpeg"
    assert _normalize_media_type("image/png") == "image/png"


def test_first_tool_input_handles_empty_content():
    empty = MagicMock()
    empty.content = []
    assert _first_tool_input(empty) is None


def test_first_tool_input_skips_non_tool_blocks():
    text_block = MagicMock()
    text_block.type = "text"
    text_block.text = "some text"
    tool_block = MagicMock()
    tool_block.type = "tool_use"
    tool_block.input = {"merchant_name": "X"}
    response = MagicMock()
    response.content = [text_block, tool_block]

    assert _first_tool_input(response) == {"merchant_name": "X"}


@patch("expense_ocr.Anthropic")
@patch("expense_ocr.get_anthropic_key", return_value="sk-test")
def test_schema_includes_only_known_category_ids(mock_key, mock_anthropic_cls):
    """Guard: the tool schema's category enum must be all valid ids."""
    mock_client = MagicMock()
    mock_client.messages.create.return_value = _mock_response(
        {"merchant_name": "X", "transaction_date": "2026-04-18", "total": "1.00"}
    )
    mock_anthropic_cls.return_value = mock_client

    extract_receipt(b"bytes", "image/jpeg")
    _, kwargs = mock_client.messages.create.call_args
    enum = kwargs["tools"][0]["input_schema"]["properties"]["suggested_category"]["enum"]

    from expense_categories import valid_category_ids

    assert set(enum) == valid_category_ids()
