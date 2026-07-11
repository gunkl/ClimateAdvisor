"""Tests for build_hvac_entity_context() (Issue #466).

hvac_mode/target_temp_low/target_temp_high now read from coordinator.data
(populated once per update cycle) instead of independently re-fetching
hass.states.get() — these tests prove the new call path produces the same
output the old live-fetch path did, for present/missing/off-mode cases.
current_temp is unaffected (still a live hass.states.get() read — not one of
the 3 fields moved to coordinator.data under this issue).
"""

from __future__ import annotations

import asyncio
from unittest.mock import MagicMock

from custom_components.climate_advisor.ai_skills_context import build_hvac_entity_context


def _make_coordinator(data: dict, climate_entity: str = "climate.thermostat") -> MagicMock:
    coord = MagicMock()
    coord.data = data
    coord.config = {"climate_entity": climate_entity}
    return coord


def _make_hass(current_temperature=72.0) -> MagicMock:
    hass = MagicMock()
    climate_state = MagicMock()
    climate_state.attributes = {"current_temperature": current_temperature}
    hass.states.get.return_value = climate_state
    return hass


class TestBuildHvacEntityContext:
    def test_present_values_appear_in_context(self):
        coord = _make_coordinator({"hvac_mode": "heat", "target_temp_low": 68.0, "target_temp_high": 76.0})
        hass = _make_hass(current_temperature=71.5)

        ctx = asyncio.run(build_hvac_entity_context(hass, coord))

        assert "hvac_mode:        heat" in ctx
        assert "current_temp:     71.5" in ctx
        assert "target_temp_low:  68.0" in ctx
        assert "target_temp_high: 76.0" in ctx

    def test_missing_hvac_mode_falls_back_to_unknown(self):
        """coordinator.data['hvac_mode'] == '' (climate entity unavailable) must
        still display 'unknown', matching the pre-#466 live-fetch fallback."""
        coord = _make_coordinator({"hvac_mode": "", "target_temp_low": None, "target_temp_high": None})
        hass = _make_hass()

        ctx = asyncio.run(build_hvac_entity_context(hass, coord))

        assert "hvac_mode:        unknown" in ctx

    def test_missing_target_temps_fall_back_to_unknown(self):
        coord = _make_coordinator({"hvac_mode": "cool", "target_temp_low": None, "target_temp_high": None})
        hass = _make_hass()

        ctx = asyncio.run(build_hvac_entity_context(hass, coord))

        assert "target_temp_low:  unknown" in ctx
        assert "target_temp_high: unknown" in ctx

    def test_no_climate_entity_configured(self):
        coord = _make_coordinator({"hvac_mode": "", "target_temp_low": None, "target_temp_high": None}, "")
        hass = _make_hass()

        ctx = asyncio.run(build_hvac_entity_context(hass, coord))

        assert "entity_id:        not configured" in ctx
        assert "hvac_mode:        unknown" in ctx

    def test_does_not_call_hass_states_get_for_hvac_mode_or_targets(self):
        """Regression guard: the removed live hass.states.get() call for
        hvac_mode/target_temp_low/target_temp_high must not silently come back —
        only current_temp still needs a live read, and only when a climate_entity
        is configured."""
        coord = _make_coordinator({"hvac_mode": "heat", "target_temp_low": 68.0, "target_temp_high": 76.0})
        hass = _make_hass(current_temperature=70.0)

        asyncio.run(build_hvac_entity_context(hass, coord))

        assert hass.states.get.call_count == 1, "must call hass.states.get() exactly once, for current_temp only"
