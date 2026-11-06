"""Tool-heavy observation regressions for aka-lacie/hermes-plugins#3, under the bounded observe contract
(agent/session_views.py: newest N messages, capped previews, never raw arguments or multi-KB blobs)."""
import json

import pytest

from hermes_state import SessionDB
from tools.delegate_session_store import DelegateSessionStore


@pytest.fixture
def history(tmp_path):
    with SessionDB(db_path=tmp_path / 'state.db') as db:
        db.create_session('parent', 'cli')
        db.create_session('child', 'subagent', parent_session_id='parent',
                          model_config={'_delegate_from': 'parent'})
        store = DelegateSessionStore(db)
        store.register('parent', 'child', model='test', provider='test', assignment='Inspect')
        yield db, store


def test_tool_heavy_window_keeps_steps_and_headers_without_changing_history(history):
    db, store = history
    for i in range(5):
        db.append_message('child', 'assistant', '', tool_calls=[{
            'id': f'call-{i}', 'type': 'function', 'function': {
                'name': 'terminal', 'arguments': json.dumps({'command': f'cat file-{i}.txt'})}}])
        db.append_message('child', 'tool', f'HEADER-{i}\n' + 'x' * 10000 + f'\nSTATUS-{i}',
                          tool_name='terminal', tool_call_id=f'call-{i}')
    before = db.get_messages('child')
    session = store.get('parent', 'child')
    result = store.observe('parent', 'child', limit=20)
    assert len(result['messages']) == 10 and not result['truncated']  # every step fits the window
    for i in range(5):
        call, output = result['messages'][i * 2:i * 2 + 2]
        assert call['calls'] == [f'terminal({{"command":"cat file-{i}.txt"}})']
        assert output['tool'] == 'terminal' and output['kind'] == 'tool_result'
        assert len(output['text']) <= 160
        assert output['text'].startswith(f'HEADER-{i}') and output['text'].endswith(f'STATUS-{i}')
        assert output['chars'] == len(before[i * 2 + 1]['content'])
    assert len(json.dumps(result)) < 4000
    assert db.get_messages('child') == before
    assert store.get('parent', 'child') == session


def test_observe_previews_prose_and_arguments_without_their_payloads(history):
    db, store = history
    prose = 'Introduction\n' + 'p' * 3000 + '\nConclusion'
    db.append_message('child', 'assistant', prose, tool_calls=[{
        'function': {'name': 'write_file', 'arguments': json.dumps({
            'content': 'private payload' * 10000, 'path': '/project/DESIGN.md'})}},
        {'function': {'name': 'broken', 'arguments': '{invalid'}},
    ])
    result = store.observe('parent', 'child', max_chars=12000)  # max_chars is accepted; caps are fixed
    row = result['messages'][-1]
    assert row['text'].startswith('Introduction') and row['text'].endswith('Conclusion')
    assert len(row['text']) <= 300 and row['chars'] == len(prose)
    write, broken = row['calls']
    assert write.startswith('write_file({"path":"/project/DESIGN.md","content":"private payload')  # target first
    assert len(write) <= 120 and broken == 'broken({invalid)'
    assert 'private payload' * 8 not in json.dumps(result)
    assert len(json.dumps(result)) < 2000


def test_many_large_tool_calls_stay_bounded(history):
    db, store = history
    db.append_message('child', 'assistant', '', tool_calls=[{
        'function': {'name': 'terminal', 'arguments': {'command': 'echo ' + 'x' * 10000}}}
        for _ in range(100)])
    result = store.observe('parent', 'child')
    row = result['messages'][-1]
    assert len(row['calls']) == 3 and row['more_calls'] == 97
    assert all(len(call) <= 120 and call.startswith('terminal({"command":"echo xxx') for call in row['calls'])
    assert len(json.dumps(result)) < 1500
