"""Skill preload contracts: actual child tools, qualified names, ordered union and stable prefixes."""
from unittest.mock import patch

import pytest


def write_skill(home, name, body, requires="", directory=None):
    path = home / "skills" / (directory or name) / "SKILL.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"---\nname: {name}\ndescription: Test skill.\n{requires}---\n\n{body}\n")
    return path


@pytest.mark.parametrize("depth", [0, 1, 2])
def test_real_factory_child_filters_requires_tools_and_inherits_auto_load(tmp_path, monkeypatch, depth):
    from agent.skill_preload import prepare_skill_preloads
    from agent.subagent_lifecycle import bind_subagent_parent
    from agent.system_prompt import build_system_prompt
    from hermes_cli import plugins
    from hermes_cli.plugins import PluginContext, PluginManager, PluginManifest
    from run_agent import AIAgent
    from tools.delegate_tool_results import _build_child_preserving_parent_tools

    write_skill(tmp_path, "delegation-taste", "TASTE_BODY", "requires_tools: [delegate_task]\n")
    write_skill(tmp_path, "profile-skill", "PROFILE_BODY")
    write_skill(tmp_path, "explicit-skill", "EXPLICIT_BODY")
    write_skill(tmp_path, "jev-skill", "JEV_BODY")
    (tmp_path / "config.yaml").write_text(
        "delegation:\n  orchestrator_enabled: true\n  max_spawn_depth: 2\n"
        "compression:\n  enabled: false\nskills:\n  auto_load: [profile-skill, delegation-taste]\n")
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    agents = []
    with patch("agent.process_bootstrap.OpenAI"):
        try:
            agents.append(AIAgent(api_key="test", base_url="http://127.0.0.1:1/v1", model="test/model",
                                  provider="custom", platform="cli", enabled_toolsets=["file", "skills", "delegation"],
                                  quiet_mode=True, skip_memory=True))
            for _ in range(depth):
                agents.append(_build_child_preserving_parent_tools(
                    task_index=0, goal="Inspect one component", context=None, toolsets=None,
                    model=None, max_iterations=3, task_count=1, parent_agent=agents[-1]))
            active = agents[-1]
            active.preload_skills = ["delegation-taste", "explicit-skill", "profile-skill"]
            before = set(active.valid_tool_names)
            manager = PluginManager()
            manager._discovered = True
            ctx = PluginContext(PluginManifest(name="preload-probe", key="preload-probe", source="user"), manager)
            ctx.register_hook("pre_skill_preload", lambda **kw: {"preload_skills": ["jev-skill", "explicit-skill"]})
            monkeypatch.setattr(plugins, "_plugin_manager", manager)
            with bind_subagent_parent(active):
                prepare_skill_preloads(active, user_message="Review it")
                prompt = build_system_prompt(active)
                rebuilt = build_system_prompt(active)
            assert ("delegate_task" in before) == (depth < 2)
            assert ("TASTE_BODY" in prompt) == (depth < 2)
            for body in ("PROFILE_BODY", "EXPLICIT_BODY", "JEV_BODY"):
                assert prompt.count(body) == 1
            names = active._auto_load_skills_result[1]
            assert names == (["profile-skill", "delegation-taste", "explicit-skill", "jev-skill"] if depth < 2
                             else ["profile-skill", "explicit-skill", "jev-skill"])
            assert before == active.valid_tool_names  # requirements never grant a tool
            assert prompt == rebuilt
        finally:
            for active in reversed(agents):
                active.close()


def test_plugin_qualified_preload_and_auto_load_share_real_resolver(tmp_path, monkeypatch):
    from agent.skill_commands import build_auto_load_prompt, build_preloaded_skills_prompt
    from hermes_cli import plugins
    from hermes_cli.plugins import PluginContext, PluginManager, PluginManifest
    manager = PluginManager()
    manager._discovered = True
    ctx = PluginContext(PluginManifest(name="preload-probe", key="preload-probe", source="user"), manager)
    plugin_skill = write_skill(tmp_path, "using-probe", "PLUGIN_BODY", "requires_tools: [delegate_task]\n")
    ctx.register_skill("using-probe", plugin_skill)
    monkeypatch.setattr(plugins, "_plugin_manager", manager)
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    names = ["preload-probe:using-probe", "plugin:preload-probe:using-probe"]
    for tools, expected in (({"delegate_task"}, True), (set(), False)):
        prompt, loaded, _ = build_preloaded_skills_prompt(names, valid_tool_names=tools, home_override=tmp_path)
        assert ("PLUGIN_BODY" in prompt) == expected
        assert prompt.count("PLUGIN_BODY") == int(expected)
        assert loaded == ([names[0]] if expected else [])
        auto, loaded_auto, _ = build_auto_load_prompt(
            user_config={"skills": {"auto_load": names}}, valid_tool_names=tools, home_override=tmp_path)
        assert ("PLUGIN_BODY" in auto) == expected
        assert loaded_auto == loaded


def test_deferred_tools_and_toolsets_are_eligibility_not_activation(tmp_path, monkeypatch):
    from agent.skill_commands import build_preloaded_skills_prompt
    write_skill(tmp_path, "remote-work", "DEFERRED_BODY", "requires_tools: [remote_lookup]\nrequires_toolsets: [remote]\n")
    write_skill(tmp_path, "blocked", "BLOCKED_BODY", "requires_tools: [delegate_task]\ndeps: [never-install]\n")
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    tools = {"skill_view", "tool_search", "tool_call", "remote_lookup"}
    with patch("pm.ensure") as ensure:
        prompt, names, _ = build_preloaded_skills_prompt(
            ["remote-work", "blocked"], valid_tool_names=tools, available_toolsets={"remote"}, home_override=tmp_path)
        ensure.assert_not_called()
    assert names == ["remote-work"] and "DEFERRED_BODY" in prompt and "BLOCKED_BODY" not in prompt
    assert "delegate_task" not in tools
    prompt, names, _ = build_preloaded_skills_prompt(
        ["remote-work"], valid_tool_names={"tool_search", "tool_call"}, home_override=tmp_path)
    assert prompt == "" and names == []  # discovery bridge alone is not evidence of tool access


def test_aliases_deduplicate_canonical_local_name(tmp_path, monkeypatch):
    from agent.skill_commands import build_auto_load_prompt
    write_skill(tmp_path, "canonical", "ONE_BODY", directory="category/canonical")
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    prompt, names, _ = build_auto_load_prompt(
        user_config={"skills": {"auto_load": ["canonical"]}},
        preload_skills=["category/canonical", "canonical"], valid_tool_names={"skill_view"}, home_override=tmp_path)
    assert names == ["canonical"] and prompt.count("ONE_BODY") == 1


def test_extension_union_validation_and_preload_selector_runs_once(monkeypatch):
    from types import SimpleNamespace
    from agent.skill_preload import prepare_skill_preloads
    from agent.subagent_extension import resolve_child_options
    class Extension:
        def child_options(self, task):
            return {"preload_skills": ["profile", "taste"]}
    options = resolve_child_options(Extension(), {"skills": ["taste", "explicit"]})
    assert options["preload_skills"] == ["profile", "taste", "explicit"]
    with pytest.raises(ValueError):
        resolve_child_options(Extension(), {"skills": "not-a-list"})
    agent = SimpleNamespace(preload_skills=options["preload_skills"], session_id="s", platform="subagent")
    with patch("hermes_cli.lifecycle.invoke_hook", return_value=[{"preload_skills": ["jev", "profile"]}]) as invoke:
        prepare_skill_preloads(agent, user_message="do it")
        prepare_skill_preloads(agent, user_message="later")
    assert invoke.call_count == 1
    assert agent.preload_skills == ["profile", "taste", "explicit", "jev"]


def test_ordinary_resume_keeps_prefix_and_selector_is_not_run():
    from types import SimpleNamespace
    from agent.skill_preload import prepare_skill_preloads
    agent = SimpleNamespace(preload_skills=[], session_id="resume", platform="cli")
    with patch("hermes_cli.lifecycle.invoke_hook") as invoke:
        prepare_skill_preloads(agent, user_message="continue", conversation_history=[{"role": "user", "content": "old"}])
    invoke.assert_not_called()
    assert agent.preload_skills == []
