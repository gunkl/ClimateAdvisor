"""Provider registration, the options-flow AI step per provider, and the AI gate."""

from __future__ import annotations

import asyncio
import json
import logging
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from custom_components.climate_advisor import config_flow
from custom_components.climate_advisor.ai_backend import PROVIDERS, get_provider
from custom_components.climate_advisor.ai_providers import ensure_providers_registered
from custom_components.climate_advisor.const import (
    CHATGPT_ACCOUNT_HASS_KEY,
    CONF_AI_CHATGPT_MODEL,
    REDACTED_CONFIG_KEYS,
)

from .test_config_flow import FULL_CONFIG_V12, _make_options_flow

PKG = Path(__file__).resolve().parent.parent / "custom_components" / "climate_advisor"


def _schema_keys(result) -> set[str]:
    return {str(k) for k in result["data_schema"].schema}


def _step(entry_data, user_input=None, provider=None):
    """Run the settings step; ``provider`` first goes through the provider-choice step."""
    flow, captured = _make_options_flow(entry_data)

    async def _go():
        if provider is not None:
            result = await flow.async_step_ai_provider({"ai_provider": provider})
            if user_input is None:
                return result
        return await flow.async_step_ai_settings(user_input)

    return asyncio.run(_go()), flow, captured


class TestRegistration:
    def test_both_providers_registered(self):
        ensure_providers_registered()
        assert {"anthropic", "chatgpt"} <= set(PROVIDERS)
        assert get_provider("chatgpt").credential_kind == "account"

    @pytest.mark.parametrize("broken", ["chatgpt_backend", "claude_api"])
    def test_one_provider_failing_to_import_does_not_block_the_rest(self, monkeypatch, caplog, broken):
        monkeypatch.setitem(sys.modules, f"custom_components.climate_advisor.{broken}", None)
        with caplog.at_level(logging.ERROR):
            ensure_providers_registered()
        assert broken in caplog.text

    def test_ai_backend_imports_no_provider(self):
        text = (PKG / "ai_backend.py").read_text(encoding="utf-8")
        assert "from .chatgpt_backend" not in text and "from .claude_api" not in text

    def test_redaction_and_version(self):
        assert {"access_token", "refresh_token", "id_token"} <= REDACTED_CONFIG_KEYS
        assert CONF_AI_CHATGPT_MODEL == "ai_chatgpt_model"


def _default(result, key):
    """Default of one field's marker (the selector is a stub in unit tests)."""
    marker = next(k for k in result["data_schema"].schema if str(k) == key)
    return marker.default() if callable(marker.default) else marker.default


class TestAiSettingsFields:
    def test_anthropic_default_has_claude_fields(self):
        result, *_ = _step({**FULL_CONFIG_V12})
        keys = _schema_keys(result)
        assert {"ai_api_key", "ai_model", "ai_temperature", "ai_max_tokens", "ai_monthly_budget"} <= keys
        assert "ai_chatgpt_model" not in keys
        assert "ai_provider" not in keys

    def test_chatgpt_hides_key_tuning_and_budget(self):
        result, *_ = _step({**FULL_CONFIG_V12, "ai_provider": "chatgpt"})
        keys = _schema_keys(result)
        assert "ai_api_key" not in keys
        assert not keys & {"ai_temperature", "ai_max_tokens", "ai_monthly_budget", "ai_reasoning_effort", "ai_model"}
        assert "ai_enabled" in keys and "ai_provider" not in keys
        status = result["description_placeholders"]["status_lines"]
        assert "sign in with ChatGPT" in status and "panel" not in status

    def test_claude_zone_keeps_the_original_wording(self):
        result, *_ = _step({**FULL_CONFIG_V12, "ai_api_key": "sk-ant-abcd1234"})
        strings = json.loads((PKG / "strings.json").read_text(encoding="utf-8"))["options"]
        placeholders = result["description_placeholders"]
        description = strings["step"]["ai_settings"]["description"].format(**placeholders)
        assert description == (
            "Configure Claude AI integration for intelligent analysis and reporting.\nAPI Key Status: ••••1234"
        )
        assert strings["error"]["ai_connection_failed"].format(**placeholders) == (
            "Could not connect to Claude API. Check your API key and try again."
        )

    def test_unknown_provider_still_renders_anthropic_form(self):
        result, *_ = _step({**FULL_CONFIG_V12, "ai_provider": "nope"})
        assert "ai_api_key" in _schema_keys(result)


class TestProviderSwitch:
    def test_provider_step_form_is_one_selector_from_registry(self):
        flow, captured = _make_options_flow({**FULL_CONFIG_V12})
        result = asyncio.run(flow.async_step_ai_provider())
        assert result["step_id"] == "ai_provider"
        assert _schema_keys(result) == {"ai_provider"}
        (marker,) = result["data_schema"].schema
        assert marker.default() == "anthropic"
        # selectors are stubbed here (the real-HA test checks the serialised form)
        values = [o["value"] for o in config_flow.selector.SelectSelectorConfig.call_args.kwargs["options"]]
        assert values == list(PROVIDERS)
        assert "data" not in captured  # nothing committed by just opening it

    def test_provider_step_preselects_saved_provider(self):
        flow, _ = _make_options_flow({**FULL_CONFIG_V12, "ai_provider": "chatgpt"})
        result = asyncio.run(flow.async_step_ai_provider())
        (marker,) = result["data_schema"].schema
        assert marker.default() == "chatgpt"

    def test_fake_extra_provider_appears_without_flow_change(self):
        from dataclasses import replace

        fake = replace(get_provider("anthropic"), id="fakeprov", label="Fake Provider")
        PROVIDERS["fakeprov"] = fake
        try:
            flow, _ = _make_options_flow({**FULL_CONFIG_V12})
            result = asyncio.run(flow.async_step_ai_provider())
            assert _schema_keys(result) == {"ai_provider"}
            opts = config_flow.selector.SelectSelectorConfig.call_args.kwargs["options"]
            assert {"value": "fakeprov", "label": "Fake Provider"} in opts
        finally:
            del PROVIDERS["fakeprov"]

    def test_choosing_provider_commits_nothing_and_shows_its_fields(self):
        data = {**FULL_CONFIG_V12, "ai_api_key": "sk-keep", "ai_model": "claude-x", "ai_temperature": 0.4}
        result, flow, captured = _step(data, provider="chatgpt")
        assert result["step_id"] == "ai_settings"
        assert "ai_api_key" not in _schema_keys(result)
        assert "data" not in captured  # abandoning here changes nothing
        assert flow.config_entry.data.get("ai_provider", "anthropic") == "anthropic"

    def test_save_commits_provider_and_keeps_claude_settings(self):
        data = {**FULL_CONFIG_V12, "ai_api_key": "sk-keep", "ai_model": "claude-x", "ai_temperature": 0.4}
        flow, captured = _make_options_flow(data)
        manager = MagicMock()
        manager.async_load = AsyncMock()
        manager.status.return_value = {"state": "disconnected"}
        flow.hass.data = {CHATGPT_ACCOUNT_HASS_KEY: manager}

        async def _go():
            await flow.async_step_ai_provider({"ai_provider": "chatgpt"})
            return await flow.async_step_ai_settings({"ai_enabled": True})

        result = asyncio.run(_go())
        assert result["step_id"] == "chatgpt_account"
        assert captured["data"]["ai_provider"] == "chatgpt"
        assert captured["data"]["ai_api_key"] == "sk-keep"
        assert captured["data"]["ai_model"] == "claude-x"
        assert captured["data"]["ai_temperature"] == 0.4

    def test_switch_back_to_claude_keeps_claude_settings(self):
        data = {
            **FULL_CONFIG_V12,
            "ai_provider": "chatgpt",
            "ai_api_key": "sk-keep",
            "ai_model": "claude-x",
            "ai_temperature": 0.4,
        }
        flow, captured = _make_options_flow(data)
        flow.hass.data = {}  # no running zone to apply to

        async def _go():
            form = await flow.async_step_ai_provider({"ai_provider": "anthropic"})
            return form, await flow.async_step_ai_settings(
                {"ai_enabled": True, "ai_temperature": 0.4, "ai_max_tokens": 1024}
            )

        form, result = asyncio.run(_go())
        assert form["data_schema"].schema and "ai_api_key" in _schema_keys(form)
        assert result["step_id"] == "init"
        assert captured["data"]["ai_provider"] == "anthropic"
        assert captured["data"]["ai_api_key"] == "sk-keep"
        assert captured["data"]["ai_model"] == "claude-x"

    def test_validation_error_keeps_chosen_provider(self):
        flow, captured = _make_options_flow({**FULL_CONFIG_V12, "ai_provider": "chatgpt"})

        async def _go():
            await flow.async_step_ai_provider({"ai_provider": "anthropic"})
            return await flow.async_step_ai_settings({"ai_enabled": True, "ai_temperature": 0.3, "ai_max_tokens": 1024})

        result = asyncio.run(_go())
        assert result["step_id"] == "ai_settings" and result["errors"] == {"ai_api_key": "ai_key_required"}
        assert "ai_api_key" in _schema_keys(result)  # still the Claude form, not the saved ChatGPT one
        assert "data" not in captured

    def test_claude_zone_saved_config_includes_provider_as_before(self):
        flow, captured = _make_options_flow({**FULL_CONFIG_V12})

        async def _go():
            await flow.async_step_ai_provider({"ai_provider": "anthropic"})
            return await flow.async_step_ai_settings(
                {"ai_enabled": False, "ai_temperature": 0.3, "ai_max_tokens": 1024}
            )

        asyncio.run(_go())
        assert captured["data"]["ai_provider"] == "anthropic"

    def test_chatgpt_submit_needs_no_key_and_keeps_claude_key(self):
        data = {**FULL_CONFIG_V12, "ai_provider": "chatgpt", "ai_api_key": "sk-keep"}
        flow, captured = _make_options_flow(data)
        manager = MagicMock()
        manager.async_load = AsyncMock()
        manager.status.return_value = {"state": "disconnected"}
        flow.hass.data = {CHATGPT_ACCOUNT_HASS_KEY: manager}

        async def _go():
            return await flow.async_step_ai_settings({"ai_enabled": True, "ai_chatgpt_model": "gpt-x"})

        result = asyncio.run(_go())
        assert not result.get("errors")
        assert captured["data"]["ai_chatgpt_model"] == "gpt-x"
        assert captured["data"]["ai_api_key"] == "sk-keep"

    def test_anthropic_still_requires_key_when_enabled(self):
        flow, _ = _make_options_flow({**FULL_CONFIG_V12})

        async def _go():
            return await flow.async_step_ai_settings({"ai_enabled": True, "ai_temperature": 0.3, "ai_max_tokens": 1024})

        result = asyncio.run(_go())
        assert result["errors"] == {"ai_api_key": "ai_key_required"}

    def test_menu_routes_ai_settings_through_provider_step(self):
        from custom_components.climate_advisor.config_flow import OPTIONS_MENU_OPTIONS

        assert "ai_provider" in OPTIONS_MENU_OPTIONS and "ai_settings" not in OPTIONS_MENU_OPTIONS


class TestCoordinatorGate:
    def _gate(self, config):
        from custom_components.climate_advisor.coordinator import ClimateAdvisorCoordinator

        return ClimateAdvisorCoordinator._ai_credentials_present(config)

    def test_anthropic_needs_key(self):
        assert not self._gate({"ai_enabled": True})
        assert self._gate({"ai_enabled": True, "ai_api_key": "k"})

    def test_chatgpt_needs_no_key(self):
        assert self._gate({"ai_provider": "chatgpt"})

    def test_unknown_provider_builds_client_that_reports_error(self):
        assert self._gate({"ai_provider": "nope"})
        from custom_components.climate_advisor.claude_api import ClaudeAPIClient

        client = ClaudeAPIClient({"ai_enabled": True, "ai_provider": "nope"})
        assert client.get_status()["status"] == "error"


def test_validate_flags_literal_tokens():
    import importlib.util

    spec = importlib.util.spec_from_file_location("ca_validate", PKG.parent.parent / "tools" / "validate.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    jwt = "eyJhbGciOiJSUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.abcdefghijklmnop"
    assert any(p.search(f'x = "{jwt}"') for p in mod.TOKEN_VALUE_PATTERNS)
    assert any(p.search('refresh_token = "abcdefghijklmnopqrstuvwxyz0123"') for p in mod.TOKEN_VALUE_PATTERNS)
    assert not any(p.search('CONF = "refresh_token"') for p in mod.TOKEN_VALUE_PATTERNS)


class TestProviderOwnModelKey:
    """An API-key provider with its own model key rides the generic flow, resolver and settings rows."""

    @staticmethod
    def _fake(monkeypatch):
        from .test_ai_backend import FAKE_SPEC

        monkeypatch.setitem(PROVIDERS, "fake", FAKE_SPEC)
        return FAKE_SPEC

    def test_form_offers_and_saves_the_providers_model_key(self, monkeypatch):
        self._fake(monkeypatch)
        base = {**FULL_CONFIG_V12, "ai_model": "claude-keep", "ai_chatgpt_model": "gpt-keep"}
        result, flow, captured = _step(base, provider="fake")
        keys = _schema_keys(result)
        assert "fake_model" in keys and not keys & {"ai_model", "ai_chatgpt_model"}
        assert flow.hass.config_entries.async_update_entry.call_count == 0

        saved, flow, captured = _step(base, {"ai_enabled": False, "fake_model": "fake/model-b"}, provider="fake")
        data = captured["data"]
        assert data["ai_provider"] == "fake" and data["fake_model"] == "fake/model-b"
        from custom_components.climate_advisor.claude_api import ClaudeAPIClient

        resolved = ClaudeAPIClient({**data, "ai_enabled": True, "fake_key": "k"}).configured_model()
        assert resolved == "fake/model-b"
        assert data["ai_model"] == "claude-keep" and data["ai_chatgpt_model"] == "gpt-keep"

    def test_model_field_default_is_never_empty_so_it_is_always_saved(self, monkeypatch):
        self._fake(monkeypatch)
        result, *_ = _step({**FULL_CONFIG_V12}, provider="fake")
        marker = next(k for k in result["data_schema"].schema if str(k) == "fake_model")
        assert isinstance(marker, config_flow.vol.Required)
        assert _default(result, "fake_model") == "fake/model-a"

    def test_configured_model_resolves_the_providers_key(self, monkeypatch):
        from custom_components.climate_advisor.claude_api import ClaudeAPIClient

        self._fake(monkeypatch)
        client = ClaudeAPIClient({"ai_enabled": True, "ai_provider": "fake", "fake_key": "k", "fake_model": "fake/m"})
        assert client.configured_model() == "fake/m"

    def test_settings_rows_show_the_providers_model_key(self, monkeypatch):
        from custom_components.climate_advisor.api import _provider_ai_rows

        spec = self._fake(monkeypatch)
        rows = _provider_ai_rows(spec, "ai_model", {"category": "ai"}, {"fake_model": "fake/m"}, None)
        assert [(r["key"], r["value"]) for r in rows] == [("fake_model", "fake/m")]


class TestAccountStateInStatus:
    @pytest.mark.parametrize(
        ("state", "status"),
        [("connected", "active"), ("disconnected", "not_connected"), ("reauth_required", "reauth_required")],
    )
    def test_status_reports_the_real_account_state(self, state, status):
        from custom_components.climate_advisor.claude_api import ClaudeAPIClient

        manager = MagicMock(state=state)
        config = {"ai_enabled": True, "ai_provider": "chatgpt", "ai_chatgpt_model": "m"}
        assert ClaudeAPIClient(config, manager).get_status()["status"] == status
