"""Tests for the S3-backed ExpenseStore."""

import json
from decimal import Decimal

from expense_models import Allocation, Expense
from expense_store import ExpenseStore


def _make_expense(**overrides) -> Expense:
    defaults = dict(
        id="EXP-2026-0001",
        submitter_slack_id="UVJ",
        merchant_name="Home Depot",
        transaction_date="2026-04-18",
        total="311.97",
        currency="USD",
        image_s3_key="receipts/2026/EXP-2026-0001.jpg",
        image_sha256="deadbeef",
        ocr_payload={},
        ocr_model="test",
        slack_channel_id="C_EXPENSES",
        slack_thread_ts="1.2",
        created_at="2026-04-18T00:00:00Z",
        updated_at="2026-04-18T00:00:00Z",
    )
    defaults.update(overrides)
    return Expense(**defaults)


def test_put_and_get_roundtrip(expenses_bucket):
    store = ExpenseStore()
    expense = _make_expense(
        id="EXP-2026-0007",
        category_id="repairs",
        property_id="3278e9cb-9239-487f-aa51-cbfbaf4b7570",
        allocations=[
            Allocation.single("3278e9cb-9239-487f-aa51-cbfbaf4b7570", Decimal("311.97"))
        ],
    )

    store.put(expense)
    fetched = store.get("EXP-2026-0007")

    assert fetched is not None
    assert fetched.id == "EXP-2026-0007"
    assert fetched.category_id == "repairs"
    assert len(fetched.allocations) == 1
    assert fetched.allocations[0].percent == "100.00"


def test_get_missing_returns_none(expenses_bucket):
    assert ExpenseStore().get("EXP-2026-9999") is None


def test_get_rejects_badly_formatted_id(expenses_bucket):
    assert ExpenseStore().get("not-a-real-id") is None
    assert ExpenseStore().get("") is None


def test_put_writes_json_to_expected_key(expenses_bucket):
    store = ExpenseStore()
    store.put(_make_expense(id="EXP-2026-0003"))

    obj = expenses_bucket.get_object(
        Bucket="hospitality-hq-expenses-test",
        Key="expenses/2026/EXP-2026-0003.json",
    )
    data = json.loads(obj["Body"].read())
    assert data["id"] == "EXP-2026-0003"


def test_put_touches_updated_at(expenses_bucket):
    store = ExpenseStore()
    expense = _make_expense(updated_at="2020-01-01T00:00:00Z")
    store.put(expense)
    fetched = store.get(expense.id)
    assert fetched.updated_at != "2020-01-01T00:00:00Z"


def test_list_for_year_returns_only_matching_year(expenses_bucket):
    store = ExpenseStore()
    store.put(_make_expense(id="EXP-2026-0001"))
    store.put(_make_expense(id="EXP-2026-0002"))
    store.put(_make_expense(id="EXP-2025-0001"))

    y2026 = store.list_for_year("2026")
    y2025 = store.list_for_year("2025")

    assert {e.id for e in y2026} == {"EXP-2026-0001", "EXP-2026-0002"}
    assert {e.id for e in y2025} == {"EXP-2025-0001"}


def test_next_id_increments_from_existing(expenses_bucket):
    store = ExpenseStore()
    assert store.next_id("2026") == "EXP-2026-0001"

    store.put(_make_expense(id="EXP-2026-0001"))
    assert store.next_id("2026") == "EXP-2026-0002"

    store.put(_make_expense(id="EXP-2026-0002"))
    assert store.next_id("2026") == "EXP-2026-0003"


def test_next_id_year_scoped(expenses_bucket):
    store = ExpenseStore()
    store.put(_make_expense(id="EXP-2025-0042"))
    assert store.next_id("2026") == "EXP-2026-0001"


def test_put_receipt_image_without_retention(expenses_bucket):
    store = ExpenseStore()
    key = store.put_receipt_image(
        year="2026",
        expense_id="EXP-2026-0001",
        body=b"fake image bytes",
        content_type="image/jpeg",
    )
    assert key == "receipts/2026/EXP-2026-0001.jpg"

    obj = expenses_bucket.get_object(
        Bucket="hospitality-hq-expenses-test", Key=key
    )
    assert obj["Body"].read() == b"fake image bytes"
    assert obj["ContentType"] == "image/jpeg"


def test_put_receipt_image_maps_content_type_to_extension(expenses_bucket):
    store = ExpenseStore()
    key = store.put_receipt_image("2026", "EXP-2026-0002", b"x", "image/png")
    assert key.endswith(".png")

    key = store.put_receipt_image("2026", "EXP-2026-0003", b"x", "image/heic")
    assert key.endswith(".heic")

    # Unknown content-type falls back to .bin — visible on disk and
    # easily remediated without losing the image.
    key = store.put_receipt_image("2026", "EXP-2026-0004", b"x", "application/weird")
    assert key.endswith(".bin")


def test_put_thumbnail(expenses_bucket):
    store = ExpenseStore()
    key = store.put_thumbnail("2026", "EXP-2026-0001", b"thumb")
    assert key == "thumbs/2026/EXP-2026-0001.jpg"

    obj = expenses_bucket.get_object(
        Bucket="hospitality-hq-expenses-test", Key=key
    )
    assert obj["Body"].read() == b"thumb"


def test_slack_index_roundtrip(expenses_bucket):
    store = ExpenseStore()
    expense = _make_expense(id="EXP-2026-0020")
    store.put(expense)
    store.put_slack_index("1745000000.000999", expense.id)

    fetched = store.get_expense_by_thread("1745000000.000999")
    assert fetched is not None
    assert fetched.id == "EXP-2026-0020"


def test_get_by_thread_returns_none_when_unknown(expenses_bucket):
    assert ExpenseStore().get_expense_by_thread("never-existed") is None


def test_put_slack_index_rejects_empty_args(expenses_bucket):
    # Should not raise; silently no-op.
    ExpenseStore().put_slack_index("", "EXP-2026-0001")
    ExpenseStore().put_slack_index("1.2", "")
    # Nothing to assert beyond "no exception" — the pointer simply isn't written.


def test_presigned_url_has_bucket_and_key(expenses_bucket):
    store = ExpenseStore()
    url = store.presigned_url("receipts/2026/EXP-2026-0001.jpg")
    assert "hospitality-hq-expenses-test" in url
    assert "receipts/2026/EXP-2026-0001.jpg" in url
