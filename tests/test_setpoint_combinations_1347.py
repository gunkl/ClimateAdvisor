"""Issue #1347: warn on setpoint combinations that thrash or settle incoherently; no hard limits.

Occupant view: a mini-split owner can now keep the house warm while away (e.g. 20 °C), which the
old 18.5 °C / 65 °F cap refused. A combination that would make the heat and AC take turns, or make
the house settle somewhere that does not make sense, shows a plain-English warning next to the
field; submitting the same values again saves them. The vacation band can be set on its own; left
blank it is the away band widened by 3 °F, exactly as before.

Layers tested here: the pure rules (``setpoint_combinations``), the vacation resolver through the
real engine / scheduler / chart, and the REAL initial-setup and options setpoints steps.
"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from custom_components.climate_advisor import setpoint_combinations as sc
from custom_components.climate_advisor.setpoint_combinations import (
    WARN_AWAY_COOL_BELOW_HOME,
    WARN_AWAY_HEAT_ABOVE_HOME,
    WARN_BAND_INVERTED,
    WARN_BAND_NARROW,
    WARN_OUTSIDE_THERMOSTAT,
    WARN_SLEEP_COOL_ABOVE_AWAY,
    WARN_SLEEP_HEAT_BELOW_AWAY,
    WARN_VACATION_COOL_BELOW_AWAY,
    WARN_VACATION_FREEZE,
    WARN_VACATION_HEAT_ABOVE_AWAY,
    WARNING_PRIORITY,
    setpoint_combination_findings,
    vacation_edges,
)

# A coherent household: home 68/74, away 62/80, sleep 64/76 (vacation derives to 59/83).
_OK = {
    "comfort_heat": 68.0,
    "comfort_cool": 74.0,
    "setback_heat": 62.0,
    "setback_cool": 80.0,
    "sleep_heat": 64.0,
    "sleep_cool": 76.0,
}


def _with(**changes) -> dict:
    return {**_OK, **changes}


# ---------------------------------------------------------------------------
# The rules — one row per plan-table row, plus controls (state matrix, not before/after)
# ---------------------------------------------------------------------------


class TestCombinationMatrix:
    @pytest.mark.parametrize(
        ("label", "values", "kwargs", "expected"),
        [
            ("coherent household", _OK, {}, {}),
            # Row 1 / 1a / 2 / 2a: away vs home, equal is a legitimate "don't set back"
            (
                "away heat above home",
                _with(setback_heat=69.0, sleep_heat=69.0),
                {},
                {"setback_heat": WARN_AWAY_HEAT_ABOVE_HOME},
            ),
            ("away heat equal home (mini-split)", _with(setback_heat=68.0, sleep_heat=68.0), {}, {}),
            (
                "away cool below home",
                _with(setback_cool=73.0, sleep_cool=73.0),
                {},
                {"setback_cool": WARN_AWAY_COOL_BELOW_HOME},
            ),
            ("away cool equal home", _with(setback_cool=74.0, sleep_cool=74.0), {}, {}),
            # Row 3 / 4: comfort band inverted, equal, narrow, just wide enough
            (
                "comfort inverted 74/72",
                _with(comfort_heat=74.0, comfort_cool=72.0, setback_cool=80.0),
                {},
                {"comfort_heat": WARN_BAND_INVERTED},
            ),
            (
                "comfort equal 72/72",
                _with(comfort_heat=72.0, comfort_cool=72.0),
                {},
                {"comfort_heat": WARN_BAND_INVERTED},
            ),
            ("comfort gap 0.5", _with(comfort_heat=71.5, comfort_cool=72.0), {}, {"comfort_heat": WARN_BAND_NARROW}),
            (
                "comfort gap 3 (common thermostat default)",
                _with(comfort_heat=70.0, comfort_cool=73.0),
                {},
                {"comfort_heat": WARN_BAND_NARROW},
            ),
            ("comfort gap 3.9", _with(comfort_heat=70.1, comfort_cool=74.0), {}, {"comfort_heat": WARN_BAND_NARROW}),
            ("comfort gap 4.0 is fine", _with(comfort_heat=70.0, comfort_cool=74.0), {}, {}),
            (
                "comfort gap 2 C (3.6 F)",
                _with(comfort_heat=71.6, comfort_cool=75.2),
                {},
                {"comfort_heat": WARN_BAND_NARROW},
            ),
            ("comfort gap 2.5 C (4.5 F) is fine", _with(comfort_heat=71.6, comfort_cool=76.1), {}, {}),
            # Row 5 / 6: sleep and away bands
            ("sleep inverted", _with(sleep_heat=77.0, sleep_cool=76.0), {}, {"sleep_heat": WARN_BAND_INVERTED}),
            (
                "away inverted",
                _with(
                    comfort_heat=68.0,
                    comfort_cool=74.0,
                    setback_heat=68.0,
                    setback_cool=68.0,
                    sleep_heat=68.0,
                    sleep_cool=68.5,
                ),
                {},
                {
                    "comfort_heat": None,
                    "setback_heat": WARN_BAND_INVERTED,
                    "sleep_heat": WARN_BAND_NARROW,
                    "setback_cool": WARN_AWAY_COOL_BELOW_HOME,
                    "sleep_cool": WARN_SLEEP_COOL_ABOVE_AWAY,
                },
            ),
            # Row 8 / 8m: sleep beyond away
            ("sleep heat below away", _with(sleep_heat=61.0), {}, {"sleep_heat": WARN_SLEEP_HEAT_BELOW_AWAY}),
            ("sleep cool above away", _with(sleep_cool=81.0), {}, {"sleep_cool": WARN_SLEEP_COOL_ABOVE_AWAY}),
            # Row 9: separate vacation setpoints ordered against away
            (
                "vacation heat above away",
                _with(vacation_heat=63.0),
                {},
                {"vacation_heat": WARN_VACATION_HEAT_ABOVE_AWAY},
            ),
            (
                "vacation cool below away",
                _with(vacation_cool=79.0),
                {},
                {"vacation_cool": WARN_VACATION_COOL_BELOW_AWAY},
            ),
            ("vacation equal away", _with(vacation_heat=62.0, vacation_cool=80.0), {}, {}),
            (
                "vacation inverted",
                _with(vacation_heat=60.0, vacation_cool=60.0),
                {},
                {"vacation_heat": WARN_BAND_INVERTED, "vacation_cool": WARN_VACATION_COOL_BELOW_AWAY},
            ),
            # Row 10: freeze advisory, on the field that sets it
            ("derived vacation below 45", _with(setback_heat=47.0), {}, {"setback_heat": WARN_VACATION_FREEZE}),
            ("derived vacation exactly 45", _with(setback_heat=48.0), {}, {}),
            ("separate vacation below 45", _with(vacation_heat=44.0), {}, {"vacation_heat": WARN_VACATION_FREEZE}),
            # Row 11: outside the thermostat's own limits
            (
                "below thermostat min",
                _with(setback_heat=50.0),
                {"thermostat_min_f": 55.0},
                {"setback_heat": WARN_OUTSIDE_THERMOSTAT},
            ),
            (
                "above thermostat max",
                _with(setback_cool=92.0),
                {"thermostat_max_f": 90.0},
                {"setback_cool": WARN_OUTSIDE_THERMOSTAT},
            ),
            (
                "at thermostat limits",
                _with(setback_heat=55.0, setback_cool=90.0),
                {"thermostat_min_f": 55.0, "thermostat_max_f": 90.0},
                {},
            ),
            # C1: a warmer/cooler bedroom at night is a preference, not a fault
            ("sleep warmer than home", _with(sleep_heat=70.0), {}, {}),
            ("sleep cooler than home cool", _with(sleep_cool=72.0), {}, {}),
            # C2 / D6: single-mode thermostats cannot flip between heat and AC
            (
                "heat-only, inverted comfort",
                _with(comfort_heat=74.0, comfort_cool=72.0),
                {"supports_heat": True, "supports_cool": False},
                {},
            ),
            (
                "cool-only, inverted comfort",
                _with(comfort_heat=74.0, comfort_cool=72.0),
                {"supports_heat": False, "supports_cool": True},
                {},
            ),
            (
                "heat-only still warns heat ordering",
                _with(setback_heat=69.0, sleep_heat=69.0),
                {"supports_heat": True, "supports_cool": False},
                {"setback_heat": WARN_AWAY_HEAT_ABOVE_HOME},
            ),
            (
                "heat-only skips cool ordering",
                _with(setback_cool=73.0),
                {"supports_heat": True, "supports_cool": False},
                {},
            ),
            (
                "cool-only skips heat ordering",
                _with(setback_heat=69.0),
                {"supports_heat": False, "supports_cool": True},
                {},
            ),
            (
                "unknown capabilities = two-mode",
                _with(comfort_heat=74.0, comfort_cool=72.0),
                {"supports_heat": None, "supports_cool": None},
                {"comfort_heat": WARN_BAND_INVERTED},
            ),
        ],
    )
    def test_row(self, label, values, kwargs, expected):
        expected = {k: v for k, v in expected.items() if v is not None}
        assert setpoint_combination_findings(values, **kwargs) == expected, label

    def test_most_severe_warning_wins_on_one_field(self):
        # setback_heat: away band inverted AND above home heat AND below the thermostat minimum.
        values = _with(comfort_heat=60.0, setback_heat=81.0, setback_cool=80.0)
        found = setpoint_combination_findings(values, thermostat_min_f=85.0)
        assert found["setback_heat"] == WARN_BAND_INVERTED

    def test_narrow_threshold_stays_above_the_engines_own_limit(self):
        """Owner decision (2026-11-06): warn-only, so conservative. It must never drop below the gap under
        which the comfort-family selector loses interior switching, or thermostats' common 3 °F default."""
        from custom_components.climate_advisor.comfort_family_decision import COMFORT_MIN_USABLE_MARGIN_F

        assert sc.MIN_HEAT_COOL_GAP_F == 4.0
        assert sc.MIN_HEAT_COOL_GAP_F > 3 * COMFORT_MIN_USABLE_MARGIN_F

    def test_every_warning_has_a_priority(self):
        keys = {v for k, v in vars(sc).items() if k.startswith("WARN_")}
        assert keys == set(WARNING_PRIORITY)


class TestCelsiusRounding:
    """Row 12: compare in °F with a tolerance, so a rounded display value never reads as 'warmer'."""

    def test_round_tripped_celsius_equal_values_are_equal(self):
        from custom_components.climate_advisor.temperature import CELSIUS, to_fahrenheit

        # 18.5 °C stored once as 65.3 °F and once as an exactly converted 18.5 °C.
        values = _with(comfort_heat=to_fahrenheit(18.5, CELSIUS), setback_heat=65.32, sleep_heat=65.32)
        assert "setback_heat" not in setpoint_combination_findings(values)

    def test_one_celsius_step_above_still_warns(self):
        from custom_components.climate_advisor.temperature import CELSIUS, to_fahrenheit

        values = _with(comfort_heat=to_fahrenheit(18.5, CELSIUS), setback_heat=to_fahrenheit(19.0, CELSIUS))
        assert setpoint_combination_findings(values)["setback_heat"] == WARN_AWAY_HEAT_ABOVE_HOME


# ---------------------------------------------------------------------------
# Vacation resolver: one derivation, used by engine, scheduler and chart
# ---------------------------------------------------------------------------


class TestVacationEdges:
    @pytest.mark.parametrize(
        ("config", "expected"),
        [
            ({"setback_heat": 62.0, "setback_cool": 80.0}, (59.0, 83.0)),  # pre-#1347 behaviour
            ({"setback_heat": 62.0, "setback_cool": 80.0, "vacation_heat": 55.0, "vacation_cool": 88.0}, (55.0, 88.0)),
            ({"setback_heat": 62.0, "setback_cool": 80.0, "vacation_heat": 55.0}, (55.0, 83.0)),  # each side alone
            ({"setback_heat": 62.0, "setback_cool": 80.0, "vacation_heat": None, "vacation_cool": ""}, (59.0, 83.0)),
        ],
    )
    def test_resolver(self, config, expected):
        assert vacation_edges(config) == expected


def _classification(day_type: str):
    from custom_components.climate_advisor.classifier import DayClassification

    return DayClassification(
        day_type=day_type,
        trend_direction="stable",
        trend_magnitude=0.0,
        today_high=95.0 if day_type == "hot" else 35.0,
        today_low=70.0 if day_type == "hot" else 20.0,
        tomorrow_high=95.0 if day_type == "hot" else 35.0,
        tomorrow_low=70.0 if day_type == "hot" else 20.0,
    )


class TestVacationReachesTheThermostat:
    """Would fail if the engine/scheduler/chart stopped reading the separate vacation setpoints."""

    _CONFIG = {"comfort_heat": 70.0, "comfort_cool": 75.0, "setback_heat": 62.0, "setback_cool": 80.0}

    @pytest.mark.parametrize(
        ("extra", "expected"),
        [({}, (59.0, 83.0)), ({"vacation_heat": 55.0, "vacation_cool": 88.0}, (55.0, 88.0))],
    )
    def test_engine_band(self, extra, expected):
        from custom_components.climate_advisor.automation import select_comfort_band
        from custom_components.climate_advisor.const import OCCUPANCY_VACATION

        band = select_comfort_band(
            _classification("cold"),
            {**self._CONFIG, **extra},
            occupancy_mode=OCCUPANCY_VACATION,
            in_sleep_window=False,
            aggressive_savings=False,
        )
        assert (band.floor, band.ceiling) == expected

    def test_vacation_handler_writes_the_separate_heat_setpoint(self):
        from custom_components.climate_advisor.automation import AutomationEngine
        from custom_components.climate_advisor.const import OCCUPANCY_VACATION

        def _close(coro):
            coro.close()

        hass = MagicMock()
        hass.services.async_call = AsyncMock()
        hass.async_create_task = MagicMock(side_effect=_close)
        state = MagicMock()
        state.state = "heat"
        state.attributes = {"hvac_modes": ["off", "heat", "cool"], "supported_features": 1}
        hass.states.get.return_value = state
        engine = AutomationEngine(
            hass=hass,
            climate_entity="climate.thermostat",
            weather_entity="weather.forecast_home",
            door_window_sensors=[],
            notify_service="notify.notify",
            config={**self._CONFIG, "vacation_heat": 55.0, "notify_service": "notify.notify"},
        )
        engine._current_classification = _classification("cold")
        engine.set_occupancy_mode(OCCUPANCY_VACATION)
        asyncio.run(engine.handle_occupancy_vacation())
        written = [
            float((c.args[2] if len(c.args) > 2 else c.kwargs.get("service_data", {}))["temperature"])
            for c in hass.services.async_call.await_args_list
            if c.args[1] == "set_temperature"
            and "temperature" in (c.args[2] if len(c.args) > 2 else c.kwargs.get("service_data", {}))
        ]
        assert 55.0 in written
        assert 59.0 not in written  # not the old away - 3

    def test_chart_band(self):
        from custom_components.climate_advisor.coordinator import _compute_target_band_schedule

        now = datetime(2026, 1, 5, 12, 0, tzinfo=UTC)
        rows = _compute_target_band_schedule(
            [now], {**self._CONFIG, "vacation_heat": 55.0, "vacation_cool": 88.0}, "vacation", now
        )
        assert (rows[0]["lower"], rows[0]["upper"]) == (55.0, 88.0)

    def test_scheduler_tou_vacation_uses_separate_heat(self):
        from custom_components.climate_advisor.const import OCCUPANCY_VACATION
        from custom_components.climate_advisor.scheduler import (
            COST_TAG_HIGH,
            Schedule,
            TOUPhase,
            resolve_tou_away_vacation_phase,
        )

        thermal_model = {"k_passive": -0.25, "confidence_k_passive": "medium", "k_active_heat": 3.0}
        schedule = Schedule(id="s1", name="s", days=("all",), start="17:00", end="21:00", cost_tag=COST_TAG_HIGH)
        res = resolve_tou_away_vacation_phase(
            [schedule],
            datetime(2026, 1, 5, 16, 0, tzinfo=UTC),
            55.0,
            20.0,
            "heat",
            thermal_model,
            {**self._CONFIG, "vacation_heat": 55.0},
            OCCUPANCY_VACATION,
        )
        assert res.phase == TOUPhase.PRECONDITIONING
        assert res.target == pytest.approx(55.0 + res.expansion_f)  # preheat above the vacation floor


# ---------------------------------------------------------------------------
# The REAL setpoints steps: warn once, keep typed values, save the identical resubmit
# ---------------------------------------------------------------------------

_ENTRY = {
    "weather_entity": "weather.forecast_home",
    "climate_entity": "climate.living_room",
    "notify_service": "notify.notify",
    "temp_unit": "fahrenheit",
    **_OK,
}


def _climate_state(modes=("off", "heat", "cool"), min_temp=None, max_temp=None):
    state = MagicMock()
    attrs = {"hvac_modes": list(modes)}
    if min_temp is not None:
        attrs["min_temp"] = min_temp
    if max_temp is not None:
        attrs["max_temp"] = max_temp
    state.attributes = attrs
    return state


def _options_flow(entry: dict, state=None):
    from custom_components.climate_advisor.config_flow import ClimateAdvisorOptionsFlow

    flow = object.__new__(ClimateAdvisorOptionsFlow)
    flow._updates, flow._removed, flow._editing_schedule_id = {}, set(), None
    flow._setpoints_acknowledged = {}
    entry_obj = MagicMock()
    entry_obj.data = dict(entry)
    entry_obj.entry_id = "e1"
    flow.config_entry = entry_obj
    captured: dict = {}

    def _update(e, data):
        captured["data"] = data
        e.data = data

    hass = MagicMock()
    hass.config_entries.async_update_entry = MagicMock(side_effect=_update)
    hass.states.get = MagicMock(return_value=state)
    hass.config.units.temperature_unit = "°F"
    flow.hass = hass
    flow.async_step_init = AsyncMock(return_value={"type": "menu", "step_id": "init"})
    return flow, captured


def _config_flow(state=None, unit="fahrenheit"):
    from custom_components.climate_advisor.config_flow import ClimateAdvisorConfigFlow

    flow = object.__new__(ClimateAdvisorConfigFlow)
    flow._data = {"climate_entity": "climate.living_room", "temp_unit": unit}
    flow._whf_acknowledged = {}
    flow._setpoints_acknowledged = {}
    hass = MagicMock()
    hass.states.get = MagicMock(return_value=state)
    hass.config.units.temperature_unit = "°C" if unit == "celsius" else "°F"
    flow.hass = hass
    flow.async_step_temperature_sources = AsyncMock(return_value={"type": "form", "step_id": "temperature_sources"})
    return flow


def _defaults(result) -> dict:
    import voluptuous as vol

    out = {}
    for k in result["data_schema"].schema:
        if k.default is not vol.UNDEFINED:
            out[str(k)] = k.default()
        elif isinstance(getattr(k, "description", None), dict):
            out[str(k)] = k.description.get("suggested_value")
    return out


class TestOptionsSetpointsStep:
    def test_coherent_values_save_first_time(self):
        flow, captured = _options_flow(_ENTRY, _climate_state())
        result = asyncio.run(flow.async_step_setpoints(dict(_OK)))
        assert result["step_id"] == "init"
        assert captured["data"]["setback_heat"] == 62.0

    def test_issue_1347_mild_away_heat_in_celsius_is_accepted(self):
        """The reporter's case: a mini-split owner holds 20 °C while away (home 21 °C)."""
        from custom_components.climate_advisor.temperature import CELSIUS, to_fahrenheit

        entry = {**_ENTRY, "temp_unit": "celsius"}
        flow, captured = _options_flow(entry, _climate_state())
        user = {
            "comfort_heat": 21.0,
            "comfort_cool": 25.0,
            "setback_heat": 20.0,
            "setback_cool": 28.0,
            "sleep_heat": 20.0,
            "sleep_cool": 26.0,
        }
        result = asyncio.run(flow.async_step_setpoints(dict(user)))
        assert result["step_id"] == "init", result.get("errors")
        assert captured["data"]["setback_heat"] == pytest.approx(to_fahrenheit(20.0, CELSIUS))

    def test_ranges_are_physical_sanity_bounds_only(self):
        from custom_components.climate_advisor.config_flow import setpoint_slider_ranges

        for key, (mn, mx, _d, _s) in setpoint_slider_ranges(False).items():
            assert (mn, mx) == (40.0, 95.0), key
        for key, (mn, mx, _d, step) in setpoint_slider_ranges(True).items():
            assert (mn, mx, step) == (4.0, 35.0, 0.5), key

    def test_warns_once_keeps_typed_values_then_saves_identical_resubmit(self):
        flow, captured = _options_flow(_ENTRY, _climate_state())
        user = _with(comfort_heat=74.0, comfort_cool=72.0)

        first = asyncio.run(flow.async_step_setpoints(dict(user)))
        assert first["type"] == "form"
        assert first["errors"] == {"comfort_heat": WARN_BAND_INVERTED}
        shown = _defaults(first)
        assert (shown["comfort_heat"], shown["comfort_cool"]) == (74.0, 72.0)  # typed values kept
        assert "data" not in captured

        second = asyncio.run(flow.async_step_setpoints(dict(user)))
        assert second["step_id"] == "init"
        assert (captured["data"]["comfort_heat"], captured["data"]["comfort_cool"]) == (74.0, 72.0)

    def test_a_changed_resubmit_is_judged_again(self):
        flow, captured = _options_flow(_ENTRY, _climate_state())
        asyncio.run(flow.async_step_setpoints(_with(comfort_heat=74.0, comfort_cool=72.0)))
        again = asyncio.run(flow.async_step_setpoints(_with(comfort_heat=73.0, comfort_cool=72.0)))
        assert again["errors"] == {"comfort_heat": WARN_BAND_INVERTED}
        assert "data" not in captured

    def test_fixing_the_value_saves_without_a_second_submit(self):
        flow, captured = _options_flow(_ENTRY, _climate_state())
        asyncio.run(flow.async_step_setpoints(_with(comfort_heat=74.0, comfort_cool=72.0)))
        fixed = asyncio.run(flow.async_step_setpoints(dict(_OK)))
        assert fixed["step_id"] == "init"

    def test_heat_only_thermostat_is_not_warned_about_bands(self):
        flow, _captured = _options_flow(_ENTRY, _climate_state(modes=("off", "heat")))
        result = asyncio.run(flow.async_step_setpoints(_with(comfort_heat=74.0, comfort_cool=72.0)))
        assert result["step_id"] == "init"

    def test_thermostat_limits_warn(self):
        flow, _captured = _options_flow(_ENTRY, _climate_state(min_temp=7.0, max_temp=32.0))
        flow.hass.config.units.temperature_unit = "°C"  # min/max are in HA's system unit
        result = asyncio.run(flow.async_step_setpoints(_with(setback_heat=42.0, sleep_heat=64.0)))
        # 7 °C = 44.6 °F, so 42 °F is below what the thermostat accepts (freeze advisory ranks lower).
        assert result["errors"] == {"setback_heat": WARN_OUTSIDE_THERMOSTAT}

    def test_vacation_fields_save_and_clear(self):
        flow, captured = _options_flow(_ENTRY, _climate_state())
        asyncio.run(flow.async_step_setpoints({**_OK, "vacation_heat": 55.0, "vacation_cool": 86.0}))
        assert (captured["data"]["vacation_heat"], captured["data"]["vacation_cool"]) == (55.0, 86.0)

        flow2, captured2 = _options_flow(captured["data"], _climate_state())
        shown = _defaults(asyncio.run(flow2.async_step_setpoints(None)))
        assert (shown["vacation_heat"], shown["vacation_cool"]) == (55.0, 86.0)
        asyncio.run(flow2.async_step_setpoints(dict(_OK)))  # both left blank
        assert "vacation_heat" not in captured2["data"]
        assert "vacation_cool" not in captured2["data"]

    def test_blank_vacation_fields_are_offered_empty(self):
        flow, _captured = _options_flow(_ENTRY, _climate_state())
        shown = _defaults(asyncio.run(flow.async_step_setpoints(None)))
        assert shown["vacation_heat"] is None and shown["vacation_cool"] is None

    def test_celsius_resave_keeps_stored_fahrenheit_exactly(self):
        """Row 12: re-saving a °C page untouched never moves a stored setpoint by rounding."""
        entry = {**_ENTRY, "temp_unit": "celsius", "setback_heat": 63.0, "comfort_heat": 68.0}
        flow, captured = _options_flow(entry, _climate_state())
        shown = _defaults(asyncio.run(flow.async_step_setpoints(None)))
        asyncio.run(flow.async_step_setpoints({k: v for k, v in shown.items() if v is not None}))
        for key in ("comfort_heat", "comfort_cool", "setback_heat", "setback_cool", "sleep_heat", "sleep_cool"):
            assert captured["data"][key] == entry[key], key


class TestInitialSetupSetpointsStep:
    def test_old_ordering_error_is_now_a_warning(self):
        flow = _config_flow(_climate_state())
        user = _with(setback_heat=70.0, sleep_heat=70.0)  # away heat above home heat (68)
        first = asyncio.run(flow.async_step_setpoints(dict(user)))
        assert first["errors"] == {"setback_heat": WARN_AWAY_HEAT_ABOVE_HOME}
        assert _defaults(first)["setback_heat"] == 70.0  # typed value kept, not reset to the default
        second = asyncio.run(flow.async_step_setpoints(dict(user)))
        assert second["step_id"] == "temperature_sources"
        assert flow._data["setback_heat"] == 70.0

    def test_away_equal_home_saves_without_warning(self):
        flow = _config_flow(_climate_state())
        result = asyncio.run(flow.async_step_setpoints(_with(setback_heat=68.0, sleep_heat=68.0)))
        assert result["step_id"] == "temperature_sources"

    def test_celsius_values_are_judged_in_fahrenheit(self):
        flow = _config_flow(_climate_state(), unit="celsius")
        user = {
            "comfort_heat": 22.0,
            "comfort_cool": 22.0,  # equal -> heat and AC fight
            "setback_heat": 17.0,
            "setback_cool": 27.0,
            "sleep_heat": 18.0,
            "sleep_cool": 25.0,
        }
        result = asyncio.run(flow.async_step_setpoints(dict(user)))
        assert result["errors"] == {"comfort_heat": WARN_BAND_INVERTED}

    def test_no_thermostat_state_is_treated_as_two_mode_without_limits(self):
        flow = _config_flow(None)
        result = asyncio.run(flow.async_step_setpoints(_with(comfort_heat=74.0, comfort_cool=72.0)))
        assert result["errors"] == {"comfort_heat": WARN_BAND_INVERTED}


# ---------------------------------------------------------------------------
# Strings: every warning has text in both flows; the old blocking errors are gone
# ---------------------------------------------------------------------------


class TestWarningStrings:
    _STRINGS = Path(__file__).resolve().parent.parent / "custom_components" / "climate_advisor" / "strings.json"

    @pytest.mark.parametrize("flow", ["config", "options"])
    def test_every_warning_has_text_ending_with_the_resubmit_hint(self, flow):
        errors = json.loads(self._STRINGS.read_text(encoding="utf-8"))[flow]["error"]
        for key in WARNING_PRIORITY:
            assert key in errors, f"{flow}: {key}"
            assert errors[key].endswith("Submit again to keep this."), key

    @pytest.mark.parametrize("flow", ["config", "options"])
    def test_old_blocking_errors_are_removed(self, flow):
        errors = json.loads(self._STRINGS.read_text(encoding="utf-8"))[flow]["error"]
        assert "setback_must_be_lower" not in errors
        assert "setback_must_be_higher" not in errors


class TestAIContextShowsTheVacationBand:
    """The AI is told the vacation band the engine actually arms, in the user's unit."""

    @pytest.mark.parametrize(
        ("config", "line"),
        [
            ({"setback_heat": 62.0, "setback_cool": 80.0}, "vacation_band:   [59, 83] (away -/+ 3°F)"),
            (
                {"setback_heat": 62.0, "setback_cool": 80.0, "vacation_heat": 52.0},
                "vacation_band:   [52, 83] (configured)",
            ),
            (
                {"temp_unit": "celsius", "setback_heat": 62.0, "setback_cool": 80.0},
                "vacation_band:   [15, 28.3] (away -/+ 3°F)",
            ),
        ],
    )
    def test_line(self, config, line):
        from custom_components.climate_advisor.ai_skills_context import build_config_context

        coord = MagicMock()
        coord.config = config
        assert line in asyncio.run(build_config_context(None, coord))
