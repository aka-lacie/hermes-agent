"""End-to-end policy tests for unattended memory consolidation."""

import json

from tools.memory_tool import load_on_disk_store
from tools.registry import registry
from tools.skill_provenance import reset_current_write_origin, set_current_write_origin
from tools.write_approval import MEMORY, get_pending, list_pending


def _dispatch_background(store, args):
    token = set_current_write_origin("background_review")
    try:
        raw = registry.dispatch("memory", args, store=store)
        return json.loads(raw) if isinstance(raw, str) else raw
    finally:
        reset_current_write_origin(token)


def test_background_add_replace_batch_applies_with_default_approval_off(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    (tmp_path / "config.yaml").write_text("memory:\n  write_approval: false\n", encoding="utf-8")
    store = load_on_disk_store()
    seed = "Prefers concise explanations with examples"
    assert store.add("memory", seed)["success"]
    prompt_snapshot = store.format_for_system_prompt("memory")

    result = _dispatch_background(store, {
        "target": "memory",
        "operations": [
            {"action": "add", "content": "Uses dark mode"},
            {"action": "replace", "old_text": seed, "content": "Prefers concise examples"},
        ],
    })

    assert result["success"] is True
    assert not result.get("staged")
    assert store.format_for_system_prompt("memory") == prompt_snapshot
    assert set(load_on_disk_store()._entries_for("memory")) == {
        "Uses dark mode", "Prefers concise examples",
    }
    assert list_pending(MEMORY) == []


def test_background_replace_stages_when_write_approval_enabled(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    (tmp_path / "config.yaml").write_text("memory:\n  write_approval: true\n", encoding="utf-8")
    store = load_on_disk_store()
    seed = "Prefers concise explanations with examples"
    assert store.add("memory", seed)["success"]

    result = _dispatch_background(store, {
        "action": "replace",
        "target": "memory",
        "old_text": seed,
        "content": "Prefers concise examples",
    })

    assert result["success"] is True
    assert result["staged"] is True
    assert load_on_disk_store()._entries_for("memory") == [seed]
    record = get_pending(MEMORY, result["pending_id"])
    assert record is not None
    assert record["origin"] == "background_review"
    assert record["payload"]["action"] == "replace"
