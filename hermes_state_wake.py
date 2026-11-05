"""Wake policy of session admissions (unified-session plan §4.7, "Non-urgent batching").

``session_admissions.wake_policy`` NULL (or ``immediate``) is today's behaviour: the drain claims the
row on its own. ``batch_with_next_user_turn`` holds a notice: it stays ``queued`` but is never a FIFO
head by itself. The next claim of a user turn (a person, or a delegate child's parent) on the same
session takes every held notice with it, as transfers bound to that claim (the ``steering`` state the
steer handoff already uses), so the drain spends zero extra turns on them. The consumer that claimed
them writes them into the transcript in front of the user turn and acknowledges them
(``ack_held_inputs``); an unacknowledged one goes back to its queued place when the claim settles, so a
crash between claim and transcript write never loses a notice.

Pure storage: callers authorize, and nothing here parses message text.
"""
import json

WAKE_IMMEDIATE = 'immediate'
WAKE_WITH_NEXT_USER_TURN = 'batch_with_next_user_turn'
WAKE_POLICIES = frozenset({WAKE_IMMEDIATE, WAKE_WITH_NEXT_USER_TURN})
# The terminal outcome of a held notice its user turn carried into the transcript.
HELD_DELIVERED = 'batched'
# The held-transfer state: identical to ``hermes_state_runtime.STEERING`` (kept literal: no import cycle).
_TRANSFER = 'steering'
# A row a drain may claim as its head (index-friendly: the planner still ranges on target/status/seq).
CLAIMABLE_SQL = "(wake_policy IS NULL OR wake_policy != 'batch_with_next_user_turn')"
# Who speaks for a session's user side: a person, or a delegate child's parent.
USER_TURN_SENDERS = frozenset({'human', 'parent'})
_NON_USER_PRINCIPAL_PREFIXES = ('automation:', 'child:', 'cron:', 'cron-owner', 'webhook:', 'peer:', 'system:')
_EVENT_PAYLOAD_KEYS = ('system_event_v1', 'local_automation_v1')


def stored_wake_policy(value):
    """The column value for a requested policy: NULL for immediate (today's rows), else the hold."""
    if value in (None, WAKE_IMMEDIATE):
        return None
    if value == WAKE_WITH_NEXT_USER_TURN:
        return value
    from hermes_state_runtime import RuntimeStoreError
    raise RuntimeStoreError('invalid_params', field='wake_policy', detail='unknown wake policy')


def is_held(row) -> bool:
    """A queued notice waiting for the next user turn (never a head on its own)."""
    return row.get('status') == 'queued' and row.get('wake_policy') == WAKE_WITH_NEXT_USER_TURN


def claimable(row) -> bool:
    """A queued row a drain may claim as its FIFO head."""
    return row.get('status') == 'queued' and row.get('wake_policy') != WAKE_WITH_NEXT_USER_TURN


def is_user_turn(principal_id, payload) -> bool:
    """Whether an admission is the session's user speaking (structure only, never text).

    A native platform message without an automation descriptor is a person; a payload naming its
    ``sender_kind`` says so; producer envelopes (system events, local automation) and producer
    principals never are."""
    if not isinstance(payload, dict) or any(key in payload for key in _EVENT_PAYLOAD_KEYS):
        return False
    native = payload.get('native_text_v1')
    if isinstance(native, dict):
        return 'automation' not in native
    if payload.get('sender_kind') is not None:
        return payload['sender_kind'] in USER_TURN_SENDERS
    return not str(principal_id or '').startswith(_NON_USER_PRINCIPAL_PREFIXES)


def hand_held_to_claim(conn, *, session_id, epoch, generation):
    """Inside the claim's write: every queued held notice of *session_id* becomes a transfer bound to
    the claim ``(epoch, generation)``, in arrival order. Returns them as admission dicts."""
    rows = conn.execute("SELECT * FROM session_admissions WHERE target_session_id=? AND status='queued' "
                        "AND wake_policy=? ORDER BY seq", (session_id, WAKE_WITH_NEXT_USER_TURN)).fetchall()
    held = []
    for row in rows:
        conn.execute("UPDATE session_admissions SET status='terminal',outcome=?,owner_epoch=?,generation=? "
                     "WHERE admission_id=? AND status='queued'", (_TRANSFER, epoch, generation, row['admission_id']))
        item = dict(row)
        item['payload'] = json.loads(item.pop('payload_json'))
        for key in ('payload_digest', 'lineage_json', 'retired_at'):
            item.pop(key, None)
        item.update(status='terminal', outcome=_TRANSFER, owner_epoch=epoch, generation=generation)
        held.append(item)
    return held


def ack_held_inputs(db, *, epoch, generation, admission_ids) -> int:
    """The claim's consumer wrote these held notices into the transcript: they are delivered."""
    ids = [str(value) for value in admission_ids or ()]
    if not ids:
        return 0

    def write(conn):
        from hermes_state_runtime import _epoch
        _epoch(conn, epoch)
        changed = 0
        for admission_id in ids:
            changed += conn.execute(
                "UPDATE session_admissions SET outcome=? WHERE admission_id=? AND status='terminal' AND outcome=? "
                "AND owner_epoch=? AND generation=? AND wake_policy=?",
                (HELD_DELIVERED, admission_id, _TRANSFER, epoch, generation, WAKE_WITH_NEXT_USER_TURN)).rowcount
        return changed
    return db._execute_write(write)


def requeue_unacked_held(conn, *, epoch, generation) -> int:
    """In the claim's settlement: a held notice its consumer never acknowledged goes back to its
    queued place (its ``seq``) for the next user turn."""
    return conn.execute(
        "UPDATE session_admissions SET status='queued',outcome=NULL,generation=NULL WHERE status='terminal' "
        "AND outcome=? AND owner_epoch=? AND generation=? AND wake_policy=?",
        (_TRANSFER, epoch, generation, WAKE_WITH_NEXT_USER_TURN)).rowcount
