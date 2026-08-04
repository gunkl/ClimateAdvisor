"""Tests for coordinator.async_persist_model_fallback() (Issue #563).

Uses the established object.__new__() + types.MethodType() partial-instantiation
pattern (see test_contact_status.py::_make_real_coordinator) to exercise the real
ClimateAdvisorCoordinator.async_persist_model_fallback() method directly, rather than
mirroring its logic — per this project's "never mirror the logic under test" doctrine.
"""

from __future__ import annotations

import asyncio
import sys
import types
from unittest.mock import AsyncMock, MagicMock

# ── HA module stubs (must happen before importing climate_advisor) ──
if "homeassistant" not in sys.modules:
    from conftest import _install_ha_stubs

    _install_ha_stubs()

from custom_components.climate_advisor.const import CONF_AI_MODEL
from custom_components.climate_advisor.coordinator import ClimateAdvisorCoordinator


def _make_coordinator(entry_id: str, config: dict, entry: MagicMock | None) -> ClimateAdvisorCoordinator:
    coord = object.__new__(ClimateAdvisorCoordinator)
    coord.config = config
    coord._entry_id = entry_id
    coord.hass = MagicMock()
    coord.hass.config_entries.async_get_entry = MagicMock(return_value=entry)
    coord.hass.config_entries.async_update_entry = MagicMock()
    coord.async_persist_model_fallback = types.MethodType(ClimateAdvisorCoordinator.async_persist_model_fallback, coord)
    return coord


def _make_entry(data: dict) -> MagicMock:
    entry = MagicMock()
    entry.data = data
    return entry


class TestAsyncPersistModelFallback:
    def test_persists_new_model_and_updates_in_memory_config(self):
        entry = _make_entry({CONF_AI_MODEL: "claude-sonnet-4-6", "other_key": "unchanged"})
        coord = _make_coordinator(
            entry_id="entry-123",
            config={CONF_AI_MODEL: "claude-sonnet-4-6"},
            entry=entry,
        )

        asyncio.run(coord.async_persist_model_fallback("claude-sonnet-5"))

        coord.hass.config_entries.async_update_entry.assert_called_once()
        call_args = coord.hass.config_entries.async_update_entry.call_args
        assert call_args.args[0] is entry
        persisted_data = call_args.kwargs["data"]
        assert persisted_data[CONF_AI_MODEL] == "claude-sonnet-5"
        assert persisted_data["other_key"] == "unchanged"  # merge, not replace
        assert coord.config[CONF_AI_MODEL] == "claude-sonnet-5"

    def test_logs_warning_with_old_and_new_model(self, caplog):
        entry = _make_entry({CONF_AI_MODEL: "claude-sonnet-4-6"})
        coord = _make_coordinator(
            entry_id="entry-123",
            config={CONF_AI_MODEL: "claude-sonnet-4-6"},
            entry=entry,
        )

        with caplog.at_level("WARNING"):
            asyncio.run(coord.async_persist_model_fallback("claude-sonnet-5"))

        assert any("claude-sonnet-4-6" in rec.message and "claude-sonnet-5" in rec.message for rec in caplog.records)

    def test_no_entry_id_logs_and_updates_in_memory_only(self, caplog):
        coord = _make_coordinator(entry_id="", config={CONF_AI_MODEL: "claude-sonnet-4-6"}, entry=None)

        with caplog.at_level("WARNING"):
            asyncio.run(coord.async_persist_model_fallback("claude-sonnet-5"))

        coord.hass.config_entries.async_update_entry.assert_not_called()
        assert coord.config[CONF_AI_MODEL] == "claude-sonnet-5"
        assert any("re-detect" in rec.message for rec in caplog.records)

    def test_entry_not_found_logs_and_does_not_update_in_memory_config(self, caplog):
        coord = _make_coordinator(entry_id="ghost-entry", config={CONF_AI_MODEL: "claude-sonnet-4-6"}, entry=None)

        with caplog.at_level("WARNING"):
            asyncio.run(coord.async_persist_model_fallback("claude-sonnet-5"))

        coord.hass.config_entries.async_update_entry.assert_not_called()
        # Entry lookup failed entirely — nothing to safely apply in-memory either.
        assert coord.config[CONF_AI_MODEL] == "claude-sonnet-4-6"
        assert any("could not be found" in rec.message for rec in caplog.records)


class TestAiSkillsPersistenceWiring:
    """ai_skills.py must only call persistence when a fallback actually occurred."""

    def test_persist_called_when_resolved_model_differs(self):
        from custom_components.climate_advisor.ai_skills import _maybe_persist_model_fallback

        coordinator = MagicMock()
        coordinator.async_persist_model_fallback = AsyncMock()
        response = MagicMock(resolved_model="claude-sonnet-5")

        asyncio.run(
            _maybe_persist_model_fallback(
                coordinator, override_model=None, cfg={CONF_AI_MODEL: "claude-sonnet-4-6"}, response=response
            )
        )

        coordinator.async_persist_model_fallback.assert_awaited_once_with("claude-sonnet-5")

    def test_persist_not_called_when_resolved_model_matches_requested(self):
        from custom_components.climate_advisor.ai_skills import _maybe_persist_model_fallback

        coordinator = MagicMock()
        coordinator.async_persist_model_fallback = AsyncMock()
        response = MagicMock(resolved_model="claude-sonnet-4-6")

        asyncio.run(
            _maybe_persist_model_fallback(
                coordinator, override_model=None, cfg={CONF_AI_MODEL: "claude-sonnet-4-6"}, response=response
            )
        )

        coordinator.async_persist_model_fallback.assert_not_awaited()

    def test_override_model_takes_precedence_over_config_model(self):
        from custom_components.climate_advisor.ai_skills import _maybe_persist_model_fallback

        coordinator = MagicMock()
        coordinator.async_persist_model_fallback = AsyncMock()
        # override_model was what was actually requested — response matches it, so no fallback.
        response = MagicMock(resolved_model="claude-opus-4-6")

        asyncio.run(
            _maybe_persist_model_fallback(
                coordinator,
                override_model="claude-opus-4-6",
                cfg={CONF_AI_MODEL: "claude-sonnet-4-6"},
                response=response,
            )
        )

        coordinator.async_persist_model_fallback.assert_not_awaited()
