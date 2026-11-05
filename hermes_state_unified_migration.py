"""Phase 2f offline migration: unified-session provenance for an existing ``state.db``.

One-shot, run once at the dev3 cutover against a quiesced (or copied) database, then deleted with its
two siblings. It is the only code allowed to read legacy bracket envelopes back out of text
(``hermes_state_unified_migration_envelopes``); the CI envelope guard allowlists these modules.

What it does (dry-run by default; ``--apply`` writes, in one transaction):

* **Sessions (every row):** ``root_session_id``, ``depth`` and ``lineage_kind`` from
  ``parent_session_id``, the parent's ``end_reason`` and the ``model_config`` markers bound to the parent
  (``_delegate_from`` / ``_branched_from`` / ``_reset_from``; a marker naming another row is an inherited
  copy, exactly as ``_non_continuation_child_sql`` reads it); ``group_id`` from the async delegation that
  spawned the child; ``role`` / ``status`` / ``workspace`` / ``fork_origin`` from ``delegate_sessions``;
  ``status=closed`` for ended rows; a credential-free ``route`` from ``delegate_sessions`` and
  ``model_config``. Only NULL columns are filled; a stored value that disagrees is counted, never changed.
* **Messages (sessions in scope):** user rows get ``sender_kind`` / ``sender_id`` / ``sent_at`` /
  ``source_event_id`` / ``intent`` / ``part_kind`` and ``parts_json``, with ``content`` set to the
  parts' plain view (envelopes parsed once, here); tool rows get ``sender_kind="tool_result"``;
  §4.7 non-model assistant rows are re-tagged as ``role="user"`` system events. Anything not typed
  with certainty is left untouched and counted under its reason.

Idempotent: typed rows (``sender_kind`` or ``parts_json`` set) and re-tagged rows are skipped, so a
second run changes nothing. ``delivery_target`` is not written (Phase 3 owns its semantics).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import random
import sqlite3
import sys
import time
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Mapping, Optional

from agent.message_parts import encode_parts_json, plain_text
from hermes_state_unified_migration_envelopes import SessionContext, Typed, Untyped, type_user_row
from hermes_state_unified_migration_retag import bare_assistant_candidate, classify_assistant_row

logger = logging.getLogger(__name__)

MARKERS = {"_delegate_from": "delegate", "_branched_from": "branch", "_reset_from": "reset"}
HUMAN_SOURCES = frozenset({"discord", "telegram", "slack", "whatsapp", "signal", "matrix", "mattermost", "cli",
                           "tui", "desktop", "gui", "acp", "local", "sms", "email", "homeassistant", "wechat"})
SESSION_FILL_COLUMNS = ("root_session_id", "depth", "lineage_kind", "group_id", "role", "status", "workspace",
                        "route", "fork_origin")
MESSAGE_COLUMNS = ("id", "session_id", "role", "content", "timestamp", "platform_message_id", "display_kind",
                   "display_metadata", "api_content", "finish_reason", "tool_calls", "reasoning", "reasoning_content",
                   "reasoning_details", "codex_reasoning_items", "codex_message_items", "gemini_content",
                   "provider_data", "token_count", "active", "compacted", "sender_kind", "parts_json")
_NEW_SESSION_COLUMNS = SESSION_FILL_COLUMNS
_NEW_MESSAGE_COLUMNS = ("sender_kind", "sender_id", "sent_at", "source_event_id", "intent", "part_kind",
                        "parts_json", "turn_outcome")


# ── schema access ─────────────────────────────────────────────────────────────


def _columns(conn: sqlite3.Connection, table: str) -> set:
    return {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}


def _select_list(available: set, wanted: Iterable[str]) -> str:
    return ", ".join(col if col in available else f"NULL AS {col}" for col in wanted)


def _json_obj(raw: Any) -> dict:
    if not isinstance(raw, str) or not raw:
        return {}
    try:
        value = json.loads(raw)
    except ValueError:
        return {}
    return value if isinstance(value, dict) else {}


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


# ── sessions: lineage and topology ────────────────────────────────────────────


@dataclass
class SessionRow:
    data: dict
    config: dict
    children: list = field(default_factory=list)

    def __getitem__(self, key: str) -> Any:
        return self.data.get(key)


def load_sessions(conn: sqlite3.Connection) -> dict:
    available = _columns(conn, "sessions")
    wanted = ("id", "source", "user_id", "model", "model_config", "parent_session_id", "started_at", "ended_at",
              "end_reason", "session_key", "chat_id", "chat_type", "last_activity_at", "billing_provider",
              "billing_base_url") + _NEW_SESSION_COLUMNS
    cur = conn.execute(f"SELECT {_select_list(available, wanted)} FROM sessions")
    names = [d[0] for d in cur.description]
    sessions = {}
    for values in cur:
        data = dict(zip(names, values))
        sessions[data["id"]] = SessionRow(data, _json_obj(data["model_config"]))
    for sid, row in sessions.items():
        parent = sessions.get(row["parent_session_id"])
        if parent is not None:
            parent.children.append(sid)
    return sessions


def lineage_kind(row: SessionRow, sessions: Mapping[str, SessionRow]) -> Optional[str]:
    """``root | continuation | branch | reset | delegate | webhook | cron`` or None (unclassifiable)."""
    parent_id = row["parent_session_id"]
    if not parent_id:
        return {"webhook": "webhook", "cron": "cron"}.get(row["source"], "root")
    for marker, kind in MARKERS.items():
        if row.config.get(marker) == parent_id:
            return kind
    parent = sessions.get(parent_id)
    if parent is None:
        return None
    if parent["end_reason"] == "compression" and row["source"] != "tool":
        return "continuation"
    if parent["end_reason"] == "branched" and (row["started_at"] or 0) >= (parent["ended_at"] or 0):
        return "branch"
    return None


def _topology(sid: str, sessions: Mapping[str, SessionRow], kinds: Mapping[str, Optional[str]],
              memo: dict) -> tuple[Optional[str], Optional[int]]:
    """``(root_session_id, depth)`` by walking ``parent_session_id`` (iterative, cycle-safe)."""
    chain, cur = [], sid
    while cur not in memo:
        row = sessions.get(cur)
        if row is None or cur in chain:
            for item in chain:
                memo[item] = (None, None)
            return memo.get(sid, (None, None))
        chain.append(cur)
        if not row["parent_session_id"]:
            memo[cur] = (cur, 0)
            break
        cur = row["parent_session_id"]
    for item in reversed(chain):
        if item in memo:
            continue
        root, depth = memo[sessions[item]["parent_session_id"]]
        step = 1 if kinds.get(item) == "delegate" else 0
        memo[item] = (root, None if depth is None or kinds.get(item) is None else depth + step)
    return memo[sid]


def conversation_origin(sid: str, sessions: Mapping[str, SessionRow], kinds: Mapping[str, Optional[str]]) -> str:
    """The segment that started this logical conversation: walk continuation/branch/reset links up."""
    cur, seen = sid, set()
    while kinds.get(cur) in ("continuation", "branch", "reset") and cur not in seen:
        seen.add(cur)
        cur = sessions[cur]["parent_session_id"]
    return cur


# ── sessions: delegation records, groups, routes ──────────────────────────────


def load_delegations(conn: sqlite3.Connection) -> dict:
    """``session_id -> delegate_sessions row`` keyed by both the original child and its current tip."""
    try:
        cur = conn.execute("SELECT * FROM delegate_sessions")
    except sqlite3.OperationalError:
        return {}
    names = [d[0] for d in cur.description]
    out: dict = {}
    for values in cur:
        rec = dict(zip(names, values))
        out.setdefault(rec["session_id"], rec)
        out[rec["current_session_id"]] = rec
    return out


def load_groups(conn: sqlite3.Connection) -> dict:
    """``child session_id -> delegation_id`` for children a recorded async delegation spawned (not resumed)."""
    try:
        rows = conn.execute("SELECT delegation_id, dispatched_at, result_json, event_json FROM async_delegations "
                            "ORDER BY dispatched_at").fetchall()
    except sqlite3.OperationalError:
        return {}
    groups: dict = {}
    for delegation_id, _at, result_json, event_json in rows:
        for result in (_json_obj(result_json) or _json_obj(event_json)).get("results") or []:
            sid = result.get("session_id") if isinstance(result, dict) else None
            if isinstance(sid, str) and sid and not result.get("resumed"):
                groups.setdefault(sid, delegation_id)
    return groups


def derive_route(row: SessionRow, delegation: Optional[Mapping[str, Any]]) -> Optional[dict]:
    """A credential-free route ``{provider, model, reasoning_effort?, fallback_providers?, endpoint_sha256?}``."""
    runtime_raw = row.config.get("gateway_runtime")
    runtime: dict = runtime_raw if isinstance(runtime_raw, dict) else {}
    record: Mapping[str, Any] = delegation or {}
    descriptor = _json_obj(record.get("route_descriptor"))
    provider = (record.get("provider") or descriptor.get("provider") or row.config.get("provider")
                or runtime.get("provider") or row["billing_provider"])
    model = record.get("model") or row.config.get("model") or row["model"]
    if not (isinstance(provider, str) and provider and isinstance(model, str) and model):
        return None
    route: dict = {"provider": provider, "model": model}
    reasoning = row.config.get("reasoning_config")
    if isinstance(reasoning, dict) and reasoning.get("enabled") and isinstance(reasoning.get("effort"), str):
        route["reasoning_effort"] = reasoning["effort"]
    fallbacks = descriptor.get("fallback_providers")
    if isinstance(fallbacks, list):
        route["fallback_providers"] = [{k: f[k] for k in ("provider", "model") if isinstance(f, dict) and f.get(k)}
                                       for f in fallbacks if isinstance(f, dict)]
    base_url = row.config.get("base_url") or runtime.get("base_url") or row["billing_base_url"]
    if isinstance(base_url, str) and base_url:
        route["endpoint_sha256"] = hashlib.sha256(base_url.encode()).hexdigest()
    return route


def _status(row: SessionRow, delegation: Optional[Mapping[str, Any]]) -> Optional[str]:
    if delegation is not None:
        if delegation["current_session_id"] == row["id"]:
            return delegation["status"]
        return "closed"
    return "closed" if row["ended_at"] is not None else None


def _dumps(value: Any) -> Optional[str]:
    return None if value is None else json.dumps(value, sort_keys=True, separators=(",", ":"))


def derive_session_fields(sid: str, sessions: Mapping[str, SessionRow], kinds: Mapping[str, Optional[str]],
                          memo: dict, delegations: Mapping[str, Any], groups: Mapping[str, str]) -> dict:
    row = sessions[sid]
    root, depth = _topology(sid, sessions, kinds, memo)
    delegation = delegations.get(sid)
    fields = {"root_session_id": root, "depth": depth, "lineage_kind": kinds.get(sid), "group_id": groups.get(sid),
              "status": _status(row, delegation), "route": _dumps(derive_route(row, delegation))}
    if delegation is not None:
        fields["role"] = delegation.get("role")
        fields["workspace"] = delegation.get("workspace")
        if delegation.get("forked_context"):
            fields["fork_origin"] = _dumps({"session_id": delegation["owner_session_id"], "message_uid": None,
                                            "policy": "legacy_forked_context"})
    return fields


@dataclass
class SessionPlan:
    updates: dict  # sid -> {column: value}
    counts: Counter
    kinds: dict


def plan_sessions(conn: sqlite3.Connection, sessions: Mapping[str, SessionRow]) -> SessionPlan:
    kinds = {sid: lineage_kind(row, sessions) for sid, row in sessions.items()}
    delegations, groups, memo = load_delegations(conn), load_groups(conn), {}
    updates, counts = {}, Counter()
    for sid, row in sessions.items():
        derived = derive_session_fields(sid, sessions, kinds, memo, delegations, groups)
        change = {}
        for column, value in derived.items():
            if value is None:
                continue
            stored = row[column]
            if stored is None:
                change[column] = value
                counts[f"set_{column}"] += 1
            elif stored != value:
                counts[f"conflict_{column}"] += 1
        if change:
            updates[sid] = change
        counts[f"lineage_kind={kinds[sid]}"] += 1
        record = delegations.get(sid)
        if record is not None and record.get("current_session_id") == sid and derived["depth"] is not None \
                and record.get("depth") != derived["depth"]:
            counts["depth_disagrees_with_delegate_sessions"] += 1
    counts["sessions_total"] = len(sessions)
    counts["sessions_changed"] = len(updates)
    return SessionPlan(updates, counts, kinds)


# ── scope ─────────────────────────────────────────────────────────────────────


def _last_message_times(conn: sqlite3.Connection) -> dict:
    return dict(conn.execute("SELECT session_id, MAX(timestamp) FROM messages GROUP BY session_id").fetchall())


def main_sessions(sessions: Mapping[str, SessionRow]) -> set:
    """The current tip of every human-platform DM conversation key (the main DM lineage seeds)."""
    tips: dict = {}
    for sid, row in sessions.items():
        if row["chat_type"] != "dm" or row["source"] not in HUMAN_SOURCES or not row["session_key"]:
            continue
        stamp = row["last_activity_at"] or row["started_at"] or 0
        if row["session_key"] not in tips or stamp > tips[row["session_key"]][0]:
            tips[row["session_key"]] = (stamp, sid)
    return {sid for _stamp, sid in tips.values()}


def _open_delegations(conn: sqlite3.Connection) -> set:
    try:
        rows = conn.execute("SELECT session_id, current_session_id FROM delegate_sessions "
                            "WHERE status IN ('running', 'idle')").fetchall()
    except sqlite3.OperationalError:
        return set()
    return {sid for row in rows for sid in row}


def _close_lineage(seeds: set, sessions: Mapping[str, SessionRow], kinds: Mapping[str, Optional[str]]) -> set:
    """Seeds plus every ancestor and every continuation/branch/reset descendant (the logical session)."""
    scope: set = set()
    for sid in seeds:
        cur = sid
        while cur in sessions and cur not in scope:
            scope.add(cur)
            cur = sessions[cur]["parent_session_id"]
    stack = list(scope)
    while stack:
        for child in sessions[stack.pop()].children:
            if child not in scope and kinds.get(child) in ("continuation", "branch", "reset"):
                scope.add(child)
                stack.append(child)
    return scope


def active_scope(conn: sqlite3.Connection, sessions: Mapping[str, SessionRow], kinds: Mapping[str, Optional[str]],
                 *, days: float, now: float, extra: Iterable[str] = ()) -> tuple[set, Counter]:
    cutoff, last = now - days * 86400, _last_message_times(conn)
    touched = {sid for sid, row in sessions.items()
               if max(row["last_activity_at"] or 0, row["ended_at"] or 0, row["started_at"] or 0, last.get(sid) or 0)
               >= cutoff}
    delegations = _open_delegations(conn) & set(sessions)
    keyed = {sid for sid, row in sessions.items() if row["source"] == "webhook" and row["ended_at"] is None
             and row["session_key"]}
    mains = main_sessions(sessions) | {sid for sid in extra if sid in sessions}
    scope = _close_lineage(touched | delegations | keyed | mains, sessions, kinds)
    counts = Counter({"seed_touched": len(touched), "seed_open_delegations": len(delegations),
                      "seed_live_keyed_webhook": len(keyed), "seed_main_dm": len(mains), "in_scope": len(scope)})
    return scope, counts


# ── messages ──────────────────────────────────────────────────────────────────


def session_context(sid: str, sessions: Mapping[str, SessionRow], kinds: Mapping[str, Optional[str]],
                    cron_jobs: Mapping[str, str]) -> SessionContext:
    origin_id = conversation_origin(sid, sessions, kinds)
    origin = sessions.get(origin_id)
    row = sessions[sid]
    ctx = SessionContext(sid, source=row["source"] or "", chat_id=row["chat_id"], user_id=row["user_id"],
                         cron_jobs=cron_jobs)
    if origin is None:
        return ctx
    kind = kinds.get(origin_id)
    if kind == "delegate":
        ctx.conversation, ctx.delegating_parent = "delegate", origin["parent_session_id"]
    elif kind in ("cron", "webhook"):
        ctx.conversation = kind
    elif kind == "root" and origin["source"] in HUMAN_SOURCES:
        ctx.conversation = "human"
    human = _human_ancestor(origin_id, sessions, kinds)
    if human is not None:
        ctx.human_platform, ctx.human_chat_id, ctx.human_user_id = human["source"], human["chat_id"], human["user_id"]
    return ctx


def _human_ancestor(sid: str, sessions: Mapping[str, SessionRow], kinds: Mapping[str, Optional[str]]):
    """The human-platform root this conversation belongs to (forks inherit their parent's human)."""
    cur, seen = sid, set()
    while cur in sessions and cur not in seen:
        seen.add(cur)
        row = sessions[cur]
        if not row["parent_session_id"]:
            return row if row["source"] in HUMAN_SOURCES and row["source"] not in ("local",) else None
        cur = row["parent_session_id"]
    return None


@dataclass
class MessageChange:
    row_id: int
    session_id: str
    rule: str
    before: dict
    after: dict


def _typed_change(row: Mapping[str, Any], typed: Typed) -> MessageChange:
    after = {"sender_kind": typed.sender_kind, "sender_id": typed.sender_id, "sent_at": typed.sent_at,
             "source_event_id": typed.source_event_id, "intent": typed.intent, "part_kind": typed.part_kind,
             "parts_json": encode_parts_json(typed.parts)}
    content = plain_text(typed.parts)
    if content != row["content"]:
        after["content"] = content
    return MessageChange(row["id"], row["session_id"], typed.rule, {"content": row["content"]}, after)


def _retag_change(row: Mapping[str, Any], retag) -> MessageChange:
    parts = [retag.part()]
    after = {"role": "user", "sender_kind": "system", "sender_id": retag.sender_id, "part_kind": retag.kind,
             "parts_json": encode_parts_json(parts), "sent_at": row["timestamp"], "finish_reason": None}
    content = plain_text(parts)
    if content != (row["content"] or ""):
        after["content"] = content
    if retag.clear_api_content:
        after["api_content"] = None
    before = {"role": "assistant", "content": row["content"], "api_content": row["api_content"],
              "finish_reason": row["finish_reason"]}
    return MessageChange(row["id"], row["session_id"], "retag:" + retag.rule, before, after)


@dataclass
class MessagePlan:
    changes: list
    counts: Counter
    tool_rows: int = 0
    bare_assistant_ids: list = field(default_factory=list)
    untyped_samples: dict = field(default_factory=dict)


def _plan_row(row: Mapping[str, Any], ctx: SessionContext, plan: MessagePlan, mirror_ids: set) -> None:
    role = row["role"]
    if role == "assistant":
        retag = classify_assistant_row(row, mirror_ids)
        if retag is not None:
            plan.changes.append(_retag_change(row, retag))
            plan.counts["retag:" + retag.rule] += 1
        elif bare_assistant_candidate(row):
            plan.bare_assistant_ids.append(row["id"])
        return
    if role != "user" or row["sender_kind"] is not None or row["parts_json"] is not None:
        if role == "user":
            plan.counts["user_already_typed"] += 1
        return
    result = type_user_row(row, ctx)
    if isinstance(result, Untyped):
        plan.counts["untyped:" + result.reason] += 1
        plan.untyped_samples.setdefault(result.reason, []).append(row["id"])
        return
    change = _typed_change(row, result)
    plan.changes.append(change)
    plan.counts["typed:" + result.rule] += 1
    if "content" in change.after:
        plan.counts["content_rewritten"] += 1
    if row["api_content"]:
        plan.counts["api_content_retained"] += 1


def plan_messages(conn: sqlite3.Connection, scope: set, sessions: Mapping[str, SessionRow],
                  kinds: Mapping[str, Optional[str]], *, cron_jobs: Mapping[str, str], mirror_ids: set) -> MessagePlan:
    available = _columns(conn, "messages")
    select = _select_list(available, MESSAGE_COLUMNS)
    plan, contexts = MessagePlan([], Counter()), {}
    ordered = sorted(scope)
    for start in range(0, len(ordered), 400):
        batch = ordered[start:start + 400]
        cur = conn.execute(f"SELECT {select} FROM messages WHERE session_id IN ({','.join('?' * len(batch))}) "
                           "AND role IN ('user', 'assistant') ORDER BY id", batch)
        names = [d[0] for d in cur.description]
        for values in cur:
            row = dict(zip(names, values))
            ctx = contexts.get(row["session_id"])
            if ctx is None:
                ctx = contexts[row["session_id"]] = session_context(row["session_id"], sessions, kinds, cron_jobs)
            _plan_row(row, ctx, plan, mirror_ids)
        plan.tool_rows += conn.execute(
            f"SELECT COUNT(*) FROM messages WHERE session_id IN ({','.join('?' * len(batch))}) AND role = 'tool' "
            f"AND {'sender_kind IS NULL' if 'sender_kind' in available else '1'}", batch).fetchone()[0]
    plan.counts["tool_result_rows"] = plan.tool_rows
    return plan


# ── apply ─────────────────────────────────────────────────────────────────────


def _apply_sessions(conn: sqlite3.Connection, plan: SessionPlan) -> None:
    for sid, change in plan.updates.items():
        columns = sorted(change)
        assignments = ", ".join(f"{col} = COALESCE({col}, ?)" for col in columns)
        conn.execute(f"UPDATE sessions SET {assignments} WHERE id = ?", [change[c] for c in columns] + [sid])


def _apply_messages(conn: sqlite3.Connection, plan: MessagePlan, scope: set) -> None:
    for change in plan.changes:
        columns = sorted(change.after)
        guard = "role = 'assistant'" if change.after.get("role") == "user" else \
            "role = 'user' AND sender_kind IS NULL AND parts_json IS NULL"
        conn.execute(f"UPDATE messages SET {', '.join(f'{c} = ?' for c in columns)} WHERE id = ? AND {guard}",
                     [change.after[c] for c in columns] + [change.row_id])
    ordered = sorted(scope)
    for start in range(0, len(ordered), 400):
        batch = ordered[start:start + 400]
        conn.execute(f"UPDATE messages SET sender_kind = 'tool_result' WHERE role = 'tool' AND sender_kind IS NULL "
                     f"AND session_id IN ({','.join('?' * len(batch))})", batch)


def ensure_schema(db_path: Path) -> None:
    """Open the database once through SessionDB so the additive columns, indexes and triggers exist."""
    from hermes_state import SessionDB
    SessionDB(db_path=db_path).close()


def _role_counts(conn: sqlite3.Connection) -> dict:
    return dict(conn.execute("SELECT role, COUNT(*) FROM messages GROUP BY role").fetchall())


# ── driver ────────────────────────────────────────────────────────────────────


@dataclass
class Options:
    db: Path
    apply: bool = False
    scope: str = "active"
    days: float = 14.0
    now: Optional[float] = None
    main_sessions: tuple = ()
    mirror_ids: tuple = ()
    cron_jobs: Optional[Path] = None
    changes_out: Optional[Path] = None
    receipt_path: Optional[Path] = None
    allow_live_db: bool = False
    sample_seed: int = 7


def load_cron_jobs(path: Optional[Path]) -> dict:
    """``job name -> job id`` from a cron ``jobs.json`` (optional; resolves ``[Cron delivery: name]`` senders)."""
    if path is None or not Path(path).is_file():
        return {}
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        logger.warning("Unreadable cron jobs file %s; cron mirror senders stay unnamed", path)
        return {}
    jobs = data.get("jobs") if isinstance(data, dict) else data
    return {job["name"]: str(job["id"]) for job in jobs or [] if isinstance(job, dict) and job.get("name")
            and job.get("id")}


def _write_changes(path: Path, plan: MessagePlan) -> None:
    with open(path, "w", encoding="utf-8") as fh:
        fh.writelines(json.dumps({"id": change.row_id, "session_id": change.session_id, "rule": change.rule,
                                  "before": change.before, "after": change.after}, ensure_ascii=False) + "\n"
                      for change in plan.changes)


def _connect(opts: Options) -> sqlite3.Connection:
    if opts.apply:
        conn = sqlite3.connect(str(opts.db), timeout=60, isolation_level=None)
    else:
        conn = sqlite3.connect(f"file:{opts.db}?mode=ro", uri=True, timeout=60, isolation_level=None)
    conn.execute("PRAGMA busy_timeout = 60000")
    return conn


def _scope_for(conn, opts: Options, sessions, kinds) -> tuple[set, Counter]:
    if opts.scope == "all":
        return set(sessions), Counter({"in_scope": len(sessions)})
    return active_scope(conn, sessions, kinds, days=opts.days, now=opts.now or time.time(),
                        extra=opts.main_sessions)


def run(opts: Options) -> dict:
    """Plan (and with ``apply``, write) the migration; returns the JSON receipt."""
    started = time.time()
    receipt: dict = {"db": str(opts.db), "mode": "apply" if opts.apply else "dry-run", "scope": opts.scope,
                     "days": opts.days, "db_sha256_before": file_sha256(opts.db)}
    if opts.apply:
        ensure_schema(opts.db)
    conn = _connect(opts)
    try:
        receipt["messages_before"] = _role_counts(conn)
        sessions = load_sessions(conn)
        session_plan = plan_sessions(conn, sessions)
        scope, scope_counts = _scope_for(conn, opts, sessions, session_plan.kinds)
        message_plan = plan_messages(conn, scope, sessions, session_plan.kinds,
                                     cron_jobs=load_cron_jobs(opts.cron_jobs), mirror_ids=set(opts.mirror_ids))
        if opts.apply:
            conn.execute("BEGIN IMMEDIATE")
            _apply_sessions(conn, session_plan)
            _apply_messages(conn, message_plan, scope)
            conn.execute("COMMIT")
        receipt["messages_after"] = _role_counts(conn)
    finally:
        conn.close()
    if opts.changes_out:
        _write_changes(opts.changes_out, message_plan)
    receipt.update(_receipt_body(session_plan, scope_counts, message_plan, opts))
    receipt["elapsed_s"] = round(time.time() - started, 2)
    return receipt


def _receipt_body(session_plan: SessionPlan, scope_counts: Counter, plan: MessagePlan, opts: Options) -> dict:
    rng = random.Random(opts.sample_seed)
    untyped = {reason: sorted(rng.sample(ids, min(5, len(ids)))) for reason, ids in plan.untyped_samples.items()}
    rules = Counter(change.rule for change in plan.changes)
    return {
        "sessions": dict(sorted(session_plan.counts.items())),
        "scope_counts": dict(scope_counts),
        "messages": dict(sorted(plan.counts.items())),
        "message_changes_total": len(plan.changes) + plan.tool_rows,
        "message_rows_by_rule": dict(sorted(rules.items())),
        "sessions_with_message_changes": len({change.session_id for change in plan.changes}),
        "bare_assistant_rows_not_retagged": len(plan.bare_assistant_ids),
        "bare_assistant_sample_ids": sorted(rng.sample(plan.bare_assistant_ids, min(10, len(plan.bare_assistant_ids)))),
        "untyped_sample_ids": untyped,
    }


def parse_args(argv: Optional[list] = None) -> Options:
    parser = argparse.ArgumentParser(description=(__doc__ or "").split("\n\n")[0])
    parser.add_argument("--db", required=True, type=Path, help="state.db to migrate (a quiesced or copied file)")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true", default=True, help="plan only (default)")
    mode.add_argument("--apply", action="store_true", help="write the changes in one transaction")
    parser.add_argument("--scope", choices=("active", "all"), default="active")
    parser.add_argument("--days", type=float, default=14.0, help="active scope: sessions touched in the last N days")
    parser.add_argument("--now", type=float, default=None, help="epoch seconds for the active-scope cutoff")
    parser.add_argument("--main-session", action="append", default=[], help="extra lineage seed (repeatable)")
    parser.add_argument("--retag-mirror-id", action="append", type=int, default=[],
                        help="assistant row id proven to be a hermes-send mirror (repeatable)")
    parser.add_argument("--cron-jobs", type=Path, default=None, help="cron jobs.json (default: <db dir>/cron/jobs.json)")
    parser.add_argument("--changes-out", type=Path, default=None, help="write every planned row change as JSONL")
    parser.add_argument("--receipt", type=Path, default=None, help="also write the receipt JSON here")
    parser.add_argument("--allow-live-db", action="store_true",
                        help="permit --apply on the active profile's own state.db (only with every gateway stopped)")
    args = parser.parse_args(argv)
    cron_jobs = args.cron_jobs or args.db.parent / "cron" / "jobs.json"
    opts = Options(db=args.db, apply=args.apply, scope=args.scope, days=args.days, now=args.now,
                   main_sessions=tuple(args.main_session), mirror_ids=tuple(args.retag_mirror_id),
                   cron_jobs=cron_jobs, changes_out=args.changes_out, receipt_path=args.receipt,
                   allow_live_db=args.allow_live_db)
    return opts


def _is_live_db(path: Path) -> bool:
    try:
        from hermes_constants import get_hermes_home
        return Path(path).resolve() == (Path(get_hermes_home()) / "state.db").resolve()
    except (ImportError, OSError):
        return False


def main(argv: Optional[list] = None) -> int:
    opts = parse_args(argv)
    if not opts.db.is_file():
        print(f"no such database: {opts.db}", file=sys.stderr)
        return 2
    if opts.apply and _is_live_db(opts.db) and not opts.allow_live_db:
        print("refusing --apply on the active profile's state.db without --allow-live-db "
              "(stop every gateway and back the file up first)", file=sys.stderr)
        return 2
    receipt = run(opts)
    text = json.dumps(receipt, indent=2, sort_keys=True)
    if opts.receipt_path:
        Path(opts.receipt_path).write_text(text + "\n", encoding="utf-8")
    print(text)
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
