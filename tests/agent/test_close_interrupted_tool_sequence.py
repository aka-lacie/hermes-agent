"""Regression tests for ``close_interrupted_tool_sequence`` (#48879 follow-up).

#48879 closed the tool-call sequence on interrupt inside ``finalize_turn``,
but the retry/backoff/error interrupt aborts in ``conversation_loop`` ``return``
early and never reach it — so they persisted a raw ``tool`` tail. The next user
message then lands as ``... tool → user``, the role-alternation violation that
makes strict providers (Gemini, Claude) hallucinate a continuation and ignore
prior context (what the user perceives as "lost context").

The fix routes every interrupt abort through this one shared helper. Phase 2e (Rule 1): the
closing row is a ``role="user"`` harness notice (one system_event, ``turn_outcome`` set), never a
synthetic assistant row; the wire renderer sends it as a system-reminder after the tool result and
the API copy merges it with the next user turn. These tests pin that contract.
"""

from agent.message_sanitization import close_interrupted_tool_sequence


def _tool_tail():
    return [
        {"role": "user", "content": "edit the file"},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [{"id": "c1", "function": {"name": "patch", "arguments": "{}"}}],
        },
        {"role": "tool", "tool_call_id": "c1", "content": "ok edited"},
    ]


def _assert_tool_tail_closed_by_a_notice(messages):
    """A tool result is followed by the harness notice that closes the turn, never by a bare person row."""
    for i in range(len(messages) - 1):
        if messages[i].get("role") == "tool":
            nxt = messages[i + 1]
            assert nxt.get("role") == "user" and nxt.get("sender_kind") == "system", (
                f"unclosed tool tail at index {i}")
            assert nxt["_parts"][0]["type"] == "system_event" and nxt.get("turn_outcome"), nxt








def test_closing_makes_next_user_message_alternation_safe():
    """The whole point: appending a user turn after the close must not
    produce the ``tool → user`` shape strict providers choke on."""
    messages = _tool_tail()
    close_interrupted_tool_sequence(messages, None)
    assert isinstance(messages[-1]["timestamp"], float)
    assert messages[-1]["display_kind"] == "hidden" and messages[-1]["turn_outcome"] == "partial"
    assert not any(m.get("role") == "assistant" and not m.get("tool_calls") for m in messages)  # Rule 1
    follow_on = messages + [{"role": "user", "content": "they do! increase the timing"}]
    _assert_tool_tail_closed_by_a_notice(follow_on)
    # A caller banner is the turn's only explanation: it stays visible (agent chrome in the UI).
    bannered = _tool_tail()
    assert close_interrupted_tool_sequence(bannered, "Response truncated.") is True
    from agent.message_parts import project_harness_notice_for_display
    assert project_harness_notice_for_display(bannered[-1])["content"] == "Response truncated."
    assert bannered[-1]["content"] == "The turn ended before delivering a response."
    assert bannered[-1]["role"] == "user" and bannered[-1]["display_kind"] == "harness_notice"


def test_assistant_tail_is_left_untouched():
    messages = [
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": "partial reply"},
    ]
    before = [dict(m) for m in messages]
    assert close_interrupted_tool_sequence(messages, "interrupted") is False
    assert messages == before


def test_user_tail_is_left_untouched():
    messages = [{"role": "user", "content": "hi"}]
    assert close_interrupted_tool_sequence(messages, None) is False
    assert len(messages) == 1



def test_only_an_interrupted_close_carries_the_stopped_mark():
    """The closing row is the turn's last stored row: it alone tells a reopened transcript the turn was stopped."""
    stopped = _tool_tail()
    assert close_interrupted_tool_sequence(stopped, None, stopped=True) is True
    assert stopped[-1]["display_metadata"]["interrupted"] is True
    truncated = _tool_tail()
    assert close_interrupted_tool_sequence(truncated, "Response truncated.") is True
    assert truncated[-1]["display_metadata"] == {"harness_notice_text": "Response truncated."}
