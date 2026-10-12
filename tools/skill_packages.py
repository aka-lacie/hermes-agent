"""Public, non-rendering skill package lookup shared by workflows and skill views."""
from pathlib import Path


def _unavailable(message):
    raise ValueError("workflow_skill_unavailable: " + message)


def _plugin_record(name):
    from hermes_cli.plugins import get_plugin_manager
    return get_plugin_manager().get_plugin_skill_registration(name)


def resolve_skill_package(name):
    """Return canonical name, source path and ownership without rendering/importing a skill."""
    from hermes_cli.plugins import discover_plugins
    discover_plugins()
    return _resolve_skill_package(name)


def _resolve_skill_package(name):
    """Already-discovered lookup, safe while holding the replacement coordinator."""
    from tools import skills_tool as skills
    if error := skills._skill_lookup_path_error(name):
        _unavailable(error)
    category_name = None
    if ":" in name:
        from agent.skill_utils import is_valid_namespace, parse_qualified_name
        from hermes_cli.plugins import get_plugin_manager
        namespace, bare = parse_qualified_name(name)
        if not is_valid_namespace(namespace):
            _unavailable("Invalid skill namespace")
        record = _plugin_record(name)
        if record is not None:
            return _resolve_plugin(name, record)
        if get_plugin_manager().list_plugin_skills(namespace) or get_plugin_manager().is_plugin_skill_namespace(namespace):
            _unavailable(f"Skill {name!r} is not registered by the active plugin")
        category_name = f"{namespace}/{bare}" if bare else None
    roots, _ = skills._skill_search_dirs()
    error, directory, skill_md = skills._locate_skill(name, category_name, roots)
    if error is not None or directory is None:
        _unavailable(f"{name!r} is not a visible unambiguous directory skill")
    # Metadata lookup must not consume the renderer's read/mark boundary: review
    # freshness records the exact bytes subsequently returned by skill_view.
    frontmatter = skills._safe_frontmatter(path=skill_md)
    relative = skills._owned_relative(directory, skill_md, [p for _, p in roots])
    declared = frontmatter.get("name", directory.name)
    if skills._is_skill_disabled(declared, relative) and (skills._is_skill_disabled(declared) or any(
            row["path"] == skill_md and row["load_name"] == relative
            for row in skills._skill_catalog(skip_disabled=True, include_hidden=True))):
        _unavailable(f"Skill {name!r} is disabled")
    if not skills.skill_matches_platform(frontmatter):
        _unavailable(f"Skill {name!r} is not supported on this platform")
    return {"name": relative, "path": Path(directory), "read_only": False, "registration_id": None}


def _resolve_plugin(name, record):
    from hermes_cli.plugins import _get_disabled_plugins
    from tools import skills_tool as skills
    if record["plugin"] in _get_disabled_plugins() or record.get("plugin_key") in _get_disabled_plugins():
        _unavailable(f"Plugin providing {name!r} is disabled")
    path = Path(record["path"])
    if not path.is_file():
        _unavailable(f"Skill {name!r} no longer exists; reload its plugin")
    if skills._is_skill_disabled(name):
        _unavailable(f"Skill {name!r} is disabled")
    frontmatter = skills._safe_frontmatter(path=path)
    if not skills.skill_matches_platform(frontmatter):
        _unavailable(f"Skill {name!r} is not supported on this platform")
    return {"name": name, "path": path.parent, "read_only": True,
            "registration_id": record["registration_id"], "plugin": record["plugin"]}


def list_skill_packages():
    """Visible directory packages; ambiguity and ownership are resolved by the same lookup."""
    from hermes_cli.plugins import discover_plugins, get_plugin_manager
    from tools.skills_tool import _skill_catalog
    discover_plugins()
    names = {row["load_name"] for row in _skill_catalog() if row.get("load_name")}
    names.update(row["name"] for row in get_plugin_manager().list_plugin_skill_metadata())
    result = []
    for name in sorted(names):
        try:
            result.append(resolve_skill_package(name))
        except ValueError:
            continue  # hidden/disabled/platform-ineligible entries are not discovery results
    return result


def plugin_write_refusal(name):
    """All managed writes reject plugin ownership, including filesystem aliases."""
    from hermes_cli.plugins import discover_plugins, get_plugin_manager
    discover_plugins()
    manager = get_plugin_manager()
    if manager.get_plugin_skill_registration(name) is not None:
        return "plugin_skill_read_only: plugin packages can run but cannot be changed through skill_manage"
    from tools.skill_manager_tool import _find_skill
    found = _find_skill(name) if name else None
    if found:
        root = Path(found["path"]).resolve()
        for item in manager.list_plugin_skill_metadata():
            record = manager.get_plugin_skill_registration(item["name"])
            if record and root == Path(record["path"]).parent.resolve():
                return "plugin_skill_read_only: this skill path belongs to a plugin"
    return None
