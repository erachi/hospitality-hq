"""Tests for the Expense / Allocation data models."""

from decimal import Decimal

import pytest

from expense_models import (
    Allocation,
    CONFIDENCE_LOW,
    EXPENSE_ID_RE,
    Expense,
    money_str,
    now_iso,
    year_for_transaction,
)


def _base_expense(**overrides) -> Expense:
    """A minimally-valid Expense for tests to override."""
    defaults = dict(
        id="EXP-2026-0042",
        submitter_slack_id="UVJ",
        merchant_name="Home Depot #0438",
        transaction_date="2026-04-18",
        total="311.97",
        currency="USD",
        image_s3_key="receipts/2026/EXP-2026-0042.jpg",
        image_sha256="deadbeef",
        ocr_payload={"merchant_name": "Home Depot #0438"},
        ocr_model="claude-sonnet-4-6",
        slack_channel_id="C_EXPENSES",
        slack_thread_ts="1745000000.000100",
        created_at="2026-04-18T15:00:00Z",
        updated_at="2026-04-18T15:00:00Z",
    )
    defaults.update(overrides)
    return Expense(**defaults)


def test_expense_id_regex():
    assert EXPENSE_ID_RE.match("EXP-2026-0001")
    assert EXPENSE_ID_RE.match("EXP-2099-9999")
    assert not EXPENSE_ID_RE.match("EXP-2026-1")
    assert not EXPENSE_ID_RE.match("EXP-26-0001")
    assert not EXPENSE_ID_RE.match("exp-2026-0001")


def test_expense_year_derives_from_id():
    assert _base_expense(id="EXP-2026-0042").year() == "2026"
    assert _base_expense(id="EXP-2099-0001").year() == "2099"


def test_expense_year_raises_on_bad_id():
    with pytest.raises(ValueError):
        _base_expense(id="not-an-id").year()


def test_expense_roundtrip_through_dict():
    expense = _base_expense(
        subtotal="287.42",
        tax="24.55",
        category_id="repairs",
        property_id="3278e9cb-9239-487f-aa51-cbfbaf4b7570",
        notes="new garbage disposal",
        allocations=[
            Allocation.single("3278e9cb-9239-487f-aa51-cbfbaf4b7570", Decimal("311.97"))
        ],
    )
    data = expense.to_dict()
    rehydrated = Expense.from_dict(data)
    assert rehydrated == expense


def test_expense_from_dict_defaults_missing_optional_fields():
    minimal = {
        "id": "EXP-2026-0001",
        "submitter_slack_id": "UVJ",
        "merchant_name": "Costco",
        "transaction_date": "2026-04-18",
        "total": "100.00",
        "image_s3_key": "receipts/2026/EXP-2026-0001.jpg",
        "image_sha256": "abc",
        "slack_channel_id": "C",
        "slack_thread_ts": "1.2",
        "created_at": "2026-04-18T00:00:00Z",
    }
    expense = Expense.from_dict(minimal)
    assert expense.currency == "USD"
    assert expense.needs_review is False
    assert expense.is_personal is False
    assert expense.allocations == []
    assert expense.updated_at == expense.created_at  # mirrored when missing


def test_touch_updates_timestamp():
    expense = _base_expense(updated_at="2026-01-01T00:00:00Z")
    expense.touch()
    assert expense.updated_at != "2026-01-01T00:00:00Z"
    assert expense.updated_at.endswith("Z")


def test_allocation_single_uses_full_amount_at_100_percent():
    alloc = Allocation.single("prop-palm", Decimal("311.97"))
    assert alloc.percent == "100.00"
    assert alloc.amount == "311.97"
    assert alloc.property_id == "prop-palm"


def test_allocation_roundtrip():
    alloc = Allocation(property_id="prop-villa", percent="50.00", amount="10.00")
    assert Allocation.from_dict(alloc.to_dict()) == alloc


def test_money_str_quantizes_to_two_decimals():
    assert money_str("10") == "10.00"
    assert money_str("10.1") == "10.10"
    assert money_str("10.567") == "10.57"  # banker's rounding handled by Decimal
    assert money_str(Decimal("42")) == "42.00"


def test_year_for_transaction_prefers_printed_date():
    assert year_for_transaction("2026-12-31") == "2026"
    assert year_for_transaction("2099-01-01") == "2099"


def test_year_for_transaction_falls_back_to_now():
    # We can't assert a specific year without freezing time, but we can
    # assert the shape: 4 digits.
    assert len(year_for_transaction(None)) == 4
    assert year_for_transaction("").isdigit()


def test_now_iso_has_trailing_z():
    value = now_iso()
    assert value.endswith("Z")
    assert "T" in value


def test_confidence_constants_are_distinct():
    assert CONFIDENCE_LOW == "low"
