"""Tests for the per-property knowledge base loader."""

from knowledge_base_loader import (
    load_kb,
    get_property_name,
    format_for_claude,
    UUID_TO_FILENAME,
)


VILLA_UUID = "f8236d9d-988a-4192-9d16-2927b0b9ad8e"
PALM_UUID = "3278e9cb-9239-487f-aa51-cbfbaf4b7570"


def test_load_kb_villa_bougainvillea():
    """The Villa KB should load with static content populated."""
    load_kb.cache_clear()
    kb = load_kb(VILLA_UUID)

    assert kb["property"]["id"] == VILLA_UUID
    assert kb["property"]["name"] == "Villa Bougainvillea"
    # Static content from the docs should be present
    assert "firepit" in kb["static"]
    assert "projector" in kb["static"]
    assert "first_aid_kit" in kb["static"]


def test_load_kb_palm_club_skeleton():
    """The Palm Club KB should load even though the static section is empty."""
    load_kb.cache_clear()
    kb = load_kb(PALM_UUID)

    assert kb["property"]["id"] == PALM_UUID
    assert kb["property"]["name"] == "The Palm Club"


def test_load_kb_unknown_property_returns_empty():
    """Unknown UUIDs should return an empty dict, not raise."""
    load_kb.cache_clear()
    assert load_kb("not-a-real-uuid") == {}


def test_load_kb_empty_uuid_returns_empty():
    """Empty string should return an empty dict."""
    load_kb.cache_clear()
    assert load_kb("") == {}


def test_get_property_name():
    """Helper should return the canonical name from the KB."""
    load_kb.cache_clear()
    assert get_property_name(VILLA_UUID) == "Villa Bougainvillea"
    assert get_property_name(PALM_UUID) == "The Palm Club"
    assert get_property_name("unknown") is None


def test_format_for_claude_includes_authoritative_header():
    """Rendered output should mark the static section as authoritative."""
    load_kb.cache_clear()
    kb = load_kb(VILLA_UUID)
    rendered = format_for_claude(kb)

    assert "PROPERTY: Villa Bougainvillea" in rendered
    assert "AUTHORITATIVE FACTS" in rendered


def test_format_for_claude_includes_firepit_details():
    """Specific static facts should appear in rendered output."""
    load_kb.cache_clear()
    kb = load_kb(VILLA_UUID)
    rendered = format_for_claude(kb)

    # Firepit lighting steps should be included
    assert "Firepit" in rendered or "firepit" in rendered.lower()
    assert "propane" in rendered.lower()


def test_format_for_claude_skips_todos():
    """TODO placeholders should NOT appear in rendered output."""
    load_kb.cache_clear()
    kb = load_kb(VILLA_UUID)
    rendered = format_for_claude(kb)

    # Villa's check_in has "TODO" values — these must not leak to Claude
    assert "TODO" not in rendered


def test_format_for_claude_empty_kb():
    """Empty KB should render to empty string."""
    assert format_for_claude({}) == ""
    assert format_for_claude(None) == ""


def test_format_for_claude_with_dynamic_faqs():
    """Dynamic FAQs should render under a clearly labeled section."""
    kb = {
        "property": {"name": "Test", "id": "x"},
        "static": {},
        "dynamic": {
            "faqs": [
                {"question": "Can we check in early?", "answer": "Usually yes."},
            ]
        },
    }
    rendered = format_for_claude(kb)
    assert "PAST GUEST Q&A" in rendered
    assert "Can we check in early?" in rendered
    assert "Usually yes." in rendered


def test_format_for_claude_with_precedents():
    """Precedents should render under a clearly labeled section."""
    kb = {
        "property": {"name": "Test", "id": "x"},
        "static": {},
        "dynamic": {
            "precedents": [
                {"situation": "AC broken", "what_we_did": "Offered 50% refund."},
            ]
        },
    }
    rendered = format_for_claude(kb)
    assert "PRECEDENTS" in rendered
    assert "AC broken" in rendered
    assert "Offered 50% refund." in rendered


def test_all_mapped_uuids_have_files():
    """Every UUID in the mapping must have a loadable YAML file."""
    load_kb.cache_clear()
    for uuid in UUID_TO_FILENAME:
        kb = load_kb(uuid)
        assert kb, f"KB failed to load for {uuid}"
        assert kb.get("property", {}).get("id") == uuid, (
            f"Property UUID in YAML doesn't match mapping for {uuid}"
        )
