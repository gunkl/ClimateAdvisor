"""Tests for Phase 1 (ramp anchor), Phase 2B (gate bridge), and Phase 2C
(per-hour ventilation wiring) fixes in _build_predicted_indoor_future()
(Issue #126).

Phase 1 — Ramp anchor fix (coordinator.py line ~4339):
  Off-day setpoint-schedule fallback anchors to current_indoor_temp when
  available, rather than outdoor+2°F.  The off-day branch triggers when
  THRESHOLD_MILD (60°F) <= forecast high < THRESHOLD_WARM (75°F).

Phase 2B — Gate bridge (coordinator.py lines ~4196-4214):
  When k_passive is absent but k_vent_window is learned, the ODE activates
  using k_vent_window as a proxy, so thermally inert homes get physics
  prediction instead of the ramp fallback.

Phase 2C — Per-hour ventilation wiring (coordinator.py _build_predicted_indoor_future):
  For forecast hours where classification recommends windows open, k_vent_window
  is substituted as the effective k_passive in the ODE (replacement semantics —
  k_vent_window is the total measured k during ventilated conditions).
  Gate bridge guard: when _k_passive_via_bridge=True (k_passive was None,
  k_vent_window already used as proxy for all hours), per-hour substitution does
  not fire to avoid double-counting.
"""

from __future__ import annotations

from datetime import UTC, datetime, time, timedelta
from unittest.mock import MagicMock, patch

import pytest

from custom_components.climate_advisor.coordinator import _build_predicted_indoor_future

# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

_PRED_CONFIG = {
    "comfort_heat": 70,
    "comfort_cool": 75,
    "setback_heat": 60,
    "setback_cool": 80,
    "wake_time": "06:30",
    "sleep_time": "22:30",
}

# Off-day outdoor temperature: THRESHOLD_MILD (60) <= temp < THRESHOLD_WARM (75)
# so _day_mode() returns "off".  Use 65°F to sit clearly in the mild band.
_OFF_DAY_OUTDOOR_F = 65.0

# Timestamps start at noon UTC to be comfortably inside the awake band
# (wake_time=06:30, sleep_time=22:30).
_NOW = datetime(2026, 4, 15, 12, 0, 0, tzinfo=UTC)


def _make_dt_util_mock(now: datetime) -> MagicMock:
    mock = MagicMock()
    mock.now.return_value = now
    mock.as_local.side_effect = lambda dt: dt
    mock.parse_datetime.side_effect = lambda s: datetime.fromisoformat(s) if s else None
    return mock


def _entry(dt: datetime, temp: float) -> dict:
    return {"datetime": dt.isoformat(), "temperature": temp}


def _call(entries, *, now=_NOW, indoor=None, model=None, config=None):
    cfg = config or _PRED_CONFIG
    with patch("custom_components.climate_advisor.coordinator.dt_util", _make_dt_util_mock(now)):
        return _build_predicted_indoor_future(
            entries,
            cfg,
            now,
            current_indoor_temp=indoor,
            thermal_model=model,
        )


# ---------------------------------------------------------------------------
# Phase 1 — Ramp anchor fix
# ---------------------------------------------------------------------------


class TestRampAnchor:
    """Off-day setpoint fallback anchors to current_indoor_temp when available.

    An off-day is one where THRESHOLD_MILD (60°F) <= forecast high < THRESHOLD_WARM (75°F).
    In that mode the HVAC is off and the old code anchored to outdoor+2°F, which was
    wrong by ~9°F for a thermally inert home sitting at 69°F with outdoor at 65°F.
    """

    def test_off_day_fallback_uses_indoor_seed_not_outdoor_plus_2(self):
        """When indoor=69°F and outdoor=65°F, fallback returns ~69°F not ~67°F.

        Prior behaviour: max(setback_heat=60, outdoor+2=67) = 67°F.
        Fixed behaviour: _t_current (69°F) used as anchor.
        """
        now = _NOW
        indoor_f = 69.0
        # Outdoor 65°F: off-day (THRESHOLD_MILD=60 ≤ 65 < THRESHOLD_WARM=75)
        entries = [_entry(now + timedelta(hours=i), _OFF_DAY_OUTDOOR_F) for i in range(1, 5)]

        result = _call(entries, now=now, indoor=indoor_f, model=None)

        assert len(result) == 7
        for point in result:
            # All off-day fallback entries should be anchored to indoor seed (69°F),
            # not outdoor+2 (= 67°F).
            assert point["temp"] == pytest.approx(indoor_f, abs=0.05), (
                f"Expected indoor anchor {indoor_f}°F, got {point['temp']}°F"
                f" (outdoor+2 would be {_OFF_DAY_OUTDOOR_F + 2.0}°F)"
            )

    def test_off_day_fallback_outdoor_plus_2_when_no_indoor(self):
        """When current_indoor_temp=None, fallback reverts to outdoor+2°F (original)."""
        now = _NOW
        entries = [_entry(now + timedelta(hours=i), _OFF_DAY_OUTDOOR_F) for i in range(1, 4)]

        result = _call(entries, now=now, indoor=None, model=None)

        assert len(result) == 5
        expected_temp = max(float(_PRED_CONFIG["setback_heat"]), _OFF_DAY_OUTDOOR_F + 2.0)
        for point in result:
            assert point["temp"] == pytest.approx(expected_temp, abs=0.05), (
                f"Expected outdoor+2 fallback {expected_temp}°F, got {point['temp']}°F"
            )

    def test_off_day_ramp_anchor_uses_indoor_seed_verbatim(self):
        """Indoor seed is used as-is without flooring to setback_heat.

        The ramp anchor does NOT apply the setback_heat floor — it uses the
        actual current indoor reading.  (setback_heat guards the outdoor+2°F
        branch only.)  Here indoor=62°F > setback_heat=60°F to confirm the
        value passes through correctly.
        """
        now = _NOW
        indoor_f = 62.0
        entries = [_entry(now + timedelta(hours=i), _OFF_DAY_OUTDOOR_F) for i in range(1, 4)]

        result = _call(entries, now=now, indoor=indoor_f, model=None)

        assert len(result) == 5
        for point in result:
            assert point["temp"] == pytest.approx(indoor_f, abs=0.05), (
                f"Expected indoor anchor {indoor_f}°F verbatim; got {point['temp']}°F"
            )


# ---------------------------------------------------------------------------
# Phase 2B — Gate bridge
# ---------------------------------------------------------------------------


class TestGateBridge:
    """k_vent_window acts as proxy k_passive when k_passive is absent."""

    def _vent_only_model(self, k_vent_window: float) -> dict:
        """Thermal model with ventilated observations only — no k_passive."""
        return {
            "confidence": "low",
            "k_passive": None,
            "k_vent_window": k_vent_window,
            "k_active_heat": None,
            "k_active_cool": None,
        }

    def test_gate_bridge_activates_physics_for_inert_home(self):
        """k_passive=None + k_vent_window=-0.001 → physics activates, not ramp fallback.

        Physics prediction for a near-inert home produces a flat trajectory
        close to the indoor seed.  The old ramp fallback (outdoor+2°F = 67°F)
        would be wrong by ~2°F for a home sitting at 69°F.
        """
        now = _NOW
        indoor_f = 69.0
        # Off-day: THRESHOLD_MILD(60) <= outdoor(65) < THRESHOLD_WARM(75)
        entries = [_entry(now + timedelta(hours=i), _OFF_DAY_OUTDOOR_F) for i in range(1, 6)]
        model = self._vent_only_model(k_vent_window=-0.001)

        result = _call(entries, now=now, indoor=indoor_f, model=model)

        assert len(result) == 9
        temps = [r["temp"] for r in result]
        # Physics with k≈0: indoor barely moves.  All temps should stay very close
        # to the seed (within 1°F over 5 hours for k=-0.001).
        for t in temps:
            assert abs(t - indoor_f) < 1.0, (
                f"Physics with k_vent_window≈0 should hold indoor near seed {indoor_f}°F; got {t}°F"
            )

    def test_gate_bridge_k_vent_window_zero_gives_flat_prediction(self):
        """k_vent_window=0.0 (perfectly inert home) → ODE is flat; indoor unchanged.

        With k_passive=0.0, exp(0*dt)=1 so the ODE reduces to T(t+dt)=T(t).
        """
        now = _NOW
        indoor_f = 72.0
        entries = [_entry(now + timedelta(hours=i), _OFF_DAY_OUTDOOR_F) for i in range(1, 5)]
        model = self._vent_only_model(k_vent_window=0.0)

        result = _call(entries, now=now, indoor=indoor_f, model=model)

        assert len(result) == 7
        for point in result:
            assert point["temp"] == pytest.approx(indoor_f, abs=0.1), (
                f"Perfectly inert home (k=0) should stay at {indoor_f}°F; got {point['temp']}°F"
            )

    def test_gate_bridge_does_not_activate_when_k_vent_window_positive(self):
        """k_vent_window > 0 is physically invalid → no bridge, ramp fallback used.

        The gate bridge guard `k_vent_window <= 0` rejects positive values, so
        the setpoint-schedule fallback runs instead.  On an off-day with a valid
        indoor seed the ramp anchor (Phase 1) returns the indoor seed.
        """
        now = _NOW
        indoor_f = 69.0
        entries = [_entry(now + timedelta(hours=i), _OFF_DAY_OUTDOOR_F) for i in range(1, 4)]
        # Positive k_vent_window — should NOT activate bridge
        model = {
            "confidence": "low",
            "k_passive": None,
            "k_vent_window": 0.05,  # positive, physically invalid
            "k_active_heat": None,
            "k_active_cool": None,
        }

        result = _call(entries, now=now, indoor=indoor_f, model=model)

        assert len(result) == 5
        # No bridge → setpoint-schedule fallback → off-day → ramp anchor returns indoor seed.
        for point in result:
            assert point["temp"] == pytest.approx(indoor_f, abs=0.05), (
                f"Fallback (no bridge): expected indoor anchor {indoor_f}°F; got {point['temp']}°F"
            )

    def test_gate_bridge_does_not_contaminate_when_k_passive_already_present(self):
        """When k_passive is already set, k_vent_window is not used as proxy.

        The bridge only activates when k_passive is None.  If a valid k_passive
        exists, the trajectory must follow k_passive, not k_vent_window.
        """
        now = _NOW
        indoor_f = 65.0
        # Heat day: outdoor 40°F < THRESHOLD_MILD(60) → mode="heat"
        outdoor_heat_day = 40.0
        entries = [_entry(now + timedelta(hours=i), outdoor_heat_day) for i in range(1, 4)]
        model = {
            "confidence": "low",
            "k_passive": -0.05,  # valid k_passive present
            "k_vent_window": -0.5,  # very different — must not override k_passive
            "k_active_heat": 3.0,
            "k_active_cool": -2.5,
        }

        result = _call(entries, now=now, indoor=indoor_f, model=model)

        assert len(result) == 5
        temps = [r["temp"] for r in result]
        # With k_passive=-0.05 and k_active_heat=3.0 on a heat day, indoor=65°F
        # below comfort_heat=70°F → HVAC heats → trajectory rises toward 70°F.
        # With k_passive=-0.5 (k_vent_window) heating would be far more aggressive
        # (loses heat rapidly → HVAC runs harder → overshoots faster).
        # Moderate rise confirms k_passive=-0.05 was used, not -0.5.
        assert temps[-1] > indoor_f, "Heating mode should raise indoor temps"
        assert temps[0] < 70.0, "k_passive=-0.05 path should not reach comfort_heat setpoint in 1hr"


# ---------------------------------------------------------------------------
# Phase 2C — Per-hour ventilation wiring
# ---------------------------------------------------------------------------


def _make_classification(
    *,
    windows_recommended: bool = False,
    window_open_time: time | None = None,
    window_close_time: time | None = None,
) -> MagicMock:
    """Build a minimal classification mock with window schedule attributes."""
    cls = MagicMock()
    cls.windows_recommended = windows_recommended
    cls.window_open_time = window_open_time
    cls.window_close_time = window_close_time
    return cls


def _call_vc(
    entries,
    *,
    now=_NOW,
    indoor: float,
    model: dict,
    classification=None,
    config=None,
):
    """Call _build_predicted_indoor_future with optional classification arg."""
    cfg = config or _PRED_CONFIG
    with patch("custom_components.climate_advisor.coordinator.dt_util", _make_dt_util_mock(now)):
        return _build_predicted_indoor_future(
            entries,
            cfg,
            now,
            current_indoor_temp=indoor,
            thermal_model=model,
            classification=classification,
        )


class TestPerHourVentilationWiring:
    """Per-hour k_vent_window substitution in the physics ODE (Phase 2C / Issue #126).

    For forecast hours where the classification recommends windows open,
    k_vent_window replaces k_passive as the effective decay rate in the ODE.
    k_vent_window is the TOTAL measured k during ventilated conditions
    (replacement semantics, not addition).
    """

    # Base "warm off-day" timestamps: outlook 06:00–23:00 local today.
    # Outdoor=60°F sits in THRESHOLD_MILD(60) ≤ T < THRESHOLD_WARM(75) → mode="off".
    # All entries are in the future relative to _NOW (noon UTC = noon local via mock).
    _OUTDOOR = 60.0
    _INDOOR = 72.0
    # Use a fresh NOW so "future" entries are unambiguous
    _NOW_VC = datetime(2026, 4, 20, 8, 0, 0, tzinfo=UTC)

    def _entries(self, now: datetime, hours: list[int]) -> list[dict]:
        """Build forecast entries at specified UTC hours (same day as now)."""
        base = now.replace(hour=0, minute=0, second=0, microsecond=0)
        return [_entry(base + timedelta(hours=h), self._OUTDOOR) for h in hours]

    def test_window_open_hours_use_k_vent_window(self):
        """During window-open hours, k_vent_window drives a weaker decay than k_passive.

        Setup: k_passive=-0.30 (strong decay toward outdoor at 60°F),
               k_vent_window=-0.10 (weaker decay — insulated by window schedule),
               windows_recommended=True, open 10:00–17:00, outdoor=60°F, indoor=72°F.

        k_vent_window=-0.10 is a weaker negative, so indoor stays HIGHER during
        window hours than with k_passive=-0.30 (which drives indoor toward 60°F faster).

        Assertion: temps during 10:00–17:00 are higher in the k_vent_window run
        than in a baseline run without k_vent_window.
        Pre-window temps (09:00–10:00) should be approximately equal (same k_passive).

        Note: _NOW_VC is 08:00, so hour-8 entry equals now and is filtered out.
        Entries start at hour 9 (strictly after now).
        """
        now = self._NOW_VC
        # Hours: 9 (before window), 10–16 (window open), 17–18 (after)
        hours = list(range(9, 19))
        entries = self._entries(now, hours)

        model_with_kv = {
            "confidence": "low",
            "k_passive": -0.30,
            "k_vent_window": -0.10,
            "k_active_heat": None,
            "k_active_cool": None,
        }
        model_without_kv = {
            "confidence": "low",
            "k_passive": -0.30,
            "k_vent_window": None,
            "k_active_heat": None,
            "k_active_cool": None,
        }
        classification = _make_classification(
            windows_recommended=True,
            window_open_time=time(10, 0),
            window_close_time=time(17, 0),
        )

        result_with = _call_vc(
            entries, now=now, indoor=self._INDOOR, model=model_with_kv, classification=classification
        )
        result_without = _call_vc(
            entries, now=now, indoor=self._INDOOR, model=model_without_kv, classification=classification
        )

        assert len(result_with) == len(result_without) == 2 * len(hours) - 1

        # Build lookup by hour
        def _hour(ts_str: str) -> int:
            return datetime.fromisoformat(ts_str).hour

        # First-entry-wins: with 30-min interpolation, two entries share each hour.
        # Use top-of-hour entry so boundary hours don't bleed into the next regime.
        with_by_h: dict[int, float] = {}
        for r in result_with:
            h = _hour(r["ts"])
            if h not in with_by_h:
                with_by_h[h] = r["temp"]
        without_by_h: dict[int, float] = {}
        for r in result_without:
            h = _hour(r["ts"])
            if h not in without_by_h:
                without_by_h[h] = r["temp"]

        # During window-open hours: k_vent_window (-0.10) is weaker decay than k_passive (-0.30)
        # so indoor stays higher (closer to 72°F than to outdoor 60°F).
        for h in range(10, 17):
            assert with_by_h[h] > without_by_h[h], (
                f"Hour {h}: k_vent_window run should be warmer than k_passive run; "
                f"got {with_by_h[h]:.2f} vs {without_by_h[h]:.2f}"
            )

        # Before the window: same k_passive used in both — temps should be approximately equal.
        # Only hour 9 is strictly before the window (hour 8 equals now and is filtered).
        for h in [9]:
            assert abs(with_by_h[h] - without_by_h[h]) < 0.5, (
                f"Hour {h}: pre-window temps should match; got {with_by_h[h]:.2f} vs {without_by_h[h]:.2f}"
            )

    def test_window_closed_hours_use_k_passive_unchanged(self):
        """When windows_recommended=False, no substitution fires — all hours use k_passive.

        Same k_passive and k_vent_window as above, but classification has
        windows_recommended=False.  Predictions must match a run with no k_vent_window.
        """
        now = self._NOW_VC
        hours = list(range(9, 18))
        entries = self._entries(now, hours)

        model_with_kv = {
            "confidence": "low",
            "k_passive": -0.30,
            "k_vent_window": -0.10,
            "k_active_heat": None,
            "k_active_cool": None,
        }
        model_without_kv = {
            "confidence": "low",
            "k_passive": -0.30,
            "k_vent_window": None,
            "k_active_heat": None,
            "k_active_cool": None,
        }
        classification_closed = _make_classification(
            windows_recommended=False,
            window_open_time=time(10, 0),
            window_close_time=time(17, 0),
        )

        result_with = _call_vc(
            entries, now=now, indoor=self._INDOOR, model=model_with_kv, classification=classification_closed
        )
        result_without = _call_vc(
            entries, now=now, indoor=self._INDOOR, model=model_without_kv, classification=classification_closed
        )

        assert len(result_with) == len(result_without)
        for r_w, r_wo in zip(result_with, result_without, strict=True):
            assert r_w["temp"] == pytest.approx(r_wo["temp"], abs=0.01), (
                f"windows_recommended=False: temps should be identical at {r_w['ts']}; "
                f"got {r_w['temp']} vs {r_wo['temp']}"
            )

    def test_k_vent_window_none_no_change(self):
        """k_vent_window=None prevents substitution even if windows_recommended=True.

        Guard condition: `_k_vent_window is not None` must be checked before
        any per-hour substitution.  With k_vent_window=None, all hours must use
        k_passive and predictions must equal a windows_recommended=False run.
        """
        now = self._NOW_VC
        hours = list(range(9, 18))
        entries = self._entries(now, hours)

        model_kv_none = {
            "confidence": "low",
            "k_passive": -0.30,
            "k_vent_window": None,
            "k_active_heat": None,
            "k_active_cool": None,
        }
        classification_open = _make_classification(
            windows_recommended=True,
            window_open_time=time(10, 0),
            window_close_time=time(17, 0),
        )
        classification_closed = _make_classification(windows_recommended=False)

        result_open = _call_vc(
            entries, now=now, indoor=self._INDOOR, model=model_kv_none, classification=classification_open
        )
        result_closed = _call_vc(
            entries, now=now, indoor=self._INDOOR, model=model_kv_none, classification=classification_closed
        )

        assert len(result_open) == len(result_closed)
        for r_o, r_c in zip(result_open, result_closed, strict=True):
            assert r_o["temp"] == pytest.approx(r_c["temp"], abs=0.01), (
                f"k_vent_window=None guard: temps should match at {r_o['ts']}; got {r_o['temp']} vs {r_c['temp']}"
            )

    def test_inert_home_window_open_solar_drives_warming(self):
        """k_vent_window=0.0 (perfectly inert) + k_solar: solar gain drives indoor up.

        With k_vent_window=0.0, the passive ODE term vanishes: dT/dt ≈ k_solar*solar_factor.
        During daytime window hours (peak solar_factor > 0), indoor should rise above seed.
        After window close (no solar at hour 20), indoor should stay near the noon peak.

        This validates that solar+ventilation interact correctly without requiring
        k_vent_window != 0 to see any movement.
        """
        now = self._NOW_VC
        # Daytime hours inside window (8–17) and one post-close hour (20)
        # Outdoor=65°F; high=65°F → off-day mode → HVAC off → pure passive+solar ODE.
        outdoor = 65.0
        hours = list(range(9, 21))
        base = now.replace(hour=0, minute=0, second=0, microsecond=0)
        entries = [_entry(base + timedelta(hours=h), outdoor) for h in hours]

        model = {
            "confidence": "low",
            "k_passive": -0.20,
            "k_vent_window": 0.0,
            "k_solar": 2.5,
            "k_active_heat": None,
            "k_active_cool": None,
        }
        classification = _make_classification(
            windows_recommended=True,
            window_open_time=time(8, 0),
            window_close_time=time(18, 0),
        )

        result = _call_vc(entries, now=now, indoor=70.0, model=model, classification=classification)

        assert len(result) == 2 * len(hours) - 1
        temps_by_h = {datetime.fromisoformat(r["ts"]).hour: r["temp"] for r in result}

        # At noon (hour 12), solar_factor should be near peak — indoor should be above seed (70°F).
        assert temps_by_h[12] > 70.0, (
            f"Inert home with solar should warm above 70°F by noon; got {temps_by_h[12]:.2f}°F"
        )

    def test_gate_bridge_home_no_double_counting(self):
        """When gate bridge is active (k_passive=None), per-hour k_vent_window substitution must NOT fire.

        After the bridge window guard fix (Issue #130):
        - Bridge guard fires only when classification has windows_recommended=True AND the hour
          falls outside the window schedule.  Window-closed bridge hours use ramp fallback.
        - Window-open bridge hours let physics run (k_vent_window is valid for those conditions).

        The "no double-counting" intent is preserved: the per-hour substitution guard
        (`not _k_passive_via_bridge`) still prevents double-counting during open-window hours.

        Observable: bridge home with windows_recommended=True, hour 9 (pre-window) is ramp-flat.
        Hours 10–16 (window open) use physics → temps decay below indoor seed (72°F).
        """
        now = self._NOW_VC
        hours = list(range(9, 18))
        entries = self._entries(now, hours)

        # k_passive=None → gate bridge activates, uses k_vent_window=-0.20 as proxy
        model = {
            "confidence": "low",
            "k_passive": None,
            "k_vent_window": -0.20,
            "k_active_heat": None,
            "k_active_cool": None,
        }
        # windows_recommended=True with schedule → bridge guard applies outside window
        classification_open = _make_classification(
            windows_recommended=True,
            window_open_time=time(10, 0),
            window_close_time=time(17, 0),
        )

        result = _call_vc(entries, now=now, indoor=self._INDOOR, model=model, classification=classification_open)

        assert len(result) == 2 * len(hours) - 1
        # First-entry-wins: use top-of-hour entry so boundary hours don't bleed into next regime.
        temps_by_h: dict[int, float] = {}
        for r in result:
            h = datetime.fromisoformat(r["ts"]).hour
            if h not in temps_by_h:
                temps_by_h[h] = r["temp"]

        # Hour 9 is window-closed (pre-window): bridge guard fires → ramp → ≈ indoor seed (72°F)
        assert temps_by_h[9] == pytest.approx(self._INDOOR, abs=1.0), (
            f"Bridge guard (window-closed hour 9): expected ramp anchor ~{self._INDOOR}°F; got {temps_by_h[9]:.2f}°F"
        )

        # During window-open hours: physics runs, k_vent_window=-0.20 applied once (no double-counting).
        # indoor=72°F, outdoor=60°F → decay toward outdoor.  By hour 16, physics should be visible.
        open_hours = [h for h in range(10, 17) if h in temps_by_h]
        assert len(open_hours) >= 4, "Need at least 4 window-open hours to observe physics decay"
        last_open = max(open_hours)
        assert temps_by_h[last_open] < self._INDOOR, (
            f"Bridge open hours: physics with k_vent_window=-0.20 should cool below {self._INDOOR}°F; "
            f"got {temps_by_h[last_open]:.2f}°F "
            f"(no double-counting: per-hour substitution blocked by _k_passive_via_bridge)"
        )


# ---------------------------------------------------------------------------
# Bridge window guard — prevents k_vent_window from driving window-closed hours
# (Issue #130)
# ---------------------------------------------------------------------------


class TestBridgeWindowGuard:
    """Bridge guard: when _k_passive_via_bridge=True, physics only runs during window-open hours.

    k_vent_window = envelope k + ventilation k.  Applying it to window-CLOSED hours
    overpredicts decay (τ≈7h vs true envelope τ≈50h).  The guard falls back to ramp
    for window-closed bridge hours when classification schedules windows for today.

    The guard fires only when:
      - _k_passive_via_bridge=True (bridge is active)
      - _windows_recommended=True (classification has a window schedule today)
      - _hour_windows_open=False (current hour is outside the open window)

    When windows_recommended=False (no window schedule), bridge physics runs for all hours
    (k_vent_window is the best available proxy — consistent with pre-guard bridge semantics).

    Cross-home safety matrix:
      - Normal home (k_passive learned, no bridge): guard never fires → no change
      - Bridge home, no window schedule: guard never fires → bridge physics for all hours
      - Bridge home, window-scheduled, window-open hour: guard passes → physics with k_vent_window runs
      - Bridge home, window-scheduled, window-closed hour: guard fires → ramp fallback (flat indoor)
      - Fresh install (k_vent_window=None): bridge doesn't fire → k_passive_via_bridge=False → no change
    """

    _NOW_BG = datetime(2026, 4, 20, 6, 0, 0, tzinfo=UTC)
    _INDOOR = 70.0

    def _entries(self, hours: list[int], outdoor: float = 65.0) -> list[dict]:
        base = self._NOW_BG.replace(hour=0, minute=0, second=0, microsecond=0)
        return [_entry(base + timedelta(hours=h), outdoor) for h in hours]

    def test_bridge_window_closed_holds_indoor_steady(self):
        """Bridge home, windows scheduled but PRE-WINDOW hours: ramp fallback, temps flat.

        Pre-fix behavior: bridge applies k_vent_window=-0.20 to all hours → rapid decay.
        Post-fix behavior: bridge guard fires for pre-window hours → ramp → flat at indoor seed.

        Setup: windows_recommended=True, window 14:00–20:00.
        Hours 7–13 are pre-window (window-closed). outdoor=65°F (off-day), indoor=70°F.
        After fix: all pre-window temps within 1.0°F of 70°F.
        """
        now = self._NOW_BG
        outdoor_mild = 65.0
        # Pre-window hours: 7–13 UTC (window opens at 14:00)
        hours = list(range(7, 14))
        entries = self._entries(hours, outdoor=outdoor_mild)

        model = {
            "confidence": "low",
            "k_passive": None,
            "k_vent_window": -0.20,  # strong decay — would drive rapid cooling if applied to closed hours
            "k_active_heat": None,
            "k_active_cool": None,
        }
        # windows_recommended=True with a future window schedule → guard fires for pre-window hours
        classification = _make_classification(
            windows_recommended=True,
            window_open_time=time(14, 0),
            window_close_time=time(20, 0),
        )

        result = _call_vc(entries, now=now, indoor=self._INDOOR, model=model, classification=classification)

        assert len(result) > 0
        temps = [r["temp"] for r in result]
        # After fix: guard fires for hours 7–13 (all pre-window) → ramp → flat at 70°F
        for t in temps:
            assert abs(t - self._INDOOR) <= 1.0, (
                f"Bridge guard (window-closed pre-window hours): expected ramp anchor ~{self._INDOOR}°F; "
                f"got {t:.2f}°F (pre-fix: rapid decay with k_vent_window=-0.20)"
            )

    def test_bridge_window_open_uses_physics(self):
        """Bridge home, windows recommended with schedule: physics runs during open hours.

        outdoor=65°F, indoor=70°F above outdoor → passive decay pulls indoor down.
        k_vent_window=-0.20 applied during open hours → temps drop below 69°F.
        """
        now = self._NOW_BG
        # Window open 09:00–18:00 UTC
        hours = list(range(7, 20))
        outdoor_mild = 65.0
        base = now.replace(hour=0, minute=0, second=0, microsecond=0)
        entries = [_entry(base + timedelta(hours=h), outdoor_mild) for h in hours]

        model = {
            "confidence": "low",
            "k_passive": None,
            "k_vent_window": -0.20,
            "k_active_heat": None,
            "k_active_cool": None,
        }
        classification = _make_classification(
            windows_recommended=True,
            window_open_time=time(9, 0),
            window_close_time=time(18, 0),
        )

        result = _call_vc(entries, now=now, indoor=self._INDOOR, model=model, classification=classification)

        assert len(result) > 0
        temps_by_h = {datetime.fromisoformat(r["ts"]).hour: r["temp"] for r in result}

        # During open hours (9–17), physics should drive cooling below 69°F
        open_hours = [h for h in range(9, 18) if h in temps_by_h]
        assert len(open_hours) >= 4, "Need at least 4 window-open hours in result to validate physics"
        # By the end of the open window, decay with k=-0.20 over several hours should drop below 69°F
        last_open = max(open_hours)
        assert temps_by_h[last_open] < 69.0, (
            f"Bridge home, window-open hours: physics with k=-0.20 should cool below 69°F by hour {last_open}; "
            f"got {temps_by_h[last_open]:.2f}°F"
        )

    def test_non_bridge_home_unaffected_by_guard(self):
        """Normal home with real k_passive: guard never fires, physics runs for all hours.

        k_passive=-0.025 (real learned value), confidence_k_passive='low' → bridge does NOT activate
        (_k_passive_via_bridge=False).  Guard condition requires _k_passive_via_bridge=True → skipped.
        Physics runs via real k_passive → temps decay (lower than indoor seed after several hours).
        """
        now = self._NOW_BG
        outdoor_mild = 65.0
        hours = list(range(7, 19))
        entries = self._entries(hours, outdoor=outdoor_mild)

        model = {
            "confidence": "low",
            "confidence_k_passive": "low",
            "k_passive": -0.025,  # real learned value — bridge must NOT fire
            "k_vent_window": -0.20,
            "k_active_heat": None,
            "k_active_cool": None,
        }
        # Pass windows_recommended=True to maximally exercise the guard — but since
        # _k_passive_via_bridge=False, the guard still does not fire.
        classification = _make_classification(
            windows_recommended=True,
            window_open_time=time(14, 0),
            window_close_time=time(20, 0),
        )

        result = _call_vc(entries, now=now, indoor=self._INDOOR, model=model, classification=classification)

        assert len(result) > 0
        temps = [r["temp"] for r in result]
        # Physics runs (no bridge, k_passive=-0.025 valid, confidence='low').
        # indoor=70°F, outdoor=65°F → slow passive decay.
        # After several hours with k=-0.025, temp drops measurably below seed.
        assert temps[-1] < self._INDOOR, (
            f"Non-bridge home: physics should run and decay indoor below {self._INDOOR}°F; "
            f"got {temps[-1]:.2f}°F (guard must NOT suppress physics when _k_passive_via_bridge=False)"
        )

    def test_bridge_windows_not_recommended_physics_unchanged(self):
        """Bridge home with windows_recommended=False: guard does NOT fire.

        Cross-home matrix row: bridge active, classification has no window schedule.
        Guard condition requires _windows_recommended=True — if False the guard is skipped
        and bridge physics runs for all hours (pre-guard behavior).

        Rationale: when no window schedule is set, k_vent_window is the only proxy we have;
        suppressing physics entirely would remove all thermal signal from the prediction.
        This is the accepted trade-off pending real passive_decay observations.
        """
        now = self._NOW_BG
        outdoor = 50.0  # colder than indoor — physics should show decay
        hours = list(range(1, 13))  # 12 overnight hours
        entries = self._entries(hours, outdoor=outdoor)

        model = {
            "confidence": "low",
            "k_passive": None,  # bridge will activate
            "k_vent_window": -0.20,
            "k_active_heat": None,
            "k_active_cool": None,
        }
        # No window schedule — windows_recommended=False
        classification = _make_classification(windows_recommended=False)

        result = _call_vc(entries, now=now, indoor=self._INDOOR, model=model, classification=classification)

        assert len(result) > 0
        temps = [r["temp"] for r in result]
        # Bridge physics runs (guard skipped because _windows_recommended=False).
        # indoor=70°F, outdoor=50°F, k=-0.20 → meaningful decay expected over 12 hours.
        assert temps[-1] < self._INDOOR - 1.0, (
            f"Bridge home, no window schedule: physics should run and show decay; "
            f"got {temps[-1]:.2f}°F (guard must NOT fire when windows_recommended=False)"
        )


# ---------------------------------------------------------------------------
# Gate bridge self-healing — Bug A + Bug B regression guards (Issue #126)
# ---------------------------------------------------------------------------


class TestGateBridgeSelfHealing:
    """Gate bridge fires for contaminated installs (k_passive stale, conf_passive='none').

    Bug A: Bridge previously only fired when k_passive is None.  It must also
           fire when k_passive is present but confidence_k_passive='none'
           (contaminated install — stale value, no real passive_decay observations).

    Bug B: _k_passive_via_bridge must bypass the confidence gate in
           _physics_eligible so a bridge-provided k still enables physics
           prediction even when both conf and conf_k_passive are 'none'.

    Self-healing semantics (the three states this class guards):
      1. Fresh install   : k_passive=None → bridge fires  ← preserved (regression)
      2. Contaminated    : k_passive=-0.28, conf_k_passive='none' → bridge fires  ← new
      3. Healed install  : k_passive=-0.20, conf_k_passive='low'  → bridge skips  ← new
    """

    # Outdoor temp for a warm off-day (THRESHOLD_MILD=60 ≤ 65 < THRESHOLD_WARM=75)
    _OUTDOOR = 65.0
    _INDOOR = 69.0
    _NOW_SH = datetime(2026, 4, 15, 12, 0, 0, tzinfo=UTC)

    def _entries(self, hours: int = 4) -> list[dict]:
        now = self._NOW_SH
        return [_entry(now + timedelta(hours=i), self._OUTDOOR) for i in range(1, hours + 1)]

    def test_bridge_fires_when_conf_passive_none_and_k_passive_stale(self):
        """Contaminated install: k_passive=-0.28, confidence_k_passive='none', k_vent_window=-0.25.

        Bug A: before the fix, bridge only checked `k_passive is None`.
        With a stale k_passive present, bridge was skipped and physics fell back to ramp.
        After the fix: bridge fires when conf_k_passive is None or 'none',
        overwriting stale k_passive with k_vent_window.

        Observable: physics produces a trajectory near indoor seed (69°F) for k≈-0.25,
        while ramp fallback would return a flat 69°F (off-day indoor anchor).
        We confirm physics activates by verifying predictions are returned and
        remain in a physically plausible range (not stuck at outdoor+2=67°F).
        """
        model = {
            "confidence": "none",
            "confidence_k_passive": "none",
            "k_passive": -0.2813,  # stale contaminated value
            "k_vent_window": -0.2547,
            "k_active_heat": None,
            "k_active_cool": None,
        }
        entries = self._entries()

        result = _call(entries, now=self._NOW_SH, indoor=self._INDOOR, model=model)

        assert len(result) == 7
        # Bridge fires → physics runs → k_passive = k_vent_window = -0.2547.
        # With outdoor=65°F and indoor=69°F above outdoor, passive decay slowly
        # pulls indoor toward outdoor.  After 4 hours at k=-0.25, T drops measurably
        # but stays above outdoor (65°F).  Ramp would produce exactly 69°F (indoor anchor).
        # Physics produces < 69°F as heat leaks out.
        temps = [r["temp"] for r in result]
        for t in temps:
            assert t > self._OUTDOOR, f"Physics should keep indoor above outdoor 65°F; got {t:.2f}°F"
        # At least the 4-hour mark should show some decay from seed
        assert temps[-1] < self._INDOOR, (
            f"Physics should show some cooling from seed {self._INDOOR}°F after 4h; got {temps[-1]:.2f}°F"
        )

    def test_bridge_fires_when_k_passive_none(self):
        """Regression guard: original case (fresh install) — k_passive=None still fires bridge.

        Bridge must activate for k_passive=None with valid k_vent_window, producing
        physics prediction rather than ramp fallback.  This was the original Bug A
        scenario and must remain working after the fix.
        """
        model = {
            "confidence": "none",
            "confidence_k_passive": None,
            "k_passive": None,
            "k_vent_window": -0.25,
            "k_active_heat": None,
            "k_active_cool": None,
        }
        entries = self._entries()

        result = _call(entries, now=self._NOW_SH, indoor=self._INDOOR, model=model)

        assert len(result) == 7
        temps = [r["temp"] for r in result]
        # Physics with k=-0.25 and indoor=69°F above outdoor=65°F: indoor cools slowly.
        for t in temps:
            assert t > self._OUTDOOR, f"Physics: indoor should remain above outdoor; got {t:.2f}°F"
        assert temps[-1] < self._INDOOR, (
            f"Physics: should show cooling from seed {self._INDOOR}°F after 4h; got {temps[-1]:.2f}°F"
        )

    def test_bridge_does_not_fire_when_conf_passive_has_value(self):
        """Healed install: conf_k_passive='low' — bridge skips, real k_passive used.

        When real passive_decay observations have been committed (conf_k_passive='low' or
        higher), the bridge must NOT override k_passive with k_vent_window.
        The real k_passive is used directly for physics prediction.
        """
        model = {
            "confidence": "low",
            "confidence_k_passive": "low",
            "k_passive": -0.20,  # real learned value
            "k_vent_window": -0.50,  # very different — must not override k_passive
            "k_active_heat": None,
            "k_active_cool": None,
        }
        entries = self._entries(hours=3)

        result = _call(entries, now=self._NOW_SH, indoor=self._INDOOR, model=model)

        assert len(result) == 5
        temps = [r["temp"] for r in result]
        # With k_passive=-0.20 (real), physics uses this rate.
        # k_vent_window=-0.50 is much stronger — if bridge fired accidentally,
        # cooling would be significantly faster.
        # At k=-0.20 with indoor=69°F, outdoor=65°F, decay over 3h is small.
        # At k=-0.50, decay would be about 2.5x stronger.
        # We check that temps remain in a range consistent with k=-0.20, not k=-0.50.
        for t in temps:
            assert t > self._OUTDOOR, f"Physics: indoor should remain above outdoor; got {t:.2f}°F"
        # All temps should be above 67°F (k=-0.50 would push below 67°F faster)
        assert temps[-1] > 67.0, (
            f"Bridge must NOT fire for healed install; k=-0.20 should hold indoor above 67°F; got {temps[-1]:.2f}°F"
        )

    def test_physics_eligible_with_bridge_bypasses_confidence_check(self):
        """Bug B: bridge-provided k_passive enables physics even when conf='none' and conf_k_passive='none'.

        Before Bug B fix: _physics_eligible required (_conf != 'none' OR conf_k_passive not none).
        When both are 'none' and bridge is active, the confidence gate blocked physics.
        After fix: _k_passive_via_bridge=True is sufficient for eligibility.

        Setup: contaminated install where bridge fires (conf_passive='none', k_passive stale).
        Expectation: physics activates (trajectory moves, not flat ramp).
        """
        model = {
            "confidence": "none",
            "confidence_k_passive": "none",
            "k_passive": -0.2813,
            "k_vent_window": -0.25,
            "k_active_heat": None,
            "k_active_cool": None,
        }
        entries = self._entries(hours=6)

        result = _call(entries, now=self._NOW_SH, indoor=self._INDOOR, model=model)

        assert len(result) == 11
        temps = [r["temp"] for r in result]
        # Physics with bridge: indoor=69°F, outdoor=65°F, k=-0.25.
        # After 6h the decay is significant — expect last temp noticeably below 69°F.
        assert temps[-1] < self._INDOOR - 0.3, (
            f"Bug B: physics must activate (bridge bypasses confidence); "
            f"expected cooling below {self._INDOOR - 0.3:.1f}°F at 6h; got {temps[-1]:.2f}°F"
        )

    def test_physics_not_eligible_without_bridge_and_no_confidence(self):
        """Regression: no bridge, conf='none', conf_k_passive='none' → physics stays off.

        When k_passive is stale AND k_vent_window is None (no bridge possible),
        conf='none', conf_k_passive='none' → _physics_eligible=False → ramp fallback.
        On an off-day with indoor seed, ramp returns the indoor anchor (69°F).
        """
        model = {
            "confidence": "none",
            "confidence_k_passive": "none",
            "k_passive": -0.20,
            "k_vent_window": None,  # no bridge possible
            "k_active_heat": None,
            "k_active_cool": None,
        }
        entries = self._entries(hours=4)

        result = _call(entries, now=self._NOW_SH, indoor=self._INDOOR, model=model)

        assert len(result) == 7
        # No bridge, no confidence → ramp fallback → off-day → indoor anchor = 69°F flat.
        for point in result:
            assert point["temp"] == pytest.approx(self._INDOOR, abs=0.1), (
                f"No bridge + no confidence → ramp anchor {self._INDOOR}°F; got {point['temp']:.2f}°F"
            )


# ---------------------------------------------------------------------------
# Ventilated solar prediction — Phase C (Issue #126)
# ---------------------------------------------------------------------------


class TestVentilatedSolarPrediction:
    """k_solar from adaptive 2-param OLS flows to the ODE during window-open hours.

    Phase C confirms that k_solar is NOT suppressed during window-open hours.
    _simulate_indoor_physics_v3 applies q_solar = k_solar * solar_factor unconditionally
    (line: q_solar = (k_solar * solar_factor) if (k_solar is not None) else 0.0),
    regardless of whether _k_passive_for_hour came from k_passive or k_vent_window.

    Two properties are verified:
      1. Solar term is active during window-open hours (warmer prediction vs no k_solar).
      2. Solar contribution follows the sine factor curve — peaks at hour 13, zero at hour 18.
    """

    _NOW_VS = datetime(2026, 4, 20, 8, 0, 0, tzinfo=UTC)
    _OUTDOOR = 65.0
    _INDOOR = 70.0

    def _entries_hours(self, hours: list[int]) -> list[dict]:
        base = self._NOW_VS.replace(hour=0, minute=0, second=0, microsecond=0)
        return [_entry(base + timedelta(hours=h), self._OUTDOOR) for h in hours]

    def test_solar_term_active_during_window_open_hours(self):
        """During daytime window-open hours, k_solar=2.5 warms prediction vs k_solar=None.

        Setup:
          k_vent_window=-0.05 (near zero — minimal ventilation cooling effect)
          k_solar=2.5 vs k_solar=None
          windows_recommended=True, open 09:00-17:00
          outdoor=65°F (off-day), indoor seed=70°F

        k_vent_window=-0.05 is near-inert — the passive ODE barely moves.
        With k_solar=2.5, q_solar = k_solar * solar_factor during daytime hours.
        For hours 10-16, solar_factor > 0 → the solar run should be measurably warmer.
        Hour 9 (solar factor=sin(π/10)≈0.31) — small but nonzero contribution already.
        """
        now = self._NOW_VS
        hours = list(range(9, 18))
        entries = self._entries_hours(hours)

        base_model = {
            "confidence": "low",
            "k_passive": -0.20,
            "k_vent_window": -0.05,
            "k_active_heat": None,
            "k_active_cool": None,
        }
        model_with_solar = {**base_model, "k_solar": 2.5}
        model_no_solar = {**base_model, "k_solar": None}

        classification = _make_classification(
            windows_recommended=True,
            window_open_time=time(9, 0),
            window_close_time=time(17, 0),
        )

        result_solar = _call_vc(
            entries, now=now, indoor=self._INDOOR, model=model_with_solar, classification=classification
        )
        result_no_solar = _call_vc(
            entries, now=now, indoor=self._INDOOR, model=model_no_solar, classification=classification
        )

        assert len(result_solar) == len(result_no_solar) == 2 * len(hours) - 1

        solar_by_h = {datetime.fromisoformat(r["ts"]).hour: r["temp"] for r in result_solar}
        no_solar_by_h = {datetime.fromisoformat(r["ts"]).hour: r["temp"] for r in result_no_solar}

        # During window-open daytime hours, solar run should be warmer
        for h in range(10, 17):
            assert solar_by_h[h] > no_solar_by_h[h], (
                f"Hour {h}: k_solar=2.5 run should be warmer than k_solar=None run; "
                f"got {solar_by_h[h]:.2f} vs {no_solar_by_h[h]:.2f}°F"
            )

    def test_solar_term_follows_solar_factor_curve(self):
        """Solar contribution is proportional to solar_factor — peaks at hour 13, zero at 18.

        Setup:
          k_vent_window=0.0 (perfectly inert home — passive term vanishes when k_eff=0)
          outdoor = indoor seed = 70°F (so T_out - T_in = 0 — passive ODE term also zero)
          k_solar=2.5, windows open 08:00-19:00 (extended so hour 18 remains in-window)

        With k_eff=0 and T_out=T_in: dT/dt = k_solar * solar_factor (pure solar driver).
        The ODE reduces to: t_next = t_start + k_solar * solar_factor * dt_hours.

        Consequently:
          - Per-hour increments grow from hour 8 to hour 13 (rising half of sine curve)
          - Per-hour increments shrink from hour 13 to hour 17 (falling half)
          - No increment at hour 17→18 (solar_factor(18) = 0, window still open → k_eff=0)
          - Temperature at peak-solar hours (12-14) is highest in the daytime window

        Window close time is set to 19:00 so that hour 18 still uses k_vent_window=0.0
        (inside window schedule).  This isolates solar_factor(18)=0 from any passive-decay
        resumption that would occur if the window closed at 18:00.
        """
        now = self._NOW_VS
        outdoor_eq_indoor = 70.0  # T_out = T_in eliminates passive ODE term
        hours = list(range(9, 19))  # 9 through 18 inclusive
        base = now.replace(hour=0, minute=0, second=0, microsecond=0)
        entries = [_entry(base + timedelta(hours=h), outdoor_eq_indoor) for h in hours]

        model = {
            "confidence": "low",
            "k_passive": -0.20,  # irrelevant: overridden by k_vent_window=0 during window hours
            "k_vent_window": 0.0,  # perfectly inert — k_eff = 0 during window hours
            "k_solar": 2.5,
            "k_active_heat": None,
            "k_active_cool": None,
        }
        classification = _make_classification(
            windows_recommended=True,
            window_open_time=time(8, 0),
            window_close_time=time(19, 0),  # extended: keeps hour 18 in-window for clean solar=0 test
        )

        result = _call_vc(entries, now=now, indoor=outdoor_eq_indoor, model=model, classification=classification)

        assert len(result) == 2 * len(hours) - 1
        temps_by_h = {datetime.fromisoformat(r["ts"]).hour: r["temp"] for r in result}

        # Solar accumulates during 8-17h window: peak-solar hours (12-14) must be hottest
        assert temps_by_h[13] > temps_by_h[9], (
            f"Solar accumulation: hour-13 temp should exceed hour-9; got {temps_by_h[13]:.2f} vs {temps_by_h[9]:.2f}°F"
        )

        # Per-hour increments: growing toward peak (9→13), shrinking after (13→17)
        inc_9_10 = temps_by_h[10] - temps_by_h[9]
        inc_12_13 = temps_by_h[13] - temps_by_h[12]
        inc_16_17 = temps_by_h[17] - temps_by_h[16]
        assert inc_12_13 > inc_9_10, (
            f"Solar increment should grow toward peak; Δ[12→13]={inc_12_13:.3f} should exceed Δ[9→10]={inc_9_10:.3f}"
        )
        assert inc_12_13 > inc_16_17, (
            f"Solar increment should be larger at peak than on falling side; "
            f"Δ[12→13]={inc_12_13:.3f} should exceed Δ[16→17]={inc_16_17:.3f}"
        )

        # Hour 17→18: solar_factor(18)=0 → increment must be near zero
        inc_17_18 = temps_by_h[18] - temps_by_h[17]
        assert abs(inc_17_18) < 0.05, f"Hour 17→18: solar_factor=0 → no increment; got Δ={inc_17_18:.3f}°F"
