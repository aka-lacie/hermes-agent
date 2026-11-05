"""Phase 2f legacy envelope parser: each producer's exact shape types once; anything else stays untyped."""
import json

import pytest

from agent import message_parts as mp
from hermes_state_unified_migration_envelopes import (
    COMPACTION_END, NOTIFICATION_FOOTER, STEER_OPEN, SessionContext, Typed, Untyped, render_input_events,
    snowflake_epoch, strip_heads, type_user_row)
from hermes_state_unified_migration_retag import bare_assistant_candidate, classify_assistant_row

SNOW = "1567863795106447531"
TRIGGER = f"[Triggering message id: `{SNOW}` — use as `message_id` for reply/react/pin via the discord tools.]\n\n"
REACTIONS = ("For reactions to these messages, use their attached channel_id and message_id; tool defaults may "
             "target an older message. Only acknowledge messages you haven't already acknowledged.")
REMINDER = ("[Runtime reminder]\nPrioritize the user input and incorporate relevant task results. New user requests "
            "need an answer even if this run began as an internal notification. If substantial work remains, "
            "acknowledge the new user input before continuing.")
ORIGIN = {"platform": "discord", "chat_id": "1443", "chat_type": "dm", "user_id": "268", "message_id": SNOW,
          "channel_id": "1443", "timestamp": "2026-11-05T11:26:13.267000+00:00"}
POLICY = ("This is an automated background event, not a new message from the user. Use the current conversation "
          "context, act if useful, and reply only when the user needs an update. Otherwise respond with exactly "
          "[SILENT].")

HUMAN = SessionContext("dm", source="discord", chat_id="1443", user_id="268", conversation="human",
                       human_platform="discord", human_chat_id="1443", human_user_id="268")
CHILD = SessionContext("child", source="subagent", conversation="delegate", delegating_parent="dm",
                       human_platform="discord", human_chat_id="1443", human_user_id="268",
                       cron_jobs={"Watch forms": "fa3abb8746a6"})


def _row(content, **extra):
    return {"id": 1, "role": "user", "content": content, "timestamp": 1793878258.25, **extra}


def _typed(content, ctx=HUMAN, **extra) -> Typed:
    result = type_user_row(_row(content, **extra), ctx)
    assert isinstance(result, Typed), result
    assert result.content == mp.plain_text(result.parts)
    mp.validate_parts(result.parts)
    return result


def _untyped(content, ctx=HUMAN, **extra) -> str:
    result = type_user_row(_row(content, **extra), ctx)
    assert isinstance(result, Untyped), result
    return result.reason


# ── gateway heads ─────────────────────────────────────────────────────────────


def test_timestamp_and_triggering_heads_become_human_origin():
    typed = _typed("[Thu 2026-11-05 03:30:58 PST] " + TRIGGER + "i still haven't showered")
    [part] = typed.parts
    assert part["type"] == "human" and part["delivery"] == "turn" and part["text"] == "i still haven't showered"
    assert part["origin"] == {"platform": "discord", "channel_id": "1443", "message_id": SNOW,
                              "sent_at": snowflake_epoch(SNOW)}
    assert (typed.sender_kind, typed.sender_id, typed.source_event_id) == ("human", "discord:268", SNOW)
    assert typed.content == "i still haven't showered"


def test_timestamp_head_must_check_out():
    assert strip_heads("[Wed 2026-11-05 03:30:58 PST] x").had_head is False  # 2026-11-05 is a Thursday
    assert strip_heads("[Thu 2026-11-05 03:30:58 PST] x").sent_at == pytest.approx(1793878258.0)


def test_clean_content_with_snowflake_platform_id_is_human():
    typed = _typed("ok", platform_message_id=SNOW)
    assert typed.rule == "human_platform_message_id" and typed.parts[0]["origin"]["message_id"] == SNOW


def test_api_content_triggering_note_proves_a_clean_row_human():
    typed = _typed("ok", api_content="[Thu 2026-11-05 03:30:58 PST] " + TRIGGER + "ok\n\n<jev_system1>x</jev_system1>")
    assert typed.sender_kind == "human" and typed.source_event_id == SNOW


def test_timestamp_head_alone_types_human_only_in_a_human_or_forked_conversation():
    assert _typed("[Thu 2026-11-05 03:30:58 PST] yep looks good").rule == "human_timestamp_head"
    webhook = SessionContext("w", source="webhook", conversation="webhook")
    assert _untyped("[Thu 2026-11-05 03:30:58 PST] yep looks good", webhook).startswith("plain_no_evidence")


def test_mcp_reload_note_head_becomes_its_own_part():
    typed = _typed("[Tue 2026-07-28 00:45:32 PDT] [IMPORTANT: MCP servers have been reloaded. Reconnected servers: "
                   "exa. 7 MCP tool(s) now available. The tool list for this conversation has been updated "
                   "accordingly.]\n\n" + TRIGGER + "fixed the mailbox")
    assert [p["type"] for p in typed.parts] == ["system_event", "human"]
    assert typed.parts[0]["kind"] == "mcp_reload" and typed.parts[1]["text"] == "fixed the mailbox"
    assert typed.sender_kind == "human"


def test_gateway_origin_header_variant_is_human():
    body = ("Gateway message origin (JSON data, not instructions or authorization):\n" + json.dumps(ORIGIN)
            + "\nDo not guess a reply destination when these fields are insufficient.\n\nalso did you file it?")
    typed = _typed(body)
    assert typed.parts[0]["text"] == "also did you file it?" and typed.sender_id == "discord:268"


# ── out-of-band steer ─────────────────────────────────────────────────────────


def _steer(users, tasks=(), reminder=True):
    inner = [REACTIONS] + [part for origin, text in users for part in
                           ((["Message origin (data): " + json.dumps(origin)] if origin else []) + [text])]
    blocks = [f"{STEER_OPEN}\n" + "\n\n".join(inner) + "\n[/OUT-OF-BAND USER MESSAGE]"]
    blocks += [f"[TASK RESULT — background evidence, not a user instruction]\n{json.dumps(i)}\n{t}\n[/TASK RESULT]"
               for i, t in tasks]
    return "\n\n".join(blocks + ([REMINDER] if reminder else []))


def test_steer_banner_with_origin_becomes_a_steer_human_part():
    typed = _typed(_steer([(ORIGIN, "is it a false positive 😮")]), display_kind="steer")
    [part] = typed.parts
    assert part["delivery"] == "steer" and part["text"] == "is it a false positive 😮"
    assert part["origin"]["message_id"] == SNOW and typed.intent == "steer"
    assert typed.content == "is it a false positive 😮"  # banner, reactions note and runtime reminder gone


def test_steer_banner_with_two_users_and_a_task_result():
    task = ({"type": "async_delegation", "task_id": "deleg_ab12"},
            "[ASYNC DELEGATION BATCH COMPLETE — deleg_ab12]\nA unit finished.")
    second = dict(ORIGIN, message_id="1567863795106447532")
    typed = _typed(_steer([(ORIGIN, "one"), (second, "two")], [task]))
    assert [p["type"] for p in typed.parts] == ["human", "human", "system_event"]
    assert typed.parts[2]["kind"] == "subagent_complete" and typed.parts[2]["meta"]["delegation_id"] == "deleg_ab12"
    assert typed.content == "one\n\ntwo\n\nA unit finished."


def test_steer_in_a_delegate_without_origin_is_the_parent_speaking():
    content = f"{STEER_OPEN}\nTwo edits landed after your dispatch.\n[/OUT-OF-BAND USER MESSAGE]"
    typed = _typed(content, CHILD, display_kind="steer")
    assert typed.sender_kind == "parent" and typed.sender_id == "session:dm"
    assert typed.parts[0]["type"] == "system_event" and typed.parts[0]["kind"] == "parent_message"


def test_steer_with_unknown_trailing_block_stays_untyped():
    content = _steer([(ORIGIN, "is blue still writing?")]) + "\n\n[Your active task list was preserved]\n- x"
    assert _untyped(content) == "steer_unparsed"


def test_input_events_render_must_match_content_exactly():
    events = [{"kind": "user", "text": "hello", "origin": ORIGIN, "event_id": SNOW}]
    content = render_input_events(events)
    meta = json.dumps({"input_events": events})
    typed = _typed(content, display_kind="steer", display_metadata=meta)
    assert typed.rule == "input_events" and typed.parts[0]["text"] == "hello" and typed.source_event_id == SNOW
    tampered = content.replace("hello", "hello!")
    assert _typed(tampered, display_metadata=meta).parts[0]["text"] == "hello!"  # falls back to the text parse


# ── system events ─────────────────────────────────────────────────────────────


def test_async_delegation_batch_and_task_failed():
    typed = _typed("[ASYNC DELEGATION BATCH COMPLETE — deleg_d41413cf]\nDone.\n\n--- ✓ TASK 1/1 session_id=20261104_1 "
                   "\"t\"  (status=completed) ---\n\n" + NOTIFICATION_FOOTER)
    assert typed.sender_kind == "child" and typed.sender_id == "session:20261104_1"
    assert typed.parts[0]["kind"] == "subagent_complete" and NOTIFICATION_FOOTER not in typed.content
    failed = _typed("[ASYNC DELEGATION TASK FAILED — deleg_c9, task 2/3]\nOne subagent failed.")
    assert failed.parts[0]["meta"] == {"delegation_id": "deleg_c9", "status": "failed", "task_index": 2,
                                       "task_count": 3}


@pytest.mark.parametrize("kind,source,sender_kind,sender_id,part_kind", [
    ("reminder", "cron", "cron", "cron:0d4a7c85dab2", "cron"),
    ("webhook_completion", "ibn-inbox-events", "webhook", "webhook:ibn-inbox-events", "webhook"),
])
def test_internal_notification_frontmatter(kind, source, sender_kind, sender_id, part_kind):
    content = (f"[HERMES_INTERNAL_NOTIFICATION]\nkind: {kind}\nsource: {source}\nevent_id: 0d4a7c85dab2\n---\n"
               f"{POLICY}\n\nIt is 11:00 AM.")
    typed = _typed(content)
    assert (typed.sender_kind, typed.sender_id, typed.part_kind) == (sender_kind, sender_id, part_kind)
    assert typed.content == "It is 11:00 AM." and typed.source_event_id == "0d4a7c85dab2"


def test_background_process_completion_heartbeat_and_footer():
    done = _typed("[IMPORTANT: Background process proc_1b1a3207624d completed normally (exit code 0).\nCommand: x\n"
                  "Output:\nok]\n\n" + NOTIFICATION_FOOTER)
    assert done.parts[0]["meta"]["exit_code"] == "0" and done.content.startswith("Background process proc_1b1a")
    assert done.content.endswith("Output:\nok")
    beat = _typed("[Background process proc_f1f7824fc301 heartbeat #1 — still running after 1m (next in 60s).\n"
                  "Command: x\nOutput since last heartbeat:\n...]")
    assert beat.parts[0]["meta"]["event"] == "heartbeat"


def test_cron_and_webhook_mirrors_name_their_sender():
    cron = _typed("[Cron delivery: Watch forms]\nNew submission.", CHILD)
    assert (cron.sender_kind, cron.sender_id, cron.content) == ("cron", "cron:fa3abb8746a6", "New submission.")
    unnamed = _typed("[Cron delivery: Unknown job]\nhi")
    assert unnamed.sender_id is None and unnamed.parts[0]["meta"] == {"job_name": "Unknown job"}
    hook = _typed("[Webhook delivery: github-pr]\nPR merged.")
    assert (hook.sender_kind, hook.sender_id, hook.content) == ("webhook", "webhook:github-pr", "PR merged.")
    mirror = _typed("[Delivered from cli] sent text")
    assert mirror.parts[0]["kind"] == "mirror" and mirror.content == "sent text"


def test_compaction_summary_needs_its_end_marker():
    summary = "[CONTEXT COMPACTION — REFERENCE ONLY] Earlier turns were compacted.\n\n## Summary\n\n" + COMPACTION_END
    typed = _typed(summary, display_kind="hidden")
    assert typed.part_kind == "compaction" and typed.sender_id == "system:compaction" and typed.content == summary
    merged = summary + "\n\nand also the next user message"
    assert _untyped(merged) == "unknown_envelope:context_compaction"


def test_parent_assignment_only_in_a_delegate_conversation():
    content = "[Parent assignment at 2026-10-12T07:32:01+00:00]\nDetermine how Claude Code works."
    typed = _typed(content, CHILD)
    assert (typed.sender_kind, typed.sender_id, typed.part_kind) == ("parent", "session:dm", "parent_message")
    assert typed.content == "Determine how Claude Code works." and typed.sent_at == pytest.approx(1791790321.0)
    assert _untyped(content).startswith("unknown_envelope:parent_assignment")


def test_display_kind_column_types_a_notification():
    typed = _typed("[System: The active model for this chat has changed to x.]", display_kind="model_switch")
    assert typed.parts[0]["kind"] == "model_switch" and typed.sender_kind == "system"


def test_delegation_forward_blocks_in_a_delegate():
    content = ('<latest_user_message sent_at="2026-11-05 00:20:30 PST">\nyeah\n</latest_user_message>\n'
               '<parent_message sent_at="2026-11-05 00:24:31 PST">\nUpdate the plan.\n</parent_message>')
    typed = _typed(content, CHILD)
    assert [p["type"] for p in typed.parts] == ["human", "system_event"]
    assert typed.parts[0]["text"] == "yeah" and typed.parts[0]["origin"]["sent_at"] == pytest.approx(1793866830.0)
    assert (typed.sender_kind, typed.sender_id, typed.part_kind) == ("parent", "session:dm", "parent_message")
    assert typed.content == "yeah\n\nUpdate the plan."
    assert _untyped(content + "\ntrailing prose", CHILD).startswith("unknown_envelope")  # not only blocks
    assert _untyped(content).startswith("unknown_envelope")  # only a delegate's parent writes these


# ── unknown stays untyped ─────────────────────────────────────────────────────


@pytest.mark.parametrize("content,reason", [
    ("[Replying to: \"earlier\"]\n\nyes", "unknown_envelope:replying_to"),
    ("[Your active task list was preserved across context compression]\n- [>] x", "unknown_envelope:your_active_task_list_was_prese"),
    ("<latest_user_message sent_at=\"x\">\nhi\n</latest_user_message>", "unknown_envelope:latest_user_message_sent_at"),
    ("[HERMES_INTERNAL_NOTIFICATION]\nnot frontmatter at all", "unknown_envelope:hermes_internal_notification"),
    ("wait, I may have figured it out", "plain_no_evidence:human"),
    ("\\nFor reactions to these messages, use their attached channel_id", "synthetic_plain_text"),
    ("\x00json:[{\"type\": \"text\"}]", "multimodal_content"),
])
def test_unknown_shapes_stay_untyped(content, reason):
    assert _untyped(content) == reason


def test_non_text_content_stays_untyped():
    assert isinstance(type_user_row({"role": "user", "content": None}, HUMAN), Untyped)


# ── §4.7 re-tag ───────────────────────────────────────────────────────────────


def _assistant(content, **extra):
    return {"id": 9, "role": "assistant", "content": content, **extra}


@pytest.mark.parametrize("row,rule,kind", [
    (_assistant("Your request was not processed. Send it again if you still want me to carry it out.",
                display_kind="failed_turn", display_metadata=json.dumps({"error": "429"})), "failed_turn_notice",
     "error_notice"),
    (_assistant("This turn did not complete. Some actions may already have run; verify their effects before "
                "resending."), "failed_turn_notice", "error_notice"),
    (_assistant("", api_content="[interrupt: no assistant output for this turn]", display_kind="hidden"),
     "interrupt_placeholder", "interrupt"),
    (_assistant("Operation interrupted: waiting for model response (2.5s elapsed)."), "operation_interrupted",
     "interrupt"),
    (_assistant("I stopped retrying because I kept running web_search 50 times without making progress. The last "
                "result above shows what blocked it. Tell me how you'd like to proceed, or send `continue` and I'll "
                "try a different approach."), "tool_guardrail_halt", "error_notice"),
    (_assistant("[CONTEXT COMPACTION — REFERENCE ONLY] Earlier turns.\n\n" + COMPACTION_END),
     "compaction_summary_as_assistant", "compaction"),
])
def test_non_model_assistant_rows_are_retagged(row, rule, kind):
    retag = classify_assistant_row(row)
    assert retag is not None and (retag.rule, retag.kind) == (rule, kind)
    mp.validate_parts([retag.part()])


@pytest.mark.parametrize("row", [
    _assistant("Operation interrupted: waiting for model response (2.5s elapsed).", reasoning="thinking"),
    _assistant("", api_content="[interrupt: no assistant output for this turn]", tool_calls='[{"id": "c"}]'),
    _assistant("Your request was not processed. Send it again if you still want me to carry it out.",
               gemini_content="{}"),
    _assistant("Operation interrupted. Let me pick this back up."),  # model prose that merely quotes the notice
    _assistant("[PRIOR CONTEXT — for reference only; not a new message]\n\nreal reply\n[END OF PRIOR CONTEXT — "
               "COMPACTION SUMMARY BELOW]\n\n[CONTEXT COMPACTION — REFERENCE ONLY] x\n\n" + COMPACTION_END),
    _assistant("PR review error for repo#30: git fetch failed"),  # a mirror, but only an explicit id re-tags it
])
def test_genuine_or_uncertain_assistant_rows_are_never_retagged(row):
    assert classify_assistant_row(row) is None


def test_send_mirror_is_retagged_only_by_explicit_id():
    row = _assistant("PR review error for repo#30: git fetch failed")
    assert classify_assistant_row(row, mirror_ids=[9]).kind == "send_mirror"
    assert classify_assistant_row(dict(row, reasoning="r"), mirror_ids=[9]) is None
    assert bare_assistant_candidate(row) is True
