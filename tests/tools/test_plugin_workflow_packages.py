"""Plugin package execution must not confer managed-write ownership."""
import json
from pathlib import Path

import pytest

from tools.workflow_packages import read_package, snapshot_skill


@pytest.fixture
def registered(tmp_path, monkeypatch):
    from hermes_cli import plugins
    from hermes_cli.plugins import PluginContext, PluginManager, PluginManifest
    manager = PluginManager()
    monkeypatch.setattr(plugins, "get_plugin_manager", lambda: manager)
    monkeypatch.setattr(plugins, "discover_plugins", lambda **kw: None)
    root = tmp_path / "provider" / "package"
    root.mkdir(parents=True)
    (root / "SKILL.md").write_text("---\nname: package\ndescription: Fixture.\n---\n")
    (root / "asset.bin").write_bytes(b"\x00\xff\x80")
    ctx = PluginContext(PluginManifest(name="fixture", version="0.1.0", source="user"), manager)
    ctx.register_skill("package", root / "SKILL.md")
    return root, ctx, manager


def test_registered_package_snapshot_is_read_only_and_preserves_binary(registered):
    root, _, _ = registered
    snap = snapshot_skill("fixture:package")
    assert snap["read_only"] is True
    assert (Path(snap["path"]) / "asset.bin").read_bytes() == b"\x00\xff\x80"
    (root / "asset.bin").write_bytes(b"new")
    updated = snapshot_skill("fixture:package")
    assert updated["revision"] != snap["revision"]
    assert (Path(snap["path"]) / "asset.bin").read_bytes() == b"\x00\xff\x80"


@pytest.mark.parametrize("gate", ["plugin", "skill", "missing", "unloaded"])
def test_hidden_or_missing_providers_cannot_resolve_old_snapshots(registered, monkeypatch, gate):
    root, _, manager = registered
    snapshot_skill("fixture:package")
    if gate == "plugin":
        monkeypatch.setattr("hermes_cli.plugins._get_disabled_plugins", lambda: {"fixture"})
    elif gate == "skill":
        monkeypatch.setattr("tools.skills_tool._is_skill_disabled", lambda *a: True)
    elif gate == "missing":
        (root / "SKILL.md").unlink()
    else:
        manager.remove_plugin_skill("fixture:package")
    with pytest.raises(ValueError, match="workflow_skill_unavailable"):
        snapshot_skill("fixture:package")


@pytest.mark.parametrize("action", ["edit", "patch", "write_file", "remove_file", "delete", "create"])
def test_all_managed_actions_refuse_plugin_packages_without_changing_bytes(registered, action):
    from tools.skill_manager_tool import skill_manage
    root, _, _ = registered
    before = read_package(root)
    result = json.loads(skill_manage(action=action, name="fixture:package", content="changed",
                                    file_path="asset.bin", file_content="changed", old_string="Fixture", new_string="Changed"))
    assert not result.get("success") and "plugin_skill_read_only" in result["error"]
    assert read_package(root) == before
    batch = json.loads(skill_manage(action="", name="", operations=[{"action": "patch" if action == "edit" else action, "name": "fixture:package",
        "content": "changed", "file_path": "asset.bin", "file_content": "changed", "old_string": "Fixture", "new_string": "Changed"}]))
    assert not batch.get("success") and "plugin_skill_read_only" in batch["error"]
    assert read_package(root) == before


def test_capture_rejects_out_of_band_replacement_and_symlinks(registered, monkeypatch):
    root, _, _ = registered
    import tools.workflow_packages as packages
    real = packages.read_package
    calls = []
    def replaced(path):
        files = real(path)
        if Path(path) == root and not calls:
            calls.append(True)
            (root / "asset.bin").write_bytes(b"changed during capture")
        return files
    monkeypatch.setattr(packages, "read_package", replaced)
    with pytest.raises(ValueError, match="workflow_package_changed"):
        snapshot_skill("fixture:package")
    monkeypatch.setattr(packages, "read_package", real)
    (root / "escape").symlink_to(root.parent)
    with pytest.raises(ValueError, match="symlink"):
        snapshot_skill("fixture:package")


def test_qualified_plugin_does_not_fall_back_to_colliding_ordinary_package(registered, tmp_path, monkeypatch):
    from tools import skills_tool
    from tools.skill_packages import resolve_skill_package
    root, _, manager = registered
    skills = tmp_path / "skills"
    local = skills / "fixture" / "package"
    local.mkdir(parents=True)
    (local / "SKILL.md").write_text("---\nname: package\ndescription: Ordinary collision.\n---\n")
    monkeypatch.setattr(skills_tool, "_skill_search_dirs", lambda: ([("profile", skills)], skills))
    assert resolve_skill_package("fixture:package")["path"] == root
    manager.remove_plugin_skill("fixture:package")
    with pytest.raises(ValueError, match="not registered"):
        resolve_skill_package("fixture:package")
    assert resolve_skill_package("fixture/package")["read_only"] is False


def test_disabled_provider_is_hidden_from_view_and_direct_resolution(registered, monkeypatch):
    from tools.skills_tool import skill_view
    monkeypatch.setattr("hermes_cli.plugins._get_disabled_plugins", lambda: {"fixture"})
    result = json.loads(skill_view("fixture:package", preprocess=False))
    assert not result.get("success") and "disabled" in result["error"]


def test_managed_write_rejects_a_filesystem_alias_to_plugin_root(registered, monkeypatch):
    from tools.skill_manager_tool import skill_manage
    root, _, _ = registered
    monkeypatch.setattr("tools.skill_manager_tool._find_skill", lambda name: {"path": root})
    before = read_package(root)
    result = json.loads(skill_manage(action="delete", name="ordinary-alias"))
    assert "plugin_skill_read_only" in result["error"]
    assert read_package(root) == before
