"""Immutable, host-derived approval attribution; never an authorization input."""
from dataclasses import asdict, dataclass
import hashlib
import json
import re
import unicodedata
import uuid


def display_text(value, limit=96):
    """Redact before bounding; titles may contain credentials, markup or mentions."""
    from agent.redact import redact_sensitive_text
    text = redact_sensitive_text(str(value or ""), force=True)
    text = re.sub(r"(?:https?://|www\.)\S+", "[endpoint]", text)
    text = re.sub(r"\b(?:[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}|\d{8}_\d{6}_[0-9a-fA-F]+|[0-9a-fA-F]{32,})\b", "[identifier]", text)
    text = " ".join("".join(c for c in text if not unicodedata.category(c).startswith("C")).split())
    text = text.replace("@", "＠").replace("<", "‹").replace(">", "›")
    text = re.sub(r"([\\`*_{}\[\]()#!|~])", r"\\\1", text)
    return text[:limit] + ("…" if len(text) > limit else "")


@dataclass(frozen=True)
class ApprovalActor:
    kind: str
    title: str
    reference: str
    parent: "ApprovalActor | None" = None


@dataclass(frozen=True)
class ApprovalProvenance:
    version: int
    actor: ApprovalActor
    tool: str
    workflow: str | None = None
    workflow_revision: str | None = None

    def to_dict(self):
        return asdict(self)


@dataclass(frozen=True)
class ApprovalOrigin:
    identity: str
    provenance: ApprovalProvenance


def actor_for(agent):
    attrs = vars(agent)
    stable = attrs.get("_delegate_session_id") or attrs.get("session_id")
    if not isinstance(stable, str) or not stable:
        return None
    # The familiar suffix remains stable across retained-child activations.
    suffix = re.sub(r"[^a-zA-Z0-9]", "", stable)[-6:]
    if len(suffix) < 6:
        suffix = hashlib.sha256(stable.encode()).hexdigest()[:6]
    child = bool(attrs.get("_delegate_depth", 0) or attrs.get("_delegate_session_id"))
    parent = attrs.get("_approval_parent_actor") if child else None
    return ApprovalActor("subagent" if child else "main", display_text(attrs.get("_approval_title", "")),
                         suffix, parent if isinstance(parent, ApprovalActor) else None)


def attach_child_actor(child, parent):
    """Capture immediate lineage at spawn, before a background worker loses its parent."""
    actor = actor_for(parent)
    if actor is not None:
        # Only immediate lineage is public; do not grow an unbounded ancestor payload.
        child._approval_parent_actor = ApprovalActor(actor.kind, actor.title, actor.reference)


def capture_origin(context, tool):
    from hermes_constants import get_hermes_home
    actor = actor_for(context.agent)
    if actor is None:
        return None
    attrs = vars(context.agent)
    activation = attrs.get("_delegate_session_token") or attrs.get("_current_turn_id")
    if not activation:
        activation = attrs.setdefault("_approval_activation", uuid.uuid4().hex)
    workflow = context.approval_workflow
    identity = json.dumps([str(get_hermes_home()), attrs.get("_delegate_session_id") or attrs.get("session_id"),
                           activation, tool, context.origin, workflow], separators=(",", ":"))
    public = ApprovalProvenance(1, actor, display_text(tool),
                                display_text(workflow[0], 160) if workflow else None,
                                workflow[1] if workflow else None)
    return ApprovalOrigin(hashlib.sha256(identity.encode()).hexdigest(), public)


def current_origin():
    from agent.tool_invocation import current_call_context
    context = current_call_context()
    value = context.approval_origin if context is not None else None
    return value if isinstance(value, ApprovalOrigin) else None


def public_provenance():
    origin = current_origin()
    return origin.provenance if origin else None


def format_provenance(value):
    """Render only the bounded snapshot, never fallback to the receiving gateway actor."""
    if isinstance(value, ApprovalProvenance):
        value = value.to_dict()
    if not isinstance(value, dict) or value.get("version") != 1:
        return "Origin unavailable"
    actor = value.get("actor") or {}
    if actor.get("kind") not in {"main", "subagent"}:
        return "Origin unavailable"
    label = "Main agent" if actor["kind"] == "main" else "Subagent"
    line = label + (": " + actor["title"] if actor.get("title") else "") + " · #" + actor["reference"]
    parent = actor.get("parent")
    if parent:
        line += "\nParent: " + (parent.get("title") or ("Main agent" if parent["kind"] == "main" else "Subagent"))
        line += " · #" + parent["reference"]
    line += "\nTool: " + value["tool"]
    if value.get("workflow"):
        line += "\nVia workflow: " + value["workflow"]
    return line
