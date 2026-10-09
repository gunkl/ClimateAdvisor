"""Options-flow reconcile of temperature source / entity pairs (Issue #1032).

The Options form shows the source select and the entity picker independently, with the entity
prefilled from storage, so every combination can be submitted. A picked outdoor sensor next to
"Weather service" used to be saved and then silently ignored. These tests drive the real
``reconcile_temperature_source`` and the real ``async_step_temperature_sources`` handler.
"""

from __future__ import annotations

import asyncio
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

if "homeassistant" not in sys.modules:
    from conftest import _install_ha_stubs

    _install_ha_stubs()

from custom_components.climate_advisor.config_flow import (
    ClimateAdvisorOptionsFlow,
    reconcile_temperature_source,
)

W = "weather_service"
C = "climate_fallback"
S = "sensor"
I = "input_number"  # noqa: E741 - mirrors the source constant's short name in the table below

# (stored_src, stored_ent, submitted_src, submitted_ent) -> (src, ent, error, rule)
_TABLE = [
    # --- default source stored -------------------------------------------------------------
    (W, None, W, None, (W, None, None, "noop")),
    (W, None, W, "sensor.a", (S, "sensor.a", None, "entity_promotes_source")),
    (W, None, W, "input_number.a", (I, "input_number.a", None, "entity_promotes_source")),
    (W, None, W, "domain.x", (W, "domain.x", None, "noop")),  # unknown domain is left alone
    (W, None, S, None, (S, None, "entity_required_for_source", "entity_required_for_source")),
    (W, None, S, "sensor.a", (S, "sensor.a", None, "noop")),
    (W, None, I, "sensor.a", (I, "sensor.a", "entity_domain_mismatch", "entity_domain_mismatch")),
    (W, "sensor.a", W, None, (W, None, None, "noop")),
    (W, "sensor.a", W, "sensor.a", (W, "sensor.a", None, "noop")),  # stale saved mismatch: entity_health warns
    (W, "sensor.a", W, "sensor.b", (S, "sensor.b", None, "entity_promotes_source")),
    (W, "sensor.a", S, None, (S, None, "entity_required_for_source", "entity_required_for_source")),
    (W, "sensor.a", S, "sensor.a", (S, "sensor.a", None, "noop")),  # the fix path for an ignored entity
    (W, "sensor.a", S, "sensor.b", (S, "sensor.b", None, "noop")),
    # --- sensor stored ---------------------------------------------------------------------
    (S, None, W, None, (W, None, None, "noop")),
    (S, None, W, "sensor.a", (W, "sensor.a", "source_entity_conflict", "source_entity_conflict")),
    (S, None, S, None, (W, None, None, "entity_cleared_reverts_source")),  # legacy S + blank entity heals
    (S, None, S, "sensor.a", (S, "sensor.a", None, "noop")),
    (S, None, I, None, (I, None, "entity_required_for_source", "entity_required_for_source")),
    (S, None, I, "input_number.a", (I, "input_number.a", None, "noop")),
    (S, "sensor.a", W, None, (W, None, None, "noop")),
    (S, "sensor.a", W, "sensor.a", (W, None, None, "source_default_drops_entity")),  # prefill must not re-promote
    (S, "sensor.a", W, "sensor.b", (W, "sensor.b", "source_entity_conflict", "source_entity_conflict")),
    (S, "sensor.a", S, None, (W, None, None, "entity_cleared_reverts_source")),  # deselect
    (S, "sensor.a", S, "sensor.a", (S, "sensor.a", None, "noop")),
    (S, "sensor.a", S, "sensor.b", (S, "sensor.b", None, "noop")),
    (S, "sensor.a", S, "input_number.a", (I, "input_number.a", None, "entity_domain_sets_source")),
    (S, "sensor.a", I, None, (I, None, "entity_required_for_source", "entity_required_for_source")),
    (S, "sensor.a", I, "sensor.a", (I, "sensor.a", "entity_domain_mismatch", "entity_domain_mismatch")),
    (S, "sensor.a", I, "input_number.a", (I, "input_number.a", None, "noop")),
    (S, "domain.x", S, "domain.x", (S, "domain.x", None, "noop")),
    # --- input_number stored ---------------------------------------------------------------
    (I, "input_number.a", W, "input_number.a", (W, None, None, "source_default_drops_entity")),
    (I, "input_number.a", I, None, (W, None, None, "entity_cleared_reverts_source")),
    (I, "sensor.a", I, "sensor.a", (I, "sensor.a", None, "noop")),  # legacy mismatch is not blocked
]


@pytest.mark.parametrize(("stored_src", "stored_ent", "sub_src", "sub_ent", "expected"), _TABLE)
def test_outdoor_truth_table(stored_src, stored_ent, sub_src, sub_ent, expected):
    assert reconcile_temperature_source(stored_src, stored_ent, sub_src, sub_ent, W) == expected


@pytest.mark.parametrize(
    ("stored_src", "stored_ent", "sub_src", "sub_ent", "expected"),
    [
        (C, None, C, "sensor.a", (S, "sensor.a", None, "entity_promotes_source")),
        (S, "sensor.a", S, None, (C, None, None, "entity_cleared_reverts_source")),
        (S, "sensor.a", C, "sensor.a", (C, None, None, "source_default_drops_entity")),
    ],
)
def test_indoor_default_source_is_climate_fallback(stored_src, stored_ent, sub_src, sub_ent, expected):
    assert reconcile_temperature_source(stored_src, stored_ent, sub_src, sub_ent, C) == expected


def test_empty_string_entity_is_treated_as_cleared():
    assert reconcile_temperature_source(S, "sensor.a", S, "", W) == (W, None, None, "entity_cleared_reverts_source")


# ---------------------------------------------------------------------------------------------
# The real flow step
# ---------------------------------------------------------------------------------------------

_BASE = {
    "weather_entity": "weather.forecast_home",
    "climate_entity": "climate.living_room",
    "outdoor_temp_source": W,
    "indoor_temp_source": C,
}


def _make_flow(entry_data: dict):
    flow = object.__new__(ClimateAdvisorOptionsFlow)
    flow._updates = {}
    flow._removed = set()
    flow._editing_schedule_id = None
    flow.config_entry = SimpleNamespace(data=dict(entry_data), entry_id="entry1")
    captured: dict = {}

    def _update_entry(entry, data):
        captured["data"] = data
        entry.data = data

    hass = MagicMock()
    hass.config_entries.async_update_entry = MagicMock(side_effect=_update_entry)
    hass.config_entries.async_reload = AsyncMock()
    flow.hass = hass
    return flow, captured


def _submit(entry_data: dict, user_input: dict):
    flow, captured = _make_flow(entry_data)
    result = asyncio.run(flow.async_step_temperature_sources(user_input))
    return result, captured.get("data"), flow


class TestFlowStep:
    def test_picking_a_sensor_next_to_weather_service_promotes_the_source(self):
        """The exact trap: entity picked, source left on Weather service."""
        _, data, _ = _submit(
            _BASE, {"outdoor_temp_source": W, "indoor_temp_source": C, "outdoor_temp_entity": "sensor.o"}
        )
        assert data["outdoor_temp_source"] == S
        assert data["outdoor_temp_entity"] == "sensor.o"

    def test_input_helper_domain_promotes_to_input_number(self):
        _, data, _ = _submit(
            _BASE, {"outdoor_temp_source": W, "indoor_temp_source": C, "outdoor_temp_entity": "input_number.o"}
        )
        assert data["outdoor_temp_source"] == I

    def test_clearing_the_entity_reverts_the_source_to_weather_service(self):
        stored = {**_BASE, "outdoor_temp_source": S, "outdoor_temp_entity": "sensor.o"}
        _, data, _ = _submit(stored, {"outdoor_temp_source": S, "indoor_temp_source": C})
        assert data["outdoor_temp_source"] == W
        assert "outdoor_temp_entity" not in data

    def test_switching_back_to_weather_service_with_the_entity_still_prefilled_sticks(self):
        stored = {**_BASE, "outdoor_temp_source": S, "outdoor_temp_entity": "sensor.o"}
        _, data, _ = _submit(
            stored, {"outdoor_temp_source": W, "indoor_temp_source": C, "outdoor_temp_entity": "sensor.o"}
        )
        assert data["outdoor_temp_source"] == W
        assert "outdoor_temp_entity" not in data

    def test_indoor_pair_is_reconciled_independently(self):
        stored = {**_BASE, "outdoor_temp_source": S, "outdoor_temp_entity": "sensor.o"}
        _, data, _ = _submit(
            stored,
            {
                "outdoor_temp_source": S,
                "outdoor_temp_entity": "sensor.o",
                "indoor_temp_source": C,
                "indoor_temp_entity": "sensor.i",
            },
        )
        assert data["indoor_temp_source"] == S
        assert data["outdoor_temp_source"] == S

    def test_source_without_entity_is_rejected_and_nothing_is_saved(self):
        flow_result, data, flow = _submit(_BASE, {"outdoor_temp_source": S, "indoor_temp_source": C})
        assert data is None
        assert flow_result["errors"] == {"outdoor_temp_entity": "entity_required_for_source"}
        flow.hass.config_entries.async_update_entry.assert_not_called()

    def test_error_form_keeps_what_the_user_submitted(self):
        """After a validation error the re-shown form defaults to the submitted source, not storage.

        Records the ``vol.Required`` call instead of introspecting the built schema, so it works
        whether ``voluptuous`` is the real package or the harness's mock (CI has no voluptuous;
        same approach as Issue #1023's unit-step default tests).
        """
        import importlib
        from unittest.mock import patch

        mod = importlib.import_module("custom_components.climate_advisor.config_flow")
        seen: dict = {}

        def _spy(key, **kwargs):
            seen[key] = kwargs.get("default")
            return key

        flow, _ = _make_flow(_BASE)
        with patch.object(mod.vol, "Required", side_effect=_spy):
            result = asyncio.run(
                flow.async_step_temperature_sources({"outdoor_temp_source": S, "indoor_temp_source": C})
            )
        assert result["step_id"] == "temperature_sources"
        assert result["errors"] == {"outdoor_temp_entity": "entity_required_for_source"}
        assert seen["outdoor_temp_source"] == S  # submitted value, not the stored weather_service

    def test_domain_mismatch_after_changing_the_source_is_rejected(self):
        stored = {**_BASE, "outdoor_temp_source": S, "outdoor_temp_entity": "sensor.o"}
        result, data, _ = _submit(
            stored, {"outdoor_temp_source": I, "indoor_temp_source": C, "outdoor_temp_entity": "sensor.o"}
        )
        assert data is None
        assert result["errors"] == {"outdoor_temp_entity": "entity_domain_mismatch"}

    def test_an_error_on_one_pair_blocks_the_whole_save(self):
        result, data, _ = _submit(
            _BASE,
            {
                "outdoor_temp_source": W,
                "outdoor_temp_entity": "sensor.o",  # would promote fine
                "indoor_temp_source": S,  # no indoor entity: error
            },
        )
        assert data is None
        assert set(result["errors"]) == {"indoor_temp_entity"}

    def test_absent_source_keys_are_tolerated_and_not_invented(self):
        """Existing option-flow tests submit only the entity key."""
        _, data, _ = _submit({**_BASE}, {"outdoor_temp_entity": "domain.new_entity"})
        assert data["outdoor_temp_entity"] == "domain.new_entity"
        assert data["outdoor_temp_source"] == W
        assert data["indoor_temp_source"] == C

    def test_empty_submission_clears_optional_entities_as_before(self):
        stored = {**_BASE, "outdoor_temp_entity": "sensor.o", "sleep_indoor_temp_entity": "sensor.sleep"}
        _, data, _ = _submit(stored, {})
        assert "outdoor_temp_entity" not in data
        assert "sleep_indoor_temp_entity" not in data

    def test_sleep_entity_is_never_reconciled(self):
        _, data, _ = _submit(
            _BASE,
            {"outdoor_temp_source": W, "indoor_temp_source": C, "sleep_indoor_temp_entity": "sensor.sleep"},
        )
        assert data["sleep_indoor_temp_entity"] == "sensor.sleep"
        assert data["indoor_temp_source"] == C
