"""Claude vision OCR for receipt images.

One API call per receipt. Uses tool_choice to force Claude to return
structured JSON matching a fixed schema — see `EXTRACT_RECEIPT_TOOL`.
The tool isn't actually executed; its input is the extracted payload.

Category suggestion is part of the same call so we don't double-pay
latency on a second round-trip.
"""

import base64
import logging
from typing import Any, Optional

from anthropic import Anthropic

from config import get_anthropic_key
from expense_categories import valid_category_ids


logger = logging.getLogger(__name__)


# Sonnet is worth the ~4× cost over Haiku for messy thermal receipts —
# see EXPENSES.md §2. Pinned exactly so accuracy regressions don't
# slip in on a model-alias upgrade.
OCR_MODEL = "claude-sonnet-4-6"

# Max tokens for the tool-use reply — schema is small, ~400 tokens more
# than covers it.
_MAX_TOKENS = 1024

_SYSTEM = (
    "You extract structured data from a photo of a retail receipt. "
    "Return your output by calling the extract_receipt tool exactly once. "
    "Never guess a value that isn't visible on the receipt — use null and "
    "mark the relevant confidence field `low` instead. For the category, "
    "pick the Schedule E line the purchase most plausibly belongs to for "
    "a short-term rental; if the receipt is a mix or unclear, suggest "
    "`other` with low confidence."
)


def _tool_schema() -> dict:
    """Build the tool schema with the live category enum.

    The category list lives in seed/expense_categories.json so the
    allowed values here are derived from that source of truth.
    """
    return {
        "name": "extract_receipt",
        "description": (
            "Record the extracted contents of a retail receipt. Call exactly "
            "once with your best structured reading of the image."
        ),
        "input_schema": {
            "type": "object",
            "required": ["merchant_name", "transaction_date", "total"],
            "properties": {
                "merchant_name": {
                    "type": "string",
                    "description": "Business name at the top of the receipt.",
                },
                "merchant_address": {
                    "type": ["string", "null"],
                    "description": "Street address if visible, else null.",
                },
                "transaction_date": {
                    "type": "string",
                    "description": (
                        "The date printed on the receipt, in YYYY-MM-DD. "
                        "If the receipt shows a shorter form (e.g. 04/18/26), "
                        "interpret the year as 20xx."
                    ),
                },
                "subtotal":       {"type": ["string", "null"]},
                "tax":            {"type": ["string", "null"]},
                "tip":            {"type": ["string", "null"]},
                "total":          {"type": "string", "description": "Grand total as printed. Decimal, no currency symbol."},
                "currency":       {"type": "string", "description": "ISO code, default USD."},
                "payment_method": {
                    "type": ["string", "null"],
                    "description": "e.g. 'Visa ****1234' or 'CASH'.",
                },
                "line_items": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "description": {"type": "string"},
                            "qty":         {"type": ["number", "null"]},
                            "amount":      {"type": "string"},
                        },
                    },
                },
                "suggested_category": {
                    "type": "string",
                    "enum": sorted(valid_category_ids()),
                    "description": "Best-fit Schedule E category id.",
                },
                "category_confidence":   {"type": "string", "enum": ["high", "medium", "low"]},
                "extraction_confidence": {"type": "string", "enum": ["high", "medium", "low"]},
                "needs_review_reason": {
                    "type": ["string", "null"],
                    "description": "Short explanation if any field is low confidence.",
                },
            },
        },
    }


def extract_receipt(image_bytes: bytes, media_type: str) -> dict:
    """Run OCR on the given image. Returns the tool input as a dict.

    Raises RuntimeError if Claude fails to call the tool. Never raises on
    low confidence — callers inspect `*_confidence` and set `needs_review`.
    """
    if not image_bytes:
        raise ValueError("image_bytes is empty")
    media_type = _normalize_media_type(media_type)
    if media_type not in _SUPPORTED_MEDIA_TYPES:
        raise ValueError(f"unsupported media_type: {media_type}")

    client = Anthropic(api_key=get_anthropic_key())
    response = client.messages.create(
        model=OCR_MODEL,
        max_tokens=_MAX_TOKENS,
        system=_SYSTEM,
        tools=[_tool_schema()],
        tool_choice={"type": "tool", "name": "extract_receipt"},
        messages=[
            {
                "role": "user",
                "content": [
                    {
                        "type": "image",
                        "source": {
                            "type": "base64",
                            "media_type": media_type,
                            "data": base64.b64encode(image_bytes).decode("ascii"),
                        },
                    },
                    {
                        "type": "text",
                        "text": "Extract this receipt.",
                    },
                ],
            }
        ],
    )

    payload = _first_tool_input(response)
    if payload is None:
        raise RuntimeError("Claude did not call extract_receipt tool")
    return payload


def _first_tool_input(response: Any) -> Optional[dict]:
    """Pull the first tool_use block's input from a messages response."""
    for block in getattr(response, "content", []) or []:
        if getattr(block, "type", None) == "tool_use":
            return dict(getattr(block, "input", {}) or {})
    return None


# Slack uploads come back with ambiguous content-type for HEIC sometimes;
# normalize to what the Anthropic API accepts.
_SUPPORTED_MEDIA_TYPES = {"image/jpeg", "image/png", "image/webp", "image/gif"}


def _normalize_media_type(media_type: str) -> str:
    mt = (media_type or "").lower()
    if mt == "image/jpg":
        return "image/jpeg"
    return mt
