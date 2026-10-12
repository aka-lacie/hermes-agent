"""The public entry lifetime remains authoritative when completion and stop coincide."""
import threading
from types import SimpleNamespace

import pytest

from agent.code_bridge import current_cell_authority
from agent.code_cells import accept_cell, _run_cell_handler
from agent.tool_invocation import CallContext, bind_call_context


@pytest.mark.parametrize('stop', ['deadline', 'interrupt'])
def test_completed_entry_is_stopped_without_losing_its_receipts(stop):
    agent = SimpleNamespace(session_id='entry-boundary', _tool_worker_threads=set(),
                            _tool_worker_threads_lock=threading.Lock())
    context = CallContext(agent, 'entry-boundary', 'entry-call', fork_messages=[])
    receipt = {'status': 'completed', 'request_id': 'already-executed'}
    with bind_call_context(context), accept_cell(context):
        authority = current_cell_authority()
        event = authority.entry_done

        class CompletionBoundary:
            def clear(self):
                event.clear()
            def set(self):
                event.set()
            def is_set(self):
                return event.is_set()
            def wait(self, timeout):
                ready = event.wait(timeout)
                if ready:
                    if stop == 'deadline':
                        authority.deadline = 0
                    else:
                        agent._interrupt_requested = True
                return ready

        authority.entry_done = CompletionBoundary()
        result = _run_cell_handler({'handler': lambda args, ctx: {
            'status': 'success', 'output': 'completed output', 'call_receipts': [receipt]}}, {}, context)
    assert result['status'] == ('timeout' if stop == 'deadline' else 'cancelled')
    assert result['output'] == 'completed output'
    assert result['call_receipts'] == [receipt]
    assert result['incomplete_teardown'] == []
