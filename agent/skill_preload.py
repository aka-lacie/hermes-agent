"""Turn-zero, data-only skill selection before the immutable system prefix is built."""
from __future__ import annotations

import logging

_LOG = logging.getLogger(__name__)


def prepare_skill_preloads(agent, *, user_message, conversation_history=None, inputs=None):
    """Plugins may add names, never tools or rendered instructions. Final eligibility is host-owned.

    Only a fresh conversation/activation runs selectors. Later turns use the normal pre_llm_call
    advisory seam and never rewrite the cached prefix. The bound turn supplies the owning profile.
    """
    if getattr(agent, "_skill_preload_selection_done", False):
        return
    if conversation_history and not getattr(agent, "_delegate_resumable", False):
        agent._skill_preload_selection_done = True
        return  # ordinary resumes restore their prefix; selections belong in this turn's advisory context
    agent._skill_preload_selection_done = True
    from hermes_cli.lifecycle import invoke_hook
    try:
        results = invoke_hook(
            "pre_skill_preload", user_message=user_message, conversation_history=conversation_history,
            platform=getattr(agent, "platform", ""), session_id=getattr(agent, "session_id", ""),
            parent_session_id=getattr(agent, "_parent_session_id", ""), is_first_turn=True, inputs=inputs)
        names = list(getattr(agent, "preload_skills", None) or [])
        for result in results or []:
            values = result.get("preload_skills") if isinstance(result, dict) else None
            if isinstance(values, list) and all(isinstance(n, str) and n.strip() for n in values):
                names.extend(n.strip() for n in values)
        agent.preload_skills = list(dict.fromkeys(names))
    except Exception:
        _LOG.warning("Skill preload selection skipped", exc_info=True)
