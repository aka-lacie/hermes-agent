"""Retained delegation through real construction, turn prologue and disk persistence."""

import json
from copy import deepcopy
from pathlib import Path
from unittest.mock import patch

import pytest


@pytest.fixture
def harness(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("TERMINAL_CWD", str(tmp_path))
    cfg = {"compression": {"enabled": False}, "delegation": {"resumable_sessions": True},
           "terminal": {"cwd": str(tmp_path)}}
    monkeypatch.setattr("hermes_cli.config.load_config", lambda *a, **kw: cfg)
    monkeypatch.setattr("hermes_cli.config.load_config_readonly", lambda *a, **kw: cfg)
    from agent.turn_context import build_turn_context
    from hermes_state import SessionDB
    from run_agent import AIAgent
    observed = []
    children = []

    def run_conversation(child, user_message, conversation_history=None, task_id=None, **kwargs):
        from agent import conversation_loop as loop
        ctx = build_turn_context(
            child, user_message, None, conversation_history, task_id, None, None,
            restore_or_build_system_prompt=loop._restore_or_build_system_prompt,
            install_safe_stdio=loop._install_safe_stdio, sanitize_surrogates=loop._sanitize_surrogates,
            summarize_user_message_for_log=loop._summarize_user_message_for_log,
            set_session_context=loop.set_session_context, set_current_write_origin=loop.set_current_write_origin,
            ra=loop._ra,
        )
        observed.append({"session_id": child.session_id, "history": deepcopy(ctx.messages),
                         "model": child.model, "provider": child.provider, "tools": deepcopy(child.tools),
                         "prompt": child.ephemeral_system_prompt, "workspace": child._delegate_workspace})
        children.append(child)
        ctx.messages.append({"role": "assistant", "content": "Findings retained for follow-up."})
        child._flush_messages_to_session_db(ctx.messages, conversation_history)
        return {"final_response": "Findings retained for follow-up.", "completed": True,
                "messages": ctx.messages, "api_calls": 0}

    # Replace the external execution only; production construction, worker,
    # persistence prologue, result routing, registration and teardown remain.
    monkeypatch.setattr(AIAgent, "run_conversation", run_conversation)
    def credentials(route, parent):
        return {"provider": route.get("provider"), "model": route.get("model"),
                "base_url": "http://127.0.0.1:1/v1", "api_key": "test-key", "api_mode": "chat_completions"}
    monkeypatch.setattr("tools.delegate_tool._resolve_delegation_credentials", credentials)
    # This isolated host has no external completion consumer; exercise the
    # production synchronous fallback through both model-facing dispatch paths.
    monkeypatch.setattr("tools.delegate_tool_dispatch._resolve_async_wake_sid", lambda *args: None)
    with patch("agent.process_bootstrap.OpenAI"):
        db = SessionDB(db_path=tmp_path / "state.db")
        def parent(session_id="owner"):
            agent = AIAgent(api_key="test-key", base_url="http://127.0.0.1:1/v1", model="test/model",
                            provider="custom", enabled_toolsets=[], quiet_mode=True, skip_context_files=True,
                            skip_memory=True, session_db=db, session_id=session_id)
            agent._ensure_db_session()
            return agent
        yield parent, db, observed, children
        for child in children:
            child.close()
        db.close()


@pytest.mark.parametrize("surface", ["registry", "inline", "plugin"])
def test_completed_child_survives_parent_reconstruction(harness, surface):
    from tools.registry import registry
    from agent.subagent_lifecycle import SubagentLifecycleService
    make_parent, db, observed, children = harness
    parent = make_parent()
    def call(agent, args):
        if surface == "inline":
            return json.loads(agent._dispatch_delegate_task(args))
        if surface == "plugin":
            service = SubagentLifecycleService(lambda: agent)
            if args.get("action") == "resume":
                return service.resume_session(args["session_id"], args["message"], model=args.get("model"))
            return service.launch_session(**args["tasks"][0])
        return json.loads(registry.get_entry("delegate_task").handler(args, parent_agent=agent))

    first = call(parent, {"tasks": [{"goal": "Investigate the first issue", "title": "Review", "fork_context": False}]})
    assert first["results"][0]["status"] == "completed", first
    sid = first["results"][0]["session_id"]
    assert children[0]._session_messages == []  # original object actually torn down
    parent.close()
    parent = make_parent()
    second = call(parent, {"action": "resume", "session_id": sid, "message": "Now check the regression", "model": "test/next"})
    assert second["results"][0]["session_id"] == sid, second
    assert second["results"][0]["status"] == "completed", second
    assert children[0] is not children[1]
    assert observed[1]["provider"] == observed[0]["provider"]
    assert observed[1]["model"] == "test/next"
    content = [m["content"] for m in observed[1]["history"]]
    assert any("Investigate the first issue" in c for c in content)
    assert "Findings retained for follow-up." in content
    persisted = db.get_messages_as_conversation(sid)
    assert sum("Now check the regression" in str(m["content"]) for m in persisted) == 1
    roster = json.loads(registry.get_entry("delegate_task").handler({"action": "list"}, parent_agent=parent))
    assert roster["subagents"][0]["status"] == "idle"
    assert roster["subagents"][0]["title"] == "Review"
    assert roster["subagents"][0]["assignment"] == "Now check the regression"
    closed = json.loads(registry.get_entry("delegate_task").handler({"action": "close", "session_id": sid}, parent_agent=parent))
    assert closed["status"] == "closed"
    refused = json.loads(registry.get_entry("delegate_task").handler(
        {"action": "resume", "session_id": sid, "message": "Try again"}, parent_agent=parent))
    assert "closed" in refused["error"].lower()
    assert len(observed) == 2
    parent.close()


def test_resume_rebuilds_current_tool_schemas(harness, monkeypatch):
    import model_tools
    from tools.delegate_tool import delegate_task
    make_parent, _, observed, _ = harness
    parent = make_parent()
    schema = {"type": "function", "function": {"name": "probe", "description": "old", "parameters": {"type": "object"}}}
    monkeypatch.setattr(model_tools, "get_tool_definitions", lambda **kw: [deepcopy(schema)])
    first = json.loads(delegate_task(tasks=[{"goal": "First capability check", "fork_context": False}], parent_agent=parent))
    sid = first["results"][0]["session_id"]
    schema["function"]["description"] = "current"
    second = json.loads(delegate_task(action="resume", session_id=sid, message="Check current capability", parent_agent=parent))
    assert second["results"][0]["status"] == "completed", second
    assert observed[0]["tools"][0]["function"]["description"] == "old"
    assert observed[1]["tools"][0]["function"]["description"] == "current"
    monkeypatch.setattr(model_tools, "get_tool_definitions", lambda **kw: [])
    third = json.loads(delegate_task(action="resume", session_id=sid, message="Check removed capability", parent_agent=parent))
    assert third["results"][0]["status"] == "completed", third
    assert observed[2]["tools"] == []
    assert not harness[1].get_session(sid).get("tool_names")
    parent.close()


@pytest.mark.parametrize("failure_stage", ["construction", "startup"])
def test_failed_admission_releases_retained_children(harness, monkeypatch, failure_stage):
    from tools import delegate_tool as tool
    make_parent, _, observed, _ = harness
    parent = make_parent()
    if failure_stage == "construction":
        original = tool._build_child_preserving_parent_tools
        def construct(**kwargs):
            if kwargs["task_index"]:
                raise RuntimeError("second construction failed")
            return original(**kwargs)
        monkeypatch.setattr(tool, "_build_child_preserving_parent_tools", construct)
        tasks = [{"goal": "First independent assignment"}, {"goal": "Second independent assignment"}]
    else:
        def fail_start(child):
            raise RuntimeError("credential lease unavailable")
        monkeypatch.setattr(tool, "_lease_child_credential", fail_start)
        tasks = [{"goal": "Start a retained assignment"}]
    if failure_stage == "construction":
        with pytest.raises(RuntimeError, match="second construction failed"):
            tool.delegate_task(tasks=tasks, parent_agent=parent)
    else:
        result = json.loads(tool.delegate_task(tasks=tasks, parent_agent=parent))
        assert result.get("error") or result["results"][0]["status"] == "error", result
    roster = json.loads(tool.delegate_task(action="list", parent_agent=parent))
    assert roster["subagents"]
    assert all(row["status"] == "idle" for row in roster["subagents"])
    assert not observed
    parent.close()


def test_resume_keeps_workspace_and_fork_origin(harness, monkeypatch, tmp_path):
    from tools import delegate_tool as tool
    from agent.subagent_context import bind_fork_source
    make_parent, _, observed, _ = harness
    parent = make_parent()
    project = tmp_path / "reviewed-project"
    project.mkdir()
    monkeypatch.setattr(tool, "_resolve_workspace_hint", lambda parent: str(project))
    with bind_fork_source(parent, [{"role": "user", "content": "Original project requirements"}]):
        first = json.loads(tool.delegate_task(tasks=[{"goal": "Review the original project"}], parent_agent=parent))
    sid = first["results"][0]["session_id"]
    monkeypatch.setattr(tool, "_resolve_workspace_hint", lambda parent: str(tmp_path / "another-project"))
    parent._session_messages = [{"role": "user", "content": "Unrelated new parent context"}]
    second = json.loads(tool.delegate_task(action="resume", session_id=sid,
                                          message="Follow up on the original review", parent_agent=parent))
    assert second["results"][0]["status"] == "completed", second
    assert observed[1]["workspace"] == str(project)
    assert "The inherited conversation contains" in observed[1]["prompt"]
    assert "No parent conversation transcript is included" not in observed[1]["prompt"]
    assert not any("Unrelated new parent context" in str(m) for m in observed[1]["history"])
    parent.close()


def test_live_controls_use_stable_session_id(harness, monkeypatch):
    import contextvars
    import threading
    from concurrent.futures import ThreadPoolExecutor
    from run_agent import AIAgent
    from tools.delegate_tool import delegate_task
    make_parent, _, _, _ = harness
    parent = make_parent()
    started, finish = threading.Event(), threading.Event()
    original = AIAgent.run_conversation
    def blocked(child, **kwargs):
        from agent.steering import begin_input_turn, finish_input_turn
        begin_input_turn(child)
        started.set()
        try:
            assert finish.wait(20)
            return original(child, **kwargs)
        finally:
            finish_input_turn(child)
    monkeypatch.setattr(AIAgent, "run_conversation", blocked)
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(contextvars.copy_context().run, delegate_task,
                             tasks=[{"goal": "Long running review", "fork_context": False}], parent_agent=parent)
        try:
            assert started.wait(20)
            roster = json.loads(delegate_task(action="list", parent_agent=parent))
            child = roster["subagents"][0]
            assert child["status"] == "running"
            sid = child["session_id"]
            busy = json.loads(delegate_task(action="resume", session_id=sid, message="Another assignment", parent_agent=parent))
            assert "already running" in busy["error"]
            steered = json.loads(delegate_task(action="steer", session_id=sid, message="Focus on concurrency", parent_agent=parent))
            assert steered.get("status") == "queued", steered
            stopped = json.loads(delegate_task(action="stop", session_id=sid, parent_agent=parent))
            assert stopped["status"] == "interrupt_requested", stopped
            assert stopped["session_id"] == sid
        finally:
            finish.set()
        future.result(timeout=20)
    roster = json.loads(delegate_task(action="list", parent_agent=parent))
    assert roster["subagents"][0]["status"] == "idle"
    parent.close()
