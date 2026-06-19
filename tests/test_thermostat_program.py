"""Tests for comfort-band selection and actuation (Issue #249, P3).

``select_comfort_band`` is the pure decision layer: maps day classification + occupancy +
sleep window + savings posture into a capability-free ``ComfortBand`` (floor/ceiling/active).
``_apply_comfort_band`` is the actuation primitive: reads live thermostat capabilities and
emits the correct service-call shape (dual / cool / heat / no-op).

All-homes matrix: day type × occupancy × sleep × aggressive.
Actuation matrix: dual-capable / cool-only / heat-only / no-capable × active edge.
"""

from __future__ import annotations

import asyncio
from datetime import datetime
from unittest.mock import AsyncMock, MagicMock

from custom_components.climate_advisor.automation import (
    AutomationEngine,
    ComfortBand,
    _in_sleep_window,
    select_comfort_band,
)
from custom_components.climate_advisor.classifier import DayClassification
from custom_components.climate_advisor.const import (
    CLIMATE_FEATURE_TARGET_TEMP_RANGE,
    DAY_TYPE_COLD,
    DAY_TYPE_COOL,
    DAY_TYPE_HOT,
    DAY_TYPE_MILD,
    DAY_TYPE_WARM,
    OCCUPANCY_AWAY,
    OCCUPANCY_HOME,
    OCCUPANCY_VACATION,
)

# ---------------------------------------------------------------------------
# Config and classification helpers
# ---------------------------------------------------------------------------

CONFIG = {
    "comfort_heat": 70.0,
    "comfort_cool": 74.0,
    "setback_heat": 60.0,
    "setback_cool": 80.0,
    "sleep_heat": 66.0,
    "sleep_cool": 78.0,
}


def _classification(day_type: str, *, pre_condition_target: float | None = None) -> DayClassification:
    """Build a real DayClassification; hvac_mode is derived from day_type by the classifier."""
    c = DayClassification(
        day_type=day_type,
        trend_direction="stable",
        trend_magnitude=0.0,
        today_high=80.0,
        today_low=60.0,
        tomorrow_high=80.0,
        tomorrow_low=60.0,
    )
    if pre_condition_target is not None:
        c.pre_condition = True
        c.pre_condition_target = pre_condition_target
    return c


def _band(day_type, *, occupancy=OCCUPANCY_HOME, sleep=False, aggressive=False, pre=None):
    """Call select_comfort_band with the shared CONFIG."""
    return select_comfort_band(
        _classification(day_type, pre_condition_target=pre),
        CONFIG,
        occupancy_mode=occupancy,
        in_sleep_window=sleep,
        aggressive_savings=aggressive,
    )


# ---------------------------------------------------------------------------
# select_comfort_band — pure function; all-homes matrix
# ---------------------------------------------------------------------------


class TestSelectComfortBandWarmDays:
    """Warm/mild/hot days defend the ceiling; floor is the full comfort_heat (no suppression)."""

    def test_warm_day_active_ceiling_setback_floor(self):
        """WARM: active="ceiling"; floor = comfort_heat (full band); ceiling = comfort_cool."""
        b = _band(DAY_TYPE_WARM)
        assert b.active == "ceiling"
        assert b.floor == 70.0  # comfort_heat — full occupied+awake band
        assert b.ceiling == 74.0  # comfort_cool — the defended edge

    def test_mild_day_same_as_warm(self):
        b = _band(DAY_TYPE_MILD)
        assert b.active == "ceiling"
        assert (b.floor, b.ceiling) == (70.0, 74.0)

    def test_hot_day_defends_ceiling_with_possible_precool(self):
        """HOT day: same ceiling defense; pre-cool offset may lower ceiling."""
        c = _classification(DAY_TYPE_HOT)
        b = select_comfort_band(
            c, CONFIG, occupancy_mode=OCCUPANCY_HOME, in_sleep_window=False, aggressive_savings=False
        )
        offset = float(c.pre_condition_target) if (c.pre_condition_target and c.pre_condition_target < 0) else 0.0
        assert b.active == "ceiling"
        assert b.ceiling == 74.0 + offset
        assert b.floor == 70.0  # comfort_heat — full occupied+awake band

    def test_hot_day_precool_lowers_ceiling(self):
        """Pre-cool offset of -2°F lowers ceiling from 74 to 72."""
        b = _band(DAY_TYPE_HOT, pre=-2.0)
        assert b.ceiling == 72.0
        assert b.floor == 70.0

    def test_aggressive_savings_widens_active_ceiling(self):
        """aggressive_savings widens BOTH edges: floor = comfort_heat - 2 = 68, ceiling = 74 + 2 = 76."""
        b = _band(DAY_TYPE_WARM, aggressive=True)
        assert b.active == "ceiling"
        assert b.ceiling == 76.0
        assert b.floor == 68.0  # comfort_heat - 2.0 (savings widens both edges)


class TestSelectComfortBandColdDays:
    """Cold/cool days defend the floor; ceiling is the full comfort_cool (no suppression)."""

    def test_cold_day_active_floor_setback_ceiling(self):
        """COLD: active="floor"; floor = comfort_heat (defended); ceiling = comfort_cool (full band)."""
        b = _band(DAY_TYPE_COLD)
        assert b.active == "floor"
        assert b.floor == 70.0  # comfort_heat — the defended edge
        assert b.ceiling == 74.0  # comfort_cool — full occupied+awake band

    def test_cool_day_same_as_cold(self):
        b = _band(DAY_TYPE_COOL)
        assert b.active == "floor"
        assert (b.floor, b.ceiling) == (70.0, 74.0)

    def test_aggressive_savings_widens_active_floor(self):
        """aggressive_savings widens BOTH edges: floor = comfort_heat - 2 = 68, ceiling = comfort_cool + 2 = 76."""
        b = _band(DAY_TYPE_COLD, aggressive=True)
        assert b.active == "floor"
        assert b.floor == 68.0
        assert b.ceiling == 76.0  # comfort_cool + 2.0 (savings widens both edges)


class TestSelectComfortBandOccupancy:
    """Away/vacation override the day-type band with setback values."""

    def test_away_uses_setback_band_active_ceiling(self):
        """Away: setback_heat floor, setback_cool ceiling; active="ceiling"."""
        b = _band(DAY_TYPE_WARM, occupancy=OCCUPANCY_AWAY)
        assert b.active == "ceiling"
        assert (b.floor, b.ceiling) == (60.0, 80.0)

    def test_away_cold_day_same_setback_band(self):
        """Away overrides day-type — cold day still gets setback band, active="ceiling"."""
        b = _band(DAY_TYPE_COLD, occupancy=OCCUPANCY_AWAY)
        assert b.active == "ceiling"
        assert (b.floor, b.ceiling) == (60.0, 80.0)

    def test_vacation_uses_deeper_setback_band(self):
        """Vacation: setback ± VACATION_SETBACK_EXTRA (3°F); active="ceiling"."""
        b = _band(DAY_TYPE_WARM, occupancy=OCCUPANCY_VACATION)
        assert b.active == "ceiling"
        assert (b.floor, b.ceiling) == (57.0, 83.0)  # 60-3 / 80+3

    def test_away_overrides_aggressive_savings(self):
        """Savings margin only applies to the comfort band; away setback is unaffected."""
        b = _band(DAY_TYPE_WARM, occupancy=OCCUPANCY_AWAY, aggressive=True)
        assert (b.floor, b.ceiling) == (60.0, 80.0)


class TestSelectComfortBandSleep:
    """Sleep window uses sleep_heat/sleep_cool; active edge follows day type."""

    def test_sleep_warm_night_active_ceiling(self):
        """Sleep on a warm night: active="ceiling" (ceiling-threat day)."""
        b = _band(DAY_TYPE_WARM, sleep=True)
        assert b.active == "ceiling"
        assert (b.floor, b.ceiling) == (66.0, 78.0)

    def test_sleep_cold_night_active_floor(self):
        """Sleep on a cold night: active="floor" (floor-threat day)."""
        b = _band(DAY_TYPE_COLD, sleep=True)
        assert b.active == "floor"
        assert (b.floor, b.ceiling) == (66.0, 78.0)


class TestComfortBandInvariants:
    """ComfortBand dataclass invariants."""

    def test_comfort_band_is_frozen(self):
        """ComfortBand must be immutable — guards against accidental mutation in callers."""
        b = _band(DAY_TYPE_WARM)
        try:
            b.ceiling = 99.0  # type: ignore[misc]
        except Exception as exc:
            assert "frozen" in type(exc).__name__.lower() or "attribute" in str(exc).lower()
        else:
            raise AssertionError("ComfortBand should be immutable")

    def test_reason_is_descriptive(self):
        """Reason string must include the band numbers and day type for log readability."""
        b = _band(DAY_TYPE_WARM)
        assert "comfort" in b.reason
        assert "74" in b.reason  # comfort_cool ceiling


# ---------------------------------------------------------------------------
# _in_sleep_window — unchanged pure helper
# ---------------------------------------------------------------------------


class TestInSleepWindow:
    CFG = {"sleep_time": "22:30", "wake_time": "07:00"}

    def test_late_evening_in_window(self):
        assert _in_sleep_window(datetime(2026, 6, 10, 23, 0), self.CFG) is True

    def test_early_morning_in_window_wraparound(self):
        assert _in_sleep_window(datetime(2026, 6, 10, 6, 0), self.CFG) is True

    def test_midday_out_of_window(self):
        assert _in_sleep_window(datetime(2026, 6, 10, 12, 0), self.CFG) is False

    def test_exactly_at_sleep_time_in_window(self):
        assert _in_sleep_window(datetime(2026, 6, 10, 22, 30), self.CFG) is True

    def test_exactly_at_wake_time_out_of_window(self):
        assert _in_sleep_window(datetime(2026, 6, 10, 7, 0), self.CFG) is False

    def test_missing_wake_time_returns_false(self):
        assert _in_sleep_window(datetime(2026, 6, 10, 23, 0), {"sleep_time": "22:30"}) is False

    def test_malformed_time_returns_false(self):
        assert _in_sleep_window(datetime(2026, 6, 10, 23, 0), {"sleep_time": "bad", "wake_time": "07:00"}) is False

    def test_hhmmss_format_in_window(self):
        # HA time selector stores values as "HH:MM:SS" — must parse correctly (Issue #336)
        cfg = {"sleep_time": "22:30:00", "wake_time": "07:00:00"}
        assert _in_sleep_window(datetime(2026, 6, 10, 23, 0), cfg) is True

    def test_hhmmss_format_after_sleep_time_in_window(self):
        # 5 min after sleep — the exact scenario from Issue #335
        cfg = {"sleep_time": "21:00:00", "wake_time": "07:00:00"}
        assert _in_sleep_window(datetime(2026, 6, 18, 21, 5), cfg) is True

    def test_hhmmss_format_out_of_window(self):
        cfg = {"sleep_time": "22:30:00", "wake_time": "07:00:00"}
        assert _in_sleep_window(datetime(2026, 6, 10, 12, 0), cfg) is False


# ---------------------------------------------------------------------------
# _apply_comfort_band — actuation primitive (requires capability stub)
# ---------------------------------------------------------------------------
#
# These tests verify the command shape emitted by _apply_comfort_band:
# - dual-capable → ONE atomic set_temperature(hvac_mode="heat_cool", low, high) — Fix 4, Issue #290
# - cool-only + active="ceiling" → set_hvac_mode("cool") + set_temperature(ceiling)
# - heat-only + active="floor" → set_hvac_mode("heat") + set_temperature(floor)
# - no capable mode → NO service calls (band not armed)
# - dry_run → DRY RUN log lines; no actual calls


def _consume_coroutine(coro):
    """Close AsyncMock coroutines to prevent 'never awaited' warnings in the full suite."""
    if asyncio.iscoroutine(coro):
        coro.close()


def _make_apply_engine(
    *,
    hvac_modes: list[str],
    supported_features: int,
    current_mode: str = "off",
    dry_run: bool = False,
    config_overrides: dict | None = None,
) -> AutomationEngine:
    """Build a minimal AutomationEngine wired to test _apply_comfort_band.

    Sets ``hvac_modes`` + ``supported_features`` on the climate state so
    ``_get_thermostat_capabilities()`` detects the correct capability tier.
    ``current_mode`` is the live thermostat state (used for idempotent-mode checks).
    """
    hass = MagicMock()
    hass.services = MagicMock()
    hass.services.async_call = AsyncMock()
    hass.async_create_task = MagicMock(side_effect=_consume_coroutine)

    # State returned for BOTH the capability read AND the current-mode read in _apply_comfort_band
    state = MagicMock()
    state.state = current_mode
    state.attributes = {
        "hvac_modes": hvac_modes,
        "supported_features": supported_features,
    }
    hass.states = MagicMock()
    hass.states.get = MagicMock(return_value=state)

    config = {
        "comfort_heat": 70.0,
        "comfort_cool": 74.0,
        "setback_heat": 60.0,
        "setback_cool": 80.0,
        "temp_unit": "fahrenheit",
        "notify_service": "notify.notify",
    }
    if config_overrides:
        config.update(config_overrides)

    eng = AutomationEngine(
        hass=hass,
        climate_entity="climate.test",
        weather_entity="weather.home",
        door_window_sensors=[],
        notify_service=config["notify_service"],
        config=config,
    )
    eng.dry_run = dry_run
    return eng


# dual-capable thermostat: heat_cool mode + TARGET_TEMPERATURE_RANGE feature bit
_DUAL_MODES = ["off", "heat", "cool", "heat_cool"]
_DUAL_FEATURES = CLIMATE_FEATURE_TARGET_TEMP_RANGE

# cool-only thermostat (no heat_cool, no range feature)
_COOL_MODES = ["off", "cool"]
_COOL_FEATURES = 1  # single-target only

# heat-only thermostat
_HEAT_MODES = ["off", "heat"]
_HEAT_FEATURES = 1


def _hvac_calls(eng: AutomationEngine) -> list:
    return [c for c in eng.hass.services.async_call.call_args_list if c.args[1] == "set_hvac_mode"]


def _temp_calls(eng: AutomationEngine) -> list:
    return [c for c in eng.hass.services.async_call.call_args_list if c.args[1] == "set_temperature"]


class TestApplyComfortBandDual:
    """heat_cool-capable thermostat — Issue #301: single-setpoint only.

    CA no longer uses dual setpoints (heat_cool mode). For a heat_cool-capable thermostat,
    _apply_comfort_band issues ONE set_temperature call using cool or heat mode based on
    the active edge, not heat_cool.
    """

    def test_warm_day_band_emits_single_cool_setpoint(self):
        """Warm band [floor=60, ceiling=74, active=ceiling]: ONE set_temperature call, hvac_mode='cool'.

        Issue #301: dual-setpoint (heat_cool) path removed. Even for a heat_cool-capable
        thermostat, CA sends a single cool command to avoid Ecobee comfort-program reassertion.
        """
        eng = _make_apply_engine(hvac_modes=_DUAL_MODES, supported_features=_DUAL_FEATURES, current_mode="off")
        band = ComfortBand(floor=60.0, ceiling=74.0, active="ceiling", reason="test")
        asyncio.run(eng._apply_comfort_band(band, reason="test warm"))

        # No separate set_hvac_mode call
        hvac = _hvac_calls(eng)
        assert len(hvac) == 0

        temp = _temp_calls(eng)
        # Single call (Issue #301): one set_temperature call with hvac_mode + temperature
        assert len(temp) == 1
        call_data = temp[0].args[2]
        assert call_data.get("hvac_mode") == "cool"
        assert call_data["temperature"] == 74.0  # ceiling
        # No dual-setpoint keys
        assert "target_temp_low" not in call_data
        assert "target_temp_high" not in call_data

    def test_cold_day_band_emits_single_heat_setpoint(self):
        """Cold band [floor=70, ceiling=80, active=floor]: ONE set_temperature call, hvac_mode='heat'.

        Issue #301: floor defense uses heat mode + single temperature, not heat_cool dual setpoints.
        """
        eng = _make_apply_engine(hvac_modes=_DUAL_MODES, supported_features=_DUAL_FEATURES, current_mode="off")
        band = ComfortBand(floor=70.0, ceiling=80.0, active="floor", reason="test")
        asyncio.run(eng._apply_comfort_band(band, reason="test cold"))

        hvac = _hvac_calls(eng)
        assert len(hvac) == 0

        temp = _temp_calls(eng)
        # Single call (Issue #301)
        assert len(temp) == 1
        call_data = temp[0].args[2]
        assert call_data.get("hvac_mode") == "heat"
        assert call_data["temperature"] == 70.0  # floor
        assert "target_temp_low" not in call_data
        assert "target_temp_high" not in call_data

    def test_heat_cool_capable_thermostat_uses_single_setpoint(self):
        """heat_cool-capable thermostat: CA sends cool mode, not heat_cool (Issue #301).

        The Ecobee reverts to its comfort program on heat_cool commands. Single-mode
        cool commands are held properly.
        """
        eng = _make_apply_engine(hvac_modes=_DUAL_MODES, supported_features=_DUAL_FEATURES, current_mode="heat_cool")
        band = ComfortBand(floor=60.0, ceiling=74.0, active="ceiling", reason="test")
        asyncio.run(eng._apply_comfort_band(band, reason="test single-setpoint"))

        hvac = _hvac_calls(eng)
        assert len(hvac) == 0

        temp = _temp_calls(eng)
        assert len(temp) == 1
        call_data = temp[0].args[2]
        # Must be 'cool', NOT 'heat_cool'
        assert call_data.get("hvac_mode") == "cool"
        assert call_data["temperature"] == 74.0

    def test_comfort_band_applied_event_emitted(self):
        """comfort_band_applied event contains floor, ceiling, active, mode, reason."""
        eng = _make_apply_engine(hvac_modes=_DUAL_MODES, supported_features=_DUAL_FEATURES, current_mode="off")
        events: list[tuple] = []
        eng._emit_event_callback = lambda n, d: events.append((n, d))

        band = ComfortBand(floor=60.0, ceiling=74.0, active="ceiling", reason="band reason")
        asyncio.run(eng._apply_comfort_band(band, reason="test event"))

        applied = [e for e in events if e[0] == "comfort_band_applied"]
        assert len(applied) == 1
        payload = applied[0][1]
        assert payload["floor"] == 60.0
        assert payload["ceiling"] == 74.0
        assert payload["active"] == "ceiling"
        assert payload["mode"] == "cool"


class TestApplyComfortBandCoolOnly:
    """Cool-only thermostat — arms cool mode + single ceiling setpoint."""

    def test_ceiling_band_sets_cool_mode_and_ceiling_setpoint(self):
        """active=ceiling + cool-capable → ONE set_temperature(hvac_mode='cool', temperature=ceiling).

        Issue #301: single call with hvac_mode embedded. No separate set_hvac_mode.
        """
        eng = _make_apply_engine(hvac_modes=_COOL_MODES, supported_features=_COOL_FEATURES, current_mode="off")
        band = ComfortBand(floor=60.0, ceiling=74.0, active="ceiling", reason="test")
        asyncio.run(eng._apply_comfort_band(band, reason="test cool"))

        hvac = _hvac_calls(eng)
        assert len(hvac) == 0  # no separate set_hvac_mode call

        temp = _temp_calls(eng)
        # Single call (Issue #301)
        assert len(temp) == 1
        call_data = temp[0].args[2]
        assert call_data.get("hvac_mode") == "cool"
        assert call_data["temperature"] == 74.0
        assert "target_temp_low" not in call_data

    def test_already_in_cool_mode_still_issues_single_call(self):
        """Already in cool mode → ONE set_temperature call still issued (idempotent-safe)."""
        eng = _make_apply_engine(hvac_modes=_COOL_MODES, supported_features=_COOL_FEATURES, current_mode="cool")
        band = ComfortBand(floor=60.0, ceiling=74.0, active="ceiling", reason="test")
        asyncio.run(eng._apply_comfort_band(band, reason="test idempotent cool"))

        hvac = _hvac_calls(eng)
        assert len(hvac) == 0

        temp = _temp_calls(eng)
        # Single call even in steady-state (Issue #301: hvac_mode in call bypasses HA dedup)
        assert len(temp) == 1

    def test_floor_band_no_op_cool_only_cannot_heat(self):
        """active=floor on cool-only thermostat → no service calls (can't defend the floor)."""
        eng = _make_apply_engine(hvac_modes=_COOL_MODES, supported_features=_COOL_FEATURES, current_mode="cool")
        band = ComfortBand(floor=70.0, ceiling=80.0, active="floor", reason="test")
        asyncio.run(eng._apply_comfort_band(band, reason="test no-op"))

        # Cool-only can't defend a floor threat — silent no-op, no false promise
        assert len(_hvac_calls(eng)) == 0
        assert len(_temp_calls(eng)) == 0


class TestApplyComfortBandHeatOnly:
    """Heat-only thermostat — arms heat mode + single floor setpoint."""

    def test_floor_band_sets_heat_mode_and_floor_setpoint(self):
        """active=floor + heat-capable → ONE set_temperature(hvac_mode='heat', temperature=floor).

        Issue #301: single call with hvac_mode embedded. No separate set_hvac_mode.
        """
        eng = _make_apply_engine(hvac_modes=_HEAT_MODES, supported_features=_HEAT_FEATURES, current_mode="off")
        band = ComfortBand(floor=70.0, ceiling=80.0, active="floor", reason="test")
        asyncio.run(eng._apply_comfort_band(band, reason="test heat"))

        hvac = _hvac_calls(eng)
        assert len(hvac) == 0  # no separate set_hvac_mode call

        temp = _temp_calls(eng)
        # Single call (Issue #301)
        assert len(temp) == 1
        call_data = temp[0].args[2]
        assert call_data.get("hvac_mode") == "heat"
        assert call_data["temperature"] == 70.0

    def test_ceiling_band_no_op_heat_only_cannot_cool(self):
        """active=ceiling on heat-only thermostat → no service calls (can't defend the ceiling)."""
        eng = _make_apply_engine(hvac_modes=_HEAT_MODES, supported_features=_HEAT_FEATURES, current_mode="heat")
        band = ComfortBand(floor=60.0, ceiling=74.0, active="ceiling", reason="test")
        asyncio.run(eng._apply_comfort_band(band, reason="test no-op"))

        # Heat-only can't cool — silent no-op per the spec
        assert len(_hvac_calls(eng)) == 0
        assert len(_temp_calls(eng)) == 0


class TestApplyComfortBandNoCapability:
    """No capable mode available (empty/unknown modes) → no service calls."""

    def test_no_modes_no_calls(self):
        """Entity with no capable modes → silent no-op (defensive, not a legacy fallback)."""
        eng = _make_apply_engine(hvac_modes=[], supported_features=0, current_mode="off")
        band = ComfortBand(floor=60.0, ceiling=74.0, active="ceiling", reason="test")
        asyncio.run(eng._apply_comfort_band(band, reason="test no-capable"))

        assert len(_hvac_calls(eng)) == 0
        assert len(_temp_calls(eng)) == 0


class TestApplyComfortBandDryRun:
    """dry_run=True → DRY RUN log entries; no actual service calls."""

    def test_dry_run_logs_without_service_calls(self, caplog):
        import logging

        eng = _make_apply_engine(
            hvac_modes=_DUAL_MODES, supported_features=_DUAL_FEATURES, current_mode="off", dry_run=True
        )
        band = ComfortBand(floor=60.0, ceiling=74.0, active="ceiling", reason="test")

        with caplog.at_level(logging.INFO, logger="custom_components.climate_advisor.automation"):
            asyncio.run(eng._apply_comfort_band(band, reason="test dry run"))

        eng.hass.services.async_call.assert_not_called()
        dry_msgs = [r.message for r in caplog.records if "[DRY RUN]" in r.message]
        assert len(dry_msgs) >= 1  # at least one DRY RUN log line
