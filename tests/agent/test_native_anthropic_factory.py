"""Native factory lifecycle and fail-closed contracts, with zero inference I/O."""
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from providers.anthropic import ProviderContractError, anthropic_factory_identity
from providers.base import ProviderProfile

PROVIDER = "native-factory-test"
BASE = "http://127.0.0.1:8080/v1"
MODEL = "claude-opus-4-6"


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    import socket
    def blocked(*args, **kwargs):
        pytest.fail("native factory tests must not perform socket or DNS I/O")
    monkeypatch.setattr(socket.socket, "connect", blocked)
    monkeypatch.setattr(socket.socket, "connect_ex", blocked)
    monkeypatch.setattr(socket, "getaddrinfo", blocked)


@pytest.fixture
def install(monkeypatch):
    import providers
    profiles = {}
    monkeypatch.setattr(providers, "get_provider_profile", lambda name: profiles.get(name))
    return profiles


def rejected(options):
    raise ProviderContractError("route blocked")


def profile(factory=rejected):
    class Native(ProviderProfile):
        def create_anthropic_client(self, options):
            return factory(options)
    return Native(PROVIDER, api_mode="anthropic_messages", anthropic_native=True, base_url=BASE)


def agent():
    from agent.client_lifecycle import ClientLifecycleMixin
    class Harness(ClientLifecycleMixin):
        def _try_refresh_anthropic_client_credentials(self):
            return False
        def _anthropic_log_context(self):
            return "test"
    obj = Harness()
    obj.provider, obj.model, obj.api_mode = PROVIDER, MODEL, "anthropic_messages"
    obj.api_key = obj._anthropic_api_key = "synthetic-test-key"
    obj.base_url = obj._anthropic_base_url = BASE
    obj.quiet_mode = True
    return obj


@pytest.mark.parametrize("path", ["init", "switch", "restore", "fallback", "direct", "request", "rebuild", "aux-wrap", "custom"])
def test_all_native_construction_paths_reach_factory(install, monkeypatch, path):
    from agent import agent_init, agent_runtime_helpers as runtime, auxiliary_client as aux
    from agent.client_lifecycle import _swap_fallback_clients
    from agent import anthropic_adapter
    calls = []
    def factory(options):
        calls.append(options)
        raise ProviderContractError("route blocked")
    install[PROVIDER] = profile(factory)
    monkeypatch.setattr(anthropic_adapter, "_build_anthropic_sdk_client", lambda *a, **k: pytest.fail("unguarded SDK"))
    obj = agent()
    with pytest.raises(ProviderContractError, match="route blocked"):
        if path == "init":
            agent_init._init_anthropic_client(obj, obj.api_key, BASE, 10)
        elif path == "switch":
            runtime._build_switched_client(obj, PROVIDER, obj.api_key, BASE, "anthropic_messages", PROVIDER)
        elif path == "restore":
            runtime._build_anthropic_client_from_runtime(obj, {"anthropic_api_key": obj.api_key,
                "anthropic_base_url": BASE, "is_anthropic_oauth": False})
        elif path == "fallback":
            _swap_fallback_clients(obj, SimpleNamespace(api_key=obj.api_key), PROVIDER, MODEL, BASE, "anthropic_messages")
        elif path == "direct":
            obj._build_direct_anthropic_client(obj.api_key, BASE)
        elif path == "request":
            obj._create_request_anthropic_client(reason="test")
        elif path == "rebuild":
            obj._rebuild_anthropic_client()
        elif path == "aux-wrap":
            aux._maybe_wrap_anthropic(SimpleNamespace(), MODEL, obj.api_key, BASE, "anthropic_messages", PROVIDER)
        else:
            monkeypatch.setattr(aux, "_resolve_custom_runtime", lambda: (BASE, obj.api_key, "anthropic_messages"))
            monkeypatch.setattr(aux, "_normalize_main_runtime", lambda value: {"provider": PROVIDER, "base_url": BASE})
            monkeypatch.setattr(aux, "_read_main_model_for_aux", lambda: MODEL)
            aux._try_custom_endpoint()
    assert len(calls) == 1
    assert calls[0].base_url == BASE


def test_factory_none_and_unexpected_failure_are_distinct(install, monkeypatch):
    from agent import anthropic_adapter as adapter
    raw = Mock(return_value=object())
    monkeypatch.setattr(adapter, "_build_anthropic_sdk_client", raw)
    install[PROVIDER] = profile(lambda options: None)
    assert adapter.build_anthropic_client("test", BASE, provider=PROVIDER) is raw.return_value
    raw.reset_mock()
    def broken(options):
        raise ValueError("SECRET-like-exception-data")
    install[PROVIDER] = profile(broken)
    with pytest.raises(ProviderContractError) as error:
        adapter.build_anthropic_client("test", BASE, provider=PROVIDER)
    assert "SECRET" not in str(error.value)
    raw.assert_not_called()


def test_request_cache_identity_changes_for_provider_and_home(install, tmp_path):
    from hermes_constants import set_hermes_home_override, reset_hermes_home_override
    def factory(options):
        return SimpleNamespace(messages=SimpleNamespace(create=lambda **k: None, stream=lambda **k: None),
                               close=lambda: None, is_closed=lambda: False)
    install[PROVIDER] = profile(factory)
    other = profile(factory)
    other.name = "another-native-factory"
    install[other.name] = other
    obj = agent()
    first = obj._create_request_anthropic_client(reason="test")
    obj._close_request_anthropic_client(first, reason="request_complete")
    assert obj._create_request_anthropic_client(reason="test") is first
    obj._close_request_anthropic_client(first, reason="request_complete")
    obj.provider = other.name
    second = obj._create_request_anthropic_client(reason="test")
    assert second is not first
    obj._close_request_anthropic_client(second, reason="request_complete")
    old_key = anthropic_factory_identity(other.name)
    token = set_hermes_home_override(tmp_path / "different-home")
    try:
        assert anthropic_factory_identity(other.name) != old_key
        assert obj._create_request_anthropic_client(reason="test") is not second
    finally:
        reset_hermes_home_override(token)


@pytest.mark.parametrize("boundary", ["transient", "ladder", "fallback-parameters", "stream-downgrade"])
def test_rejections_never_enter_recovery(boundary):
    from agent import auxiliary_client as aux
    from agent.auxiliary_fallback_recovery import send_with_parameter_rungs
    from agent.anthropic_adapter import create_anthropic_message
    error = ProviderContractError("stream not supported; connection reset; 401")
    def fail(*args, **kwargs):
        raise error
    with pytest.raises(ProviderContractError) as caught:
        if boundary == "transient":
            aux._is_transient_transport_error(error)
        elif boundary == "ladder":
            aux._start_recovery_ladder(error, None, {}, task="test", async_mode=False, route_info=None)
        elif boundary == "fallback-parameters":
            send_with_parameter_rungs(fail, None, {}, task="test")
        else:
            create_anthropic_message(SimpleNamespace(messages=SimpleNamespace(stream=fail,
                create=lambda **k: pytest.fail("downgrade"))), {})
    assert caught.value is error


def test_main_rejection_verdict_does_not_attempt_recovery():
    from agent.turn_api_error import handle_api_error
    obj = SimpleNamespace(thinking_callback=None)
    result = handle_api_error(obj, api_error=ProviderContractError("blocked"), _retry=None,
        thinking_spinner=None, messages=[], api_messages=[], api_kwargs={}, system_message="", active_system_prompt="",
        conversation_history=[], approx_tokens=0, retry_count=0, max_retries=3, compression_attempts=0,
        max_compression_attempts=3, api_call_count=1, api_request_id="test", api_start_time=0,
        effective_task_id="test", turn_id="test")
    assert result.action == "return"
    assert result.result["failure_reason"] == "provider_contract"
    assert result.result["failure_retryable"] is False


def test_factory_cache_identity_pins_owner_across_reload(install):
    import gc
    import weakref
    install[PROVIDER] = profile()
    owner = weakref.ref(install[PROVIDER])
    first = anthropic_factory_identity(PROVIDER)
    assert first == anthropic_factory_identity(PROVIDER)
    install[PROVIDER] = profile()  # An equal dataclass, but a different declaration owner.
    gc.collect()
    assert owner() is not None  # An integer id key would release the old owner.
    assert first != anthropic_factory_identity(PROVIDER)


def test_ordinary_cache_cannot_satisfy_guarded_provider(install, monkeypatch):
    from agent import anthropic_adapter
    ordinary = SimpleNamespace(messages=SimpleNamespace(create=Mock(), stream=Mock()),
                               close=Mock(), is_closed=lambda: False)
    raw_builder = Mock(return_value=ordinary)
    monkeypatch.setattr(anthropic_adapter, "_build_anthropic_sdk_client", raw_builder)
    obj = agent()
    obj.provider = "ordinary-unhooked"
    assert obj._create_request_anthropic_client(reason="test") is ordinary
    obj._close_request_anthropic_client(ordinary, reason="request_complete")
    install[PROVIDER] = profile()
    obj.provider = PROVIDER  # Same credential, endpoint and timeout.
    with pytest.raises(ProviderContractError):
        obj._create_request_anthropic_client(reason="test")
    assert raw_builder.call_count == 1
    ordinary.messages.create.assert_not_called()


@pytest.mark.parametrize("route", [PROVIDER, "auto", "custom"])
def test_auxiliary_cache_cannot_bypass_reloaded_factory(install, monkeypatch, route):
    from agent import auxiliary_client as aux, anthropic_adapter
    monkeypatch.setattr(aux, "_client_cache", {})
    monkeypatch.setattr(aux, "_pool_cache_hint", lambda *args, **kwargs: None)
    ordinary = SimpleNamespace(close=Mock())
    def resolve(*args, **kwargs):
        if PROVIDER not in install:
            return ordinary, MODEL
        return anthropic_adapter.build_anthropic_client("synthetic-test-key", BASE, provider=PROVIDER), MODEL
    monkeypatch.setattr(aux, "resolve_provider_client", resolve)
    kwargs = dict(model=MODEL, base_url=BASE, api_key="synthetic-test-key", api_mode="anthropic_messages",
                  main_runtime={"provider": PROVIDER, "model": MODEL, "base_url": BASE,
                                "api_key": "synthetic-test-key", "api_mode": "anthropic_messages"})
    assert aux._get_cached_client(route, **kwargs)[0] is ordinary
    install[PROVIDER] = profile()
    with pytest.raises(ProviderContractError):
        aux._get_cached_client(route, **kwargs)


def test_refresh_rejection_is_not_swallowed(install, monkeypatch):
    from agent.client_lifecycle import ClientLifecycleMixin
    from agent import anthropic_credentials
    install["anthropic"] = profile()
    obj = agent()
    obj.provider = "anthropic"
    obj._anthropic_base_url = "https://api.anthropic.com"
    obj._anthropic_client = SimpleNamespace(close=Mock())
    monkeypatch.setattr(anthropic_credentials, "resolve_anthropic_token", lambda **k: "new-synthetic-key")
    with pytest.raises(ProviderContractError):
        ClientLifecycleMixin._try_refresh_anthropic_client_credentials(obj)


def test_iteration_summary_rejection_remains_terminal(monkeypatch):
    from agent import chat_completion_helpers as helpers
    obj = SimpleNamespace(max_iterations=2, suppress_status_output=True, api_mode="anthropic_messages")
    def fail(*args, **kwargs):
        raise ProviderContractError("summary blocked")
    monkeypatch.setattr(helpers, "_iteration_summary_api_messages", lambda agent, messages: messages)
    monkeypatch.setitem(helpers._SUMMARY_ATTEMPT_BUILDERS, "anthropic_messages", lambda *args: fail)
    with pytest.raises(ProviderContractError, match="summary blocked"):
        helpers.handle_max_iterations(obj, [], 1)


def test_compression_rejection_cannot_activate_summary_fallback():
    from agent.context_compressor import ContextCompressor
    def fail(*args, **kwargs):
        raise ProviderContractError("compression blocked")
    obj = SimpleNamespace(_compression_cancelled=lambda: False, _summary_failure_cooldown_until=0,
        _previous_summary=None, _compute_summary_budget=lambda turns: 1024, tail_mode="legacy",
        _serialize_for_summary=lambda turns: "input", _bound_summary_input=lambda text: text,
        _summary_has_user_turn=True, _build_summary_prompt=lambda *args: "prompt", _call_summary_llm=fail,
        _on_summary_failure=lambda *args: pytest.fail("summary fallback"))
    with pytest.raises(ProviderContractError, match="compression blocked"):
        ContextCompressor._generate_summary(obj, [{"role": "user", "content": "hello"}])


def test_home_io_guard_is_active():
    with pytest.raises(AssertionError, match="TEST BUG"):
        Path("/home/sonya/.hermes/.native-factory-test-nonexistent").read_text()
