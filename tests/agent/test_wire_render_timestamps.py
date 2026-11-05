"""Timestamp presentation is optional, profile-configured, and stable within an agent."""

import copy
from types import SimpleNamespace

import pytest

from agent import message_parts as mp
from agent import wire_render as wr
from agent.prompt_builder import wire_conventions_section


@pytest.mark.parametrize("setting,enabled", [(None, False), (False, False), (True, True),
                                               ({"enabled": False}, False), ({"enabled": True}, True)])
@pytest.mark.parametrize("native", [False, True])
def test_timestamp_setting_hides_only_sent_at_on_the_wire(monkeypatch, setting, enabled, native):
    monkeypatch.setattr("hermes_cli.config.load_config_readonly",
                        lambda: {"gateway": {"message_timestamps": setting}})
    agent = SimpleNamespace(provider="timestamp-test")
    renderer = wr.WireRenderer()
    renderer.native = native
    wr.register_renderer(renderer, provider=agent.provider)
    origin = {"platform": "discord", "message_id": "15", "channel_id": "42", "sent_at": 0.0}
    rows = []
    for delivery in ("turn", "steer"):
        row = {"role": "user"}
        mp.set_parts(row, [mp.human_message("hello", delivery=delivery, origin=origin)])
        rows.append(row)
    before = copy.deepcopy(rows)
    try:
        wire = wr.render_messages(agent, rows)
        for row in wire:
            assert ('sent_at="' in row["content"]) is enabled
            assert all(f'{key}="{value}"' in row["content"] for key, value in origin.items() if key != "sent_at")
            if native:
                assert ("sent_at" in row["_parts"][0]["origin"]) is enabled
        assert rows == before  # provenance stays durable even when it is not displayed
        # A setting edited mid-conversation must not mutate the already-sent history/cache prefix.
        monkeypatch.setattr("hermes_cli.config.load_config_readonly",
                            lambda: {"gateway": {"message_timestamps": not enabled}})
        assert wr.render_messages(agent, rows) == wire
    finally:
        wr.unregister_renderer(provider=agent.provider)


def test_conventions_describe_the_toggle_without_changing_the_cached_prefix(monkeypatch):
    agent = SimpleNamespace(provider="custom")
    sections = []
    for enabled in (False, True):
        monkeypatch.setattr("hermes_cli.config.load_config_readonly",
                            lambda: {"gateway": {"message_timestamps": {"enabled": enabled}}})
        sections.append(wire_conventions_section(agent))
    assert sections[0] == sections[1]
    assert "sent_at attribute in origin and steer tags is included only when message timestamps are enabled" in sections[0]
