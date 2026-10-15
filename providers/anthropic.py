"""Public opt-in native Messages factory contract; no provider-specific routing policy."""
from __future__ import annotations

from dataclasses import dataclass, field
from inspect import getattr_static
from typing import Any, Callable, ContextManager, Protocol


class ProviderContractError(RuntimeError):
    """Terminal local provider rejection. Never retry, rotate, downgrade or fail over.

    Messages must be safe to display: do not include credentials or raw SDK errors.
    """


def raise_if_provider_contract_error(error: BaseException) -> None:
    """Preserve rejection through wrappers that retain their original cause."""
    seen: set[int] = set()
    current: BaseException | None = error
    while current is not None and id(current) not in seen:
        if isinstance(current, ProviderContractError):
            raise current
        seen.add(id(current))
        current = current.__cause__ or current.__context__


class AnthropicMessages(Protocol):
    def create(self, **kwargs: Any) -> Any: ...
    def stream(self, **kwargs: Any) -> ContextManager[Any]: ...


class AnthropicClient(Protocol):
    """Native SDK surface consumed by Hermes (not an OpenAI chat facade).

    ``_client`` exposes the HTTP client for watchdog socket shutdown. Stream
    objects must retain official SDK events, response and final-message APIs.
    """
    messages: AnthropicMessages
    _client: Any
    api_key: Any
    base_url: Any

    def close(self) -> None: ...
    def is_closed(self) -> bool: ...
    def with_options(self, **kwargs: Any) -> AnthropicClient: ...


@dataclass(frozen=True)
class AnthropicClientOptions:
    """Resolved construction inputs. Never persist or log this credential-bearing object."""
    api_key: str | Callable[[], str] = field(repr=False)
    base_url: str | None = None
    timeout: float | None = None
    drop_context_1m_beta: bool = False

    def build_default(self, *, http_client: Any = None) -> AnthropicClient:
        """Build the official SDK with Hermes' auth/header/retry policy, without factory dispatch.

        A factory can use dataclasses.replace to canonicalize its endpoint, and
        supply an owned HTTP client. Callable bearer credentials require the
        standard bearer-hook builder and cannot replace its HTTP transport.
        """
        from agent.anthropic_adapter import _build_anthropic_sdk_client
        return _build_anthropic_sdk_client(
            self.api_key, self.base_url, self.timeout,
            drop_context_1m_beta=self.drop_context_1m_beta, http_client=http_client,
        )


def build_provider_anthropic_client(provider: str | None, options: AnthropicClientOptions) -> AnthropicClient:
    """Single native construction boundary; None explicitly declines customization."""
    from providers import get_provider_profile
    try:
        profile = get_provider_profile(provider) if provider else None
        binding = provider_client_binding(provider, profile=profile)
        factory = binding[0][2]
        if factory is not None:
            client = factory(options)
            if client is not None:
                if not callable(getattr(getattr(client, "messages", None), "create", None)) or not callable(
                    getattr(getattr(client, "messages", None), "stream", None)
                ):
                    raise ProviderContractError("Native provider factory returned an incompatible Messages client")
                return bind_client_provenance(client, binding, native_factory=True)
    except ProviderContractError:
        raise
    except Exception:
        raise ProviderContractError("Native provider client factory failed; request blocked") from None
    return bind_client_provenance(options.build_default(), binding, native_factory=True)


_PROFILE_UNSET = object()


def provider_client_binding(provider: str | None, *, profile: Any = _PROFILE_UNSET) -> tuple:
    """Snapshot the selected declaration BEFORE construction; never include credentials."""
    from hermes_constants import get_hermes_home
    from providers import get_provider_profile
    if profile is _PROFILE_UNSET:
        profile = get_provider_profile(provider) if provider else None
    factory = profile.create_anthropic_client if profile is not None else None
    return ((str(get_hermes_home()), provider, factory),
            str(getattr(profile, "base_url", "") or ""), getattr(profile, "api_mode", None))


def client_provenance(client: Any) -> tuple | None:
    """Native auxiliary sync/async facades both retain the original leaf client."""
    # Metadata inspection must not invoke a probe stub's deliberately loud __getattr__.
    for obj in (client, getattr_static(client, "_real_client", None)):
        binding = getattr_static(obj, "_hermes_provider_provenance", None)
        if isinstance(binding, tuple) and len(binding) == 3:
            return binding
    return None


def bind_client_provenance(client: Any, binding: tuple | None, *, native_factory: bool = False) -> Any:
    """Carry an earlier snapshot, never relabel an already constructed native client.

    A generic router snapshot cannot certify an implemented native factory: only
    its actual dispatcher can. Untaggable clients remain usable but uncached.
    """
    from providers.base import ProviderProfile
    if client is None or binding is None or client_provenance(client) is not None:
        return client
    factory = binding[0][2]
    if not native_factory and factory is not None and getattr(factory, "__func__", None) is not ProviderProfile.create_anthropic_client:
        return client
    try:
        client._hermes_provider_provenance = binding
    except (AttributeError, TypeError):
        pass
    return client


def anthropic_factory_identity(provider: str | None) -> tuple[Any, ...]:
    """Scope and declaration identity prevent reuse across providers or profile reloads."""
    from hermes_constants import get_hermes_home
    from providers import get_provider_profile
    profile = get_provider_profile(provider) if provider else None
    # A bound method pins its owner and compares by owner/function identity. An
    # integer id alone can be recycled after a profile reload while the cached
    # client survives, accidentally reusing an ordinary client for a guarded one.
    factory = profile.create_anthropic_client if profile is not None else None
    return (str(get_hermes_home()), provider, factory)
