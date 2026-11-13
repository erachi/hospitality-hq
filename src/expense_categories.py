"""Schedule E category taxonomy + merchant-name → category rules.

Both tables ship as JSON files in src/seed/ and are bundled with the
Lambda by virtue of being inside the CodeUri. Loaded once per cold
start via lru_cache. Category IDs map 1:1 to Schedule E lines (see
EXPENSES.md §6) — do not invent subcategories.
"""

import json
import logging
import os
from functools import lru_cache

logger = logging.getLogger(__name__)


def _default_seed_dir() -> str:
    """Locate the directory holding expense_categories.json and
    expense_merchant_patterns.json.

    The files live at `src/seed/` so SAM's `CodeUri: src/` picks them
    up automatically. For local dev we also fall back to the legacy
    `../seed/` location (relative to this file) for any files that
    haven't been migrated. A `SEED_DIR` env var overrides both for
    tests / unusual layouts.
    """
    override = os.environ.get("SEED_DIR")
    if override:
        return override
    here = os.path.dirname(__file__)
    primary = os.path.normpath(os.path.join(here, "seed"))
    if os.path.isdir(primary):
        return primary
    # Legacy location — kept for backwards compatibility with any
    # checkout that still has seed/ at the repo root.
    return os.path.normpath(os.path.join(here, "..", "seed"))


@lru_cache(maxsize=1)
def load_categories() -> list[dict]:
    """Schedule E category taxonomy. Each row: id, display_name, schedule_e_line."""
    return _load_seed("expense_categories.json")


@lru_cache(maxsize=1)
def load_merchant_patterns() -> list[dict]:
    """Merchant → category rules. Each row: pattern, category_id, note."""
    return _load_seed("expense_merchant_patterns.json")


def get_category(category_id: str) -> dict | None:
    for c in load_categories():
        if c["id"] == category_id:
            return c
    return None


def valid_category_ids() -> set[str]:
    return {c["id"] for c in load_categories()}


def suggest_from_merchant(merchant_name: str) -> str | None:
    """Return the best category_id for a merchant name, or None.

    Case-insensitive substring match; first matching rule wins.
    """
    if not merchant_name:
        return None
    haystack = merchant_name.lower()
    for rule in load_merchant_patterns():
        if rule["pattern"].lower() in haystack:
            return rule["category_id"]
    return None


def _load_seed(filename: str) -> list[dict]:
    path = os.path.join(_default_seed_dir(), filename)
    try:
        with open(path) as f:
            return json.load(f)
    except FileNotFoundError:
        logger.warning(f"Seed file not found: {path}")
        return []
