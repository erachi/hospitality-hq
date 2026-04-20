"""Tests for the Slack-thread Q&A module."""

from unittest.mock import patch, MagicMock

from slack_qa import answer


def _mock_anthropic_response(text: str):
    resp = MagicMock()
    content = MagicMock()
    content.text = text
    resp.content = [content]
    return resp


@patch("slack_qa._get_client")
def test_answer_uses_haiku_model(mock_client):
    """Q&A should use the Haiku model for latency."""
    mock_client.return_value.messages.create.return_value = _mock_anthropic_response("Check-in is at 4 PM.")
    answer(
        question="when is check-in?",
        property_name="Villa Bougainvillea",
        local_kb_context="check_in: 4pm",
    )
    call_kwargs = mock_client.return_value.messages.create.call_args.kwargs
    assert "haiku" in call_kwargs["model"].lower()


@patch("slack_qa._get_client")
def test_answer_includes_kb_in_prompt(mock_client):
    """KB content should reach Claude's prompt verbatim."""
    mock_client.return_value.messages.create.return_value = _mock_anthropic_response("OK")
    answer(
        question="what's the wifi?",
        property_name="Villa",
        local_kb_context="AUTHORITATIVE FACTS: wifi password is super-secret",
    )
    prompt = mock_client.return_value.messages.create.call_args.kwargs["messages"][0]["content"]
    assert "super-secret" in prompt
    assert "Villa" in prompt


@patch("slack_qa._get_client")
def test_answer_includes_thread_logs_in_prompt(mock_client):
    """Prior thread logs should be included so Claude can reference them."""
    mock_client.return_value.messages.create.return_value = _mock_anthropic_response("Yes, we sent them towels")
    answer(
        question="any notes on this guest?",
        property_name="Villa",
        thread_logs_context="NOTE: Delivered extra towels yesterday",
    )
    prompt = mock_client.return_value.messages.create.call_args.kwargs["messages"][0]["content"]
    assert "Delivered extra towels" in prompt


@patch("slack_qa._get_client")
def test_answer_returns_text(mock_client):
    mock_client.return_value.messages.create.return_value = _mock_anthropic_response("Check-in is at 4 PM.")
    result = answer(question="when is check-in?", property_name="Villa")
    assert "Check-in" in result


@patch("slack_qa._get_client")
def test_empty_question_short_circuits(mock_client):
    """Empty input should return a help prompt without calling Claude."""
    result = answer(question="", property_name="Villa")
    assert "didn't catch" in result.lower() or "question" in result.lower()
    mock_client.return_value.messages.create.assert_not_called()


@patch("slack_qa._get_client")
def test_anthropic_error_returns_friendly_message(mock_client):
    """An API failure shouldn't bubble up — return a friendly fallback."""
    mock_client.return_value.messages.create.side_effect = Exception("API down")
    result = answer(question="what's checkout?", property_name="Villa")
    assert "error" in result.lower()
