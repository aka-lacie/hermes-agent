"""Regression coverage: the compaction summary role must alternate against
TEMPLATE-VISIBLE neighbours, not literal list neighbours.

Mistral-family chat templates (Devstral, Mistral Small 3.x, Magistral)
enforce user/assistant alternation at render time but exempt the tool flow
from the check: ``tool`` results and assistant messages carrying
``tool_calls`` are skipped. The summary-role selection previously keyed off
the LITERAL last head message (``compressed[-1]``), producing this captured
failure (Hermes Desktop v0.19.0 against llama.cpp-served Devstral):

    [0] system
    [1] user            <- original first user turn, protected
    [2] assistant       (tool_calls, exempt)
    [3] tool            (exempt)
    [4] user            <- COMPACTION SUMMARY, pinned to "user" because the
                           literal previous role was "tool"
    [5..] assistant(tool_calls)/tool pairs

The template counts only [1] and [4]: user -> user, and the backend rejects
the ENTIRE request with a Jinja alternation error (HTTP 500):

    "After the optional system message, conversation roles must alternate
     user and assistant roles except for tool calls and results"

Because the summary persists in the stored conversation, every retry
replays the same poisoned history: the session is permanently broken, on
EVERY compaction, overflow or not.

These tests pin the fix. Since Phase 2e (Rule 1) the summary is always a role="user"
system_event row, never an assistant row; what adapts is its PLACEMENT: neighbour roles are
computed by ``_template_visible_role`` (skipping the exempt tool flow), and the row is put where
the API copy's consecutive-user merge yields a sequence that satisfies the Mistral alternation
check (``_wire_view`` replays that merge). The #52160 / #58753 forced-user shapes (summary-user
followed only by exempt messages) stay alternation-safe.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from agent.context_compressor import (
    COMPRESSED_SUMMARY_METADATA_KEY,
    SUMMARY_PREFIX,
    _template_visible_role,
)
import itertools


@pytest.fixture()
def compressor():
    from agent.context_compressor import ContextCompressor

    with patch(
        "agent.context_compressor.get_model_context_length",
        return_value=100_000,
    ):
        c = ContextCompressor(
            model="test/model",
            threshold_percent=0.50,
            protect_first_n=3,
            protect_last_n=20,
            quiet_mode=True,
        )
        c.tail_token_budget = 40
        return c


def _tool_turns(start: int, n: int, payload: str = "x" * 300) -> list[dict]:
    out: list[dict] = []
    for i in range(start, start + n):
        out.append(
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": f"c{i}",
                        "function": {"name": "terminal", "arguments": "{}"},
                    }
                ],
            }
        )
        out.append({"role": "tool", "content": payload, "tool_call_id": f"c{i}"})
    return out


def _summary_rows(messages: list[dict]) -> list[dict]:
    return [
        m
        for m in messages
        if isinstance(m, dict) and m.get(COMPRESSED_SUMMARY_METADATA_KEY)
    ]


def _wire_view(messages: list[dict]) -> list[dict]:
    """The API copy's consecutive-user merge (roles only matter here)."""
    out: list[dict] = []
    for m in messages:
        if out and m.get("role") == "user" and out[-1].get("role") == "user":
            out[-1] = {**out[-1], "content": f"{out[-1].get('content')}\n\n{m.get('content')}"}
            continue
        out.append(dict(m))
    return out


def _mistral_alternation_ok(messages: list[dict]) -> bool:
    """Replay the Mistral template's pre-flight alternation check: count
    only user and assistant-without-tool_calls messages; the counted
    sequence must go user, assistant, user, assistant, ..."""
    expect = "user"
    for m in messages:
        role = _template_visible_role(m)
        if role in (None, "system"):
            continue
        if role != expect:
            return False
        expect = "assistant" if expect == "user" else "user"
    return True


class TestTemplateVisibleRoleHelper:
    def test_tool_flow_is_exempt(self):
        assert _template_visible_role({"role": "tool", "content": "r"}) is None
        assert (
            _template_visible_role(
                {"role": "assistant", "content": "", "tool_calls": [{"id": "c"}]}
            )
            is None
        )

    def test_counted_roles_pass_through(self):
        assert _template_visible_role({"role": "user", "content": "q"}) == "user"
        assert (
            _template_visible_role({"role": "assistant", "content": "a"})
            == "assistant"
        )
        assert _template_visible_role({"role": "system", "content": "s"}) == "system"

    def test_non_dict_is_exempt(self):
        assert _template_visible_role(None) is None
        assert _template_visible_role("not a message") is None


class TestSummaryRoleAlternatesAgainstVisibleNeighbours:
    def test_captured_devstral_shape_places_the_user_summary_beside_the_visible_user(self, compressor):
        """The byte-captured poisoning shape: protected head ends
        ``[user, assistant(tool_calls), tool]``, tail is all tool flow.
        The template-visible previous role is ``user``, so a user summary at the end of the head
        would read user -> user; it goes directly after that visible user row instead, where the
        API copy merges the two into one turn."""
        c = compressor
        messages = [{"role": "user", "content": "run a full systems diagnostic"}]
        messages += _tool_turns(0, 30)

        mocked = f"{SUMMARY_PREFIX}\nrolled-up summary of the tool work"
        with patch.object(c, "_generate_summary", return_value=mocked):
            out = c.compress(messages, current_tokens=90_000)

        rows = _summary_rows(out)
        assert len(rows) == 1
        assert rows[0].get("role") == "user" and rows[0].get("sender_kind") == "system"  # Rule 1
        assert out.index(rows[0]) == 1, "summary must sit directly after the visible user row"
        assert _mistral_alternation_ok(_wire_view(out)), (
            "REGRESSION: compaction summary separated from the template-visible user turn only by "
            "exempt tool flow. Mistral-strict templates reject the whole request with a Jinja "
            "alternation 500 and the stored session is poisoned permanently.")

    def test_captured_shape_passes_mistral_alternation(self, compressor):
        c = compressor
        messages = [{"role": "user", "content": "run a full systems diagnostic"}]
        messages += _tool_turns(0, 30)

        mocked = f"{SUMMARY_PREFIX}\nrolled-up summary of the tool work"
        with patch.object(c, "_generate_summary", return_value=mocked):
            out = c.compress(messages, current_tokens=90_000)

        wire = _wire_view(out)
        assert _mistral_alternation_ok(wire), (
            "Compressed transcript fails the Mistral template alternation "
            "check. Visible-role sequence: "
            f"{[_template_visible_role(m) for m in wire if _template_visible_role(m)]}"
        )

    def test_visible_head_assistant_visible_tail_user_merges(self, compressor):
        """When the visible head ends ``assistant`` and the visible tail
        opens ``user`` after exempt tool flow, a user summary at the end of
        the head would read user(summary) -> user(tail) to the template.
        It goes directly before the tail's first visible user row instead,
        so the API copy merges it into that turn."""
        c = compressor
        messages = [
            {"role": "user", "content": "question " + "q" * 200},
            {"role": "assistant", "content": "answer " + "a" * 200},
        ]
        messages += _tool_turns(0, 30)
        # A recent user turn preserved in the tail keeps the zero-user
        # guard out of the picture.
        messages += [
            {"role": "user", "content": "latest question"},
            {"role": "assistant", "content": "on it"},
        ]

        mocked = f"{SUMMARY_PREFIX}\nsummary body"
        with patch.object(c, "_generate_summary", return_value=mocked):
            out = c.compress(messages, current_tokens=90_000)

        rows = _summary_rows(out)
        assert len(rows) == 1
        assert rows[0]["role"] == "user"
        nxt = out[out.index(rows[0]) + 1]
        assert nxt == {"role": "user", "content": "latest question", **{k: v for k, v in nxt.items() if k.startswith("_")}}
        assert _mistral_alternation_ok(_wire_view(out))


class TestForcedUserGuardsStillWin:
    def test_zero_user_guard_still_forces_user(self, compressor):
        """#58753: when no genuine user turn survives, the summary must
        still be pinned to role=user (and that shape is alternation-safe
        because everything after it is template-exempt)."""
        c = compressor
        c.compression_count = 1  # protect_first_n decays -> no head
        messages = [{"role": "user", "content": "work kanban task 42"}]
        messages += _tool_turns(0, 12)

        mocked = f"{SUMMARY_PREFIX}\nsummary body"
        with patch.object(c, "_generate_summary", return_value=mocked):
            out = c.compress(messages, current_tokens=90_000)

        rows = _summary_rows(out)
        assert len(rows) == 1
        assert rows[0].get("role") == "user"
        assert _mistral_alternation_ok(_wire_view(out))

    def test_only_the_summary_sits_beside_another_user_row(self, compressor):
        """Literal consecutive user rows appear only where the summary row joins a user turn
        (the API copy merges them); the wire view has none."""
        c = compressor
        messages = [{"role": "user", "content": "run diagnostics"}]
        messages += _tool_turns(0, 30)

        mocked = f"{SUMMARY_PREFIX}\nsummary body"
        with patch.object(c, "_generate_summary", return_value=mocked):
            out = c.compress(messages, current_tokens=90_000)

        for prev, cur in itertools.pairwise(out):
            if prev.get("role") == "user" and cur.get("role") == "user":
                assert prev.get(COMPRESSED_SUMMARY_METADATA_KEY) or cur.get(COMPRESSED_SUMMARY_METADATA_KEY)
        for prev, cur in itertools.pairwise(_wire_view(out)):
            assert not (prev.get("role") == "user" and cur.get("role") == "user")
