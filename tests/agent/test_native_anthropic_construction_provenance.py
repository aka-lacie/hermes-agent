"""Construction provenance must predate cache ownership (independent review repro).
Events expose registry replacement; exercise bare, sync and async facade owners.
"""
from contextvars import copy_context
from queue import Queue
from threading import Event, Thread
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from providers.anthropic import ProviderContractError
from providers.base import ProviderProfile

BASE = 'http://127.0.0.1:8080/v1'
MODEL = 'claude-opus-4-6'
PROVIDER = 'review-race-fallback'


@pytest.mark.parametrize('replace_during_build', [False, True])
@pytest.mark.parametrize('facade', ['bare', 'sync', 'async'])
def test_cache_binding_records_factory_used_to_build(monkeypatch, replace_during_build, facade):
    import socket
    import providers
    from agent import auxiliary_client as aux, anthropic_adapter as adapter
    def no_io(*args, **kwargs):
        pytest.fail('race test must not open sockets or perform DNS')
    monkeypatch.setattr(socket.socket, 'connect', no_io)
    monkeypatch.setattr(socket.socket, 'connect_ex', no_io)
    monkeypatch.setattr(socket, 'getaddrinfo', no_io)
    # Use actual registry registration and profile lookup with isolated dictionaries.
    providers.list_providers()
    for name in ('_REGISTRY', '_SOURCES', '_ALIASES'):
        monkeypatch.setattr(providers, name, dict(getattr(providers, name)))
    monkeypatch.setattr(providers, '_PROVIDER_LIST_CACHE', None)
    monkeypatch.setattr(providers, '_sync_auth_registry', lambda: None)
    old = ProviderProfile(PROVIDER, base_url=BASE, api_mode='anthropic_messages', anthropic_native=True)
    providers.register_provider(old)
    assert providers.get_provider_profile(PROVIDER) is old
    monkeypatch.setattr(aux, '_client_cache', {})
    monkeypatch.setattr(aux, '_pool_cache_hint', lambda *a, **kw: None)
    monkeypatch.setattr(aux, '_try_main_provider_route', lambda *a, **kw: None)
    monkeypatch.setattr(aux, '_task_prefers_fast_model', lambda *a, **kw: False)
    monkeypatch.setattr(aux, '_warn_stale_openai_base_url', lambda *a, **kw: None)
    loop = SimpleNamespace(is_closed=lambda: False)
    monkeypatch.setattr(aux, '_current_event_loop', lambda: loop)
    entered, release = Event(), Event()
    ordinary = SimpleNamespace(messages=SimpleNamespace(create=Mock(), stream=Mock()),
                               base_url=BASE, close=Mock(), is_closed=lambda: False)
    builds = []
    def ordinary_builder(*args, **kwargs):
        builds.append('old')
        entered.set()  # Actual native factory dispatcher already chose old's default.
        assert release.wait(10), 'test controller failed to release builder'
        return ordinary
    monkeypatch.setattr(adapter, '_build_anthropic_sdk_client', ordinary_builder)
    guard_hits = []
    class Guarded(ProviderProfile):
        def create_anthropic_client(self, options):
            guard_hits.append(options)
            raise ProviderContractError('new fallback declaration rejects request')
    def fallback(*args, **kwargs):
        client = adapter.build_anthropic_client('synthetic', BASE, provider=PROVIDER)
        if facade != 'bare':
            client = aux.AnthropicAuxiliaryClient(client, MODEL, 'synthetic', BASE, provider=PROVIDER)
        return client, MODEL, f'fallback_chain[0]({PROVIDER})'
    monkeypatch.setattr(aux, '_try_configured_fallback_chain', fallback)
    kwargs = dict(model=MODEL, base_url=BASE, api_key='synthetic', task='compression', async_mode=facade == 'async',
                  main_runtime={'provider':'unavailable-main','model':MODEL,'base_url':BASE,'api_key':'synthetic'})
    result = Queue()
    def acquire():
        try:
            result.put(('ok', aux._get_cached_client('auto', **kwargs)))
        except BaseException as error:
            result.put(('error', error))
    ctx = copy_context()
    worker = Thread(target=lambda: ctx.run(acquire))
    worker.start()
    try:
        assert entered.wait(10), 'factory was not reached'
        if replace_during_build:
            guarded = Guarded(PROVIDER, base_url=BASE, api_mode='anthropic_messages', anthropic_native=True)
            providers.register_provider(guarded)
            assert providers.get_provider_profile(PROVIDER) is guarded
    finally:
        release.set()
        worker.join(10)
    assert not worker.is_alive()
    status, value = result.get(timeout=1)
    assert status == 'ok', value
    first = value[0]
    assert (first if facade == 'bare' else first._real_client) is ordinary
    assert first._hermes_aux_effective_provider == PROVIDER
    assert builds == ['old']
    if replace_during_build:
        assert not aux._client_cache  # Obsolete construction is returned only to its original caller.
        try:
            second = aux._get_cached_client('auto', **kwargs)
        except ProviderContractError as error:
            assert 'new fallback declaration' in str(error)
            assert len(guard_hits) == 1
        else:
            pytest.fail(f'new declaration bypassed: reused_old_client={second[0] is ordinary}, guarded_factory_calls={len(guard_hits)}')
    else:
        assert aux._get_cached_client('auto', **kwargs)[0] is first
        assert guard_hits == []
        assert builds == ['old']
        assert next(iter(aux._client_cache.values()))[2] is (loop if facade == 'async' else None)
    ordinary.close.assert_not_called()


@pytest.mark.parametrize('replace_during_build', [False, True])
@pytest.mark.parametrize('async_mode', [False, True])
def test_nous_refresh_carries_preconstruction_owner(monkeypatch, replace_during_build, async_mode):
    import providers
    from agent import auxiliary_client as aux
    profile = ProviderProfile('nous', base_url=BASE)
    profiles = {'nous': profile}
    monkeypatch.setattr(providers, 'get_provider_profile', profiles.get)
    monkeypatch.setattr(aux, '_client_cache', {})
    monkeypatch.setattr(aux, '_pool_cache_hint', lambda *a, **kw: None)
    monkeypatch.setattr(aux, '_resolve_nous_runtime_api', lambda **kw: ('synthetic', BASE))
    loop = SimpleNamespace(is_closed=lambda: False)
    monkeypatch.setattr(aux, '_current_event_loop', lambda: loop)
    fresh = SimpleNamespace(base_url=BASE, close=Mock())
    def construct(**kwargs):
        if replace_during_build:
            # A supported replacement while the old constructor still owns this attempt.
            profiles['nous'] = ProviderProfile('nous', base_url=BASE)
        return fresh
    monkeypatch.setattr(aux, '_create_openai_client', construct)
    monkeypatch.setattr(aux, '_to_async_client', lambda *a, **kw: (fresh, MODEL))
    resolve = Mock(side_effect=AssertionError('unchanged refresh must be reused'))
    monkeypatch.setattr(aux, 'resolve_provider_client', resolve)
    runtime = {'provider': 'nous', 'model': MODEL}
    result = aux._refresh_nous_auxiliary_client(
        cache_provider='auto', model=MODEL, async_mode=async_mode,
        api_key='synthetic', main_runtime=runtime, lookup_task='compression',
    )
    assert result[0] is fresh
    if replace_during_build:
        assert not aux._client_cache
    else:
        assert aux._get_cached_client('auto', async_mode=async_mode, api_key='synthetic',
                                      main_runtime=runtime, task='compression')[0] is fresh
        assert next(iter(aux._client_cache.values()))[2] is (loop if async_mode else None)
    resolve.assert_not_called()
    fresh.close.assert_not_called()
