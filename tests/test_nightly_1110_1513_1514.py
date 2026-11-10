"""The 2026-11-10 nightly (run 42838336649, seeds 16001-16012): Issues #1513 and #1514.

* #1513 (harness): a simulated restart kept the observe-only whole-house-fan notice flag a real restart resets, so
  Climate Advisor sent no second notice and P9 called a noticed sealed run a miss (day 16007000162).
* #1514 (narrow gap, owner ruling 2026-11-10): with a sleep band setup warns is narrow or inverted (67/67 F), the
  overnight nat-vent target sleep_heat + hysteresis (68 F) sits outside the band. That is the expected result of the
  setting: P8 excuses it as ``narrow_gap`` (counted, printed), and Climate Advisor's log names the narrow gap.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock, patch

from custom_components.climate_advisor.automation import AutomationEngine
from custom_components.climate_advisor.setpoint_combinations import (
    WARN_BAND_INVERTED,
    WARN_BAND_NARROW,
    narrow_band_findings,
)
from tools.sim_harness import generator
from tools.sim_harness import surfaces as S

SEALED_DAY = 16007000162
NARROW_DAY = 16012000040


def _surfaces(day_seed: int) -> S.SurfaceProbe:
    holder: dict[str, S.SurfaceProbe] = {}

    def _probe(**kw: Any) -> S.SurfaceProbe:
        holder["p"] = S.SurfaceProbe(sample_every=S.GENERATED_SAMPLE_EVERY, all_events=False, **kw)
        return holder["p"]

    with patch.object(generator, "PromiseProbe", _probe):
        generator.run_day(day_seed)
    return holder["p"]


# ── #1513: the simulated restart resets what a real restart resets ───────────────────────────────────────────────────
def test_sealed_fan_noticed_in_observe_only_stays_noticed_across_a_restart() -> None:
    rec = generator.run_day(SEALED_DAY)
    assert not [(h.ts, h.detail) for h in rec.report.hard if h.promise == "whf_sealed"]


# ── #1514: setup's rule, one source ──────────────────────────────────────────────────────────────────────────────────
def test_narrow_band_findings_use_setups_rule() -> None:
    base = {"comfort_heat": 68.0, "comfort_cool": 74.0, "setback_heat": 60.0, "setback_cool": 80.0}
    assert narrow_band_findings(base | {"sleep_heat": 65.0, "sleep_cool": 72.0}) == {}
    assert narrow_band_findings(base | {"sleep_heat": 67.0, "sleep_cool": 67.0}) == {"sleep_heat": WARN_BAND_INVERTED}
    assert narrow_band_findings(base | {"sleep_heat": 67.0, "sleep_cool": 69.0}) == {"sleep_heat": WARN_BAND_NARROW}
    # another warning on the same field (sleep heat below away) never hides the band warning
    assert narrow_band_findings(base | {"sleep_heat": 59.0, "sleep_cool": 61.0}) == {"sleep_heat": WARN_BAND_NARROW}
    # a heat-only thermostat cannot take turns: setup does not warn, nor does this
    assert narrow_band_findings(base | {"sleep_heat": 67.0, "sleep_cool": 67.0}, supports_cool=False) == {}


# ── #1514: P8 excuses only what the narrow gap explains ──────────────────────────────────────────────────────────────
def test_the_nightly_day_excuses_the_narrow_sleep_band_and_says_so() -> None:
    probe = _surfaces(NARROW_DAY)
    assert not [S.format_divergence(d) for d in probe.divergences if d.surface == "nat_vent_target"]
    assert probe.excused.get("nat_vent_target:narrow_gap", 0) >= 1, "the excuse must be counted, not silent"


def _probe_on(config: dict, *, sleeping: bool) -> S.SurfaceProbe:
    probe = S.SurfaceProbe(scenario_name="t")
    probe._session = SimpleNamespace(coordinator=SimpleNamespace(config=config))
    probe._time_band = lambda _now: "sleep" if sleeping else "awake"  # type: ignore[method-assign]
    return probe


def test_control_an_engine_narrowed_range_on_a_normal_band_is_not_excused() -> None:
    normal = {"comfort_heat": 68.0, "comfort_cool": 74.0, "sleep_heat": 64.0, "sleep_cool": 72.0}
    now = datetime(2026, 5, 13, 1, 0, tzinfo=UTC)
    narrow_range = S.Armed(floor=67.0, ceiling=67.0, active="floor")
    assert _probe_on(normal, sleeping=True)._narrow_band_in_force(now, narrow_range) is False
    narrow_sleep = normal | {"sleep_heat": 67.0, "sleep_cool": 67.0}
    assert _probe_on(narrow_sleep, sleeping=True)._narrow_band_in_force(now, narrow_range) is True
    # the narrow SLEEP band does not excuse a daytime sample on the normal home band
    assert _probe_on(narrow_sleep, sleeping=False)._narrow_band_in_force(now, narrow_range) is False
    # a wide armed range is never excused, whatever the config says
    wide = S.Armed(floor=67.0, ceiling=72.0, active="floor")
    assert _probe_on(narrow_sleep, sleeping=True)._narrow_band_in_force(now, wide) is False


# ── #1514: the log names the narrow gap as the cause ─────────────────────────────────────────────────────────────────
def _coordinator(config: dict) -> Any:
    from custom_components.climate_advisor.coordinator import ClimateAdvisorCoordinator  # noqa: PLC0415

    coord = object.__new__(ClimateAdvisorCoordinator)
    coord.hass = MagicMock()
    coord.hass.states.get.return_value = SimpleNamespace(attributes={"hvac_modes": ["off", "heat", "cool"]})
    coord.config = {"climate_entity": "climate.t"} | config
    return coord


def test_start_up_logs_each_narrow_band_as_the_cause(caplog) -> None:
    cfg = {"comfort_heat": 68.0, "comfort_cool": 74.0, "setback_heat": 60.0, "setback_cool": 80.0}
    with caplog.at_level(logging.WARNING, logger="custom_components.climate_advisor.coordinator"):
        _coordinator(cfg | {"sleep_heat": 67.0, "sleep_cool": 67.0})._log_narrow_setpoint_bands()
    lines = [r.getMessage() for r in caplog.records if "Setpoint band" in r.getMessage()]
    assert len(lines) == 1 and "'sleep' is inverted" in lines[0] and "natural ventilation" in lines[0], lines

    caplog.clear()
    with caplog.at_level(logging.WARNING, logger="custom_components.climate_advisor.coordinator"):
        _coordinator(cfg | {"sleep_heat": 64.0, "sleep_cool": 72.0})._log_narrow_setpoint_bands()
    assert not [r for r in caplog.records if "Setpoint band" in r.getMessage()], "a normal setup logs nothing"


def _engine(config: dict) -> AutomationEngine:
    hass = MagicMock()
    return AutomationEngine(
        hass=hass,
        climate_entity="climate.t",
        weather_entity="weather.t",
        door_window_sensors=["binary_sensor.w"],
        notify_service="notify.notify",
        config={"comfort_heat": 68, "comfort_cool": 74, "temp_unit": "fahrenheit"} | config,
    )


def test_the_overnight_target_above_the_band_is_logged_once_a_night(caplog) -> None:
    engine = _engine({"sleep_heat": 67.0, "sleep_cool": 67.0})
    with caplog.at_level(logging.INFO, logger="custom_components.climate_advisor.automation"):
        engine._note_sleep_vent_target_above_band(68.0, 67.0)
        engine._note_sleep_vent_target_above_band(68.0, 67.0)
    lines = [r.getMessage() for r in caplog.records if "overnight target" in r.getMessage()]
    assert len(lines) == 1 and "narrow gap" in lines[0], lines

    caplog.clear()
    normal = _engine({"sleep_heat": 64.0, "sleep_cool": 72.0})
    with caplog.at_level(logging.INFO, logger="custom_components.climate_advisor.automation"):
        normal._note_sleep_vent_target_above_band(65.0, 64.0)
    assert not [r for r in caplog.records if "overnight target" in r.getMessage()]
