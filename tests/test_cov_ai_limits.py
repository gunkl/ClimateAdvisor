"""Coverage for the AI limit reader's defensive paths (ai_limits.py, Issue #1336).

Occupant view: the pre-run "raise this setting" warning must never break the investigation or the options flow.
When Home Assistant's registry, the AI Task preferences or the provider's settings cannot be read, the reader says
"unreadable" (no warning, no crash) instead of guessing a limit.
"""

from __future__ import annotations

import asyncio
import sys
from types import ModuleType, SimpleNamespace
from unittest.mock import MagicMock, patch

from custom_components.climate_advisor import ai_limits
from custom_components.climate_advisor.const import AI_PROVIDER_HA_AI_TASK

_TASK_CFG = {"ai_enabled": True, "ai_provider": AI_PROVIDER_HA_AI_TASK, "ai_task_entity": "ai_task.x"}


def _read(hass, registry, config):
    # A whole fake entity_registry module (see tests/test_ai_task_1336.py: never patch the shared stub's child).
    fake_er = SimpleNamespace(async_get=lambda _hass: registry)
    with (
        patch.dict(sys.modules, {"homeassistant.helpers.entity_registry": fake_er}),
        patch.object(sys.modules["homeassistant.helpers"], "entity_registry", fake_er),
    ):
        return asyncio.run(ai_limits.async_read_limits(hass, config))


def _registry(reg_entry):
    registry = MagicMock()
    registry.async_get.return_value = reg_entry
    return registry


class TestAsInt:
    def test_bool_is_not_a_token_count(self):
        # True would otherwise read as a 1-token limit.
        assert ai_limits._as_int(True) is None
        assert ai_limits._as_int(False) is None

    def test_numbers_and_numeric_strings(self):
        assert ai_limits._as_int("8192") == 8192
        assert ai_limits._as_int(None) is None


class TestPreferredEntity:
    def test_real_ai_task_preferences_key_is_used_when_importable(self):
        const = ModuleType("homeassistant.components.ai_task.const")
        const.DATA_PREFERENCES = "real_prefs_key"
        hass = MagicMock()
        hass.data = {"real_prefs_key": SimpleNamespace(gen_data_entity_id="ai_task.real")}
        with patch.dict(sys.modules, {"homeassistant.components.ai_task.const": const}):
            assert ai_limits._preferred_ai_task_entity(hass) == "ai_task.real"

    def test_unreadable_hass_data_means_no_preferred_entity(self):
        hass = MagicMock()
        hass.data = MagicMock()
        hass.data.get.side_effect = RuntimeError("boom")
        assert ai_limits._preferred_ai_task_entity(hass) is None

    def test_empty_or_non_string_preference_is_none(self):
        hass = MagicMock()
        hass.data = {"ai_task_preferences": SimpleNamespace(gen_data_entity_id="")}
        assert ai_limits._preferred_ai_task_entity(hass) is None
        hass.data = {}
        assert ai_limits._preferred_ai_task_entity(hass) is None


class TestReader:
    def test_entity_without_config_entry_uses_registry_platform_but_is_unreadable(self):
        hass = MagicMock()
        hass.data = {}
        reg = SimpleNamespace(config_entry_id=None, config_subentry_id="sub1", platform="anthropic")
        out = _read(hass, _registry(reg), _TASK_CFG)
        assert out["integration"] == "anthropic"
        assert out["readable"] is False and out["checks"] == [] and out["ok"] is True
        hass.config_entries.async_get_entry.assert_not_called()

    def test_missing_subentry_is_unreadable(self):
        hass = MagicMock()
        reg = SimpleNamespace(config_entry_id="ce1", config_subentry_id="gone", platform="anthropic")
        hass.config_entries.async_get_entry.return_value = SimpleNamespace(
            domain="anthropic", subentries={"sub1": SimpleNamespace(data={"max_tokens": 9000})}
        )
        out = _read(hass, _registry(reg), _TASK_CFG)
        assert out["integration"] == "anthropic" and out["readable"] is False

    def test_non_mapping_subentry_data_is_unreadable(self):
        hass = MagicMock()
        reg = SimpleNamespace(config_entry_id="ce1", config_subentry_id="sub1", platform="anthropic")
        hass.config_entries.async_get_entry.return_value = SimpleNamespace(
            domain="anthropic", subentries={"sub1": SimpleNamespace(data=["not", "a", "mapping"])}
        )
        assert _read(hass, _registry(reg), _TASK_CFG)["readable"] is False

    def test_registry_failure_never_breaks_the_caller(self):
        registry = MagicMock()
        registry.async_get.side_effect = RuntimeError("registry not loaded")
        out = _read(MagicMock(), registry, _TASK_CFG)
        assert out == {
            "provider": AI_PROVIDER_HA_AI_TASK,
            "entity_id": "ai_task.x",
            "integration": None,
            "readable": False,
            "checks": [],
            "ok": True,
        }

    def test_builtin_with_garbage_max_tokens_is_unreadable(self):
        out = _read(MagicMock(), MagicMock(), {"ai_api_key": "k", "ai_max_tokens": "lots"})
        assert out["readable"] is False and out["integration"] == "climate_advisor" and out["ok"] is True
