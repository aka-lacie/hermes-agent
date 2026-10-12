"""Static candidate validation and compare-and-swap for managed skill workflows.

Candidate construction is data-only. The existing managed handlers still own
permission/read guards, actual writes, audit and rollback; validation never
imports or executes candidate Python.
"""
from __future__ import annotations

from contextvars import ContextVar
import json
from pathlib import Path

_PUBLICATION = ContextVar("hermes_validated_workflow_publication", default=False)


def publication_active():
    return _PUBLICATION.get()


def _declared_changes(before, after):
    """Informational publication evidence, never an additional permission gate."""
    def declarations(files):
        try:
            entries = json.loads(files.get("workflows.json", b"{}")).get("workflows", {})
            tools = {tool for entry in entries.values() for tool in entry.get("tools", {})}
            effects = {name: entry.get("effect") for name, entry in entries.items()}
            return tools, effects
        except (ValueError, AttributeError, TypeError):
            return set(), {}
    old_tools, old_effects = declarations(before)
    new_tools, new_effects = declarations(after)
    return {"tools_added": sorted(new_tools - old_tools), "tools_removed": sorted(old_tools - new_tools),
            "effect_changes": {name: {"before": old_effects.get(name), "after": new_effects.get(name)}
                               for name in sorted(old_effects.keys() | new_effects.keys())
                               if old_effects.get(name) != new_effects.get(name)}}


def has_workflow_mutation(name, file_path=None):
    from tools.skill_manager_tool import _find_skill
    skill = _find_skill(name)
    return file_path == "workflows.json" or bool(skill and (skill["path"] / "workflows.json").is_file())


def _expected_for(expected_revision, name, names):
    if isinstance(expected_revision, str):
        if len(set(names)) != 1:
            raise ValueError("workflow_revision_required: use a name-to-revision map for a multi-skill batch")
        return expected_revision
    if isinstance(expected_revision, dict):
        return expected_revision.get(name)
    if expected_revision is not None:
        raise ValueError("workflow_revision_required: expected_revision must be a hash or name-to-hash map")
    return None


def check_revision(name, files, expected):
    from tools.workflow_packages import package_revision, materialize_snapshot
    current = package_revision(files)
    if expected != current:
        raise ValueError(f"workflow_revision_conflict: '{name}' is at {current}; read that snapshot, "
                         "reconcile changes, and supply its hash as expected_revision")
    if files:
        materialize_snapshot(files)
    return current


def _apply_candidate(files, operation, root):
    from tools import skill_manager_tool as smt
    action = operation["action"]
    if action == "create" or action == "edit" or (action == "patch" and operation.get("content")):
        content = operation.get("content", "")
        error = smt._validate_frontmatter(content, new_skill=action == "create")
        if error:
            raise ValueError(error)
        files["SKILL.md"] = content.encode("utf-8")
        return
    relative = operation.get("file_path") or "SKILL.md"
    target, error = smt._resolve_supporting_file(root, relative)
    if error:
        raise ValueError(error["error"])
    relative = target.relative_to(root).as_posix()
    if action == "remove_file":
        if relative not in files:
            raise ValueError(f"File not found: {relative}")
        del files[relative]
    elif action == "write_file":
        files[relative] = operation["file_content"].encode("utf-8")
    elif action == "patch":
        if relative not in files:
            raise ValueError(f"File not found: {relative}")
        from tools.fuzzy_match import fuzzy_find_and_replace
        content = files[relative].decode("utf-8-sig")
        changed, _, _, error = fuzzy_find_and_replace(
            content, operation.get("old_string"), operation.get("new_string"), operation.get("replace_all", False))
        if error:
            raise ValueError(error)
        files[relative] = changed.encode("utf-8")


def prepare_publications(operations, names, expected_revision):
    """Called with the batch's existing locks held, before ANY live mutation."""
    from tools import skill_manager_tool as smt
    from tools.workflow_packages import read_package, validate_candidate, package_revision
    prepared = {}
    for name in dict.fromkeys(names):
        existing = smt._find_skill(name)
        root = Path(existing["path"]) if existing else smt._resolve_skill_dir(name)
        relevant = [op for op, target in zip(operations, names) if target == name]
        workflow = (root / "workflows.json").is_file() or any(op.get("file_path") == "workflows.json" for op in relevant)
        expected = _expected_for(expected_revision, name, names)
        if not workflow and expected is None:
            continue
        files = read_package(root) if root.exists() else {}
        before = check_revision(name, files, expected)
        candidate = dict(files)
        for operation in relevant:
            _apply_candidate(candidate, operation, root)
        if workflow:
            snapshot = validate_candidate(candidate)
            validated = read_package(snapshot["path"])
            if package_revision(validated) != snapshot["revision"] or validated != candidate:
                raise ValueError("workflow_validation_changed_candidate: validator changed the immutable package")
            prepared[name] = {"base_revision": before, **snapshot,
                              "notice": _declared_changes(files, candidate)}
    return prepared


def verify_publications(prepared):
    from tools.skill_manager_tool import _find_skill
    from tools.workflow_packages import read_package, package_revision
    for name, snapshot in prepared.items():
        found = _find_skill(name)
        if not found or package_revision(read_package(found["path"])) != snapshot["revision"]:
            raise ValueError(f"workflow_publication_mismatch: '{name}' differs from its validated candidate")
