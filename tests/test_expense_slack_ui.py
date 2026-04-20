"""Tests for Block Kit builders in expense_slack_ui."""

from expense_models import CONFIDENCE_HIGH, CONFIDENCE_LOW, Expense
from expense_slack_ui import (
    ACTION_CATEGORY_SELECT,
    ACTION_FILE_IT,
    ACTION_PROPERTY_SELECT,
    ACTION_SKIP,
    ACTION_SPLIT,
    BLOCK_ACTIONS,
    BLOCK_CATEGORY,
    BLOCK_PROPERTY,
    build_error_card,
    build_extracted_card,
    build_filed_card,
)


_PROPERTIES = [
    {"id": "3278e9cb-9239-487f-aa51-cbfbaf4b7570", "name": "The Palm Club", "slug": "palm"},
    {"id": "f8236d9d-988a-4192-9d16-2927b0b9ad8e", "name": "Villa Bougainvillea", "slug": "villa"},
]


def _expense(**overrides) -> Expense:
    defaults = dict(
        id="EXP-2026-0042",
        submitter_slack_id="UVJ",
        merchant_name="Home Depot #0438",
        transaction_date="2026-04-18",
        total="311.97",
        currency="USD",
        image_s3_key="receipts/2026/EXP-2026-0042.jpg",
        image_sha256="abc",
        ocr_payload={},
        ocr_model="claude-sonnet-4-6",
        slack_channel_id="C_EXPENSES",
        slack_thread_ts="1745000000.000100",
        created_at="2026-04-18T15:00:00Z",
        updated_at="2026-04-18T15:00:00Z",
        subtotal="287.42",
        tax="24.55",
        category_id="repairs",
        ocr_extraction_confidence=CONFIDENCE_HIGH,
        ocr_category_confidence=CONFIDENCE_HIGH,
    )
    defaults.update(overrides)
    return Expense(**defaults)


def _find(blocks, block_id_prefix):
    """Find a block by block_id prefix — dropdown blocks now suffix the
    expense id (e.g. `expense_property_block:EXP-2026-0042`), so exact
    equality no longer works for them."""
    for b in blocks:
        bid = b.get("block_id") or ""
        if bid == block_id_prefix or bid.startswith(block_id_prefix + ":"):
            return b
    return None


def _actions(blocks):
    return _find(blocks, BLOCK_ACTIONS)["elements"]


def test_extracted_card_has_all_expected_blocks():
    blocks, fallback = build_extracted_card(expense=_expense(), properties=_PROPERTIES)

    # Header + merchant/date + amount + divider + property + category + id + actions
    assert any(b.get("type") == "header" for b in blocks)
    assert _find(blocks, BLOCK_PROPERTY) is not None
    assert _find(blocks, BLOCK_CATEGORY) is not None
    assert _find(blocks, BLOCK_ACTIONS) is not None
    assert "Home Depot" in fallback
    assert "EXP-2026-0042" in fallback


def test_property_dropdown_lists_all_known_properties():
    blocks, _ = build_extracted_card(expense=_expense(), properties=_PROPERTIES)
    prop_block = _find(blocks, BLOCK_PROPERTY)
    options = prop_block["accessory"]["options"]
    assert {o["value"] for o in options} == {p["id"] for p in _PROPERTIES}


def test_property_dropdown_preselects_when_expense_has_property():
    expense = _expense(property_id="3278e9cb-9239-487f-aa51-cbfbaf4b7570")
    blocks, _ = build_extracted_card(expense=expense, properties=_PROPERTIES)
    prop_block = _find(blocks, BLOCK_PROPERTY)
    initial = prop_block["accessory"].get("initial_option")
    assert initial is not None
    assert initial["value"] == "3278e9cb-9239-487f-aa51-cbfbaf4b7570"


def test_property_dropdown_no_preselect_when_unset():
    blocks, _ = build_extracted_card(expense=_expense(property_id=None), properties=_PROPERTIES)
    prop_block = _find(blocks, BLOCK_PROPERTY)
    assert "initial_option" not in prop_block["accessory"]


def test_category_dropdown_preselects_from_expense():
    blocks, _ = build_extracted_card(
        expense=_expense(category_id="supplies"), properties=_PROPERTIES
    )
    cat_block = _find(blocks, BLOCK_CATEGORY)
    assert cat_block["accessory"]["initial_option"]["value"] == "supplies"


def test_low_category_confidence_adds_red_dot_to_category_label():
    expense = _expense(ocr_category_confidence=CONFIDENCE_LOW)
    blocks, _ = build_extracted_card(expense=expense, properties=_PROPERTIES)
    cat_block = _find(blocks, BLOCK_CATEGORY)
    assert "🔴" in cat_block["text"]["text"]
    assert "please confirm" in cat_block["text"]["text"].lower()


def test_high_confidence_has_no_red_dot():
    blocks, _ = build_extracted_card(expense=_expense(), properties=_PROPERTIES)
    merchant_block = blocks[1]  # header at 0, merchant section at 1
    assert "🔴" not in merchant_block["text"]["text"]


def test_action_buttons_carry_expense_id_as_value():
    blocks, _ = build_extracted_card(expense=_expense(id="EXP-2026-9999"), properties=_PROPERTIES)
    actions = _actions(blocks)
    by_action_id = {a["action_id"]: a for a in actions}

    assert by_action_id[ACTION_FILE_IT]["value"] == "EXP-2026-9999"
    assert by_action_id[ACTION_SPLIT]["value"] == "EXP-2026-9999"
    assert by_action_id[ACTION_SKIP]["value"] == "EXP-2026-9999"


def test_action_buttons_present_in_order():
    blocks, _ = build_extracted_card(expense=_expense(), properties=_PROPERTIES)
    actions = _actions(blocks)
    ids = [a["action_id"] for a in actions]
    assert ids == [ACTION_FILE_IT, ACTION_SPLIT, ACTION_SKIP]


def test_needs_review_adds_context_block():
    expense = _expense(
        needs_review=True,
        review_reason="total was partially obscured",
    )
    blocks, _ = build_extracted_card(expense=expense, properties=_PROPERTIES)
    texts = [
        el["text"]
        for b in blocks
        if b["type"] == "context"
        for el in b.get("elements", [])
    ]
    assert any("Flagged for review" in t for t in texts)


def test_dropdown_action_ids_stable():
    """These ids are the contract between the card and the interactions
    handler (coming in PR 3). A rename here silently breaks card input."""
    blocks, _ = build_extracted_card(expense=_expense(), properties=_PROPERTIES)
    assert _find(blocks, BLOCK_PROPERTY)["accessory"]["action_id"] == ACTION_PROPERTY_SELECT
    assert _find(blocks, BLOCK_CATEGORY)["accessory"]["action_id"] == ACTION_CATEGORY_SELECT


def test_filed_card_shows_filed_state():
    blocks, fallback = build_filed_card(
        expense=_expense(),
        property_display_name="The Palm Club",
        category_display_name="Repairs",
    )
    text = blocks[0]["text"]["text"]
    assert "✅ Filed EXP-2026-0042" in text
    assert "The Palm Club" in text
    assert "Repairs" in text
    assert "$311.97" in text
    assert "Filed" in fallback


def test_filed_card_handles_missing_property_and_category():
    blocks, _ = build_filed_card(
        expense=_expense(),
        property_display_name=None,
        category_display_name=None,
    )
    text = blocks[0]["text"]["text"]
    assert "—" in text  # shown for missing property/category


def test_error_card_shows_reason():
    blocks, fallback = build_error_card(reason="OCR blew up")
    text = blocks[0]["text"]["text"]
    assert "OCR blew up" in text
    assert "Couldn't read" in text
    assert "OCR blew up" in fallback


def test_extracted_card_escapes_slack_markup_in_merchant_name():
    """Protect against caption/merchant containing <, >, & that would
    otherwise break Slack mrkdwn."""
    expense = _expense(merchant_name="<script>alert('x')</script> & Co")
    blocks, _ = build_extracted_card(expense=expense, properties=_PROPERTIES)
    merchant_block_text = blocks[1]["text"]["text"]
    assert "<script>" not in merchant_block_text
    assert "&lt;script&gt;" in merchant_block_text
    assert "&amp;" in merchant_block_text
