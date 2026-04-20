"""Tests for issue classifier and response drafter."""

from unittest.mock import patch, MagicMock
from classifier import classify_message, draft_response, summarize_conversation


def _mock_anthropic_response(text: str):
    """Create a mock Anthropic API response."""
    mock_resp = MagicMock()
    mock_content = MagicMock()
    mock_content.text = text
    mock_resp.content = [mock_content]
    return mock_resp


@patch("classifier._get_client")
def test_classify_urgent_maintenance(mock_client):
    """AC broken message should classify as URGENT_MAINTENANCE / HIGH."""
    mock_client.return_value.messages.create.return_value = _mock_anthropic_response(
        "CATEGORY: URGENT_MAINTENANCE\n"
        "URGENCY: HIGH\n"
        "RESPONSE_NEEDED: YES\n"
        "DESCRIPTOR: AC Not Working\n"
        "SUMMARY: AC not working"
    )

    result = classify_message("The AC isn't working and it's really hot", "Villa Bougainvillea")

    assert result["category"] == "URGENT_MAINTENANCE"
    assert result["urgency"] == "HIGH"
    assert result["response_needed"] is True
    assert result["descriptor"] == "AC Not Working"
    assert "AC" in result["summary"]


@patch("classifier._get_client")
def test_classify_pre_arrival(mock_client):
    """Check-in question should classify as PRE_ARRIVAL."""
    mock_client.return_value.messages.create.return_value = _mock_anthropic_response(
        "CATEGORY: PRE_ARRIVAL\n"
        "URGENCY: MEDIUM\n"
        "RESPONSE_NEEDED: YES\n"
        "DESCRIPTOR: Check-in Time Question\n"
        "SUMMARY: Guest asking about check-in time"
    )

    result = classify_message("What time can we check in?", "The Palm Club")

    assert result["category"] == "PRE_ARRIVAL"
    assert result["urgency"] == "MEDIUM"
    assert result["response_needed"] is True
    assert result["descriptor"] == "Check-in Time Question"


@patch("classifier._get_client")
def test_classify_positive_feedback(mock_client):
    """Pure thank-you should classify as POSITIVE / LOW with response_needed=False."""
    mock_client.return_value.messages.create.return_value = _mock_anthropic_response(
        "CATEGORY: POSITIVE\n"
        "URGENCY: LOW\n"
        "RESPONSE_NEEDED: NO\n"
        "DESCRIPTOR: Thank You Note\n"
        "SUMMARY: Guest enjoyed their stay"
    )

    result = classify_message("We had an amazing time, thank you!", "Villa Bougainvillea")

    assert result["category"] == "POSITIVE"
    assert result["urgency"] == "LOW"
    assert result["response_needed"] is False
    assert result["descriptor"] == "Thank You Note"


@patch("classifier._get_client")
def test_classify_positive_with_embedded_request_needs_response(mock_client):
    """Thank-you + embedded ask should still flag response_needed=True."""
    mock_client.return_value.messages.create.return_value = _mock_anthropic_response(
        "CATEGORY: POSITIVE\n"
        "URGENCY: LOW\n"
        "RESPONSE_NEEDED: YES\n"
        "DESCRIPTOR: Early Check-in Request\n"
        "SUMMARY: Thanks plus early check-in request"
    )

    result = classify_message(
        "Thank you so much! By the way, can we check in at 11?",
        "Villa Bougainvillea",
    )

    assert result["category"] == "POSITIVE"
    assert result["response_needed"] is True
    assert result["descriptor"] == "Early Check-in Request"


@patch("classifier._get_client")
def test_classify_handles_malformed_response(mock_client):
    """Malformed API response should fall back to GENERAL / MEDIUM / response_needed=True."""
    mock_client.return_value.messages.create.return_value = _mock_anthropic_response(
        "I'm not sure how to classify this"
    )

    result = classify_message("Random message", "Villa Bougainvillea")

    assert result["category"] == "GENERAL"
    assert result["urgency"] == "MEDIUM"
    # Fail-safe: default must keep the host in the loop.
    assert result["response_needed"] is True
    # Descriptor has a generic fallback so the Slack header still renders.
    assert result["descriptor"] == "Guest Message"


@patch("classifier._get_client")
def test_draft_response_returns_text(mock_client):
    """Draft response should return the generated text."""
    mock_client.return_value.messages.create.return_value = _mock_anthropic_response(
        "Hi Jane! I'm so sorry about the AC issue. I'm looking into this right away and will have someone out to fix it as soon as possible."
    )

    classification = {
        "category": "URGENT_MAINTENANCE",
        "urgency": "HIGH",
        "summary": "AC not working",
    }

    result = draft_response(
        message_text="The AC isn't working",
        property_name="Villa Bougainvillea",
        property_description="5-bedroom desert oasis",
        knowledge_hub_context="AC controls are in the hallway",
        guest_name="Jane Smith",
        checkin_date="2026-04-20",
        checkout_date="2026-04-25",
        classification=classification,
    )

    assert isinstance(result, str)
    assert len(result) > 0
    assert "AC" in result


@patch("classifier._get_client")
def test_draft_includes_local_kb_context(mock_client):
    """Local KB context should be included in the prompt sent to Claude."""
    mock_client.return_value.messages.create.return_value = _mock_anthropic_response(
        "Check-in is at 4 PM."
    )

    local_kb = (
        "PROPERTY: Villa Bougainvillea\n\n"
        "═══ AUTHORITATIVE FACTS ═══\n"
        "Check In: 4:00 PM"
    )

    draft_response(
        message_text="What time is check-in?",
        property_name="Villa Bougainvillea",
        property_description="desc",
        knowledge_hub_context="",
        local_kb_context=local_kb,
        guest_name="Guest",
        checkin_date="2026-04-20",
        checkout_date="2026-04-25",
        classification={"category": "PRE_ARRIVAL", "urgency": "MEDIUM", "summary": "Check-in time"},
    )

    call_args = mock_client.return_value.messages.create.call_args
    prompt = call_args.kwargs["messages"][0]["content"]
    assert "AUTHORITATIVE FACTS" in prompt
    assert "4:00 PM" in prompt


@patch("classifier._get_client")
def test_draft_uses_correct_models(mock_client):
    """Classification should use Haiku, drafting should use Sonnet."""
    mock_client.return_value.messages.create.return_value = _mock_anthropic_response(
        "CATEGORY: GENERAL\nURGENCY: LOW\nSUMMARY: General question"
    )

    classify_message("Hello", "Villa Bougainvillea")

    call_args = mock_client.return_value.messages.create.call_args
    assert "haiku" in call_args.kwargs["model"]

    mock_client.return_value.messages.create.return_value = _mock_anthropic_response(
        "Hi there! How can I help?"
    )

    draft_response(
        message_text="Hello",
        property_name="Villa Bougainvillea",
        property_description="desc",
        knowledge_hub_context="kb",
        guest_name="Guest",
        checkin_date="2026-04-20",
        checkout_date="2026-04-25",
        classification={"category": "GENERAL", "urgency": "LOW", "summary": "General"},
    )

    call_args = mock_client.return_value.messages.create.call_args
    assert "sonnet" in call_args.kwargs["model"]


@patch("classifier._get_client")
def test_summarize_conversation_returns_bullets(mock_client):
    """Summary call sends the transcript to Haiku and returns the stripped output."""
    mock_client.return_value.messages.create.return_value = _mock_anthropic_response(
        "• *Agreed so far:* Guest to complete rental agreement + ID\n"
        "• *Still open:* Early check-in at 11am needs verification\n"
    )

    messages = [
        {"sender_type": "host", "body": "Please sign the rental agreement", "created_at": "t1"},
        {"sender_type": "guest", "body": "Will do today", "created_at": "t2"},
        {"sender_type": "guest", "body": "Can we check in at 11?", "created_at": "t3"},
    ]

    result = summarize_conversation(messages, "Villa Bougainvillea")

    assert "Agreed so far" in result
    assert "Still open" in result
    # Stripped of trailing whitespace/newlines.
    assert not result.endswith("\n")

    # Uses Haiku (cheap), not Sonnet.
    call_args = mock_client.return_value.messages.create.call_args
    assert "haiku" in call_args.kwargs["model"]
    # Transcript makes it into the prompt body.
    assert "rental agreement" in call_args.kwargs["messages"][0]["content"]


@patch("classifier._get_client")
def test_summarize_conversation_skips_thin_threads(mock_client):
    """Threads with fewer than 2 messages aren't worth an API call."""
    assert summarize_conversation([], "Villa Bougainvillea") == ""
    assert summarize_conversation(
        [{"sender_type": "guest", "body": "hi"}],
        "Villa Bougainvillea",
    ) == ""

    # No call was made.
    mock_client.return_value.messages.create.assert_not_called()
