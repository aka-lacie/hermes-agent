"""Shared types for normalized provider responses.

These dataclasses define the canonical shape that all provider adapters
normalize responses to.  The shared surface is intentionally minimal —
only fields that every downstream consumer reads are top-level.
Protocol-specific state goes in ``provider_data`` dicts (response-level
and per-tool-call) so that protocol-aware code paths can access it
without polluting the shared type.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any


@dataclass
class ToolCall:
    """A normalized tool call from any provider.

    ``id`` is the protocol's canonical identifier — what gets used in
    ``tool_call_id`` / ``tool_use_id`` when constructing tool result
    messages.  May be ``None`` when the provider omits it; the agent
    fills it via ``_deterministic_call_id()`` before storing in history.

    ``provider_data`` carries per-tool-call protocol metadata that only
    protocol-aware code reads. Providers should namespace opaque replay
    state, e.g. ``{"openai_codex": {...}}`` or ``{"google": {...}}``.
    """

    id: str | None
    name: str
    arguments: str  # JSON string
    provider_data: dict[str, Any] | None = field(default=None, repr=False)

    # ── Backward compatibility ──────────────────────────────────
    # The agent loop reads tc.function.name / tc.function.arguments
    # throughout run_agent.py (45+ sites).  These properties let
    # NormalizedResponse pass through without the _nr_to_assistant_message
    # shim, while keeping ToolCall's canonical fields flat.
    @property
    def type(self) -> str:
        return "function"

    @property
    def function(self) -> ToolCall:
        """Return self so tc.function.name / tc.function.arguments work."""
        return self

    @property
    def call_id(self) -> str | None:
        """Codex call_id from provider_data, accessed via getattr by _build_assistant_message."""
        pd = self.provider_data or {}
        codex = pd.get("openai_codex") if isinstance(pd.get("openai_codex"), dict) else {}
        return codex.get("call_id") or pd.get("call_id")

    @property
    def response_item_id(self) -> str | None:
        """Codex response_item_id from provider_data."""
        pd = self.provider_data or {}
        codex = pd.get("openai_codex") if isinstance(pd.get("openai_codex"), dict) else {}
        return codex.get("response_item_id") or pd.get("response_item_id")

    @property
    def extra_content(self) -> dict[str, Any] | None:
        """Provider-specific OpenAI-compatible extra_content replay payload.

        Gemini thinking models attach a thought signature in this payload, and
        older provider paths may store it either at the top level or under the
        Google namespace.
        """
        pd = self.provider_data or {}
        google = pd.get("google") if isinstance(pd.get("google"), dict) else {}
        return google.get("extra_content") or pd.get("extra_content")


@dataclass
class Usage:
    """Token usage from an API response."""

    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    cached_tokens: int = 0


@dataclass
class NormalizedResponse:
    """Normalized API response from any provider.

    Shared fields are truly cross-provider — every caller can rely on
    them without branching on api_mode.  Protocol-specific state goes in
    ``provider_data`` so that only protocol-aware code paths read it.

    Response-level ``provider_data`` examples:

    * Anthropic: ``{"reasoning_details": [...]}``
    * Codex: ``{"codex_reasoning_items": [...], "codex_message_items": [...]}``
    * Gemini: ``{"google": {"gemini_content": {"role": "model", "parts": [...]}}}``
    * Others: ``None``
    """

    content: str | None
    tool_calls: list[ToolCall] | None
    finish_reason: str  # "stop", "tool_calls", "length", "content_filter"
    reasoning: str | None = None
    usage: Usage | None = None
    provider_data: dict[str, Any] | None = field(default=None, repr=False)

    # ── Backward compatibility ──────────────────────────────────
    # The shim _nr_to_assistant_message() mapped these from provider_data.
    # These properties let NormalizedResponse pass through directly.
    @property
    def reasoning_content(self) -> str | None:
        pd = self.provider_data or {}
        return pd.get("reasoning_content")

    @property
    def reasoning_details(self):
        pd = self.provider_data or {}
        return pd.get("reasoning_details")

    @property
    def codex_reasoning_items(self):
        pd = self.provider_data or {}
        codex = pd.get("openai_codex") if isinstance(pd.get("openai_codex"), dict) else {}
        return codex.get("codex_reasoning_items") or pd.get("codex_reasoning_items")

    @property
    def gemini_content(self):
        pd = self.provider_data or {}
        google = pd.get("google") if isinstance(pd.get("google"), dict) else {}
        return google.get("gemini_content") or pd.get("gemini_content")

    @property
    def codex_message_items(self):
        pd = self.provider_data or {}
        codex = pd.get("openai_codex") if isinstance(pd.get("openai_codex"), dict) else {}
        return codex.get("codex_message_items") or pd.get("codex_message_items")


# ---------------------------------------------------------------------------
# Factory helpers
# ---------------------------------------------------------------------------


def build_tool_call(
    id: str | None,
    name: str,
    arguments: Any,
    **provider_fields: Any,
) -> ToolCall:
    """Build a ``ToolCall``, auto-serialising *arguments* if it's a dict.

    Any extra keyword arguments are collected into ``provider_data``.
    """
    args_str = json.dumps(arguments) if isinstance(arguments, dict) else str(arguments)
    pd = dict(provider_fields) if provider_fields else None
    return ToolCall(id=id, name=name, arguments=args_str, provider_data=pd)


def map_finish_reason(reason: str | None, mapping: dict[str, str]) -> str:
    """Translate a provider-specific stop reason to the normalised set.

    Falls back to ``"stop"`` for unknown or ``None`` reasons.
    """
    if reason is None:
        return "stop"
    return mapping.get(reason, "stop")
