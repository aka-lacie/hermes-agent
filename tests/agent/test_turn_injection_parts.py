"""pre_llm_call results become turn injection parts on the canonical user dict (Phase 2c step 2).

A typed turn carries its per-turn context (memory recall, ``pre_llm_call`` results, gateway notes) as
``injection`` parts appended to the canonical current user dict, so the turn-start flush persists them
in ``parts_json`` and a replay renders the same bytes. ``content`` stays what the human wrote.
"""

from __future__ import annotations

import threading
from types import SimpleNamespace
from unittest.mock import patch

from agent import message_parts as mp
from agent.turn_context import (
    _append_turn_injections, _collect_pre_llm_call_context, build_api_messages,
)


def _agent(**kw):
    return SimpleNamespace(**{"session_id": "s1", "model": "m", "platform": "discord", "_persist_disabled": False,
                              "_session_persist_lock": threading.Lock(), "_session_db": None, **kw})


def test_hook_results_are_collected_as_kinded_injections():
    results = [{"context": "route hint", "kind": "jev_system1"}, {"context": "legacy"}, "bare",
               {"action": "block"}, {"context": ""}]
    injections: list = []
    with patch("hermes_cli.lifecycle.invoke_hook", return_value=results):
        joined = _collect_pre_llm_call_context(
            _agent(), effective_task_id="t", turn_id="u", original_user_message="hi", messages=[],
            conversation_history=None, injections=injections)
    assert injections == [("jev_system1", "route hint"), ("plugin_context", "legacy"), ("plugin_context", "bare")]
    assert joined == "route hint\n\nlegacy\n\nbare"  # the untyped sidecar text is unchanged


class _DB:
    def __init__(self):
        self.calls = []

    def set_message_parts(self, session_id, row_id, content, parts):
        self.calls.append((session_id, row_id, content, parts))
        return 1


def test_injections_ride_the_canonical_dict_and_reach_an_early_row():
    msg = {"role": "user", "_row_id": 7}
    mp.set_parts(msg, [mp.human_message("what changed?", origin={"platform": "discord", "message_id": "9"})])
    db = _DB()
    _append_turn_injections(_agent(_session_db=db), msg, "recalled fact",
                            [("jev_system1", "route hint"), ("turn_note", "voice channel now: x")])
    assert [(p["type"], p.get("kind")) for p in msg["_parts"]] == [
        ("human", None), ("injection", "memory_context"), ("injection", "jev_system1"), ("injection", "turn_note")]
    assert "recalled fact" in msg["_parts"][1]["text"]
    assert msg["content"] == "what changed?"  # injections never enter the plain view
    assert "api_content" not in msg
    assert db.calls and db.calls[0][1] == 7 and db.calls[0][3] == msg["_parts"]
