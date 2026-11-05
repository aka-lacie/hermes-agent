"""Session discovery and bounded observation over the unified ``sessions`` table (plan §4.5, §4.6).

Freya's rule for ``delegate_task(action="list")`` (binding, BRIEF3): every session shares one table, but a
caller's default list (``scope="children"``) shows only

- its own delegate spawns (``lineage_kind='delegate'`` whose parent is in the caller's conversation), and
- sessions explicitly opted in from another source: a webhook/cron (or other non-delegate) session whose
  ``delivery_target`` is ``{kind: "session", session_id: <caller>, attach: true}``, badged with its
  ``lineage_kind``.

Compression continuations (the same conversation), memory/background forks, branches/resets, standalone
cron/webhook sessions and the caller's own lineage never appear. Grandchildren (d=2) never flatten into the
top-level list: each row carries a ``children`` rollup (active/total, newest activity), ``observe`` on a
child lists its sub-children, and ``scope="tree"`` expands the caller's whole subtree.

Policy is pluggable: :func:`set_session_acl` replaces the default ACL (the caller may reach its descendants
and sessions whose ``delivery_target`` names it). The primitives here take ``caller_session_id`` and apply
only that ACL plus the store's own safety rules.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from typing import Any, Callable, Iterable, Optional

logger = logging.getLogger(__name__)

LIST_SCOPES = ("children", "siblings", "tree")
OPT_IN_KINDS = frozenset({"webhook", "cron"})
_MAX_TREE_DEPTH = 8
_MAX_TREE_ROWS = 500
_CHAIN_CAP = 64

# Chain segments of one logical conversation: a compression continuation of *parent* (never a fork).
_NEXT_SEGMENT_SQL = None  # built lazily (needs hermes_state_common)


def _next_segment_sql() -> str:
    global _NEXT_SEGMENT_SQL
    if _NEXT_SEGMENT_SQL is None:
        from hermes_state_common import _non_continuation_child_sql, _sql_session_last_active
        _NEXT_SEGMENT_SQL = (
            "SELECT child.id FROM sessions parent JOIN sessions child ON child.parent_session_id = parent.id "
            "WHERE parent.id = ? AND parent.end_reason = 'compression' "
            + _non_continuation_child_sql("child.", "parent.id")
            + " ORDER BY CASE WHEN child.end_reason = 'compression' THEN 0 "
            "WHEN child.ended_at IS NULL THEN 1 ELSE 2 END, "
            + _sql_session_last_active("child") + " DESC, child.started_at DESC, child.id DESC LIMIT 1")
    return _NEXT_SEGMENT_SQL


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


def _last_activity(conn, ids: list[str], floor: float) -> float:
    best = floor or 0.0
    for sid in ids:
        stamp = conn.execute("SELECT MAX(timestamp) FROM messages WHERE session_id = ?", (sid,)).fetchone()[0]
        if stamp is not None:
            best = max(best, float(stamp))
    return best


def _admission_state(conn, session_id: str) -> tuple[bool, int, Optional[str]]:
    """``(running, queued_count, latest_assignment_text)`` from the session's own FIFO."""
    rows = conn.execute("SELECT status, principal_id, payload_json FROM session_admissions WHERE target_session_id = ? "
                        "ORDER BY seq", (session_id,)).fetchall()
    running = any(r["status"] in ("started", "unknown") for r in rows)
    queued = sum(1 for r in rows if r["status"] == "queued")
    latest = None
    for r in rows:
        if str(r["principal_id"]).startswith("parent:"):
            payload = json_field(r["payload_json"]) or {}
            if isinstance(payload.get("text"), str):
                latest = payload["text"]
    return running, queued, latest


def record(conn, row) -> dict:
    """The public record of one session row (credential-free; never transcript bodies)."""
    row = dict(row)
    sid = row["id"]
    config = json_field(row.get("model_config")) or {}
    brief = config.get("_delegate_brief") if isinstance(config.get("_delegate_brief"), dict) else {}
    legacy = _legacy_delegate(conn, sid) if not row.get("lineage_kind") else None
    route = json_field(row.get("route")) or {}
    segments = chain(conn, sid)
    running, queued, latest = _admission_state(conn, sid)
    stored = row.get("status") or (legacy or {}).get("status")
    if stored == "closed":
        status = "closed"
    elif running:
        status = "running"
    elif stored in ("running", "idle"):
        status = "idle"  # a cached "running" with no started admission left is a finished run
    else:
        status = "closed" if row.get("ended_at") and row.get("end_reason") != "compression" else "idle"
    fork = json_field(row.get("fork_origin"))
    kind = row.get("lineage_kind") or ("delegate" if legacy else _effective_kind(_RowView(row)))
    initial = brief.get("initial_goal") or (legacy or {}).get("initial_goal") or ""
    created = float(row.get("started_at") or (legacy or {}).get("created_at") or 0)
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
        "status": status, "queued": queued, "created_at": created,
        "last_activity_at": _last_activity(conn, segments, max(created, float(row.get("last_activity_at") or 0))),
        "delivery_target": json_field(row.get("delivery_target")), "group_id": row.get("group_id"),
        "retained": bool(brief.get("retain") or legacy),
    }


class _RowView(dict):
    def __getitem__(self, key):
        return self.get(key)


# ── ACL ──────────────────────────────────────────────────────────────────────

def _targets(row, caller_lineage: set) -> bool:
    target = json_field(row["delivery_target"])
    return (isinstance(target, dict) and target.get("kind") == "session"
            and target.get("session_id") in caller_lineage)


def default_session_acl(db, conn, caller_session_id: str, target_row) -> bool:
    """The default policy (plan §4.5): the caller may reach its descendants (any depth, through compression
    continuations of every ancestor) and any session whose ``delivery_target`` targets the caller's
    conversation (how the main agent resumes webhook sessions delivering to it)."""
    if target_row is None or not caller_session_id:
        return False
    mine = set(lineage(db, conn, caller_session_id))
    if target_row["id"] in mine:
        return False  # one's own lineage is not a child
    if _targets(target_row, mine):
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


_acl: Callable[..., bool] = default_session_acl


def set_session_acl(policy: Optional[Callable[..., bool]]) -> Callable[..., bool]:
    """Install ``policy(db, conn, caller_session_id, target_row) -> bool`` (None restores the default).
    Returns the previous policy. Worker C's ``ctx.sessions`` widens it (e.g. to siblings) from a plugin."""
    global _acl
    previous, _acl = _acl, (policy or default_session_acl)
    return previous


def get_session_acl() -> Callable[..., bool]:
    return _acl


def allowed(db, conn, caller_session_id: str, target_row) -> bool:
    try:
        return bool(_acl(db, conn, caller_session_id, target_row))
    except Exception:  # health: allow BLE001 -- plugin policy boundary: a broken ACL fails closed, logged
        logger.warning("session ACL policy %r failed; access denied", _acl, exc_info=True)
        return False


# ── listing ──────────────────────────────────────────────────────────────────

_EXCLUDED_KINDS = frozenset({"continuation", "branch", "reset", "switch", "root"})


def _listed_kind(conn, row) -> Optional[str]:
    """The kind a row lists under: its stamped ``lineage_kind``, or ``delegate`` for a pre-unified retained child
    (a legacy ``delegate_sessions`` row). A bare ``_delegate_from`` marker is a child still being built or a
    non-retained run (stamped when it ends): the live registry lists those, not the table."""
    kind = row["lineage_kind"]
    if kind:
        return kind
    return "delegate" if _legacy_delegate(conn, row["id"]) is not None else None


def _is_default_child(row, caller_lineage: set, conn=None) -> Optional[str]:
    """``"own"`` for a delegate spawn of the caller, ``"attached"`` for an explicit opt-in, else None."""
    kind = _listed_kind(conn, row) if conn is not None else _effective_kind(row)
    if kind == "delegate" and row["parent_session_id"] in caller_lineage:
        return "own"
    target = json_field(row["delivery_target"])
    if (kind not in _EXCLUDED_KINDS and kind != "delegate" and isinstance(target, dict)
            and target.get("kind") == "session" and target.get("attach") is True
            and target.get("session_id") in caller_lineage):
        return "attached"
    return None


def _rollup(conn, session_id: str) -> dict:
    """Grandchild counts of one d=1 row: delegate children of its conversation, active = running/queued."""
    ids = chain(conn, session_id)
    rows = conn.execute(f"SELECT * FROM sessions WHERE parent_session_id IN ({_placeholders(ids)})", ids).fetchall()
    active = total = 0
    newest = None
    for row in rows:
        if _listed_kind(conn, row) != "delegate":
            continue
        total += 1
        info = record(conn, row)
        if info["status"] == "running" or info["queued"]:
            active += 1
        newest = max(newest or 0.0, info["last_activity_at"])
    return {"active": active, "total": total, "last_activity_at": newest}


def _matches(conn, info: dict, needle: str) -> bool:
    if any(needle in str(info.get(key) or "").casefold() for key in ("session_id", "title", "assignment", "initial_goal")):
        return True
    for sid in chain(conn, info["session_id"]):
        if conn.execute("SELECT 1 FROM messages WHERE session_id = ? AND instr(lower(content), ?) > 0 LIMIT 1",
                        (sid, needle)).fetchone():
            return True
    return False


def _entry(conn, row, how: Optional[str]) -> dict:
    info = record(conn, row)
    if how == "attached" or info["lineage_kind"] not in ("delegate", None):
        info["badge"] = info["lineage_kind"]
    info["children"] = _rollup(conn, info["session_id"])
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
    if scope == "children":
        candidates = conn.execute(
            f"SELECT * FROM sessions WHERE parent_session_id IN ({_placeholders(mine)}) "
            f"UNION SELECT * FROM sessions WHERE delivery_target IS NOT NULL AND "
            f"json_extract(delivery_target, '$.session_id') IN ({_placeholders(mine)})", (*mine, *mine)).fetchall()
        picked = [(row, _is_default_child(row, mine_set, conn)) for row in candidates]
        picked = [(row, how) for row, how in picked if how]
    elif scope == "siblings":
        own = conn.execute("SELECT * FROM sessions WHERE id = ?", (logical_root(db, conn, caller_session_id),)).fetchone()
        parent = own["parent_session_id"] if own is not None else None
        if not parent or _effective_kind(own) != "delegate":
            return []
        parents = lineage(db, conn, parent)
        rows = conn.execute(f"SELECT * FROM sessions WHERE parent_session_id IN ({_placeholders(parents)})",
                            parents).fetchall()
        picked = [(row, "sibling") for row in rows
                  if row["id"] not in mine_set and _listed_kind(conn, row) == "delegate"]
    else:
        picked = _subtree(db, conn, mine)
    out = []
    for row, how in picked:
        if not allowed(db, conn, caller_session_id, row) and how != "sibling":
            continue
        info = _entry(conn, row, how if how != "sibling" else None)
        if scope == "tree":
            info["depth_below_caller"] = how
        if needle:
            if not _matches(conn, info, needle):
                continue
        elif info["status"] == "closed" and not include_closed:
            continue
        out.append(info)
    out.sort(key=lambda r: (r["last_activity_at"], r["created_at"], r["session_id"]), reverse=True)
    return out[:limit]


def _subtree(db, conn, mine: list[str]) -> list[tuple]:
    """Every delegate/attached descendant of the caller's conversation, breadth-first, bounded. The
    candidate set is pre-filtered on the indexed ``root_session_id``; ``parent_session_id`` decides."""
    frontier, out, seen = list(mine), [], set(mine)
    root = conn.execute("SELECT root_session_id FROM sessions WHERE id = ?", (mine[0],)).fetchone()
    root_id = root[0] if root is not None else None
    for level in range(1, _MAX_TREE_DEPTH + 1):
        if not frontier or len(out) >= _MAX_TREE_ROWS:
            break
        sql = f"SELECT * FROM sessions WHERE parent_session_id IN ({_placeholders(frontier)})"
        params: list[Any] = list(frontier)
        if root_id:
            sql += " AND (root_session_id = ? OR root_session_id IS NULL)"
            params.append(root_id)
        nxt = []
        for row in conn.execute(sql, params).fetchall():
            if row["id"] in seen:
                continue
            how = _is_default_child(row, set(frontier), conn)
            if how is None:
                continue
            seen.add(row["id"])
            out.append((row, level))
            nxt.extend(chain(conn, row["id"]))
        frontier = nxt
    return out


def get_record(db, conn, caller_session_id: str, session_id: str) -> dict:
    """One session the caller may reach (ACL), or LookupError without disclosing whose it is."""
    row = conn.execute("SELECT * FROM sessions WHERE id = ?", (session_id,)).fetchone()
    if row is None or not allowed(db, conn, caller_session_id, row):
        raise LookupError("No session with that session_id is reachable from this conversation.")
    return record(conn, row)


def observe(db, conn, caller_session_id: str, session_id: str, *, limit: int = 20, max_chars: int = 12000) -> dict:
    """A bounded read of one reachable session: its record, its transcript tail (never reasoning or full tool
    arguments) and its sub-children (d+1) with status. Observation never counts as activity."""
    from tools.delegate_observation import message_metadata, preview_text
    info = get_record(db, conn, caller_session_id, session_id)
    limit = max(1, min(int(limit or 20), 100))
    budget = max(1, min(int(max_chars or 12000), 50000))
    messages = db.get_messages(info["current_session_id"], include_ancestors=True, limit=limit + 1, latest=True)
    selected, truncated = [], len(messages) > limit
    for message in reversed(messages[-limit:]):
        content = _text_content(message.get("content"))
        metadata, metadata_truncated = message_metadata(message)
        metadata_size = len(json.dumps(metadata, ensure_ascii=False)) if metadata else 0
        if metadata_size > budget:
            metadata, metadata_size, metadata_truncated = {}, 0, True
        budget -= metadata_size
        cap = min(budget, 1500) if message["role"] == "tool" else budget
        truncated = truncated or metadata_truncated or len(content) > cap
        content = preview_text(content, cap)
        selected.append({"role": message["role"], "content": content, "timestamp": message.get("timestamp"), **metadata})
        budget -= len(content)
        if budget <= 0:
            truncated = truncated or len(selected) < len(messages)
            break
    ids = chain(conn, session_id)
    subs = []
    for row in conn.execute(f"SELECT * FROM sessions WHERE parent_session_id IN ({_placeholders(ids)})", ids).fetchall():
        if _is_default_child(row, set(ids), conn) is None:
            continue
        sub = record(conn, row)
        subs.append({key: sub[key] for key in ("session_id", "title", "status", "lineage_kind", "queued",
                                               "last_activity_at", "model", "depth")})
    subs.sort(key=lambda r: (r["last_activity_at"], r["session_id"]), reverse=True)
    return {"session": info, "messages": list(reversed(selected)), "truncated": truncated, "children": subs}


def _text_content(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(part.get("text", "") for part in content
                         if isinstance(part, dict) and isinstance(part.get("text"), str))
    return "" if content is None else str(content)
