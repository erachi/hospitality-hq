"""Tests for the one-liner parser."""

from datetime import datetime, timedelta, timezone

from task_parser import parse_quick_create


USERS = [
    {"id": "vj", "slack_user_id": "UVJ", "display_name": "VJ"},
    {"id": "maggie", "slack_user_id": "UMAGGIE", "display_name": "Maggie"},
]
PROPERTIES = [
    {"id": "prop-palm", "name": "The Palm Club", "slug": "palm"},
    {"id": "prop-villa", "name": "Villa Bougainvillea", "slug": "villa"},
]


def _parse(text: str) -> dict:
    return parse_quick_create(text, users=USERS, properties=PROPERTIES)


def test_user_example():
    # "maggie rearrange airbnb photos for moreland medium urgent"
    result = _parse("maggie rearrange airbnb photos for moreland medium urgent")
    assert result["assignee_id"] == "maggie"
    # "urgent" consumed at the back; "medium" stays in title (ambiguous)
    assert result["priority"] == "urgent"
    # "moreland" doesn't match any property → stays in title
    assert result["property_id"] is None
    # Title keeps the middle tokens
    assert "rearrange airbnb photos" in result["title"]


def test_everything_at_front():
    result = _parse("maggie palm urgent fix garbage disposal")
    assert result["assignee_id"] == "maggie"
    assert result["property_id"] == "prop-palm"
    assert result["priority"] == "urgent"
    assert result["title"] == "fix garbage disposal"


def test_everything_at_back():
    result = _parse("fix garbage disposal palm urgent tomorrow")
    assert result["property_id"] == "prop-palm"
    assert result["priority"] == "urgent"
    assert result["due_date"] is not None
    assert result["title"] == "fix garbage disposal"


def test_strips_connective_after_property():
    result = _parse("fix sliding door at villa high")
    assert result["property_id"] == "prop-villa"
    assert result["priority"] == "high"
    # "at" should be stripped since its object (villa) was consumed
    assert result["title"] == "fix sliding door"


def test_middle_tokens_ignored():
    """Priority words mid-sentence should not be stolen from the title."""
    result = _parse("maggie follow up with low-income tenant")
    assert result["assignee_id"] == "maggie"
    assert result["priority"] is None
    assert "low-income" in result["title"]


def test_iso_due_date():
    result = _parse("vj villa restock consumables 2026-05-01")
    assert result["due_date"] == "2026-05-01"
    assert result["title"] == "restock consumables"


def test_day_of_week_due_date_is_next_occurrence():
    result = _parse("vj buy salt friday")
    # We can't assert the exact date without knowing today, but we can
    # confirm it's within the next 7 days and lands on a Friday.
    due = result["due_date"]
    assert due is not None
    due_dt = datetime.fromisoformat(due).date()
    today = datetime.now(timezone.utc).date()
    assert 1 <= (due_dt - today).days <= 7
    assert due_dt.weekday() == 4  # Friday


def test_today_and_tomorrow():
    today = datetime.now(timezone.utc).date()
    assert _parse("buy soap today")["due_date"] == today.isoformat()
    assert _parse("buy soap tomorrow")["due_date"] == (
        today + timedelta(days=1)
    ).isoformat()


def test_business_wide_aliases():
    for alias in ("business", "biz", "bw"):
        result = _parse(f"vj review scottsdale compliance {alias} urgent")
        assert result["property_id"] == "business"


def test_display_name_matches():
    """`VJ` (capitalized) should still resolve to the vj id."""
    result = _parse("VJ fix leaky faucet")
    assert result["assignee_id"] == "vj"


def test_empty_title_when_only_metadata():
    result = _parse("maggie palm urgent tomorrow")
    # Everything consumed; title is empty
    assert result["title"] == ""
    assert result["assignee_id"] == "maggie"
    assert result["property_id"] == "prop-palm"
    assert result["priority"] == "urgent"
    assert result["due_date"] is not None


def test_medium_maps_to_normal():
    result = _parse("maggie check garage medium")
    assert result["priority"] == "normal"


def test_single_word_title():
    result = _parse("maggie compliance")
    assert result["assignee_id"] == "maggie"
    assert result["title"] == "compliance"


def test_unknown_tokens_stay_in_title():
    """Tokens that don't match anything known stay as part of the title."""
    result = _parse("maggie asher call the plumber")
    assert result["assignee_id"] == "maggie"
    # "asher" isn't in the users list — stays in title
    assert "asher" in result["title"].lower()
