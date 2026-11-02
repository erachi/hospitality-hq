"""Snapshot the Hospitable Knowledge Hub into git-tracked sibling YAML files.

For each property we write src/knowledge_base/<slug>.hospitable_hub.yaml with
every item preserved field-for-field (id, content, original_content, is_edited,
state, last_updated_via, sources, updated_at) so a reviewer can read the diff
and the data can round-trip to the Hospitable API later if needed.

Usage:
    python scripts/sync_knowledge_base.py --all
    python scripts/sync_knowledge_base.py --property villa_bougainvillea
    python scripts/sync_knowledge_base.py --all --dry-run

    # Build from pre-fetched JSON (e.g. an MCP response) when outbound HTTPS
    # to Hospitable is not available:
    python scripts/sync_knowledge_base.py --property palm_club --from-file palm.json

The input JSON for --from-file is the Hospitable response body — either the raw
response (with `data` wrapper) or just the inner payload with `topics` at the
top level. Both shapes are accepted.

Credentials are read from SSM via src/config.py OR from the HOSPITABLE_API_TOKEN
env var. This is a local script, not Lambda.
"""

import argparse
import json
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path

SRC = Path(__file__).resolve().parent.parent / "src"
sys.path.insert(0, str(SRC))

import yaml  # noqa: E402

from hospitable_client import HospitableClient  # noqa: E402
from knowledge_base_loader import UUID_TO_FILENAME  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

FILENAME_TO_UUID = {v: k for k, v in UUID_TO_FILENAME.items()}
KB_DIR = SRC / "knowledge_base"

ITEM_FIELDS = (
    "id",
    "content",
    "original_content",
    "is_edited",
    "state",
    "last_updated_via",
    "sources",
    "updated_at",
)


def _slugify(name: str) -> str:
    """snake_case slug for a Hub topic name — stable across runs."""
    out = []
    prev_sep = True
    for ch in name.strip().lower():
        if ch.isalnum():
            out.append(ch)
            prev_sep = False
        elif not prev_sep:
            out.append("_")
            prev_sep = True
    slug = "".join(out).strip("_")
    return slug or "unnamed"


def _simplify_sources(raw_sources) -> list:
    """Keep sources as a flat list of short labels. The Hub payload nests full
    objects per item; store just the human-readable name for diff legibility.
    Falls back to the type if name is missing."""
    out = []
    for s in raw_sources or []:
        if isinstance(s, dict):
            label = s.get("name") or s.get("type")
            if label:
                out.append(label)
        elif isinstance(s, str):
            out.append(s)
    return out


def _normalize_item(raw: dict) -> dict:
    """Keep only the fields we care about, in a stable order."""
    normalized = {}
    for key in ITEM_FIELDS:
        if key == "sources":
            normalized[key] = _simplify_sources(raw.get("sources"))
        else:
            normalized[key] = raw.get(key)
    return normalized


def _topic_items(topic: dict) -> list:
    """Hub payload uses `aggregate_items` for the item array; also accept `items`."""
    for key in ("aggregate_items", "items"):
        value = topic.get(key)
        if isinstance(value, list):
            return value
    return []


def build_snapshot(property_uuid: str, slug: str, hub_payload: dict) -> dict:
    """Shape the Hub payload into the sibling-YAML schema."""
    topics_out = []
    for topic in hub_payload.get("topics") or []:
        name = topic.get("name") or "Unnamed"
        items_out = [_normalize_item(it) for it in _topic_items(topic)]
        topics_out.append(
            {
                "name": name,
                "slug": _slugify(name),
                "items": items_out,
            }
        )
    return {
        "property": {"id": property_uuid, "slug": slug},
        "hospitable_snapshot": {
            "fetched_at": datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
            "source": "hospitable_knowledge_hub",
            "api_endpoint": "/properties/{uuid}/knowledge-hub",
            "topics": topics_out,
        },
    }


def _write_yaml(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        yaml.safe_dump(
            data,
            f,
            sort_keys=False,
            allow_unicode=True,
            width=100,
            default_flow_style=False,
        )


def _load_payload_from_file(path: Path) -> dict:
    """Accept either {"data": {...}} or the inner payload with topics at top level."""
    with path.open("r", encoding="utf-8") as f:
        raw = json.load(f)
    if isinstance(raw, dict) and "topics" in raw:
        return raw
    if isinstance(raw, dict) and isinstance(raw.get("data"), dict):
        return raw["data"]
    raise ValueError(f"{path}: expected a Hospitable Hub payload with top-level `topics` or a `data` wrapper")


def sync_property(
    slug: str,
    hospitable: HospitableClient | None,
    dry_run: bool = False,
    from_file: Path | None = None,
) -> Path:
    uuid = FILENAME_TO_UUID[slug]
    if from_file is not None:
        logger.info(f"Loading Knowledge Hub for {slug} ({uuid}) from {from_file}")
        hub_payload = _load_payload_from_file(from_file)
    else:
        assert hospitable is not None, "hospitable client required when --from-file not given"
        logger.info(f"Fetching Knowledge Hub for {slug} ({uuid})")
        hub_payload = hospitable.get_property_knowledge_hub(uuid)
    snapshot = build_snapshot(uuid, slug, hub_payload)

    topics = snapshot["hospitable_snapshot"]["topics"]
    item_count = sum(len(t["items"]) for t in topics)
    logger.info(f"  {len(topics)} topics / {item_count} items")

    out_path = KB_DIR / f"{slug}.hospitable_hub.yaml"
    if dry_run:
        logger.info(f"  (dry-run) would write {out_path}")
    else:
        _write_yaml(out_path, snapshot)
        logger.info(f"  wrote {out_path}")
    return out_path


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument(
        "--property",
        dest="property_slug",
        choices=sorted(FILENAME_TO_UUID.keys()),
        help="Sync one property by its slug (e.g. villa_bougainvillea).",
    )
    group.add_argument("--all", action="store_true", help="Sync every known property.")
    parser.add_argument("--dry-run", action="store_true", help="Do not write files.")
    parser.add_argument(
        "--from-file",
        type=Path,
        default=None,
        help="Build the snapshot from a pre-fetched JSON file instead of calling Hospitable. "
        "Requires --property (not --all).",
    )
    args = parser.parse_args(argv)

    if args.from_file is not None and args.all:
        parser.error("--from-file requires --property, not --all")

    hospitable = None if args.from_file is not None else HospitableClient()
    targets = sorted(FILENAME_TO_UUID.keys()) if args.all else [args.property_slug]
    written = []
    for slug in targets:
        written.append(
            sync_property(
                slug,
                hospitable,
                dry_run=args.dry_run,
                from_file=args.from_file,
            )
        )

    if not args.dry_run:
        print("\nSnapshot complete. Review the diff and open a PR:")
        print("  git diff --stat src/knowledge_base/")
        for p in written:
            print(f"  git add {p.relative_to(Path.cwd()) if p.is_absolute() else p}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
