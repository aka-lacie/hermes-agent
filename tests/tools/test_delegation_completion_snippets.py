"""Dev2 completion identity parity: identify a child, never replay its assignment."""
from types import SimpleNamespace

from tools.process_registry_notifications import format_process_notification
from tests.tools.test_async_delegation import _make_async_evt


def _header(text):
    return next(line for line in text.splitlines() if line.startswith("--- "))


def test_batch_header_names_session_and_title_without_echoing_the_assignment():
    brief = "Freya asked:\n\"long quoted message\"\n\n" + "Step one. " * 200
    evt = _make_async_evt(is_batch=True, goals=[brief], results=[{
        "task_index": 0, "status": "completed", "summary": "done", "api_calls": 3,
        "session_id": "20261104_134225_d7c56c", "title": "Build Session & Wire Inspector"}])
    header = _header(format_process_notification(evt))
    assert header.startswith('--- ✓ TASK 1/1 session_id=20261104_134225_d7c56c "Build Session & Wire Inspector"  (status=completed')
    assert "Step one" not in header and "\n" not in header


def test_batch_header_snippets_untitled_or_resumed_assignments():
    brief = "Fix   the\nheader " + "x" * 300
    untitled = _header(format_process_notification(_make_async_evt(is_batch=True, goals=[brief], results=[{
        "task_index": 0, "status": "completed", "summary": "ok", "session_id": "s1"}])))
    assert ": Fix the header x" in untitled
    snippet = untitled.split(": ", 1)[1].split("  (status=")[0]
    assert len(snippet) <= 120 and snippet.endswith("…")
    resumed = _header(format_process_notification(_make_async_evt(is_batch=True, goals=["round two: post the fix"], results=[{
        "task_index": 0, "status": "completed", "summary": "ok", "session_id": "s1", "title": "Reviewer", "resumed": True}])))
    assert resumed.startswith('--- ✓ TASK 1/1 session_id=s1 "Reviewer": round two: post the fix  (status=completed')


def test_missed_steer_note_is_a_snippet_but_the_entry_keeps_the_full_steer():
    from tools.delegate_tool_child_run import _append_missed_steer
    steer = "please also\n" + "do this " * 100
    entry = {"error": "stopped"}
    _append_missed_steer(entry, steer)
    assert entry["missed_steer"] == steer
    note = entry["error"].split("stopped: ", 1)[1]
    assert len(note) <= 121 and "\n" not in note and note.endswith("…]")


def test_success_missed_steer_is_bounded_and_retains_full_entry():
    from tools.delegate_tool_child_run import _build_result_entry, _SchemaOutcome
    steer = "please also\n" + "do this " * 100
    child = SimpleNamespace(model="fixture", max_iterations=5)
    entry = _build_result_entry(child, {"completed": True, "final_response": "done", "pending_steer": steer}, 0, 1.0, _SchemaOutcome(None, None, [], 0))
    assert entry["missed_steer"] == steer
    assert steer not in entry["summary"] and len(entry["summary"]) < 250


def test_result_metadata_carries_title_and_explicit_resumed_flag():
    from tools.delegate_tool_results import _child_session_metadata
    child = SimpleNamespace(_delegate_session_id="s1", _approval_title="Review", _delegate_resumed=True)
    assert _child_session_metadata(child) == {"session_id": "s1", "title": "Review", "resumed": True}
    child._delegate_resumed = False
    assert _child_session_metadata(child) == {"session_id": "s1", "title": "Review"}


def test_task_failure_bounds_goal_and_keeps_session_identity():
    from tools.process_registry_notifications import _format_task_failure_notice
    goal = "private long assignment\n" + "x" * 1000
    text = _format_task_failure_notice({"goals": [goal], "n_tasks": 1, "results": [
        {"task_index": 0, "session_id": "child-full-id", "error": "failed"}]}, "deleg1")
    assert "session_id=child-full-id" in text and goal not in text
    assert "…" in text
