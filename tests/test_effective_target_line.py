"""Tests for Phase 3a/3b of the TOU-scheduler-follow-up plan: the unified "effective
target" chart line.

Investigation B (see the plan) found the pre-existing predicted_setpoint/
historical_setpoint fields were dead payload (never rendered) that were also blind to
TOU banking and had no nat-vent concept at all. This adds a corrected pair of series —
_extract_historical_effective_target() (past) and _compute_effective_target_forward()
(future) — that fix both gaps, built entirely from data Phase 2 and the pre-existing
predicted_activity proxy already produce (no new resolution logic).

Follows the same lightweight direct-unit-test pattern test_chart_setpoint.py uses for
the sibling _derive_predicted_setpoint()/_extract_historical_setpoint() functions,
rather than standing up a full coordinator.
"""

from __future__ import annotations

import importlib
import sys
from datetime import UTC, datetime, timedelta

if "homeassistant" not in sys.modules:
    from conftest import _install_ha_stubs

    _install_ha_stubs()


def _get_coordinator_module():
    return importlib.import_module("custom_components.climate_advisor.coordinator")


# ===========================================================================
# 3a — _extract_historical_effective_target()
# ===========================================================================


class TestExtractHistoricalEffectiveTarget:
    def _fn(self):
        return _get_coordinator_module()._extract_historical_effective_target

    def test_real_setpoint_wins_when_present(self) -> None:
        """A compressor-commanded setpoint (chart_log's real 'setpoint' field) is always
        the effective target when present, regardless of nat_vent_active/nat_vent_target."""
        fn = self._fn()
        entries = [
            {
                "ts": "2026-05-17T10:00:00+00:00",
                "setpoint": 70.0,
                "nat_vent_active": True,
                "nat_vent_target": 72.0,
            }
        ]
        result = fn(entries)
        assert result == [{"ts": "2026-05-17T10:00:00+00:00", "target": 70.0}]

    def test_nat_vent_target_used_when_no_setpoint_and_nat_vent_active(self) -> None:
        """No real setpoint (thermostat off, nat-vent operating) -> nat_vent_target is
        the effective target -- the exact gap the dead historical_setpoint field had
        (it was always None here since nat-vent never calls set_temperature)."""
        fn = self._fn()
        entries = [
            {
                "ts": "2026-05-17T02:00:00+00:00",
                "setpoint": None,
                "nat_vent_active": True,
                "nat_vent_target": 66.0,
            }
        ]
        result = fn(entries)
        assert result == [{"ts": "2026-05-17T02:00:00+00:00", "target": 66.0}]

    def test_none_when_no_setpoint_and_not_nat_vent_active(self) -> None:
        """Thermostat off, no nat-vent -> genuinely undefined target -> None."""
        fn = self._fn()
        entries = [{"ts": "2026-05-17T03:00:00+00:00", "setpoint": None, "nat_vent_active": False}]
        result = fn(entries)
        assert result == [{"ts": "2026-05-17T03:00:00+00:00", "target": None}]

    def test_missing_keys_treated_as_none_and_inactive(self) -> None:
        """Pre-fix log entries (predating both the setpoint-write-site fix and the
        nat_vent_target field) have neither key -- must not raise, must resolve to None."""
        fn = self._fn()
        entries = [{"ts": "2026-05-17T04:00:00+00:00", "indoor": 68.0}]
        result = fn(entries)
        assert result == [{"ts": "2026-05-17T04:00:00+00:00", "target": None}]

    def test_entries_missing_ts_are_skipped(self) -> None:
        fn = self._fn()
        entries = [{"setpoint": 70.0}, {"ts": "2026-05-17T05:00:00+00:00", "setpoint": 70.0}]
        result = fn(entries)
        assert result == [{"ts": "2026-05-17T05:00:00+00:00", "target": 70.0}]

    def test_empty_input_returns_empty_list(self) -> None:
        fn = self._fn()
        assert fn([]) == []


# ===========================================================================
# 3b — _compute_effective_target_forward()
# ===========================================================================


class TestComputeEffectiveTargetForward:
    def _fn(self):
        return _get_coordinator_module()._compute_effective_target_forward

    def _band(self, ts: str, lower: float = 68.0, upper: float = 76.0) -> list[dict]:
        return [{"ts": ts, "lower": lower, "upper": upper}]

    def _config(self, **overrides) -> dict:
        cfg = {
            "sleep_time": "22:00:00",
            "wake_time": "06:00:00",
        }
        cfg.update(overrides)
        return cfg

    def test_comfort_band_only_matches_active_edge_heat(self) -> None:
        """No TOU window, no predicted nat-vent activity, hvac_mode=heat -> lower bound,
        matching what _derive_predicted_setpoint() has always done for this regime."""
        fn = self._fn()
        ts = "2026-05-17T14:00:00+00:00"
        result = fn(self._band(ts, lower=68.0, upper=76.0), [], "heat", 1.0, self._config())
        assert result == [{"ts": ts, "target": 68.0}]

    def test_comfort_band_only_matches_active_edge_cool(self) -> None:
        fn = self._fn()
        ts = "2026-05-17T14:00:00+00:00"
        result = fn(self._band(ts, lower=68.0, upper=76.0), [], "cool", 1.0, self._config())
        assert result == [{"ts": ts, "target": 76.0}]

    def test_off_mode_with_no_nat_vent_or_tou_is_none(self) -> None:
        fn = self._fn()
        ts = "2026-05-17T14:00:00+00:00"
        result = fn(self._band(ts, lower=68.0, upper=76.0), [], "off", 1.0, self._config())
        assert result == [{"ts": ts, "target": None}]

    def test_tou_banking_target_wins_inside_window(self) -> None:
        """The exact gap _derive_predicted_setpoint() had (Investigation B): a TOU
        precondition window must show the banked target, not the plain band edge."""
        fn = self._fn()
        ts = "2026-05-17T14:00:00+00:00"
        ts_dt = datetime.fromisoformat(ts)
        window = (ts_dt - timedelta(hours=1), ts_dt + timedelta(hours=1), 71.0, "heat")
        result = fn(
            self._band(ts, lower=68.0, upper=76.0),
            [],
            "heat",
            1.0,
            self._config(),
            tou_precondition_window=window,
        )
        assert result == [{"ts": ts, "target": 71.0}]

    def test_tou_target_does_not_apply_outside_window(self) -> None:
        fn = self._fn()
        ts = "2026-05-17T14:00:00+00:00"
        ts_dt = datetime.fromisoformat(ts)
        window = (ts_dt + timedelta(hours=1), ts_dt + timedelta(hours=2), 71.0, "heat")
        result = fn(
            self._band(ts, lower=68.0, upper=76.0),
            [],
            "heat",
            1.0,
            self._config(),
            tou_precondition_window=window,
        )
        assert result == [{"ts": ts, "target": 68.0}]

    def test_nat_vent_target_used_when_predicted_activity_fan_active(self) -> None:
        """Daytime (not sleep window per the test config's wake/sleep hours) predicted
        nat-vent activity -> effective target is the comfort-midpoint cycling target
        (fed from this timestamp's own band lower/upper), not the plain edge."""
        fn = self._fn()
        ts = "2026-05-17T14:00:00+00:00"  # 14:00 -> awake per default 06:00-22:00 window
        predicted_activity = [{"ts": ts, "fan_active": True}]
        result = fn(
            self._band(ts, lower=68.0, upper=76.0),
            predicted_activity,
            "off",
            1.0,
            self._config(),
        )
        # Daytime nat-vent cycling target: (lower + upper) / 2 = 72.0
        assert result == [{"ts": ts, "target": 72.0}]

    def test_nat_vent_target_uses_sleep_branch_during_sleep_window(self) -> None:
        fn = self._fn()
        ts = "2026-05-17T02:00:00+00:00"  # 02:00 -> inside 22:00-06:00 sleep window
        predicted_activity = [{"ts": ts, "fan_active": True}]
        result = fn(
            self._band(ts, lower=64.0, upper=76.0),
            predicted_activity,
            "off",
            1.0,
            self._config(),
        )
        # Sleep branch: sleep_heat (fed as band lower=64.0) + hysteresis(1.0) = 65.0
        assert result == [{"ts": ts, "target": 65.0}]

    def test_tou_wins_over_nat_vent_when_both_apply(self) -> None:
        """Tier ordering: TOU banking target (tier 1) takes priority over the nat-vent
        cycling target (tier 2) when both could apply to the same timestamp."""
        fn = self._fn()
        ts = "2026-05-17T14:00:00+00:00"
        ts_dt = datetime.fromisoformat(ts)
        window = (ts_dt - timedelta(hours=1), ts_dt + timedelta(hours=1), 71.0, "heat")
        predicted_activity = [{"ts": ts, "fan_active": True}]
        result = fn(
            self._band(ts, lower=68.0, upper=76.0),
            predicted_activity,
            "off",
            1.0,
            self._config(),
            tou_precondition_window=window,
        )
        assert result == [{"ts": ts, "target": 71.0}]

    def test_missing_ts_entries_are_skipped(self) -> None:
        fn = self._fn()
        result = fn([{"lower": 68.0, "upper": 76.0}], [], "heat", 1.0, self._config())
        assert result == []

    def test_empty_band_returns_empty_list(self) -> None:
        fn = self._fn()
        assert fn([], [], "heat", 1.0, self._config()) == []

    # --- Assumption Audit #5: graceful degradation when the fan_active proxy could
    # disagree with the real gate (e.g. AWAY_CEILING/PROACTIVE_FLOOR/MANUAL_OVERRIDE_CONFLICT
    # exit reasons the proxy doesn't model) ---

    def test_fan_active_true_but_band_bounds_missing_falls_back_to_tier3_not_crash(self) -> None:
        """If predicted_activity says fan_active=True for a timestamp but this
        timestamp's own band entry has incomplete lower/upper (e.g. a data gap), the
        derivation must degrade to the tier-3 plain edge, never raise or fabricate."""
        fn = self._fn()
        ts = "2026-05-17T14:00:00+00:00"
        band = [{"ts": ts, "lower": None, "upper": 76.0}]
        predicted_activity = [{"ts": ts, "fan_active": True}]
        result = fn(band, predicted_activity, "cool", 1.0, self._config())
        # lower is None -> tier 2 guard fails -> falls through to tier 3 (cool -> upper)
        assert result == [{"ts": ts, "target": 76.0}]

    def test_unparseable_timestamp_falls_back_to_tier3_not_crash(self) -> None:
        fn = self._fn()
        band = [{"ts": "not-a-real-timestamp", "lower": 68.0, "upper": 76.0}]
        predicted_activity = [{"ts": "not-a-real-timestamp", "fan_active": True}]
        window = (datetime.now(UTC), datetime.now(UTC) + timedelta(hours=1), 71.0, "heat")
        result = fn(band, predicted_activity, "heat", 1.0, self._config(), tou_precondition_window=window)
        # ts_dt fails to parse -> both tier 1 and tier 2 skip -> tier 3 (heat -> lower)
        assert result == [{"ts": "not-a-real-timestamp", "target": 68.0}]
