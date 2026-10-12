"""Locked, content-addressed skill snapshots shared by execution and publication."""
from __future__ import annotations

import hashlib
import os
from pathlib import Path
import shutil
import tempfile

from hermes_constants import get_hermes_home


MAX_PACKAGE_BYTES = 8 * 1024 * 1024
_IGNORED = {"__pycache__", ".git", ".pytest_cache"}


def read_package(root):
    root = Path(root)
    if root.is_symlink():
        raise ValueError("workflow_package_invalid: package root cannot be a symlink")
    files, size = {}, 0
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root)
        if any(part in _IGNORED for part in relative.parts):
            continue
        if path.is_symlink():
            raise ValueError(f"workflow_package_invalid: symlink {relative}")
        if not path.is_file():
            continue
        content = path.read_bytes()
        size += len(content)
        if size > MAX_PACKAGE_BYTES:
            raise ValueError("workflow_package_invalid: package exceeds 8 MiB staging ceiling")
        files[relative.as_posix()] = content
    return files


def package_revision(files):
    digest = hashlib.sha256()
    for name, content in sorted(files.items()):
        encoded = name.encode("utf-8")
        digest.update(len(encoded).to_bytes(8, "big"))
        digest.update(encoded)
        digest.update(len(content).to_bytes(8, "big"))
        digest.update(content)
    return digest.hexdigest()


def materialize_snapshot(files):
    """Publish a complete immutable tree, never exposing a partially copied hash."""
    from tools.skill_usage import skill_file_lock
    revision = package_revision(files)
    base = get_hermes_home() / "cache" / "workflow-snapshots"
    base.mkdir(parents=True, exist_ok=True, mode=0o700)
    target = base / revision
    with skill_file_lock(base / ".locks" / (revision + ".lock")):
        if target.exists():
            if package_revision(read_package(target)) != revision:
                raise ValueError("workflow_snapshot_corrupt: restore the recorded package revision")
            return {"revision": revision, "path": str(target)}
        staging = Path(tempfile.mkdtemp(prefix=".snapshot-", dir=base))
        try:
            for name, content in files.items():
                relative = Path(name)
                if relative.is_absolute() or ".." in relative.parts:
                    raise ValueError("workflow_package_invalid: unsafe snapshot path")
                path = staging / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(content)
                path.chmod(0o400)
            os.rename(staging, target)
        finally:
            if staging.exists():
                shutil.rmtree(staging)
    return {"revision": revision, "path": str(target)}


def snapshot_skill(name):
    """Resolve under current profile visibility and hold its mutation lock while copying."""
    from hermes_cli.plugins_loader import replacement_coordinator
    from tools.skill_packages import resolve_skill_package, _resolve_skill_package
    from tools.skill_manager_tool import _skill_mutation_lock
    # Never wait for discovery while holding replacement: loaders register on worker
    # threads. Mutation precedes replacement consistently, including nested skill_view.
    package = resolve_skill_package(name)
    source = package["path"]
    with _skill_mutation_lock(source), replacement_coordinator.transaction():
        if _resolve_skill_package(name) != package:
            raise ValueError("workflow_package_changed: provider changed before capture; resolve it again")
        before = source.stat()
        files = read_package(source)
        after = source.stat()
        current = _resolve_skill_package(name)
        if (before.st_dev, before.st_ino, before.st_mtime_ns) != (after.st_dev, after.st_ino, after.st_mtime_ns) or current != package:
            raise ValueError("workflow_package_changed: package replaced during capture; resolve it again")
        if files != read_package(source):
            raise ValueError("workflow_package_changed: files changed during capture; resolve it again")
        if "SKILL.md" not in files:
            raise ValueError("workflow_skill_unavailable: skill disappeared; resolve it again")
        return {**materialize_snapshot(files), "name": package["name"], "read_only": package["read_only"]}


def validate_candidate(files):
    """Invoke only registered static package checks, bound to the exact immutable hash."""
    from hermes_cli.plugins import get_plugin_manager
    snapshot = materialize_snapshot(files)
    validators = getattr(get_plugin_manager(), "_workflow_validators", {})
    if not validators:
        raise ValueError("workflow_validation_unavailable: enable the skill-workflows plugin")
    for validator in list(validators.values()):
        result = validator(snapshot["path"])
        if result is False or (isinstance(result, dict) and result.get("success") is False):
            raise ValueError("workflow_package_invalid: registered static validator rejected candidate")
    if package_revision(read_package(snapshot["path"])) != snapshot["revision"]:
        raise ValueError("workflow_snapshot_corrupt: static validator changed the candidate")
    return snapshot
