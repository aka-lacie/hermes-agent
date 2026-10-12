"""Exact-request approval view; base view retains the shared live authorization gate."""
from typing import Optional
import logging
from agent.i18n import t

logger = logging.getLogger("plugins.platforms.discord.adapter")


def make_exec_approval_view(_HermesView, discord, _read_discord_prompt_timeout, _unauthorized):
    class ExecApprovalView(_HermesView):
        """Allow Once / Allow Session / Always Allow / Deny buttons for a dangerous command.
        Clicks call ``resolve_gateway_approval()`` — the same mechanism as the text ``/approve`` flow."""

        def __init__(
            self, session_key: str, allowed_user_ids: set, allowed_role_ids: Optional[set] = None,
            require_admin: bool = False, admin_user_ids: Optional[set] = None,
            allow_permanent: bool = True, allow_session: bool = True, smart_denied: bool = False,
            request_id: Optional[str] = None,
        ):
            super().__init__(allowed_user_ids, allowed_role_ids, timeout=_read_discord_prompt_timeout())
            self.session_key = session_key
            self.require_admin = require_admin
            self.admin_user_ids = {str(a).strip() for a in (admin_user_ids or set()) if str(a).strip()}
            self._localize_buttons(
                allow_once="gateway.exec_approval.action_once", allow_session="gateway.exec_approval.action_session",
                allow_always="gateway.exec_approval.action_always", deny="gateway.exec_approval.action_deny")
            if smart_denied or not allow_session:
                self.remove_item(self.allow_session)
                self.remove_item(self.allow_always)
            elif not allow_permanent:
                self.remove_item(self.allow_always)
            from gateway.platforms.approval_cards import ApprovalCardBinding
            actions = {"once": self.allow_once, "session": self.allow_session,
                       "always": self.allow_always, "deny": self.deny}
            self._approval_binding = ApprovalCardBinding(session_key, request_id,
                [choice for choice, button in actions.items() if button in self.children])
            for choice, button in actions.items():
                if choice in self._approval_binding.ids:
                    button.custom_id = self._approval_binding.ids[choice]
                    button.disabled = not bool(request_id)

        def _check_auth(self, interaction: discord.Interaction) -> bool:
            """Base admission always required; with ``require_admin`` the clicker must
            also be an admin. Fails closed (logged once) when no admins are configured."""
            if not super()._check_auth(interaction):
                return False
            if not self.require_admin:
                return True
            user = getattr(interaction, "user", None)
            try:
                uid = str(getattr(user, "id", "") or "")
            except Exception:
                uid = ""
            if uid and uid in self.admin_user_ids:
                return True
            if not self.admin_user_ids:
                logger.warning(
                    "[Discord] require_admin_for_exec_approval is enabled but "
                    "no admins are configured (allow_admin_from is empty) — "
                    "exec approval buttons are disabled for everyone. Add "
                    "admin user IDs under the discord platform's "
                    "allow_admin_from, or disable the toggle."
                )
            return False

        async def _resolve(self, interaction: discord.Interaction, choice: str, color: discord.Color, label_key: str):
            """Resolve the approval via the gateway approval queue and update the embed."""
            if not await self._gate(
                interaction, resolved_msg=t("platform.discord.approval.already_resolved"),
                unauth_msg=_unauthorized(),
            ):
                return
            label = t(label_key)
            self.resolved = True
            # Unblock the waiting agent thread FIRST. A click after the approval
            # wait timed out (count == 0) must not claim "Approved".
            try:
                binding = self._approval_binding
                custom_id = (getattr(interaction, "data", None) or {}).get("custom_id")
                message_matches = self._message is not None and interaction.message.id == self._message.id
                count = binding.resolve(custom_id, choice) if message_matches else 0
                logger.info(
                    "Discord button resolved %d approval(s) for session %s (choice=%s, user=%s)",
                    count, self.session_key, choice, interaction.user.display_name,
                )
            except Exception as exc:
                logger.exception("Failed to resolve gateway approval from button: %s", exc)
                count = 0
            if not count:
                color = discord.Color.dark_grey()
                label = t("platform.discord.approval.expired")
            await self._finalize_embed(
                interaction, color,
                t("platform.discord.approval.by_user", label=label, user=interaction.user.display_name) if count else label)

        # Decorator labels are placeholders; ``_localize_buttons`` in __init__ sets the real text.
        @discord.ui.button(label="Allow Once", style=discord.ButtonStyle.green)
        async def allow_once(self, interaction: discord.Interaction, button: discord.ui.Button):
            await self._resolve(interaction, "once", discord.Color.green(), "platform.discord.approval.resolved_once")

        @discord.ui.button(label="Allow Session", style=discord.ButtonStyle.grey)
        async def allow_session(self, interaction: discord.Interaction, button: discord.ui.Button):
            await self._resolve(interaction, "session", discord.Color.blue(), "platform.discord.approval.resolved_session")

        @discord.ui.button(label="Always Allow", style=discord.ButtonStyle.blurple)
        async def allow_always(self, interaction: discord.Interaction, button: discord.ui.Button):
            await self._resolve(interaction, "always", discord.Color.purple(), "platform.discord.approval.resolved_always")

        @discord.ui.button(label="Deny", style=discord.ButtonStyle.red)
        async def deny(self, interaction: discord.Interaction, button: discord.ui.Button):
            await self._resolve(interaction, "deny", discord.Color.red(), "platform.discord.approval.resolved_deny")

    return ExecApprovalView
