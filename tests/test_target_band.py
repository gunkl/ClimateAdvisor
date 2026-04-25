"""Tests for _compute_target_band_schedule (Issue #119 — dynamic Target Band).

The function is a pure module-level function in coordinator.py, so it can be
imported and tested directly without a running HA instance.
"""

from __future__ import annotations

import sys
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

# ── HA module stubs (must happen before importing climate_advisor) ──
if "homeassistant" not in sys.modules:
    from conftest import _install_ha_stubs

    _install_ha_stubs()

from custom_components.climate_advisor.coordinator import _compute_target_band_schedule  # noqa: E402

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _ts(hour: int, minute: int = 0, day_offset: int = 0) -> datetime:
    """Return a UTC-aware datetime on a fixed base date at the given hour:minute."""
    base = datetime(2025, 6, 15, tzinfo=UTC)
    return base.replace(hour=hour, minute=minute) + timedelta(days=day_offset)


_BASE_CONFIG = {
    "comfort_heat": 70.0,
    "comfort_cool": 75.0,
    "setback_heat": 60.0,
    "setback_cool": 80.0,
    "sleep_heat": 66.0,
    "sleep_cool": 78.0,
    "wake_time": "07:00",
    "sleep_time": "22:00",
}

# "now" is at 08:00 on the same base date (within wake ramp period)
_NOW = _ts(8)


# ---------------------------------------------------------------------------
# Test 1 — home, awake hours → comfort band
# ---------------------------------------------------------------------------
class TestHomeAwakeBand:
    def test_home_awake_band_is_comfort(self):
        """Timestamps during awake hours (wake+2h…sleep) should return comfort band."""
        ts_awake = _ts(12)  # noon — well inside the awake window
        result = _compute_target_band_schedule([ts_awake], _BASE_CONFIG, "home", _NOW)
        assert len(result) == 1
        entry = result[0]
        assert entry["lower"] == 70.0
        assert entry["upper"] == 75.0


# ---------------------------------------------------------------------------
# Test 2 — home, sleep hours → sleep setback band
# ---------------------------------------------------------------------------
class TestHomeSleepBand:
    def test_home_sleep_band_is_sleep_setback(self):
        """Timestamps before wake_time (pre-wake / deep-sleep) return sleep band."""
        ts_sleep = _ts(2)  # 02:00 — before wake_time of 07:00
        result = _compute_target_band_schedule([ts_sleep], _BASE_CONFIG, "home", _NOW)
        assert len(result) == 1
        entry = result[0]
        assert entry["lower"] == 66.0
        assert entry["upper"] == 78.0


# ---------------------------------------------------------------------------
# Test 3 — away today → flat setback band
# ---------------------------------------------------------------------------
class TestAwayTodayBand:
    def test_away_today_band_is_setback(self):
        """Away occupancy + today's date → flat setback band, no schedule ramp."""
        ts_today = _ts(14)  # 14:00 today
        result = _compute_target_band_schedule([ts_today], _BASE_CONFIG, "away", _NOW)
        assert len(result) == 1
        entry = result[0]
        assert entry["lower"] == 60.0
        assert entry["upper"] == 80.0


# ---------------------------------------------------------------------------
# Test 4 — vacation today → deep setback band (extra ±3°F)
# ---------------------------------------------------------------------------
class TestVacationTodayBand:
    def test_vacation_today_band_is_deep_setback(self):
        """Vacation occupancy + today → setback ± VACATION_SETBACK_EXTRA (3°F)."""
        ts_today = _ts(14)  # 14:00 today
        result = _compute_target_band_schedule([ts_today], _BASE_CONFIG, "vacation", _NOW)
        assert len(result) == 1
        entry = result[0]
        assert entry["lower"] == 57.0  # 60 - 3
        assert entry["upper"] == 83.0  # 80 + 3


# ---------------------------------------------------------------------------
# Test 5 — away but tomorrow → uses home schedule, not setback
# ---------------------------------------------------------------------------
class TestAwayTomorrowUsesSchedule:
    def test_away_tomorrow_uses_schedule(self):
        """Away occupancy + future day → home wake/sleep schedule applies (not setback)."""
        ts_tomorrow_sleep = _ts(2, day_offset=1)  # 02:00 tomorrow — sleep hours
        result = _compute_target_band_schedule([ts_tomorrow_sleep], _BASE_CONFIG, "away", _NOW)
        assert len(result) == 1
        entry = result[0]
        # Tomorrow future → pre-wake sleep band
        assert entry["lower"] == 66.0
        assert entry["upper"] == 78.0


# ---------------------------------------------------------------------------
# Test 6 — wake ramp period interpolates between sleep and comfort
# ---------------------------------------------------------------------------
class TestWakeRampInterpolates:
    def test_ramp_period_interpolates(self):
        """At wake_h + 1h (midpoint of 2h ramp), values should be halfway between sleep and comfort."""
        # wake_time=07:00, ramp=2h → at 08:00 (wake_h + 1h) frac = 0.5
        ts_ramp = _ts(8)  # exactly 1h into the 2h wake ramp
        result = _compute_target_band_schedule([ts_ramp], _BASE_CONFIG, "home", _NOW)
        assert len(result) == 1
        entry = result[0]
        # lower = 66 + 0.5 * (70 - 66) = 68.0
        # upper = 78 + 0.5 * (75 - 78) = 76.5
        assert entry["lower"] == 68.0
        assert entry["upper"] == 76.5


# ---------------------------------------------------------------------------
# Test 7 — returns one entry per timestamp
# ---------------------------------------------------------------------------
class TestBandLengthMatchesTimestamps:
    def test_band_returns_one_entry_per_timestamp(self):
        """Result length must equal input timestamp list length."""
        timestamps = [_ts(h) for h in range(0, 8)]
        result = _compute_target_band_schedule(timestamps, _BASE_CONFIG, "home", _NOW)
        assert len(result) == 8


# ---------------------------------------------------------------------------
# Test 8 — guest mode uses home schedule
# ---------------------------------------------------------------------------
class TestGuestModeUsesHomeSchedule:
    def test_guest_mode_uses_home_schedule(self):
        """Guest occupancy should behave identically to home occupancy."""
        ts_awake = _ts(12)
        result = _compute_target_band_schedule([ts_awake], _BASE_CONFIG, "guest", _NOW)
        assert len(result) == 1
        entry = result[0]
        assert entry["lower"] == 70.0
        assert entry["upper"] == 75.0


# ---------------------------------------------------------------------------
# Test 9 — sleep ramp at bedtime (1h ramp from comfort → sleep band)
# ---------------------------------------------------------------------------
class TestSleepRampInterpolates:
    def test_sleep_ramp_interpolates(self):
        """At sleep_h + 0.5h (midpoint of 1h ramp), values should be halfway between comfort and sleep."""
        # sleep_time=22:00, 1h ramp → at 22:30 frac = 0.5
        ts_sleep_ramp = _ts(22, minute=30)
        result = _compute_target_band_schedule([ts_sleep_ramp], _BASE_CONFIG, "home", _NOW)
        assert len(result) == 1
        entry = result[0]
        # lower = 70 + 0.5 * (66 - 70) = 68.0
        # upper = 75 + 0.5 * (78 - 75) = 76.5
        assert entry["lower"] == 68.0
        assert entry["upper"] == 76.5


# ---------------------------------------------------------------------------
# Test 10 — post-sleep (after sleep_h + 1h) → full sleep band
# ---------------------------------------------------------------------------
class TestPostSleepBand:
    def test_post_sleep_is_sleep_band(self):
        """Timestamps after sleep_h + 1h (post-ramp) should return the full sleep band."""
        # sleep_time=22:00; post-sleep starts at 23:00
        ts_post_sleep = _ts(23, minute=30)
        result = _compute_target_band_schedule([ts_post_sleep], _BASE_CONFIG, "home", _NOW)
        assert len(result) == 1
        entry = result[0]
        assert entry["lower"] == 66.0
        assert entry["upper"] == 78.0


# ---------------------------------------------------------------------------
# NEW-1: vacation future days stay in setback (I5)
# ---------------------------------------------------------------------------
class TestVacationFutureDaysSetback:
    def test_vacation_future_days_stay_in_setback(self):
        """Vacation future days must stay at deep setback, not revert to home schedule.

        Away mode only applies setback to today; vacation mode applies setback to ALL days
        because there is no scheduled return time during a vacation.
        """
        ts_tomorrow_waking = _ts(14, day_offset=1)  # 14:00 tomorrow
        result = _compute_target_band_schedule([ts_tomorrow_waking], _BASE_CONFIG, "vacation", _NOW)
        assert len(result) == 1
        entry = result[0]
        assert entry["lower"] == 57.0, (  # setback_heat(60) - VACATION_EXTRA(3)
            f"Vacation tomorrow: expected lower=57.0, got {entry['lower']}"
        )
        assert entry["upper"] == 83.0, (  # setback_cool(80) + VACATION_EXTRA(3)
            f"Vacation tomorrow: expected upper=83.0, got {entry['upper']}"
        )


# ---------------------------------------------------------------------------
# NEW-2: midnight wraparound for night-owl schedules (I6)
# ---------------------------------------------------------------------------
class TestMidnightWraparound:
    def test_noon_is_awake_for_late_night_schedule(self):
        """Night-owl schedule (sleep=01:00, wake=09:00): noon (12:00) must be in awake band.

        Without wraparound normalization, noon falls through to 'post-sleep' (sleep band)
        because h=12 > sleep_h=1 and h > sleep_h+1=2, which the naïve comparisons
        misidentify as post-sleep.
        """
        night_owl_config = {
            **_BASE_CONFIG,
            "sleep_time": "01:00",
            "wake_time": "09:00",
        }
        ts_noon = _ts(12)
        result = _compute_target_band_schedule([ts_noon], night_owl_config, "home", _NOW)
        assert len(result) == 1
        entry = result[0]
        assert entry["lower"] == 70.0, f"Night-owl noon: expected awake lower=70.0, got {entry['lower']}"
        assert entry["upper"] == 75.0, f"Night-owl noon: expected awake upper=75.0, got {entry['upper']}"


# ---------------------------------------------------------------------------
# NEW-3: setback_modifier shifts away and vacation bands (I3)
# ---------------------------------------------------------------------------
class TestSetbackModifier:
    def test_setback_modifier_shifts_away_band(self):
        """setback_modifier=+2 must shift both bounds of the away band up by 2°F."""
        ts_today = _ts(14)
        result = _compute_target_band_schedule([ts_today], _BASE_CONFIG, "away", _NOW, setback_modifier=2.0)
        assert len(result) == 1
        entry = result[0]
        assert entry["lower"] == 62.0, (  # setback_heat(60) + modifier(2)
            f"Away + modifier=2: expected lower=62.0, got {entry['lower']}"
        )
        assert entry["upper"] == 82.0, (  # setback_cool(80) + modifier(2)
            f"Away + modifier=2: expected upper=82.0, got {entry['upper']}"
        )

    def test_setback_modifier_zero_unchanged(self):
        """modifier=0 must produce identical output to calling without modifier."""
        ts_today = _ts(14)
        result_no_mod = _compute_target_band_schedule([ts_today], _BASE_CONFIG, "away", _NOW)
        result_zero = _compute_target_band_schedule([ts_today], _BASE_CONFIG, "away", _NOW, setback_modifier=0.0)
        assert result_no_mod == result_zero


# ---------------------------------------------------------------------------
# NEW-4: compute_bedtime_setback used for sleep band when thermal model available (G1/G2)
# ---------------------------------------------------------------------------
class TestComputeBedtimeSetbackIntegration:
    def test_compute_bedtime_setback_used_when_model_available(self):
        """With thermal_model + classification, sleep lower = compute_bedtime_setback() output.

        Config has explicit sleep_heat=66. compute_bedtime_setback returns
        max(sleep_heat + setback_modifier, setback_heat) = max(66+2, 60) = 68.0.
        """
        ts_sleep = _ts(2)  # 02:00 — deep sleep window
        thermal_model = {
            "confidence": "low",
            "k_passive": -0.3,
            "k_active_heat": 4.0,
            "k_active_cool": -4.0,
            "heating_rate_f_per_hour": 4.0,
            "cooling_rate_f_per_hour": -4.0,
        }
        classification = SimpleNamespace(hvac_mode="heat", setback_modifier=2.0)
        result = _compute_target_band_schedule(
            [ts_sleep],
            _BASE_CONFIG,
            "home",
            _NOW,
            thermal_model=thermal_model,
            classification=classification,
        )
        assert len(result) == 1
        entry = result[0]
        assert entry["lower"] == 68.0, f"With thermal_model+classification: expected lower=68.0, got {entry['lower']}"

    def test_compute_bedtime_setback_cool_used_when_model_available(self):
        """With thermal_model + cool classification, sleep upper = compute_bedtime_setback() output.

        Cool path negates setback_modifier: min(sleep_cool + (-modifier), setback_cool)
        = min(78 + (-2), 80) = 76.0 — less extreme than raw sleep_cool=78.
        """
        ts_sleep = _ts(2)  # 02:00 — deep sleep window
        thermal_model = {
            "confidence": "low",
            "k_passive": -0.3,
            "k_active_heat": 4.0,
            "k_active_cool": -4.0,
            "heating_rate_f_per_hour": 4.0,
            "cooling_rate_f_per_hour": -4.0,
        }
        classification = SimpleNamespace(hvac_mode="cool", setback_modifier=2.0)
        result = _compute_target_band_schedule(
            [ts_sleep],
            _BASE_CONFIG,
            "home",
            _NOW,
            thermal_model=thermal_model,
            classification=classification,
        )
        assert len(result) == 1
        entry = result[0]
        assert entry["upper"] == 76.0, (
            f"With cool thermal_model+classification: expected upper=76.0, got {entry['upper']}"
        )

    # ---------------------------------------------------------------------------
    # NEW-5: config fallback when no thermal model (G1/G2)
    # ---------------------------------------------------------------------------
    def test_config_fallback_when_no_model(self):
        """Without thermal_model, sleep lower bound = config sleep_heat (no adaptive calc)."""
        ts_sleep = _ts(2)  # 02:00 — deep sleep window
        result = _compute_target_band_schedule([ts_sleep], _BASE_CONFIG, "home", _NOW)
        assert len(result) == 1
        entry = result[0]
        assert entry["lower"] == 66.0, (
            f"Without thermal_model: expected lower=66.0 (config default), got {entry['lower']}"
        )
