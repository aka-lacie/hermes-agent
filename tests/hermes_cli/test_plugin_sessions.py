"""``ctx.sessions`` (unified-session plan §4.6) and the per-manager service registry on the plugin context."""

import json

import pytest

from hermes_cli.plugins import PluginContext, PluginManager, PluginManifest


def _ctx(manager=None, name="sessplug"):
    return PluginContext(PluginManifest(name=name, source="user", key=name), manager or PluginManager())


@pytest.fixture
def profile_db(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    from hermes_state import SessionDB
    from tools.delegate_session_store import DelegateSessionStore
    db = SessionDB(db_path=tmp_path / "state.db")
    db.create_session("parent", "cli")
    store = DelegateSessionStore(db)
    for sid, parent in (("a", "parent"), ("b", "parent"), ("a-sub", "a")):
        db.create_session(sid, "subagent", parent_session_id=parent, model_config={"_delegate_from": parent})
        store.register(parent, sid, model="m", provider="p", assignment=f"Assignment of {sid}", title=sid.upper())
    db.append_message("a-sub", "assistant", "x" * 5000)
    yield tmp_path / "state.db", db
    db.close()


def test_ctx_sessions_lists_observes_admits_and_resolves_for_the_calling_session(profile_db):
    path, _ = profile_db
    from hermes_cli.plugins_sessions import PluginSessions
    sessions = PluginSessions(db_path=path)
    assert isinstance(_ctx().sessions, PluginSessions)
    assert sorted(r["session_id"] for r in sessions.list(caller_session_id="parent")) == ["a", "b"]
    assert [r["session_id"] for r in sessions.list("siblings", caller_session_id="a")] == ["b"]
    assert {r["session_id"] for r in sessions.list("tree", caller_session_id="parent")} == {"a", "b", "a-sub"}
    observed = sessions.observe("a-sub", caller_session_id="parent")  # a grandchild, read-only
    assert len(observed["messages"][-1]["text"]) <= 300 and observed["messages"][-1]["chars"] == 5000
    receipt = sessions.admit("b", caller_session_id="a", text="ping", intent="steer")
    assert receipt["delivered"] and receipt["peer_hop"] == 1
    with pytest.raises(sessions.SessionAdmissionError) as refused:
        sessions.admit("a-sub", caller_session_id="b", text="not my sibling")
    assert refused.value.to_dict()["error"] == "unavailable"
    with pytest.raises(ValueError):
        sessions.list(caller_session_id="")
    assert sessions.resolve("session:b") == "b"
    assert sessions.PEER_HOP_MAX == 8
    json.dumps([observed, receipt])  # plain JSON, no row objects


def test_ctx_sessions_acl_is_pluggable_and_restored_on_unload(profile_db):
    path, _ = profile_db
    from agent import session_directory as directory
    from hermes_cli.plugins_sessions import PluginSessions
    manager = PluginManager()
    ctx = _ctx(manager)
    sessions = PluginSessions(ctx, db_path=path)
    default = directory.get_session_acl("peer")
    sessions.set_acl(lambda db, conn, caller, row: row["id"] == "a-sub", action="peer")
    try:
        assert sessions.admit("a-sub", caller_session_id="b", text="widened")["delivered"]
        manager.unload("sessplug")
        assert directory.get_session_acl("peer") is default
    finally:
        directory.set_session_acl(None, action="peer")


def test_service_registry_is_per_manager_unique_and_removed_on_unload():
    manager, other = PluginManager(), PluginManager()
    ctx = _ctx(manager)
    service = object()
    ctx.register_service("peer-directory", service)
    assert _ctx(manager, "otherplug").get_service("peer-directory") is service
    assert _ctx(other).get_service("peer-directory") is None
    with pytest.raises(ValueError):
        _ctx(manager, "otherplug").register_service("peer-directory", object())
    manager.unload("sessplug")
    assert ctx.get_service("peer-directory") is None
