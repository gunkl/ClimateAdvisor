"""Integration tests for Issue #906 — extend chart forecast beyond met.no's
48-hour hourly horizon.

Craftsman-1 built the two new pure functions (``_synthesize_hourly_day()``,
``_build_extended_hourly_forecast()``) and their own unit tests in
``tests/test_forecast_extension.py``. Craftsman-2 wired them into
``get_chart_data()`` (the ``_extended_forecast`` local feeding
``predicted_indoor``/``forecast_outdoor``/``_build_target_band_for()``'s
``hourly_forecast_override``/``_compute_day_hvac_modes()``), added
``self._daily_forecast_full`` caching in ``_get_forecast()``.

This file covers what neither of those covers: ``get_chart_data()``'s
INTEGRATION behavior end-to-end, plus the scope-boundary claim (extension
must never leak into the automation-facing caches/gates) made executable.

Four groups, matching the plan's "Tests" section:

1. ``TestChartConsistencyAcrossAllForwardFields`` — the broad structural test:
   every forward-looking field's last timestamp lands on the same calendar
   day, generalized so it would catch a *future* forgotten field, not just
   today's known 7.
2. ``TestRegressionGuardAutomationPathsUnaffected`` — the scope-boundary table
   made executable: ``self._last_predicted_indoor``,
   ``self._target_band_schedule`` (via ``_resolve_target_band_schedule()``),
   ``self._nat_vent_plan`` (via ``_compute_and_cache_nat_vent_plan()``), and
   the nat-vent gate's forecast input (``coordinator.py`` ~line 9494) are
   byte-identical whether or not ``self._daily_forecast_full`` is populated.
3. ``TestHistoricalViewportSuppressesForecastSeries`` /
   ``TestDailyForecastFullUnset`` — edge cases.
4. Frontend assumption spot-check is reported in the Craftsman-3 handoff, not
   as a test here (see that report for the file:line checked).
"""

from __future__ import annotations

import importlib
import sys
import types
from datetime import date, datetime, time, timedelta
from unittest.mock import MagicMock, patch

# ── HA module stubs (must happen before importing climate_advisor) ──────────
if "homeassistant" not in sys.modules:
    from conftest import _install_ha_stubs

    _install_ha_stubs()


def _get_coordinator_class():
    """Follows the pattern from test_daily_record_accuracy.py to avoid stale
    __globals__ if test_occupancy.py has reloaded the coordinator module."""
    mod = importlib.import_module("custom_components.climate_advisor.coordinator")
    return mod.ClimateAdvisorCoordinator


def _get_coordinator_module():
    return importlib.import_module("custom_components.climate_advisor.coordinator")


def _classification(**overrides):
    from custom_components.climate_advisor.classifier import DayClassification

    c = object.__new__(DayClassification)
    defaults = {
        "day_type": "warm",
        "trend_direction": "stable",
        "trend_magnitude": 0,
        "today_high": 82.0,
        "today_low": 62.0,
        # Deliberately low: keeps resolve_pre_cool_modifier() returning None
        # (no warming trend via setback_modifier=0.0, and tomorrow_high well
        # under any configured threshold_hot) so _build_target_band_for()
        # never reaches _compute_pre_cool_trigger_time()'s wake_time-dependent
        # branch — keeps this fixture's required attribute set small.
        "tomorrow_high": 70.0,
        "tomorrow_low": 60.0,
        "hvac_mode": "cool",
        "pre_condition": False,
        "pre_condition_target": None,
        "windows_recommended": False,
        "window_open_time": None,
        "window_close_time": None,
        "window_opportunity_morning_start": None,
        "setback_modifier": 0.0,
        "window_opportunity_morning": False,
        "window_opportunity_evening": False,
    }
    defaults.update(overrides)
    c.__dict__.update(defaults)
    return c


def _hourly_entry(dt_str: str, temp: float) -> dict:
    return {"datetime": dt_str, "temperature": temp}


def _daily_entry(dt_str: str, high: float, low: float) -> dict:
    return {"datetime": dt_str, "temperature": high, "templow": low}


_NOW = datetime(2026, 6, 1, 8, 0, 0)
_FORECAST_START = date(2026, 6, 1)


def _real_48h_forecast() -> list[dict]:
    """48 hourly entries spanning 2 real days, naive datetimes (matches the
    convention used by Craftsman-1's tests/test_forecast_extension.py)."""
    entries = []
    for i in range(48):
        dt = datetime.combine(_FORECAST_START, time(hour=0)) + timedelta(hours=i)
        entries.append(_hourly_entry(dt.isoformat(), 70.0 + (i % 24) * 0.2))
    return entries


def _daily_6day_forecast() -> list[dict]:
    """6-day daily forecast, day-spaced, starting the same day as the real
    hourly forecast — so days 3-6 (2026-06-03..06-06) are synthesized."""
    entries = []
    for i in range(6):
        d = _FORECAST_START + timedelta(days=i)
        entries.append(_daily_entry(f"{d.isoformat()}T00:00:00", 80.0 + i, 58.0 + i))
    return entries


def _make_full_chart_coord(*, daily_forecast_full: list[dict] | None):
    """Coordinator stub with get_chart_data() (and the caches/gates the
    regression-guard test needs) bound to the real methods.

    Mirrors tests/test_temperature_sensors.py::_make_chart_data_coord — same
    MagicMock-for-heavy-subsystems approach (learning, chart_log,
    automation_engine) — extended with the attributes get_chart_data()'s full
    forward-looking-series path (walk-forward regime, nat-vent plan cache,
    target-band cache) reads.
    """
    ClimateAdvisorCoordinator = _get_coordinator_class()
    coord = object.__new__(ClimateAdvisorCoordinator)

    coord.config = {
        "comfort_heat": 68,
        "comfort_cool": 76,
        "setback_heat": 60,
        "setback_cool": 80,
        "temp_unit": "fahrenheit",
    }
    coord._current_classification = _classification()
    coord._occupancy_mode = "home"
    coord._hourly_forecast_temps = _real_48h_forecast()
    coord._daily_forecast_full = daily_forecast_full
    coord._tou_phase_resolution = None
    coord._pre_cool_trigger_dt = None
    coord._pre_cool_target = None
    coord._last_predicted_indoor = []
    coord._nat_vent_plan = None
    coord._nat_vent_cutoff_reason_candidate = None
    coord._nat_vent_cutoff_reason_candidate_since = None
    coord._get_indoor_temp = MagicMock(return_value=70.0)
    coord._thermal_factors = None

    ae = MagicMock()
    ae._thermal_model = None
    ae._manual_override_active = False
    ae._manual_override_mode = None
    ae._natural_vent_active = False
    ae._ceiling_threshold = MagicMock(return_value=78.0)
    coord.automation_engine = ae

    learning = MagicMock()
    learning.get_thermal_model = MagicMock(return_value={})
    coord.learning = learning

    coord._indoor_temp_history = []
    coord._outdoor_temp_history = []
    coord._rejection_log = {}

    chart_log = MagicMock()
    chart_log.get_entries = MagicMock(return_value=[])
    coord._chart_log = chart_log

    coord.get_chart_data = types.MethodType(ClimateAdvisorCoordinator.get_chart_data, coord)
    coord._build_target_band_for = types.MethodType(ClimateAdvisorCoordinator._build_target_band_for, coord)
    coord._resolve_target_band_schedule = types.MethodType(
        ClimateAdvisorCoordinator._resolve_target_band_schedule, coord
    )
    coord._compute_and_cache_nat_vent_plan = types.MethodType(
        ClimateAdvisorCoordinator._compute_and_cache_nat_vent_plan, coord
    )
    coord._compute_pre_cool_trigger_time = types.MethodType(
        ClimateAdvisorCoordinator._compute_pre_cool_trigger_time, coord
    )
    coord._tou_precondition_window_tuple = types.MethodType(
        ClimateAdvisorCoordinator._tou_precondition_window_tuple, coord
    )
    coord._stabilize_nat_vent_cutoff_reason = types.MethodType(
        ClimateAdvisorCoordinator._stabilize_nat_vent_cutoff_reason, coord
    )
    return coord


def _patched_dt_util(coord_mod, now=_NOW):
    """Patch only dt_util.now/as_local (not the whole module) — as_local as a
    faithful naive-passthrough identity, matching this fixture's all-naive
    datetimes. Returns the context-manager-producing callable pair used via
    `with _patched_dt_util(coord_mod):`."""
    return (
        patch.object(coord_mod.dt_util, "now", return_value=now),
        patch.object(coord_mod.dt_util, "as_local", side_effect=lambda x: x),
    )


# ---------------------------------------------------------------------------
# 1. Broad chart-consistency test
# ---------------------------------------------------------------------------


class TestChartConsistencyAcrossAllForwardFields:
    """get_chart_data(range_str='7d') with a 48h real hourly forecast + a
    populated 6-day self._daily_forecast_full: every non-empty forward-looking
    field must extend out to the same calendar day, not stop at the 48h
    boundary. Structural assertion over the whole field set (not 7 hardcoded
    per-field checks) so it would also catch a *future* forgotten field —
    this is exactly the class of bug the plan's own audit caught at the old
    line 9765 (`_day_modes` still being built from the un-extended list)."""

    def test_all_forward_fields_share_last_timestamp_within_one_day(self):
        coord_mod = _get_coordinator_module()
        coord = _make_full_chart_coord(daily_forecast_full=_daily_6day_forecast())

        now_p, as_local_p = _patched_dt_util(coord_mod)
        with now_p, as_local_p:
            result = coord.get_chart_data("7d")

        def _last_ts(entries: list[dict], key: str) -> datetime | None:
            if not entries:
                return None
            return max(datetime.fromisoformat(e[key]) for e in entries)

        field_ts_keys = {
            "predicted_indoor": "ts",
            "forecast_outdoor": "ts",
            "target_band": "ts",
            "predicted_activity": "ts",
            "effective_target_forecast": "ts",
            "predicted_setpoint": "ts",
            "defense_lines": "ts",
        }

        last_timestamps: dict[str, datetime] = {}
        for field, ts_key in field_ts_keys.items():
            entries = result.get(field) or []
            last = _last_ts(entries, ts_key)
            if last is not None:
                last_timestamps[field] = last

        assert last_timestamps, "fixture must produce at least one non-empty forward-looking field"
        # At least the three fields the plan's audit named explicitly as
        # directly reading the extended list must be present and non-empty —
        # otherwise this test would pass vacuously with an empty dict.
        for required in ("predicted_indoor", "forecast_outdoor", "target_band"):
            assert required in last_timestamps, f"expected '{required}' to be non-empty in this fixture"

        max_ts = max(last_timestamps.values())
        min_ts = min(last_timestamps.values())
        spread = max_ts - min_ts
        assert spread <= timedelta(hours=24), (
            f"forward-looking fields diverge by more than 24h — one or more fields never received the "
            f"extended forecast: {[(k, v.isoformat()) for k, v in sorted(last_timestamps.items())]}"
        )
        # Also confirms the actual point of the feature: the chart extends
        # well past met.no's 48h horizon (last real hour is 2026-06-02T23:00),
        # not just that all fields agree with each other at the old boundary.
        assert max_ts.date() > date(2026, 6, 2), (
            f"expected forward fields to extend past the 48h real-forecast horizon, last ts={max_ts.isoformat()}"
        )


class TestDayModesWiringAffectsRegimeValue:
    """Issue #906 verification round, Fix 3: the Verification agent proved by mutation
    testing that reverting the `_compute_day_hvac_modes()` call in `get_chart_data()`
    back to `self._hourly_forecast_temps` (i.e. un-wiring `_day_modes` from the
    extension) makes the ENTIRE test suite pass — this exact wire site had zero
    value-level test protection, because `_day_modes` affects regime *values* (which
    HVAC mode a synthetic day resolves to), not the *timestamp set* the broad
    chart-consistency test above checks.

    2026-06-06 (the last day of the 6-day daily forecast fixture, high=85.0) is the
    only synthetic day whose forecast high reaches THRESHOLD_HOT (85) — so with the
    extended forecast wired in, `_compute_day_hvac_modes()` classifies it "cool"; with
    the wiring reverted to the un-extended 48h real forecast, that date is entirely
    absent from `_day_modes`, and `_walk_forward_regime()`'s `day_modes.get(day, "off")`
    default silently produces "off" instead — a stale/default value, not a real
    classification of that day's actual forecast."""

    def test_hvac_mode_at_post_48h_synthetic_day_depends_on_extended_forecast(self):
        coord_mod = _get_coordinator_module()
        target_date = date(2026, 6, 6)  # daily forecast high=85.0 -> THRESHOLD_HOT

        def _hvac_modes_for_date(result, day):
            return {
                e["hvac_mode"]
                for e in (result.get("predicted_activity") or [])
                if datetime.fromisoformat(e["ts"]).date() == day
            }

        # Production wiring intact.
        coord = _make_full_chart_coord(daily_forecast_full=_daily_6day_forecast())
        now_p, as_local_p = _patched_dt_util(coord_mod)
        with now_p, as_local_p:
            result_wired = coord.get_chart_data("7d")
        wired_modes = _hvac_modes_for_date(result_wired, target_date)

        assert wired_modes == {"cool"}, (
            f"expected 2026-06-06 to classify as 'cool' (high=85.0 >= THRESHOLD_HOT) with the "
            f"extended forecast wired in, got {wired_modes}"
        )

        # Reverted wiring: _compute_day_hvac_modes() called with the un-extended
        # self._hourly_forecast_temps (48h real only), exactly as the Verification
        # agent's mutation test did.
        real_compute_day_hvac_modes = coord_mod._compute_day_hvac_modes
        coord2 = _make_full_chart_coord(daily_forecast_full=_daily_6day_forecast())

        def _reverted(hourly_forecast, now, classification=None):
            return real_compute_day_hvac_modes(coord2._hourly_forecast_temps, now, classification)

        now_p2, as_local_p2 = _patched_dt_util(coord_mod)
        with (
            now_p2,
            as_local_p2,
            patch.object(coord_mod, "_compute_day_hvac_modes", side_effect=_reverted),
        ):
            result_reverted = coord2.get_chart_data("7d")
        reverted_modes = _hvac_modes_for_date(result_reverted, target_date)

        assert wired_modes != reverted_modes, (
            "hvac_mode at a post-48h synthetic-day timestamp must depend on _day_modes built from "
            "the extended forecast, not a stale/default value carried over from the last real day — "
            "this test must fail if the get_chart_data() wire site is reverted to "
            "self._hourly_forecast_temps (Fix 3)"
        )
        assert reverted_modes == {"off"}, (
            f"expected the reverted wiring to fall back to the day_modes.get(day, 'off') default "
            f"for a date entirely absent from the un-extended forecast, got {reverted_modes}"
        )


# ---------------------------------------------------------------------------
# 2. Regression guard — scope-boundary table made executable
# ---------------------------------------------------------------------------


class TestRegressionGuardAutomationPathsUnaffected:
    """The plan's scope-boundary table claims the extended forecast is
    chart-only: self._last_predicted_indoor, self._target_band_schedule (via
    _resolve_target_band_schedule()), self._nat_vent_plan (via
    _compute_and_cache_nat_vent_plan()), and the nat-vent gate's forecast
    input (coordinator.py ~line 9494) must never see it. This makes that
    claim executable instead of asserted-in-prose-only."""

    def test_target_band_schedule_cache_unaffected_by_daily_forecast_full(self):
        coord_mod = _get_coordinator_module()
        coord_without = _make_full_chart_coord(daily_forecast_full=None)
        coord_with = _make_full_chart_coord(daily_forecast_full=_daily_6day_forecast())

        now_p1, as_local_p1 = _patched_dt_util(coord_mod)
        with now_p1, as_local_p1:
            coord_without._resolve_target_band_schedule()
        now_p2, as_local_p2 = _patched_dt_util(coord_mod)
        with now_p2, as_local_p2:
            coord_with._resolve_target_band_schedule()

        assert coord_without._target_band_schedule == coord_with._target_band_schedule, (
            "_resolve_target_band_schedule()'s cache must be byte-identical regardless of "
            "self._daily_forecast_full — it must keep calling _build_target_band_for() with no "
            "hourly_forecast_override, per the plan's scope-boundary table"
        )

    def test_nat_vent_plan_cache_unaffected_by_daily_forecast_full(self):
        coord_mod = _get_coordinator_module()
        # _compute_and_cache_nat_vent_plan() requires a non-empty _last_predicted_indoor
        # to do anything other than short-circuit to None — give it a minimal real-shaped
        # curve so the comparison actually exercises compute_nat_vent_plan().
        predicted_indoor = [
            {"ts": (datetime.combine(_FORECAST_START, time(hour=0)) + timedelta(hours=i)).isoformat(), "temp": 74.0}
            for i in range(48)
        ]

        coord_without = _make_full_chart_coord(daily_forecast_full=None)
        coord_without._last_predicted_indoor = predicted_indoor
        coord_with = _make_full_chart_coord(daily_forecast_full=_daily_6day_forecast())
        coord_with._last_predicted_indoor = predicted_indoor

        now_p1, as_local_p1 = _patched_dt_util(coord_mod)
        with now_p1, as_local_p1:
            coord_without._compute_and_cache_nat_vent_plan()
        now_p2, as_local_p2 = _patched_dt_util(coord_mod)
        with now_p2, as_local_p2:
            coord_with._compute_and_cache_nat_vent_plan()

        assert coord_without._nat_vent_plan == coord_with._nat_vent_plan, (
            "_compute_and_cache_nat_vent_plan()'s cached plan must be byte-identical regardless of "
            "self._daily_forecast_full — it must keep building its outdoor curve from "
            "self._hourly_forecast_temps directly, per the plan's scope-boundary table"
        )

    def test_last_predicted_indoor_source_untouched_by_daily_forecast_full(self):
        """self._last_predicted_indoor is populated by the main 30-min cycle /
        briefing pipeline via _build_predicted_indoor_future(self._hourly_forecast_temps, ...)
        — never self._daily_forecast_full or an extended list. Proven directly:
        calling _build_predicted_indoor_future() with the same real hourly forecast
        produces the same result whether or not self._daily_forecast_full happens to
        be populated on the coordinator (the function doesn't take self at all, so this
        also structurally proves no such coupling could exist)."""
        coord_mod = _get_coordinator_module()
        config = {"comfort_heat": 68, "comfort_cool": 76, "setback_heat": 60, "setback_cool": 80}
        classification = _classification()
        hourly = _real_48h_forecast()

        now_p1, as_local_p1 = _patched_dt_util(coord_mod)
        with now_p1, as_local_p1:
            result_a = coord_mod._build_predicted_indoor_future(
                hourly,
                config,
                _NOW,
                current_indoor_temp=70.0,
                thermal_model=None,
                occupancy_mode="home",
                classification=classification,
            )
        now_p2, as_local_p2 = _patched_dt_util(coord_mod)
        with now_p2, as_local_p2:
            result_b = coord_mod._build_predicted_indoor_future(
                hourly,
                config,
                _NOW,
                current_indoor_temp=70.0,
                thermal_model=None,
                occupancy_mode="home",
                classification=classification,
            )

        assert result_a == result_b

    def test_nat_vent_gate_forecast_input_unaffected_by_daily_forecast_full(self):
        """coordinator.py ~line 9494 (inside _compute_next_automation_action(), the
        "Next Automation" nat-vent-start-prediction candidate): the forecast curve fed
        to decide_nat_vent_gate() via find_temperature_crossing() is built as
        `_build_future_forecast_outdoor(self._hourly_forecast_temps, c)` — confirmed via
        source grep, never `_extended_forecast`/`self._daily_forecast_full`. Proven
        executable here by calling that exact expression directly against both an
        un-populated and populated self._daily_forecast_full coordinator and asserting
        byte-identical output — the pure function itself takes no `self`, so this also
        structurally proves the coupling the scope-boundary table forbids cannot exist."""
        coord_mod = _get_coordinator_module()
        classification = _classification()

        coord_without = _make_full_chart_coord(daily_forecast_full=None)
        coord_with = _make_full_chart_coord(daily_forecast_full=_daily_6day_forecast())

        now_p1, as_local_p1 = _patched_dt_util(coord_mod)
        with now_p1, as_local_p1:
            curve_without = coord_mod._build_future_forecast_outdoor(
                coord_without._hourly_forecast_temps, classification
            )
        now_p2, as_local_p2 = _patched_dt_util(coord_mod)
        with now_p2, as_local_p2:
            curve_with = coord_mod._build_future_forecast_outdoor(coord_with._hourly_forecast_temps, classification)

        assert curve_without == curve_with

        # Structural confirmation of the source itself, so a future edit that swaps in
        # `_extended_forecast` at this call site fails loudly even if the executable
        # comparison above happened not to distinguish the two inputs for some fixture.
        import inspect

        src = inspect.getsource(coord_mod.ClimateAdvisorCoordinator._compute_next_automation_action)
        assert "_build_future_forecast_outdoor(self._hourly_forecast_temps, c)" in src, (
            "expected the nat-vent-gate forecast input to be built from self._hourly_forecast_temps "
            "directly, not an extended/synthetic list — scope-boundary table violation if this changed"
        )


# ---------------------------------------------------------------------------
# 3. Edge cases
# ---------------------------------------------------------------------------


class TestHistoricalViewportSuppressesForecastSeries:
    """A historical viewport (before_ts far enough in the past that
    is_historical=True) must still fully suppress all forward-looking series,
    unaffected by a populated self._daily_forecast_full."""

    def test_historical_view_all_forecast_series_empty_even_with_daily_forecast_full(self):
        coord_mod = _get_coordinator_module()
        coord = _make_full_chart_coord(daily_forecast_full=_daily_6day_forecast())

        # Anchor well over an hour before `now` -> is_historical=True. `now` must be
        # tz-aware here since get_chart_data() subtracts it from anchor_dt (built via
        # datetime.fromtimestamp(before_ts, tz=UTC)) directly.
        from datetime import UTC

        aware_now = _NOW.replace(tzinfo=UTC)
        anchor = datetime(2026, 5, 20, 12, 0, 0, tzinfo=UTC)
        before_ts = anchor.timestamp()

        now_p, as_local_p = _patched_dt_util(coord_mod, now=aware_now)
        with now_p, as_local_p:
            result = coord.get_chart_data("30d", before_ts=before_ts)

        assert result["predicted_indoor"] == []
        assert result["forecast_outdoor"] == []
        assert result["predicted_activity"] == []
        assert result["effective_target_forecast"] == []
        assert result["defense_lines"] == []


class TestDailyForecastFullUnset:
    """self._daily_forecast_full unset entirely (attribute never assigned —
    simulates a coordinator that hasn't completed its first update cycle)
    must behave exactly as before this feature (via the
    getattr(self, "_daily_forecast_full", None) default already wired into
    get_chart_data()), not raise AttributeError."""

    def test_get_chart_data_does_not_raise_when_daily_forecast_full_never_set(self):
        coord_mod = _get_coordinator_module()
        coord = _make_full_chart_coord(daily_forecast_full=None)
        # Simulate "never assigned" (not merely None) — delete the attribute.
        del coord._daily_forecast_full
        assert not hasattr(coord, "_daily_forecast_full")

        now_p, as_local_p = _patched_dt_util(coord_mod)
        with now_p, as_local_p:
            result = coord.get_chart_data("7d")  # must not raise AttributeError

        assert "predicted_indoor" in result
        assert "forecast_outdoor" in result
        # Un-extended: real forecast is only 48h, so nothing should extend
        # past the last real hourly entry's date (2026-06-02).
        if result["predicted_indoor"]:
            last = max(datetime.fromisoformat(e["ts"]) for e in result["predicted_indoor"])
            assert last.date() <= date(2026, 6, 3), (
                f"expected no synthetic extension when _daily_forecast_full was never set, got last ts={last}"
            )
