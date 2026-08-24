"""Plugin-facing delivery of internal notifications to live gateway sessions.

An internal turn is ordinary gateway input with two important differences:

* it is synthetic, so it bypasses inbound-user authorization; and
* its payload is handled by the main agent before anything is sent outward.

The target conversation is resolved when the event is dispatched.  Callers do
not pin a session id, so a reminder or webhook completion naturally follows
the channel's current active session after ``/new`` or compression.
"""

from __future__ import annotations

from dataclasses import dataclass
import logging
from typing import Any, Mapping, Optional

logger = logging.getLogger(__name__)

INTERNAL_NOTIFICATION_MARKER = "[HERMES_INTERNAL_NOTIFICATION]"


def trusted_internal_notification_context(
    event: Any,
) -> Optional[dict[str, str]]:
    """Return sanitized notification metadata for an authenticated event.

    The model-visible marker is deliberately not part of the trust decision:
    ordinary inbound text can contain it.  Only synthetic ``MessageEvent``
    instances carrying the service-owned metadata flag are authenticated.
    """
    if not bool(getattr(event, "internal", False)):
        return None
    metadata = getattr(event, "metadata", None)
    if not isinstance(metadata, Mapping):
        return None
    if metadata.get("internal_notification") is not True:
        return None

    context = {
        "kind": str(metadata.get("internal_notification_kind") or "notification"),
        "source": str(metadata.get("internal_notification_source") or "internal"),
    }
    event_id = metadata.get("internal_notification_id")
    if event_id:
        context["event_id"] = str(event_id)
    return context


def format_internal_notification(
    text: str,
    *,
    kind: str,
    source_label: str,
    event_id: Optional[str] = None,
) -> str:
    """Return the stable, model-visible envelope for an internal turn."""
    payload = str(text or "").strip()
    lines = [
        INTERNAL_NOTIFICATION_MARKER,
        f"kind: {str(kind or 'notification').strip() or 'notification'}",
        f"source: {str(source_label or 'internal').strip() or 'internal'}",
    ]
    if event_id:
        lines.append(f"event_id: {str(event_id).strip()}")
    lines.extend(
        (
            "---",
            "This is an automated background event, not a new message from "
            "the user. Use the current conversation context, act if useful, "
            "and reply only when the user needs an update. Otherwise respond "
            "with exactly [SILENT].",
            "",
            payload,
        )
    )
    return "\n".join(lines)


@dataclass(frozen=True)
class InternalTurnTarget:
    """Concrete gateway destination resolved before enqueueing."""

    platform: str
    chat_id: str
    thread_id: Optional[str] = None
    user_id: Optional[str] = None
    chat_type: str = "dm"
    profile: Optional[str] = None


def handle_internal_turn_control_request(
    runner: Any, request: Mapping[str, Any]
) -> dict[str, bool]:
    """Handle one trusted local control-socket delivery request."""
    payload = request.get("payload")
    if not isinstance(payload, Mapping):
        return {"accepted": False}
    target_payload = payload.get("target")
    if not isinstance(target_payload, Mapping):
        return {"accepted": False}

    target = InternalTurnTarget(
        platform=str(target_payload.get("platform") or "").strip(),
        chat_id=str(target_payload.get("chat_id") or "").strip(),
        thread_id=(
            str(target_payload.get("thread_id")).strip()
            if target_payload.get("thread_id") is not None
            else None
        ),
        user_id=(
            str(target_payload.get("user_id")).strip()
            if target_payload.get("user_id") is not None
            else None
        ),
        chat_type=str(target_payload.get("chat_type") or "dm").strip() or "dm",
        profile=(
            str(target_payload.get("profile")).strip()
            if target_payload.get("profile") is not None
            else None
        ),
    )
    text = str(payload.get("text") or "").strip()
    metadata = payload.get("metadata") or {}
    if (
        not target.platform
        or not target.chat_id
        or not text
        or not isinstance(metadata, Mapping)
    ):
        return {"accepted": False}
    kind = str(payload.get("kind") or "notification")
    source_label = str(payload.get("source_label") or "internal")
    event_id = payload.get("event_id")
    event_metadata = dict(metadata)
    event_metadata.update(
        {
            "internal_notification": True,
            "internal_notification_kind": kind,
            "internal_notification_source": source_label,
        }
    )
    if event_id:
        event_metadata["internal_notification_id"] = str(event_id)
    try:
        accepted = runner.enqueue_internal_turn(
            target=target,
            text=format_internal_notification(
                text,
                kind=kind,
                source_label=source_label,
                event_id=str(event_id) if event_id else None,
            ),
            metadata=event_metadata,
        )
    except Exception:
        logger.warning("Control-socket internal-turn enqueue failed", exc_info=True)
        accepted = False
    return {"accepted": bool(accepted)}


class InternalTurnService:
    """Small facade exposed to plugins as ``ctx.internal_turns``."""

    def enqueue(
        self,
        text: str,
        *,
        platform: str,
        chat_id: str,
        thread_id: Optional[str] = None,
        user_id: Optional[str] = None,
        chat_type: str = "dm",
        profile: Optional[str] = None,
        kind: str = "notification",
        source_label: str = "plugin",
        event_id: Optional[str] = None,
        metadata: Optional[Mapping[str, Any]] = None,
    ) -> bool:
        """Queue one internal main-agent turn on a concrete live channel."""
        payload = str(text or "").strip()
        if not payload:
            return False

        runner = None
        try:
            from gateway.run import get_active_gateway_runner

            runner = get_active_gateway_runner()
        except Exception:
            logger.debug("Internal-turn enqueue could not resolve gateway runner", exc_info=True)

        target = InternalTurnTarget(
            platform=str(platform or "").strip(),
            chat_id=str(chat_id or "").strip(),
            thread_id=str(thread_id).strip() if thread_id is not None else None,
            user_id=str(user_id).strip() if user_id is not None else None,
            chat_type=str(chat_type or "dm").strip() or "dm",
            profile=str(profile).strip() if profile is not None else None,
        )
        if not target.platform or not target.chat_id:
            return False

        event_metadata = dict(metadata or {})
        event_metadata.update(
            {
                "internal_notification": True,
                "internal_notification_kind": str(kind or "notification"),
                "internal_notification_source": str(source_label or "internal"),
            }
        )
        if event_id:
            event_metadata["internal_notification_id"] = str(event_id)

        formatted_text = format_internal_notification(
            payload,
            kind=kind,
            source_label=source_label,
            event_id=event_id,
        )
        if runner is not None:
            return bool(
                runner.enqueue_internal_turn(
                    target=target,
                    text=formatted_text,
                    metadata=event_metadata,
                )
            )

        # The Desktop dashboard owns a backup cron ticker but deliberately has
        # no messaging adapters. If it wins the store's cross-process tick
        # lock, hand the synthetic turn to the gateway that owns this profile
        # instead of falsely marking delivery failed.
        try:
            from gateway.control_socket import query_gateway_control
            from hermes_constants import get_hermes_home

            result = query_gateway_control(
                get_hermes_home(),
                "enqueue_internal_turn",
                payload={
                    "target": {
                        "platform": target.platform,
                        "chat_id": target.chat_id,
                        "thread_id": target.thread_id,
                        "user_id": target.user_id,
                        "chat_type": target.chat_type,
                        "profile": target.profile,
                    },
                    "text": payload,
                    "kind": kind,
                    "source_label": source_label,
                    "event_id": event_id,
                    "metadata": dict(metadata or {}),
                },
            )
        except Exception:
            logger.debug(
                "Internal-turn enqueue could not reach the gateway control socket",
                exc_info=True,
            )
            result = None
        if isinstance(result, Mapping) and result.get("accepted") is True:
            return True
        logger.warning(
            "Internal-turn enqueue skipped: no active gateway runner accepted %s:%s",
            target.platform,
            target.chat_id,
        )
        return False

    def enqueue_home(
        self,
        text: str,
        *,
        platform: str,
        kind: str = "notification",
        source_label: str = "plugin",
        event_id: Optional[str] = None,
        metadata: Optional[Mapping[str, Any]] = None,
    ) -> bool:
        """Queue an internal turn on a platform's configured HOME channel."""
        try:
            from gateway.config import Platform
            from gateway.run import get_active_gateway_runner

            runner = get_active_gateway_runner()
            platform_enum = Platform(str(platform or "").strip().lower())
            home = runner.config.get_home_channel(platform_enum) if runner else None
        except Exception:
            logger.debug("Internal-turn HOME resolution failed", exc_info=True)
            return False
        if home is None:
            logger.warning(
                "Internal-turn enqueue skipped: no HOME channel for %s", platform
            )
            return False
        return self.enqueue(
            text,
            platform=platform_enum.value,
            chat_id=str(home.chat_id),
            thread_id=home.thread_id,
            user_id=home.user_id,
            kind=kind,
            source_label=source_label,
            event_id=event_id,
            metadata=metadata,
        )
