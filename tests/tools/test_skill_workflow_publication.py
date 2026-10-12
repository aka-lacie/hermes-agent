"""Managed publication validates immutable candidates before locked CAS writes.

These boundary tests use the real managed writer and validator registration
store. The fixture validator parses Python/JSON as data; workflow-schema
semantics are covered by the workflow plugin's own static validator tests.
"""
import ast
import json
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from tools.skill_manager_tool import skill_manage
from tools.workflow_packages import package_revision, read_package


SKILL = "---\nname: fixture\ndescription: Exercise workflow publication.\n---\n# Fixture\n"
EMPTY_REVISION = package_revision({})


@pytest.fixture
def publication(tmp_path, monkeypatch):
    from hermes_cli.plugins import get_plugin_manager
    root = tmp_path / "skills"
    root.mkdir()
    monkeypatch.setattr("tools.skill_manager_tool.SKILLS_DIR", root)
    monkeypatch.setattr("agent.skill_utils.get_all_skills_dirs", lambda: [root])
    seen = []
    def validate(snapshot):
        files = read_package(snapshot)
        manifest = json.loads(files["workflows.json"])
        ast.parse(files[manifest["entry_point"]].decode())
        for name, content in files.items():
            if name.startswith("examples/"):
                json.loads(content)
        seen.append(package_revision(files))
    manager = get_plugin_manager()
    monkeypatch.setattr(manager, "_workflow_validators", {"fixture": validate}, raising=False)
    return root, seen, manager


def create_package():
    return json.loads(skill_manage("batch", "", expected_revision=EMPTY_REVISION, operations=[
        {"action": "create", "name": "fixture", "content": SKILL},
        {"action": "write_file", "name": "fixture", "file_path": "scripts/main.py", "file_content": "def run(): return 1\n"},
        {"action": "write_file", "name": "fixture", "file_path": "workflows.json",
         "file_content": json.dumps({"entry_point": "scripts/main.py"})},
        {"action": "write_file", "name": "fixture", "file_path": "examples/case.json", "file_content": '{"args": {}}'},
    ]))


def edit(script, revision):
    return json.loads(skill_manage("write_file", "fixture", file_path="scripts/main.py",
                                  file_content=script, expected_revision=revision))


def test_exact_candidate_validates_without_execution_before_publication(publication, tmp_path):
    root, seen, manager = publication
    created = create_package()
    assert created["success"], created
    before = read_package(root / "fixture")
    revision = package_revision(before)
    assert created["workflow_revisions"]["fixture"] == revision == seen[-1]
    marker = tmp_path / "must-not-exist"
    script = f"from pathlib import Path\nPath({str(marker)!r}).write_text('executed')\ndef run(): return 2\n"
    old_validator = manager._workflow_validators["fixture"]
    def validate_before_live_change(snapshot):
        assert read_package(root / "fixture") == before
        old_validator(snapshot)
    manager._workflow_validators["fixture"] = validate_before_live_change
    result = edit(script, revision)
    assert result["success"], result
    assert not marker.exists()
    assert read_package(root / "fixture")["scripts/main.py"].decode() == script
    assert result["workflow_revisions"]["fixture"] == seen[-1]
    assert not edit("def run(): return 3\n", revision)["success"]


def test_failed_missing_and_bypassed_validation_leave_live_revision(publication):
    root, _seen, manager = publication
    assert create_package()["success"]
    before = read_package(root / "fixture")
    revision = package_revision(before)
    assert not edit("def broken(\n", revision)["success"]
    assert not edit("def run(): return 2\n", None)["success"]
    manager._workflow_validators.clear()
    unavailable = edit("def run(): return 2\n", revision)
    assert not unavailable["success"] and "validation_unavailable" in unavailable["error"]
    assert read_package(root / "fixture") == before


def test_concurrent_review_cas_and_handler_failure_roll_back(publication, monkeypatch):
    root, _, _ = publication
    assert create_package()["success"]
    revision = package_revision(read_package(root / "fixture"))
    gate = threading.Barrier(2)
    def writer(value):
        gate.wait(timeout=5)
        return edit(f"def run(): return {value}\n", revision)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(writer, [2, 3]))
    assert sum(result["success"] for result in results) == 1
    before = read_package(root / "fixture")
    revision = package_revision(before)
    import tools.skill_manager_tool as smt
    original = smt._write_file
    def failing_write(name, file_path, content):
        if file_path == "examples/case.json":
            raise OSError("fixture disk failure after first operation")
        return original(name, file_path, content)
    monkeypatch.setattr(smt, "_write_file", failing_write)
    result = json.loads(skill_manage("batch", "", expected_revision=revision, operations=[
        {"action": "write_file", "name": "fixture", "file_path": "scripts/main.py", "file_content": "def run(): return 4\n"},
        {"action": "write_file", "name": "fixture", "file_path": "examples/case.json", "file_content": '{"args": {"x": 1}}'},
    ]))
    assert not result["success"]
    assert read_package(root / "fixture") == before


def test_rollback_republishes_old_snapshot_via_cas(publication):
    root, _, _ = publication
    assert create_package()["success"]
    original = read_package(root / "fixture")
    old_revision = package_revision(original)
    updated = edit("def run(): return 2\n", old_revision)
    assert updated["success"]
    restored = edit(original["scripts/main.py"].decode(), updated["workflow_revisions"]["fixture"])
    assert restored["success"] and restored["workflow_revisions"]["fixture"] == old_revision


def test_background_review_keeps_read_guard_and_cancellation(publication, monkeypatch):
    from tools.skill_manager_guards import mark_background_review_skill_read, background_review_write_scope
    root, _, _ = publication
    assert create_package()["success"]
    target = root / "fixture" / "scripts/main.py"
    revision = package_revision(read_package(root / "fixture"))
    monkeypatch.setattr("tools.skill_provenance.is_background_review", lambda: True)
    refused = edit("def run(): return 2\n", revision)
    assert not refused["success"] and refused.get("_read_before_write_required")
    mark_background_review_skill_read(target)
    updated = edit("def run(): return 2\n", revision)
    assert updated["success"], updated
    before = read_package(root / "fixture")
    cancel = threading.Event()
    cancel.set()
    with background_review_write_scope(cancel):
        refused = edit("def run(): return 3\n", package_revision(before))
    assert not refused["success"]
    assert read_package(root / "fixture") == before


def test_publication_notice_reports_changed_tool_and_effect_declarations(publication):
    """Autonomous revision remains immediate and reports changed declared reach."""
    root, _, _ = publication
    assert create_package()["success"]
    def publish(declarations):
        revision = package_revision(read_package(root / "fixture"))
        manifest = {"entry_point": "scripts/main.py", "workflows": declarations}
        return json.loads(skill_manage("write_file", "fixture", file_path="workflows.json",
            file_content=json.dumps(manifest), expected_revision=revision))
    assert publish({"run": {"tools": {"old_tool": "old"}, "effect": "read-only"}})["success"]
    result = publish({"run": {"tools": {"new_tool": "new"}, "effect": "write"}})
    assert result["success"], result
    notice = result["workflow_publication_notice"]["fixture"]
    assert notice == {"tools_added": ["new_tool"], "tools_removed": ["old_tool"],
                      "effect_changes": {"run": {"before": "read-only", "after": "write"}}}
    assert "new_tool" in json.loads((root / "fixture" / "workflows.json").read_text())["workflows"]["run"]["tools"]
