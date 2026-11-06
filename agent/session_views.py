"""Bounded, low-noise views of sessions for ``list`` and ``observe`` (plan §4.5, §4.6).

Freya: "ensure it does not get too noisy like the json output from earlier". Every view here is built from
fixed per-field caps, never from a raw dump of a row, its parts or its metadata:

- :func:`message_view`: one transcript message as ``role``, ``kind``, a text preview (<= ``TEXT_CAP``),
  tool calls as ``name(args preview)`` (<= ``CALL_CAP`` each) and tool results as a <= ``RESULT_CAP``
  preview with the original length in ``chars``.
- :func:`session_brief`: the observed session's own header (no route, delivery target or context).
- :func:`list_row`: one flat ``list`` row; grandchildren only as the integers ``running_children`` /
  ``children_total``.
- :func:`children_rollup`: the observed session's delegate children: at most ``CHILDREN_MAX``, running
  first then newest activity, each ``session_id, title, model, status, last_activity_at, latest``; the
  serialized roll-up stays under ``CHILDREN_BUDGET`` bytes (``children_total`` says how many exist when it
  was cut).
"""

from __future__ import annotations

import json
from typing import Any, Callable, Iterable, Mapping, Optional

OBSERVE_DEFAULT = 6
OBSERVE_MAX = 20
TEXT_CAP = 300
CALL_CAP = 120
RESULT_CAP = 160
CALLS_PER_MESSAGE = 3
FROM_CAP = 64
_TARGET_ARGS = ("path", "file_path", "command", "url", "query", "session_id", "action", "name")

LIST_TITLE_CAP = 48
LIST_ASSIGNMENT_CAP = 64
MODEL_CAP = 40

CHILDREN_MAX = 8
CHILD_TITLE_CAP = 32
CHILD_MODEL_CAP = 24
CHILD_LATEST_CAP = 120
CHILD_LATEST_MIN = 32  # a shorter latest-step preview says nothing: show fewer children instead
CHILDREN_BUDGET = 1480  # serialized bytes of the whole roll-up (the contract is < 1.5 KB)


def observe_limit(limit: Any) -> int:
    try:
        value = int(limit) if limit is not None else OBSERVE_DEFAULT
    except (TypeError, ValueError):
        value = OBSERVE_DEFAULT
    return max(1, min(value, OBSERVE_MAX))


def one_line(text: Any) -> str:
    return " ".join(str(text or "").split())


def clip(text: Any, cap: int) -> str:
    """*text* on one line, at most *cap* characters: head and tail kept around an ellipsis."""
    flat = one_line(text)
    if len(flat) <= cap:
        return flat
    if cap <= 8:
        return flat[:max(0, cap - 1)] + "…"
    head = (cap - 1) * 2 // 3
    tail = cap - 1 - head
    return flat[:head] + "…" + (flat[-tail:] if tail else "")


def clip_head(text: Any, cap: int) -> str:
    flat = one_line(text)
    return flat if len(flat) <= cap else flat[:max(0, cap - 1)] + "…"


def stamp(value: Any) -> Optional[int]:
    try:
        return int(float(value)) if value is not None else None
    except (TypeError, ValueError):
        return None


def _text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(part.get("text", "") for part in content
                         if isinstance(part, dict) and isinstance(part.get("text"), str))
    return "" if content is None else str(content)


def call_summary(call: Any, cap: int = CALL_CAP) -> Optional[str]:
    """``name(args preview)`` of one tool call, at most *cap* characters (arguments compacted, never whole)."""
    function = call.get("function") if isinstance(call, Mapping) else None
    if not isinstance(function, Mapping) or not isinstance(function.get("name"), str):
        return None
    name = clip_head(function["name"], 48)
    arguments = function.get("arguments")
    if isinstance(arguments, str):
        try:
            arguments = json.loads(arguments)
        except (ValueError, RecursionError):
            pass
    if isinstance(arguments, dict):
        # What the call acts on first (path, command, url, ...), so a short preview names its target.
        arguments = {**{k: arguments[k] for k in _TARGET_ARGS if k in arguments},
                     **{k: v for k, v in arguments.items() if k not in _TARGET_ARGS}}
    if isinstance(arguments, (dict, list)):
        try:
            arguments = json.dumps(arguments, ensure_ascii=False, separators=(",", ":"))
        except (TypeError, ValueError):
            arguments = str(arguments)
    room = cap - len(name) - 2
    args = clip_head(arguments or "", room) if room > 0 else ""
    return f"{name}({args})"


def _parts(message: Mapping[str, Any]) -> list:
    parts = message.get("_parts")
    if parts is None and isinstance(message.get("parts_json"), str):
        try:
            parts = json.loads(message["parts_json"])
        except (TypeError, ValueError):
            parts = None
    return parts if isinstance(parts, list) else []


def message_kind(message: Mapping[str, Any]) -> str:
    role = message.get("role")
    if role == "tool":
        return "tool_result"
    if role == "assistant":
        return "tool_call" if message.get("tool_calls") else "text"
    if message.get("part_kind"):
        return str(message["part_kind"])
    for part in _parts(message):
        if isinstance(part, Mapping) and part.get("type") == "system_event" and part.get("kind"):
            return str(part["kind"])
        if isinstance(part, Mapping) and part.get("type") == "human":
            return "steer" if part.get("delivery") == "steer" else "human"
    return "text"


def message_view(message: Mapping[str, Any]) -> dict:
    """One transcript message, compact: never reasoning, never whole arguments, parts or metadata."""
    role = str(message.get("role") or "")
    out: dict[str, Any] = {"role": role, "kind": message_kind(message)}
    content = _text(message.get("content"))
    if role == "tool":
        if message.get("tool_name"):
            out["tool"] = clip_head(message["tool_name"], 48)
        out["text"] = clip(content, RESULT_CAP)
        if len(content) > RESULT_CAP:
            out["chars"] = len(content)
    else:
        if content.strip():
            out["text"] = clip(content, TEXT_CAP)
            if len(one_line(content)) > TEXT_CAP:
                out["chars"] = len(content)
        calls = message.get("tool_calls") if role == "assistant" else None
        if isinstance(calls, list) and calls:
            summaries = [s for s in (call_summary(c) for c in calls[:CALLS_PER_MESSAGE]) if s]
            if summaries:
                out["calls"] = summaries
            if len(calls) > CALLS_PER_MESSAGE:
                out["more_calls"] = len(calls) - CALLS_PER_MESSAGE
    sender_kind = message.get("sender_kind")
    if role == "user" and sender_kind and sender_kind != "human" and message.get("sender_id"):
        out["from"] = clip_head(message["sender_id"], FROM_CAP)
    at = stamp(message.get("timestamp"))
    if at is not None:
        out["at"] = at
    return out


def latest_step(message: Optional[Mapping[str, Any]], cap: int = CHILD_LATEST_CAP) -> Optional[str]:
    """One line saying what a session did last: its latest tool call, tool result or text."""
    if not message:
        return None
    role = message.get("role")
    calls = message.get("tool_calls") if role == "assistant" else None
    if isinstance(calls, list) and calls:
        summary = call_summary(calls[0], cap)
        if summary:
            return summary
    text = _text(message.get("content"))
    if role == "tool":
        name = clip_head(message.get("tool_name") or "tool", 32)
        return clip_head(f"{name} → {one_line(text)}", cap)
    if not text.strip():
        return None
    return clip_head(f"{message_kind(message)}: {one_line(text)}" if role == "user" else one_line(text), cap)


def session_brief(record: Mapping[str, Any]) -> dict:
    """The observed session's header: identity, status and the latest assignment, nothing else."""
    out = {"session_id": record["session_id"], "title": clip_head(record.get("title"), LIST_TITLE_CAP + 20),
           "status": record.get("status"), "model": clip_head(record.get("model") or "", MODEL_CAP) or None}
    kind = record.get("lineage_kind")
    if kind and kind != "delegate":
        out["badge"] = kind
    if record.get("queued"):
        out["queued"] = record["queued"]
    if record.get("depth") is not None:
        out["depth"] = record["depth"]
    out["last_activity_at"] = stamp(record.get("last_activity_at"))
    assignment = record.get("assignment") or ""
    if assignment and one_line(assignment) != one_line(record.get("title")):
        out["assignment"] = clip(assignment, 200)
    return out


def list_row(record: Mapping[str, Any]) -> dict:
    """One flat ``list`` row (d=1 child or attached opt-in); its own children only as two integers."""
    out = {"session_id": record["session_id"], "title": clip_head(record.get("title"), LIST_TITLE_CAP),
           "status": record.get("status"), "model": clip_head(record.get("model") or "", MODEL_CAP) or None,
           "last_activity_at": stamp(record.get("last_activity_at"))}
    assignment = record.get("assignment") or ""
    if assignment and one_line(assignment) != one_line(record.get("title")):
        out["assignment"] = clip_head(assignment, LIST_ASSIGNMENT_CAP)
    if record.get("badge"):
        out["badge"] = record["badge"]
    if record.get("queued"):
        out["queued"] = record["queued"]
    if record.get("children_total"):
        if record.get("running_children"):
            out["running_children"] = int(record["running_children"])
        out["children_total"] = int(record["children_total"])
    if record.get("depth_below_caller") is not None:
        out["depth_below_caller"] = record["depth_below_caller"]
    return out


def _size(value: Any) -> int:
    return len(json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))


def children_rollup(records: Iterable[Mapping[str, Any]], latest: Callable[[Mapping[str, Any]], Optional[str]],
                    *, inherit_model: Optional[str] = None, max_children: int = CHILDREN_MAX,
                    budget: int = CHILDREN_BUDGET) -> dict:
    """``{"children": [...], ["children_total": n]}``: running first, then most recent activity; per child only
    ``session_id, title, model, status, last_activity_at`` and a one-line ``latest`` step (``model`` omitted
    when it is *inherit_model*, the observed session's own). The serialized roll-up stays within *budget*
    bytes: ``latest`` previews shrink first (never below ``CHILD_LATEST_MIN``), then the least recent rows go
    (``children_total`` still counts them)."""
    ordered = sorted(records, key=lambda r: (r.get("status") != "running", -(float(r.get("last_activity_at") or 0)),
                                             str(r.get("session_id"))))
    total = len(ordered)
    rows = []
    for record in ordered[:max_children]:
        row = {"session_id": record["session_id"], "title": clip_head(record.get("title"), CHILD_TITLE_CAP)}
        model = record.get("model")
        if model and model != inherit_model:
            row["model"] = clip_head(model, CHILD_MODEL_CAP)
        row.update(status=record.get("status"), last_activity_at=stamp(record.get("last_activity_at")))
        step = latest(record)
        if step:
            row["latest"] = clip_head(step, CHILD_LATEST_CAP)
        rows.append(row)

    def assemble(items):
        out: dict[str, Any] = {"children": items}
        if len(items) < total:
            out["children_total"] = total
        return out

    def fitted(items):
        bare = [{k: v for k, v in row.items() if k != "latest"} for row in items]
        if not items or _size(assemble(items)) <= budget:
            return assemble(items)
        with_latest = sum(1 for row in items if row.get("latest"))
        room = min(CHILD_LATEST_CAP, (budget - _size(assemble(bare))) // max(1, with_latest) - len(',"latest":""'))
        while room >= CHILD_LATEST_MIN:  # characters, not bytes: step down until the bytes fit too
            trimmed = [{**row, "latest": clip_head(row["latest"], room)} if row.get("latest") else row
                       for row in items]
            if _size(assemble(trimmed)) <= budget:
                return assemble(trimmed)
            room -= 8
        return None

    while rows:
        result = fitted(rows)
        if result is not None:
            return result
        rows = rows[:-1]
    return assemble([])
