"""Issue #1404: during a time-of-use bank every surface quotes what the thermostat is told.

Occupant view: while the house pre-cools or pre-heats for a high-cost period (a "bank") the thermostat is told the
bank target (e.g. cool 77 F) but Status, the briefing, Next Action, Next Automation, the plan list and the reading
cards described the schedule band (e.g. "Away band 60-80 F"), so the person could not tell what the house was held to
or when it went back. ``ClimateAdvisorCoordinator.current_hold()`` is the ONE display resolver every surface reads;
these tests pin its answer for each bank kind, a deferred bank, a range thermostat, Celsius and the bank's end time,
and that it never feeds a decision.
"""

from __future__ import annotations

import ast
import types
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from custom_components.climate_advisor.automation import (
    BAND_CONTEXT_AWAY,
    BAND_CONTEXT_COMFORT,
    BAND_CONTEXT_SLEEP,
    BAND_CONTEXT_TOU_BANK,
    BAND_CONTEXT_TOU_EXPANSION,
    BAND_CONTEXT_VACATION,
    BAND_CONTEXTS,
    DISPLAY_BAND_CONTEXTS,
    ComfortBand,
    CurrentHold,
    TouBankHold,
    active_thresholds_for,
    hold_after_text,
    hold_band_label,
    hold_sentence,
    tou_bank_band,
)
from custom_components.climate_advisor.const import OCCUPANCY_AWAY, OCCUPANCY_HOME, OCCUPANCY_VACATION
from custom_components.climate_advisor.coordinator import ClimateAdvisorCoordinator
from custom_components.climate_advisor.scheduler import TOUPhase, TOUPhaseResolution
from tests.test_tou_precondition import _make_cool_day_classification, _make_engine

PKG = Path(__file__).resolve().parent.parent / "custom_components" / "climate_advisor"
NOW = datetime(2026, 7, 13, 14, 0, tzinfo=UTC)
PEAK = datetime(2026, 7, 13, 16, 15, tzinfo=UTC)
OPEN = datetime(2026, 7, 13, 12, 21, tzinfo=UTC)
PEAK_END = datetime(2026, 7, 13, 21, 0, tzinfo=UTC)


def _band(floor: float, ceiling: float, context: str = BAND_CONTEXT_COMFORT, active: str = "ceiling") -> ComfortBand:
    return ComfortBand(floor=floor, ceiling=ceiling, active=active, reason="t", context=context)


# ── pure: the bank band and the one sentence ────────────────────────────────────────────────────────────────────
class TestBankBand:
    def test_pre_cool_ceiling_is_the_target_the_thermostat_is_told(self) -> None:
        b = tou_bank_band(_band(60.0, 80.0, BAND_CONTEXT_AWAY), 77.0, "cool")
        assert (b.floor, b.ceiling, b.active, b.context) == (60.0, 77.0, "ceiling", BAND_CONTEXT_TOU_BANK)

    def test_pre_heat_floor_is_the_target(self) -> None:
        b = tou_bank_band(_band(60.0, 80.0, BAND_CONTEXT_AWAY), 63.0, "heat")
        assert (b.floor, b.ceiling, b.active) == (63.0, 80.0, "floor")

    def test_home_bank_to_the_opposite_edge_collapses_to_the_target(self) -> None:
        """Home cool bank to the comfort floor: the house is held at 68, not 68-76."""
        assert (
            tou_bank_band(_band(68.0, 76.0), 68.0, "cool").floor,
            tou_bank_band(_band(68.0, 76.0), 68.0, "cool").ceiling,
        ) == (
            68.0,
            68.0,
        )
        heat = tou_bank_band(_band(68.0, 76.0), 76.0, "heat")
        assert (heat.floor, heat.ceiling) == (76.0, 76.0)

    def test_a_target_past_the_other_edge_keeps_floor_below_ceiling(self) -> None:
        """A sleep-destination bank (cool to sleep_heat 64 while awake at 68-76) never inverts the band."""
        b = tou_bank_band(_band(68.0, 76.0), 64.0, "cool")
        assert (b.floor, b.ceiling) == (64.0, 64.0)
        h = tou_bank_band(_band(64.0, 72.0, BAND_CONTEXT_SLEEP), 76.0, "heat")
        assert (h.floor, h.ceiling) == (76.0, 76.0)

    def test_display_contexts_add_the_bank_without_changing_the_selector_set(self) -> None:
        assert BAND_CONTEXT_TOU_BANK not in BAND_CONTEXTS
        assert (*BAND_CONTEXTS, BAND_CONTEXT_TOU_BANK) == DISPLAY_BAND_CONTEXTS


class TestSentence:
    def _hold(self, *, mode="cool", target=77.0, after=None, occ=OCCUPANCY_AWAY, period_end=None) -> CurrentHold:
        schedule = _band(60.0, 80.0, BAND_CONTEXT_AWAY)
        bank = TouBankHold(target=target, mode=mode, ends=PEAK, schedule_id="peak1", period_end=period_end)
        return CurrentHold(tou_bank_band(schedule, target, mode), schedule, bank, after or schedule, occ)

    def test_away_pre_cool_fahrenheit(self) -> None:
        assert hold_sentence(self._hold(), "fahrenheit") == (
            "Pre-cooling to 77°F until the 4:15 PM high-cost period, then back to the Away band 60–80°F"
        )

    def test_celsius_quotes_the_display_unit(self) -> None:
        text = hold_sentence(self._hold(), "celsius")
        assert text == "Pre-cooling to 25°C until the 4:15 PM high-cost period, then back to the Away band 16–27°C"

    def test_away_widened_band_names_its_end(self) -> None:
        widened = _band(60.0, 83.0, BAND_CONTEXT_TOU_EXPANSION)
        hold = self._hold(after=widened, period_end=PEAK_END)
        assert hold_after_text(hold, "fahrenheit") == "the Away band widened to 60–83°F until 9:00 PM"

    def test_home_pre_heat(self) -> None:
        schedule = _band(66.0, 78.0, "prefer_savings", active="floor")
        bank = TouBankHold(target=76.0, mode="heat", ends=PEAK)
        hold = CurrentHold(tou_bank_band(schedule, 76.0, "heat"), schedule, bank, schedule, OCCUPANCY_HOME)
        assert hold_sentence(hold, "fahrenheit") == (
            "Pre-heating to 76°F until the 4:15 PM high-cost period, then back to the comfort band 66–78°F"
        )

    def test_no_bank_no_sentence(self) -> None:
        schedule = _band(68.0, 76.0)
        assert hold_sentence(CurrentHold(schedule, schedule), "fahrenheit") is None
        assert hold_sentence(None, "fahrenheit") is None

    @pytest.mark.parametrize(
        ("context", "occ", "label"),
        [
            (BAND_CONTEXT_COMFORT, OCCUPANCY_HOME, "comfort band"),
            (BAND_CONTEXT_SLEEP, OCCUPANCY_HOME, "sleep band"),
            (BAND_CONTEXT_AWAY, OCCUPANCY_AWAY, "Away band"),
            (BAND_CONTEXT_VACATION, OCCUPANCY_VACATION, "Vacation band"),
            (BAND_CONTEXT_TOU_EXPANSION, OCCUPANCY_VACATION, "Vacation band"),
            (BAND_CONTEXT_TOU_EXPANSION, OCCUPANCY_AWAY, "Away band"),
        ],
    )
    def test_band_labels(self, context: str, occ: str, label: str) -> None:
        assert hold_band_label(_band(60.0, 80.0, context), occ) == label


# ── the coordinator resolver on a real engine ───────────────────────────────────────────────────────────────────
def _coordinator(
    *,
    occupancy: str = OCCUPANCY_AWAY,
    mode: str = "cool",
    target: float = 77.0,
    phase: TOUPhase = TOUPhase.PRECONDITIONING,
    unit: str = "fahrenheit",
    thermostat_attrs: dict | None = None,
    expansion: float | None = 3.0,
) -> ClimateAdvisorCoordinator:
    engine = _make_engine(indoor_temp=76.0, current_mode=mode)
    engine.config.update({"setback_heat": 60.0, "setback_cool": 80.0, "temp_unit": unit})
    engine._occupancy_mode = occupancy
    if thermostat_attrs is not None:
        engine.hass.states.get.return_value.attributes = thermostat_attrs
    c = _make_cool_day_classification()
    if mode == "heat":
        c.day_type, c.hvac_mode = "cold", "heat"
    engine._current_classification = c
    coord = object.__new__(ClimateAdvisorCoordinator)
    coord.automation_engine = engine
    coord._current_classification = c
    coord._occupancy_mode = occupancy
    coord.config = engine.config
    away = occupancy in (OCCUPANCY_AWAY, OCCUPANCY_VACATION)
    coord._tou_phase_resolution = TOUPhaseResolution(
        phase=phase,
        target=target,
        mode=mode,
        schedule_id="peak1",
        schedule_start=PEAK,
        precondition_start=OPEN,
        expansion_f=expansion if away else None,
        window_end=PEAK_END if away else None,
    )
    return coord


class TestCurrentHold:
    def test_away_pre_cool_holds_the_bank_and_names_the_schedule_band(self) -> None:
        hold = _coordinator().current_hold(NOW)
        assert hold is not None and hold.bank is not None
        assert (hold.band.floor, hold.band.ceiling, hold.band.context) == (60.0, 77.0, BAND_CONTEXT_TOU_BANK)
        assert (hold.schedule_band.floor, hold.schedule_band.ceiling) == (60.0, 80.0)
        assert hold.bank.ends == PEAK and hold.bank.period_end == PEAK_END
        # the band at the high-cost period's start is the Away band widened by the expansion
        assert (hold.after_band.floor, hold.after_band.ceiling) == (60.0, 83.0)

    def test_away_pre_heat(self) -> None:
        hold = _coordinator(mode="heat", target=63.0).current_hold(NOW)
        assert (hold.band.floor, hold.band.ceiling, hold.band.active) == (63.0, 80.0, "floor")
        assert (hold.after_band.floor, hold.after_band.ceiling) == (57.0, 80.0)

    def test_home_pre_cool_and_pre_heat(self) -> None:
        cool = _coordinator(occupancy=OCCUPANCY_HOME, target=68.0).current_hold(NOW)
        assert (cool.band.floor, cool.band.ceiling) == (68.0, 68.0)
        assert (cool.after_band.floor, cool.after_band.ceiling) == (68.0, 76.0) and cool.bank.period_end is None
        heat = _coordinator(occupancy=OCCUPANCY_HOME, mode="heat", target=76.0).current_hold(NOW)
        assert (heat.band.floor, heat.band.ceiling) == (76.0, 76.0)

    def test_vacation_bank(self) -> None:
        hold = _coordinator(occupancy=OCCUPANCY_VACATION, target=80.0).current_hold(NOW)
        assert hold.bank is not None and hold.band.ceiling == 80.0
        assert hold_band_label(hold.after_band, OCCUPANCY_VACATION) == "Vacation band"

    @pytest.mark.parametrize(
        "defer",
        [
            lambda e: setattr(e, "_override_confirm_pending", True),
            lambda e: setattr(e, "_manual_override_active", True),
            lambda e: setattr(e, "_paused_by_door", True),
            lambda e: setattr(e, "_natural_vent_active", True),
        ],
        ids=["unconfirmed_change", "manual_override", "door_pause", "nat_vent"],
    )
    def test_a_deferred_bank_is_not_claimed(self, defer) -> None:
        """Something holds the thermostat instead of the bank: the hold is the schedule band (what the cycle writes)."""
        coord = _coordinator()
        defer(coord.automation_engine)
        hold = coord.current_hold(NOW)
        assert hold is not None and hold.bank is None
        assert (hold.band.floor, hold.band.ceiling) == (60.0, 80.0)
        assert coord.current_hold_payload(NOW) is None

    def test_bank_ends_at_the_high_cost_period_start(self) -> None:
        coord = _coordinator()
        assert coord.current_hold(PEAK - timedelta(seconds=1)).bank is not None
        assert coord.current_hold(PEAK).bank is None
        assert coord.current_hold(OPEN - timedelta(seconds=1)).bank is None

    def test_no_bank_outside_preconditioning(self) -> None:
        hold = _coordinator(phase=TOUPhase.NONE).current_hold(NOW)
        assert hold.bank is None and hold.band == hold.schedule_band

    def test_range_thermostat_gets_the_same_hold(self) -> None:
        """The hold is what CA tells the thermostat; a heat/cool-range device's own attributes do not change it."""
        single = _coordinator().current_hold(NOW)
        ranged = _coordinator(
            thermostat_attrs={
                "hvac_modes": ["off", "heat", "cool", "heat_cool"],
                "supported_features": 2,
                "target_temp_low": 60.0,
                "target_temp_high": 80.0,
                "current_temperature": 76.0,
            }
        ).current_hold(NOW)
        assert (ranged.band, ranged.bank) == (single.band, single.bank)

    def test_celsius_payload_is_in_the_display_unit(self) -> None:
        payload = _coordinator(unit="celsius").current_hold_payload(NOW)
        assert payload["target"] == pytest.approx(25.0, abs=0.1)
        assert payload["cool"] == pytest.approx(25.0, abs=0.1) and payload["heat"] == pytest.approx(15.6, abs=0.1)
        assert payload["text"].startswith("Pre-cooling to 25°C until the 4:15 PM high-cost period")
        assert payload["ends"] == PEAK.isoformat()

    def test_thresholds_quote_the_bank(self) -> None:
        coord = _coordinator()
        assert active_thresholds_for(coord, NOW) == {
            "heat": 60.0,
            "cool": 77.0,
            "context": BAND_CONTEXT_TOU_BANK,
            "provisional": False,
        }

    def test_a_resolver_fault_returns_none_so_callers_keep_their_path(self) -> None:
        coord = _coordinator()
        coord.automation_engine._resolved_comfort_band = MagicMock(side_effect=RuntimeError("boom"))
        assert coord.current_hold(NOW) is None

    def test_status_says_the_same_sentence(self) -> None:
        coord = _coordinator()
        coord._automation_enabled = True
        coord._startup_coalesce_active = False
        e = coord.automation_engine
        e._is_within_planned_window_period = types.MethodType(lambda self: False, e)
        coord._any_sensor_open = lambda: False
        import custom_components.climate_advisor.coordinator as cm

        orig = cm.dt_util.now
        cm.dt_util.now = lambda time_zone=None: NOW
        try:
            status = coord._compute_automation_status_core()
        finally:
            cm.dt_util.now = orig
        assert status == (
            "pre-cooling to 77°F until the 4:15 PM high-cost period, then the Away band widened to 60–83°F"
            " until 9:00 PM"
        )


# ── structural: no decision reads the display resolver ──────────────────────────────────────────────────────────
DISPLAY_NAMES = {
    "current_hold",
    "current_hold_payload",
    "tou_bank_holds_setpoint",
    "_active_tou_bank",
    "_chart_display_band",
    "_chart_bank_band",
}
# The functions allowed to read the display resolver (all display: cards, payloads, briefing, chart, the bank-refresh).
DISPLAY_CALLERS = {
    "current_hold",
    "current_hold_payload",
    "_active_tou_bank",
    "_compute_automation_status_core",
    "_compute_next_action",
    "_compute_next_automation_action",
    "_build_briefing_text",
    "_briefing_tou_bank_key",
    "_maybe_regenerate_briefing_for_drift",
    # Issue #1409: the occupancy-change bank writer asks whether a bank is active for the new occupancy; what it then
    # writes is the bank's own decision path (_apply_tou_schedule with its gates), not a display value.
    "_occupancy_bank_write",  # text only: regenerates the shown briefing, never a setpoint
    "timeline_payload",
    "_chart_display_band",
    "_chart_bank_band",
    "get_chart_data",
    "active_thresholds_for",
    "_current_hold_payload",
    "generate_briefing",
    "_generate_tldr_table",
}


def test_only_display_functions_read_the_hold() -> None:
    """Decision paths (apply_classification, the bank writes, restores, adoption, offset cap, incidents) never call the
    display resolver, so what the thermostat is told cannot depend on it."""
    offenders: list[str] = []
    for path in (PKG / "coordinator.py", PKG / "automation.py", PKG / "api.py", PKG / "briefing.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for fn in ast.walk(tree):
            if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            for node in ast.walk(fn):
                name = None
                if isinstance(node, ast.Attribute):
                    name = node.attr
                elif (
                    isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Name)
                    and node.func.id == "getattr"
                    and len(node.args) > 1
                    and isinstance(node.args[1], ast.Constant)
                ):
                    name = node.args[1].value
                if name in DISPLAY_NAMES and fn.name not in DISPLAY_CALLERS and fn.name not in DISPLAY_NAMES:
                    offenders.append(f"{path.name}:{fn.name} reads {name}")
    assert not offenders, offenders


def test_resolved_comfort_band_is_still_the_schedule_band_during_a_bank() -> None:
    """The decision band (restores, adoption, offset cap) is untouched: during a bank it is still the Away band."""
    coord = _coordinator()
    band = coord.automation_engine._resolved_comfort_band(coord._current_classification)
    assert (band.floor, band.ceiling) == (60.0, 80.0)


# ── the chart draws the hold; the band schedule the decisions read is unchanged ─────────────────────────────────
def _chart_coord(*, owns: bool = True) -> ClimateAdvisorCoordinator:
    coord = _coordinator()
    if not owns:
        coord.automation_engine._paused_by_door = True

    def build(self, now, thermal_model, hourly_forecast_override=None, *, with_tou_window=True):
        out = []
        for h in range(11, 18):
            ts = datetime(2026, 7, 13, h, 0, tzinfo=UTC)
            lower, upper = 60.0, 80.0
            if with_tou_window and OPEN <= ts < PEAK:
                lower = 77.0  # the #786 shape: the cool bank target on the opposite edge
            out.append({"ts": ts.isoformat(), "lower": lower, "upper": upper})
        return out

    coord._build_target_band_for = types.MethodType(build, coord)
    return coord


def test_chart_draws_the_bank_hours_as_the_hold() -> None:
    coord = _chart_coord()
    raw = coord._build_target_band_for(NOW, None)
    shown = {e["ts"][11:16]: (e["lower"], e["upper"]) for e in coord._chart_display_band(NOW, None, raw_band=raw)}
    assert shown["11:00"] == (60.0, 80.0)  # before the window: untouched
    assert shown["13:00"] == shown["14:00"] == shown["15:00"] == shown["16:00"] == (60.0, 77.0)
    assert shown["17:00"] == (60.0, 83.0)  # the high-cost period: the Away band widened by its 3 F expansion
    # the band the prediction, chart_log and the comfort score read keeps its #786 shape
    assert {e["ts"][11:16]: e["lower"] for e in raw}["14:00"] == 77.0


def test_chart_current_hour_follows_a_deferred_bank() -> None:
    """A bank a door pause defers right now: the current hour shows the band the thermostat is actually on."""
    shown = {e["ts"][11:16]: (e["lower"], e["upper"]) for e in _chart_coord(owns=False)._chart_display_band(NOW, None)}
    assert shown["14:00"] == (60.0, 80.0)
    assert shown["15:00"] == (60.0, 77.0)  # later bank hours stay the plan


def test_next_automation_label_for_the_bank_end_is_ontology_clean() -> None:
    from custom_components.climate_advisor.timeline_surfaces import NEXT_AUTOMATION_LABELS, tou_bank_end_label

    assert (
        tou_bank_end_label("back to the Away band 60–80°F") == "High-cost period starts — back to the Away band 60–80°F"
    )
    assert "tou_bank_end" in NEXT_AUTOMATION_LABELS


# ── review mirrors: the family after a bank, the widened Away/Vacation period ──────────────────────────────────────
def test_armed_family_follows_the_selector_for_an_awake_home_only() -> None:
    coord = _coordinator(occupancy=OCCUPANCY_HOME, phase=TOUPhase.NONE)
    coord.automation_engine._last_resolved_family = "heating"
    assert coord._armed_family(NOW) == "heat"
    assert coord.current_hold(NOW).family == "heat"
    coord.config.update({"sleep_time": "13:00", "wake_time": "15:00"})  # asleep at 14:00: the day's mode stands
    assert coord._armed_family(NOW) is None
    away = _coordinator(phase=TOUPhase.NONE)
    away.automation_engine._last_resolved_family = "heating"
    assert away._armed_family(NOW) is None
    assert _coordinator().current_hold(NOW).family == "cool"  # a bank: its own mode


def test_chart_draws_the_widened_away_period() -> None:
    coord = _coordinator()
    coord._tou_phase_resolution = TOUPhaseResolution(
        phase=TOUPhase.NONE,
        target=77.0,
        mode="cool",
        schedule_id="peak1",
        schedule_start=PEAK,
        precondition_start=OPEN,
        expansion_f=3.0,
        window_end=PEAK_END,
    )
    band = [
        {"ts": datetime(2026, 7, 13, h, 0, tzinfo=UTC).isoformat(), "lower": 60.0, "upper": 80.0}
        for h in (15, 16, 17, 21)
    ]
    at = datetime(2026, 7, 13, 16, 20, tzinfo=UTC)
    got = {e["ts"][11:16]: e["upper"] for e in coord._chart_widened_band(band, at)}
    assert got == {"15:00": 80.0, "16:00": 83.0, "17:00": 83.0, "21:00": 80.0}  # 16:00 holds now (16:20)
    coord._occupancy_mode = OCCUPANCY_HOME
    assert coord._chart_widened_band(band, at) == band
