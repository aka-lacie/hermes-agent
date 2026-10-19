"""Worker-only routing: operator precedence, durable replay, scope and credential isolation."""
import hashlib
import hmac
import json
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from gateway.config import Platform, PlatformConfig
from gateway.platforms.event import MessageEvent, WebhookRuntimeOverride
from gateway.platforms.webhook import WebhookAdapter
from gateway.platforms.webhook_runtime import configured_override, snapshot_runtime, validate_overrides
from gateway.session_envelope import check_native_route, restore_native
from hermes_state_runtime import RuntimeStoreError
from tests.gateway.fixtures.webhook_route_authority import mount_authority


@pytest.mark.parametrize('route,platform,expected', [
    ({}, {}, None),
    ({'model': 'route', 'provider': 'route-p'}, {'model': 'platform', 'provider': 'platform-p'}, ('route', 'route-p')),
    ({'model': 'route'}, {'model': 'platform', 'provider': 'platform-p'}, ('route', 'platform-p')),
    ({'provider': 'route-p'}, {'model': 'platform'}, ('platform', 'route-p')),
    ({'model': None, 'provider': '  '}, {'model': 'platform'}, ('platform', 'global-p')),
    ({'provider': 'route-p'}, {}, ('global-m', 'route-p')),
    ({'model': 'route'}, {}, ('route', 'global-p')),
])
def test_fieldwise_precedence(route, platform, expected, monkeypatch):
    monkeypatch.setattr('gateway.run._load_gateway_config', lambda: {
        'model': {'default': 'global-m', 'provider': 'global-p'}})
    selected = snapshot_runtime(configured_override(route, platform))
    assert selected == (dict(zip(('model', 'provider'), expected)) if expected else None)


@pytest.mark.parametrize('field', ['model', 'provider'])
@pytest.mark.parametrize('value', [False, 0, ['x'], {'name': 'x'}])
def test_bad_config_rejected_at_static_and_dynamic_boundaries(field, value):
    route = {'secret': 'owned', field: value}
    adapter = WebhookAdapter(PlatformConfig(extra={'secret': 'owned'}))
    with pytest.raises(ValueError, match=field):
        adapter._validate_route('fixture', route)
    assert not adapter._dynamic_route_allowed('fixture', route)
    with pytest.raises(ValueError, match=field):
        WebhookAdapter(PlatformConfig(extra={field: value}))


@pytest.mark.parametrize('mode', ['deliver_only', 'cron_job'])
@pytest.mark.parametrize('field', ['model', 'provider'])
def test_non_worker_rejects_route_pins_but_ignores_platform_defaults(mode, field):
    with pytest.raises(ValueError, match='detached agent'):
        validate_overrides({mode: True, field: 'explicit'})
    assert configured_override({mode: True}, {field: 'default'}) is None


def test_both_platform_spellings_and_profile_scoped_global(tmp_path, monkeypatch):
    from gateway.run import _profile_runtime_scope
    primary = tmp_path / 'primary'
    routed = tmp_path / 'routed'
    for path, provider in ((primary, 'primary-p'), (routed, 'routed-p')):
        path.mkdir()
        (path / 'config.yaml').write_text(json.dumps({'model': {'default': provider + '-m', 'provider': provider}}))
    monkeypatch.setenv('HERMES_HOME', str(primary))
    config = PlatformConfig.from_dict({'model': 'top', 'extra': {'model': 'nested'}})
    override = configured_override({}, config.extra)
    with _profile_runtime_scope(primary):
        with _profile_runtime_scope(routed):
            assert snapshot_runtime(override) == {'model': 'nested', 'provider': 'routed-p'}
        assert snapshot_runtime(override) == {'model': 'nested', 'provider': 'primary-p'}


def _request(text='hello'):
    body = json.dumps({'text': text, 'model': 'forged', 'provider': 'forged'}).encode()
    return body, {'X-GitHub-Delivery': 'runtime-one', 'X-Hub-Signature-256':
                  'sha256=' + hmac.new(b'owned-secret', body, hashlib.sha256).hexdigest()}


def _record(adapter):
    authority = adapter._message_handler.__self__.session_authority
    row = authority.db._read_one('SELECT * FROM session_admissions WHERE request_id=?', ('runtime-one',))
    return authority, row, json.loads(row['payload_json'])


@pytest.mark.asyncio
@pytest.mark.parametrize('legacy', [False, True])
async def test_replay_recovery_and_automation_retain_original_pair(tmp_path, monkeypatch, legacy):
    import gateway.run as gateway_run
    current = {'model': {'default': 'global-before', 'provider': 'provider-before'}}
    monkeypatch.setattr(gateway_run, '_load_gateway_config', lambda: current)
    route = {'prompt': '{text}', 'deliver': 'log'}
    config = PlatformConfig(enabled=True, extra={'secret': 'owned-secret', 'routes': {'fixture': route}})
    if not legacy:
        config.extra['model'] = 'worker-before'
    adapter = WebhookAdapter(config)
    app = web.Application()
    mount_authority(app, adapter)
    app.router.add_post('/webhooks/{route_name}', adapter._handle_webhook)
    async with TestClient(TestServer(app)) as client:
        body, headers = _request()
        assert (await client.post('/webhooks/fixture', data=body, headers=headers)).status == 202
        authority, row, payload = _record(adapter)
        expected = None if legacy else {'model': 'worker-before', 'provider': 'provider-before'}
        assert payload['native_text_v1'].get('webhook_runtime') == expected
        event = restore_native(payload, authority.runner)
        assert event.webhook_runtime_override == (WebhookRuntimeOverride(**expected) if expected else None)
        current['model'] = {'default': 'global-after', 'provider': 'provider-after'}
        config.extra.update(model='worker-after', provider='provider-after')
        retry = await client.post('/webhooks/fixture', data=body, headers=headers)
        assert retry.status == 200, await retry.text()
        assert _record(adapter)[2] == payload
        # A fresh adapter reconstructs the pin from the durable admission, not a local cache.
        replacement = WebhookAdapter(config)
        runner = authority.runner
        runner.adapters[Platform.WEBHOOK] = replacement
        runner._wire_adapter_handlers(replacement)
        replacement.gateway_runner = runner
        from gateway.platforms.webhook_runtime import retained_runtime
        assert retained_runtime(runner, event.source) == expected
        await check_native_route(runner, payload, row['target_session_id'], event.source, replacement)
        # Later process completions use a separate admission but the original worker selection.
        entry = runner.session_store.lookup_by_session_key(payload['native_text_v1']['route'])
        completion = MessageEvent(text='process finished', source=event.source, internal=True,
            metadata={'gateway_session_key': entry.session_key, 'gateway_session_id': entry.session_id})
        await authority.admit_automation(replacement, completion, 'process:owned:1')
        assert retained_runtime(runner, completion.source) == expected
        # Neither a route edit nor a rotated signing credential is silently authorized by a pin.
        route['prompt'] = 'changed'
        with pytest.raises(RuntimeStoreError):
            await check_native_route(runner, payload, row['target_session_id'], event.source, replacement)
        route['prompt'] = '{text}'
        replacement._global_secret = 'rotated'
        with pytest.raises(RuntimeStoreError):
            await check_native_route(runner, payload, row['target_session_id'], event.source, replacement)


@pytest.mark.asyncio
async def test_provider_runtime_is_selected_before_default_and_pool_is_retained(monkeypatch):
    import gateway.run as gateway_run
    pin = {'model': 'claude-opus-5-5', 'provider': 'cliproxy-claude'}
    config = PlatformConfig(enabled=True, extra={'secret': 'owned-secret', 'routes': {
        'fixture': {'prompt': '{text}', **pin}}})
    adapter = WebhookAdapter(config)
    app = web.Application()
    mount_authority(app, adapter)
    app.router.add_post('/webhooks/{route_name}', adapter._handle_webhook)
    async with TestClient(TestServer(app)) as client:
        body, headers = _request()
        assert (await client.post('/webhooks/fixture', data=body, headers=headers)).status == 202
        authority, _, payload = _record(adapter)
        source = restore_native(payload, authority.runner).source
        pool = object()
        resolve = Mock(return_value={'provider': 'cliproxy-claude', 'requested_provider': 'cliproxy-claude',
            'api_key': 'owned-key', 'base_url': 'http://127.0.0.1:8080/v1', 'api_mode': 'anthropic_messages',
            'credential_pool': pool, 'request_overrides': {'temperature': 0}, 'capabilities': {'vision': True}})
        monkeypatch.setattr(gateway_run, '_resolve_runtime_agent_kwargs_for_provider', resolve)
        default = Mock(side_effect=AssertionError('global credentials must not be resolved'))
        monkeypatch.setattr(gateway_run, '_resolve_runtime_agent_kwargs', default)
        model, runtime = authority.runner._resolve_session_agent_runtime(source=source)
        resolve.assert_called_once_with('cliproxy-claude', target_model='claude-opus-5-5')
        assert model == pin['model'] and runtime['credential_pool'] is pool
        turn = authority.runner._resolve_turn_agent_config('hello', model, runtime)
        assert turn['webhook_runtime_pinned'] is True
        assert turn['runtime']['credential_pool'] is pool
        assert '_webhook_runtime_pinned' not in turn['runtime']
        resolve.side_effect = RuntimeError('provider credentials revoked')
        with pytest.raises(RuntimeError, match='revoked'):
            authority.runner._resolve_session_agent_runtime(source=source)
        default.assert_not_called()


@pytest.mark.parametrize('selection', [None, {}, {'model': 'm'}, {'model': 'm', 'provider': 4},
    {'model': 'm', 'provider': 'p', 'api_key': 'forged'}, {'model': ' ', 'provider': 'p'}])
def test_invalid_selection_cannot_rehydrate(selection):
    from gateway.session import SessionSource
    event = MessageEvent(text='hello', source=SessionSource(platform=Platform.WEBHOOK, chat_id='x'))
    source = event.source.to_dict()
    source['is_bot'] = False
    payload = {'text': event.text, 'native_text_v1': {'source': source, 'event': {},
        'timestamp': event.timestamp.isoformat(), 'webhook_runtime': selection}}
    with pytest.raises(RuntimeStoreError, match='invalid_params'):
        restore_native(payload)


@pytest.mark.parametrize('pinned,activated', [(True, False), (True, True), (False, False)])
def test_cached_worker_pin_suppresses_fallback_and_evicts_switched_agent(pinned, activated):
    import threading
    from gateway.session_execution import GatewaySessionAgentMixin
    from gateway.run_config_loaders import GatewayConfigLoadersMixin
    from gateway.session import SessionSource
    old = SimpleNamespace(_fallback_activated=activated, _fallback_chain=[{'provider': 'other'}])
    fresh = object()
    runner = Mock()
    runner._agent_cache_lock = threading.Lock()
    runner._agent_cache = {'worker': old}
    runner._refresh_fallback_model.return_value = [{'provider': 'global-fallback'}]
    runner._apply_fallback_chain_to_agent = GatewayConfigLoadersMixin._apply_fallback_chain_to_agent
    owner = GatewaySessionAgentMixin()
    owner._runner = runner
    owner._ctx = SimpleNamespace(session_key='worker', session_id='owned', user_config={}, enabled_toolsets=[],
                                 source=SessionSource(platform=Platform.WEBHOOK, chat_id='owned'))
    owner._skip_context_files = Mock(return_value=False)
    owner._cached_sid_is_dead = Mock(return_value=('owned', False))
    owner._current_message_count = Mock(return_value=0)
    owner._lookup_cached_agent = Mock(return_value=owner._CachedAgentLookup(agent=old, reused=True))
    owner._release_evicted_agent = Mock()
    owner._build_fresh_agent = Mock(return_value=fresh)
    route = {'model': 'worker', 'runtime': {}, 'webhook_runtime_pinned': pinned}
    agent, reused = owner._resolve_turn_agent(route, 'webhook', '', 5, {}, {})
    if pinned:
        runner._refresh_fallback_model.assert_not_called()
        if activated:
            assert agent is fresh and not reused
            owner._release_evicted_agent.assert_called_once_with(old)
        else:
            assert agent is old and reused and not old._fallback_chain
    else:
        assert agent is old and reused
        assert old._fallback_chain == [{'provider': 'global-fallback'}]


@pytest.mark.asyncio
async def test_coalesced_admission_keeps_route_override(monkeypatch):
    from tests.gateway.test_webhook_coalescing import _capture_admissions, _mock_request, _payload
    from gateway.platforms.webhook import _INSECURE_NO_AUTH
    config = PlatformConfig(enabled=True, extra={'provider': 'platform-p', 'routes': {'pr': {
        'secret': _INSECURE_NO_AUTH, 'model': 'route-m', 'prompt': '{action}',
        'coalesce': {'key': 'pull_request.number', 'window_seconds': 60}}}})
    adapter = WebhookAdapter(config)
    admitted = _capture_admissions(adapter, monkeypatch)
    await adapter._handle_webhook(_mock_request(_payload(7), delivery_id='owned'))
    assert admitted == []
    await adapter.disconnect()
    assert len(admitted) == 1
    assert admitted[0].webhook_runtime_override == WebhookRuntimeOverride('route-m', 'platform-p')
