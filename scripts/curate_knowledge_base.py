"""One-shot curation script: extract FAQs from historical guest conversations.

Usage:
    python scripts/curate_knowledge_base.py --property villa_bougainvillea [--dry-run] [--limit N]

What it does:
  1. Fetches all reservations for the given property (paginated, all statuses)
  2. For each reservation, fetches the full message thread
  3. Batches threads to Claude Sonnet to extract FAQ-worthy Q&A pairs
  4. Deduplicates across all extractions
  5. Merges into src/knowledge_base/<property>.yaml under dynamic.faqs
  6. Prints a diff and (unless --dry-run) writes the YAML

Credentials are loaded from SSM (via src/config.py) OR from env vars:
    HOSPITABLE_API_TOKEN, ANTHROPIC_API_KEY

This script is meant to be run LOCALLY, not in Lambda. Requires internet access
and the ability to read the SSM parameters (or set env vars with the tokens).
"""

import argparse
import json
import logging
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

# Make src/ importable
SRC = Path(__file__).resolve().parent.parent / "src"
sys.path.insert(0, str(SRC))

import yaml  # noqa: E402
import anthropic  # noqa: E402

from hospitable_client import HospitableClient  # noqa: E402
from config import get_anthropic_key, DRAFT_MODEL  # noqa: E402
from knowledge_base_loader import UUID_TO_FILENAME  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

# Reverse the UUID→filename map so users can pass the friendly name
FILENAME_TO_UUID = {v: k for k, v in UUID_TO_FILENAME.items()}

KB_DIR = SRC / "knowledge_base"

EXTRACTION_PROMPT = """You are extracting reusable FAQ knowledge from guest conversations at a short-term rental.

You'll receive a conversation thread between a guest and the host. Extract any question-answer pairs that would be useful to reuse in the future — things like:
  - Check-in/check-out logistics
  - Amenity questions (pool, wifi, parking, firepit, BBQ, etc.)
  - Common issues and how they were resolved
  - Policies (pets, parties, extra guests, etc.)

Output STRICT JSON. No prose, no markdown fences. Schema:
{
  "faqs": [
    {
      "topic": "short_snake_case_topic",
      "question": "Paraphrased guest question (remove personal details)",
      "answer": "The canonical answer the host gave (clean, no personal details)"
    }
  ],
  "precedents": [
    {
      "situation": "Short description of an issue/event",
      "what_we_did": "How it was resolved"
    }
  ]
}

Rules:
- Skip greetings, thank-yous, and one-off chitchat.
- Skip personal details (guest names, reservation IDs, phone numbers, specific dates).
- If the thread has nothing useful, return {"faqs": [], "precedents": []}.
- Only extract items where the host actually answered — don't extract unanswered questions.
"""

DEDUP_PROMPT = """You'll receive a JSON list of FAQ items extracted from many guest conversations. There will be duplicates and near-duplicates.

Deduplicate and merge them into a clean canonical list. Rules:
  - Merge similar questions into one entry, keeping the clearest, most complete answer.
  - Keep similar but distinct questions separate.
  - Preserve the topic field; if multiple items share a topic, use the most descriptive one.
  - Output STRICT JSON. No prose, no markdown fences. Same schema:
    {"faqs": [...], "precedents": [...]}
"""


def fetch_all_reservations(hospitable: HospitableClient, property_uuid: str) -> list[dict]:
    """Fetch all reservations (no date/status filter) for a property."""
    logger.info(f"Fetching all reservations for {property_uuid}")
    all_reservations = []
    page = 1
    while True:
        data = hospitable._get(
            "/reservations",
            params={
                "properties[]": property_uuid,
                "include": "guest",
                "per_page": 50,
                "page": page,
            },
        )
        reservations = data.get("data", [])
        all_reservations.extend(reservations)
        meta = data.get("meta", {})
        if meta.get("current_page", 1) >= meta.get("last_page", 1):
            break
        page += 1
    logger.info(f"Fetched {len(all_reservations)} total reservations")
    return all_reservations


def format_thread(messages: list[dict]) -> str:
    """Render a message thread as plain text for Claude to read."""
    lines = []
    for msg in messages:
        sender = msg.get("sender_type", "unknown")
        body = (msg.get("body") or "").strip()
        if not body:
            continue
        label = "GUEST" if sender == "guest" else "HOST"
        lines.append(f"[{label}] {body}")
    return "\n".join(lines)


def extract_from_thread(
    client: anthropic.Anthropic, thread_text: str, res_id: str
) -> dict:
    """Ask Claude to extract FAQs from a single conversation thread."""
    resp = client.messages.create(
        model=DRAFT_MODEL,
        max_tokens=2000,
        system=EXTRACTION_PROMPT,
        messages=[{"role": "user", "content": thread_text}],
    )
    raw = resp.content[0].text.strip()
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        logger.warning(f"Could not parse extraction for reservation {res_id}")
        return {"faqs": [], "precedents": []}

    # Attach source reservation to each item for provenance
    for faq in parsed.get("faqs", []) or []:
        faq["source_reservations"] = [res_id]
        faq["last_seen"] = datetime.now(timezone.utc).date().isoformat()
    for prec in parsed.get("precedents", []) or []:
        prec["source_reservations"] = [res_id]

    return parsed


def dedupe(client: anthropic.Anthropic, items: dict) -> dict:
    """Ask Claude to deduplicate a list of extracted items."""
    if not items.get("faqs") and not items.get("precedents"):
        return items
    resp = client.messages.create(
        model=DRAFT_MODEL,
        max_tokens=8000,
        system=DEDUP_PROMPT,
        messages=[{"role": "user", "content": json.dumps(items)}],
    )
    raw = resp.content[0].text.strip()
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        logger.error("Could not parse dedup output — returning original items")
        return items


def load_existing_kb(filename: str) -> dict:
    path = KB_DIR / f"{filename}.yaml"
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def write_kb(filename: str, kb: dict) -> None:
    path = KB_DIR / f"{filename}.yaml"
    with open(path, "w", encoding="utf-8") as f:
        yaml.safe_dump(kb, f, sort_keys=False, allow_unicode=True, width=100)
    logger.info(f"Wrote {path}")


def main():
    parser = argparse.ArgumentParser(description="Curate per-property knowledge base from guest history")
    parser.add_argument(
        "--property",
        required=True,
        choices=list(FILENAME_TO_UUID.keys()),
        help="Property filename (without .yaml)",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Optional cap on number of reservations to process (useful for testing)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print extracted items but don't write the YAML",
    )
    args = parser.parse_args()

    property_uuid = FILENAME_TO_UUID[args.property]
    logger.info(f"Curating knowledge base for {args.property} ({property_uuid})")

    hospitable = HospitableClient()
    claude = anthropic.Anthropic(api_key=get_anthropic_key())

    reservations = fetch_all_reservations(hospitable, property_uuid)
    if args.limit:
        reservations = reservations[: args.limit]
        logger.info(f"Limited to first {args.limit} reservations")

    aggregated = {"faqs": [], "precedents": []}
    for idx, res in enumerate(reservations):
        res_id = res.get("id", "")
        try:
            messages = hospitable.get_reservation_messages(res_id)
        except Exception as e:
            logger.warning(f"Skipping {res_id}: failed to fetch messages ({e})")
            continue

        if len(messages) < 2:
            continue  # Skip threads with no real conversation

        thread_text = format_thread(messages)
        if not thread_text.strip():
            continue

        logger.info(f"[{idx + 1}/{len(reservations)}] Extracting from reservation {res_id}")
        extracted = extract_from_thread(claude, thread_text, res_id)
        aggregated["faqs"].extend(extracted.get("faqs", []))
        aggregated["precedents"].extend(extracted.get("precedents", []))

    logger.info(
        f"Extracted {len(aggregated['faqs'])} FAQs and {len(aggregated['precedents'])} precedents — deduping"
    )
    deduped = dedupe(claude, aggregated)
    logger.info(
        f"After dedup: {len(deduped.get('faqs', []))} FAQs, {len(deduped.get('precedents', []))} precedents"
    )

    if args.dry_run:
        print(yaml.safe_dump({"dynamic": deduped}, sort_keys=False, allow_unicode=True))
        logger.info("Dry run complete — no files written")
        return

    # Merge into existing YAML (replace the dynamic section wholesale)
    kb = load_existing_kb(args.property)
    kb.setdefault("dynamic", {})
    kb["dynamic"]["faqs"] = deduped.get("faqs", [])
    kb["dynamic"]["precedents"] = deduped.get("precedents", [])
    write_kb(args.property, kb)
    logger.info("Curation complete. Review the diff with `git diff` and open a PR.")


if __name__ == "__main__":
    main()
