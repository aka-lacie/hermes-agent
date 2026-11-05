"""Spec: a typed row renders to the same wire bytes live and when replayed from state.db (§4.8 Layer 2).

Each part type is produced by its live code path (gateway typed turn input, the steer drain, a
notification turn, a turn-start steer, pre_llm_call injections, an image inside the replay window),
sent once as the current turn, persisted to a real SessionDB, reloaded, run through the gateway's
replay builder (with ``inject_timestamps`` on, the path that used to rewrite replayed steers) and sent
again as history. Every live request must be a byte-identical prefix of the replayed request: the
conversation is append-only on the wire, so the prompt cache holds (forensics 2026-11-04 §1-2).
"""

from __future__ import annotations

import logging
import time
from datetime import datetime, timezone, UTC
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from agent import message_parts as mp
from agent.turn_context import _PER_TURN_RESET_STATE, _append_turn_injections, _stage_turn_user_message
from gateway.config import Platform
from gateway.session import SessionSource
from gateway.typed_input import turn_typed_input
from hermes_state import SessionDB

_SHA = "c" * 64
_IMAGE = f"/native-inputs/{_SHA}/photo.png"


@pytest.fixture(params=[False, True], ids=["timestamps-off", "timestamps-on"])
def timestamp_mode(request):
    return request.param


@pytest.fixture
def agent(tmp_path, monkeypatch, timestamp_mode):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    from run_agent import AIAgent

    agent = AIAgent(api_key="k", base_url="https://api.groq.com/openai/v1", provider="custom", model="m",
                    quiet_mode=True, skip_context_files=True, skip_memory=True)
    for name, value in _PER_TURN_RESET_STATE:
        setattr(agent, name, value)
    agent._cached_system_prompt = "SYS"
    agent._current_turn_timestamp = time.time()
    agent._current_turn_id, agent._current_task_id, agent._current_api_request_id = "u", "t", "r"
    agent._disable_streaming = True
    agent._pending_cli_user_message = None
    agent._model_supports_vision = lambda: True  # images go out natively on this route
    monkeypatch.setattr("hermes_cli.config.load_config_readonly",
                        lambda: {"gateway": {"message_timestamps": {"enabled": timestamp_mode}}})
    return agent


@pytest.fixture(autouse=True)
def image_window():
    # The model takes images natively and the retained file verifies: the image replays as bytes.
    with patch("agent.image_replay._turn_replay_window", return_value=3), \
            patch("agent.image_replay._retained_data_url",
                  side_effect=lambda p: "data:image/png;base64,QUJD" if p == _IMAGE else None):
        yield


def _wire(agent, messages, idx):
    from agent.turn_api_request import build_request_payload
    from agent.turn_request_assembly import assemble_api_request

    assembled = assemble_api_request(
        agent, messages=messages, current_turn_user_idx=idx, _ext_prefetch_cache=None, _plugin_user_context=None,
        moa_config=None, active_system_prompt="SYS", original_user_message=None, pending_moa_prepared_request=None,
        request_logger=logging.getLogger("test"))
    built = build_request_payload(
        agent, api_messages=assembled.api_messages, _moa_prepared_request=None, tools_for_api=None,
        system_message=None, api_call_count=1, api_request_id="r", effective_task_id="t", turn_id="u")
    return built.api_kwargs["messages"]


def _discord_event(text, message_id, *, internal=False, metadata=None):
    source = SessionSource(platform=Platform.DISCORD, chat_id="42", chat_type="dm", user_id="268934270939037698")
    return SimpleNamespace(text=text, message_id=message_id, source=source, internal=internal,
                           metadata=metadata or {}, timestamp=datetime(2026, 11, 4, 23, 35, tzinfo=UTC)), source


def _gateway_turn(agent, text, message_id, *, sent_at, internal=False, display_kind=None, metadata=None):
    """The canonical user dict a gateway turn builds (typed input -> the turn prologue's staging)."""
    event, source = _discord_event(text, message_id, internal=internal, metadata=metadata)
    typed = turn_typed_input(event, source, text, sent_at, display_kind)
    assert typed is not None
    msg, _ = _stage_turn_user_message(agent, text, text, sent_at, message_id, display_kind, None, typed_input=typed)
    assert mp.has_parts(msg)
    return msg


def _assistant(text, **extra):
    return {"role": "assistant", "content": text, mp.ASSISTANT_ORIGIN: mp.MODEL_ORIGIN, **extra}


def _live_transcript(agent):
    """(messages, [(index of the row that was current, label)]) built by the live code paths."""
    from agent.agent_runtime_helpers import apply_pending_steer_to_tool_results

    messages, live_turns = [], []

    def turn(msg, label):
        messages.append(msg)
        live_turns.append((len(messages) - 1, label))

    # turn + injection: a Discord message whose pre_llm_call / recall context rides as injection parts
    human = _gateway_turn(agent, "what changed in the wire?", "1567683734684041401", sent_at=1762299300.0)
    _append_turn_injections(agent, human, "Freya prefers terse replies.",
                            [("jev_system1", "route: analysis"), ("post_compaction_reminder", "check the summary")])
    turn(human, "turn+injection")
    messages.append(_assistant("", tool_calls=[{"id": "call_1", "type": "function",
                                                "function": {"name": "terminal", "arguments": "{\"cmd\": \"ls\"}"}}]))
    messages.append({"role": "tool", "tool_call_id": "call_1", "content": "a.py\nb.py"})
    # steer: sent mid-turn through AIAgent.steer(origin=...), delivered after the tool batch
    agent.steer("spawn title is a feature of my plugin or core from upstream?",
                origin={"platform": "discord", "message_id": "1567683734684041408", "channel_id": "42",
                        "sent_at": 1762299375.0})
    apply_pending_steer_to_tool_results(agent, messages, 1)
    live_turns.append((len(messages) - 1, "steer"))
    messages.append(_assistant("It is core."))
    # system_event: a self-injected background-process completion turn
    turn(_gateway_turn(agent, "Command: make test\nOutput:\nok", None, sent_at=1762299400.0, internal=True,
                       display_kind="process_complete"), "system_event")
    messages.append(_assistant("Tests pass."))
    # turn-start out-of-band: a steer that missed the last tool batch, delivered as the next turn
    turn(_gateway_turn(agent, "also check the replay path", "1567683734684041420", sent_at=1762299500.0,
                       metadata={"delivery": "steer"}), "turn-start steer")
    messages.append(_assistant("Checked."))
    # image inside the replay window
    image = {"role": "user"}
    mp.set_parts(image, [mp.human_message("what is in this photo?", origin={"platform": "discord", "message_id": "9"}),
                         mp.media_ref(_SHA, "image/png", _IMAGE)])
    image.update(sender_kind="human", sender_id="discord:268934270939037698")
    turn(image, "image")
    messages.append(_assistant("A cat."))
    return messages, live_turns


def _replayed(messages, tmp_path, timestamp_mode):
    from gateway.run import _build_gateway_agent_history

    db = SessionDB(db_path=tmp_path / "state.db")
    try:
        db.create_session("s", source="discord")
        db.append_messages_batch("s", [dict(m) for m in messages])
        history = db.get_messages_as_conversation("s")
    finally:
        db.close()
    replay, _ = _build_gateway_agent_history(history, inject_timestamps=timestamp_mode)
    return replay


def test_every_part_type_renders_the_same_live_and_from_state_db(agent, tmp_path, timestamp_mode):
    messages, live_turns = _live_transcript(agent)
    live_requests = {label: _wire(agent, messages[: idx + 1], idx) for idx, label in live_turns}
    # The rendered shape of each live current row (the bytes under test), spot-checked.
    origin_end = ' sent_at="' if timestamp_mode else '/>'
    assert live_requests["turn+injection"][-1]["content"].startswith(
        '<origin platform="discord" message_id="1567683734684041401" channel_id="42"' + origin_end)
    assert '<system-reminder kind="jev_system1">\nroute: analysis\n</system-reminder>' in \
        live_requests["turn+injection"][-1]["content"]
    steer_start = '<steer sent_at="' if timestamp_mode else '<steer message_id="'
    assert live_requests["steer"][-1]["content"].startswith(steer_start)
    assert live_requests["system_event"][-1]["content"].startswith('<system-reminder kind="process_complete">')
    assert live_requests["turn-start steer"][-1]["content"].startswith(steer_start)
    assert live_requests["image"][-1]["content"][-1] == {"type": "image_url",
                                                        "image_url": {"url": "data:image/png;base64,QUJD"}}

    replay = _replayed(messages, tmp_path, timestamp_mode)
    replay.append({"role": "user", "content": "next question"})
    replayed_request = _wire(agent, replay, len(replay) - 1)
    for idx, label in live_turns:
        live = live_requests[label]
        assert replayed_request[: len(live)] == live, label  # byte-identical, append-only


def test_a_replayed_typed_steer_keeps_its_shape(agent, tmp_path, timestamp_mode):
    """Forensics §1-2: the gateway's timestamp head used to turn a replayed steer into a different shape."""
    messages, live_turns = _live_transcript(agent)
    steer_idx = next(idx for idx, label in live_turns if label == "steer")
    replay = _replayed(messages, tmp_path, timestamp_mode)
    replayed_steer = next(m for m in replay if m.get("intent") == "steer" and m.get("role") == "user"
                          and m["_parts"][0]["origin"].get("message_id") == "1567683734684041408")
    assert replayed_steer["content"] == messages[steer_idx]["content"]  # no "[Wed …] " head
    assert replayed_steer["_parts"] == messages[steer_idx]["_parts"]
    live = _wire(agent, messages[: steer_idx + 1], steer_idx)[-1]
    rendered = _wire(agent, replay[: steer_idx + 1], steer_idx)[-1]
    assert rendered == live


def test_the_live_rows_persist_their_parts(agent, tmp_path):
    messages, _ = _live_transcript(agent)
    db = SessionDB(db_path=tmp_path / "state.db")
    try:
        db.create_session("s", source="discord")
        db.append_messages_batch("s", [dict(m) for m in messages])
        rows = db.get_messages_as_conversation("s")
    finally:
        db.close()
    typed = [m for m in messages if mp.has_parts(m)]
    stored = [m for m in rows if mp.has_parts(m)]
    assert [m["_parts"] for m in stored] == [m["_parts"] for m in typed]
    assert [m.get("sender_kind") for m in stored] == ["human", "human", "system", "human", "human"]
    assert stored[0]["content"] == "what changed in the wire?"
    assert [p["kind"] for p in stored[0]["_parts"] if p["type"] == mp.INJECTION] == [
        "memory_context", "jev_system1", "post_compaction_reminder"]
    assert "route: analysis" not in stored[0]["content"] and "check the summary" not in stored[0]["content"]
    rendered = _wire(agent, [stored[0]], 0)[-1]["content"]
    assert "route: analysis" in rendered and "check the summary" in rendered
