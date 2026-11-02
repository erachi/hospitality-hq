"""Tests for the merged KB behavior: curated YAML + Hospitable Hub snapshot."""

import yaml

import knowledge_base_loader
from knowledge_base_loader import load_kb, format_for_claude


PALM_UUID = "3278e9cb-9239-487f-aa51-cbfbaf4b7570"


def _write_curated(dir_, slug: str, data: dict) -> None:
    (dir_ / f"{slug}.yaml").write_text(yaml.safe_dump(data), encoding="utf-8")


def _write_snapshot(dir_, slug: str, data: dict) -> None:
    (dir_ / f"{slug}.hospitable_hub.yaml").write_text(yaml.safe_dump(data), encoding="utf-8")


def _redirect_kb_dir(monkeypatch, tmp_path):
    monkeypatch.setattr(knowledge_base_loader, "_KB_DIR", str(tmp_path))
    load_kb.cache_clear()


def test_load_kb_attaches_hub_snapshot_when_sibling_present(monkeypatch, tmp_path):
    _redirect_kb_dir(monkeypatch, tmp_path)
    _write_curated(tmp_path, "palm_club", {"property": {"name": "The Palm Club"}, "static": {}})
    _write_snapshot(
        tmp_path,
        "palm_club",
        {
            "property": {"id": PALM_UUID, "slug": "palm_club"},
            "hospitable_snapshot": {
                "fetched_at": "2026-11-02T00:00:00Z",
                "topics": [
                    {"name": "Access", "slug": "access", "items": [{"id": 1, "content": "code is 1234"}]}
                ],
            },
        },
    )

    kb = load_kb(PALM_UUID)
    assert kb["property"]["name"] == "The Palm Club"
    assert kb["hospitable_snapshot"]["topics"][0]["items"][0]["content"] == "code is 1234"


def test_load_kb_without_sibling_is_curated_only(monkeypatch, tmp_path):
    _redirect_kb_dir(monkeypatch, tmp_path)
    _write_curated(tmp_path, "palm_club", {"property": {"name": "The Palm Club"}})
    kb = load_kb(PALM_UUID)
    assert "hospitable_snapshot" not in kb
    assert kb["property"]["name"] == "The Palm Club"


def test_format_for_claude_appends_snapshot_block_after_precedents(monkeypatch, tmp_path):
    _redirect_kb_dir(monkeypatch, tmp_path)
    _write_curated(
        tmp_path,
        "palm_club",
        {
            "property": {"name": "The Palm Club"},
            "static": {"wifi": {"network": "N", "password": "P"}},
            "dynamic": {
                "faqs": [{"question": "Q1?", "answer": "A1."}],
                "precedents": [{"situation": "S1", "what_we_did": "W1"}],
            },
        },
    )
    _write_snapshot(
        tmp_path,
        "palm_club",
        {
            "property": {"id": PALM_UUID, "slug": "palm_club"},
            "hospitable_snapshot": {
                "fetched_at": "2026-11-02T00:00:00Z",
                "topics": [{"name": "Access", "items": [{"content": "Door opens with code"}]}],
            },
        },
    )

    kb = load_kb(PALM_UUID)
    rendered = format_for_claude(kb)

    assert "AUTHORITATIVE FACTS" in rendered
    assert "PRECEDENTS" in rendered
    assert "HOSPITABLE HUB SNAPSHOT" in rendered
    # Snapshot block must come AFTER precedents
    assert rendered.index("PRECEDENTS") < rendered.index("HOSPITABLE HUB SNAPSHOT")
    assert "[Access] Door opens with code" in rendered


def test_format_for_claude_includes_fetched_at_in_header(monkeypatch, tmp_path):
    _redirect_kb_dir(monkeypatch, tmp_path)
    _write_curated(tmp_path, "palm_club", {"property": {"name": "The Palm Club"}})
    _write_snapshot(
        tmp_path,
        "palm_club",
        {
            "hospitable_snapshot": {
                "fetched_at": "2026-11-02T18:00:00Z",
                "topics": [{"name": "X", "items": [{"content": "hello"}]}],
            }
        },
    )
    rendered = format_for_claude(load_kb(PALM_UUID))
    assert "fetched_at 2026-11-02T18:00:00Z" in rendered


def test_format_for_claude_omits_block_when_snapshot_empty(monkeypatch, tmp_path):
    _redirect_kb_dir(monkeypatch, tmp_path)
    _write_curated(tmp_path, "palm_club", {"property": {"name": "The Palm Club"}})
    _write_snapshot(
        tmp_path,
        "palm_club",
        {"hospitable_snapshot": {"fetched_at": "2026-11-02T00:00:00Z", "topics": []}},
    )
    rendered = format_for_claude(load_kb(PALM_UUID))
    assert "HOSPITABLE HUB SNAPSHOT" not in rendered


def test_snapshot_item_fields_round_trip(monkeypatch, tmp_path):
    """Fields relevant for future round-trip (id, last_updated_via) must survive load."""
    _redirect_kb_dir(monkeypatch, tmp_path)
    _write_curated(tmp_path, "palm_club", {"property": {"name": "The Palm Club"}})
    item = {
        "id": 42,
        "content": "x",
        "original_content": "y",
        "is_edited": True,
        "state": "ready",
        "last_updated_via": "mcp",
        "sources": ["foo"],
        "updated_at": "2026-10-28T23:54:10Z",
    }
    _write_snapshot(
        tmp_path,
        "palm_club",
        {
            "hospitable_snapshot": {
                "fetched_at": "2026-11-02T00:00:00Z",
                "topics": [{"name": "T", "items": [item]}],
            }
        },
    )
    loaded_item = load_kb(PALM_UUID)["hospitable_snapshot"]["topics"][0]["items"][0]
    for key, value in item.items():
        assert loaded_item[key] == value, f"{key} did not round-trip"
