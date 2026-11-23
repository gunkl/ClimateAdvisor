"""Issue #1691 (plan #1507 H1, step 2): the humid-air guard on every fan start.

Occupant view: on a muggy night neither the whole-house fan nor the HVAC blower starts a ventilation session, the
economizer does not switch its fan on, a session whose fan cycled off does not switch it back on, and a whole-house
fan's min-runtime cycle waits; a dry night is unchanged. The "Next Automation" prediction is unchanged (future hours
have no humidity). Each test fails if the guard it names is deleted.

Tier-A scenarios (the real engine, headless) for the same rules: tools/simulations/pending/humid_air_1691_*.json,
run by test_pending_humid_air_scenario_passes_production_engine below.
"""

from __future__ import annotations

import asyncio
import math
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from custom_components.climate_advisor.automation import (
    NAT_VENT_START_PREDICTION,
    NAT_VENT_START_WINDOW_OPEN,
)
from custom_components.climate_advisor.const import (
    CONF_FAN_MIN_RUNTIME_PER_HOUR,
    CONF_FAN_MODE,
    CONF_HUMID_AIR_GUARD_ENABLED,
    FAN_MODE_HVAC,
    FAN_MODE_WHOLE_HOUSE,
)
from custom_components.climate_advisor.engine_inputs import StaticEngineInputs
from custom_components.climate_advisor.humidity_facts import HUMID_AIR_REASON_CEILING, HUMID_AIR_REASON_WETTER
from custom_components.climate_advisor.nat_vent_gate import (
    FORECAST_PEAK_ABOVE_THRESHOLD,
    HUMID_AIR,
    NatVentStartVetoInputs,
    decide_nat_vent_start_veto,
)
from custom_components.climate_advisor.nat_vent_lifecycle import NatVentLifecycleState
from tests.test_fan_control import _PATCH_CALL_LATER, _make_automation_engine

ROOT = Path(__file__).resolve().parents[1]


def _rh(temp_f: float, dp_f: float) -> float:
    """The RH that gives ``dp_f`` at ``temp_f`` (inverse of the Magnus feels_like.dew_point_f)."""
    t, d = (temp_f - 32) / 1.8, (dp_f - 32) / 1.8
    return 100 * math.exp(17.62 * d / (243.12 + d) - 17.62 * t / (243.12 + t))


def _veto_inputs(**kw) -> NatVentStartVetoInputs:
    base = dict(
        outdoor=70.0,
        indoor=76.0,
        comfort_heat=68.0,
        comfort_cool=76.0,
        nat_vent_delta=3.0,
        forecast_peak_2h=None,
        thermal_confidence="none",
        thermal_k_passive=None,
    )
    base.update(kw)
    return NatVentStartVetoInputs(**base)


class TestStartVeto:
    def test_defaults_are_todays_behaviour(self):
        # dew points present but no ceiling (= the guard not asked, e.g. the prediction profile): no veto
        assert decide_nat_vent_start_veto(_veto_inputs(indoor_dew_point_f=55.0, outdoor_dew_point_f=68.0)) is None

    def test_humid_outdoor_air_vetoes_with_its_numbers(self):
        veto = decide_nat_vent_start_veto(
            _veto_inputs(indoor_dew_point_f=55.0, outdoor_dew_point_f=66.0, humid_ceiling_f=65.0)
        )
        assert veto is not None and veto.reason == HUMID_AIR
        assert veto.humid_reason == HUMID_AIR_REASON_CEILING
        assert (veto.outdoor_dew_point_f, veto.indoor_dew_point_f, veto.humid_ceiling_f) == (66.0, 55.0, 65.0)

    def test_wetter_than_house_and_latch(self):
        veto = decide_nat_vent_start_veto(
            _veto_inputs(indoor_dew_point_f=56.5, outdoor_dew_point_f=58.0, humid_ceiling_f=65.0)
        )
        assert veto is None  # 1.5 F wetter: below the 2 F engage margin
        veto = decide_nat_vent_start_veto(
            _veto_inputs(
                indoor_dew_point_f=56.5,
                outdoor_dew_point_f=58.0,
                humid_ceiling_f=65.0,
                humid_latches=frozenset({"humid_air_wetter"}),
            )
        )
        assert veto is not None and veto.humid_reason == HUMID_AIR_REASON_WETTER  # latched: holds inside the band

    def test_dry_air_no_veto(self):
        assert (
            decide_nat_vent_start_veto(
                _veto_inputs(indoor_dew_point_f=56.0, outdoor_dew_point_f=50.0, humid_ceiling_f=65.0)
            )
            is None
        )

    def test_earlier_guards_win(self):
        veto = decide_nat_vent_start_veto(
            _veto_inputs(forecast_peak_2h=90.0, indoor_dew_point_f=55.0, outdoor_dew_point_f=68.0, humid_ceiling_f=65.0)
        )
        assert veto is not None and veto.reason == FORECAST_PEAK_ABOVE_THRESHOLD


def _engine(fan_mode: str = FAN_MODE_WHOLE_HOUSE, *, indoor_dp: float | None, outdoor_dp: float | None, **cfg):
    config = {
        CONF_FAN_MODE: fan_mode,
        "fan_entity": "fan.whf",
        "comfort_heat": 68,
        "comfort_cool": 76,
        "natural_vent_delta": 3.0,
    }
    config.update(cfg)
    engine = _make_automation_engine(config)
    engine._inputs = StaticEngineInputs(
        indoor_f=76.0,
        outdoor_f=70.0,
        humidity=_rh(76.0, indoor_dp) if indoor_dp is not None else None,
        outdoor_humidity=_rh(70.0, outdoor_dp) if outdoor_dp is not None else None,
    )
    events: list[tuple[str, dict]] = []
    engine._emit_event_callback = lambda kind, payload: events.append((kind, payload))
    return engine, events


class TestStartChain:
    def test_window_open_full_gate_vetoed_on_humid_air(self):
        engine, _ = _engine(indoor_dp=56.0, outdoor_dp=67.0)
        result = engine._nat_vent_start_decision(NAT_VENT_START_WINDOW_OPEN, indoor=76.0, outdoor=70.0)
        assert result.to_state == NatVentLifecycleState.INACTIVE
        assert result.veto is not None and result.veto.reason == HUMID_AIR

    def test_window_open_starts_on_dry_air(self):
        engine, _ = _engine(indoor_dp=56.0, outdoor_dp=50.0)
        result = engine._nat_vent_start_decision(NAT_VENT_START_WINDOW_OPEN, indoor=76.0, outdoor=70.0)
        assert result.to_state == NatVentLifecycleState.ACTIVE_FULL_GATE

    def test_unknown_humidity_starts(self):
        engine, _ = _engine(indoor_dp=None, outdoor_dp=None)
        result = engine._nat_vent_start_decision(NAT_VENT_START_WINDOW_OPEN, indoor=76.0, outdoor=70.0)
        assert result.to_state == NatVentLifecycleState.ACTIVE_FULL_GATE

    def test_guard_off_starts(self):
        engine, _ = _engine(indoor_dp=56.0, outdoor_dp=67.0, **{CONF_HUMID_AIR_GUARD_ENABLED: False})
        result = engine._nat_vent_start_decision(NAT_VENT_START_WINDOW_OPEN, indoor=76.0, outdoor=70.0)
        assert result.to_state == NatVentLifecycleState.ACTIVE_FULL_GATE

    def test_soft_start_is_vetoed_too(self):
        """#540 soft start (parity after the peak) goes through the same veto: humid air refuses it."""
        from custom_components.climate_advisor import nat_vent_fsm

        for outdoor_dp, expected in ((67.0, NatVentLifecycleState.INACTIVE), (50.0, None)):
            engine, _ = _engine(indoor_dp=56.0, outdoor_dp=outdoor_dp)
            with (
                patch.object(nat_vent_fsm, "decide_nat_vent_gate", return_value=False),
                patch.object(nat_vent_fsm, "decide_nat_vent_soft_start_gate", return_value=True),
            ):
                result = engine._nat_vent_start_decision(NAT_VENT_START_WINDOW_OPEN, indoor=76.0, outdoor=70.0)
            if expected is None:
                assert result.to_state == NatVentLifecycleState.ACTIVE_SOFT_START
            else:
                assert result.to_state == expected
                assert result.veto.reason == HUMID_AIR
                assert result.vetoed_state == NatVentLifecycleState.ACTIVE_SOFT_START

    def test_prediction_profile_is_unchanged(self):
        """The Next Automation card's future-hour question gets no humidity input (plan: unchanged)."""
        engine, events = _engine(indoor_dp=56.0, outdoor_dp=67.0)
        engine._humid_skip_reported = True
        at = datetime.now(UTC) + timedelta(hours=1)
        result = engine._nat_vent_start_decision(NAT_VENT_START_PREDICTION, indoor=76.0, outdoor=70.0, at=at)
        assert result.to_state == NatVentLifecycleState.ACTIVE_FULL_GATE
        assert events == [] and engine._humid_skip_reported is True  # neither reports nor clears the spell


class TestReporting:
    def test_one_row_per_humid_spell(self):
        engine, events = _engine(indoor_dp=56.0, outdoor_dp=67.0)
        assert engine._humid_air_blocks_fan_start(starter="economizer") is True
        assert engine._humid_air_blocks_fan_start(starter="economizer") is True
        assert [k for k, _ in events] == ["nat_vent_humid_skip"]
        payload = events[0][1]
        assert payload["reason"] == HUMID_AIR_REASON_CEILING and payload["starter"] == "economizer"
        assert payload["outdoor_dew_point"] == pytest.approx(67.0, abs=0.05)
        assert payload["indoor_dew_point"] == pytest.approx(56.0, abs=0.05)
        assert payload["ceiling"] == 65.0
        # the air dries and the coordinator's tick ends the spell (review B1); humid again: a new row
        engine._inputs.outdoor_humidity = _rh(70.0, 50.0)
        assert engine._humid_air_blocks_fan_start(starter="economizer") is False
        engine.note_humid_spell_over()
        engine._inputs.outdoor_humidity = _rh(70.0, 67.0)
        assert engine._humid_air_blocks_fan_start(starter="nat_vent_cycling") is True
        assert [k for k, _ in events] == ["nat_vent_humid_skip", "nat_vent_humid_skip"]

    def test_start_chain_refusal_reports_through_the_dedup(self):
        engine, events = _engine(indoor_dp=56.0, outdoor_dp=67.0)
        result = engine._nat_vent_start_decision(NAT_VENT_START_WINDOW_OPEN, indoor=76.0, outdoor=70.0)
        engine._report_nat_vent_start_veto(result.veto, entity_id="binary_sensor.w")
        engine._log_nat_vent_not_engaged(
            result.to_state,
            indoor=76.0,
            outdoor=70.0,
            comfort_heat=68.0,
            comfort_cool=76.0,
            nat_vent_delta=3.0,
            hysteresis=0.0,
            threshold=79.0,
            veto=result.veto,
        )
        assert [k for k, _ in events] == ["nat_vent_humid_skip"]
        assert events[0][1]["starter"] == "nat_vent"


class TestMinRuntimeCycle:
    def test_whole_house_fan_cycle_waits_on_humid_air(self):
        engine, events = _engine(
            FAN_MODE_WHOLE_HOUSE, indoor_dp=56.0, outdoor_dp=67.0, **{CONF_FAN_MIN_RUNTIME_PER_HOUR: 10}
        )
        engine._whf_start_blocked_by_sealed_house = lambda: False  # a window is open
        with patch(_PATCH_CALL_LATER, return_value=MagicMock()) as later:
            asyncio.run(engine._fan_cycle_on())
        engine.hass.services.async_call.assert_not_called()
        assert engine._fan_min_runtime_active is False
        assert [c.args[1] for c in later.call_args_list] == [300]
        assert [k for k, _ in events] == ["nat_vent_humid_skip"] and events[0][1]["starter"] == "min_runtime"

    def test_whole_house_fan_cycle_runs_on_dry_air(self):
        engine, _ = _engine(
            FAN_MODE_WHOLE_HOUSE, indoor_dp=56.0, outdoor_dp=50.0, **{CONF_FAN_MIN_RUNTIME_PER_HOUR: 10}
        )
        engine._whf_start_blocked_by_sealed_house = lambda: False
        with patch(_PATCH_CALL_LATER, return_value=MagicMock()):
            asyncio.run(engine._fan_cycle_on())
        engine.hass.services.async_call.assert_called()
        assert engine._fan_min_runtime_active is True

    def test_blower_cycle_only_recirculates_and_is_unaffected(self):
        engine, events = _engine(FAN_MODE_HVAC, indoor_dp=56.0, outdoor_dp=67.0, **{CONF_FAN_MIN_RUNTIME_PER_HOUR: 10})
        with patch(_PATCH_CALL_LATER, return_value=MagicMock()):
            asyncio.run(engine._fan_cycle_on())
        engine.hass.services.async_call.assert_called()
        assert engine._fan_min_runtime_active is True
        assert "nat_vent_humid_skip" not in [k for k, _ in events]


class TestStatusAndActivity:
    def test_status_line_while_blocking(self):
        from custom_components.climate_advisor.api import humid_air_view

        engine, _ = _engine(indoor_dp=56.0, outdoor_dp=67.0)
        view = humid_air_view(engine, "fahrenheit")
        assert view is not None and view["code"] == HUMID_AIR_REASON_CEILING
        assert "67.0" in view["text"] and "65.0" in view["text"]
        assert view["text_msg"]["key"] == "status.humid_air.blocking_ceiling"
        celsius = humid_air_view(engine, "celsius")
        assert celsius["ceiling"] == pytest.approx(18.3, abs=0.05)  # absolute conversion of 65 F
        engine._inputs.outdoor_humidity = _rh(70.0, 50.0)
        assert humid_air_view(engine, "fahrenheit") is None
        assert humid_air_view(SimpleNamespace(), "fahrenheit") is None

    def test_status_line_wetter(self):
        from custom_components.climate_advisor.api import humid_air_view

        engine, _ = _engine(indoor_dp=55.0, outdoor_dp=58.0)
        view = humid_air_view(engine, "fahrenheit", "de")
        assert view["code"] == HUMID_AIR_REASON_WETTER and "58,0" in view["text"] and "55,0" in view["text"]  # de
        assert view["text_msg"]["key"] == "status.humid_air.blocking_wetter"

    def test_activity_renderer(self):
        from custom_components.climate_advisor import i18n
        from custom_components.climate_advisor.ai_skills_context import EVENT_RENDERERS

        render = EVENT_RENDERERS["nat_vent_humid_skip"]
        label, _ = render({"reason": "ceiling", "outdoor_dew_point": 67.0, "ceiling": 65.0}, "fahrenheit")
        text = i18n.render(label, "en") if not isinstance(label, str) else label
        assert "67" in text and "65" in text
        label, _ = render(
            {"reason": "wetter_than_house", "outdoor_dew_point": 58.0, "indoor_dew_point": 55.0}, "fahrenheit"
        )
        text = i18n.render(label, "en") if not isinstance(label, str) else label
        assert "58" in text and "55" in text
        label, _ = render({}, "fahrenheit")
        assert (i18n.render(label, "en") if not isinstance(label, str) else label) == "Fan not started -- humid outside"


class TestTwoZones:
    """A two-zone install where only one zone has an indoor humidity reading: the relative clause needs both dew
    points (zone without one is not blocked by it), the ceiling needs only the outdoor one (both blocked)."""

    def test_zone_without_indoor_humidity(self):
        from tools.sim_harness.build_coordinator import build_headless_multi_zone
        from tools.sim_harness.fake_hass import FakeState
        from tools.sim_harness.humidity_devices import (
            INDOOR_HUMIDITY_ENTITY,
            OUTDOOR_HUMIDITY_ENTITY,
            seed_same_device,
        )

        outdoor = "sensor.outdoor_temp"
        cfg = {"outdoor_temp_source": "sensor", "outdoor_temp_entity": outdoor}
        zones, fake_hass, scheduler = build_headless_multi_zone(
            configs=[
                {"climate_entity": "climate.zone_a", "zone_title": "Zone A", **cfg},
                {"climate_entity": "climate.zone_b", "zone_title": "Zone B", **cfg},
            ]
        )
        a, b = zones["zone_0"]["coordinator"], zones["zone_1"]["coordinator"]
        for climate in ("climate.zone_a", "climate.zone_b"):
            st = fake_hass.states.get(climate)
            fake_hass.states.set(
                climate, FakeState(state="cool", attributes={**st.attributes, "current_temperature": 76.0})
            )
        fake_hass.states.set(outdoor, FakeState(state="70.0", attributes={"unit_of_measurement": "°F"}))
        seed_same_device(a, "climate.zone_a", INDOOR_HUMIDITY_ENTITY, outdoor=False)
        for coord in (a, b):
            seed_same_device(coord, outdoor, OUTDOOR_HUMIDITY_ENTITY, outdoor=True)
        with scheduler.installed():
            fake_hass.states.async_report_humidity(INDOOR_HUMIDITY_ENTITY, _rh(76.0, 55.0))
            fake_hass.states.async_report_humidity(OUTDOOR_HUMIDITY_ENTITY, _rh(70.0, 60.0))
            now = scheduler.now()
            va, vb = a.automation_engine.humid_air_verdict(now), b.automation_engine.humid_air_verdict(now)
            assert va.indoor_dp_f == pytest.approx(55.0, abs=0.1) and vb.indoor_dp_f is None
            assert va.blocks and va.reason == HUMID_AIR_REASON_WETTER
            assert not vb.blocks  # zone B has no indoor dew point: the relative clause does not apply
            fake_hass.states.async_report_humidity(OUTDOOR_HUMIDITY_ENTITY, _rh(70.0, 67.0))
            va, vb = a.automation_engine.humid_air_verdict(now), b.automation_engine.humid_air_verdict(now)
            assert va.blocks and vb.blocks and vb.reason == HUMID_AIR_REASON_CEILING


_PENDING = sorted((ROOT / "tools" / "simulations" / "pending").glob("humid_air_1691_*.json"))


@pytest.mark.parametrize("scenario_path", _PENDING, ids=lambda p: p.stem.removeprefix("humid_air_1691_"))
def test_pending_humid_air_scenario_passes_production_engine(scenario_path: Path) -> None:
    import sys

    sys.path.insert(0, str(ROOT / "tools"))
    from simulate import run_scenario_production  # noqa: PLC0415

    result = run_scenario_production(scenario_path, state="pending")
    assert not result.get("callback_errors"), result.get("callback_errors")
    real = [x for x in result["assertions"] if not x.get("skipped")]
    assert real and len(real) == len(result["assertions"]), "no assertion of a humid-air scenario may be skipped"
    assert result["passed"] is True, "; ".join(
        f"@{x['at']} expected={x['expected']!r} actual={x['actual']!r}"
        for x in result["assertions"]
        if not x.get("pass")
    )


def test_humid_air_scenarios_discovered() -> None:
    assert len(_PENDING) == 14, [p.stem for p in _PENDING]


# ── review W2A fixes ──────────────────────────────────────────────────────────────────────────────────────────────
class TestReviewFixes:
    def test_min_runtime_retry_runs_the_fan_once_dry(self):
        """A6: the 300 s retry of a humid deferral starts the fan when the air has dried."""
        engine, _ = _engine(
            FAN_MODE_WHOLE_HOUSE, indoor_dp=56.0, outdoor_dp=67.0, **{CONF_FAN_MIN_RUNTIME_PER_HOUR: 10}
        )
        engine._whf_start_blocked_by_sealed_house = lambda: False
        spawned: list = []
        engine._spawn_whf_task = spawned.append
        with patch(_PATCH_CALL_LATER, return_value=MagicMock()) as later:
            asyncio.run(engine._fan_cycle_on())
            (delay, retry) = later.call_args_list[-1].args[1:3]
            assert delay == 300 and engine._fan_min_runtime_active is False
            engine._inputs.outdoor_humidity = _rh(70.0, 50.0)
            retry(None)
            assert len(spawned) == 1
            asyncio.run(spawned[0])
        assert engine._fan_min_runtime_active is True
        engine.hass.services.async_call.assert_called()

    def test_spell_reset_comes_from_the_tick_not_from_decisions(self):
        """B1 / A7: a dry start decision no longer ends the spell; note_humid_spell_over() (the tick) does."""
        engine, events = _engine(indoor_dp=56.0, outdoor_dp=67.0)
        assert engine._humid_air_blocks_fan_start(starter="economizer") is True
        engine._inputs.outdoor_humidity = _rh(70.0, 50.0)
        assert engine._humid_air_blocks_fan_start(starter="economizer") is False
        engine._nat_vent_start_decision(NAT_VENT_START_WINDOW_OPEN, indoor=76.0, outdoor=70.0)
        assert engine._humid_skip_reported is True  # decisions never clear it
        engine.note_humid_spell_over()
        engine._inputs.outdoor_humidity = _rh(70.0, 67.0)
        assert engine._humid_air_blocks_fan_start(starter="economizer") is True
        assert [k for k, _ in events] == ["nat_vent_humid_skip", "nat_vent_humid_skip"]

    def test_tick_ends_the_spell_only_when_no_latch_is_on(self):
        """B1: a humid night after a dry, windows-shut day (no start decision ran) still writes its Activity row."""
        from tools.sim_harness.build_coordinator import build_headless_multi_zone
        from tools.sim_harness.fake_hass import FakeState
        from tools.sim_harness.humidity_devices import OUTDOOR_HUMIDITY_ENTITY, seed_same_device

        outdoor = "sensor.outdoor_temp"
        zones, fake_hass, scheduler = build_headless_multi_zone(
            configs=[
                {"climate_entity": "climate.zone_a", "outdoor_temp_source": "sensor", "outdoor_temp_entity": outdoor}
            ]
        )
        coord = zones["zone_0"]["coordinator"]
        engine = coord.automation_engine
        fake_hass.states.set(outdoor, FakeState(state="70.0", attributes={"unit_of_measurement": "°F"}))
        seed_same_device(coord, outdoor, OUTDOOR_HUMIDITY_ENTITY, outdoor=True)
        with scheduler.installed():
            fake_hass.states.async_report_humidity(OUTDOOR_HUMIDITY_ENTITY, _rh(70.0, 67.0))
            engine._humid_skip_reported = True  # a refusal was reported earlier in this spell
            coord._sample_humidity_facts(scheduler.now())
            assert engine._humid_skip_reported is True  # ceiling latch on: still the same spell
            fake_hass.states.async_report_humidity(OUTDOOR_HUMIDITY_ENTITY, _rh(70.0, 50.0))
            coord._dew_point_history = None  # a fresh window: the dry reading alone
            coord._sample_humidity_facts(scheduler.now())
            assert engine._humid_skip_reported is False  # no latch on: the spell is over

    def test_status_line_only_when_a_fan_could_start(self):
        """A4/B5, W2A-C N1: no Status line on a fan-less install; with a fan it shows on any day type (heat days
        included: ventilation is asked then too); neutral wording."""
        from custom_components.climate_advisor.api import humid_air_view
        from custom_components.climate_advisor.const import FAN_MODE_DISABLED

        engine, _ = _engine(indoor_dp=56.0, outdoor_dp=67.0)
        view = humid_air_view(engine, "fahrenheit")
        assert view["text"].startswith("Fan not started: humid outside") and "AC" not in view["text"]
        assert "window" not in view["text"].lower()
        engine._current_classification = SimpleNamespace(hvac_mode="heat")
        assert humid_air_view(engine, "fahrenheit") is not None
        engine._current_classification = SimpleNamespace(hvac_mode="cool")
        assert humid_air_view(engine, "fahrenheit") is not None
        fanless, _ = _engine(FAN_MODE_DISABLED, indoor_dp=56.0, outdoor_dp=67.0)
        assert humid_air_view(fanless, "fahrenheit") is None

    def test_next_automation_promises_no_fan_start_while_humid(self):
        """B4: the natural-ventilation prediction scan is skipped while the live verdict blocks."""
        from custom_components.climate_advisor import coordinator as coord_mod

        for outdoor_dp, scanned in ((67.0, False), (50.0, True)):
            engine, _ = _engine(indoor_dp=56.0, outdoor_dp=outdoor_dp)
            engine._grace_active = False
            fake = SimpleNamespace(
                automation_engine=engine,
                config=engine.config,
                control_predicted_indoor=lambda: [("t", 76.0)],
                _any_sensor_open=lambda: True,
                _hourly_forecast_temps=[],
            )
            calls: list = []
            with (
                patch.object(
                    coord_mod,
                    "decide_sync",
                    lambda _c, fn, *a, **k: fn(*a, **{x: v for x, v in k.items() if x != "facts_at"}),
                ),
                patch.object(coord_mod, "_build_future_forecast_outdoor", lambda *_a: []),
                patch.object(coord_mod, "find_temperature_crossing", lambda *a, _c=calls, **k: _c.append(1) or "at"),
            ):
                result = coord_mod.ClimateAdvisorCoordinator._predict_nat_vent_start(fake, datetime.now(UTC), None)
            assert (result == "at") is scanned and bool(calls) is scanned
