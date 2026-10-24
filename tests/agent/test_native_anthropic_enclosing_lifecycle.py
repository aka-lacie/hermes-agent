"""Regressions for the enclosing boundaries found by independent native review."""
from types import SimpleNamespace
from unittest.mock import Mock

import anthropic
import httpx
import pytest

from providers.anthropic import ProviderContractError
from providers.base import ProviderProfile

BASE = "http://127.0.0.1:8080/v1"
MODEL = "claude-opus-4-6"


def wrapped(error):
    outer = anthropic.APIConnectionError(request=httpx.Request("POST", BASE + "/messages"))
    outer.__cause__ = error
    return outer


@pytest.mark.parametrize("inline", [False, True])
@pytest.mark.parametrize("partial", [False, True])
def test_stream_worker_preserves_policy_in_result_channel(monkeypatch, inline, partial):
    from agent import chat_completion_helpers as helpers
    obj = helpers._StreamingCall.__new__(helpers._StreamingCall)
    obj.agent = SimpleNamespace(_interrupt_requested=False)
    obj.result = {"error": None, "response": None}
    obj.deltas_were_sent = {"yes": partial}
    obj._request_cancelled = {"value": False}
    obj._resolve_stale_timeout = lambda: None
    obj._monitor_loop = lambda: obj._call_done.wait(2)
    obj._start_stream_attempt = lambda: 1
    obj._close_managed_stream = Mock()
    obj.clients = SimpleNamespace(close_once=Mock())
    obj._partial_stream_stub = Mock()
    obj._retry_after_drop = Mock()
    rejection = ProviderContractError("stream policy rejected")
    obj._call_wire = Mock(side_effect=wrapped(rejection))
    monkeypatch.setattr(helpers, "should_use_direct_api_call", lambda agent: inline)
    monkeypatch.setattr(helpers, "_with_stream_emitters", lambda agent, call: call())
    with pytest.raises(ProviderContractError) as caught:
        obj.run()  # Real worker, _call retry loop, handler and caller result conversion.
    assert caught.value is rejection
    assert obj.result["error"] is rejection
    obj._call_wire.assert_called_once()
    obj._retry_after_drop.assert_not_called()
    obj._partial_stream_stub.assert_not_called()
    obj.clients.close_once.assert_called_once_with("stream_error_cleanup")


@pytest.mark.parametrize("policy", [False, True])
def test_compression_stall_chain_stops_only_for_policy(monkeypatch, policy):
    from agent import conversation_compression as cc
    routes = [{"provider": "guarded", "model": MODEL, "label": "guarded"},
              {"deterministic": True, "label": "deterministic"}]
    monkeypatch.setattr(cc, "_stall_retry_routes", lambda _: routes)
    failure = wrapped(ProviderContractError("compression policy rejected")) if policy else RuntimeError("ordinary failure")
    runner = Mock(side_effect=failure)
    monkeypatch.setattr(cc, "run_compress_context_with_progress_timeout", runner)
    import time
    kwargs = dict(worker=lambda fence: None, messages=[], system_prompt_fallback="",
                  precommit_deadline_monotonic=time.monotonic() + 120,
                  idle_timeout_seconds=1, total_ceiling_seconds=2,
                  telemetry_agent=SimpleNamespace(), new_fence=cc.CompressionCommitFence,
                  escalate_deterministic=True)
    if policy:
        with pytest.raises(ProviderContractError):
            cc._retry_compression_on_fallback_chain(**kwargs)
        assert runner.call_count == 1
    else:
        assert cc._retry_compression_on_fallback_chain(**kwargs) is None
        assert runner.call_count == 2


def test_auto_fallback_cache_revalidates_effective_owner(monkeypatch):
    import providers
    from agent import auxiliary_client as aux, anthropic_adapter as adapter
    monkeypatch.setattr(aux, "_client_cache", {})
    monkeypatch.setattr(aux, "_pool_cache_hint", lambda *a, **kw: None)
    monkeypatch.setattr(aux, "_try_main_provider_route", lambda *a, **kw: None)
    monkeypatch.setattr(aux, "_task_prefers_fast_model", lambda *a, **kw: False)
    monkeypatch.setattr(aux, "_warn_stale_openai_base_url", lambda *a, **kw: None)
    profiles = {}
    monkeypatch.setattr(providers, "get_provider_profile", profiles.get)
    ordinary = SimpleNamespace(messages=SimpleNamespace(create=Mock(), stream=Mock()),
                               base_url=BASE, close=Mock(), is_closed=lambda: False)
    raw = Mock(return_value=ordinary)
    monkeypatch.setattr(adapter, "_build_anthropic_sdk_client", raw)
    factory_hits = []
    class Guarded(ProviderProfile):
        def create_anthropic_client(self, options):
            factory_hits.append(options)
            raise ProviderContractError("reloaded fallback rejected")
    def fallback(*args, **kwargs):
        client = adapter.build_anthropic_client("synthetic", BASE, provider="review-fallback")
        return client, MODEL, "fallback_chain[0](review-fallback)"
    monkeypatch.setattr(aux, "_try_configured_fallback_chain", fallback)
    kwargs = dict(model=MODEL, base_url=BASE, api_key="synthetic", task="compression",
                  main_runtime={"provider": "unavailable-main", "model": MODEL,
                                "base_url": BASE, "api_key": "synthetic"})
    assert aux._get_cached_client("auto", **kwargs)[0] is ordinary
    assert aux._get_cached_client("auto", **kwargs)[0] is ordinary
    raw.assert_called_once()
    profiles["review-fallback"] = Guarded("review-fallback", api_mode="anthropic_messages", anthropic_native=True)
    with pytest.raises(ProviderContractError, match="reloaded fallback"):
        aux._get_cached_client("auto", **kwargs)
    assert len(factory_hits) == 1
    ordinary.close.assert_not_called()  # Declaration eviction cannot kill another live caller.


@pytest.mark.parametrize("async_mode", [False, True])
def test_auto_cache_revalidates_effective_route_and_keeps_loop_binding(monkeypatch, async_mode):
    import providers
    from agent import auxiliary_client as aux
    monkeypatch.setattr(aux, "_client_cache", {})
    monkeypatch.setattr(aux, "_pool_cache_hint", lambda *a, **kw: None)
    profile = ProviderProfile("fallback", base_url=BASE)
    monkeypatch.setattr(providers, "get_provider_profile", lambda name: profile if name == "fallback" else None)
    loop = SimpleNamespace(is_closed=lambda: False)
    monkeypatch.setattr(aux, "_current_event_loop", lambda: loop)
    clients = []
    def resolve(*args, **kwargs):
        from providers.anthropic import bind_client_provenance, provider_client_binding
        binding = provider_client_binding("fallback")
        client = SimpleNamespace(_hermes_aux_effective_provider="fallback", base_url=profile.base_url, close=Mock())
        bind_client_provenance(client, binding)
        clients.append(client)
        return client, MODEL
    monkeypatch.setattr(aux, "resolve_provider_client", resolve)
    kwargs = dict(model=MODEL, async_mode=async_mode, api_key="synthetic", base_url=BASE)
    first = aux._get_cached_client("auto", **kwargs)[0]
    assert aux._get_cached_client("auto", **kwargs)[0] is first
    profile.base_url = "http://127.0.0.1:8081/v1"
    second = aux._get_cached_client("auto", **kwargs)[0]
    assert second is not first
    first.close.assert_not_called()
    assert next(iter(aux._client_cache.values()))[2] is (loop if async_mode else None)
    if async_mode:
        loop = SimpleNamespace(is_closed=lambda: False)
        assert aux._get_cached_client("auto", **kwargs)[0] is not second
        second.close.assert_called_once()
