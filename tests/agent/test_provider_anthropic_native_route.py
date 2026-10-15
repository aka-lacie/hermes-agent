"""Provider-declared native Anthropic replay on arbitrary endpoints."""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

import providers
from agent.anthropic_message_convert import convert_messages_to_anthropic
from agent.anthropic_thinking_policy import (
    anthropic_thinking_route,
    native_anthropic_preserves_prior_thinking,
)
from agent.anthropic_thinking_replay import (
    apply_rejected_thinking_suppression,
    remember_rejected_thinking,
    tracks_rejected_thinking,
)
from agent.message_sanitization import stale_thinking_reaches_wire
from hermes_state import SessionDB
from providers.base import ProviderProfile


NATIVE_PROVIDER = "test-provider-declared-anthropic-native"
RELAY_PROVIDER = "test-provider-anthropic-compatible-relay"
LOCAL_MESSAGES_URL = "http://127.0.0.1:8317/v1/messages"
MODEL = "claude-opus-4-6"


@pytest.fixture(autouse=True)
def _registered_profiles(monkeypatch):
    """Function-scoped provider state, including registration's auth/catalog mirrors."""
    from copy import deepcopy
    from hermes_cli import auth, auth_plugin_providers, models_catalog_static

    # Complete ordinary discovery before installing test profiles; restore every mutated collection,
    # not just our names (registration can add aliases and populate previously unloaded mirrors).
    providers.list_providers()
    for module, names in (
        (providers, ("_REGISTRY", "_ALIASES", "_SOURCES", "_HOME_LAYERS",
                     "_NAMED_CUSTOM_MEMO", "_NAMED_CUSTOM_MEMO_SIG")),
        (auth, ("PROVIDER_REGISTRY",)),
        (auth_plugin_providers, ("PLUGIN_MIRRORED_PROVIDERS",)),
        (models_catalog_static, ("CANONICAL_PROVIDERS", "_canonical_slugs", "_PROVIDER_LABELS")),
    ):
        for name in names:
            monkeypatch.setattr(module, name, deepcopy(getattr(module, name)))
    monkeypatch.setattr(providers, "_PROVIDER_LIST_CACHE", None)
    providers.register_provider(ProviderProfile(
        name=NATIVE_PROVIDER,
        aliases=(f"{NATIVE_PROVIDER}-alias",),
        api_mode="anthropic_messages",
        base_url=LOCAL_MESSAGES_URL,
        env_vars=("TEST_NATIVE_API_KEY",),
        anthropic_native=True,
    ))
    providers.register_provider(ProviderProfile(
        name=RELAY_PROVIDER,
        api_mode="anthropic_messages",
        base_url=LOCAL_MESSAGES_URL,
    ))


def _signed_assistant(label: str = "thought") -> dict:
    return {
        "role": "assistant",
        "content": "answer",
        "reasoning": label,
        "anthropic_content_blocks": [
            {"type": "thinking", "thinking": label, "signature": f"sig-{label}"},
            {"type": "redacted_thinking", "data": f"redacted-{label}"},
            {"type": "text", "text": "answer"},
        ],
    }


def _assistant_block_types(converted: list[dict]) -> list[str]:
    return [
        str(block.get("type"))
        for message in converted
        if message.get("role") == "assistant" and isinstance(message.get("content"), list)
        for block in message["content"]
        if isinstance(block, dict)
    ]


def test_profile_declaration_is_authoritative_for_custom_localhost_and_alias():
    assert anthropic_thinking_route(LOCAL_MESSAGES_URL, MODEL, NATIVE_PROVIDER) == "native"
    assert anthropic_thinking_route(
        LOCAL_MESSAGES_URL, MODEL, f"{NATIVE_PROVIDER}-alias"
    ) == "native"
    assert native_anthropic_preserves_prior_thinking(
        LOCAL_MESSAGES_URL, MODEL, NATIVE_PROVIDER
    ) is True

    messages = [{"role": "user", "content": "question"}, _signed_assistant()]
    _, converted = convert_messages_to_anthropic(
        messages, base_url=LOCAL_MESSAGES_URL, model=MODEL, provider=NATIVE_PROVIDER,
    )
    assert _assistant_block_types(converted) == ["thinking", "redacted_thinking", "text"]


def test_undeclared_custom_localhost_remains_conservative():
    assert anthropic_thinking_route(LOCAL_MESSAGES_URL, MODEL, RELAY_PROVIDER) == "third_party"
    assert anthropic_thinking_route(LOCAL_MESSAGES_URL, MODEL) == "third_party"
    assert native_anthropic_preserves_prior_thinking(
        LOCAL_MESSAGES_URL, MODEL, RELAY_PROVIDER
    ) is False

    messages = [{"role": "user", "content": "question"}, _signed_assistant()]
    _, converted = convert_messages_to_anthropic(
        messages, base_url=LOCAL_MESSAGES_URL, model=MODEL, provider=RELAY_PROVIDER,
    )
    assert _assistant_block_types(converted) == ["text"]


def test_declared_route_keeps_historical_and_tool_loop_replay_semantics():
    messages = [
        {"role": "user", "content": "first"},
        _signed_assistant("historical"),
        {"role": "user", "content": "run tool"},
        {
            **_signed_assistant("tool-loop"),
            "tool_calls": [{
                "id": "toolu_1", "type": "function",
                "function": {"name": "lookup", "arguments": "{}"},
            }],
        },
        {"role": "tool", "tool_call_id": "toolu_1", "content": "ok"},
    ]
    _, converted = convert_messages_to_anthropic(
        messages, base_url=LOCAL_MESSAGES_URL, model=MODEL, provider=NATIVE_PROVIDER,
    )
    assert _assistant_block_types(converted).count("thinking") == 2
    assert _assistant_block_types(converted).count("redacted_thinking") == 2

    # Older Claude generations retain only the active loop, exactly as native Anthropic did before
    # provider declarations existed. The declaration classifies the route; model policy still decides
    # whether closed historical turns survive.
    _, old_model_converted = convert_messages_to_anthropic(
        messages, base_url=LOCAL_MESSAGES_URL, model="claude-opus-4-1",
        provider=NATIVE_PROVIDER,
    )
    assert _assistant_block_types(old_model_converted).count("thinking") == 1
    assert _assistant_block_types(old_model_converted).count("redacted_thinking") == 1


def test_declared_native_enables_native_tool_cache_layout_on_custom_endpoint():
    from agent.agent_runtime_helpers import _direct_native_anthropic_tool_cache_capability

    agent = SimpleNamespace(
        provider=NATIVE_PROVIDER, base_url=LOCAL_MESSAGES_URL,
        api_mode="anthropic_messages", model=MODEL,
    )
    assert _direct_native_anthropic_tool_cache_capability(agent) is True
    agent.provider = RELAY_PROVIDER
    assert _direct_native_anthropic_tool_cache_capability(agent) is False


@pytest.mark.parametrize("provider", [NATIVE_PROVIDER, f"{NATIVE_PROVIDER}-alias"])
@pytest.mark.parametrize("cache_disabled", [False, True])
def test_upstream_cache_ownership_prevents_markers_without_changing_signed_replay(
    monkeypatch, provider, cache_disabled,
):
    from agent.agent_runtime_helpers import (
        anthropic_prompt_cache_policy, plan_cache_sections_for_destination,
    )
    from agent.anthropic_adapter import build_anthropic_kwargs

    monkeypatch.setattr(providers._REGISTRY[NATIVE_PROVIDER],
                        "anthropic_prompt_cache_upstream_owned", True, raising=False)
    agent = SimpleNamespace(provider=provider, base_url=LOCAL_MESSAGES_URL,
                            api_mode="anthropic_messages", model=MODEL,
                            _cache_disabled=cache_disabled, _custom_providers=[])
    assert anthropic_prompt_cache_policy(agent) == (False, False)
    messages = [{"role": "system", "content": "Stable system prefix"},
                {"role": "user", "content": "question"}, _signed_assistant()]
    tools = [{"type": "function", "function": {
        "name": "lookup", "description": "Lookup", "parameters": {"type": "object"},
    }}]
    planned, planned_tools = plan_cache_sections_for_destination(
        messages, tools, provider=provider, base_url=LOCAL_MESSAGES_URL,
        api_mode="anthropic_messages", model=MODEL,
        cache_disabled=cache_disabled, cache_ttl="1h",
    )
    wire = build_anthropic_kwargs(
        MODEL, planned, planned_tools, 128, None,
        base_url=LOCAL_MESSAGES_URL, provider=provider,
    )
    assert '"cache_control"' not in json.dumps(wire)
    assert _assistant_block_types(wire["messages"]) == ["thinking", "redacted_thinking", "text"]
    assert wire["messages"][-1]["content"] == messages[-1]["anthropic_content_blocks"]
    assert planned == messages and planned_tools == tools


@pytest.mark.parametrize("value", [False, None, "true", 1])
def test_only_explicit_upstream_cache_ownership_changes_native_defaults(monkeypatch, value):
    from agent.agent_runtime_helpers import anthropic_prompt_cache_policy

    assert ProviderProfile(name="ordinary").anthropic_prompt_cache_upstream_owned is False
    monkeypatch.setattr(providers._REGISTRY[NATIVE_PROVIDER],
                        "anthropic_prompt_cache_upstream_owned", value, raising=False)
    agent = SimpleNamespace(provider=NATIVE_PROVIDER, base_url=LOCAL_MESSAGES_URL,
                            api_mode="anthropic_messages", model=MODEL, _custom_providers=[])
    assert anthropic_prompt_cache_policy(agent) == (True, True)
    agent._cache_disabled = True
    assert anthropic_prompt_cache_policy(agent) == (False, False)


def test_declared_route_participates_in_rejection_healing_without_mutating_history():
    canonical = _signed_assistant("bad")
    request_copy = [{**canonical, "anthropic_content_blocks": list(canonical["anthropic_content_blocks"])}]
    agent = SimpleNamespace(
        api_mode="anthropic_messages",
        provider=NATIVE_PROVIDER,
        model=MODEL,
        base_url=LOCAL_MESSAGES_URL,
        session_id="session-native",
        _session_db=None,
        _persist_disabled=True,
        context_compressor=None,
    )
    assert tracks_rejected_thinking(agent) is True
    assert remember_rejected_thinking(agent, request_copy) == 2
    assert [block["type"] for block in request_copy[0]["anthropic_content_blocks"]] == ["text"]
    assert [block["type"] for block in canonical["anthropic_content_blocks"]] == [
        "thinking", "redacted_thinking", "text",
    ]

    rebuilt = [{**canonical, "anthropic_content_blocks": list(canonical["anthropic_content_blocks"])}]
    assert apply_rejected_thinking_suppression(agent, rebuilt) == 2
    assert [block["type"] for block in rebuilt[0]["anthropic_content_blocks"]] == ["text"]


def test_accounting_switch_fallback_and_restore_follow_current_provider_identity():
    agent = SimpleNamespace(
        api_mode="anthropic_messages", provider=NATIVE_PROVIDER,
        model=MODEL, base_url=LOCAL_MESSAGES_URL,
    )
    assert stale_thinking_reaches_wire(
        agent.api_mode, agent.provider, agent.model, agent.base_url
    ) is True

    # A fallback/model switch changes the existing provider identity field. Capability lookup follows
    # that route and therefore cannot retain stale native authority from the route being left.
    agent.provider = RELAY_PROVIDER
    assert stale_thinking_reaches_wire(
        agent.api_mode, agent.provider, agent.model, agent.base_url
    ) is False

    from agent.agent_runtime_helpers import _apply_primary_runtime_fields

    agent._transport_cache = {}
    _apply_primary_runtime_fields(agent, {
        "model": MODEL,
        "provider": NATIVE_PROVIDER,
        "requested_provider": NATIVE_PROVIDER,
        "base_url": LOCAL_MESSAGES_URL,
        "api_mode": "anthropic_messages",
        "api_key": "not-a-secret",
        "reasoning_echo_flag": False,
        "request_overrides": {},
        "client_kwargs": {},
    })
    assert stale_thinking_reaches_wire(
        agent.api_mode, agent.provider, agent.model, agent.base_url
    ) is True


def test_context_tail_budget_follows_provider_declaration():
    from agent.context_compressor import ContextCompressor

    native = ContextCompressor.__new__(ContextCompressor)
    native.api_mode = "anthropic_messages"
    native.provider = NATIVE_PROVIDER
    native.model = MODEL
    native.base_url = LOCAL_MESSAGES_URL
    native._session_db = None
    native._session_id = "session-native"
    native_budget = native._native_anthropic_budget()
    assert callable(native_budget)
    assert native_budget(_signed_assistant("accounted")) > 0

    relay = ContextCompressor.__new__(ContextCompressor)
    relay.api_mode = "anthropic_messages"
    relay.provider = RELAY_PROVIDER
    relay.model = MODEL
    relay.base_url = LOCAL_MESSAGES_URL
    assert relay._native_anthropic_budget() is None


def test_session_route_persistence_restores_provider_declaration_by_identity(tmp_path):
    db = SessionDB(db_path=tmp_path / "state.db")
    try:
        db.create_session("s-native", source="cli", model=MODEL)
        db.update_session_model(
            "s-native", MODEL, provider=NATIVE_PROVIDER,
            base_url=LOCAL_MESSAGES_URL, api_mode="anthropic_messages",
        )
        config = json.loads(db.get_session("s-native")["model_config"])
        route = config["gateway_runtime"]
        assert route == {
            "provider": NATIVE_PROVIDER,
            "base_url": LOCAL_MESSAGES_URL,
            "api_mode": "anthropic_messages",
        }
        assert native_anthropic_preserves_prior_thinking(
            route["base_url"], MODEL, route["provider"]
        ) is True
    finally:
        db.close()


def test_auxiliary_anthropic_adapter_forwards_provider_declaration():
    from agent.auxiliary_client import _AnthropicCompletionsAdapter

    adapter = _AnthropicCompletionsAdapter(
        MagicMock(), MODEL, base_url=LOCAL_MESSAGES_URL, provider=NATIVE_PROVIDER,
    )
    normalized = SimpleNamespace(
        content="ok", tool_calls=None, reasoning=None, finish_reason="stop",
    )
    response = SimpleNamespace(usage=None)
    with (
        patch("agent.anthropic_adapter.build_anthropic_kwargs", return_value={
            "model": MODEL, "messages": [], "max_tokens": 64,
        }) as build,
        patch("agent.anthropic_adapter.create_anthropic_message", return_value=response),
        patch("agent.transports.get_transport") as get_transport,
    ):
        get_transport.return_value.normalize_response.return_value = normalized
        adapter.create(messages=[{"role": "user", "content": "summarize"}])

    assert build.call_args.kwargs["provider"] == NATIVE_PROVIDER


@pytest.mark.parametrize("base_url,model,legacy_route", [
    ("https://api.kimi.com/coding", "kimi-k2.5", "kimi"),
    ("https://api.deepseek.com/anthropic", "deepseek-v4-pro", "deepseek"),
    ("https://api.minimax.io/anthropic", "MiniMax-M3", "minimax"),
    (LOCAL_MESSAGES_URL, "opaque-model-alias", "third_party"),
])
def test_declaration_outranks_endpoint_and_model_heuristics(base_url, model, legacy_route):
    assert anthropic_thinking_route(base_url, model, RELAY_PROVIDER) == legacy_route
    assert anthropic_thinking_route(base_url, model, NATIVE_PROVIDER) == "native"
    # Native validation, not Kimi/DeepSeek's unsigned echo rule.
    messages = [{"role": "user", "content": "question"}, {
        **_signed_assistant(),
        "anthropic_content_blocks": [
            {"type": "thinking", "thinking": "unsigned"},
            *_signed_assistant()["anthropic_content_blocks"],
        ],
    }]
    _, converted = convert_messages_to_anthropic(
        messages, base_url=base_url, model=model, provider=NATIVE_PROVIDER,
    )
    assert _assistant_block_types(converted) == ["text", "thinking", "redacted_thinking", "text"]

    from agent.agent_runtime_helpers import anthropic_prompt_cache_policy
    agent = SimpleNamespace(provider=NATIVE_PROVIDER, base_url=base_url,
                            api_mode="anthropic_messages", model=model, _custom_providers=[])
    assert anthropic_prompt_cache_policy(agent) == (True, True)
    agent._cache_disabled = True
    assert anthropic_prompt_cache_policy(agent) == (False, False)


@pytest.mark.parametrize("value", [False, None, "true", 1])
def test_non_boolean_declarations_fail_closed(monkeypatch, value):
    monkeypatch.setattr(providers._REGISTRY[NATIVE_PROVIDER], "anthropic_native", value)
    assert anthropic_thinking_route(LOCAL_MESSAGES_URL, MODEL, NATIVE_PROVIDER) == "third_party"


def _main_agent():
    from agent.transports.anthropic import AnthropicTransport
    transport = AnthropicTransport()
    return SimpleNamespace(
        model=MODEL, provider=NATIVE_PROVIDER, max_tokens=4096, reasoning_config=None,
        _is_anthropic_oauth=False, _anthropic_base_url=LOCAL_MESSAGES_URL,
        _get_transport=lambda: transport, _anthropic_preserve_dots=lambda: False,
        _prepare_anthropic_messages_for_api=lambda messages: messages,
        _interruptible_api_call=MagicMock(),
    )


def test_main_transport_and_summary_replay_through_real_builder():
    from agent.chat_completion_helpers import _build_anthropic_kwargs, _anthropic_summary_attempt
    agent = _main_agent()
    messages = [{"role": "user", "content": "question"}, _signed_assistant()]
    for provider, expected in ((NATIVE_PROVIDER, ["thinking", "redacted_thinking", "text"]),
                               (RELAY_PROVIDER, ["text"])):
        agent.provider = provider
        main = _build_anthropic_kwargs(agent, messages, None, None, {})
        assert _assistant_block_types(main["messages"]) == expected
        assert "provider" not in main  # internal policy metadata never reaches the SDK
        with (
            patch("agent.chat_completion_helpers._managed_summary_call") as call,
            patch("agent.chat_completion_helpers._summary_text", return_value="summary"),
        ):
            assert _anthropic_summary_attempt(agent, messages, "summary-test")(0) == "summary"
        summary = call.call_args.args[2]
        assert _assistant_block_types(summary["messages"]) == expected


def _aux_wire_types(client):
    """Stop only at the network seam; exercise the real wrapper, builder, and converter."""
    response = SimpleNamespace(content=[SimpleNamespace(type="text", text="ok")],
                               stop_reason="end_turn", usage=None)
    with patch("agent.anthropic_adapter.create_anthropic_message", return_value=response) as create:
        client.chat.completions.create(
            model=MODEL, messages=[{"role": "user", "content": "question"}, _signed_assistant()],
        )
    return _assistant_block_types(create.call_args.args[1]["messages"])


@pytest.mark.parametrize("custom_base,native", [
    (LOCAL_MESSAGES_URL, True),
    (LOCAL_MESSAGES_URL + "/", True),
    ("http://127.0.0.1:8318/v1/messages", False),
    ("http://127.0.0.1:8317/other", False),
])
def test_bare_custom_aux_authority_is_bound_to_exact_main_endpoint(monkeypatch, custom_base, native):
    from agent import auxiliary_client as aux
    runtime = dict(provider=NATIVE_PROVIDER, base_url=LOCAL_MESSAGES_URL, api_mode="anthropic_messages")
    monkeypatch.setattr(aux, "_resolve_custom_runtime", lambda: (custom_base, "test-key", "anthropic_messages"))
    monkeypatch.setattr(aux, "_normalize_main_runtime", lambda _: runtime)
    monkeypatch.setattr(aux, "_read_main_model_for_aux", lambda: MODEL)
    with patch("agent.anthropic_adapter.build_anthropic_client", return_value=MagicMock()):
        client, _ = aux._try_custom_endpoint()
    assert _aux_wire_types(client) == (["thinking", "redacted_thinking", "text"] if native else ["text"])


@pytest.mark.parametrize("other,same", [
    ("HTTPS://Example.COM:443/v1/", True),
    ("http://example.com/v1", False),
    ("https://example.com:444/v1", False),
    ("https://example.com/other", False),
    ("https://example.com/v1?route=b", False),
    ("https://example.com/v1#b", False),
    ("https://user@example.com/v1", False),
    ("https://example.com/v1//", False),
    ("https://example.com:bad/v1", False),
    ("https://[bad", False),
    ("example.com/v1", False),
    ("", False),
])
def test_endpoint_binding_normalizes_only_benign_url_differences(other, same):
    from agent.auxiliary_client import _same_normalized_base_url
    assert _same_normalized_base_url("https://example.com/v1", other) is same


def test_api_key_discovery_honors_declared_messages_wire(monkeypatch):
    from agent import auxiliary_client as aux
    from hermes_cli import auth
    monkeypatch.setattr(auth, "PROVIDER_REGISTRY", {NATIVE_PROVIDER: auth.PROVIDER_REGISTRY[NATIVE_PROVIDER]})
    monkeypatch.setattr(aux, "_is_provider_unhealthy", lambda _: False)
    monkeypatch.setattr(aux, "_get_aux_model_for_provider", lambda _: MODEL)
    monkeypatch.setattr(aux, "_select_pool_entry", lambda _: (False, None))
    monkeypatch.setattr(aux, "_normalize_main_runtime", lambda _: {})
    monkeypatch.setattr(auth, "resolve_api_key_provider_credentials", lambda _: {
        "api_key": "test-key", "base_url": LOCAL_MESSAGES_URL,
    })
    with patch("agent.anthropic_adapter.build_anthropic_client", return_value=MagicMock()):
        client, _ = aux._resolve_api_key_provider()
    assert isinstance(client, aux.AnthropicAuxiliaryClient)
    assert _aux_wire_types(client) == ["thinking", "redacted_thinking", "text"]


def test_inherited_custom_route_and_cache_follow_runtime_identity(monkeypatch):
    from agent import auxiliary_client as aux
    runtime = dict(provider=NATIVE_PROVIDER, base_url=LOCAL_MESSAGES_URL, api_mode="anthropic_messages",
                   api_key="test-key", model=MODEL)
    # A fresh cache owned by this test; never close or mutate clients owned by another test.
    monkeypatch.setattr(aux, "_client_cache", {})
    monkeypatch.setattr(aux, "_pool_cache_hint", lambda *a, **kw: ())
    with patch("agent.anthropic_adapter.build_anthropic_client", return_value=MagicMock()):
        native, _ = aux._get_cached_client("custom", model=MODEL, main_runtime=runtime)
        relay, _ = aux._get_cached_client("custom", model=MODEL,
                                         main_runtime={**runtime, "provider": RELAY_PROVIDER})
        explicit, _ = aux._get_cached_client("custom", model=MODEL, main_runtime=runtime,
                                            base_url="http://127.0.0.1:8318/v1/messages",
                                            api_key="test-key", api_mode="anthropic_messages")
    assert native is not relay
    assert _aux_wire_types(native) == ["thinking", "redacted_thinking", "text"]
    assert _aux_wire_types(relay) == ["text"]
    assert _aux_wire_types(explicit) == ["text"]


def test_loaded_declaration_is_home_scoped_and_hot_lookup_does_not_discover(tmp_path, monkeypatch):
    from hermes_constants import hermes_home_key
    from agent.anthropic_thinking_policy import provider_declares_anthropic_native
    a, b = tmp_path / "home-a", tmp_path / "home-b"
    profile = ProviderProfile(name="scoped-native", anthropic_native=True)
    monkeypatch.setattr(providers, "_HOME_LAYERS", {
        hermes_home_key(a): providers._HomeLayer(registry={profile.name: profile}),
        hermes_home_key(b): providers._HomeLayer(),
    })
    with patch.object(providers, "_discover_providers", side_effect=AssertionError("discovery")), \
         patch.object(providers, "_refresh_home_layer", side_effect=AssertionError("filesystem scan")):
        for home, expected in ((a, True), (b, False), (a, True)):
            monkeypatch.setenv("HERMES_HOME", str(home))
            assert provider_declares_anthropic_native(profile.name) is expected
        assert provider_declares_anthropic_native("custom:scoped-native") is False


def test_native_accounting_and_persisted_rejection_use_resolved_provider(tmp_path, monkeypatch):
    from copy import deepcopy
    from agent.turn_context import _preflight_request_tokens
    from tests.agent.test_anthropic_preserved_thinking_replay import (
        _agent, _assemble_and_wire, _patch_assembly_loop, _session_db, _signed_turn,
    )
    db = _session_db(tmp_path, "s1")
    try:
        _patch_assembly_loop(monkeypatch)
        route = (NATIVE_PROVIDER, LOCAL_MESSAGES_URL)
        agent = _agent(db, route=route)
        def history(size):
            return (_signed_turn("Q1", "A1", "historical", thinking="x" * size)
                    + _signed_turn("Q2", "A2", "latest")
                    + [{"role": "user", "content": "continue"}])
        def measure(owner, size):
            rows = history(size)
            preflight = _preflight_request_tokens(owner, deepcopy(rows), "")
            assembled, _ = _assemble_and_wire(owner, rows)
            tail = owner.context_compressor._walk_tail_budget(rows, 0, 10**9, 0, cut_at_break=False)[1]
            return preflight, assembled.approx_tokens, tail
        small, large = measure(agent, 1), measure(agent, 8000)
        delta = [b - a for a, b in zip(small, large)]
        assert delta[0] > 0 and len(set(delta)) == 1
        remember_rejected_thinking(agent, deepcopy(history(8000)))
        resumed = _agent(db, route=route)
        assert measure(resumed, 1) == measure(resumed, 8000)
        rows = deepcopy(history(8000))
        assert apply_rejected_thinking_suppression(resumed, rows) == 2
        assert "historical" not in repr(rows)
    finally:
        db.close()


def test_installed_plugin_declaration_reaches_runtime_and_auxiliary(tmp_path, monkeypatch):
    import sys
    from agent.auxiliary_client import resolve_provider_client
    from hermes_cli.runtime_provider import resolve_runtime_provider
    home = tmp_path / "plugin-home"
    plugin = home / "plugins" / "native-route"
    plugin.mkdir(parents=True)
    (plugin / "plugin.yaml").write_text("name: native-route\nkind: model-provider\n")
    (plugin / "__init__.py").write_text(
        "from providers import register_provider\n"
        "from providers.base import ProviderProfile\n"
        "register_provider(ProviderProfile(name='installed-native', aliases=('native-alias',), "
        "api_mode='anthropic_messages', anthropic_native=True, "
        f"base_url={LOCAL_MESSAGES_URL!r}, env_vars=('INSTALLED_NATIVE_API_KEY',)))\n"
    )
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("INSTALLED_NATIVE_API_KEY", "test-key")
    before = set(sys.modules)
    try:
        runtime = resolve_runtime_provider(requested="native-alias", target_model=MODEL)
        assert runtime["provider"] == "installed-native"
        assert runtime["api_mode"] == "anthropic_messages"
        with patch("agent.anthropic_adapter.build_anthropic_client", return_value=MagicMock()):
            client, _ = resolve_provider_client(runtime["provider"], MODEL, main_runtime=runtime)
        assert _aux_wire_types(client) == ["thinking", "redacted_thinking", "text"]
    finally:
        for name in set(sys.modules) - before:
            if name.startswith("_hermes_user_provider"):
                sys.modules.pop(name, None)


def test_registry_oauth_arm_carries_overridden_provider_identity(monkeypatch):
    from agent.auxiliary_client import resolve_provider_client
    profile = providers.get_provider_profile("minimax-oauth")
    monkeypatch.setattr(profile, "anthropic_native", True)
    with (
        patch("hermes_cli.auth.resolve_minimax_oauth_runtime_credentials", return_value={
            "api_key": lambda: "test-key", "base_url": LOCAL_MESSAGES_URL,
        }),
        patch("agent.anthropic_adapter.build_anthropic_client", return_value=MagicMock()),
    ):
        client, _ = resolve_provider_client("minimax-oauth", MODEL)
    assert _aux_wire_types(client) == ["thinking", "redacted_thinking", "text"]


@pytest.mark.parametrize("install_in_b", [False, True])
def test_native_authority_does_not_follow_stale_auth_rows_between_profile_homes(tmp_path, install_in_b):
    import sys
    from hermes_constants import set_hermes_home_override, reset_hermes_home_override
    from hermes_cli import auth
    from agent.anthropic_thinking_policy import provider_declares_anthropic_native
    from agent.agent_runtime_helpers import _direct_native_anthropic_tool_cache_capability

    name, alias = "profile-native-route", "profile-native-alias"
    a, b = tmp_path / "profile-a", tmp_path / "profile-b"
    for home, native in ((a, True), (b, False)):
        if home == b and not install_in_b:
            continue
        plugin = home / "plugins" / "model-providers" / "scoped-route"
        plugin.mkdir(parents=True)
        (plugin / "__init__.py").write_text(
            "from providers import register_provider\nfrom providers.base import ProviderProfile\n"
            f"register_provider(ProviderProfile(name={name!r}, aliases=({alias!r},), "
            f"anthropic_native={native!r}, api_mode='anthropic_messages', "
            f"base_url={LOCAL_MESSAGES_URL!r}, env_vars=('PROFILE_ROUTE_API_KEY',)))\n"
        )
    before = set(sys.modules)
    try:
        for home, native in ((a, True), (b, False), (a, True)):
            token = set_hermes_home_override(home)
            try:
                providers.get_provider_profile(name)  # real per-home discovery + auth mirroring
                auth.sync_plugin_provider_registry()
                # auth.PROVIDER_REGISTRY may retain A's metadata in B. Native authority MUST come
                # from the bound provider layer, not this process-global compatibility snapshot.
                for identity in (name, alias):
                    assert provider_declares_anthropic_native(identity) is native
                    agent = SimpleNamespace(provider=identity, model=MODEL, base_url=LOCAL_MESSAGES_URL,
                                            api_mode="anthropic_messages")
                    assert tracks_rejected_thinking(agent) is native
                    assert _direct_native_anthropic_tool_cache_capability(agent) is native
                    _, messages = convert_messages_to_anthropic(
                        [{"role": "user", "content": "question"}, _signed_assistant()],
                        base_url=LOCAL_MESSAGES_URL, model=MODEL, provider=identity,
                    )
                    assert _assistant_block_types(messages) == (
                        ["thinking", "redacted_thinking", "text"] if native else ["text"]
                    )
            finally:
                reset_hermes_home_override(token)
    finally:
        for module in set(sys.modules) - before:
            if module.startswith("_hermes_user_provider"):
                sys.modules.pop(module, None)
