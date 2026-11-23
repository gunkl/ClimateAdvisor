"""Issue #1693 (plan #1507 H2): the humid-aware HVAC-fan restriction.

Occupant view: on a blower install that chose "Heat only" for ``hvac_fan_restrict_mode``, the blower waits out the
2 hours after a cooling run only while the house is humid (indoor dew point at or above 55 F, released at 54 F): a
wet coil would blow its water back into an already humid house. A dry house gets its blower circulation (free
cooling) right away. Unknown indoor humidity keeps the 2-hour rule exactly as before. The "currently in cool mode"
clause, the default ``both`` (no restriction), the whole-house fan and the 'cool' restriction ("heated within 2h",
no condensate on heat) are unchanged.

Detectors vs controls (review W2C A2): the dry-house tests and the latch-hold test fail if the humid condition is
deleted (they would see today's 2 h refusal). The humid-house and unknown-humidity tests also pass on the code
before #1693 (it refused every start within 2 h): they are CONTROLS that guard against the opposite mutation (humid
never restricting, unknown read as dry); the humid test additionally pins the extended log reason. The ``both`` tests
fail if the blower verdict is taken before the whole-house-fan leg switches the thermostat off.

Tier-A scenarios (the real engine and coordinator, headless): tools/simulations/pending/humid_fan_restrict_1693_*.json,
run by test_pending_humid_fan_restrict_scenario_passes_production_engine below.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from custom_components.climate_advisor.automation import FanCommandResult
from custom_components.climate_advisor.const import (
    CONF_FAN_ENTITY,
    CONF_FAN_MODE,
    CONF_HVAC_FAN_RESTRICT_MODE,
    FAN_MODE_BOTH,
    FAN_MODE_HVAC,
    FAN_MODE_WHOLE_HOUSE,
    HVAC_FAN_RESTRICT_BOTH,
    HVAC_FAN_RESTRICT_COOL,
    HVAC_FAN_RESTRICT_HEAT,
)
from custom_components.climate_advisor.engine_inputs import StaticEngineInputs
from custom_components.climate_advisor.humidity_facts import (
    HUMID_AIR_CEILING_LATCH,
    HUMID_AIR_WETTER_LATCH,
    HVAC_FAN_RESTRICT_DP_F,
    HVAC_FAN_RESTRICT_DP_RELEASE_F,
    HVAC_FAN_RESTRICT_HUMID_LATCH,
    HVAC_FAN_RESTRICT_LATCH_SPEC,
    LATCH_SPECS,
    SIGNAL_INDOOR,
    SIGNAL_OUTDOOR,
    humid_air_verdict_from_facts,
    hvac_fan_restrict_humid,
    hvac_fan_restrict_humid_from_facts,
    step_humid_latches,
)
from tests.test_fan_control import _get_service_calls, _make_automation_engine, _thermostat_state
from tests.test_humid_fan_guard_1691 import _rh

ROOT = Path(__file__).resolve().parents[1]
_NOW = datetime(2026, 7, 12, 20, 30, 0)
_AUTOMATION_NOW = "custom_components.climate_advisor.automation.dt_util.now"


# ---------------------------------------------------------------------------
# The pure predicate and its latch
# ---------------------------------------------------------------------------


class TestPredicate:
    def test_constants_are_the_plan_numbers(self):
        assert (HVAC_FAN_RESTRICT_DP_F, HVAC_FAN_RESTRICT_DP_RELEASE_F) == (55.0, 54.0)
        spec = HVAC_FAN_RESTRICT_LATCH_SPEC
        assert (spec.name, spec.signal, spec.on, spec.off) == (HVAC_FAN_RESTRICT_HUMID_LATCH, SIGNAL_INDOOR, 55.0, 54.0)
        assert spec in LATCH_SPECS  # stepped by the coordinator's tick with the other humid latches

    def test_engage_at_55(self):
        assert hvac_fan_restrict_humid(55.0) is True
        assert hvac_fan_restrict_humid(58.0) is True
        assert hvac_fan_restrict_humid(54.9) is False
        assert hvac_fan_restrict_humid(50.0) is False

    def test_hold_inside_the_band_release_at_54(self):
        latched = {HVAC_FAN_RESTRICT_HUMID_LATCH}
        assert hvac_fan_restrict_humid(54.5, latched=latched) is True
        assert hvac_fan_restrict_humid(54.0, latched=latched) is False
        assert hvac_fan_restrict_humid(53.5, latched=latched) is False

    def test_other_latches_do_not_hold_it(self):
        assert hvac_fan_restrict_humid(54.5, latched={HUMID_AIR_CEILING_LATCH, HUMID_AIR_WETTER_LATCH}) is False

    def test_unknown_is_none(self):
        assert hvac_fan_restrict_humid(None) is None
        assert hvac_fan_restrict_humid(None, latched={HVAC_FAN_RESTRICT_HUMID_LATCH}) is None

    def test_from_facts_type_checks(self):
        facts = SimpleNamespace(indoor_dew_point_f=54.5, humid_latches=frozenset({HVAC_FAN_RESTRICT_HUMID_LATCH}))
        assert hvac_fan_restrict_humid_from_facts(facts) == (True, 54.5)
        # a stub's placeholder (MagicMock, NaN, bool) is unknown, never a dry house
        assert hvac_fan_restrict_humid_from_facts(MagicMock()) == (None, None)
        assert hvac_fan_restrict_humid_from_facts(SimpleNamespace(indoor_dew_point_f=float("nan"))) == (None, None)
        assert hvac_fan_restrict_humid_from_facts(SimpleNamespace(indoor_dew_point_f=True)) == (None, None)
        assert hvac_fan_restrict_humid_from_facts(SimpleNamespace()) == (None, None)

    def test_one_facts_reader_for_both_rules(self):
        """H1 and H2 read the facts through the same finite-number reader (review W2C B2): NaN is unknown for both."""
        nan = SimpleNamespace(indoor_dew_point_f=float("nan"), outdoor_dew_point_f=float("nan"), humid_latches=None)
        assert not humid_air_verdict_from_facts(nan, 65.0).blocks
        v = humid_air_verdict_from_facts(nan, 65.0)
        assert (v.indoor_dp_f, v.outdoor_dp_f) == (None, None)
        ok = SimpleNamespace(indoor_dew_point_f=56, outdoor_dew_point_f=67.0, humid_latches=frozenset())
        assert humid_air_verdict_from_facts(ok, 65.0).blocks  # an int still counts


class TestTickHysteresis:
    """The coordinator's 5-minute tick keeps the latch (``step_humid_latches``, the one step it calls)."""

    def _tick(self, indoor, state):
        return step_humid_latches({SIGNAL_INDOOR: indoor, SIGNAL_OUTDOOR: 50.0}, state, ceiling_f=65.0)

    def test_55_on_54_off_across_ticks(self):
        state: dict[str, bool] = {}
        seq = []
        for dp in (54.5, 55.0, 54.5, 54.1, 54.0, 54.5, 55.2):
            state = self._tick(dp, state)
            latched = {k for k, on in state.items() if on}
            seq.append(hvac_fan_restrict_humid(dp, latched=latched))
        assert seq == [False, True, True, True, False, False, True]

    def test_unknown_releases(self):
        state = self._tick(58.0, {})
        assert state[HVAC_FAN_RESTRICT_HUMID_LATCH] is True
        state = self._tick(None, state)
        assert state[HVAC_FAN_RESTRICT_HUMID_LATCH] is False

    def test_guard_off_still_steps_the_restrict_latch(self):
        """The H1 option Off (ceiling None) does not switch H2 off: it is an internal rule of the restrict mode."""
        state = step_humid_latches({SIGNAL_INDOOR: 58.0, SIGNAL_OUTDOOR: 50.0}, {}, ceiling_f=None)
        assert state[HVAC_FAN_RESTRICT_HUMID_LATCH] is True


# ---------------------------------------------------------------------------
# The engine: _activate_fan through _hvac_fan_restriction_verdict
# ---------------------------------------------------------------------------


def _engine(
    *,
    restrict: str = HVAC_FAN_RESTRICT_HEAT,
    fan_mode: str = FAN_MODE_HVAC,
    thermostat: str = "off",
    indoor_dp: float | None,
    cooled_min: float | None = 30.0,
    heated_min: float | None = None,
    latched: frozenset[str] = frozenset(),
):
    config = {CONF_FAN_MODE: fan_mode, CONF_HVAC_FAN_RESTRICT_MODE: restrict}
    if fan_mode == FAN_MODE_WHOLE_HOUSE:
        config[CONF_FAN_ENTITY] = "fan.whf"
    engine = _make_automation_engine(config)
    engine.hass.states.get = MagicMock(return_value=_thermostat_state(thermostat))
    engine._inputs = StaticEngineInputs(
        indoor_f=76.0,
        humidity=_rh(76.0, indoor_dp) if indoor_dp is not None else None,
        humid_latches=latched,
    )
    if cooled_min is not None:
        engine._last_hvac_cooling_active = (_NOW - timedelta(minutes=cooled_min)).isoformat()
    if heated_min is not None:
        engine._last_hvac_heating_active = (_NOW - timedelta(minutes=heated_min)).isoformat()
    return engine


def _activate(engine) -> FanCommandResult:
    with patch(_AUTOMATION_NOW, return_value=_NOW):
        return asyncio.run(engine._activate_fan(reason="test"))


def _blower_on_calls(engine) -> int:
    return sum(1 for c in _get_service_calls(engine, "climate", "set_fan_mode") if c[0][2]["fan_mode"] == "on")


class TestBlowerAfterCooling:
    def test_humid_house_restricted(self, caplog):
        """Control (also refused before #1693) plus the extended reason text."""
        engine = _engine(indoor_dp=58.0)
        with caplog.at_level(logging.INFO):
            assert _activate(engine) == FanCommandResult.SUPPRESSED
        assert _blower_on_calls(engine) == 0
        assert engine._fan_active is False
        msg = next(r.getMessage() for r in caplog.records if "HVAC fan not activated" in r.getMessage())
        assert "cooled within the last 2h, humid indoors (indoor_dew_point_f=58.0" in msg

    def test_dry_house_allowed(self, caplog):
        engine = _engine(indoor_dp=50.0)
        with caplog.at_level(logging.INFO):
            assert _activate(engine) == FanCommandResult.EXECUTED
        assert _blower_on_calls(engine) == 1
        msg = next(r.getMessage() for r in caplog.records if "HVAC fan restriction lifted" in r.getMessage())
        assert "indoor_dew_point_f=50.0" in msg and "release_f=54.0" in msg

    def test_unknown_humidity_is_todays_rule(self, caplog):
        """Control: guards against unknown being read as dry (passes before #1693 too)."""
        engine = _engine(indoor_dp=None)
        with caplog.at_level(logging.INFO):
            assert _activate(engine) == FanCommandResult.SUPPRESSED
        assert _blower_on_calls(engine) == 0
        msg = next(r.getMessage() for r in caplog.records if "HVAC fan not activated" in r.getMessage())
        assert "restricted to heat — HVAC cooled within the last 2h (" in msg  # the unchanged #835 reason
        assert "humid" not in msg

    def test_hysteresis_from_the_facts_latch(self):
        # 54.5 F: restricted only while the tick's latch is on (the decision re-steps it from the facts)
        assert _activate(_engine(indoor_dp=54.5)) == FanCommandResult.EXECUTED
        latched = frozenset({HVAC_FAN_RESTRICT_HUMID_LATCH})
        assert _activate(_engine(indoor_dp=54.5, latched=latched)) == FanCommandResult.SUPPRESSED
        assert _activate(_engine(indoor_dp=53.5, latched=latched)) == FanCommandResult.EXECUTED

    def test_humid_does_not_extend_the_2h_window(self):
        assert _activate(_engine(indoor_dp=62.0, cooled_min=180.0)) == FanCommandResult.EXECUTED

    def test_runtime_refusal_reads_the_same_verdict(self):
        """``_fan_start_runtime_refusal`` (quiet-pause promise, #1225) agrees with ``_activate_fan``."""
        with patch(_AUTOMATION_NOW, return_value=_NOW):
            assert "humid indoors" in (_engine(indoor_dp=58.0)._fan_start_runtime_refusal() or "")
            assert _engine(indoor_dp=50.0)._fan_start_runtime_refusal() is None
            assert "cooled within the last 2h" in (_engine(indoor_dp=None)._fan_start_runtime_refusal() or "")

    def test_one_verdict_per_activation(self):
        """hvac_fan only: no await between the early check and the blower write."""
        engine = _engine(indoor_dp=50.0)
        with patch.object(engine, "_hvac_fan_restriction_verdict", wraps=engine._hvac_fan_restriction_verdict) as spy:
            assert _activate(engine) == FanCommandResult.EXECUTED
        assert spy.call_count == 1


def _both_engine(*, indoor_dp: float | None, cooled_min: float):
    """fan_mode both (WHF + blower), restrict heat, thermostat in cool: the WHF leg switches it off first, and the
    state follows the command (as FakeHass and a polled HA entity do)."""
    engine = _engine(fan_mode=FAN_MODE_BOTH, thermostat="cool", indoor_dp=indoor_dp, cooled_min=cooled_min)
    engine.config[CONF_FAN_ENTITY] = "fan.attic"
    state = _thermostat_state("cool")
    engine.hass.states.get = MagicMock(return_value=state)
    calls = engine.hass.services.async_call

    async def _call(domain, service, data=None, *args, **kwargs):
        if domain == "climate" and service == "set_hvac_mode" and data:
            state.state = data["hvac_mode"]
        return None

    calls.side_effect = _call
    return engine


class TestBothModeJudgedAfterWhfLeg:
    """Review W2C A1/B1: with ``both`` the blower is judged at its write, after the WHF leg turned the AC off."""

    def test_not_cooled_recently_blower_runs(self):
        engine = _both_engine(indoor_dp=None, cooled_min=180.0)
        assert _activate(engine) == FanCommandResult.EXECUTED
        assert len(_get_service_calls(engine, "fan", "turn_on")) == 1
        assert _blower_on_calls(engine) == 1  # a verdict cached before the WHF leg read "cool mode" and sent 0

    def test_cooled_recently_dry_house_blower_runs(self):
        engine = _both_engine(indoor_dp=50.0, cooled_min=30.0)
        assert _activate(engine) == FanCommandResult.EXECUTED
        assert _blower_on_calls(engine) == 1

    def test_cooled_recently_humid_house_blower_restricted_whf_runs(self):
        engine = _both_engine(indoor_dp=58.0, cooled_min=30.0)
        assert _activate(engine) == FanCommandResult.EXECUTED  # the WHF leg ran
        assert len(_get_service_calls(engine, "fan", "turn_on")) == 1
        assert _blower_on_calls(engine) == 0


class TestUnchanged:
    def test_mode_both_restricts_nothing(self):
        for dp in (58.0, 50.0, None):
            engine = _engine(restrict=HVAC_FAN_RESTRICT_BOTH, indoor_dp=dp)
            assert _activate(engine) == FanCommandResult.EXECUTED
            assert _blower_on_calls(engine) == 1

    def test_currently_in_cool_mode_ignores_humidity(self):
        engine = _engine(thermostat="cool", indoor_dp=50.0)
        assert _activate(engine) == FanCommandResult.SUPPRESSED
        with patch(_AUTOMATION_NOW, return_value=_NOW):
            assert engine._hvac_fan_restriction_block_reason() == "restricted to heat — HVAC currently in cool mode"

    def test_heat_side_restriction_ignores_humidity(self):
        """'cool' restriction ("heated within 2h"): heating leaves no condensate, so humidity never changes it."""
        for dp in (58.0, 50.0, None):
            engine = _engine(restrict=HVAC_FAN_RESTRICT_COOL, indoor_dp=dp, cooled_min=None, heated_min=30.0)
            assert _activate(engine) == FanCommandResult.SUPPRESSED
            with patch(_AUTOMATION_NOW, return_value=_NOW):
                assert (
                    engine._hvac_fan_restriction_block_reason() == "restricted to cool — HVAC heated within the last 2h"
                )
            assert _blower_on_calls(engine) == 0

    def test_whole_house_fan_unaffected(self):
        for dp in (58.0, 50.0):
            engine = _engine(fan_mode=FAN_MODE_WHOLE_HOUSE, indoor_dp=dp)
            assert _activate(engine) == FanCommandResult.EXECUTED
            assert len(_get_service_calls(engine, "fan", "turn_on")) == 1


# ---------------------------------------------------------------------------
# Tier A: the real engine and coordinator, headless (pending scenarios)
# ---------------------------------------------------------------------------

_SCENARIOS = sorted((ROOT / "tools" / "simulations" / "pending").glob("humid_fan_restrict_1693_*.json"))


def test_scenarios_exist():
    assert len(_SCENARIOS) == 4


@pytest.mark.parametrize("scenario_path", _SCENARIOS, ids=lambda p: p.stem)
def test_pending_humid_fan_restrict_scenario_passes_production_engine(scenario_path: Path) -> None:
    import sys

    sys.path.insert(0, str(ROOT / "tools"))
    from simulate import run_scenario_production  # noqa: PLC0415

    result = run_scenario_production(scenario_path, state="pending")
    assert not result.get("callback_errors"), result.get("callback_errors")
    real = [x for x in result["assertions"] if not x.get("skipped")]
    assert real and len(real) == len(result["assertions"]), "no assertion of a humid restrict scenario may be skipped"
    assert result["passed"] is True, "; ".join(
        f"@{x['at']} expected={x['expected']!r} actual={x['actual']!r}"
        for x in result["assertions"]
        if not x.get("pass")
    )
