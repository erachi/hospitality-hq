"""Tests for scripts/sync_knowledge_base.py — snapshot build + round-trip."""

import json
import sys
from pathlib import Path

import pytest
import yaml

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS))

import sync_knowledge_base as sync  # noqa: E402


def test_slugify_normalizes_topic_names():
    assert sync._slugify("Rules & Policies") == "rules_policies"
    assert sync._slugify("TV/Streaming") == "tv_streaming"
    assert sync._slugify("Access Codes") == "access_codes"
    assert sync._slugify("   ") == "unnamed"


def test_normalize_item_keeps_only_known_fields():
    raw = {
        "id": 1,
        "content": "c",
        "original_content": None,
        "is_edited": False,
        "state": "ready",
        "last_updated_via": "mcp",
        "sources": [{"name": "pdf-file.pdf", "type": "pdf"}],
        "updated_at": "2026-01-01T00:00:00Z",
        "created_via": "ignore_me",
        "metadata": [],
    }
    out = sync._normalize_item(raw)
    assert out == {
        "id": 1,
        "content": "c",
        "original_content": None,
        "is_edited": False,
        "state": "ready",
        "last_updated_via": "mcp",
        "sources": ["pdf-file.pdf"],
        "updated_at": "2026-01-01T00:00:00Z",
    }


def test_build_snapshot_preserves_item_edited_via_mcp():
    payload = {
        "property": {"id": 1, "name": "X"},
        "topics": [
            {
                "name": "Rules & Policies",
                "aggregate_items": [
                    {
                        "id": 42,
                        "content": "no smoking",
                        "original_content": "vaping allowed outside",
                        "is_edited": True,
                        "last_updated_via": "mcp",
                        "state": "ready",
                        "sources": [{"name": "inbox", "type": "inbox_swai_edit"}],
                        "updated_at": "2026-10-28T23:54:02Z",
                    }
                ],
            }
        ],
    }
    snapshot = sync.build_snapshot("uuid-1", "palm_club", payload)
    item = snapshot["hospitable_snapshot"]["topics"][0]["items"][0]
    assert item["id"] == 42
    assert item["last_updated_via"] == "mcp"
    assert item["is_edited"] is True
    assert item["original_content"] == "vaping allowed outside"
    assert snapshot["hospitable_snapshot"]["topics"][0]["slug"] == "rules_policies"


def test_build_snapshot_accepts_items_key_too():
    payload = {"topics": [{"name": "T", "items": [{"id": 1, "content": "c"}]}]}
    snap = sync.build_snapshot("uuid-1", "palm_club", payload)
    assert snap["hospitable_snapshot"]["topics"][0]["items"][0]["content"] == "c"


def test_load_payload_from_file_accepts_data_wrapper(tmp_path):
    p = tmp_path / "hub.json"
    p.write_text(json.dumps({"data": {"topics": [{"name": "T", "aggregate_items": []}]}}))
    out = sync._load_payload_from_file(p)
    assert out["topics"][0]["name"] == "T"


def test_load_payload_from_file_accepts_flat_payload(tmp_path):
    p = tmp_path / "hub.json"
    p.write_text(json.dumps({"topics": [{"name": "T", "aggregate_items": []}]}))
    out = sync._load_payload_from_file(p)
    assert out["topics"][0]["name"] == "T"


def test_load_payload_from_file_rejects_bad_shape(tmp_path):
    p = tmp_path / "hub.json"
    p.write_text(json.dumps({"foo": "bar"}))
    with pytest.raises(ValueError):
        sync._load_payload_from_file(p)


def test_sync_property_from_file_writes_sibling_yaml(tmp_path, monkeypatch):
    hub = tmp_path / "hub.json"
    hub.write_text(
        json.dumps(
            {
                "data": {
                    "topics": [
                        {
                            "name": "Access Codes",
                            "aggregate_items": [
                                {
                                    "id": 100,
                                    "content": "Open with code 1234",
                                    "original_content": None,
                                    "is_edited": False,
                                    "state": "ready",
                                    "last_updated_via": None,
                                    "sources": [],
                                    "updated_at": "2026-02-06T05:17:45Z",
                                }
                            ],
                        }
                    ]
                }
            }
        )
    )

    kb_dir = tmp_path / "kb"
    kb_dir.mkdir()
    monkeypatch.setattr(sync, "KB_DIR", kb_dir)

    out_path = sync.sync_property("palm_club", hospitable=None, from_file=hub)
    assert out_path.exists()
    written = yaml.safe_load(out_path.read_text())
    assert written["property"]["slug"] == "palm_club"
    assert written["hospitable_snapshot"]["topics"][0]["items"][0]["id"] == 100
    assert written["hospitable_snapshot"]["topics"][0]["slug"] == "access_codes"


def test_main_rejects_all_with_from_file(tmp_path):
    with pytest.raises(SystemExit):
        sync.main(["--all", "--from-file", str(tmp_path / "x.json")])
