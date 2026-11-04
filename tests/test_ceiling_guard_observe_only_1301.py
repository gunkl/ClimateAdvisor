"""Issue #1301: observe-only ODE ceiling guard must not record a switch to cool it never sent.

Occupant: with automation switched off, the ceiling guard's decision used to leave an Activity row
"ODE ceiling guard fired -- mode: off->cool" although nothing reached the thermostat, which still
reads Off. Now the decision is logged at INFO with the [DRY RUN] prefix and no ``ceiling_guard_fired``
event is emitted; with automation on the guard behaves exactly as before (control test).
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime
from unittest.mock import patch

from tests.test_warm_day_comfort_gap import (
    AUTOMATION_LOGGER,
    _make_cg_engine,
    _make_predicted_indoor,
    _make_warm_off_classification,
    _set_thermal_model,
)

_NOW = datetime(2026, 5, 11, 10, 0, 0, tzinfo=UTC)


def _fire_guard(*, dry_run: bool):
    """Off thermostat, outdoor above indoor, breach of the 74 F ceiling predicted in ~1.5 h."""
    engine = _make_cg_engine(comfort_cool=74.0, indoor_temp=73.0)  # thermostat reads "off"
    engine.dry_run = dry_run
    engine._last_outdoor_temp = 76.0
    _set_thermal_model(engine, k_passive=-0.05, conf="medium", k_active_cool=None)
    events: list[tuple[str, dict]] = []
    engine._emit_event_callback = lambda n, d: events.append((n, d))
    curve = _make_predicted_indoor(start_hour_utc=10, temps=[73.0, 74.5, 76.0])
    with patch("custom_components.climate_advisor.automation.dt_util") as mock_dt:
        mock_dt.now.return_value = _NOW
        asyncio.run(engine.apply_classification(_make_warm_off_classification(), predicted_indoor=curve))
    return engine, events


def test_observe_only_guard_records_no_switch_to_cool(caplog):
    caplog.set_level(logging.INFO, logger=AUTOMATION_LOGGER)
    engine, events = _fire_guard(dry_run=True)

    # Nothing reached the thermostat.
    climate_calls = [c for c in engine.hass.services.async_call.call_args_list if c.args[0] == "climate"]
    assert climate_calls == []
    # No Activity row claiming a mode switch that never happened.
    fired = [d for n, d in events if n == "ceiling_guard_fired"]
    assert fired == [], f"observe-only must not record ceiling_guard_fired: {fired}"
    # The decision is still visible in the log, with the [DRY RUN] prefix.
    assert any(
        r.levelno == logging.INFO and "[DRY RUN] ODE ceiling guard would switch to cool" in r.getMessage()
        for r in caplog.records
    )
    # Observe-only never stamps a commanded mode (nothing the selector could mistake for "cool ran").
    assert engine._last_commanded_hvac_mode is None


def test_control_automation_on_guard_still_fires_and_records_cool():
    engine, events = _fire_guard(dry_run=False)

    calls = engine.hass.services.async_call.call_args_list
    mode_calls = [c.args[2]["hvac_mode"] for c in calls if c.args[1] == "set_hvac_mode"]
    assert mode_calls and mode_calls[-1] == "cool"
    assert any(c.args[1] == "set_temperature" and c.args[2].get("temperature") == 74.0 for c in calls)
    fired = [d for n, d in events if n == "ceiling_guard_fired"]
    assert len(fired) == 1
    assert fired[0]["new_hvac_mode"] == "cool"
    assert fired[0]["old_hvac_mode"] == "off"
    assert fired[0]["new_setpoint_f"] == 74.0
    assert engine._comfort_mode_family == "cooling"
