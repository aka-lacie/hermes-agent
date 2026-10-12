"""A terminal RPC reply cannot turn a concurrent deadline/interrupt into success."""
import json
from types import SimpleNamespace

import pytest

from tools.code_kernel import CellAuthority, _await_cell, cell_timeout_message
from tools.code_kernel_remote import _run_remote_cell


@pytest.mark.parametrize('transport', ['local', 'remote'])
@pytest.mark.parametrize('stop', ['deadline', 'interrupt', 'local_budget'])
@pytest.mark.parametrize('reply_status', ['ok', 'error'])
def test_stop_wins_when_terminal_response_arrives(transport, stop, reply_status, monkeypatch):
    clock = [0.0]
    interrupted = [False]
    monkeypatch.setattr('tools.code_kernel.time.monotonic', lambda: clock[0])
    monkeypatch.setattr('tools.interrupt.is_interrupted', lambda: interrupted[0])
    authority = CellAuthority('completion-boundary', timeout=100)
    authority.in_flight = 0 if stop == 'local_budget' else 1
    payload = {'status': reply_status, 'traceback': 'CellCancelled', 'stdout': 'receipt received'}

    def completed_response():
        clock[0] = 101 if stop == 'deadline' else 11
        interrupted[0] = stop == 'interrupt'
        return payload

    class Responses:
        def get(self, timeout):
            return completed_response()

    kernel = SimpleNamespace(cell_authority=authority, response_q=Responses(),
                             cell_seq=0, kernel_dir='/fixture', env=object(),
                             sh=lambda *args, **kwargs: json.dumps(completed_response()))
    if transport == 'local':
        status, _ = _await_cell(kernel, 10, lambda: interrupted[0])
    else:
        monkeypatch.setattr('tools.code_execution_tool._ship_file_to_remote', lambda *args, **kwargs: None)
        status, _ = _run_remote_cell(kernel, 'pass', 10)
    assert status == ('interrupted' if stop == 'interrupt' else 'timeout')
    assert authority.cancel.is_set()
    if stop == 'deadline':
        assert 'absolute workflow deadline' in cell_timeout_message(authority, 10)


@pytest.mark.parametrize('cleanup_fails', [False, True])
def test_remote_cleanup_cannot_hide_deadline_or_lose_completed_payload(monkeypatch, cleanup_fails):
    clock = [0.0]
    monkeypatch.setattr('tools.code_kernel.time.monotonic', lambda: clock[0])
    monkeypatch.setattr('tools.interrupt.is_interrupted', lambda: False)
    monkeypatch.setattr('tools.code_execution_tool._ship_file_to_remote', lambda *args, **kwargs: None)
    authority = CellAuthority('cleanup-boundary', timeout=100)
    payload = {'status': 'ok', 'stdout': 'effect already completed', 'execution_count': 1}
    calls = []

    def transport(command, **kwargs):
        calls.append(command)
        if command.startswith('cat '):
            return json.dumps(payload)
        clock[0] = 101
        if cleanup_fails:
            raise ConnectionError('cleanup transport failed after completion')
        return ''

    kernel = SimpleNamespace(cell_authority=authority, cell_seq=0, kernel_dir='/fixture',
                             env=object(), sh=transport)
    status, completed = _run_remote_cell(kernel, 'pass', 200)
    assert status == 'timeout'
    assert completed == payload
    assert authority.cancel.is_set()
    assert len(calls) == 2  # Read once and clean up once; no replay.
