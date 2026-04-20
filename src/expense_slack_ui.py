"""Block Kit UI for the expense-capture workflow.

The confirmation card is posted in-thread right after OCR finishes. Low-
confidence fields get a red-dot prefix so the eye is drawn to them first.
Property and category dropdowns are populated from seed/.
"""

from typing import Optional

from expense_categories import load_categories
from expense_models import CONFIDENCE_LOW, Expense


# Block action ids and block ids. Interactions in PR 3 dispatch on these.
ACTION_FILE_IT = "expense_file_it"
ACTION_SKIP = "expense_skip"
ACTION_SPLIT = "expense_split"
ACTION_PROPERTY_SELECT = "expense_property_select"
ACTION_CATEGORY_SELECT = "expense_category_select"

BLOCK_PROPERTY = "expense_property_block"
BLOCK_CATEGORY = "expense_category_block"
BLOCK_ACTIONS = "expense_actions_block"

_LOW_CONFIDENCE_DOT = "🔴 "


def _dot(is_low: bool) -> str:
    return _LOW_CONFIDENCE_DOT if is_low else ""


def build_extracted_card(
    *,
    expense: Expense,
    properties: list[dict],
) -> tuple[list[dict], str]:
    """Build the confirmation card for a freshly-extracted expense.

    Returns (blocks, fallback_text). fallback_text is what Slack shows in
    notifications and screen readers when blocks can't render.
    """
    extraction_low = expense.ocr_extraction_confidence == CONFIDENCE_LOW
    category_low = expense.ocr_category_confidence == CONFIDENCE_LOW

    merchant_line = f"{_dot(extraction_low)}*{_escape(expense.merchant_name)}*"
    if expense.merchant_name:
        merchant_line += "\n"
    merchant_line += f"_{expense.transaction_date}_"

    amount_line = f"{_dot(extraction_low)}*${expense.total}*"
    if expense.subtotal or expense.tax:
        parts = []
        if expense.subtotal:
            parts.append(f"subtotal ${expense.subtotal}")
        if expense.tax:
            parts.append(f"tax ${expense.tax}")
        amount_line += f"  ({' · '.join(parts)})"

    blocks: list[dict] = [
        {
            "type": "header",
            "text": {"type": "plain_text", "text": "🧾 Receipt captured", "emoji": True},
        },
        {
            "type": "section",
            "text": {"type": "mrkdwn", "text": merchant_line},
        },
        {
            "type": "section",
            "text": {"type": "mrkdwn", "text": amount_line},
        },
        {"type": "divider"},
        _property_dropdown_block(
            properties=properties,
            selected_id=expense.property_id,
        ),
        _category_dropdown_block(
            selected_id=expense.category_id,
            low_confidence=category_low,
        ),
    ]

    if expense.notes:
        blocks.append(
            {
                "type": "section",
                "text": {"type": "mrkdwn", "text": f"_Note:_ {_escape(expense.notes)}"},
            }
        )

    if expense.needs_review and expense.review_reason:
        blocks.append(
            {
                "type": "context",
                "elements": [
                    {
                        "type": "mrkdwn",
                        "text": f"⚠️ *Flagged for review:* {_escape(expense.review_reason)}",
                    }
                ],
            }
        )

    blocks.append(
        {
            "type": "context",
            "elements": [
                {
                    "type": "mrkdwn",
                    "text": (
                        f"`{expense.id}` · submitted by <@{expense.submitter_slack_id}>"
                    ),
                }
            ],
        }
    )

    blocks.append(_action_buttons_block(expense_id=expense.id))

    fallback = (
        f"🧾 {expense.merchant_name} · {expense.transaction_date} · ${expense.total} "
        f"({expense.id})"
    )
    return blocks, fallback


def build_filed_card(
    *,
    expense: Expense,
    property_display_name: Optional[str],
    category_display_name: Optional[str],
) -> tuple[list[dict], str]:
    """Replace the confirmation card once the submitter clicks File it.

    Collapses dropdowns and action buttons into a static filed-status
    block. Original message is updated in place via chat.update.
    """
    prop = property_display_name or "—"
    cat = category_display_name or "—"
    lines = [
        f"*✅ Filed {expense.id}*",
        f"{_escape(expense.merchant_name)} · {expense.transaction_date} · *${expense.total}*",
        f"{prop} · {cat}",
    ]
    if expense.notes:
        lines.append(f"_{_escape(expense.notes)}_")

    blocks: list[dict] = [
        {"type": "section", "text": {"type": "mrkdwn", "text": "\n".join(lines)}},
        {
            "type": "context",
            "elements": [
                {
                    "type": "mrkdwn",
                    "text": f"submitted by <@{expense.submitter_slack_id}>",
                }
            ],
        },
    ]
    fallback = f"✅ Filed {expense.id} · {expense.merchant_name} · ${expense.total}"
    return blocks, fallback


def build_error_card(*, reason: str) -> tuple[list[dict], str]:
    """Posted when ingest fails before an Expense could be persisted."""
    blocks = [
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": f":warning: *Couldn't read this receipt* — {_escape(reason)}\n\nTry a clearer photo or file it manually.",
            },
        }
    ]
    return blocks, f"Couldn't read this receipt: {reason}"


# ─── Dropdown helpers ────────────────────────────────────────────────────


def _property_dropdown_block(*, properties: list[dict], selected_id: Optional[str]) -> dict:
    options = [
        {
            "text": {"type": "plain_text", "text": p.get("name", p.get("id", "?")), "emoji": True},
            "value": p["id"],
        }
        for p in properties
    ]
    element = {
        "type": "static_select",
        "action_id": ACTION_PROPERTY_SELECT,
        "placeholder": {"type": "plain_text", "text": "Pick a property", "emoji": True},
        "options": options,
    }
    if selected_id:
        match = next((p for p in properties if p["id"] == selected_id), None)
        if match:
            element["initial_option"] = {
                "text": {"type": "plain_text", "text": match.get("name", match["id"]), "emoji": True},
                "value": match["id"],
            }

    return {
        "type": "section",
        "block_id": BLOCK_PROPERTY,
        "text": {"type": "mrkdwn", "text": "*Property*"},
        "accessory": element,
    }


def _category_dropdown_block(*, selected_id: Optional[str], low_confidence: bool) -> dict:
    categories = load_categories()
    options = [
        {
            "text": {"type": "plain_text", "text": c["display_name"], "emoji": True},
            "value": c["id"],
        }
        for c in categories
    ]
    element = {
        "type": "static_select",
        "action_id": ACTION_CATEGORY_SELECT,
        "placeholder": {"type": "plain_text", "text": "Pick a category", "emoji": True},
        "options": options,
    }
    if selected_id:
        match = next((c for c in categories if c["id"] == selected_id), None)
        if match:
            element["initial_option"] = {
                "text": {"type": "plain_text", "text": match["display_name"], "emoji": True},
                "value": match["id"],
            }

    label = "*Category*"
    if low_confidence:
        label = f"{_LOW_CONFIDENCE_DOT}*Category* _(please confirm — OCR wasn't sure)_"

    return {
        "type": "section",
        "block_id": BLOCK_CATEGORY,
        "text": {"type": "mrkdwn", "text": label},
        "accessory": element,
    }


def _action_buttons_block(*, expense_id: str) -> dict:
    return {
        "type": "actions",
        "block_id": BLOCK_ACTIONS,
        "elements": [
            {
                "type": "button",
                "style": "primary",
                "text": {"type": "plain_text", "text": "✅  File it", "emoji": True},
                "action_id": ACTION_FILE_IT,
                "value": expense_id,
            },
            {
                "type": "button",
                "text": {"type": "plain_text", "text": "✂️  Split", "emoji": True},
                "action_id": ACTION_SPLIT,
                "value": expense_id,
            },
            {
                "type": "button",
                "style": "danger",
                "text": {"type": "plain_text", "text": "🗑  Skip", "emoji": True},
                "action_id": ACTION_SKIP,
                "value": expense_id,
            },
        ],
    }


def _escape(text: str) -> str:
    """Slack mrkdwn escaping for user-provided strings."""
    if not text:
        return ""
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
