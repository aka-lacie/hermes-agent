"""PluginContext webhook capabilities: route-scoped session keys for plugin-owned webhook routes."""
from __future__ import annotations

import inspect
from typing import Any, Callable


class WebhookContextMethods:
    def register_webhook_session_key(self, route_name: str, resolver: Callable[..., Any]):
        """Key deliveries of webhook route *route_name* to resumable sessions.

        ``resolver(payload, *, headers, event_type, delivery_id, profile, route)`` (sync or async;
        extra keyword arguments it does not declare are withheld) returns the session key (``str`` or
        ``{"key": str}``) or None for a per-delivery session. Every delivery that returns the same key
        on this route resumes one session (gateway/platforms/webhook_session_keys.py). Built on the
        ``resolve_session_key`` hook and fired only for *route_name*; the returned registration
        handle unregisters it like any other hook."""
        if not isinstance(route_name, str) or not route_name.strip():
            raise ValueError("register_webhook_session_key needs a route name")
        if not callable(resolver):
            raise TypeError("register_webhook_session_key needs a callable resolver")
        target = route_name.strip()
        from hermes_cli.plugins_dispatch import PluginDispatchMixin
        from gateway.platforms.webhook_session_keys import HOOK

        async def route_session_key(*, route_name: str, payload: Any = None, **kwargs: Any):
            if route_name != target:
                return None
            answer = resolver(payload, **PluginDispatchMixin._hook_callback_kwargs(
                resolver, {"route_name": route_name, **kwargs}))
            return await answer if inspect.isawaitable(answer) else answer

        route_session_key.__name__ = f"webhook_session_key[{target}]"
        return self.register_hook(HOOK, route_session_key)
