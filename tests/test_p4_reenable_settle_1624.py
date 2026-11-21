"""Issue #1624: switching automation back on only has to settle the house in a good state (owner rule 2026-11-20).

Occupant story: with automation off and a window open, you switch the whole-house fan on and set the heat or AC.
When you switch automation back on, Climate Advisor stops one of them, because the fan and the heat/AC never run
together. Turning the heat/AC Off then is the house settling, not Climate Advisor undoing your change, so the
"your change is respected" promise (P4) must not report it. Every other reversal still must.
"""

from __future__ import annotations

from custom_components.climate_advisor.invariant_watchdog import NUMBERS
from tests.test_promise_checks import at, cmd, events, hits_for, make_input, rd
from tools.sim_harness.generator import run_day

P4 = "override_respected"
NIGHTLY_DAY_1624 = 26009000121  # nightly 2026-11-20, master seed 26009


def _marks():
    return events(
        (0, "override_detected", {"detected_mode": "heat"}),
        (0, "grace_started", {"source": "manual", "trigger": "manual", "duration_seconds": 1800}),
    )


def _readings(*, fan_while_off: bool = True, override_while_off: bool = True, fan_at_reenable: bool = True):
    """Automation off until minute 8; the occupant's fan (minute -2) and heat (minute 0); re-enable at minute 8."""
    heating = {"hvac_mode": "heat", "hvac_action": "heating", "setpoint_f": 70.0}
    return [
        rd(-3, hvac_mode="cool", automation_enabled=False),
        rd(-2, hvac_mode="cool", fan_on=fan_while_off, automation_enabled=False),
        rd(0, fan_on=fan_while_off, automation_enabled=not override_while_off, **heating),
        rd(4, fan_on=fan_while_off, automation_enabled=False, **heating),
        rd(8, hvac_mode="off", hvac_action="off", setpoint_f=70.0, fan_on=fan_at_reenable),
    ]


def _input(*, commands=None, now=None, **readings_kw):
    commands = commands if commands is not None else [cmd(8, mode="off")]
    return make_input(_readings(**readings_kw), commands, _marks(), now=now or at(40))


class TestReEnableSettle:
    def test_the_rule_2_off_at_re_enable_with_the_occupants_fan_running_is_not_a_miss(self):
        """The #1624 shape: fan and heat both set while automation was off; the re-enable turns the heat Off."""
        assert hits_for(P4, _input()) == []

    def test_control_the_same_off_with_no_fan_running_is_still_a_miss(self):
        inp = _input(fan_while_off=False, fan_at_reenable=False)
        assert hits_for(P4, inp), "an HVAC-off at re-enable with no occupant fan must still be reported"

    def test_control_a_fan_that_came_on_only_after_the_re_enable_does_not_excuse_it(self):
        """Climate Advisor's own fan start at the re-enable never excuses the off (a fan the occupant switched on
        then would be a newer occupant action, which P4 already handles on its own)."""
        commands = [cmd(8, target="fan", service="turn_on", fan_on=True, mode=None), cmd(8, mode="off")]
        inp = _input(commands=commands, fan_while_off=False, fan_at_reenable=True)
        assert hits_for(P4, inp), "only a fan the occupant had running while automation was off counts"

    def test_control_an_override_set_while_automation_was_on_is_not_covered(self):
        inp = _input(override_while_off=False)
        assert hits_for(P4, inp), "the exception covers changes made while automation was off"

    def test_control_an_off_well_after_the_re_enable_is_still_a_miss(self):
        late = 8 + (NUMBERS.P4_REENABLE_SETTLE_S + 60.0) / 60.0
        heating = {"hvac_mode": "heat", "hvac_action": "heating", "setpoint_f": 70.0}
        rs = [*_readings()[:-1], rd(8, fan_on=True, **heating), rd(late, fan_on=True, hvac_mode="off", setpoint_f=70.0)]
        inp = make_input(rs, [cmd(late, mode="off")], _marks(), now=at(40))
        assert hits_for(P4, inp), "only the settling at the re-enable is excused"

    def test_control_a_setpoint_reversal_at_re_enable_is_still_a_miss(self):
        inp = _input(commands=[cmd(8, service="set_temperature", mode=None, setpoint_f=62.0)])
        assert hits_for(P4, inp), "the exception covers the rule-2 Off only"


class TestEvidenceAgedOut:
    """The readings before the override leave the 3-hour window minutes before the command does."""

    @staticmethod
    def _aged(now_min: float):
        rs = [r for r in _readings() if r.ts >= at(8)]  # only the re-enable reading is left
        return make_input(rs, [cmd(8, mode="off")], _marks(), now=at(now_min))

    def test_a_command_judged_earlier_on_full_evidence_is_not_reported_again(self):
        assert hits_for(P4, self._aged(8 + NUMBERS.P4_REJUDGE_AFTER_MIN + 1)) == []

    def test_a_command_never_judged_on_full_evidence_is_not_excused(self):
        assert hits_for(P4, self._aged(9)), "partial evidence on a first look must not excuse a reversal"


def test_nightly_day_1624_settles_without_a_promise_miss() -> None:
    """End to end: the nightly day that failed, through the real coordinator and the household promises. The house
    settles (one of fan and AC stopped) and nothing is reported as overriding the occupant."""
    report = run_day(NIGHTLY_DAY_1624, "coordinator").report
    assert not [(h.ts, h.detail) for h in report.hard if h.promise == P4]
    assert not [(h.ts, h.detail) for h in report.hard if h.promise == "ac_whf_mutex"]
