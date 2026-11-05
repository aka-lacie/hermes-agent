"""Reference session-key resolvers for the two keyed consumers (plan §7 Phase 3 item 3).

The owning plugins register these (or their own equivalents) for their routes::

    from gateway.platforms.webhook_session_key_builtins import github_pr_session_key, outlook_session_key
    ctx.register_webhook_session_key("github-pr", github_pr_session_key)
    ctx.register_webhook_session_key("ibn-inbox-events", outlook_session_key)

Both produce exactly the keys the live route gates already emit through ``__hermes_session_key__``
(``~/.hermes/profiles/ykhos/scripts/github_pr_gate.py`` / ``ibn_inbox_gate.py``), so a route can move
from its gate script to a plugin resolver (or keep both: the script answer wins) without forking an
existing conversation.

- github-pr: one session per pull request, ``github-pr|v1|<owner/repo lowercased>#<number>``.
- ibn-inbox-events: one session per (mailbox, Outlook thread),
  ``outlook|v1|<mailbox>|<normalized conversation id>``. One message copied into several mailboxes
  carries a different Graph ``conversationId`` per mailbox; decoding shows the same 16-byte
  conversation token after a ``00 10 00`` marker, so the id normalizes to ``graph-token:<hex>``
  (unknown shapes stay ``exact:<casefolded id>``). The mailbox is part of the key: each mailbox's
  copy of a thread is its own conversation.
"""
from __future__ import annotations

import base64
import re
import unicodedata
from typing import Any, Optional

_GRAPH_TOKEN_MARKER = bytes((0, 16, 0))


def _normalized_text(value: Any) -> str:
    if value in (None, ""):
        return ""
    return re.sub(r"\s+", " ", unicodedata.normalize("NFKC", str(value))).strip().casefold()


def normalized_conversation_id(value: Any) -> str:
    """Graph ``conversationId`` without its per-mailbox wrapper (see the module docstring)."""
    raw = str(value or "").strip()
    if not raw:
        return ""
    try:
        decoded = base64.urlsafe_b64decode((raw + "=" * (-len(raw) % 4)).encode("ascii"))
        marker_at = decoded.rfind(_GRAPH_TOKEN_MARKER)
        token = decoded[marker_at + len(_GRAPH_TOKEN_MARKER):] if marker_at >= 0 else b""
        if len(token) == 16:
            return "graph-token:" + token.hex()
    except (ValueError, UnicodeEncodeError):
        pass  # not base64: the exact id below
    return "exact:" + _normalized_text(raw)


def outlook_key(mailbox: Any, conversation_id: Any) -> Optional[str]:
    """``outlook|v1|<mailbox>|<conversation>``, or None when either part is missing."""
    mailbox_text = _normalized_text(mailbox)
    conversation = normalized_conversation_id(conversation_id)
    return f"outlook|v1|{mailbox_text}|{conversation}" if mailbox_text and conversation else None


def outlook_session_key(payload: Any, **_: Any) -> Optional[str]:
    """ibn-inbox-events resolver: ``payload.inbox.mailbox`` + ``payload.outlook.conversation_id``."""
    inbox = payload.get("inbox") if isinstance(payload, dict) else None
    outlook = payload.get("outlook") if isinstance(payload, dict) else None
    if not isinstance(inbox, dict) or not isinstance(outlook, dict):
        return None
    return outlook_key(inbox.get("mailbox"), outlook.get("conversation_id"))


def github_pr_key(repo: Any, number: Any) -> Optional[str]:
    """``github-pr|v1|<repo lowercased>#<number>``, or None for a malformed repo/number."""
    if not isinstance(repo, str) or not re.fullmatch(r"[A-Za-z0-9._-]+/[A-Za-z0-9._-]+", repo.strip()):
        return None
    if isinstance(number, bool) or not str(number).strip().isdigit() or int(str(number).strip()) <= 0:
        return None
    return f"github-pr|v1|{repo.strip().lower()}#{int(str(number).strip())}"


def github_pr_session_key(payload: Any, **_: Any) -> Optional[str]:
    """github-pr resolver: the gate's normalized ``repository`` / ``number``, else a raw GitHub
    ``pull_request`` / ``issue`` event (``repository.full_name`` + PR number)."""
    if not isinstance(payload, dict):
        return None
    if isinstance(payload.get("repository"), str):
        return github_pr_key(payload["repository"], payload.get("number"))
    repository = payload.get("repository") if isinstance(payload.get("repository"), dict) else {}
    pull = payload.get("pull_request") if isinstance(payload.get("pull_request"), dict) else None
    issue = payload.get("issue") if isinstance(payload.get("issue"), dict) else None
    item = pull if pull is not None else issue
    if item is None or (pull is None and not isinstance(item.get("pull_request"), dict)):
        return None  # an issue comment on a plain issue is not a PR conversation
    return github_pr_key(repository.get("full_name"), item.get("number"))
