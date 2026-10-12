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

    def launch_session(self, *, goal, context=None, title=None, fork_context=None, task_parameters=None,
                       background=False, output_schema=None, _workflow_request=None, _credentials_cfg=None):
        """Start a retained child through the normal delegation routing and admission."""
        from agent.delegation_provider import RESERVED_TASK_PARAMETERS
        from agent.subagent_lifecycle import SubagentLifecycleError
        if task_parameters is not None and (not isinstance(task_parameters, dict)
                                           or RESERVED_TASK_PARAMETERS.intersection(task_parameters)):
            raise SubagentLifecycleError("task_parameters must be an object of backend-specific fields, not core task fields.")
        task = {**(task_parameters or {}), "goal": goal, "context": context, "title": title}
        if fork_context is not None:
            task["fork_context"] = fork_context
        return self._session_action("spawn", tasks=[task], background=background, output_schema=output_schema,
                                    _workflow_request=_workflow_request, credentials_cfg=_credentials_cfg)


    def resume_session(self, session_id, message, *, model=None, background=False, _workflow_request=None):
        return self._session_action("resume", session_id=session_id, message=message, model=model, background=background, _workflow_request=_workflow_request)

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

    def run_workflow_agent(self, assignment, input, output_schema, *, fork_context=False, session_id=None,
                           model=None, provider=None, code_context):
        """Run/continue an owned retained child and return its complete validated output."""
        from agent.workflow_agents import run_workflow_agent
        return run_workflow_agent(self, assignment, input, output_schema, fork_context=fork_context,
                                  session_id=session_id, model=model, provider=provider, code_context=code_context)

    def run_workflow_agents(self, items, max_concurrency, *, fail_fast=False, code_context):
        """Run a bounded rolling map sharing host admission and ancestor limits."""
        from agent.workflow_agents import run_workflow_agents
        return run_workflow_agents(self, items, max_concurrency, fail_fast=fail_fast, code_context=code_context)
