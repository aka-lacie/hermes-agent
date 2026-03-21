"""Tests for hermes_cli.tools_config platform tool persistence."""

from unittest.mock import patch

from hermes_cli.tools_config import (
    _get_platform_tools,
    _platform_toolset_summary,
    _save_platform_tools,
    _toolset_has_keys,
    TOOL_CATEGORIES,
    _visible_providers,
    tools_command,
)


def test_get_platform_tools_uses_default_when_platform_not_configured():
    config = {}

    enabled = _get_platform_tools(config, "cli")

    assert enabled


def test_get_platform_tools_preserves_explicit_empty_selection():
    config = {"platform_toolsets": {"cli": []}}

    enabled = _get_platform_tools(config, "cli")

    assert enabled == set()


def test_platform_toolset_summary_uses_explicit_platform_list():
    config = {}

    summary = _platform_toolset_summary(config, platforms=["cli"])

    assert set(summary.keys()) == {"cli"}
    assert summary["cli"] == _get_platform_tools(config, "cli")


def test_toolset_has_keys_for_vision_accepts_codex_auth(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    (tmp_path / "auth.json").write_text(
        '{"active_provider":"openai-codex","providers":{"openai-codex":{"tokens":{"access_token": "codex-...oken","refresh_token": "codex-...oken"}}}}'
    )
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_BASE_URL", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("AUXILIARY_VISION_PROVIDER", raising=False)
    monkeypatch.delenv("CONTEXT_VISION_PROVIDER", raising=False)
    monkeypatch.setattr(
        "agent.auxiliary_client.resolve_vision_provider_client",
        lambda: ("openai-codex", object(), "gpt-4.1"),
    )

    assert _toolset_has_keys("vision") is True


def test_save_platform_tools_preserves_mcp_server_names():
    """Ensure MCP server names are preserved when saving platform tools.

    Regression test for https://github.com/NousResearch/hermes-agent/issues/1247
    """
    config = {
        "platform_toolsets": {
            "cli": ["web", "terminal", "time", "github", "custom-mcp-server"]
        }
    }

    new_selection = {"web", "browser"}

    with patch("hermes_cli.tools_config.save_config"):
        _save_platform_tools(config, "cli", new_selection)

    saved_toolsets = config["platform_toolsets"]["cli"]

    assert "time" in saved_toolsets
    assert "github" in saved_toolsets
    assert "custom-mcp-server" in saved_toolsets
    assert "web" in saved_toolsets
    assert "browser" in saved_toolsets
    assert "terminal" not in saved_toolsets


def test_save_platform_tools_handles_empty_existing_config():
    """Saving platform tools works when no existing config exists."""
    config = {}

    with patch("hermes_cli.tools_config.save_config"):
        _save_platform_tools(config, "telegram", {"web", "terminal"})

    saved_toolsets = config["platform_toolsets"]["telegram"]
    assert "web" in saved_toolsets
    assert "terminal" in saved_toolsets


def test_save_platform_tools_handles_invalid_existing_config():
    """Saving platform tools works when existing config is not a list."""
    config = {
        "platform_toolsets": {
            "cli": "invalid-string-value"
        }
    }

    with patch("hermes_cli.tools_config.save_config"):
        _save_platform_tools(config, "cli", {"web"})

    saved_toolsets = config["platform_toolsets"]["cli"]
    assert "web" in saved_toolsets


def test_visible_providers_include_nous_subscription_when_logged_in(monkeypatch):
    config = {"model": {"provider": "nous"}}

    monkeypatch.setattr(
        "hermes_cli.nous_subscription.get_nous_auth_status",
        lambda: {"logged_in": True},
    )

    providers = _visible_providers(TOOL_CATEGORIES["browser"], config)

    assert providers[0]["name"].startswith("Nous Subscription")


def test_first_install_nous_auto_configures_managed_defaults(monkeypatch):
    config = {
        "model": {"provider": "nous"},
        "platform_toolsets": {"cli": []},
    }
    for env_var in (
        "VOICE_TOOLS_OPENAI_KEY",
        "OPENAI_API_KEY",
        "ELEVENLABS_API_KEY",
        "FIRECRAWL_API_KEY",
        "FIRECRAWL_API_URL",
        "TAVILY_API_KEY",
        "PARALLEL_API_KEY",
        "BROWSERBASE_API_KEY",
        "BROWSERBASE_PROJECT_ID",
        "BROWSER_USE_API_KEY",
        "FAL_KEY",
    ):
        monkeypatch.delenv(env_var, raising=False)

    monkeypatch.setattr(
        "hermes_cli.tools_config._prompt_toolset_checklist",
        lambda *args, **kwargs: {"web", "image_gen", "tts", "browser"},
    )
    monkeypatch.setattr("hermes_cli.tools_config.save_config", lambda config: None)
    monkeypatch.setattr(
        "hermes_cli.nous_subscription.get_nous_auth_status",
        lambda: {"logged_in": True},
    )

    configured = []
    monkeypatch.setattr(
        "hermes_cli.tools_config._configure_toolset",
        lambda ts_key, config: configured.append(ts_key),
    )

    tools_command(first_install=True, config=config)

    assert config["web"]["backend"] == "firecrawl"
    assert config["tts"]["provider"] == "openai"
    assert config["browser"]["cloud_provider"] == "browserbase"
    assert configured == []
