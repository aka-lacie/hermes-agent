"""Conversation-only forks: user turns, final answers, and compaction handoffs.

Like Codex subagent forks, a child never inherits the parent's unfinished spawn
call, sibling tool results, or reasoning. The assignment arrives as its own turn.
"""

from contextlib import contextmanager
from contextvars import ContextVar
from copy import deepcopy
from dataclasses import dataclass, field
from threading import Lock


@dataclass
class _ForkSource:
    owner: int
    messages: list | None
    snapshot: list | None = None
    lock: Lock = field(default_factory=Lock)


_SOURCE: ContextVar[_ForkSource | None] = ContextVar("subagent_fork_source", default=None)


@contextmanager
def bind_fork_source(parent, messages):
    """One lazy snapshot for all launches in a hook/tool round, including worker threads."""
    source = _ForkSource(id(parent), messages)
    token = _SOURCE.set(source)
    try:
        yield
    finally:
        # Copied Contexts may outlive a tool round; never let them pin its transcript.
        with source.lock:
            source.messages = source.snapshot = None
        _SOURCE.reset(token)


def conversation_snapshot(messages):
    from agent.context_compressor import is_compaction_summary_message
    from agent.session_persistence import _is_ephemeral_scaffolding

    result = []
    for message in messages:
        if not isinstance(message, dict) or _is_ephemeral_scaffolding(message):
            continue
        role = message.get("role")
        if role not in {"user", "assistant"}:
            continue
        summary = is_compaction_summary_message(message)
        final = (
            role == "assistant" and not message.get("tool_calls")
            and message.get("finish_reason") in {None, "stop", "end_turn"}
        )
        if role != "user" and not final and not summary:
            continue
        # Rebuild instead of copying session row ids, persistence flags, opaque
        # reasoning signatures, usage anchors, or the parent's tool-call metadata.
        content = message.get("api_content")
        if content is None:
            content = message.get("content")
        if not content:
            continue
        row = {"role": role, "content": deepcopy(content)}
        for key in ("timestamp", "_compressed_summary", "_compressed_summary_has_user_turn"):
            if key in message:
                row[key] = deepcopy(message[key])
        if final:
            row["finish_reason"] = "stop"
        result.append(row)
    return result


def fork_history(parent):
    """Return a private copy; never share mutable message parts between siblings."""
    source = _SOURCE.get()
    if source is not None and source.owner == id(parent):
        with source.lock:
            if source.messages is not None:
                if source.snapshot is None:
                    source.snapshot = conversation_snapshot(source.messages)
                return deepcopy(source.snapshot)
    messages = getattr(parent, "_session_messages", None)
    return conversation_snapshot(messages) if isinstance(messages, list) else []


def attach_fork_history(child, history):
    if history:
        child._delegate_fork_history = deepcopy(history)
        child._delegate_fork_needs_persist = True


def persist_fork_history(agent, conversation_history):
    """Seed the child's own session before compaction; ordinary history is already durable."""
    if vars(agent).get("_delegate_fork_needs_persist") is not True:
        return
    if agent._flush_messages_to_session_db(conversation_history, None) is False:
        raise RuntimeError("Could not persist inherited subagent context; refusing to start the child.")
    agent._delegate_fork_needs_persist = False
