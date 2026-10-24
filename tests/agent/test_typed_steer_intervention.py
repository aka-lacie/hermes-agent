"""Typed gateway input must retain human intervention provenance for turn results."""
import threading

import pytest

from agent.steering import begin_input_turn, finish_input_turn
from run_agent import AIAgent


@pytest.mark.parametrize("kind,open_turn,intervened", [
    ("user", True, True), ("task", True, False), ("user", False, False),
])
def test_only_accepted_human_input_marks_intervention(kind, open_turn, intervened):
    agent = object.__new__(AIAgent)
    agent._pending_steer_lock = threading.Lock()
    agent._turn_user_intervened = False
    begin_input_turn(agent)
    if not open_turn:
        finish_input_turn(agent)

    assert agent.steer_input_events([{"kind": kind, "text": "Follow-up context"}]) is open_turn
    assert agent._turn_user_intervened is intervened


@pytest.mark.parametrize("recover_after_error", [False, True])
def test_typed_turn_handoff_preserves_ticketed_queue_ownership(recover_after_error):
    agent = object.__new__(AIAgent)
    agent._pending_steer_lock = threading.Lock()
    agent._pending_steer = None
    begin_input_turn(agent)
    assert agent.steer("queued prompt", ticket="admission-1")
    assert agent.steer("unclaimed note")
    assert agent.steer_input_events([{"kind": "task", "text": "child result"}])
    if recover_after_error:
        finish_input_turn(agent)  # finalizer closes admission; memory sync may then raise
        assert agent.steer("late prompt", ticket="admission-2") is False
    result = {}
    finish_input_turn(agent, result)
    assert result["pending_steer"] == "unclaimed note"
    assert result["pending_input_events"] == [{"kind": "task", "text": "child result"}]
    assert agent.withdraw_steer("admission-1") is True
    assert agent._pending_steer is None
    again = {}
    finish_input_turn(agent, again)
    assert again == {}
