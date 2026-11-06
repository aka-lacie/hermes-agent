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
activity ordering and the page bound run in SQL over indexed arms; only the returned rows are hydrated
(compression segments, latest message timestamp per segment, running/queued admissions through
``session_admissions_pending``, the latest parent assignment through ``(target_session_id, seq)`` DESC
LIMIT 1, grandchild roll-up counts). Nothing reads a transcript or an admission history in Python, and the
statement count of a list does not depend on how many children, grandchildren or messages exist. The
ENGINE work is bounded too (re-review R8): each arm walks a trigger-maintained recency index
(``sessions.conversation_activity_at``, an upper bound of the exact order key) a window at a time and stops
once the page is settled, so ``list(limit)`` and ``scope="tree"`` scan O(limit) rows however many
candidates exist (see "bounded recency scans" below).

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


def _hydration_sql(o: str, *, rollup: bool, legacy: bool, when: Optional[str] = None) -> str:
    """Per-row record columns for an already-selected row *o* (never evaluated before ``LIMIT``). *when*: an
    SQL condition gating every column but ``_segs`` (a scan round hydrates only its top rows)."""
    payload = "(CASE WHEN json_valid(a.payload_json) THEN a.payload_json ELSE '{}' END)"

    def gated(expr: str, name: str) -> str:
        return f"(CASE WHEN {when} THEN {expr} END) AS {name}" if when else f"{expr} AS {name}"

    cols = [
        f"(SELECT json_group_array(json_array(seg.n, seg.id)) FROM seg WHERE seg.start = {o}.id "
        f"AND seg.id IS NOT NULL) AS _segs",
        gated(f"(SELECT COUNT(*) FROM session_admissions a WHERE a.target_session_id = {o}.id "
              f"AND a.status = 'queued')", "_queued"),
        gated(f"(SELECT json_extract({payload}, '$.text') FROM session_admissions a WHERE a.target_session_id = "
              f"{o}.id AND substr(a.principal_id, 1, 7) = 'parent:' AND json_type({payload}, '$.text') = 'text' "
              f"ORDER BY a.seq DESC LIMIT 1)", "_latest"),
    ]
    if rollup:
        # COUNT(DISTINCT): a looping chain repeats segments; seg(start) and idx_sessions_parent drive the join.
        children = (f"FROM seg JOIN sessions c ON c.parent_session_id = seg.id{_legacy_join(legacy, 'c', 'dc')} "
                    f"WHERE seg.start = {o}.id AND seg.id IS NOT NULL AND {_kind_sql('c.', 'dc.')} = 'delegate'")
        cols += [
            gated(f"(SELECT COUNT(DISTINCT c.id) {children})", "_children_total"),
            gated(f"(SELECT COUNT(DISTINCT c.id) {children} AND c.status IS NOT 'closed' AND EXISTS (SELECT 1 FROM "
                  f"session_admissions a WHERE a.target_session_id = c.id AND a.status IN ('started', 'unknown', "
                  f"'queued')))", "_running_children"),
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
        "initial_goal": initial,
        # A migrated retained child's brief carries its last legacy assignment (review R1) until a new
        # parent admission supersedes it.
        "assignment": latest or brief.get("assignment") or (legacy or {}).get("assignment") or initial,
        "context": brief.get("context") or (legacy or {}).get("context") or "",
        "preload_skills": list(brief.get("preload_skills") or []),
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


_SEG_ENDS_SQL = ("(SELECT json_group_object(x.id, x.end_reason) FROM sessions x WHERE x.id IN (SELECT seg.id FROM seg "
                 "WHERE seg.start = {o}.id AND seg.id IS NOT NULL)) AS _seg_ends")


def _hydrate_rows(conn, rows: Iterable[Any], *, rollup: bool = False, legacy: Optional[bool] = None,
                  ends: bool = False) -> list[tuple[dict, list[str], dict]]:
    """``(record, compression segments, {segment: end_reason})`` of many rows from ONE statement (plus one
    legacy read only for legacy rows), in input order. *legacy*: whether ``delegate_sessions`` exists (None:
    probe when needed); *ends*: also read each segment's ``end_reason`` (else the mapping is empty)."""
    rows = list(rows)
    if not rows:
        return []
    ids = [r["id"] for r in rows]
    if not any(not r["lineage_kind"] for r in rows):
        legacy = False
    elif legacy is None:
        legacy = _legacy_table(conn)
    extra = (", " + _SEG_ENDS_SQL.format(o="s")) if ends else ""
    got = {r["id"]: r for r in conn.execute(
        f"WITH RECURSIVE base(id) AS (SELECT value FROM json_each(:ids)), {_seg_cte('base')} "
        f"SELECT {_row_cols(False)}, {_activity_sql('s')} AS _act, {_hydration_sql('s', rollup=rollup, legacy=legacy)}"
        f"{extra} "
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
            seg_ends = (json_field(got[sid]["_seg_ends"]) or {}) if ends else {}
            out.append((info, _segments_from(got[sid]["_segs"], sid), seg_ends))
    return out


def hydrate(conn, rows: Iterable[Any], *, rollup: bool = False) -> list[dict]:
    """:func:`record` of many rows in ONE statement (plus one legacy read only for legacy rows), input order."""
    return [info for info, _, _ in _hydrate_rows(conn, rows, rollup=rollup)]


def _legacy_hint(row) -> Optional[bool]:
    """A row read with ``_legacy_present`` (observe's / the store's reach read) already knows."""
    try:
        return bool(row["_legacy_present"])
    except (KeyError, IndexError):
        return None


def record(conn, row) -> dict:
    """The public record of one session row (credential-free; never transcript bodies)."""
    found = hydrate(conn, [row])
    if not found:
        raise LookupError("No session with that session_id.")
    return found[0]


def record_and_tip(conn, row) -> tuple[dict, Optional[str]]:
    """:func:`record` plus the ``end_reason`` of its execution tip (``current_session_id``), in the same ONE
    statement: ``'compression'`` there means the continuation is gone."""
    found = _hydrate_rows(conn, [row], legacy=_legacy_hint(row), ends=True)
    if not found:
        raise LookupError("No session with that session_id.")
    info, _, seg_ends = found[0]
    return info, seg_ends.get(info["current_session_id"])


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

    __slots__ = ("_registry", "action", "active", "generation", "owner", "policy", "scope")

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


# ── bounded recency scans (review R8: O(limit) engine work, not only O(limit) statements) ──────────────
#
# Every list arm walks one recency index (hermes_state_common: idx_sessions_*_recency) in
# (conversation_activity_at, started_at, id) DESC order, a window of rows at a time (keyset paginated).
# ``conversation_activity_at`` is a trigger-maintained UPPER BOUND of a row's exact order key
# MAX(_floor, activity of every compression segment) and its tie-breakers are the key's own
# (``started_at`` is ``_created`` for every unified row), so once the page's limit-th exact key reaches the
# next unscanned row's bound no unscanned row can enter the page: the scan stops after O(limit) index rows
# in the normal (bound == key) case, whatever the number of candidates. The exact key, Freya's semantic
# filter, the closed filter, the ACL and the query are still decided per scanned row, so the result is the
# head of the same total order the unbounded list had.

_SCAN_WINDOW_MAX = 512
_MATERIALIZED = "MATERIALIZED " if sqlite3.sqlite_version_info >= (3, 35, 0) else ""
_KEY_SQL = "MAX(_floor, COALESCE(_act, _floor))"


def _recency_arm(where: str, closed: Any, after: bool) -> str:
    """One arm's next ``:w`` rows in bound order. *closed*: the ``list_closed`` partition (0 / 1 / None for
    both), or ``"running"``: the flagged rows a running admission overrides (``_status_sql``: running wins
    over "ended"), read admission-first through idx_session_admissions_running -- one row per in-flight run."""
    from hermes_state_common import LIST_RECENCY_ORDER_SQL, LIST_RUNNING_ADMISSION_SQL
    keyset = " AND (conversation_activity_at, started_at, id) < (:cm, :cs, :cid)" if after else ""
    if closed == "running":
        return (f"SELECT * FROM (SELECT id, conversation_activity_at AS _m, started_at AS _st FROM "
                f"(SELECT DISTINCT target_session_id FROM session_admissions WHERE {LIST_RUNNING_ADMISSION_SQL}) ra "
                f"CROSS JOIN sessions ON sessions.id = ra.target_session_id "
                f"WHERE {where} AND list_closed = 1{keyset} ORDER BY {LIST_RECENCY_ORDER_SQL} LIMIT :w)")
    flag = f" AND list_closed = {int(closed)}" if closed is not None else ""
    return (f"SELECT * FROM (SELECT id, conversation_activity_at AS _m, started_at AS _st FROM sessions "
            f"WHERE {where}{flag}{keyset} ORDER BY {LIST_RECENCY_ORDER_SQL} LIMIT :w)")


def _scan_sql(wheres: list[str], flags: tuple, *, after: bool, selectors: bool, legacy: bool, filter_sql: str,
              hydrate: Optional[str]) -> str:
    """One scan round: the next ``:w`` candidates in bound order over every arm, the window's last bound
    (``_ncand`` / ``_last_*``: one meta row even when nothing passes), and the rows passing *filter_sql*
    with their exact key (``_key``) and rank in the window (``_rn``); *hydrate*: extra columns over
    ``ranked`` (the record columns of the window's top rows)."""
    arms = " UNION ALL ".join(_recency_arm(w, f, after) for w in wheres for f in flags)
    extra = f", {hydrate}" if hydrate else ""
    return (
        f"WITH RECURSIVE arm(id, _m, _st) AS ({arms}), "
        f"cand AS {_MATERIALIZED}(SELECT DISTINCT id, _m, _st FROM arm ORDER BY _m DESC, _st DESC, id DESC LIMIT :w), "
        f"meta AS (SELECT COUNT(*) OVER () AS _ncand, _m AS _last_m, _st AS _last_st, id AS _last_id FROM cand "
        f"ORDER BY _m ASC, _st ASC, id ASC LIMIT 1), "
        f"pick AS (SELECT {_row_cols(selectors)} FROM cand JOIN sessions s ON s.id = cand.id{_legacy_join(legacy)}), "
        f"picked AS (SELECT * FROM pick WHERE {filter_sql}), {_seg_cte('picked')}, "
        f"keyed AS (SELECT picked.*, {_activity_sql('picked')} AS _act FROM picked), "
        f"ranked AS (SELECT keyed.*, {_KEY_SQL} AS _key, ROW_NUMBER() OVER (ORDER BY {_KEY_SQL} DESC, _created DESC, "
        f"id DESC) AS _rn FROM keyed) "
        f"SELECT meta._ncand, meta._last_m, meta._last_st, meta._last_id, ranked.*{extra} FROM meta LEFT JOIN ranked ON 1")


def _ord(value: Any) -> tuple:
    """SQLite's cross-type order (NULL < numbers < text < blob) as a Python sort key."""
    if value is None:
        return (0, 0)
    if isinstance(value, (int, float)):
        return (1, value)
    if isinstance(value, str):
        return (2, value)
    return (3, bytes(value))


def _exact_key(row) -> tuple:
    return (_ord(row["_key"]), _ord(row["_created"]), _ord(row["id"]))


def _recency_scan(conn, *, sql_for: Callable[[bool], str], params: dict, limit: int, window: int,
                  accept: Callable[[list], list]) -> list[tuple]:
    """Keyset-paginated scan rounds until the top *limit* accepted ``(exact key, item)`` are settled: the
    arms are exhausted, or the limit-th accepted key is >= the bound of every unscanned row (the last
    scanned bound: the index order is strict). ``accept(rows)`` -> ``[(exact key, item)]`` for one round's
    passing rows (the ACL / query decide there). Returns the head, best first."""
    accepted: list[tuple] = []
    cursor: Optional[tuple] = None
    rounds = 0
    while True:
        rounds += 1
        bind = {**params, "w": window}
        if cursor is not None:
            bind.update(cm=cursor[0], cs=cursor[1], cid=cursor[2])
        rows = conn.execute(sql_for(cursor is not None), bind).fetchall()
        if not rows:
            break
        meta = rows[0]
        accepted.extend(accept([row for row in rows if row["id"] is not None]))
        if int(meta["_ncand"] or 0) < window:
            break  # every arm is exhausted
        cursor = (meta["_last_m"], meta["_last_st"], meta["_last_id"])
        if len(accepted) >= limit:
            accepted.sort(key=lambda item: item[0], reverse=True)
            if accepted[limit - 1][0] >= tuple(_ord(v) for v in cursor):
                break  # no unscanned row (key <= bound < cursor) can enter the page
        if rounds > 1:  # a short second window first (a few stale bounds), then geometric growth
            window = min(window * 2, _SCAN_WINDOW_MAX)
    accepted.sort(key=lambda item: item[0], reverse=True)
    return accepted[:limit]


def _complete_rollups(conn, picked: list[tuple[Any, dict]], legacy: bool) -> list[dict]:
    """``picked`` = ``[(scan row, record or None)]``; rows the scan did not hydrate (outside a round's top
    rows, or a query round) are hydrated with their roll-ups in ONE statement, input order kept."""
    missing = [row for row, info in picked if info is None]
    filled = {info["session_id"]: info for info in hydrate(conn, missing, rollup=True)} if missing else {}
    out = []
    for row, info in picked:
        info = info if info is not None else filled.get(row["id"])
        if info is not None:  # a row deleted since its round simply drops out
            out.append(info)
    return out


def _scanned_record(row, shim: Optional[dict]) -> Optional[dict]:
    """The full list record of a scan row hydrated in its round (``_hyd``), else None."""
    if not row["_hyd"]:
        return None
    info = _build_record(row, shim)
    info["running_children"] = int(row["_running_children"] or 0)
    info["children_total"] = int(row["_children_total"] or 0)
    return info


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
    from hermes_state_common import LIST_ATTACHABLE_KIND_SQL, LIST_OWN_KIND_SQL
    policy = get_session_acl("observe", scope=db)
    if scope == "children":
        legacy, held, unres = _selector_split(db, conn, mine_set, _Selectors(db))
        sid, sel = target_sql("", "$.session_id"), target_sql("", "$.selector")
        names = {f"l{i}": value for i, value in enumerate(mine)}
        selected = {f"h{i}": value for i, value in enumerate(held)}
        # One arm per lineage segment (and held selector): own delegate spawns by parent; opt-ins by their
        # literal target, attach binding or held selector (an opt-in is never a delegate: the partial indexes
        # hold exactly the kinds each arm can list).
        wheres = ([f"parent_session_id = :{n} AND {LIST_OWN_KIND_SQL}" for n in names]
                  + [f"{sid} = :{n} AND delivery_target IS NOT NULL AND {LIST_ATTACHABLE_KIND_SQL}" for n in names]
                  + [f"attached_session_id = :{n} AND {LIST_ATTACHABLE_KIND_SQL}" for n in names]
                  + [f"{sel} = :{n} AND delivery_target IS NOT NULL AND {sel} IS NOT NULL "
                     f"AND {LIST_ATTACHABLE_KIND_SQL}" for n in selected])
        filter_sql = "_how IS NOT NULL AND id NOT IN (SELECT value FROM json_each(:lin))"
        params = {**_params(mine, held, unres), **names, **selected}

        # The default ACL admits every row the rule picks (an own delegate spawn or a target the caller holds);
        # only a custom policy or a legacy shim row (no lineage_kind) needs the per-row policy call.
        def acl_needed(row) -> bool:
            return policy is not default_session_acl or not row["lineage_kind"]
        selectors = True
    else:  # siblings
        own = conn.execute("SELECT * FROM sessions WHERE id = ?", (logical_root(db, conn, caller_session_id),)).fetchone()
        parent = own["parent_session_id"] if own is not None else None
        if not parent or _effective_kind(own) != "delegate":
            return []
        parents = lineage(db, conn, parent)
        legacy = _legacy_table(conn)
        names = {f"l{i}": value for i, value in enumerate(parents)}
        wheres = [f"parent_session_id = :{n} AND {LIST_OWN_KIND_SQL}" for n in names]
        filter_sql = "_kind = 'delegate' AND id NOT IN (SELECT value FROM json_each(:lin))"
        params = {**_params(mine), **names}

        def acl_needed(row) -> bool:
            return False  # a sibling list is the peer roster: the observe ACL does not gate it
        selectors = False
    flags: tuple = (0, 1)
    if not needle and not include_closed:
        filter_sql += " AND _status != 'closed'"
        # list_closed = 1 rows are closed unless a running admission overrides that (LIST_CLOSED_SQL): the
        # open partition plus the flagged rows with an in-flight run.
        flags = (0, "running")
    # A query round reads its rows' assignment/segments to match; roll-ups wait for the final page. A plain
    # round hydrates only its top `limit` rows (usually the whole answer: no second statement).
    hydrate_sql = (_hydration_sql("ranked", rollup=False, legacy=legacy) + ", 0 AS _hyd" if needle else
                   _hydration_sql("ranked", rollup=True, legacy=legacy, when="ranked._rn <= :limit")
                   + ", ranked._rn <= :limit AS _hyd")
    params["limit"] = limit

    def sql_for(after: bool) -> str:
        return _scan_sql(wheres, flags, after=after, selectors=selectors, legacy=legacy, filter_sql=filter_sql,
                         hydrate=hydrate_sql)

    def accept(rows: list) -> list[tuple]:
        kept = [row for row in rows if not acl_needed(row) or allowed(db, conn, caller_session_id, row)]
        shims = _legacy_rows(conn, kept, legacy) if legacy else {}
        if needle:
            infos = [(row, _build_record(row, shims.get(row["id"]))) for row in kept]
            segments = {info["session_id"]: _segments_from(row["_segs"], row["id"]) for row, info in infos}
            unmatched = [info for _, info in infos if not _matches_fields(info, needle)]
            hits = _transcript_hits(conn, unmatched, segments, needle)
            kept = [row for row, info in infos if _matches_fields(info, needle) or info["session_id"] in hits]
        return [(_exact_key(row), (row, _scanned_record(row, shims.get(row["id"])))) for row in kept]

    page = _recency_scan(conn, sql_for=sql_for, params=params, limit=limit,
                         window=max(limit, 20) if needle else limit + 2, accept=accept)
    picked = [item for _, item in page]
    how = {row["id"]: row["_how"] for row, _ in picked}
    infos = _complete_rollups(conn, picked, legacy)
    return [_entry(info, how[info["session_id"]] if scope == "children" else None) for info in infos]


# ── tree: the same bounded scan over the caller's delegation tree ───────────────────────────────────────
#
# Membership is decided upward from each scanned row (batched per walk step, never per row): a row is a
# level-L member when its parent is a compression segment of a level-(L-1) member (level 0: the caller's own
# lineage), within _MAX_TREE_DEPTH, root_session_id = the tree's (or NULL), and it is an own delegate spawn
# or an attachable opt-in whose literal target is a segment of a level-(L-1) member -- exactly the
# breadth-first walk's edge rule (``_subtree``), read from the other end. Ordering contract: the newest
# `limit` of the WHOLE subtree (the breadth-first walk stopped collecting after ~_MAX_TREE_ROWS rows, so a
# tree wider than that used to rank only its first levels). The scan runs over the caller's delegation
# tree (``root_session_id``): for the tree's own root that is its subtree; a deeper caller's scan also
# passes (and rejects) newer rows of sibling branches. A ``query`` keeps the breadth-first walk (a search
# reads every member anyway), as does a store whose rows predate the topology columns.

def _tree_rows_sql(legacy: bool) -> str:
    return (f"SELECT s.id, s.parent_session_id, s.lineage_kind, s.delivery_target, s.root_session_id, s.model_config, "
            f"{_kind_sql()} AS _kind, p.end_reason IS 'compression' AS _pc, CASE WHEN p.end_reason = 'compression' "
            f"THEN (({_next_segment_sql('p.id')}) IS s.id) ELSE 0 END AS _cont FROM sessions s{_legacy_join(legacy)} "
            f"LEFT JOIN sessions p ON p.id = s.parent_session_id WHERE s.id IN (SELECT value FROM json_each(?))")


class _TreeMembership:
    """Upward membership/levels for scanned rows of one delegation tree (memoized for one listing)."""

    def __init__(self, conn, mine: Iterable[str], root_id: str, legacy: bool):
        self.conn, self.mine, self.root, self.legacy = conn, set(mine), root_id, legacy
        self.rows: dict[str, Optional[dict]] = {}
        self.nodes: dict[str, Optional[int]] = {}
        self.frontiers: dict[str, Optional[int]] = {}

    @staticmethod
    def _deps(row) -> list:
        out = [row["parent_session_id"]]
        if row["_kind"] != "delegate":  # an opt-in edge reads its literal target's level
            target = json_field(row["delivery_target"])
            if isinstance(target, dict) and isinstance(target.get("session_id"), str):
                out.append(target["session_id"])
        return out

    def prepare(self, scanned: Iterable[Any]) -> None:
        """Fetch every ancestor segment and literal target the levels of *scanned* consult: one statement
        per upward step for the whole batch (the first step always runs, so a round's statement count is
        its tree depth, not whether its rows' parents were already read)."""
        need = [dep for row in scanned for dep in self._deps(row)]
        for step in range((_MAX_TREE_DEPTH + 1) * 4):
            need = [sid for sid in dict.fromkeys(need) if sid and sid not in self.mine and sid not in self.rows]
            if not need and step:
                return
            got = {r["id"]: dict(r) for r in self.conn.execute(_tree_rows_sql(self.legacy), (json.dumps(need),))}
            self.rows.update((sid, got.get(sid)) for sid in need)
            need = [dep for row in got.values() for dep in self._deps(row)]

    def conversations(self) -> dict[str, Any]:
        """``{segment id: its conversation root row}`` for fetched rows that are their own conversation root
        (no compression-ended parent: ``logical_root`` stops there) or gone (None), seeding
        :func:`_default_tree_reach` so it re-reads none of them."""
        return {sid: row for sid, row in self.rows.items() if row is None or not row["_pc"]}

    def frontier_level(self, seg: Optional[str], stack: frozenset = frozenset()) -> Optional[int]:
        """The level L whose breadth-first frontier holds *seg* (a compression segment of a level-L member;
        0 for the caller's own lineage), else None."""
        if not seg:
            return None
        if seg in self.mine:
            return 0
        if seg in self.frontiers:
            return self.frontiers[seg]
        best, current, walked = None, seg, set()
        for _ in range(_CHAIN_CAP + 1):  # seg's chain heads: back through chosen continuations only
            row = self.rows.get(current)
            if row is None or current in walked:
                break
            walked.add(current)
            level = self.node_level(current, stack)
            if level is not None and (best is None or level < best):
                best = level
            if not row["_cont"]:
                break
            current = row["parent_session_id"]
            if current in self.mine:
                best = 0
                break
        if not stack:
            self.frontiers[seg] = best
        return best

    def node_level(self, sid: str, stack: frozenset = frozenset(), row: Any = None) -> Optional[int]:
        if sid in self.mine or sid in stack:
            return None
        if sid in self.nodes:
            return self.nodes[sid]
        row = row if row is not None else self.rows.get(sid)
        if row is None:
            return None
        inner = stack | {sid}
        level = None
        if row["root_session_id"] is None or row["root_session_id"] == self.root:
            parent_level = self.frontier_level(row["parent_session_id"], inner)
            if parent_level is not None and parent_level < _MAX_TREE_DEPTH and self._edge(row, parent_level, inner):
                level = parent_level + 1
        if not stack:
            self.nodes[sid] = level
        return level

    def _edge(self, row, parent_level: int, stack: frozenset) -> bool:
        """``_how_sql(selectors=False)`` against the parent's frontier."""
        kind = row["_kind"]
        if kind == "delegate":
            return True
        if kind in _EXCLUDED_KINDS:
            return False
        target = json_field(row["delivery_target"])
        if not (isinstance(target, dict) and target.get("attach") is True and target.get("kind") == "session"):
            return False
        sid = target.get("session_id")
        return bool(sid) and isinstance(sid, str) and self.frontier_level(sid, stack) == parent_level


def _list_tree(db, conn, caller_session_id: str, mine: list[str], *, needle: str, limit: int,
               include_closed: bool) -> list[dict]:
    root = conn.execute("SELECT root_session_id FROM sessions WHERE id = ?", (mine[0],)).fetchone()
    root_id = root[0] if root is not None else None
    if not root_id or needle:
        # A store whose topology predates the lineage migration, or a query: a search reads every member's
        # fields/transcript anyway (inherently O(subtree)), so it keeps the walk's one statement per level.
        return _list_tree_bfs(db, conn, caller_session_id, mine, needle=needle, limit=limit,
                              include_closed=include_closed)
    legacy = _legacy_table(conn)
    policy = get_session_acl("observe", scope=db)
    members = _TreeMembership(conn, mine, root_id, legacy)
    filter_sql = "id NOT IN (SELECT value FROM json_each(:lin))"
    flags: tuple = (0, 1)
    if not include_closed:
        filter_sql += " AND _status != 'closed'"
        flags = (0, "running")
    wheres = ["root_session_id = :root", "root_session_id IS NULL"]

    def sql_for(after: bool) -> str:
        return _scan_sql(wheres, flags, after=after, selectors=False, legacy=legacy, filter_sql=filter_sql,
                         hydrate=hydrate_sql)

    # The window's top rows (by exact key, members or not) carry their record columns, so the usual page
    # needs no second hydration statement; membership/ACL drop-outs are only skipped work.
    hydrate_sql = (_hydration_sql("ranked", rollup=True, legacy=legacy, when="ranked._rn <= :hyd")
                   + ", ranked._rn <= :hyd AS _hyd")

    def accept(rows: list) -> list[tuple]:
        members.prepare(rows)
        found = [(row, level) for row in rows if (level := members.node_level(row["id"], row=row)) is not None]
        kept = [(row, level) for row, level in found if allowed(db, conn, caller_session_id, row)] \
            if policy is not default_session_acl else _default_tree_reach(db, conn, mine, found,
                                                                          members.conversations())
        return [(_exact_key(row), (row, level, _scanned_record(row, None) if row["lineage_kind"] else None))
                for row, level in kept]  # a legacy shim row is hydrated with its shim at the end

    page = _recency_scan(conn, sql_for=sql_for, params=_params(mine, root=root_id, hyd=limit + 2), limit=limit,
                         window=limit + 2, accept=accept)
    levels = {row["id"]: level for _, (row, level, _) in page}
    out = []
    for info in _complete_rollups(conn, [(row, info) for _, (row, _, info) in page], legacy):
        info["depth_below_caller"] = levels[info["session_id"]]
        out.append(_entry(info, None))
    out.sort(key=lambda r: (r["last_activity_at"], r["created_at"], r["session_id"]), reverse=True)
    return out


def _list_tree_bfs(db, conn, caller_session_id: str, mine: list[str], *, needle: str, limit: int,
                   include_closed: bool) -> list[dict]:
    """The breadth-first tree listing (stores without root_session_id topology only)."""
    picked = _subtree(db, conn, mine)
    policy = get_session_acl("observe", scope=db)
    kept = [(row, level) for row, level in picked if allowed(db, conn, caller_session_id, row)] \
        if policy is not default_session_acl else _default_tree_reach(db, conn, mine, picked)
    levels = {row["id"]: level for row, level in kept}
    hydrated = _hydrate_rows(conn, [row for row, _ in kept], rollup=True)
    segments = {info["session_id"]: segs for info, segs, _ in hydrated}
    infos = [_entry(info, None) for info, _, _ in hydrated]
    hits: set[str] = set()
    if needle:  # one transcript statement for every row its fields do not already match
        hits = _transcript_hits(conn, [info for info in infos if not _matches_fields(info, needle)], segments, needle)
    out = []
    for info in infos:
        info["depth_below_caller"] = levels[info["session_id"]]
        if needle:
            if not _matches_fields(info, needle) and info["session_id"] not in hits:
                continue
        elif info["status"] == "closed" and not include_closed:
            continue
        out.append(info)
    out.sort(key=lambda r: (r["last_activity_at"], r["created_at"], r["session_id"]), reverse=True)
    return out[:limit]


_ROOT_WALK_COLS = "id, parent_session_id, source, model_config, end_reason"  # the lease-key walk's columns


def _logical_roots(db, conn, ids: Iterable[str]) -> dict[str, str]:
    """:func:`logical_root` of many ids at once: the same walk as ``SessionDB._session_turn_lease_key_on_conn``
    (compression parents, stopping at a fork child, a repeat or a non-compression parent), advanced for every
    id together: one statement per walk step, not per id."""
    rows: dict[str, Optional[dict]] = {}

    def fetch(need: Iterable[str]) -> None:
        need = [sid for sid in dict.fromkeys(need) if sid not in rows]
        if need:
            got = {r["id"]: dict(r) for r in conn.execute(
                f"SELECT {_ROOT_WALK_COLS} FROM sessions WHERE id IN (SELECT value FROM json_each(?))",
                (json.dumps(need),))}
            rows.update((sid, got.get(sid)) for sid in need)

    ids = [sid for sid in dict.fromkeys(ids) if sid]
    out: dict[str, str] = {}
    fetch(ids)
    walks: dict[str, tuple[dict, set]] = {}
    for sid in ids:
        if rows[sid] is None:
            out[sid] = sid
        else:
            walks[sid] = (rows[sid], {sid})
    while walks:
        step: dict[str, str] = {}
        for sid, (current, seen) in list(walks.items()):
            parent_id = current.get("parent_session_id")
            if not parent_id or parent_id in seen or db._is_explicit_fork_child_row(current, include_reset=True):
                out[sid] = str(current.get("id") or sid)
                del walks[sid]
            else:
                step[sid] = parent_id
        fetch(step.values())
        for sid, parent_id in step.items():
            current, seen = walks[sid]
            parent = rows[parent_id]
            if not parent or parent.get("end_reason") != "compression":
                out[sid] = str(current.get("id") or sid)
                del walks[sid]
            else:
                seen.add(parent_id)
                walks[sid] = (parent, seen)
    return out


def _default_tree_reach(db, conn, mine: list[str], picked: list[tuple],
                        known: Optional[dict[str, Any]] = None) -> list[tuple]:
    """:func:`default_session_acl` over a whole subtree with the caller's lineage resolved once: every row's
    ancestor walk advances together, one batched conversation lookup per step (statements grow with the
    tree's depth, never with its row count); each row's verdict is exactly the per-row walk's. *known*:
    ``{parent id: the sessions row of its conversation root (None: gone)}`` a caller already read."""
    mine_set, selectors = set(mine), _Selectors(db)
    conversations: dict[str, Any] = dict(known or {})  # parent id -> its conversation root row (None: gone)

    def resolve(parents: Iterable[str]) -> None:
        need = [p for p in dict.fromkeys(parents) if p not in conversations]
        if not need:
            return
        roots = _logical_roots(db, conn, need)
        got = {r["id"]: r for r in conn.execute("SELECT * FROM sessions WHERE id IN (SELECT value FROM json_each(?))",
                                                (json.dumps(sorted(set(roots.values()))),))}
        conversations.update((p, got.get(roots.get(p, p))) for p in need)

    verdict: dict[int, bool] = {}
    walks: dict[int, Any] = {}
    for index, (row, _) in enumerate(picked):
        if row["id"] in mine_set:
            continue
        if _targets(row, mine_set, selectors):
            verdict[index] = True
        else:
            walks[index] = row
    for _ in range(_MAX_TREE_DEPTH):
        if not walks:
            break
        step: dict[int, str] = {}
        for index, current in list(walks.items()):
            parent = current["parent_session_id"]
            if parent and parent in mine_set:
                verdict[index] = _effective_kind(current) not in (None, "continuation", "branch", "reset", "switch")
            if not parent or parent in mine_set:
                del walks[index]
            else:
                step[index] = parent
        resolve(step.values())
        for index, parent in step.items():
            if conversations[parent] is None:
                del walks[index]
            else:
                walks[index] = conversations[parent]
    return [item for index, item in enumerate(picked) if verdict.get(index)]


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
    row = conn.execute(
        "SELECT s.*, EXISTS (SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'delegate_sessions') "
        "AS _legacy_present FROM sessions s WHERE s.id = ?", (session_id,)).fetchone()
    if row is None or not allowed(db, conn, caller_session_id, row):
        raise LookupError("No session with that session_id is reachable from this conversation.")
    return observe_row(db, conn, row, limit=limit, max_chars=max_chars)


def observe_row(db, conn, row, *, limit: Optional[int] = None, max_chars: Optional[int] = None,
                tip_check: Optional[Callable[[dict, Optional[str]], None]] = None) -> dict:
    """:func:`observe` of a *row* the caller's ACL already reached (:func:`allowed` on this *db*; the
    delegate store's reach read). ``tip_check(record, tip end_reason)`` may refuse before any transcript
    is read. Same bounded statements as :func:`observe` minus its row read and ACL."""
    from agent import session_views as views
    info, ids, seg_ends = _hydrate_rows(conn, [row], legacy=_legacy_hint(row), ends=tip_check is not None)[0]
    if tip_check is not None:
        tip_check(info, seg_ends.get(info["current_session_id"]))
    legacy = bool(_legacy_hint(row)) if _legacy_hint(row) is not None else _legacy_table(conn)
    limit = views.observe_limit(limit)
    messages = db.get_lineage_tail_preview(info["current_session_id"], limit + 1)
    truncated = len(messages) > limit
    tail = [views.message_view(m) for m in messages[-limit:]]
    subs, total = _children_preview(db, conn, ids, views.CHILDREN_MAX, legacy=legacy)
    rollup = views.children_rollup(subs, lambda sub: views.latest_step(sub.get("_latest_message")),
                                   inherit_model=info.get("model"), total=total)
    return {"session": views.session_brief(info), "messages": tail, "truncated": truncated, **rollup}
