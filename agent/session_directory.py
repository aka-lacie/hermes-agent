"""Session discovery and bounded observation over the unified ``sessions`` table (plan §4.5, §4.6).

Freya's rule for ``delegate_task(action="list")`` (binding, BRIEF3): every session shares one table, but a
caller's default list (``scope="children"``) shows only

- its own delegate spawns (``lineage_kind='delegate'`` whose parent is in the caller's conversation), and
- sessions explicitly opted in from another source: a webhook/cron (or other non-delegate) session whose
  ``delivery_target`` is ``{kind: "session", session_id: <caller>, attach: true}`` or
  ``{kind: "session", selector: <a selector the caller's conversation holds NOW>, attach: true}``, badged
  with its ``lineage_kind``. A selector (``discord:dm:<id>``, ``discord``, ``bot-chat``, ...) is resolved at
  list time (``gateway.session_selectors``), so after ``/new`` the opt-in follows the chat to its new session;
  an unresolvable selector falls back to the session the opt-in was last bound to
  (``sessions.attached_session_id``, the attach primitive: never ``parent_session_id``).

Compression continuations (the same conversation), memory/background forks, branches/resets, standalone
cron/webhook sessions and the caller's own lineage never appear. Grandchildren (d=2) never flatten into the
top-level list: each row carries only the integers ``running_children`` / ``children_total``, ``observe`` on
a child carries a compact roll-up of its sub-children (``agent.session_views``), and ``scope="tree"``
expands the caller's whole subtree.

**Bounded work (review R8/R9).** Candidate selection, Freya's semantic filter, the closed filter, the
activity ordering and ``LIMIT`` all run in ONE SQL statement over indexed arms (``parent_session_id``,
``attached_session_id`` and the expression indexes on ``delivery_target``'s ``session_id`` / ``selector``);
only the returned rows are hydrated, in the same statement, through correlated per-row subqueries
(compression segments, latest message timestamp per segment, running/queued admissions through
``session_admissions_pending``, the latest parent assignment through ``(target_session_id, seq)`` DESC
LIMIT 1, grandchild roll-up counts). Nothing reads a transcript or an admission history in Python, and the
statement count of a list does not depend on how many children, grandchildren or messages exist.

Policy is pluggable **per profile database** (review R6/R7): :func:`set_session_acl` replaces the base ACL
of one scope (a profile's ``state.db``; default: the current profile's) and :func:`install_session_acl`
stacks a generation lease on top of it (what ``ctx.sessions.set_acl`` uses; plugin unload disposes the
lease). The effective policy of a scope is its newest live lease, else its base, else the default; a
lookup always uses the scope of the database it authorizes against, so profile A's widening never reaches
profile B, and disposing leases in any order never resurrects a retired policy. ``action="peer"`` is the
peer-admission ACL (default: siblings only: same parent conversation, both delegate sessions). The
primitives here take ``caller_session_id`` and apply only those ACLs plus the store's own safety rules;
nothing hardcodes ``caller == parent``.
"""

from __future__ import annotations

import itertools
import json
import logging
import os
import sqlite3
import threading
from functools import lru_cache
from pathlib import Path
from typing import Any, Callable, Iterable, Optional

logger = logging.getLogger(__name__)

LIST_SCOPES = ("children", "siblings", "tree")
OPT_IN_KINDS = frozenset({"webhook", "cron"})
_MAX_TREE_DEPTH = 8
_MAX_TREE_ROWS = 500
_CHAIN_CAP = 64
_CONTENT_JSON_PREFIX = "\x00json:"  # hermes_state.SessionDB._CONTENT_JSON_PREFIX (structured content)
_LATEST_STEP_CHARS = 4000  # head of the latest-step message read for a roll-up row (clip is 120 chars)

# Chain segments of one logical conversation: a compression continuation of *parent* (never a fork).
_NEXT_SEGMENT_SQL = None  # built lazily (needs hermes_state_common)


def _next_segment_sql(parent_expr: str = "?") -> str:
    global _NEXT_SEGMENT_SQL
    if _NEXT_SEGMENT_SQL is None:
        from hermes_state_common import _non_continuation_child_sql, _sql_session_last_active
        _NEXT_SEGMENT_SQL = (
            "SELECT child.id FROM sessions parent JOIN sessions child ON child.parent_session_id = parent.id "
            "WHERE parent.id = {parent} AND parent.end_reason = 'compression' "
            + _non_continuation_child_sql("child.", "parent.id")
            + " ORDER BY CASE WHEN child.end_reason = 'compression' THEN 0 "
            "WHEN child.ended_at IS NULL THEN 1 ELSE 2 END, "
            + _sql_session_last_active("child") + " DESC, child.started_at DESC, child.id DESC LIMIT 1")
    return _NEXT_SEGMENT_SQL.replace("{parent}", parent_expr)


def json_field(value: Any) -> Any:
    if value in (None, ""):
        return None
    if isinstance(value, (dict, list)):
        return value
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return None


# ── lineage ──────────────────────────────────────────────────────────────────

def chain(conn, session_id: str) -> list[str]:
    """``session_id`` and its compression continuations, oldest first (the execution tip is last)."""
    out, seen = [session_id], {session_id}
    for _ in range(_CHAIN_CAP):
        row = conn.execute(_next_segment_sql(), (out[-1],)).fetchone()
        if row is None or row[0] in seen:
            break
        out.append(row[0])
        seen.add(row[0])
    return out


def logical_root(db, conn, session_id: str) -> str:
    """The first segment of *session_id*'s compression lineage (its stable conversation identity)."""
    return db._session_turn_lease_key_on_conn(conn, session_id)


def lineage(db, conn, session_id: str) -> list[str]:
    """Every segment of *session_id*'s conversation: what the caller's own lineage means."""
    if not session_id:
        return []
    return chain(conn, logical_root(db, conn, session_id))


def _placeholders(values: Iterable[Any]) -> str:
    return ",".join("?" * len(list(values)))


def _segments_from(raw: Any, start: str) -> list[str]:
    """The ``[[n, id], ...]`` JSON a segment CTE aggregates, as :func:`chain` returns it (cut at a repeat)."""
    pairs = sorted((int(n), sid) for n, sid in (json_field(raw) or []) if sid is not None)
    out, seen = [], set()
    for _, sid in pairs:
        if sid in seen:
            break
        out.append(sid)
        seen.add(sid)
    return out or [start]


# ── SQL building blocks (one definition each; every list/observe statement composes them) ─────────────

def _safe_target(alias: str) -> str:
    return f"(CASE WHEN json_valid({alias}delivery_target) THEN {alias}delivery_target ELSE json_object() END)"


def target_sql(alias: str, path: str) -> str:
    """``delivery_target``'s *path* (a non-throwing json_extract). With ``alias=""`` it is exactly the
    expression ``idx_sessions_delivery_session`` / ``idx_sessions_delivery_selector`` index."""
    from hermes_state_common import _sql_json_extract
    return _sql_json_extract(f"{alias}delivery_target", path)


def _target_type(alias: str, path: str) -> str:
    return f"json_type({_safe_target(alias)}, '{path}')"


def _truthy(alias: str, path: str) -> str:
    """Python truthiness of the JSON value at *path* (missing/null/false/""/0/[]/{} are falsy)."""
    kind, value = _target_type(alias, path), target_sql(alias, path)
    return (f"COALESCE({kind} NOT IN ('null', 'false') AND NOT ({kind} = 'text' AND {value} = '') "
            f"AND NOT ({kind} IN ('integer', 'real') AND {value} = 0) AND NOT ({kind} = 'array' AND {value} = '[]') "
            f"AND NOT ({kind} = 'object' AND {value} = '{{}}'), 0)")


def _held_sql(alias: str, selectors: bool) -> str:
    """:func:`_target_held_by` in SQL (params ``:lin`` = the caller lineage, ``:held`` / ``:unres`` = the
    selectors that resolve into it / resolve nowhere)."""
    sid, sel = target_sql(alias, "$.session_id"), target_sql(alias, "$.selector")
    selector_arm = (f"({sel} IN (SELECT value FROM json_each(:held)) OR ({sel} IN (SELECT value FROM json_each(:unres)) "
                    f"AND {alias}attached_session_id IN (SELECT value FROM json_each(:lin))))") if selectors else "0"
    return (f"(CASE WHEN {_truthy(alias, '$.session_id')} THEN ({_target_type(alias, '$.session_id')} = 'text' "
            f"AND {sid} IN (SELECT value FROM json_each(:lin))) "
            f"WHEN {_truthy(alias, '$.selector')} THEN {selector_arm} ELSE 0 END)")


def _attached_sql(alias: str, selectors: bool) -> str:
    """:func:`attached_to` in SQL: an ``attach: true`` ``kind=session`` target the caller holds."""
    return (f"({alias}delivery_target IS NOT NULL AND {_target_type(alias, '$.attach')} IS 'true' "
            f"AND {target_sql(alias, '$.kind')} IS 'session' AND {_held_sql(alias, selectors)})")


def _kind_sql(s: str = "s.", d: str = "d.") -> str:
    """:func:`_listed_kind` in SQL (*d*: the legacy ``delegate_sessions`` join, NULL when absent)."""
    return f"COALESCE(NULLIF({s}lineage_kind, ''), CASE WHEN {d}session_id IS NOT NULL THEN 'delegate' END)"


_EXCLUDED_KINDS = frozenset({"continuation", "branch", "reset", "switch", "root"})
_NOT_ATTACHABLE_SQL = "('continuation', 'branch', 'reset', 'switch', 'root', 'delegate')"


def _how_sql(selectors: bool, s: str = "s.", d: str = "d.") -> str:
    """:func:`_is_default_child` in SQL: ``'own'`` / ``'attached'`` / NULL (params ``:lin`` and selectors)."""
    kind = _kind_sql(s, d)
    return (f"(CASE WHEN {kind} = 'delegate' AND {s}parent_session_id IN (SELECT value FROM json_each(:lin)) "
            f"THEN 'own' WHEN ({kind} IS NULL OR {kind} NOT IN {_NOT_ATTACHABLE_SQL}) "
            f"AND {_attached_sql(s, selectors)} THEN 'attached' END)")


def _running_sql(s: str = "s.") -> str:
    return (f"EXISTS (SELECT 1 FROM session_admissions a WHERE a.target_session_id = {s}id "
            f"AND a.status IN ('started', 'unknown'))")


def _status_sql(s: str = "s.", d: str = "d.") -> str:
    """:func:`record`'s status (a cached ``running`` with no started admission left is a finished run)."""
    stored = f"COALESCE(NULLIF({s}status, ''), {d}status)"
    return (f"(CASE WHEN {stored} = 'closed' THEN 'closed' WHEN {_running_sql(s)} THEN 'running' "
            f"WHEN {stored} IN ('running', 'idle') THEN 'idle' "
            f"WHEN COALESCE({s}ended_at, 0) != 0 AND {s}end_reason IS NOT 'compression' THEN 'closed' "
            f"ELSE 'idle' END)")


def _created_sql(s: str = "s.", d: str = "d.") -> str:
    return f"COALESCE(NULLIF({s}started_at, 0), NULLIF({d}created_at, 0), 0)"


def _floor_sql(s: str = "s.", d: str = "d.") -> str:
    return f"MAX({_created_sql(s, d)}, COALESCE(NULLIF({s}last_activity_at, 0), 0))"


def _legacy_join(present: bool, s: str = "s", d: str = "d") -> str:
    if present:
        return (f" LEFT JOIN delegate_sessions {d} ON {d}.session_id = {s}.id "
                f"AND COALESCE({s}.lineage_kind, '') = ''")
    return f" LEFT JOIN (SELECT NULL AS session_id, NULL AS status, NULL AS created_at) {d} ON 0"


def _seg_cte(base: str) -> str:
    """``seg(start, id, n)``: every compression segment of each ``base`` row (``n`` = position; at most
    ``_CHAIN_CAP`` steps; a NULL ``id`` row marks the end of a chain)."""
    return (f"seg(start, id, n) AS (SELECT id, id, 0 FROM {base} UNION ALL "
            f"SELECT seg.start, ({_next_segment_sql('seg.id')}), seg.n + 1 FROM seg "
            f"WHERE seg.id IS NOT NULL AND seg.n < {_CHAIN_CAP})")


def _activity_sql(o: str) -> str:
    """Newest message timestamp over *o*'s segments: one index-served MAX per segment (never a scan)."""
    return (f"(SELECT MAX((SELECT MAX(m.timestamp) FROM messages m WHERE m.session_id = seg.id)) FROM seg "
            f"WHERE seg.start = {o}.id AND seg.id IS NOT NULL)")


def _hydration_sql(o: str, *, rollup: bool, legacy: bool) -> str:
    """Per-row record columns for an already-selected row *o* (never evaluated before ``LIMIT``)."""
    payload = "(CASE WHEN json_valid(a.payload_json) THEN a.payload_json ELSE '{}' END)"
    cols = [
        f"(SELECT json_group_array(json_array(seg.n, seg.id)) FROM seg WHERE seg.start = {o}.id "
        f"AND seg.id IS NOT NULL) AS _segs",
        f"(SELECT COUNT(*) FROM session_admissions a WHERE a.target_session_id = {o}.id "
        f"AND a.status = 'queued') AS _queued",
        f"(SELECT json_extract({payload}, '$.text') FROM session_admissions a WHERE a.target_session_id = {o}.id "
        f"AND substr(a.principal_id, 1, 7) = 'parent:' AND json_type({payload}, '$.text') = 'text' "
        f"ORDER BY a.seq DESC LIMIT 1) AS _latest",
    ]
    if rollup:
        # COUNT(DISTINCT): a looping chain repeats segments; seg(start) and idx_sessions_parent drive the join.
        children = (f"FROM seg JOIN sessions c ON c.parent_session_id = seg.id{_legacy_join(legacy, 'c', 'dc')} "
                    f"WHERE seg.start = {o}.id AND seg.id IS NOT NULL AND {_kind_sql('c.', 'dc.')} = 'delegate'")
        cols += [
            f"(SELECT COUNT(DISTINCT c.id) {children}) AS _children_total",
            f"(SELECT COUNT(DISTINCT c.id) {children} AND c.status IS NOT 'closed' AND EXISTS (SELECT 1 FROM "
            f"session_admissions a WHERE a.target_session_id = c.id AND a.status IN ('started', 'unknown', 'queued'))"
            f") AS _running_children",
        ]
    return ", ".join(cols)


def _row_cols(selectors: bool) -> str:
    return (f"s.*, {_kind_sql()} AS _kind, {_how_sql(selectors)} AS _how, {_status_sql()} AS _status, "
            f"{_created_sql()} AS _created, {_floor_sql()} AS _floor, d.session_id AS _legacy_id")


def _legacy_table(conn) -> bool:
    return conn.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'delegate_sessions'").fetchone() \
        is not None


def _params(lin: Iterable[str], held: Iterable[Any] = (), unres: Iterable[Any] = (), **extra) -> dict:
    return {"lin": json.dumps(list(lin)), "held": json.dumps(list(held)), "unres": json.dumps(list(unres)), **extra}


# ── records ──────────────────────────────────────────────────────────────────

def _effective_kind(row) -> Optional[str]:
    """``lineage_kind``, or a legacy delegate marker naming the row's own parent (pre-migration rows)."""
    kind = row["lineage_kind"]
    if kind:
        return kind
    config = json_field(row["model_config"]) or {}
    if row["parent_session_id"] and config.get("_delegate_from") == row["parent_session_id"]:
        return "delegate"
    return None


# TODO(phase5): the retired parallel store's DDL, kept only so migration fixtures (and the read shim's tests)
# can build a pre-unified store. Nothing in the runtime creates this table any more.
LEGACY_DELEGATE_SESSIONS_SCHEMA = """
CREATE TABLE IF NOT EXISTS delegate_sessions (
    session_id TEXT PRIMARY KEY REFERENCES sessions(id) ON DELETE CASCADE,
    owner_session_id TEXT NOT NULL,
    current_session_id TEXT NOT NULL,
    title TEXT NOT NULL,
    initial_goal TEXT NOT NULL,
    assignment TEXT NOT NULL,
    context TEXT NOT NULL,
    workspace TEXT,
    forked_context INTEGER NOT NULL DEFAULT 0,
    route_descriptor TEXT,
    model TEXT NOT NULL,
    provider TEXT NOT NULL,
    role TEXT NOT NULL,
    depth INTEGER NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('running', 'idle', 'closed')),
    created_at REAL NOT NULL,
    last_activity_at REAL NOT NULL,
    run_token TEXT,
    runner TEXT
)
"""


def _legacy_delegate(conn, session_id: str) -> Optional[dict]:
    """TODO(phase5): one-way read shim for pre-unified ``delegate_sessions`` rows (2f copies them into the
    sessions columns; this only fills what a not-yet-migrated store still keeps there). Never written."""
    try:
        row = conn.execute("SELECT * FROM delegate_sessions WHERE session_id = ?", (session_id,)).fetchone()
    except sqlite3.OperationalError:  # no such table: a store created after the unified model
        return None
    return dict(row) if row is not None else None


def _legacy_rows(conn, rows: Iterable[Any], present: Optional[bool] = None) -> dict[str, dict]:
    """The legacy shim rows of every *rows* entry without a ``lineage_kind`` (one batched read, only when
    such a row and the table exist)."""
    ids = [r["id"] for r in rows if not r["lineage_kind"]]
    if not ids or not (_legacy_table(conn) if present is None else present):
        return {}
    return {r["session_id"]: dict(r) for r in conn.execute(
        "SELECT * FROM delegate_sessions WHERE session_id IN (SELECT value FROM json_each(?))", (json.dumps(ids),))}


def _build_record(row, legacy: Optional[dict]) -> dict:
    """The public record of one hydrated session row (credential-free; never transcript bodies)."""
    row = dict(row)
    sid = row["id"]
    config = json_field(row.get("model_config")) or {}
    brief = config.get("_delegate_brief") if isinstance(config.get("_delegate_brief"), dict) else {}
    legacy = legacy if not row.get("lineage_kind") else None
    route = json_field(row.get("route")) or {}
    segments = _segments_from(row.get("_segs"), sid)
    fork = json_field(row.get("fork_origin"))
    kind = row.get("lineage_kind") or ("delegate" if legacy else _effective_kind(_RowView(row)))
    initial = brief.get("initial_goal") or (legacy or {}).get("initial_goal") or ""
    created = float(row.get("_created") or 0)
    activity = row.get("_act")
    last = max(float(row.get("_floor") or 0), float(activity) if activity is not None else 0.0)
    latest = row.get("_latest")
    return {
        "session_id": sid, "current_session_id": segments[-1], "parent_session_id": row.get("parent_session_id"),
        "root_session_id": row.get("root_session_id"), "depth": row.get("depth"), "lineage_kind": kind,
        "title": str(brief.get("title") or (legacy or {}).get("title") or " ".join(initial.split())[:80]
                     or row.get("title") or "Session"),
        "initial_goal": initial, "assignment": latest or (legacy or {}).get("assignment") or initial,
        "context": brief.get("context") or (legacy or {}).get("context") or "",
        "workspace": row.get("workspace") or (legacy or {}).get("workspace"),
        "forked_context": bool(fork) or bool((legacy or {}).get("forked_context")), "fork_origin": fork,
        "route": route or None, "model": route.get("model") or row.get("model") or (legacy or {}).get("model"),
        "provider": route.get("provider") or (legacy or {}).get("provider") or row.get("billing_provider"),
        "role": row.get("role") or (legacy or {}).get("role") or "leaf",
        "status": row.get("_status") or "idle", "queued": int(row.get("_queued") or 0), "created_at": created,
        "last_activity_at": last,
        "delivery_target": json_field(row.get("delivery_target")), "group_id": row.get("group_id"),
        "retained": bool(brief.get("retain") or legacy),
    }


def _hydrate_rows(conn, rows: Iterable[Any], *, rollup: bool = False, legacy: Optional[bool] = None
                  ) -> list[tuple[dict, list[str]]]:
    """``(record, compression segments)`` of many rows from ONE statement (plus one legacy read only for
    legacy rows), in input order. *legacy*: whether ``delegate_sessions`` exists (None: probe when needed)."""
    rows = list(rows)
    if not rows:
        return []
    ids = [r["id"] for r in rows]
    if not any(not r["lineage_kind"] for r in rows):
        legacy = False
    elif legacy is None:
        legacy = _legacy_table(conn)
    got = {r["id"]: r for r in conn.execute(
        f"WITH RECURSIVE base(id) AS (SELECT value FROM json_each(:ids)), {_seg_cte('base')} "
        f"SELECT {_row_cols(False)}, {_activity_sql('s')} AS _act, {_hydration_sql('s', rollup=rollup, legacy=legacy)} "
        f"FROM base JOIN sessions s ON s.id = base.id{_legacy_join(legacy)}",
        _params([], ids=json.dumps(ids))).fetchall()}
    shims = _legacy_rows(conn, got.values(), legacy) if legacy else {}
    out = []
    for sid in ids:
        if sid in got:
            info = _build_record(got[sid], shims.get(sid))
            if rollup:
                info["running_children"] = int(got[sid]["_running_children"] or 0)
                info["children_total"] = int(got[sid]["_children_total"] or 0)
            out.append((info, _segments_from(got[sid]["_segs"], sid)))
    return out


def hydrate(conn, rows: Iterable[Any], *, rollup: bool = False) -> list[dict]:
    """:func:`record` of many rows in ONE statement (plus one legacy read only for legacy rows), input order."""
    return [info for info, _ in _hydrate_rows(conn, rows, rollup=rollup)]


def record(conn, row) -> dict:
    """The public record of one session row (credential-free; never transcript bodies)."""
    found = hydrate(conn, [row])
    if not found:
        raise LookupError("No session with that session_id.")
    return found[0]


class _RowView(dict):
    def __getitem__(self, key):
        return self.get(key)


# ── attachment (literal or selector targets) ─────────────────────────────────

def _column(row, key: str) -> Any:
    try:
        return row[key]
    except (KeyError, IndexError):
        return None


class _Selectors:
    """Per-call memo of selector resolutions (one list resolves each distinct selector once)."""

    def __init__(self, db):
        self.db, self._seen = db, {}

    def resolve(self, selector: str) -> Optional[str]:
        if selector not in self._seen:
            from gateway.session_selectors import resolve_session_selector
            try:
                self._seen[selector] = resolve_session_selector(self.db, selector)
            except Exception:  # health: allow BLE001 -- an unresolvable selector names no one (fails closed)
                logger.warning("session selector %r could not be resolved", selector, exc_info=True)
                self._seen[selector] = None
        return self._seen[selector]


def _target_held_by(row, target: Any, caller_lineage: set, selectors: Optional[_Selectors]) -> bool:
    """True when ``kind=session`` *target* (of *row*) names the caller's conversation now: a literal id in it,
    or a selector it currently holds (unresolvable: the conversation the row was last bound to)."""
    if not (isinstance(target, dict) and target.get("kind") == "session"):
        return False
    if target.get("session_id"):
        return target["session_id"] in caller_lineage
    if not target.get("selector") or selectors is None:
        return False
    resolved = selectors.resolve(str(target["selector"]))
    if resolved is not None:
        return resolved in caller_lineage
    return _column(row, "attached_session_id") in caller_lineage


def attached_to(row, caller_lineage: set, selectors: Optional[_Selectors] = None) -> bool:
    """The opt-in rule: *row*'s ``delivery_target`` is an ``attach: true`` session target the caller's
    conversation holds (see :func:`_target_held_by`)."""
    target = json_field(row["delivery_target"])
    return isinstance(target, dict) and target.get("attach") is True and _target_held_by(
        row, target, caller_lineage, selectors)


def _selector_split(db, conn, lineage_ids: set, selectors: _Selectors, legacy_probe: bool = False
                    ) -> tuple[bool, list, list]:
    """``(legacy table present, selectors resolving into the caller, unresolvable selectors)``: one statement
    (an index walk over the selector expression index; each distinct selector resolves once)."""
    sel = target_sql("", "$.selector")
    rows = conn.execute(
        "SELECT 'legacy', NULL FROM sqlite_master WHERE type = 'table' AND name = 'delegate_sessions' "
        f"UNION ALL SELECT 'selector', v FROM (SELECT DISTINCT {sel} AS v FROM sessions "
        f"WHERE delivery_target IS NOT NULL AND {sel} IS NOT NULL)").fetchall()
    legacy = any(r[0] == "legacy" for r in rows)
    held, unres = [], []
    for kind, value in rows:
        if kind != "selector" or value in ("", 0):
            continue
        resolved = selectors.resolve(str(value))
        if resolved is None:
            unres.append(value)
        elif resolved in lineage_ids:
            held.append(value)
    return legacy, held, unres


# ── ACL (per profile database; generation leases) ───────────────────────────

def _targets(row, caller_lineage: set, selectors: Optional[_Selectors] = None) -> bool:
    return _target_held_by(row, json_field(row["delivery_target"]), caller_lineage, selectors)


def default_session_acl(db, conn, caller_session_id: str, target_row) -> bool:
    """The default policy (plan §4.5): the caller may reach its descendants (any depth, through compression
    continuations of every ancestor) and any session whose ``delivery_target`` targets the caller's
    conversation (how the main agent resumes webhook sessions delivering to it), literally or through a
    selector it holds now."""
    if target_row is None or not caller_session_id:
        return False
    mine = set(lineage(db, conn, caller_session_id))
    if target_row["id"] in mine:
        return False  # one's own lineage is not a child
    if _targets(target_row, mine, _Selectors(db)):
        return True
    current = target_row
    for _ in range(_MAX_TREE_DEPTH):
        parent = current["parent_session_id"]
        if not parent:
            return False
        if parent in mine:
            return _effective_kind(current) not in (None, "continuation", "branch", "reset", "switch")
        current = conn.execute("SELECT * FROM sessions WHERE id = ?", (logical_root(db, conn, parent),)).fetchone()
        if current is None:
            return False
    return False


def _delegate_root(db, conn, session_id: str):
    """The first segment row of *session_id*'s conversation (a delegate child's own row), or None."""
    if not session_id:
        return None
    return conn.execute("SELECT * FROM sessions WHERE id = ?", (logical_root(db, conn, session_id),)).fetchone()


def are_siblings(db, conn, caller_session_id: str, target_row) -> bool:
    """Both sessions are delegate children of one parent conversation (and not the same conversation)."""
    if target_row is None or not caller_session_id:
        return False
    own = _delegate_root(db, conn, caller_session_id)
    other = _delegate_root(db, conn, target_row["id"])
    if own is None or other is None or own["id"] == other["id"]:
        return False
    if _effective_kind(own) != "delegate" or _effective_kind(other) != "delegate":
        return False
    if not own["parent_session_id"] or not other["parent_session_id"]:
        return False
    return other["parent_session_id"] in set(lineage(db, conn, own["parent_session_id"]))


def default_peer_acl(db, conn, caller_session_id: str, target_row) -> bool:
    """Who a ``sender_kind="peer"`` admission may reach by default (plan §4.4): siblings only."""
    return are_siblings(db, conn, caller_session_id, target_row)


ACL_ACTIONS = ("observe", "peer")
_DEFAULT_ACLS = {"observe": default_session_acl, "peer": default_peer_acl}


def _check_action(action: str) -> str:
    if action not in _DEFAULT_ACLS:
        raise ValueError(f"action must be one of {', '.join(ACL_ACTIONS)}")
    return action


@lru_cache(maxsize=256)
def _resolved(path: str) -> str:
    try:
        return str(Path(path).expanduser().resolve())
    except (OSError, RuntimeError):
        return str(path)


def acl_scope(target: Any = None) -> str:
    """The ACL scope key of *target*: a SessionDB (or a wrapper holding one as ``_db``), a database path, or
    None for the current profile's ``state.db``. Two handles on one database share one scope."""
    if target is None:
        from hermes_state import _default_db_path
        path: Any = _default_db_path()
    elif isinstance(target, (str, os.PathLike)):
        path = target
    else:
        path = getattr(getattr(target, "_db", None) or target, "db_path", None)
        if not isinstance(path, (str, os.PathLike)):
            return ""
    return _resolved(os.fspath(path))


class AclLease:
    """One installed policy generation in one ``(scope, action)`` slot; :meth:`dispose` retires exactly it."""

    __slots__ = ("scope", "action", "policy", "generation", "owner", "active", "_registry")

    def __init__(self, registry, scope: str, action: str, policy: Callable[..., bool], generation: int,
                 owner: Optional[str]):
        self._registry, self.scope, self.action, self.policy = registry, scope, action, policy
        self.generation, self.owner, self.active = generation, owner, True

    def dispose(self) -> None:
        self._registry.dispose(self)

    def __repr__(self) -> str:
        return f"AclLease({self.action}@{self.scope!r} gen={self.generation} owner={self.owner!r} active={self.active})"


class _AclRegistry:
    """Per-scope ACL state: a base policy plus a stack of generation leases. The effective policy is the
    newest live lease, else the base, else the default; a disposed lease simply leaves the stack, so no
    unload order can bring back a policy whose owner is gone (review R7)."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._base: dict[tuple[str, str], Callable[..., bool]] = {}
        self._leases: dict[tuple[str, str], list[AclLease]] = {}
        self._generations = itertools.count(1)

    def effective(self, scope: str, action: str) -> Callable[..., bool]:
        with self._lock:
            leases = self._leases.get((scope, action))
            if leases:
                return leases[-1].policy
            return self._base.get((scope, action)) or _DEFAULT_ACLS[action]

    def set_base(self, scope: str, action: str, policy: Optional[Callable[..., bool]]) -> Callable[..., bool]:
        with self._lock:
            previous = self._base.get((scope, action)) or _DEFAULT_ACLS[action]
            if policy is None or policy is _DEFAULT_ACLS[action]:
                self._base.pop((scope, action), None)
            else:
                self._base[(scope, action)] = policy
            return previous

    def install(self, scope: str, action: str, policy: Optional[Callable[..., bool]],
                owner: Optional[str] = None) -> AclLease:
        with self._lock:
            lease = AclLease(self, scope, action, policy or _DEFAULT_ACLS[action], next(self._generations), owner)
            self._leases.setdefault((scope, action), []).append(lease)
            return lease

    def dispose(self, lease: AclLease) -> None:
        with self._lock:
            if not lease.active:
                return
            lease.active = False
            slot = (lease.scope, lease.action)
            live = [item for item in self._leases.get(slot, []) if item is not lease]
            if live:
                self._leases[slot] = live
            else:
                self._leases.pop(slot, None)

    def leases(self, scope: str, action: str) -> list[AclLease]:
        with self._lock:
            return list(self._leases.get((scope, action), []))


_registry = _AclRegistry()


def set_session_acl(policy: Optional[Callable[..., bool]], *, action: str = "observe", scope: Any = None
                    ) -> Callable[..., bool]:
    """Install ``policy(db, conn, caller_session_id, target_row) -> bool`` as the BASE policy of one profile
    database (*scope*: a SessionDB, a db path, or None for the current profile's ``state.db``; None policy
    restores the default) for *action*: ``"observe"`` (observe / list / resume / steer / close reach) or
    ``"peer"`` (peer admissions). Returns the previous base. Plugin leases (:func:`install_session_acl`)
    stack above the base."""
    return _registry.set_base(acl_scope(scope), _check_action(action), policy)


def install_session_acl(policy: Optional[Callable[..., bool]], *, action: str = "observe", scope: Any = None,
                        owner: Optional[str] = None) -> AclLease:
    """Stack *policy* for *action* on *scope* as a new generation lease (newest live lease wins); dispose
    the returned lease to retire exactly this generation."""
    return _registry.install(acl_scope(scope), _check_action(action), policy, owner)


def get_session_acl(action: str = "observe", *, scope: Any = None) -> Callable[..., bool]:
    """The effective policy for *action* on *scope* (see :func:`set_session_acl`)."""
    return _registry.effective(acl_scope(scope), _check_action(action))


def allowed(db, conn, caller_session_id: str, target_row, *, action: str = "observe") -> bool:
    policy = get_session_acl(action, scope=db)
    try:
        return bool(policy(db, conn, caller_session_id, target_row))
    except Exception:  # health: allow BLE001 -- plugin policy boundary: a broken ACL fails closed, logged
        logger.warning("session ACL policy %r failed; access denied", policy, exc_info=True)
        return False


# ── listing ──────────────────────────────────────────────────────────────────

def _listed_kind(conn, row) -> Optional[str]:
    """The kind a row lists under: its stamped ``lineage_kind``, or ``delegate`` for a pre-unified retained child
    (a legacy ``delegate_sessions`` row). A bare ``_delegate_from`` marker is a child still being built or a
    non-retained run (stamped when it ends): the live registry lists those, not the table."""
    kind = row["lineage_kind"]
    if kind:
        return kind
    return "delegate" if _legacy_delegate(conn, row["id"]) is not None else None


def _is_default_child(row, caller_lineage: set, conn=None, selectors: Optional[_Selectors] = None) -> Optional[str]:
    """``"own"`` for a delegate spawn of the caller, ``"attached"`` for an explicit opt-in, else None."""
    kind = _listed_kind(conn, row) if conn is not None else _effective_kind(row)
    if kind == "delegate" and row["parent_session_id"] in caller_lineage:
        return "own"
    if kind not in _EXCLUDED_KINDS and kind != "delegate" and attached_to(row, caller_lineage, selectors):
        return "attached"
    return None


def _entry(info: dict, how: Optional[str]) -> dict:
    if how == "attached" or info["lineage_kind"] not in ("delegate", None):
        info["badge"] = info["lineage_kind"]
    return info


def _transcript_hits(conn, records: list[dict], segments: dict[str, list[str]], needle: str) -> set[str]:
    """The *records* whose own transcript (any segment) contains *needle*: one statement per page."""
    pairs = [[info["session_id"], sid] for info in records for sid in segments.get(info["session_id"], [])]
    if not pairs:
        return set()
    return {row[0] for row in conn.execute(
        "SELECT DISTINCT json_extract(p.value, '$[0]') FROM json_each(?) p WHERE EXISTS (SELECT 1 FROM messages m "
        "WHERE m.session_id = json_extract(p.value, '$[1]') AND instr(lower(m.content), ?) > 0)",
        (json.dumps(pairs), needle))}


def _matches_fields(info: dict, needle: str) -> bool:
    return any(needle in str(info.get(key) or "").casefold() for key in ("session_id", "title", "assignment", "initial_goal"))


def _ordered_page(conn, *, cand_sql: str, filter_sql: str, legacy: bool, selectors: bool, params: dict,
                  rollup: bool = True) -> list[Any]:
    """One statement: candidates -> semantic filter -> activity order -> LIMIT/OFFSET -> per-row hydration."""
    return conn.execute(
        f"WITH RECURSIVE cand(id) AS ({cand_sql}), "
        f"pick AS (SELECT {_row_cols(selectors)} FROM cand JOIN sessions s ON s.id = cand.id{_legacy_join(legacy)}), "
        f"picked AS (SELECT * FROM pick WHERE {filter_sql}), {_seg_cte('picked')}, "
        f"ordered AS (SELECT picked.*, {_activity_sql('picked')} AS _act FROM picked), "
        f"page AS (SELECT * FROM ordered ORDER BY MAX(_floor, COALESCE(_act, _floor)) DESC, _created DESC, id DESC "
        f"LIMIT :page OFFSET :offset) "
        f"SELECT page.*, {_hydration_sql('page', rollup=rollup, legacy=legacy)} FROM page "
        f"ORDER BY MAX(_floor, COALESCE(_act, _floor)) DESC, _created DESC, id DESC", params).fetchall()


def list_sessions(db, conn, caller_session_id: str, *, scope: str = "children", query: Optional[str] = None,
                  limit: int = 20, include_closed: bool = False) -> list[dict]:
    """The caller's session list under Freya's rule. Recent running/idle rows first; a ``query`` searches
    closed ones too (title, assignment and the rows' own transcripts, never anyone else's)."""
    if scope not in LIST_SCOPES:
        raise ValueError(f"scope must be one of {', '.join(LIST_SCOPES)}")
    limit = max(1, min(int(limit or 20), 100))
    needle = str(query or "").strip().casefold()
    mine = lineage(db, conn, caller_session_id)
    mine_set = set(mine)
    if scope == "tree":
        return _list_tree(db, conn, caller_session_id, mine, needle=needle, limit=limit, include_closed=include_closed)
    policy = get_session_acl("observe", scope=db)
    if scope == "children":
        legacy, held, unres = _selector_split(db, conn, mine_set, _Selectors(db))
        sid, sel = target_sql("", "$.session_id"), target_sql("", "$.selector")
        lin = "(SELECT value FROM json_each(:lin))"
        cand = (f"SELECT id FROM sessions WHERE parent_session_id IN {lin} "
                f"UNION SELECT id FROM sessions WHERE delivery_target IS NOT NULL AND {sid} IN {lin} "
                f"UNION SELECT id FROM sessions WHERE attached_session_id IN {lin}")
        if held:
            cand += (f" UNION SELECT id FROM sessions WHERE delivery_target IS NOT NULL AND {sel} IS NOT NULL "
                     f"AND {sel} IN (SELECT value FROM json_each(:held))")
        filter_sql = f"_how IS NOT NULL AND id NOT IN {lin}"
        params = _params(mine, held, unres)
        # The default ACL admits every row the rule picks (an own delegate spawn or a target the caller holds);
        # only a custom policy or a legacy shim row (no lineage_kind) needs the per-row policy call.
        def acl_needed(row) -> bool:
            return policy is not default_session_acl or not row["lineage_kind"]
        selectors = True
    elif scope == "siblings":
        own = conn.execute("SELECT * FROM sessions WHERE id = ?", (logical_root(db, conn, caller_session_id),)).fetchone()
        parent = own["parent_session_id"] if own is not None else None
        if not parent or _effective_kind(own) != "delegate":
            return []
        parents = lineage(db, conn, parent)
        legacy = _legacy_table(conn)
        cand = "SELECT id FROM sessions WHERE parent_session_id IN (SELECT value FROM json_each(:parents))"
        filter_sql = "_kind = 'delegate' AND id NOT IN (SELECT value FROM json_each(:lin))"
        params = _params(mine, parents=json.dumps(parents))
        def acl_needed(row) -> bool:
            return False  # a sibling list is the peer roster: the observe ACL does not gate it
        selectors = False
    if not needle and not include_closed:
        filter_sql += " AND _status != 'closed'"
    out: list[dict] = []
    page_size = limit if not needle else max(limit, 20)
    offset = 0
    while len(out) < limit:
        rows = _ordered_page(conn, cand_sql=cand, filter_sql=filter_sql, legacy=legacy, selectors=selectors,
                             params={**params, "page": page_size, "offset": offset})
        kept = [row for row in rows if not acl_needed(row) or allowed(db, conn, caller_session_id, row)]
        shims = _legacy_rows(conn, kept, legacy) if legacy else {}
        infos = []
        for row in kept:
            info = _entry(_build_record(row, shims.get(row["id"])), row["_how"] if scope == "children" else None)
            info["running_children"] = int(row["_running_children"] or 0)
            info["children_total"] = int(row["_children_total"] or 0)
            infos.append((info, _segments_from(row["_segs"], row["id"])))
        if needle:
            unmatched = [info for info, _ in infos if not _matches_fields(info, needle)]
            hits = _transcript_hits(conn, unmatched, {info["session_id"]: segs for info, segs in infos}, needle)
            infos = [(info, segs) for info, segs in infos if _matches_fields(info, needle) or info["session_id"] in hits]
        out.extend(info for info, _ in infos)
        if len(rows) < page_size:
            break
        offset += page_size
        page_size = max(page_size, limit * 2)
    return out[:limit]


def _list_tree(db, conn, caller_session_id: str, mine: list[str], *, needle: str, limit: int,
               include_closed: bool) -> list[dict]:
    picked = _subtree(db, conn, mine)
    policy = get_session_acl("observe", scope=db)
    kept = [(row, level) for row, level in picked if allowed(db, conn, caller_session_id, row)] \
        if policy is not default_session_acl else _default_tree_reach(db, conn, mine, picked)
    levels = {row["id"]: level for row, level in kept}
    infos = [_entry(info, None) for info in hydrate(conn, [row for row, _ in kept], rollup=True)]
    out = []
    for info in infos:
        info["depth_below_caller"] = levels[info["session_id"]]
        if needle:
            if not _matches_fields(info, needle) and not _transcript_hits(
                    conn, [info], {info["session_id"]: chain(conn, info["session_id"])}, needle):
                continue
        elif info["status"] == "closed" and not include_closed:
            continue
        out.append(info)
    out.sort(key=lambda r: (r["last_activity_at"], r["created_at"], r["session_id"]), reverse=True)
    return out[:limit]


def _default_tree_reach(db, conn, mine: list[str], picked: list[tuple]) -> list[tuple]:
    """:func:`default_session_acl` over a whole subtree with the caller's lineage resolved once (the per-row
    walk stays exact; its lookups are memoized per conversation)."""
    mine_set, selectors = set(mine), _Selectors(db)
    roots: dict[str, Any] = {}

    def conversation(parent: str):
        if parent not in roots:
            roots[parent] = conn.execute("SELECT * FROM sessions WHERE id = ?",
                                         (logical_root(db, conn, parent),)).fetchone()
        return roots[parent]

    out = []
    for row, level in picked:
        if row["id"] in mine_set:
            continue
        if _targets(row, mine_set, selectors):
            out.append((row, level))
            continue
        current = row
        for _ in range(_MAX_TREE_DEPTH):
            parent = current["parent_session_id"]
            if not parent:
                break
            if parent in mine_set:
                if _effective_kind(current) not in (None, "continuation", "branch", "reset", "switch"):
                    out.append((row, level))
                break
            current = conversation(parent)
            if current is None:
                break
    return out


def _subtree(db, conn, mine: list[str]) -> list[tuple]:
    """Every delegate/attached descendant of the caller's conversation, breadth-first, bounded: one
    statement per level (its rows' compression segments included). The candidate set is pre-filtered on
    the indexed ``root_session_id``; ``parent_session_id`` decides."""
    frontier, out, seen = list(mine), [], set(mine)
    root = conn.execute("SELECT root_session_id FROM sessions WHERE id = ?", (mine[0],)).fetchone()
    root_id = root[0] if root is not None else None
    legacy = _legacy_table(conn)
    for level in range(1, _MAX_TREE_DEPTH + 1):
        if not frontier or len(out) >= _MAX_TREE_ROWS:
            break
        root_filter = " AND (s.root_session_id = :root OR s.root_session_id IS NULL)" if root_id else ""
        rows = conn.execute(
            f"WITH RECURSIVE picked AS (SELECT {_row_cols(False)} FROM sessions s{_legacy_join(legacy)} "
            f"WHERE s.parent_session_id IN (SELECT value FROM json_each(:lin)){root_filter}), {_seg_cte('picked')} "
            f"SELECT picked.*, (SELECT json_group_array(json_array(seg.n, seg.id)) FROM seg WHERE seg.start = picked.id "
            f"AND seg.id IS NOT NULL) AS _segs FROM picked",
            _params(frontier, root=root_id)).fetchall()
        nxt = []
        for row in rows:
            if row["id"] in seen or row["_how"] is None:
                continue
            seen.add(row["id"])
            out.append((row, level))
            nxt.extend(_segments_from(row["_segs"], row["id"]))
        frontier = nxt
    return out


def get_record(db, conn, caller_session_id: str, session_id: str) -> dict:
    """One session the caller may reach (ACL), or LookupError without disclosing whose it is."""
    row = conn.execute("SELECT * FROM sessions WHERE id = ?", (session_id,)).fetchone()
    if row is None or not allowed(db, conn, caller_session_id, row):
        raise LookupError("No session with that session_id is reachable from this conversation.")
    return record(conn, row)


def _latest_message(db, conn, session_id: str) -> Optional[dict]:
    tip = chain(conn, session_id)[-1]
    rows = db.get_messages(tip, limit=1, latest=True)
    return rows[-1] if rows else None


def _children_preview(db, conn, ids: list[str], max_children: int, *, legacy: bool) -> tuple[list[dict], int]:
    """The observed conversation's default children (``parent_session_id`` in *ids*): the newest
    *max_children* in roll-up order, selected in SQL before hydration, each with its latest step's message
    (head-bounded); and how many there are. One statement (plus fallbacks only for odd rows)."""
    order = "(_status IS NOT 'running'), MAX(_floor, COALESCE(_act, _floor)) DESC, id ASC"
    rows = conn.execute(
        f"WITH RECURSIVE picked AS (SELECT {_row_cols(False)} FROM sessions s{_legacy_join(legacy)} "
        f"WHERE s.parent_session_id IN (SELECT value FROM json_each(:lin))), "
        f"kept AS (SELECT * FROM picked WHERE _how IS NOT NULL), {_seg_cte('kept')}, "
        f"acted AS (SELECT kept.*, {_activity_sql('kept')} AS _act, COUNT(*) OVER () AS _total FROM kept), "
        f"page AS (SELECT * FROM acted ORDER BY {order} LIMIT :max), "
        f"hydrated AS (SELECT page.*, {_hydration_sql('page', rollup=False, legacy=legacy)}, "
        f"(SELECT seg.id FROM seg WHERE seg.start = page.id AND seg.id IS NOT NULL ORDER BY seg.n DESC LIMIT 1) AS _tip "
        f"FROM page) "
        f"SELECT hydrated.*, lm.id AS _lm_id, lm.role AS _lm_role, lm.tool_calls AS _lm_tool_calls, "
        f"lm.tool_name AS _lm_tool_name, lm.part_kind AS _lm_part_kind, lm.parts_json AS _lm_parts_json, "
        f"length(lm.content) AS _lm_chars, CASE WHEN length(lm.content) > :cap AND substr(lm.content, 1, 6) != :prefix "
        f"THEN substr(lm.content, 1, :cap) ELSE lm.content END AS _lm_content FROM hydrated "
        f"LEFT JOIN messages lm ON lm.id = (SELECT x.id FROM messages x WHERE x.session_id = hydrated._tip "
        f"AND x.active = 1 ORDER BY x.id DESC LIMIT 1) ORDER BY (hydrated._status IS NOT 'running'), "
        f"MAX(hydrated._floor, COALESCE(hydrated._act, hydrated._floor)) DESC, hydrated.id ASC",
        _params(ids, max=max_children, cap=_LATEST_STEP_CHARS, prefix=_CONTENT_JSON_PREFIX)).fetchall()
    shims = _legacy_rows(conn, rows, legacy) if legacy else {}
    total = int(rows[0]["_total"]) if rows else 0
    out = []
    for row in rows:
        info = _build_record(row, shims.get(row["id"]))
        if info["current_session_id"] != row["_tip"]:  # a looping chain: the walk's tip, read the plain way
            info["_latest_message"] = _latest_message(db, conn, row["id"])
        else:
            info["_latest_message"] = _latest_row(db, conn, row)
        out.append(info)
    return out, total


def _latest_row(db, conn, row) -> Optional[dict]:
    if row["_lm_id"] is None:
        return None
    content = row["_lm_content"]
    if row["_lm_chars"] is not None and content is not None and row["_lm_chars"] > len(content):
        from agent.session_views import CHILD_LATEST_CAP, one_line
        if len(one_line(content)) <= CHILD_LATEST_CAP * 2:  # whitespace-heavy head: the clip needs the body
            content = conn.execute("SELECT content FROM messages WHERE id = ?", (row["_lm_id"],)).fetchone()[0]
    calls = json_field(row["_lm_tool_calls"]) if row["_lm_tool_calls"] else None
    return {"role": row["_lm_role"], "content": db._decode_content(content),
            "tool_calls": calls if isinstance(calls, list) else ([] if row["_lm_tool_calls"] else None),
            "tool_name": row["_lm_tool_name"], "part_kind": row["_lm_part_kind"], "parts_json": row["_lm_parts_json"]}


def observe(db, conn, caller_session_id: str, session_id: str, *, limit: Optional[int] = None,
            max_chars: Optional[int] = None) -> dict:
    """A bounded, low-noise read of one reachable session (any descendant of the caller, read-only): its
    header, the NEWEST *limit* transcript messages in chronological order (each capped, never reasoning,
    whole tool arguments or raw parts) and a compact roll-up of its own children (d+1). Observation never
    counts as activity. ``max_chars`` is accepted for compatibility; the per-field caps bound the size.

    Bounded work (review R9): the tail is a lineage-aware ``ORDER BY id DESC LIMIT`` window
    (``SessionDB.get_lineage_tail_preview``: deduplicated exactly like the full read, body previews read
    head/tail-bounded); the roll-up selects its at most ``CHILDREN_MAX`` children in SQL before hydrating
    them, each latest step one ``DESC LIMIT 1`` lookup in the same statement."""
    from agent import session_views as views
    row = conn.execute(
        "SELECT s.*, EXISTS (SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'delegate_sessions') "
        "AS _legacy_present FROM sessions s WHERE s.id = ?", (session_id,)).fetchone()
    if row is None or not allowed(db, conn, caller_session_id, row):
        raise LookupError("No session with that session_id is reachable from this conversation.")
    legacy = bool(row["_legacy_present"])
    info, ids = _hydrate_rows(conn, [row], legacy=legacy)[0]
    limit = views.observe_limit(limit)
    messages = db.get_lineage_tail_preview(info["current_session_id"], limit + 1)
    truncated = len(messages) > limit
    tail = [views.message_view(m) for m in messages[-limit:]]
    subs, total = _children_preview(db, conn, ids, views.CHILDREN_MAX, legacy=legacy)
    rollup = views.children_rollup(subs, lambda sub: views.latest_step(sub.get("_latest_message")),
                                   inherit_model=info.get("model"), total=total)
    return {"session": views.session_brief(info), "messages": tail, "truncated": truncated, **rollup}
