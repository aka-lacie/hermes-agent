"""Rule 1 write gate: a new ``role="assistant"`` row holds only this session's model output.

The append paths admit an assistant row only with an origin from ``ASSISTANT_ORIGINS``. Under pytest an
unattributed write from production code raises; rows written directly by test code are fixtures and pass.
In production the write is logged and stored as a ``role="user"`` system_event row instead.
"""
import logging

import pytest

import hermes_state_provenance as prov
from agent import message_parts as mp
from hermes_state import SessionDB


def _production_writer(name="writer"):
    """A writer whose frames belong to a non-test module, as a gateway or tool caller's would."""
    namespace = {"__name__": "prod_writer"}
    exec(compile(
        "def single(db, **kw):\n    return db.append_message('s', 'assistant', 'not the model', **kw)\n"
        "def batch(db, rows):\n    return db.append_messages_batch('s', rows)\n",
        f"/opt/prod/{name}.py", "exec"), namespace)
    return namespace["single"], namespace["batch"]


@pytest.fixture
def db(tmp_path):
    db = SessionDB(db_path=tmp_path / "state.db")
    db.create_session("s", source="cli")
    yield db
    db.close()


def _rows(db):
    return [tuple(r) for r in db._conn.execute(
        "SELECT role, content, sender_kind, part_kind FROM messages WHERE session_id = 's' ORDER BY id")]


def test_strict_mode_refuses_unattributed_production_writes(db):
    single, batch = _production_writer()
    with pytest.raises(prov.AssistantOriginError):
        single(db)
    with pytest.raises(prov.AssistantOriginError):
        single(db, origin="mirror")
    with pytest.raises(prov.AssistantOriginError):
        batch(db, [{"role": "assistant", "content": "not the model"}])
    assert _rows(db) == []


def test_named_origins_pass(db):
    single, batch = _production_writer()
    single(db, origin=mp.MODEL_ORIGIN)
    batch(db, [{"role": "assistant", "content": "copied", mp.ASSISTANT_ORIGIN: mp.COPY_ORIGIN},
               {"role": "assistant", "content": "notice", mp.ASSISTANT_ORIGIN: mp.HARNESS_LEGACY_ORIGIN}])
    assert [r[0] for r in _rows(db)] == ["assistant"] * 3


def test_test_code_fixture_rows_pass(db):
    db.append_message("s", "assistant", "fixture reply")
    db.append_messages_batch("s", [{"role": "assistant", "content": "fixture batch"}])
    assert [r[:2] for r in _rows(db)] == [("assistant", "fixture reply"), ("assistant", "fixture batch")]


def test_production_mode_reroutes_to_a_system_event(db, monkeypatch, caplog):
    monkeypatch.setattr(prov, "ASSISTANT_GATE_STRICT", False)
    single, batch = _production_writer()
    with caplog.at_level(logging.WARNING, logger="hermes_state"):
        single(db, reasoning="secret thoughts")
        batch(db, [{"role": "assistant", "content": "batched notice", "finish_reason": "stop"}])
    assert _rows(db) == [("user", "not the model", "system", prov.UNATTRIBUTED_ASSISTANT_KIND),
                         ("user", "batched notice", "system", prov.UNATTRIBUTED_ASSISTANT_KIND)]
    first, second = db.get_messages_as_conversation("s")
    assert first["_parts"][0]["type"] == second["_parts"][0]["type"] == "system_event"
    assert first["sender_id"] == prov.UNATTRIBUTED_ASSISTANT_SENDER
    assert tuple(db._conn.execute("SELECT reasoning, finish_reason FROM messages").fetchone()) == (None, None)
    assert "rerouted to a system_event" in caplog.text


def test_production_mode_never_orphans_tool_results(db, monkeypatch, caplog):
    monkeypatch.setattr(prov, "ASSISTANT_GATE_STRICT", False)
    _, batch = _production_writer()
    calls = [{"id": "c1", "type": "function", "function": {"name": "t", "arguments": "{}"}}]
    with caplog.at_level(logging.ERROR, logger="hermes_state"):
        batch(db, [{"role": "assistant", "content": "", "tool_calls": calls}])
    assert [r[0] for r in _rows(db)] == ["assistant"]
    assert "kept as assistant" in caplog.text


def test_row_rewrites_are_not_gated(db):
    db.append_messages_batch("s", [{"role": "assistant", "content": "draft", mp.ASSISTANT_ORIGIN: mp.MODEL_ORIGIN}])
    [live] = db.get_messages_as_conversation("s", repair_alternation=True)
    live["content"] = "final"
    _, batch = _production_writer()
    batch(db, [live])  # a row-addressed rewrite of an admitted row, not a new assistant row
    assert _rows(db) == [("assistant", "final", None, None)]
