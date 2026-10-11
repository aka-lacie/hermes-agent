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
