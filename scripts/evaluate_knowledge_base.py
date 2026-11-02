"""Evaluate the merged, authoritative knowledge base for each property.

Reads the curated YAML + the sibling Hospitable Hub snapshot (both already
tracked in git via `scripts/sync_knowledge_base.py`) and emits four reports
per property:
  1. Content audit — stale, duplicate, near-empty, suspicious.
  2. Cross-source consistency — Hub items vs. local YAML facts.
  3. Coverage gap check — against a standard topic set.
  4. LLM-judge quality scores — one Sonnet call per Hub item.

Reports land under `reports/knowledge_base/<YYYY-MM-DD>/<slug>.md` plus a
cross-property `summary.md`. The `reports/` tree is gitignored — each run is
on-demand, per the plan.

Usage:
    python scripts/evaluate_knowledge_base.py --all
    python scripts/evaluate_knowledge_base.py --property palm_club --skip-judge
    python scripts/evaluate_knowledge_base.py --all --max-items 20
    python scripts/evaluate_knowledge_base.py --all --date 2026-11-02
"""

import argparse
import json
import logging
import re
import sys
from datetime import date, datetime
from pathlib import Path
from typing import Any

SRC = Path(__file__).resolve().parent.parent / "src"
sys.path.insert(0, str(SRC))

import yaml  # noqa: E402

from knowledge_base_loader import UUID_TO_FILENAME, load_kb  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

FILENAME_TO_UUID = {v: k for k, v in UUID_TO_FILENAME.items()}
REPO_ROOT = Path(__file__).resolve().parent.parent
REPORTS_ROOT = REPO_ROOT / "reports" / "knowledge_base"

# Standard topic set the gap check expects covered by at least one of
# (local static keys, local dynamic FAQ topic strings, hub topic slug).
STANDARD_TOPICS: dict[str, dict[str, list[str]]] = {
    "access": {
        "hub_slugs": ["access", "access_codes", "arrival", "check_in"],
        "local_static_keys": ["access_code", "lockbox", "entry", "check_in"],
        "faq_substrings": ["access", "code", "lockbox", "arrival"],
    },
    "wifi": {
        "hub_slugs": ["wifi", "wi_fi", "internet"],
        "local_static_keys": ["wifi"],
        "faq_substrings": ["wifi", "internet"],
    },
    "parking": {
        "hub_slugs": ["parking"],
        "local_static_keys": ["parking"],
        "faq_substrings": ["parking", "driveway"],
    },
    "rules": {
        "hub_slugs": ["rules", "rules_policies", "house_rules", "policies"],
        "local_static_keys": ["rules", "house_rules", "quiet_hours", "noise"],
        "faq_substrings": ["rule", "quiet", "smoking", "pet", "party"],
    },
    "checkout": {
        "hub_slugs": ["check_out", "checkout_procedures", "check_out_procedures"],
        "local_static_keys": ["check_out", "checkout"],
        "faq_substrings": ["check out", "checkout"],
    },
    "amenities": {
        "hub_slugs": ["amenities", "appliances", "tv_streaming"],
        "local_static_keys": ["pool", "hot_tub", "amenities", "firepit", "bbq"],
        "faq_substrings": ["pool", "hot tub", "grill", "firepit", "amenity"],
    },
    "maintenance": {
        "hub_slugs": ["maintenance", "appliances"],
        "local_static_keys": ["maintenance", "regular_service"],
        "faq_substrings": ["ac", "hot water", "broken", "not working"],
    },
    "vendors": {
        "hub_slugs": ["vendor_policies", "vendors"],
        "local_static_keys": ["vendors", "regular_service"],
        "faq_substrings": ["vendor", "landscap", "pool service"],
    },
    "safety": {
        "hub_slugs": ["safety_features", "safety"],
        "local_static_keys": ["safety", "first_aid"],
        "faq_substrings": ["safety", "fire", "co detector", "smoke detector"],
    },
}

# Minimum item count below which the Hub is flagged as "near-empty".
NEAR_EMPTY_HUB_THRESHOLD = 5

# Consistency pairs: (local path, hub keyword, label). The evaluator pulls the
# local value by dotted path and searches hub items for lines mentioning the
# keyword, then flags disagreement.
CONSISTENCY_PAIRS = [
    ("static.wifi.password", "wifi password", "Wi-Fi password"),
    ("static.wifi.network", "wifi network", "Wi-Fi network"),
    ("static.quiet_hours.start", "quiet hours", "Quiet-hours start"),
    ("static.pool.heating.cost", "pool heating", "Pool heating cost"),
]

JUDGE_SYSTEM_PROMPT = """You rate short knowledge-base items on four axes for a short-term rental.

Each item is a single statement the host system shows guests. For each item, score 1-5 on:
- c1 (clarity): a guest with no context understands it in one read.
- c2 (specificity): names the exact place, step, or number — no vague references.
- c3 (guest_friendliness): warm, direct, not a rules-dump.
- c4 (self_containedness): no "see other item" / dead pointer; stands alone.

Return STRICT JSON only. No prose, no markdown fences.
Schema: {"c1": 1-5, "c2": 1-5, "c3": 1-5, "c4": 1-5, "notes": "<=120 chars"}
"""


# ------------------------------- utilities ---------------------------------


def _slugify(name: str) -> str:
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


def _by_path(data: dict, path: str) -> Any:
    cur: Any = data
    for seg in path.split("."):
        if isinstance(cur, dict):
            cur = cur.get(seg)
        else:
            return None
    return cur


def _flatten_static_keys(static: dict, prefix: str = "") -> list[str]:
    keys = []
    for k, v in (static or {}).items():
        key = f"{prefix}.{k}" if prefix else k
        keys.append(k)
        if isinstance(v, dict):
            keys.extend(_flatten_static_keys(v, key))
    return keys


# -------------------------------- audits ---------------------------------


def audit_content(snapshot: dict) -> list[dict]:
    """Flag structural problems in the Hub snapshot."""
    findings: list[dict] = []
    topics = snapshot.get("topics") or []
    total_items = sum(len(t.get("items") or []) for t in topics)

    if total_items == 0 or len(topics) == 0:
        findings.append(
            {
                "severity": "HIGH",
                "area": "snapshot",
                "finding": f"Near-empty Hub: {len(topics)} topics / {total_items} items",
                "where": "hospitable_snapshot",
            }
        )
    elif total_items < NEAR_EMPTY_HUB_THRESHOLD:
        findings.append(
            {
                "severity": "HIGH",
                "area": "snapshot",
                "finding": f"Near-empty Hub: {len(topics)} topics / {total_items} items (< {NEAR_EMPTY_HUB_THRESHOLD})",
                "where": "hospitable_snapshot",
            }
        )

    seen_contents: dict[str, list[str]] = {}
    for topic in topics:
        topic_name = topic.get("name") or "Unnamed"
        items = topic.get("items") or []
        if not items:
            findings.append(
                {
                    "severity": "MEDIUM",
                    "area": "topic",
                    "finding": f"Empty topic '{topic_name}'",
                    "where": f"topics[name={topic_name}]",
                }
            )
            continue
        for item in items:
            content = (item.get("content") or "").strip()
            item_id = item.get("id")
            if not content:
                findings.append(
                    {
                        "severity": "HIGH",
                        "area": "item",
                        "finding": "Empty item content",
                        "where": f"[{topic_name}] id={item_id}",
                    }
                )
                continue
            if len(content) < 20:
                findings.append(
                    {
                        "severity": "LOW",
                        "area": "item",
                        "finding": f"Very short item ({len(content)} chars)",
                        "where": f"[{topic_name}] id={item_id}",
                    }
                )
            seen_contents.setdefault(content, []).append(f"[{topic_name}] id={item_id}")

    for content, locations in seen_contents.items():
        if len(locations) > 1:
            findings.append(
                {
                    "severity": "MEDIUM",
                    "area": "item",
                    "finding": "Duplicate content across items",
                    "where": "; ".join(locations),
                }
            )
    return findings


def check_consistency(curated_kb: dict, snapshot: dict) -> list[dict]:
    """Flag disagreement between the curated local YAML and Hub items."""
    findings: list[dict] = []
    hub_items = []
    for topic in snapshot.get("topics") or []:
        for item in topic.get("items") or []:
            hub_items.append({"topic": topic.get("name") or "", "item": item})

    for path, keyword, label in CONSISTENCY_PAIRS:
        local_value = _by_path(curated_kb, path)
        if local_value in (None, "", "TODO"):
            continue
        local_str = str(local_value).lower()
        pattern = re.compile(re.escape(keyword), re.IGNORECASE)
        for hi in hub_items:
            content = (hi["item"].get("content") or "")
            if pattern.search(content):
                # If the local value appears nowhere in this hub line, flag.
                if local_str not in content.lower():
                    findings.append(
                        {
                            "local_path": path,
                            "local_value": str(local_value),
                            "hub_item_id": hi["item"].get("id"),
                            "hub_content": content[:160] + ("…" if len(content) > 160 else ""),
                            "verdict": "possible_conflict",
                            "label": label,
                        }
                    )
    return findings


def coverage_gap(curated_kb: dict, snapshot: dict) -> list[dict]:
    """For each STANDARD_TOPICS entry, note which source covers it (if any)."""
    results: list[dict] = []
    local_static_keys = set(_flatten_static_keys(curated_kb.get("static") or {}))
    local_faq_topics = {
        _slugify(faq.get("topic") or "")
        for faq in (curated_kb.get("dynamic") or {}).get("faqs") or []
    }
    hub_topic_slugs = {t.get("slug") or _slugify(t.get("name") or "") for t in snapshot.get("topics") or []}

    for standard_slug, synonyms in STANDARD_TOPICS.items():
        covered_by = []
        if any(k in local_static_keys for k in synonyms["local_static_keys"]):
            covered_by.append("local static")
        if any(any(s in topic for s in synonyms["local_static_keys"]) for topic in local_faq_topics):
            covered_by.append("local faqs")
        if any(s in hub_topic_slugs for s in synonyms["hub_slugs"]):
            covered_by.append("hub snapshot")
        results.append({"topic": standard_slug, "covered_by": covered_by})
    return results


# -------------------------------- judge ----------------------------------


def _get_judge_client():
    """Lazy import so tests can mock without touching the real anthropic lib."""
    import anthropic  # noqa: WPS433

    from config import get_anthropic_key, DRAFT_MODEL  # noqa: WPS433

    client = anthropic.Anthropic(api_key=get_anthropic_key())
    return client, DRAFT_MODEL


def _parse_judge_json(raw: str) -> dict:
    """Robust-ish JSON extraction — strip code fences if the model adds them."""
    text = raw.strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\n?", "", text)
        text = re.sub(r"\n?```$", "", text)
    return json.loads(text)


def judge_item(client, model: str, topic_name: str, content: str) -> dict:
    user_msg = (
        f"Topic: {topic_name}\nItem content:\n{content}\n\n"
        "Return the JSON only."
    )
    resp = client.messages.create(
        model=model,
        max_tokens=200,
        system=JUDGE_SYSTEM_PROMPT,
        messages=[{"role": "user", "content": user_msg}],
    )
    text = resp.content[0].text if resp.content else ""
    parsed = _parse_judge_json(text)
    scores = {k: int(parsed.get(k, 0)) for k in ("c1", "c2", "c3", "c4")}
    scores["notes"] = str(parsed.get("notes", "")).strip()
    scores["mean"] = round(sum(scores[k] for k in ("c1", "c2", "c3", "c4")) / 4, 2)
    return scores


def judge_snapshot(snapshot: dict, max_items: int, client, model: str) -> list[dict]:
    """One Sonnet call per Hub item, sequential, capped."""
    judged: list[dict] = []
    budget = max_items
    for topic in snapshot.get("topics") or []:
        topic_name = topic.get("name") or "General"
        for item in topic.get("items") or []:
            if budget <= 0:
                return judged
            content = (item.get("content") or "").strip()
            if not content:
                continue
            try:
                score = judge_item(client, model, topic_name, content)
            except Exception as e:  # noqa: BLE001
                logger.warning(f"Judge call failed for id={item.get('id')}: {e}")
                continue
            judged.append(
                {
                    "id": item.get("id"),
                    "topic": topic_name,
                    "mean": score["mean"],
                    "c1": score["c1"],
                    "c2": score["c2"],
                    "c3": score["c3"],
                    "c4": score["c4"],
                    "notes": score["notes"],
                }
            )
            budget -= 1
    return judged


# ------------------------------- rendering --------------------------------


def _severity_count(findings: list[dict], severity: str) -> int:
    return sum(1 for f in findings if f.get("severity") == severity)


def render_property_report(
    *,
    slug: str,
    property_name: str,
    report_date: str,
    audit: list[dict],
    consistency: list[dict],
    coverage: list[dict],
    judged: list[dict] | None,
) -> str:
    out: list[str] = []
    out.append(f"# Knowledge-base evaluation — {property_name} ({report_date})")
    out.append(f"_Property slug: `{slug}`_\n")

    out.append("## 1. Content audit")
    if audit:
        out.append("| Severity | Area | Finding | Where |")
        out.append("|---|---|---|---|")
        for f in sorted(audit, key=lambda x: {"HIGH": 0, "MEDIUM": 1, "LOW": 2}.get(x["severity"], 9)):
            out.append(f"| {f['severity']} | {f['area']} | {f['finding']} | `{f['where']}` |")
    else:
        out.append("_No issues found._")
    out.append("")

    out.append("## 2. Cross-source consistency")
    if consistency:
        out.append("| Local path | Local value | Hub item id | Hub content excerpt | Verdict |")
        out.append("|---|---|---|---|---|")
        for c in consistency:
            out.append(
                f"| `{c['local_path']}` | `{c['local_value']}` | `{c['hub_item_id']}` "
                f"| {c['hub_content']} | {c['verdict']} |"
            )
    else:
        out.append("_No cross-source conflicts detected._")
    out.append("")

    out.append("## 3. Coverage gap check")
    out.append("| Standard topic | Covered by |")
    out.append("|---|---|")
    missing = []
    for row in coverage:
        covered_by = ", ".join(row["covered_by"]) if row["covered_by"] else "— **MISSING**"
        if not row["covered_by"]:
            missing.append(row["topic"])
        out.append(f"| `{row['topic']}` | {covered_by} |")
    if missing:
        out.append(f"\n**Missing:** {', '.join(f'`{m}`' for m in missing)}")
    out.append("")

    out.append("## 4. LLM-judge quality scores")
    if judged is None:
        out.append("_Judge skipped (--skip-judge)._")
    elif not judged:
        out.append("_No items to score._")
    else:
        out.append("| Hub item id | Topic | Mean | C1 | C2 | C3 | C4 | Notes |")
        out.append("|---|---|---|---|---|---|---|---|")
        for j in sorted(judged, key=lambda x: x["mean"]):
            out.append(
                f"| `{j['id']}` | {j['topic']} | **{j['mean']}** "
                f"| {j['c1']} | {j['c2']} | {j['c3']} | {j['c4']} | {j['notes']} |"
            )
        mean = round(sum(j["mean"] for j in judged) / len(judged), 2)
        lows = [j for j in judged if j["mean"] <= 2.0 or min(j["c1"], j["c2"], j["c3"], j["c4"]) == 1]
        out.append(
            f"\n**Summary:** {len(judged)} items judged · mean={mean} · "
            f"needs-rewrite bucket={len(lows)}"
        )
    out.append("")
    return "\n".join(out)


def render_summary(per_property: list[dict], report_date: str) -> str:
    out = [f"# Knowledge-base evaluation — summary ({report_date})", ""]
    out.append("| Property | HIGH | MEDIUM | LOW | Consistency flags | Missing topics | Judge mean | Needs-rewrite |")
    out.append("|---|---|---|---|---|---|---|---|")
    for p in per_property:
        audit = p["audit"]
        judged = p["judged"]
        judge_mean = round(sum(j["mean"] for j in judged) / len(judged), 2) if judged else "—"
        needs_rewrite = (
            sum(1 for j in judged if j["mean"] <= 2.0 or min(j["c1"], j["c2"], j["c3"], j["c4"]) == 1)
            if judged
            else "—"
        )
        missing = sum(1 for row in p["coverage"] if not row["covered_by"])
        out.append(
            f"| {p['property_name']} | "
            f"{_severity_count(audit, 'HIGH')} | "
            f"{_severity_count(audit, 'MEDIUM')} | "
            f"{_severity_count(audit, 'LOW')} | "
            f"{len(p['consistency'])} | "
            f"{missing} | {judge_mean} | {needs_rewrite} |"
        )
    return "\n".join(out) + "\n"


# -------------------------------- driver ----------------------------------


def evaluate_property(
    slug: str,
    report_dir: Path,
    *,
    skip_judge: bool,
    max_items: int,
    judge_factory=_get_judge_client,
) -> dict:
    uuid = FILENAME_TO_UUID[slug]
    kb = load_kb(uuid)
    snapshot = kb.get("hospitable_snapshot") or {}
    property_name = (kb.get("property") or {}).get("name") or slug

    audit = audit_content(snapshot)
    consistency = check_consistency(kb, snapshot)
    coverage = coverage_gap(kb, snapshot)

    judged: list[dict] | None
    if skip_judge:
        judged = None
    else:
        try:
            client, model = judge_factory()
            judged = judge_snapshot(snapshot, max_items=max_items, client=client, model=model)
        except Exception as e:  # noqa: BLE001
            logger.warning(f"Judge initialization failed; skipping: {e}")
            judged = []

    body = render_property_report(
        slug=slug,
        property_name=property_name,
        report_date=report_dir.name,
        audit=audit,
        consistency=consistency,
        coverage=coverage,
        judged=judged,
    )
    out_path = report_dir / f"{slug}.md"
    report_dir.mkdir(parents=True, exist_ok=True)
    out_path.write_text(body, encoding="utf-8")
    logger.info(f"wrote {out_path}")
    return {
        "slug": slug,
        "property_name": property_name,
        "audit": audit,
        "consistency": consistency,
        "coverage": coverage,
        "judged": judged or [],
    }


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--property", dest="property_slug", choices=sorted(FILENAME_TO_UUID.keys()))
    group.add_argument("--all", action="store_true")
    parser.add_argument("--date", default=None, help="YYYY-MM-DD (default today, UTC).")
    parser.add_argument("--skip-judge", action="store_true", help="Skip the Sonnet quality score.")
    parser.add_argument("--max-items", type=int, default=50, help="Cap judge calls (default 50).")
    args = parser.parse_args(argv)

    report_date = args.date or date.today().isoformat()
    try:
        datetime.strptime(report_date, "%Y-%m-%d")
    except ValueError:
        parser.error(f"Bad --date: {report_date!r}; expected YYYY-MM-DD")

    report_dir = REPORTS_ROOT / report_date
    targets = sorted(FILENAME_TO_UUID.keys()) if args.all else [args.property_slug]

    per_property = []
    for slug in targets:
        per_property.append(
            evaluate_property(
                slug,
                report_dir,
                skip_judge=args.skip_judge,
                max_items=args.max_items,
            )
        )

    (report_dir / "summary.md").write_text(render_summary(per_property, report_date), encoding="utf-8")
    logger.info(f"wrote {report_dir / 'summary.md'}")
    print(f"\nReports written under {report_dir.relative_to(REPO_ROOT)}/")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
