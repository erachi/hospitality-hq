"""Tests for the category taxonomy + merchant rule matching."""

import expense_categories
from expense_categories import (
    get_category,
    load_categories,
    load_merchant_patterns,
    suggest_from_merchant,
    valid_category_ids,
)


def setup_function(_fn):
    """Clear lru_cache between tests so each test sees a fresh load."""
    load_categories.cache_clear()
    load_merchant_patterns.cache_clear()


def test_categories_cover_all_schedule_e_lines():
    ids = valid_category_ids()
    # Sanity: every Schedule E line that matters for an STR must be present.
    required = {
        "advertising",
        "auto_travel",
        "cleaning_maintenance",
        "commissions",
        "insurance",
        "legal_professional",
        "management_fees",
        "repairs",
        "supplies",
        "taxes",
        "utilities",
        "depreciation",
        "other",
        "personal_skip",
    }
    missing = required - ids
    assert not missing, f"missing required category ids: {missing}"


def test_get_category_returns_full_row():
    row = get_category("repairs")
    assert row is not None
    assert row["id"] == "repairs"
    assert row["display_name"] == "Repairs"
    assert row["schedule_e_line"] == "Repairs"


def test_get_category_returns_none_for_unknown():
    assert get_category("bogus") is None


def test_suggest_from_merchant_matches_home_depot_to_supplies():
    assert suggest_from_merchant("HOME DEPOT #0438") == "supplies"
    assert suggest_from_merchant("home depot") == "supplies"
    assert suggest_from_merchant("Home Depot, Scottsdale") == "supplies"


def test_suggest_from_merchant_matches_utilities():
    assert suggest_from_merchant("APS Online Payment") == "utilities"
    assert suggest_from_merchant("Cox Communications") == "utilities"


def test_suggest_from_merchant_matches_repairs():
    assert suggest_from_merchant("Roto-Rooter Services") == "repairs"
    assert suggest_from_merchant("Scottsdale HVAC Pros") == "repairs"


def test_suggest_from_merchant_returns_none_for_unknown():
    assert suggest_from_merchant("Some Random Bodega") is None
    assert suggest_from_merchant("") is None
    assert suggest_from_merchant(None) is None


def test_suggest_is_case_insensitive():
    assert suggest_from_merchant("ALLSTATE INSURANCE CO") == "insurance"


def test_category_ids_in_merchant_patterns_all_exist():
    """Guard against a typo in seed/expense_merchant_patterns.json where a
    rule points to a category that doesn't exist."""
    valid = valid_category_ids()
    for rule in load_merchant_patterns():
        assert rule["category_id"] in valid, (
            f"merchant rule {rule['pattern']!r} → unknown category {rule['category_id']!r}"
        )
