"""Issue #1336: the AI settings options step picks any Home Assistant AI provider, once for the whole install.

Occupant story: the owner opens AI settings in any zone, picks Home Assistant AI Task (no Claude key needed) or
the deprecated built-in Claude (key needed), and saves. Every zone gets the same AI settings. When the chosen
provider's limits are known to be too small for a full report, a warning step says which setting to raise, but
the save has already happened and is never blocked.

These drive the REAL options-flow step handlers (``_make_options_flow``), never a re-implementation.
"""

from __future__ import annotations

import asyncio
import sys
import types
from unittest.mock import AsyncMock, patch

import pytest

from tests.test_config_flow import _make_config_entry, _make_options_flow

_AI_LIMITS = "custom_components.climate_advisor.ai_limits"


def _submit(**overrides):
    values = {
        "ai_enabled": True,
        "ai_provider": "ha_ai_task",
        "ai_api_key": "",
        "ai_temperature": 0.3,
        "ai_max_tokens": 8192,
        "ai_monthly_budget": 10,
        "ai_auto_requests_per_day": 5,
        "ai_manual_requests_per_day": 20,
        "ai_investigator_enabled": True,
        "ai_investigator_requests_per_day": 3,
    }
    values.update(overrides)
    return values


@pytest.fixture
def limits(monkeypatch):
    """Install a stub ``ai_limits`` reader (BE builds the real one); returns the AsyncMock to configure."""
    import custom_components.climate_advisor as pkg

    reader = AsyncMock(return_value={"readable": True, "checks": [], "ok": True})
    stub = types.ModuleType(_AI_LIMITS)
    stub.async_read_limits = reader
    monkeypatch.setitem(sys.modules, _AI_LIMITS, stub)
    monkeypatch.setattr(pkg, "ai_limits", stub, raising=False)
    return reader


def _run(flow, user_input):
    return asyncio.run(flow.async_step_ai_settings(user_input))


class TestVersion:
    def test_config_entry_version_is_21(self):
        from custom_components.climate_advisor.config_flow import ClimateAdvisorConfigFlow

        assert ClimateAdvisorConfigFlow.VERSION == 21


class TestForm:
    def test_provider_and_entity_fields_are_offered(self):
        from homeassistant.helpers import selector

        from custom_components.climate_advisor.const import AI_PROVIDER_OPTIONS

        flow, _ = _make_options_flow({"ai_provider": "ha_ai_task", "ai_task_entity": "ai_task.claude"})
        selector.SelectSelectorConfig.reset_mock()
        selector.EntitySelectorConfig.reset_mock()
        result = _run(flow, None)

        assert result["step_id"] == "ai_settings"
        keys = {str(k): k for k in result["data_schema"].schema}
        assert {"ai_provider", "ai_task_entity", "ai_api_key"} <= set(keys)
        assert keys["ai_provider"].default() == "ha_ai_task"
        assert keys["ai_task_entity"].description == {"suggested_value": "ai_task.claude"}
        provider_cfg = [
            c.kwargs
            for c in selector.SelectSelectorConfig.call_args_list
            if c.kwargs.get("translation_key") == "ai_provider"
        ]
        assert provider_cfg and provider_cfg[0]["options"] == list(AI_PROVIDER_OPTIONS)
        assert selector.EntitySelectorConfig.call_args.kwargs["filter"]["domain"] == "ai_task"

    def test_entity_picker_filters_on_generate_data_when_ai_task_is_loaded(self, monkeypatch):
        """Same filter as HA's ai_task/services.yaml; without ai_task loaded, every ai_task entity is offered."""
        from homeassistant.helpers import selector

        from custom_components.climate_advisor import config_flow

        monkeypatch.delitem(sys.modules, "homeassistant.components.ai_task", raising=False)
        selector.EntitySelectorConfig.reset_mock()
        config_flow._ai_task_entity_selector()
        assert selector.EntitySelectorConfig.call_args.kwargs == {"filter": {"domain": "ai_task"}}

        monkeypatch.setitem(sys.modules, "homeassistant.components.ai_task", types.ModuleType("ai_task"))
        selector.EntitySelectorConfig.reset_mock()
        config_flow._ai_task_entity_selector()
        assert selector.EntitySelectorConfig.call_args.kwargs == {
            "filter": {"domain": "ai_task", "supported_features": ["ai_task.AITaskEntityFeature.GENERATE_DATA"]}
        }

    def test_reasoning_offers_low_and_medium_and_a_stored_high_shows_medium(self):
        from homeassistant.helpers import selector

        flow, _ = _make_options_flow({"ai_reasoning_effort": "high"})
        selector.SelectSelectorConfig.reset_mock()
        result = _run(flow, None)
        keys = {str(k): k for k in result["data_schema"].schema}
        assert keys["ai_reasoning_effort"].default() == "medium"
        reasoning_cfg = [
            c.kwargs
            for c in selector.SelectSelectorConfig.call_args_list
            if c.kwargs.get("translation_key") == "ai_reasoning_effort"
        ]
        assert reasoning_cfg[0]["options"] == ["low", "medium"]

    def test_entry_with_a_key_and_no_provider_defaults_to_builtin(self):
        flow, _ = _make_options_flow({"ai_enabled": True, "ai_api_key": "sk-ant-1234567"})
        with patch(
            "custom_components.climate_advisor.claude_api.fetch_available_models",
            new_callable=AsyncMock,
            return_value=["claude-sonnet-5"],
        ):
            result = _run(flow, None)
        keys = {str(k): k for k in result["data_schema"].schema}
        assert keys["ai_provider"].default() == "builtin_claude"

    def test_a_retired_stored_model_stays_selectable(self):
        """HA rejects a default that is not an option; a Claude-only field must never block an AI Task save."""
        from homeassistant.helpers import selector

        flow, _ = _make_options_flow({"ai_model": "claude-sonnet-4-5"})
        selector.SelectSelectorConfig.reset_mock()
        result = _run(flow, None)
        keys = {str(k): k for k in result["data_schema"].schema}
        assert keys["ai_model"].default() == "claude-sonnet-4-5"
        offered = [
            o["value"]
            for c in selector.SelectSelectorConfig.call_args_list
            for o in c.kwargs.get("options", [])
            if isinstance(o, dict)
        ]
        assert "claude-sonnet-4-5" in offered

    def test_live_model_fetch_only_for_builtin_claude(self):
        with patch(
            "custom_components.climate_advisor.claude_api.fetch_available_models",
            new_callable=AsyncMock,
            return_value=["claude-sonnet-5"],
        ) as fetch:
            flow, _ = _make_options_flow({"ai_provider": "ha_ai_task"})
            _run(flow, None)
            fetch.assert_not_called()
            flow, _ = _make_options_flow({"ai_provider": "builtin_claude", "ai_api_key": "sk-ant-1234567"})
            _run(flow, None)
            fetch.assert_called_once()


class TestKeyRequirement:
    def test_ai_task_needs_no_key(self, limits):
        flow, _ = _make_options_flow({})
        result = _run(flow, _submit())
        assert result["type"] == "menu"
        data = flow.config_entry.data
        assert data["ai_provider"] == "ha_ai_task"
        assert data["ai_task_entity"] == ""  # an empty picker means HA's preferred AI Task entity
        assert "ai_api_key" not in data

    def test_ai_task_entity_is_saved(self, limits):
        flow, _ = _make_options_flow({})
        _run(flow, _submit(ai_task_entity="ai_task.anthropic"))
        assert flow.config_entry.data["ai_task_entity"] == "ai_task.anthropic"

    def test_builtin_claude_without_a_key_is_refused(self, limits):
        flow, captured = _make_options_flow({})
        result = _run(flow, _submit(ai_provider="builtin_claude"))
        assert result["type"] == "form"
        assert result["errors"] == {"ai_api_key": "ai_key_required"}
        assert "data" not in captured  # nothing saved

    def test_builtin_claude_with_ai_off_needs_no_key(self, limits):
        flow, _ = _make_options_flow({})
        result = _run(flow, _submit(ai_provider="builtin_claude", ai_enabled=False))
        assert result["type"] == "menu"
        assert flow.config_entry.data["ai_provider"] == "builtin_claude"

    def test_builtin_claude_keeps_the_existing_key(self, limits):
        flow, _ = _make_options_flow({"ai_provider": "builtin_claude", "ai_api_key": "sk-ant-existing"})
        result = _run(flow, _submit(ai_provider="builtin_claude"))
        assert result["type"] == "menu"
        assert flow.config_entry.data["ai_api_key"] == "sk-ant-existing"

    def test_no_connection_test_for_ai_task_even_with_a_new_key(self, limits):
        flow, _ = _make_options_flow({})
        result = _run(flow, _submit(ai_api_key="sk-ant-new"))
        assert result["type"] == "menu"
        flow.hass.async_add_executor_job.assert_not_called()


class TestInstallWide:
    def test_saving_copies_ai_settings_to_every_other_zone(self, limits):
        flow, _ = _make_options_flow({"climate_entity": "climate.a", "ai_enabled": False})
        flow.config_entry.entry_id = "zone_a"
        other = _make_config_entry({"climate_entity": "climate.b", "ai_enabled": False, "ai_model": "old"})
        other.entry_id = "zone_b"
        flow.hass.config_entries.async_entries.return_value = [flow.config_entry, other]

        _run(flow, _submit(ai_task_entity="ai_task.anthropic"))

        assert other.data["climate_entity"] == "climate.b"  # the zone's own settings stay
        from custom_components.climate_advisor.ai_settings_sync import extract_ai_values

        assert extract_ai_values(other.data) == extract_ai_values(flow.config_entry.data)
        assert other.data["ai_task_entity"] == "ai_task.anthropic"
        assert other.data["ai_provider"] == "ha_ai_task"


class TestLimitWarning:
    def test_small_limits_show_the_warning_after_saving(self, limits):
        limits.return_value = {
            "provider": "ha_ai_task",
            "entity_id": "ai_task.anthropic",
            "integration": "anthropic",
            "readable": True,
            "checks": [
                {"kind": "output", "setting": "max_tokens", "value": 3000, "required": 8192, "ok": False},
                {"kind": "context", "setting": "num_ctx", "value": 20000, "required": 16384, "ok": True},
            ],
            "ok": False,
        }
        flow, _ = _make_options_flow({})
        texts = {"ai_limit_output": "{integration}: the output limit ({setting}) is {value} tokens; needs {required}."}
        with patch(
            "custom_components.climate_advisor.config_flow._async_texts", new_callable=AsyncMock, return_value=texts
        ):
            result = _run(flow, _submit(ai_task_entity="ai_task.anthropic"))

        assert flow.config_entry.data["ai_provider"] == "ha_ai_task"  # saved before the warning
        assert result["type"] == "form" and result["step_id"] == "ai_limits_warning"
        assert result["description_placeholders"]["checks"] == (
            "- anthropic: the output limit (max_tokens) is 3000 tokens; needs 8192."
        )
        assert dict(limits.call_args.args[1])["ai_task_entity"] == "ai_task.anthropic"
        # Submitting the warning goes back to the menu, the same as every other section.
        after = asyncio.run(flow.async_step_ai_limits_warning({}))
        assert after["type"] == "menu"

    def test_builtin_claude_check_names_the_response_length_field(self, limits):
        limits.return_value = {
            "provider": "builtin_claude",
            "entity_id": None,
            "integration": None,
            "readable": True,
            "checks": [{"kind": "output", "setting": "max_tokens", "value": 4096, "required": 8192, "ok": False}],
            "ok": False,
        }
        flow, _ = _make_options_flow({"ai_provider": "builtin_claude", "ai_api_key": "sk-ant-existing"})
        texts = {"ai_limit_builtin_output": "Built-in Claude: {value} < {required}"}
        with patch(
            "custom_components.climate_advisor.config_flow._async_texts", new_callable=AsyncMock, return_value=texts
        ):
            result = _run(flow, _submit(ai_provider="builtin_claude", ai_max_tokens=4096))
        assert result["description_placeholders"]["checks"] == "- Built-in Claude: 4096 < 8192"

    def test_good_or_unreadable_limits_go_straight_to_the_menu(self, limits):
        for answer in (
            {"readable": True, "checks": [], "ok": True},
            {"readable": False, "checks": [], "ok": True},
        ):
            limits.return_value = answer
            flow, _ = _make_options_flow({})
            assert _run(flow, _submit())["type"] == "menu"

    def test_a_failing_reader_never_blocks_saving(self, limits):
        limits.side_effect = RuntimeError("boom")
        flow, _ = _make_options_flow({})
        result = _run(flow, _submit())
        assert result["type"] == "menu"
        assert flow.config_entry.data["ai_provider"] == "ha_ai_task"

    def test_missing_reader_module_never_blocks_saving(self, monkeypatch):
        import custom_components.climate_advisor as pkg

        monkeypatch.setitem(sys.modules, _AI_LIMITS, None)  # import raises ImportError
        monkeypatch.delattr(pkg, "ai_limits", raising=False)
        flow, _ = _make_options_flow({})
        result = _run(flow, _submit())
        assert result["type"] == "menu"
        assert flow.config_entry.data["ai_provider"] == "ha_ai_task"

    def test_no_check_when_ai_is_off(self, limits):
        flow, _ = _make_options_flow({})
        _run(flow, _submit(ai_enabled=False))
        limits.assert_not_called()
