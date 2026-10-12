"""Plugin-facing conversation operations; run handles keep their existing contract."""

import json


class SubagentSessionMethods:
    def _session_action(self, action, **kwargs):
        from agent.subagent_lifecycle import SubagentLifecycleError
        from tools.delegate_sessions import control
        from tools.delegate_tool import delegate_task
        parent = self._parent_agent_resolver()
        if parent is None:
            raise SubagentLifecycleError("Session operations require an active parent.")
        raw = (delegate_task(parent_agent=parent, _retain_sessions=True, **kwargs) if action == "spawn"
               else control(action, parent, **kwargs))
        result = json.loads(raw)
        if result.get("error"):
            raise SubagentLifecycleError(str(result["error"]))
        return result

    def launch_session(self, *, goal, context=None, title=None, fork_context=True, model=None, provider=None,
                       background=False):
        """Start a retained child through the normal delegation routing and admission."""
        task = {"goal": goal, "context": context, "title": title, "fork_context": fork_context}
        task.update({key: value for key, value in {"model": model, "provider": provider}.items() if value is not None})
        return self._session_action("spawn", tasks=[task], background=background)

    def resume_session(self, session_id, message, *, model=None, background=False):
        return self._session_action("resume", session_id=session_id, message=message, model=model, background=background)

    def list_sessions(self, *, query=None, limit=20):
        return self._session_action("list", query=query, limit=limit)

    def observe_session(self, session_id, *, limit=20):
        return self._session_action("observe", session_id=session_id, limit=limit)

    def close_session(self, session_id):
        return self._session_action("close", session_id=session_id)

    def steer_session(self, session_id, message):
        return self._session_action("steer", session_id=session_id, message=message)

    def stop_session(self, session_id):
        return self._session_action("stop", session_id=session_id)
