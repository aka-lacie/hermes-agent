"""Content registrars on ``PluginContext``: plugin skills and Automation Blueprints.

Bound into the class body (``from hermes_cli.plugins_content import ...``), so they are ordinary
``ctx.register_*`` methods and get the same abandoned-load guard as every other registrar.
"""

from __future__ import annotations

import logging
import uuid
from pathlib import Path
from typing import Any, Iterable, Mapping, Optional

from hermes_cli.plugins_ledger import PluginRegistration
from hermes_cli.plugins_loader import _serialized_replacement

# Same logger as the rest of the ctx surface, so plugin-load diagnostics stay in one place.
logger = logging.getLogger("hermes_cli.plugins")

__all__ = ["register_automation_blueprint", "register_skill"]


@_serialized_replacement
def register_skill(
    self, name: str, path: Path, description: str = "",
    frontmatter: Optional[Mapping[str, Any]] = None,
) -> PluginRegistration:
    """Register a read-only skill resolvable as ``'<plugin_name>:<name>'`` via ``skill_view()``,
    listed by ``skills_list`` and in the system prompt's ``<available_skills>`` while the plugin
    is enabled. Not copied into ``~/.hermes/skills/``. Raises ``ValueError`` (``':'``/invalid
    chars) or ``FileNotFoundError``."""
    from agent.skill_utils import _NAMESPACE_RE
    if ":" in name:
        raise ValueError(f"Skill name '{name}' must not contain ':' (the namespace is derived from the "
                         f"plugin name '{self.manifest.name}' automatically).")
    if not name or not _NAMESPACE_RE.match(name):
        raise ValueError(f"Invalid skill name '{name}'. Must match [a-zA-Z0-9_-]+.")
    # Plugin register() helpers commonly pass the SKILL.md location as str
    # (PluginManifest.path is stored as str); the registry and find_plugin_skill()
    # promise a Path downstream.
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(f"SKILL.md not found at {path}")
    namespace = self.manifest.skill_namespace or self.manifest.name
    qualified = f"{namespace}:{name}"
    if self.manifest.portable and qualified in self._manager._plugin_skills:
        raise ValueError(f"Plugin skill '{qualified}' is already registered")
    entry = {
        "registration_id": uuid.uuid4().hex,
        "path": path, "plugin": namespace, "plugin_key": self.plugin_id, "bare_name": name,
        "description": description, "frontmatter": dict(frontmatter or {}),
    }
    registered = self._register_entry("skill", qualified, self._manager._plugin_skills, entry,
                                      "Plugin %s registered skill: %s", qualified)
    vars(self._manager).setdefault("_plugin_skill_namespaces", set()).add(namespace)
    return registered

def register_automation_blueprint(
    self, key: str, *, title: str, description: str, schedule_template: str,
    prompt_template: str, category: str = "general", slots: Iterable[Any] = (),
    deliver_default: str = "origin", skills: Iterable[str] = (), tags: Iterable[str] = (),
) -> Optional[PluginRegistration]:
    """Add an Automation Blueprint to this profile's catalog as ``'<plugin_name>:<key>'``
    (``/blueprint``, the dashboard/Desktop gallery). Fields mirror
    :class:`cron.blueprint_catalog.AutomationBlueprint`; ``slots`` are ``BlueprintSlot`` field
    mappings. Malformed input or a duplicate key logs a warning and returns ``None``."""
    from cron.blueprint_plugins import build_plugin_blueprint
    try:
        blueprint = build_plugin_blueprint(
            self.manifest.name, key, title=title, description=description,
            schedule_template=schedule_template, prompt_template=prompt_template, category=category,
            slots=slots, deliver_default=deliver_default, skills=skills, tags=tags,
        )
    except ValueError as exc:
        logger.warning("Plugin '%s' automation blueprint %r rejected: %s", self.manifest.name, key, exc)
        return None
    if blueprint.key in self._manager._automation_blueprints:
        logger.warning("Plugin '%s' automation blueprint %r is already registered",
                       self.manifest.name, blueprint.key)
        return None
    return self._register_entry("automation_blueprint", blueprint.key,
                                self._manager._automation_blueprints, blueprint,
                                "Plugin %s registered automation blueprint: %s", blueprint.key)


def is_plugin_skill_namespace(self, namespace: str) -> bool:
    """Known provider namespaces remain reserved while a provider is disabled/unloaded."""
    return (namespace in vars(self).get("_plugin_skill_namespaces", set())
            or any(qn.startswith(namespace + ":") for qn in self._plugin_skills)
            or any((p.manifest.skill_namespace or p.manifest.name) == namespace for p in self._plugins.values()))

def get_plugin_skill_registration(self, qualified_name: str) -> Optional[dict]:
    """Public read-only package identity. A replacement receives a new registration id."""
    entry = self._plugin_skills.get(qualified_name)
    if entry is None:
        return None
    return {key: entry.get(key) for key in ("path", "plugin", "plugin_key", "registration_id")}

def list_plugin_skills(self, plugin_name: str) -> list[str]:
    """Return sorted bare names of all skills registered by *plugin_name*."""
    prefix = f"{plugin_name}:"
    return sorted(e["bare_name"] for qn, e in self._plugin_skills.items() if qn.startswith(prefix))

def list_plugin_skill_metadata(self) -> list[dict[str, Any]]:
    """Return progressive-disclosure metadata for registered plugin skills."""
    return [
        {
            "name": qualified, "description": str(entry.get("description", "")),
            "category": "plugin", "frontmatter": dict(entry.get("frontmatter", {})),
        } for qualified, entry in sorted(self._plugin_skills.items())
    ]
