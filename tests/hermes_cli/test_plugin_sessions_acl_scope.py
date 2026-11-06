"""Session ACL registrations are per profile database and generation-leased (independent review R6/R7).

R6: a plugin widening the peer ACL in profile A must not change profile B (distinct databases in one
process). R7: plugin1 installs allow-all, plugin2 deny-all; unloading them in either order must never
bring back a policy whose plugin is gone, and a targeted reload installs a fresh generation.
"""

import pytest

from hermes_cli.plugins import PluginContext, PluginManager, PluginManifest


def _ctx(manager, name):
    return PluginContext(PluginManifest(name=name, source="user", key=name), manager)


def _profile(path):
    from hermes_state import SessionDB
    from tools.delegate_session_store import DelegateSessionStore
    path.parent.mkdir(parents=True, exist_ok=True)
    db = SessionDB(db_path=path)
    db.create_session("parent", "cli")
    db.create_session("other", "cli")
    store = DelegateSessionStore(db)
    for sid, parent in (("a", "parent"), ("b", "parent"), ("x", "other")):
        db.create_session(sid, "subagent", parent_session_id=parent, model_config={"_delegate_from": parent})
        store.register(parent, sid, model="m", provider="p", assignment=f"Assignment of {sid}", title=sid.upper())
    return db


@pytest.fixture
def profiles(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "A"))
    a, b = _profile(tmp_path / "A" / "state.db"), _profile(tmp_path / "B" / "state.db")
    yield tmp_path / "A" / "state.db", tmp_path / "B" / "state.db"
    a.close()
    b.close()


def _reaches(sessions, caller, target) -> bool:
    """A peer admission from *caller* to *target* is delivered (True) or refused as unreachable (False)."""
    try:
        return bool(sessions.admit(target, caller_session_id=caller, text="ping")["delivered"])
    except sessions.SessionAdmissionError as refused:
        assert refused.code == "unavailable", refused.to_dict()
        return False


def _allow_all(db, conn, caller, row):
    return True


def _deny_all(db, conn, caller, row):
    return False


def test_a_profiles_acl_widening_never_reaches_another_profile(profiles):
    from hermes_cli.plugins_sessions import PluginSessions
    path_a, path_b = profiles
    manager = PluginManager()
    in_a = PluginSessions(_ctx(manager, "widener"), db_path=path_a)
    in_b = PluginSessions(db_path=path_b)  # an explicit facade on profile B, same process
    assert not _reaches(in_a, "a", "x") and not _reaches(in_b, "a", "x")  # cross-parent: refused by default
    in_a.set_acl(_allow_all, action="peer")
    assert _reaches(in_a, "a", "x")       # A widened
    assert not _reaches(in_b, "a", "x")   # B untouched
    assert _reaches(in_b, "a", "b")       # B's own default still admits siblings
    manager.unload("widener")
    assert not _reaches(in_a, "a", "x")   # A restored
    assert not _reaches(in_b, "a", "x")


def test_bound_home_scope_is_the_profile_the_plugin_loaded_in(profiles, monkeypatch):
    import hermes_state
    from hermes_cli.plugins_sessions import PluginSessions
    path_a, path_b = profiles
    monkeypatch.setattr(hermes_state, "DEFAULT_DB_PATH", path_a)  # this process's profile is A
    manager = PluginManager()
    bound = PluginSessions(_ctx(manager, "bound"))  # no db_path: the current profile's state.db
    bound.set_acl(_allow_all, action="peer")
    try:
        assert _reaches(bound, "a", "x")
        assert _reaches(PluginSessions(db_path=path_a), "a", "x")
        assert not _reaches(PluginSessions(db_path=path_b), "a", "x")
    finally:
        manager.unload("bound")
    assert not _reaches(PluginSessions(db_path=path_a), "a", "x")


@pytest.mark.parametrize("order", [("p1", "p2"), ("p2", "p1")])
def test_non_lifo_unload_never_resurrects_a_retired_policy(profiles, order):
    from hermes_cli.plugins_sessions import PluginSessions
    path_a, _ = profiles
    manager = PluginManager()
    facade = PluginSessions(db_path=path_a)
    PluginSessions(_ctx(manager, "p1"), db_path=path_a).set_acl(_allow_all, action="peer")
    PluginSessions(_ctx(manager, "p2"), db_path=path_a).set_acl(_deny_all, action="peer")
    assert not _reaches(facade, "a", "b")  # newest generation (p2, deny-all) wins
    first, second = order
    manager.unload(first)
    if first == "p1":
        assert not _reaches(facade, "a", "b")  # p2 still loaded: deny-all
        assert not _reaches(facade, "a", "x")
    else:
        assert _reaches(facade, "a", "x")      # p1 still loaded: allow-all
    manager.unload(second)
    # both gone: the default (siblings only), never the retired allow-all
    assert _reaches(facade, "a", "b")
    assert not _reaches(facade, "a", "x")
    # a targeted reload of p1 installs a fresh generation; unloading it again restores the default
    PluginSessions(_ctx(manager, "p1"), db_path=path_a).set_acl(_allow_all, action="peer")
    assert _reaches(facade, "a", "x")
    manager.unload("p1")
    assert not _reaches(facade, "a", "x") and _reaches(facade, "a", "b")


def test_observe_acl_is_scoped_and_leased_too(profiles):
    from hermes_cli.plugins_sessions import PluginSessions
    from tools.delegate_session_store import DelegateSessionError
    path_a, path_b = profiles
    manager = PluginManager()
    in_a, in_b = PluginSessions(_ctx(manager, "observer"), db_path=path_a), PluginSessions(db_path=path_b)
    with pytest.raises(DelegateSessionError):
        in_a.observe("x", caller_session_id="parent")
    in_a.set_acl(_allow_all)
    assert in_a.observe("x", caller_session_id="parent")["session"]["session_id"] == "x"
    with pytest.raises(DelegateSessionError):
        in_b.observe("x", caller_session_id="parent")
    manager.unload("observer")
    with pytest.raises(DelegateSessionError):
        in_a.observe("x", caller_session_id="parent")
