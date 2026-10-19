"""Review regressions: compression constructor boundaries and partial webhook pins."""
import inspect
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from gateway.config import Platform
from gateway.platforms.webhook_runtime import configured_override, snapshot_runtime
from gateway.session import SessionSource


@pytest.mark.asyncio
@pytest.mark.parametrize('builder', ['hygiene', 'manual'])
async def test_compression_builders_accept_pinned_worker_runtime(monkeypatch, builder):
    import gateway.run as gateway_run
    import run_agent
    from gateway.run_turn_prepare import GatewayTurnPrepareMixin
    from gateway.run_turn_hygiene import GatewayTurnHygieneMixin
    from gateway.slash_commands_session import GatewaySessionCommandsMixin
    monkeypatch.setattr('gateway.platforms.webhook_runtime.retained_runtime', lambda runner, source: {
        'model': 'worker-model', 'provider': 'worker-provider'})
    pool = object()
    monkeypatch.setattr(gateway_run, '_resolve_runtime_agent_kwargs_for_provider', lambda *args, **kwargs: {
        'provider': 'custom', 'requested_provider': 'worker-provider', 'api_key': 'owned-key',
        'base_url': 'http://127.0.0.1:9/v1', 'api_mode': 'chat_completions', 'credential_pool': pool})
    owner = SimpleNamespace(_session_db=SimpleNamespace(get_session=AsyncMock(return_value=None), _db=object()))
    model, runtime = GatewayTurnPrepareMixin._resolve_session_agent_runtime(
        owner, source=SessionSource(platform=Platform.WEBHOOK, chat_id='owned'), user_config={})
    assert runtime['_webhook_runtime_pinned'] is True
    original = dict(runtime)
    # Validate against the REAL constructor signature, not a permissive Mock(**kwargs).
    signature = inspect.signature(run_agent.AIAgent)
    calls = []
    def construct(**kwargs):
        signature.bind(**kwargs)
        calls.append(kwargs)
        return SimpleNamespace()
    monkeypatch.setattr(run_agent, 'AIAgent', construct)
    monkeypatch.setattr('hermes_cli.config.load_config', dict)
    monkeypatch.setattr(gateway_run, '_seed_hygiene_system_prompt', lambda *args: None)
    if builder == 'hygiene':
        await GatewayTurnHygieneMixin._hmwa_hygiene_build_agent(owner, model, runtime,
                                                              SimpleNamespace(session_id='owned'))
    else:
        await GatewaySessionCommandsMixin._build_manual_compression_agent(owner, 'owned', model, runtime)
    assert len(calls) == 1
    assert calls[0]['model'] == 'worker-model'
    assert calls[0]['api_key'] == 'owned-key' and calls[0]['credential_pool'] is pool
    assert '_webhook_runtime_pinned' not in calls[0]
    assert runtime == original  # Do not unpin the worker's subsequent live turn.


@pytest.mark.parametrize('configured,env,expected', [(None, 'nous', 'nous'), ('anthropic', 'nous', 'anthropic')])
def test_model_only_pin_inherits_normal_provider_env_fallback(monkeypatch, configured, env, expected):
    import gateway.run as gateway_run
    from hermes_cli import runtime_provider
    config = {'model': {'default': 'global-model'}}
    if configured:
        config['model']['provider'] = configured
    monkeypatch.setattr(gateway_run, '_load_gateway_config', lambda: config)
    monkeypatch.setattr(runtime_provider, '_get_model_config', lambda: config['model'])
    monkeypatch.setenv('HERMES_INFERENCE_PROVIDER', env)
    assert snapshot_runtime(configured_override({'model': 'worker-model'}, {})) == {
        'model': 'worker-model', 'provider': expected}


def test_provider_only_pin_uses_ordinary_catalog_default(monkeypatch):
    import gateway.run as gateway_run
    from hermes_cli.models import get_default_model_for_provider
    monkeypatch.setattr(gateway_run, '_load_gateway_config', lambda: {'model': {}})
    expected = get_default_model_for_provider('anthropic')
    assert expected
    assert snapshot_runtime(configured_override({'provider': 'anthropic'}, {})) == {
        'model': expected, 'provider': 'anthropic'}


def test_provider_only_plugin_pin_uses_registered_catalog(monkeypatch):
    import gateway.run as gateway_run
    import providers
    from providers.base import ProviderProfile
    monkeypatch.setattr(gateway_run, '_load_gateway_config', lambda: {'model': {'provider': 'anthropic'}})
    profile = ProviderProfile(name='cliproxy-claude', fallback_models=('claude-opus-5-5',))
    lookup = Mock(return_value=profile)
    monkeypatch.setattr(providers, 'get_provider_profile', lookup)
    assert snapshot_runtime(configured_override({'provider': 'cliproxy-claude'}, {})) == {
        'model': 'claude-opus-5-5', 'provider': 'cliproxy-claude'}
    lookup.assert_called_once_with('cliproxy-claude')


def test_provider_without_any_catalog_still_requires_explicit_model(monkeypatch):
    import gateway.run as gateway_run
    import providers
    from hermes_state_runtime import RuntimeStoreError
    monkeypatch.setattr(gateway_run, '_load_gateway_config', lambda: {'model': {}})
    monkeypatch.setattr(providers, 'get_provider_profile', lambda provider: None)
    with pytest.raises(RuntimeStoreError, match='invalid_params'):
        snapshot_runtime(configured_override({'provider': 'unknown-owned-provider'}, {}))
