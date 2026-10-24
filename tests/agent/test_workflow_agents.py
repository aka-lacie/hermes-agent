"""Accepted workflow contracts: real retained admission/leases and result capture.

Only child/provider construction is simulated. These tests prevent admission
bypass, post-validation clipping, foreign continuation, repair tool execution,
rolling-map stalls, and resident release before abandoned workers settle.
"""
import json
import threading
import time
from types import SimpleNamespace

import pytest

from agent.code_bridge import CodeCallContext, bind_cell_authority
from agent.subagent_lifecycle import SubagentLifecycleService
from agent.tool_invocation import CallContext, bind_call_context
from agent.workflow_agents import AgentError, AgentOutputError, AgentAdmissionError, AgentTimeoutError
from agent.workflow_agents_admission import WorkflowAdmission
from hermes_state import SessionDB
from tools.code_kernel import CellAuthority


SCHEMA = {"type": "object", "properties": {"answer": {"type": "string"}}, "required": ["answer"]}


@pytest.fixture
def workflow(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    from tools import delegate_tool as delegate
    from tools import delegate_tool_config as config
    from agent import workflow_agents_admission as admission
    db = SessionDB(db_path=tmp_path / "state.db")
    db.create_session("parent", "cli")
    parent = SimpleNamespace(session_id="parent", _session_db=db, model="test-model", provider="custom",
                             requested_provider="custom", base_url="http://fixture.invalid/v1", api_key="fixture",
                             _delegate_depth=0, _current_task_id="parent-task", valid_tool_names=["execute_code"],
                             _session_messages=[{"role": "user", "content": "acceptance boundary"}])
    monkeypatch.setattr(delegate, "_load_config", lambda: {"max_summary_chars": 100, "max_spawn_depth": 3})
    monkeypatch.setattr(config, "_load_config", lambda: {"max_summary_chars": 100, "max_spawn_depth": 3})
    monkeypatch.setattr(delegate, "is_spawn_paused", lambda: False)
    monkeypatch.setattr(delegate, "_resolve_delegation_credentials", lambda cfg, parent: {
        "model": cfg.get("model", "test-model"), "provider": "custom", "base_url": parent.base_url,
        "api_key": "fixture", "api_mode": "chat_completions"})
    monkeypatch.setattr("agent.delegated_review.finish_delegated_review", lambda child: None)
    monkeypatch.setattr("tools.delegate_sessions._cancel_review", lambda *a: None)
    monkeypatch.setattr("agent.subagent_extension.get_subagent_extension", lambda: None)
    pool = WorkflowAdmission(active_limit=2, resident_limit=4, queue_limit=256)
    monkeypatch.setattr(admission, "HOST_ADMISSION", pool)
    children = []
    behavior = {"text": json.dumps({"answer": "ok"}), "repair": '{"answer":"repaired"}', "tools": []}

    class Child:
        tool_progress_callback = None
        _credential_pool = None
        _subagent_id = None
        _delegate_role = "leaf"
        _delegate_depth = 1
        _delegate_workspace = None
        _delegate_saved_tool_names = []
        valid_tool_names = ["execute_code"]
        session_prompt_tokens = session_completion_tokens = session_estimated_cost_usd = 0
        session_api_calls = 0
        context_compressor = SimpleNamespace()
        api_mode = "chat_completions"
        _current_turn_timestamp = time.time()
        ephemeral_system_prompt = None

        def _copy_reasoning_content_for_api(self, original, outgoing):
            pass

        def _should_sanitize_tool_calls(self):
            return False
        provider = requested_provider = "custom"
        base_url = parent.base_url
        model = "test-model"

        def __init__(self, kwargs):
            self.session_id = kwargs.get("session_id") or f"child-{len(children)}"
            self.kwargs = kwargs
            self._session_db = db
            self._delegate_forked_context = kwargs.get("has_fork_context", False)
            self.calls = []
            self.repair_calls = 0
            self.closed = threading.Event()
            self.interrupted = threading.Event()

        def _ensure_db_session(self):
            db.create_session(self.session_id, "subagent", parent_session_id="parent", model_config={"_delegate_from": "parent"})

        def run_conversation(self, user_message, **kwargs):
            self.calls.append((user_message, kwargs))
            if behavior.get("run"):
                return behavior["run"](self, user_message, kwargs)
            messages = [{"role": "user", "content": user_message}, {"role": "assistant", "content": behavior["text"]}]
            return {"final_response": behavior["text"], "completed": True, "api_calls": 1, "messages": messages}

        def _build_api_kwargs(self, messages):
            return {"messages": messages, "tools": [{"name": "terminal"}]}

        def _interruptible_api_call(self, kwargs):
            self.repair_calls += 1
            assert kwargs["tools"]  # Advertised tools cannot sneak through the response-only boundary.
            return SimpleNamespace(content=behavior["repair"], tool_calls=behavior["tools"])

        def _get_transport(self):
            return SimpleNamespace(normalize_response=lambda response: response)

        def _flush_messages_to_session_db(self, messages, _):
            return True

        def hard_interrupt(self, reason, **kwargs):
            self.interrupted.set()

        def get_activity_summary(self):
            return {"api_call_count": 1, "max_iterations": 10, "current_tool": None}

        def close(self):
            self.closed.set()

    def build(**kwargs):
        child = Child(kwargs)
        children.append(child)
        return child
    monkeypatch.setattr(delegate, "_build_child_agent", build)
    context = CallContext(parent, "parent-task", "outer-call")
    with bind_call_context(context):
        authority = CellAuthority("parent-task", timeout=30)
        with bind_cell_authority(authority):
            code = CodeCallContext(context)
    service = SubagentLifecycleService(lambda: parent)
    yield SimpleNamespace(service=service, code=code, children=children, behavior=behavior, pool=pool, parent=parent, db=db)
    authority.retire()
    db.close()


def run(w, **kwargs):
    return w.service.run_workflow_agent("Choose an answer", {"value": 3}, SCHEMA, code_context=w.code, **kwargs)


def test_retained_child_complete_output_fork_boundary_and_resume(workflow):
    w = workflow
    w.behavior["text"] = json.dumps({"answer": "x" * 40_000})
    w.parent._session_messages.append({"role": "user", "content": "too late"})
    first = run(w, fork_context=True)
    assert first["output"]["answer"] == "x" * 40_000  # Captured before the 100-char model budget.
    assert w.children[0].calls[0][1]["conversation_history"] == [{"role": "user", "content": "acceptance boundary"}]
    next_result = run(w, session_id=first["session_id"])
    assert next_result["session_id"] == first["session_id"]
    assert "conversation_history" not in w.children[1].calls[0][1]  # Empty persisted fixture history, never a parent re-fork.
    assert w.pool.active == w.pool.resident == 0
    with pytest.raises(AgentError):
        run(w, session_id="parent")
    assert len(w.children) == 2


def test_normal_pause_and_depth_admission_and_fresh_context(workflow, monkeypatch):
    from tools import delegate_tool as delegate
    w = workflow
    monkeypatch.setattr(delegate, "is_spawn_paused", lambda: True)
    with pytest.raises(AgentError, match="paused"):
        run(w)
    assert not w.children
    monkeypatch.setattr(delegate, "is_spawn_paused", lambda: False)
    w.parent._delegate_depth = 50
    with pytest.raises(AgentError, match="depth limit"):
        run(w)
    w.parent._delegate_depth = 0
    run(w)
    assert "conversation_history" not in w.children[0].calls[0][1]


@pytest.mark.parametrize("tools,repair,succeeds", [([], '{"answer":"repaired"}', True),
                                                  ([{"name": "terminal"}], '{"answer":"repaired"}', False),
                                                  ([], "invalid again", False)])
def test_exactly_one_same_child_response_only_repair(workflow, tools, repair, succeeds):
    w = workflow
    w.behavior.update(text="not json", tools=tools, repair=repair)
    if succeeds:
        assert run(w)["output"] == {"answer": "repaired"}
    else:
        with pytest.raises(AgentOutputError):
            run(w)
    assert len(w.children) == 1
    assert len(w.children[0].calls) == w.children[0].repair_calls == 1


def test_large_input_is_readable_by_child_and_not_inlined(workflow):
    from agent.code_artifacts import read_artifact
    w = workflow
    payload = {"value": "large" * 20_000}
    w.service.run_workflow_agent("Read input", payload, SCHEMA, code_context=w.code)
    child = w.children[0]
    assert "large" * 100 not in child.kwargs["context"]
    assert read_artifact(child._workflow_request.artifact, child.session_id) == payload
    with pytest.raises(PermissionError):
        read_artifact(child._workflow_request.artifact, "foreign-session")


def test_rolling_map_replaces_finished_work_before_slow_sibling(workflow):
    w = workflow
    slow_started, third_started = threading.Event(), threading.Event()
    def behavior(child, message, kwargs):
        if message == "0":
            slow_started.set()
            assert third_started.wait(5), "scheduler waited for a whole wave"
        elif message == "1":
            assert slow_started.wait(5)
        elif message == "2":
            third_started.set()
        return {"completed": True, "final_response": json.dumps({"answer": message}), "messages": [], "api_calls": 1}
    w.behavior["run"] = behavior
    results = w.service.run_workflow_agents([{"assignment": str(i), "input": None, "output_schema": SCHEMA} for i in range(5)],
                                            max_concurrency=2, code_context=w.code)
    assert [result["output"]["answer"] for result in results] == list(map(str, range(5)))
    assert len({result["session_id"] for result in results}) == 5
    assert w.pool.active == w.pool.resident == 0


def test_deadline_keeps_resident_lease_until_running_child_exits(workflow):
    w = workflow
    started, finish = threading.Event(), threading.Event()
    def behavior(child, message, kwargs):
        started.set()
        assert finish.wait(10)
        return {"completed": True, "final_response": '{"answer":"late"}', "messages": []}
    w.behavior["run"] = behavior
    w.code.authority.deadline = time.monotonic() + .3
    try:
        with pytest.raises(AgentTimeoutError):
            run(w)
        assert started.is_set()
        assert w.children[0].interrupted.is_set()
        assert w.pool.resident == 1
        assert not w.children[0].closed.is_set()
    finally:
        finish.set()
        assert w.children[0].closed.wait(5)
    deadline = time.monotonic() + 5
    while w.pool.resident and time.monotonic() < deadline:
        time.sleep(.01)
    assert w.pool.resident == 0


def test_nested_resident_ceiling_is_actionable_and_releases_active_slot():
    pool = WorkflowAdmission(active_limit=1, resident_limit=1)
    parent = pool.acquire(lambda: None)
    try:
        with parent.suspend(lambda: None):
            assert pool.active == 0 and pool.resident == 1
            with pytest.raises(AgentAdmissionError, match="Resident child limit"):
                pool.acquire(lambda: None, nested=True)
        assert pool.active == 1
    finally:
        parent.close()


def test_model_delegation_descendant_shares_admission_and_deadline(workflow):
    from tools.delegate_tool import delegate_task
    w = workflow
    w.pool.active_limit = 1
    seen = []
    def behavior(child, message, kwargs):
        seen.append((message, w.pool.active, w.pool.resident, child._workflow_deadline))
        if message == "Choose an answer":
            result = json.loads(delegate_task(goal="nested", parent_agent=child, background=False))
            assert result["results"][0]["status"] == "completed"
            assert w.pool.resident == 1  # The waiting parent stayed resident throughout.
        return {"completed": True, "final_response": '{"answer":"done"}', "messages": [], "api_calls": 1}
    w.behavior["run"] = behavior
    assert run(w)["output"] == {"answer": "done"}
    assert [(message, active, resident) for message, active, resident, _ in seen] == [
        ("Choose an answer", 1, 1), ("nested", 1, 2)]
    assert len({deadline for *_, deadline in seen}) == 1
    assert w.pool.active == w.pool.resident == 0


def test_fail_fast_preserves_results_cancels_running_and_does_not_start_queue(workflow):
    w = workflow
    first_started, second_started = threading.Event(), threading.Event()
    def behavior(child, message, kwargs):
        if message == "0":
            first_started.set()
            assert second_started.wait(5)
            return {"completed": False, "failed": True, "error": "intentional failure", "messages": []}
        second_started.set()
        assert first_started.wait(5)
        assert child.interrupted.wait(5)
        return {"completed": False, "interrupted": True, "messages": []}
    w.behavior["run"] = behavior
    items = [{"assignment": str(i), "input": None, "output_schema": SCHEMA} for i in range(4)]
    results = w.service.run_workflow_agents(items, 2, fail_fast=True, code_context=w.code)
    assert results[0]["status"] == "failed"
    assert all(r["status"] == "cancelled" for r in results[1:])
    assert len(w.children) == 2
    assert results[0]["session_id"] and results[1]["session_id"]
    assert results[2]["session_id"] is None


def test_duplicate_continuations_and_missing_validator_fail_before_spawn(workflow, monkeypatch):
    import sys
    w = workflow
    item = {"assignment": "next", "input": None, "output_schema": SCHEMA, "session_id": "same"}
    with pytest.raises(AgentAdmissionError, match="Duplicate"):
        w.service.run_workflow_agents([item, item], 2, code_context=w.code)
    monkeypatch.setitem(sys.modules, "jsonschema.validators", None)
    with pytest.raises(AgentOutputError, match="requires jsonschema"):
        run(w)
    assert not w.children


def test_simultaneous_maps_share_the_host_queue_allowance():
    pool = WorkflowAdmission(active_limit=2, resident_limit=4, queue_limit=3)
    first = pool.reserve_items(2)
    try:
        with pytest.raises(AgentAdmissionError, match="queue is full"):
            pool.reserve_items(2)
        first.consume()
        second = pool.reserve_items(2)
        second.close()
    finally:
        first.close()
    assert pool.pending_items == 0


def test_workflow_explicit_route_survives_backend_api_change(workflow, monkeypatch):
    """Workflow SDK model/provider options retain ownership without plugin field names."""
    from tools import delegate_tool as delegate
    w = workflow
    config = {"model": "default", "provider": "original", "base_url": "https://old.invalid",
              "api_key": "old-secret", "api_mode": "chat_completions", "command": "old-command",
              "args": ["old"], "request_overrides": {"old": True}, "fallback_providers": ["original"]}
    monkeypatch.setattr(delegate, "_load_config", lambda: config)
    seen = []
    def credentials(cfg, parent):
        seen.append(dict(cfg))
        return {"model": cfg["model"], "provider": cfg["provider"],
                "base_url": cfg.get("base_url", "https://new.invalid"),
                "api_key": cfg.get("api_key", "new-secret"), "api_mode": "chat_completions"}
    monkeypatch.setattr(delegate, "_resolve_delegation_credentials", credentials)
    run(w, model="model-only")
    assert w.children[-1].kwargs["model"] == "model-only"
    assert seen[-1]["base_url"] == "https://old.invalid"
    run(w, model="other-model", provider="other")
    assert w.children[-1].kwargs["model"] == "other-model"
    assert w.children[-1].kwargs["override_api_key"] == "new-secret"
    assert seen[-1]["provider"] == "other"
    assert seen[-1]["fallback_providers"] == []
    assert not {"base_url", "api_key", "api_mode", "request_overrides", "command", "args"} & seen[-1].keys()
    assert config["api_key"] == "old-secret"
    with pytest.raises(AgentError, match="requires model"):
        run(w, provider="other")
    assert len(w.children) == 2
    assert w.pool.active == w.pool.resident == 0
