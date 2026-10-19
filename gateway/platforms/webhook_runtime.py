"""Owner-configured webhook worker routes; durable selection, fresh scoped credentials.

The destination chat is a separate conversation. Neither POST fields nor completion
metadata may select its model. Only the original signed worker admission owns this pin.
"""
from typing import Mapping

from gateway.platforms.event import WebhookRuntimeOverride
from hermes_state_runtime import RuntimeStoreError


def validate_overrides(config: Mapping) -> dict[str, str | None]:
    """Validate operator configuration without interpreting model/provider identifiers."""
    result = {}
    for key in ('model', 'provider'):
        value = config.get(key)
        if value is not None and not isinstance(value, str):
            raise ValueError(f'Webhook {key} must be a string or null')
        result[key] = value.strip() or None if value is not None else None
    if any(result.values()) and (config.get('deliver_only') or config.get('cron_job')):
        raise ValueError('Webhook model/provider overrides require a detached agent; not deliver_only or cron_job')
    return result


def configured_override(route: Mapping, platform: Mapping) -> WebhookRuntimeOverride | None:
    route_values = validate_overrides(route)
    defaults = validate_overrides(platform)
    if route.get('deliver_only') or route.get('cron_job'):
        return None
    values = {key: route_values[key] or defaults[key] for key in ('model', 'provider')}
    return WebhookRuntimeOverride(**values) if any(values.values()) else None


def snapshot_runtime(override: WebhookRuntimeOverride | None) -> dict | None:
    """Called inside native admission's routed profile scope; never persist credentials."""
    if override is None:
        return None
    if not isinstance(override, WebhookRuntimeOverride):
        raise RuntimeStoreError('invalid_params', field='webhook_runtime')
    from gateway.run import _load_gateway_config, _resolve_gateway_model
    config = _load_gateway_config()
    model_config = config.get('model') or {}
    from hermes_cli.runtime_provider import resolve_requested_provider
    provider = model_config.get('provider') if isinstance(model_config, dict) else None
    provider = override.provider or provider or resolve_requested_provider()
    if provider.lower() == 'auto':
        provider = _automatic_provider(model_config)
    model = override.model or _resolve_gateway_model(config) or _default_model(provider)
    return validate_selection({'model': model, 'provider': provider})


def _default_model(provider: str) -> str:
    """Use the ordinary catalog default, then a registered provider's own catalog."""
    from hermes_cli.models import get_default_model_for_provider
    from providers import get_provider_profile
    model = get_default_model_for_provider(provider)
    if model:
        return model
    profile = get_provider_profile(provider)
    models = profile.fallback_models if profile is not None else ()
    return models[0] if models else ''


def _automatic_provider(model_config) -> str:
    """Freeze automatic identity, not credentials or an authentication-time fallback.

    Match the runtime's local endpoint bypass without resolving/refreshing OAuth.
    Named custom identities from config stay named, rather than the custom transport.
    """
    from hermes_cli.auth import resolve_provider
    from hermes_cli.runtime_provider import _LOCAL_BYPASS_CLOUD_HOSTS, base_url_host_matches
    config = model_config if isinstance(model_config, dict) else {}
    base = str(config.get('base_url') or '').strip()
    if (base and str(config.get('provider') or 'auto').strip().lower() == 'auto'
            and not any(base_url_host_matches(base, host) for host in _LOCAL_BYPASS_CLOUD_HOSTS)):
        return 'custom'
    return resolve_provider('auto')


def validate_selection(selection: dict) -> dict[str, str]:
    if (not isinstance(selection, dict) or set(selection) != {'model', 'provider'}
            or any(not isinstance(value, str) or not value.strip() or value != value.strip()
                   for value in selection.values())):
        raise RuntimeStoreError('invalid_params', field='webhook_runtime')
    return dict(selection)


def retained_runtime(runner, source) -> dict[str, str] | None:
    """The original admission also pins automation continuations on this worker lane."""
    from gateway.config import Platform
    if source is None or source.platform != Platform.WEBHOOK:
        return None
    from gateway.session_authorities import all_authorities
    if not all_authorities(runner):
        return None
    from gateway.platforms.webhook_delivery import retained_envelope
    adapter = runner._adapter_for_source(source)
    envelope = retained_envelope(adapter, source.chat_id)
    selection = envelope.get('webhook_runtime')
    return validate_selection(selection) if selection is not None else None
