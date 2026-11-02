"""Tests for scripts/evaluate_knowledge_base.py — audit, consistency, coverage, judge."""

import sys
from pathlib import Path
from unittest.mock import MagicMock

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS))

import evaluate_knowledge_base as ev  # noqa: E402


# ------------------------------- audit -----------------------------------


def test_audit_flags_near_empty_hub():
    snapshot = {
        "topics": [
            {"name": "Access", "items": [{"id": 1, "content": "x" * 40}]},
            {"name": "Arrival", "items": [{"id": 2, "content": "y" * 40}]},
        ]
    }
    findings = ev.audit_content(snapshot)
    high = [f for f in findings if f["severity"] == "HIGH"]
    assert any("Near-empty Hub" in f["finding"] for f in high)


def test_audit_flags_empty_topic():
    snapshot = {"topics": [{"name": "Empty Topic", "items": []}, {"name": "T", "items": [{"id": 1, "content": "x" * 40}] * 5}]}
    findings = ev.audit_content(snapshot)
    assert any("Empty topic" in f["finding"] for f in findings)


def test_audit_flags_duplicate_content():
    content = "The firepit lives in the yard — operate with care."
    snapshot = {
        "topics": [
            {"name": "A", "items": [{"id": 1, "content": content}]},
            {"name": "B", "items": [{"id": 2, "content": content}]},
            {"name": "C", "items": [{"id": 3, "content": "unique" + "x" * 40}]},
            {"name": "D", "items": [{"id": 4, "content": "another" + "x" * 40}]},
            {"name": "E", "items": [{"id": 5, "content": "distinct" + "x" * 40}]},
        ]
    }
    findings = ev.audit_content(snapshot)
    assert any(f["finding"] == "Duplicate content across items" for f in findings)


def test_audit_flags_short_item():
    snapshot = {"topics": [{"name": "T", "items": [{"id": 1, "content": "short"}] + [{"id": i, "content": "x" * 40} for i in range(10, 20)]}]}
    findings = ev.audit_content(snapshot)
    assert any("Very short item" in f["finding"] for f in findings)


# --------------------------- consistency --------------------------------


def test_consistency_flags_wifi_password_disagreement():
    curated = {"static": {"wifi": {"password": "welcome5120!!"}}}
    snapshot = {
        "topics": [
            {
                "name": "Wi-Fi",
                "items": [{"id": 99, "content": "The wifi password is Welcome5120!"}],
            }
        ]
    }
    findings = ev.check_consistency(curated, snapshot)
    assert len(findings) == 1
    assert findings[0]["hub_item_id"] == 99
    assert findings[0]["verdict"] == "possible_conflict"


def test_consistency_no_flag_when_local_value_appears_in_hub():
    curated = {"static": {"wifi": {"password": "welcome5120!!"}}}
    snapshot = {
        "topics": [
            {"name": "Wi-Fi", "items": [{"id": 99, "content": "Wifi password: welcome5120!!"}]}
        ]
    }
    findings = ev.check_consistency(curated, snapshot)
    assert findings == []


def test_consistency_skips_todo_local_values():
    curated = {"static": {"wifi": {"password": "TODO"}}}
    snapshot = {"topics": [{"name": "W", "items": [{"id": 1, "content": "the wifi password is X"}]}]}
    findings = ev.check_consistency(curated, snapshot)
    assert findings == []


# ---------------------------- coverage ----------------------------------


def test_coverage_marks_missing_topics():
    """Palm-Club-shaped case: only access, no safety or vendor data."""
    curated = {"static": {}, "dynamic": {"faqs": []}}
    snapshot = {"topics": [{"name": "Access Codes", "slug": "access_codes", "items": []}]}
    coverage = ev.coverage_gap(curated, snapshot)
    by_topic = {row["topic"]: row["covered_by"] for row in coverage}
    assert by_topic["access"] == ["hub snapshot"]
    assert by_topic["safety"] == []
    assert by_topic["wifi"] == []


def test_coverage_credits_local_static_and_hub_snapshot():
    curated = {"static": {"wifi": {"network": "x", "password": "y"}, "parking": {"driveway_capacity": 2}}}
    snapshot = {"topics": [{"name": "Rules & Policies", "slug": "rules_policies", "items": []}]}
    coverage = ev.coverage_gap(curated, snapshot)
    by_topic = {row["topic"]: row["covered_by"] for row in coverage}
    assert "local static" in by_topic["wifi"]
    assert "local static" in by_topic["parking"]
    assert "hub snapshot" in by_topic["rules"]


# ------------------------------ judge -----------------------------------


def _make_mock_client(response_text: str):
    client = MagicMock()
    msg = MagicMock()
    msg.content = [MagicMock(text=response_text)]
    client.messages.create.return_value = msg
    return client


def test_judge_item_parses_valid_json_and_computes_mean():
    client = _make_mock_client('{"c1": 5, "c2": 4, "c3": 5, "c4": 4, "notes": "good"}')
    score = ev.judge_item(client, "sonnet", "Access", "Open with code 1234")
    assert score["c1"] == 5
    assert score["mean"] == 4.5
    assert score["notes"] == "good"


def test_judge_item_strips_markdown_fences():
    client = _make_mock_client('```json\n{"c1":1,"c2":2,"c3":3,"c4":4,"notes":"meh"}\n```')
    score = ev.judge_item(client, "sonnet", "Access", "x")
    assert score["c1"] == 1
    assert score["mean"] == 2.5


def test_judge_snapshot_respects_max_items():
    client = _make_mock_client('{"c1":5,"c2":5,"c3":5,"c4":5,"notes":""}')
    snapshot = {
        "topics": [
            {"name": "T", "items": [{"id": i, "content": f"item {i}"} for i in range(10)]}
        ]
    }
    judged = ev.judge_snapshot(snapshot, max_items=3, client=client, model="sonnet")
    assert len(judged) == 3
    assert client.messages.create.call_count == 3


# ------------------------------ driver ----------------------------------


def test_evaluate_property_writes_report_for_empty_hub(monkeypatch, tmp_path):
    """Palm-Club-shaped: evaluator must produce a report that flags HIGH severity."""
    import knowledge_base_loader

    (tmp_path / "palm_club.yaml").write_text(
        'property:\n  name: The Palm Club\n  id: 3278e9cb-9239-487f-aa51-cbfbaf4b7570\nstatic: {}\n',
        encoding="utf-8",
    )
    (tmp_path / "palm_club.hospitable_hub.yaml").write_text(
        'property:\n  id: 3278e9cb-9239-487f-aa51-cbfbaf4b7570\n  slug: palm_club\n'
        'hospitable_snapshot:\n  fetched_at: "2026-11-02T00:00:00Z"\n  topics: []\n',
        encoding="utf-8",
    )
    monkeypatch.setattr(knowledge_base_loader, "_KB_DIR", str(tmp_path))
    knowledge_base_loader.load_kb.cache_clear()

    report_dir = tmp_path / "reports" / "2026-11-02"
    result = ev.evaluate_property("palm_club", report_dir, skip_judge=True, max_items=0)

    out_path = report_dir / "palm_club.md"
    assert out_path.exists()
    body = out_path.read_text()
    assert "The Palm Club" in body
    assert "HIGH" in body
    assert "Near-empty Hub" in body
    assert any(f["severity"] == "HIGH" for f in result["audit"])
