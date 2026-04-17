"""Loads the per-property curated knowledge base from YAML files.

Each property has a YAML file under src/knowledge_base/ with two sections:
  - static: rock-solid facts that don't change (check-in time, wifi, instructions)
  - dynamic: curated from past guest conversations (FAQs, precedents)

The loader maps property UUIDs to YAML filenames and caches results per
Lambda cold start.
"""

import logging
import os
from functools import lru_cache
from typing import Optional

import yaml

logger = logging.getLogger(__name__)

# Directory containing the YAML files — co-located with this module
_KB_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "knowledge_base")

# Map property UUID → YAML filename (without extension).
# To add a new property, drop a YAML file under src/knowledge_base/ and add
# the UUID → filename mapping here.
UUID_TO_FILENAME = {
    "f8236d9d-988a-4192-9d16-2927b0b9ad8e": "villa_bougainvillea",
    "3278e9cb-9239-487f-aa51-cbfbaf4b7570": "palm_club",
}


@lru_cache(maxsize=8)
def load_kb(property_id: str) -> dict:
    """Load the knowledge base YAML for a property.

    Returns an empty dict if the property is unknown or the file is missing.
    """
    if not property_id:
        return {}

    filename = UUID_TO_FILENAME.get(property_id)
    if not filename:
        logger.info(f"No knowledge base mapping for property {property_id}")
        return {}

    path = os.path.join(_KB_DIR, f"{filename}.yaml")
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
            return data if isinstance(data, dict) else {}
    except FileNotFoundError:
        logger.warning(f"Knowledge base file not found: {path}")
        return {}
    except yaml.YAMLError as e:
        logger.error(f"Failed to parse knowledge base {path}: {e}")
        return {}


def get_property_name(property_id: str) -> Optional[str]:
    """Convenience: return the canonical property name from the KB, if known."""
    kb = load_kb(property_id)
    return kb.get("property", {}).get("name")


def format_for_claude(kb: dict) -> str:
    """Render the KB as a structured plain-text block to include in a Claude prompt.

    The output has clearly labeled sections so Claude knows which parts are
    authoritative (static) vs. historical examples (dynamic).
    """
    if not kb:
        return ""

    parts = []

    prop = kb.get("property", {})
    if prop:
        header = f"PROPERTY: {prop.get('name', 'Unknown')}"
        if prop.get("nickname"):
            header += f" ({prop['nickname']})"
        if prop.get("address"):
            header += f"\nAddress: {prop['address']}"
        parts.append(header)

    static = kb.get("static") or {}
    static_block = _render_dict(static)
    if static_block.strip():
        parts.append(
            "═══ AUTHORITATIVE FACTS (cite these exactly — do not contradict) ═══\n"
            + static_block
        )

    dynamic = kb.get("dynamic") or {}
    faqs = dynamic.get("faqs") or []
    if faqs:
        faq_lines = []
        for faq in faqs:
            q = faq.get("question", "").strip()
            a = faq.get("answer", "").strip()
            if q and a:
                faq_lines.append(f"Q: {q}\nA: {a}")
        if faq_lines:
            parts.append(
                "═══ PAST GUEST Q&A (how we've answered similar questions before) ═══\n"
                + "\n\n".join(faq_lines)
            )

    precedents = dynamic.get("precedents") or []
    if precedents:
        prec_lines = []
        for p in precedents:
            situation = p.get("situation", "").strip()
            action = p.get("what_we_did", "").strip()
            if situation and action:
                prec_lines.append(f"Situation: {situation}\nWhat we did: {action}")
        if prec_lines:
            parts.append(
                "═══ PRECEDENTS (how we've handled similar situations) ═══\n"
                + "\n\n".join(prec_lines)
            )

    return "\n\n".join(parts)


def _render_dict(data, indent: int = 0) -> str:
    """Recursively render a dict/list into indented plain text.

    Skips empty values and TODO placeholders so Claude doesn't see them.
    """
    lines = []
    pad = "  " * indent

    if isinstance(data, dict):
        for key, value in data.items():
            if _is_empty_or_todo(value):
                continue
            if isinstance(value, (dict, list)) and value:
                lines.append(f"{pad}{_humanize(key)}:")
                nested = _render_dict(value, indent + 1)
                if nested:
                    lines.append(nested)
            else:
                lines.append(f"{pad}{_humanize(key)}: {value}")
    elif isinstance(data, list):
        for item in data:
            if _is_empty_or_todo(item):
                continue
            if isinstance(item, (dict, list)):
                nested = _render_dict(item, indent + 1)
                if nested:
                    lines.append(f"{pad}-")
                    lines.append(nested)
            else:
                lines.append(f"{pad}- {item}")
    else:
        if not _is_empty_or_todo(data):
            lines.append(f"{pad}{data}")

    return "\n".join(lines)


def _is_empty_or_todo(value) -> bool:
    """Treat empty containers and TODO placeholders as absent."""
    if value is None:
        return True
    if isinstance(value, (dict, list)) and not value:
        return True
    if isinstance(value, str):
        stripped = value.strip()
        if not stripped:
            return True
        if stripped.upper().startswith("TODO"):
            return True
    return False


def _humanize(key: str) -> str:
    """Convert snake_case key to Title Case for readable output."""
    return key.replace("_", " ").title()
