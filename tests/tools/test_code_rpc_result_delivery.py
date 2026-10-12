"""Bounded RPC terminal records preserve aggregates and typed partial evidence."""
import json
import os
import subprocess
import sys
from types import SimpleNamespace

import pytest

from agent.code_artifacts import INLINE_BYTES, MAX_DELIVERY_BYTES, read_artifact
from agent.workflow_agents import AgentOutputError
from tools.code_execution_client import client_source
from tools.code_kernel import CellAuthority
from tests.tools.test_code_rpc_v2 import socket_host

pytestmark = pytest.mark.usefixtures('rpc_transport_context')


@pytest.mark.parametrize('failure,remote', [(False, False), (True, False), (False, True), (True, True)])
def test_terminal_aggregates_and_error_evidence_are_bounded(tmp_path, monkeypatch, failure, remote):
    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    authority = CellAuthority('fixture')
    authority.parent_context.agent.session_id = 'rpc-owner'
    if remote:
        authority.artifact_delivery = {'kind': 'remote', 'env': SimpleNamespace(), 'directory': '/kernel/artifacts'}
    size = MAX_DELIVERY_BYTES + 1 if remote else 300 * 1024
    # Every map item is below the inline ceiling; the aggregate must still spill.
    payload = [{'output': {'answer': 'x' * (size // 3)}, 'session_id': str(i), 'status': 'completed'} for i in range(3)]
    effects = []
    def dispatch(*args):
        effects.append('once')
        if failure:
            raise AgentOutputError('Invalid output', session_id='retained-child', partial=payload)
        return json.dumps(payload)
    authority._invoke = dispatch
    with socket_host(authority) as (address, state, counter):
        code = client_source() + '\n_bind_cell(' + repr(authority.identity()) + ''')
try:
    result = _call('effect', {})
    print(json.dumps({'result_size': sum(len(item['output']['answer']) for item in result)}))
except BridgeError as error:
    print(json.dumps({'type': type(error).__name__, 'code': error.code, 'session_id': error.session_id,
                      'partial_size': sum(len(item['output']['answer']) for item in error.partial)
                                      if isinstance(error.partial, list) else None,
                      'partial': error.partial if isinstance(error.partial, dict) else None,
                      'request_id': error.receipt['request_id']}))
'''
        env = dict(os.environ, HERMES_RPC_SOCKET=f'tcp://{address[0]}:{address[1]}', HERMES_RPC_TOKEN='token')
        process = subprocess.run([sys.executable, '-c', code], env=env, text=True, capture_output=True, timeout=15, check=False)
        assert process.returncode == 0, process.stderr
        result = json.loads(process.stdout)
        record = next(iter(state.records.values()))
        response = state._response(record)
        assert len(json.dumps(response).encode()) <= INLINE_BYTES
        assert effects == ['once'] and counter == [1]
        if not remote:
            assert result['partial_size' if failure else 'result_size'] == (size // 3) * 3
            if failure:
                assert result['type'] == 'AgentOutputError' and result['session_id'] == 'retained-child'
        else:
            assert result['code'] == ('agent_output_invalid' if failure else 'result_delivery_unsupported')
            assert result['request_id']
            artifact = result['partial']['artifact']
            retained = read_artifact(artifact, authority.parent_context.agent.session_id)
            if failure:
                assert result['session_id'] == 'retained-child'
                assert retained['partial'] == payload
            else:
                assert json.loads(retained) == payload


@pytest.mark.parametrize('remove,expected', [(False, 'artifact_changed'), (True, 'result_delivery_unsupported')])
def test_staged_terminal_artifact_failure_keeps_original_receipt(tmp_path, monkeypatch, remove, expected):
    from pathlib import Path
    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    authority = CellAuthority('fixture')
    authority.parent_context.agent.session_id = 'rpc-owner'
    effects = []
    def dispatch(*args):
        effects.append('once')
        return json.dumps({'answer': 'x' * (300 * 1024)})
    authority._invoke = dispatch
    with socket_host(authority) as (address, state, counter):
        original = state._response
        tampered = []
        def response(record):
            value = original(record)
            if value.get('status') == 'completed' and not tampered:
                prepared = value['result']['_hermes_rpc_artifact']
                path = Path(prepared['path'])
                path.unlink() if remove else path.write_text('changed after staging')
                tampered.append(prepared['id'])
            return value
        state._response = response
        code = client_source() + '\n_bind_cell(' + repr(authority.identity()) + ''')
try:
    _call('effect', {})
except ToolCallError as error:
    print(json.dumps({'code': error.code, 'receipt': error.receipt, 'record': error.record}))
else:
    raise AssertionError('undelivered result became success')
'''
        env = dict(os.environ, HERMES_RPC_SOCKET=f'tcp://{address[0]}:{address[1]}', HERMES_RPC_TOKEN='token')
        process = subprocess.run([sys.executable, '-c', code], env=env, text=True, capture_output=True, timeout=15, check=False)
        assert process.returncode == 0, process.stderr
        result = json.loads(process.stdout)
        assert result['code'] == expected
        assert result['receipt']['request_id'] == next(iter(state.records.values())).request['request_id']
        assert result['record']['retry_safe'] is False
        assert result['record']['artifact']['id'] == tampered[0]
        assert effects == ['once'] and counter == [1]


@pytest.mark.parametrize('invalid_partial', [False, True])
def test_error_delivery_failure_still_settles_original_typed_record(tmp_path, monkeypatch, invalid_partial):
    from tools.code_execution_acceptance import Acceptance, RPCState
    from tests.tools.test_code_rpc_v2 import request_for
    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    authority = CellAuthority('fixture')
    authority.parent_context.agent.session_id = 'rpc-owner'
    partial = {'not-json'} if invalid_partial else {'text': 'x' * (300 * 1024)}
    def fail(_):
        if not invalid_partial:
            authority.parent_context.deadline = 0
        raise AgentOutputError('Provider returned invalid output', session_id='retained-child', partial=partial)
    record = Acceptance(request_for(authority), authority)
    authority.begin_wait()
    RPCState._execute(record, fail)
    response = RPCState._response(record)
    assert record.done.is_set() and response['status'] == 'error'
    assert response['code'] == 'agent_output_invalid' and response['session_id'] == 'retained-child'
    assert response['receipt']['request_id'] == record.request['request_id']
    assert response['delivery_error']['retry_safe'] is False
    assert len(json.dumps(response).encode()) <= INLINE_BYTES
    if not invalid_partial:
        retained = read_artifact(response['partial']['artifact'], 'rpc-owner')
        assert retained['partial'] == partial
