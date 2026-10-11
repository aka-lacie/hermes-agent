"""Durable conversation controls layered on the existing delegation runner."""

from __future__ import annotations

import json
import time
from datetime import UTC, datetime
from pathlib import Path

from tools.registry import tool_error


def enabled() -> bool:
    from tools.delegate_tool_config import _load_config
    return _load_config().get("resumable_sessions") is True


def _store(parent):
    from tools.delegate_session_store import DelegateSessionStore
    db = getattr(parent, "_session_db", None)
    if db is None:
        raise ValueError("Resumable delegation requires a persisted parent session.")
    return DelegateSessionStore(db)


def bind_child(parent, child, task, resume=None, *, routing_cfg=None):
    """Reserve a persisted conversation before dispatch, including queued children."""
    if not enabled():
        return
    from tools.delegate_session_store import DelegateSessionStore
    if resume is None:
        child._ensure_db_session()
    else:
        child._session_db_created = True
    db = child._session_db
    store = DelegateSessionStore(db)
    if resume is None:
        from tools.delegate_session_routes import capture_route
        record = store.register(
            parent.session_id, child.session_id,
            model=child.model, provider=getattr(child, "requested_provider", None) or child.provider,
            assignment=task["goal"], context=task.get("context") or "", title=task.get("title"),
            role=child._delegate_role, depth=child._delegate_depth,
            workspace=child._delegate_workspace, forked_context=child._delegate_forked_context,
            route_descriptor=capture_route(parent, child, task, routing_cfg=routing_cfg),
        )
        record, token = store.acquire(parent.session_id, record["session_id"], task["goal"])
    else:
        record, token = resume["record"], resume["token"]
        db.reopen_session(child.session_id)
        child._delegate_resume_history = db.get_resume_conversations(child.session_id)[0]
        resume["transferred"] = True
    child._delegate_resumable = True
    from agent.prompt_builder import clear_skills_system_prompt_cache
    clear_skills_system_prompt_cache(clear_snapshot=True)
    child._delegate_session_id = record["session_id"]
    child._delegate_session_token = token
    child._delegate_store_path = str(Path(db.db_path).resolve())
    child._delegate_assignment_at = record["last_activity_at"]
    progress = child.tool_progress_callback
    last_touch = time.monotonic()

    def touch_progress(*args, **kwargs):
        nonlocal last_touch
        now = time.monotonic()
        if now - last_touch >= 1:
            store.touch(record["session_id"], token)
            last_touch = now
        if progress:
            return progress(*args, **kwargs)

    if hasattr(progress, "_flush"):
        touch_progress._flush = progress._flush
    child.tool_progress_callback = touch_progress


def release_child(child):
    """Release only after the worker and resource cleanup finish; fence old callbacks."""
    token = vars(child).pop("_delegate_session_token", None)
    if not token:
        return
    from hermes_state_registry import acquire, release
    from tools.delegate_session_store import DelegateSessionStore
    db = acquire(Path(child._delegate_store_path))
    try:
        DelegateSessionStore(db).release(
            child._delegate_session_id, token, current_session_id=child.session_id,
        )
    finally:
        release(db)


def assignment_message(child, message):
    """Only temporal context is added; the parent owns the assignment's instructions."""
    if getattr(child, "_delegate_resumable", False) is not True:
        return message
    stamp = datetime.now(UTC).isoformat(timespec="seconds")
    prefix = f"[Parent assignment at {stamp}]\n"
    if isinstance(message, str):
        return prefix + message
    return [{"type": "text", "text": prefix}, *message]


def control(action, parent, *, session_id=None, message=None, model=None, query=None, limit=20, background=False):
    from tools.delegate_session_store import DelegateSessionError
    try:
        store = _store(parent)
        if action == "list":
            records = store.list(parent.session_id, query=query, limit=limit)
            return json.dumps({"action": action, "count": len(records), "subagents": [_roster_entry(r) for r in records]})
        if not isinstance(session_id, str) or not session_id.strip():
            raise ValueError(f"action='{action}' requires session_id from spawn or list.")
        session_id = session_id.strip()
        record = store.get(parent.session_id, session_id)
        if action == "observe":
            observed = store.observe(parent.session_id, session_id, limit=limit)
            observed["session"] = _roster_entry(observed["session"])
            return json.dumps(observed, ensure_ascii=False)
        if action == "close":
            record = store.close(parent.session_id, session_id)
            _cancel_review(parent, session_id)
            return json.dumps({"action": action, **_roster_entry(record)})
        if action == "resume":
            return _resume(parent, store, record, message, model, background)
        return _live_control(action, parent, record, message)
    except (ValueError, DelegateSessionError) as exc:
        return tool_error(str(exc))


def _roster_entry(record):
    entry = {key: record[key] for key in ("session_id", "title", "assignment", "status", "created_at",
                                         "last_activity_at", "model", "provider")}
    entry["title"] = entry["title"][:120]
    if len(entry["assignment"]) > 500:
        entry["assignment"] = entry["assignment"][:500] + "…"
        entry["assignment_truncated"] = True
    return entry


def _cancel_review(parent, session_id):
    from agent.delegated_review import cancel_delegated_review
    cancel_delegated_review(session_id, db_path=parent._session_db.db_path)


def _resume(parent, store, record, message, model, background):
    from tools.delegate_tool import (
        delegate_task, is_spawn_paused, _resolve_delegation_credentials,
    )
    from tools.delegate_session_routes import resume_route
    if not isinstance(message, str) or not message.strip():
        raise ValueError("action='resume' requires a non-empty message describing the next assignment.")
    if is_spawn_paused():
        raise ValueError("Delegation spawning is paused; resume starts another run and is paused too.")
    task = {"goal": message, "context": record["context"], "fork_context": False,
            "model": model if model is not None else record["model"]}
    # An invalid route must not replace the conversation's saved model.
    route = resume_route(parent, record, model)
    _resolve_delegation_credentials(route, parent)
    record, token = store.acquire(parent.session_id, record["session_id"], message, model=model)
    reservation = {"record": record, "token": token, "transferred": False}
    try:
        _cancel_review(parent, record["session_id"])
        return delegate_task(tasks=[task], parent_agent=parent, background=background,
                             credentials_cfg=route, _resume=reservation)
    finally:
        if not reservation["transferred"]:
            store.release(record["session_id"], token)


def _live_control(action, parent, record, message):
    from tools.delegate_tool_registry import _active_subagents, _active_subagents_lock, _handle_control_action
    profile_db = str(Path(parent._session_db.db_path).resolve())
    with _active_subagents_lock:
        live_id = next((sid for sid, live in _active_subagents.items()
                        if getattr(live.get("agent"), "_delegate_session_id", None) == record["session_id"]
                        and getattr(live.get("agent"), "_delegate_store_path", None) == profile_db), None)
    if live_id is None:
        raise ValueError("This child is not accepting live control. Use resume for an idle child.")
    result = json.loads(_handle_control_action(action, live_id, message, parent))
    result.pop("subagent_id", None)
    result["session_id"] = record["session_id"]
    return json.dumps(result)


def extend_schema(parameters):
    """Expose the session surface only in profiles that enable retained children."""
    if not enabled():
        return parameters
    from copy import deepcopy
    parameters = deepcopy(parameters)
    props = parameters["properties"]
    props.pop("subagent_id", None)
    props["action"] = {
        "type": "string", "enum": ["spawn", "resume", "list", "observe", "steer", "stop", "close"],
        "description": "Spawn a new child; resume an idle conversation; list recent running/idle children "
        "(query searches all retained children); observe history; steer/stop a running child; close an idle "
        "child permanently. Execution returns through the normal delegation completion path.",
    }
    props["session_id"] = {"type": "string", "description": "Stable child session ID from spawn, completion, or list."}
    props["message"] = {"type": "string", "description": "Next assignment for resume, or course correction for steer. "
                         "Supply relevant changes since the child's previous assignment."}
    props["model"] = {"type": "string", "description": "Optional resume model override within the saved provider."}
    props["query"] = {"type": "string", "description": "Search all your retained children, including older/closed sessions."}
    props["limit"] = {"type": "integer", "minimum": 1, "maximum": 100, "description": "Maximum list entries or observed messages (default 20)."}
    props["tasks"]["items"]["properties"]["title"] = {
        "type": "string", "description": "Optional short stable title. Defaults to a shortened initial assignment.",
    }
    return parameters
