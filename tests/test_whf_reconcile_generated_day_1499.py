"""Issues #1499/#1500: the nightly's generated day 13007000023 (run 42652897507, P8 ``fan_cards``).

Command-only whole-house fan. Climate Advisor turns the fan off when the door closes (21:32 UTC), the occupant turns
cooling on and sets the thermostat blower "on", then switches automation off. At 22:00 the AC reaches its setpoint and
the thermostat reports cooling -> fan. The fan reconcile read that blower as the whole-house fan (#1500) and, in
observe-only, left its temporary "CA owns the fan" flag set (#1499): the fan card said "active" for an hour, a false
"fan running with the house closed up" alert went out, and re-enabling sent a turn-off to the already-off fan.
With automation on, the same day sent a needless turn-off at 22:00 and logged a nat-vent exit.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from unittest.mock import patch

from tools.sim_harness import generator
from tools.sim_harness import surfaces as S

DAY = 13007000023
_DOOR_CLOSED = datetime(2026, 5, 12, 21, 32, 30, tzinfo=UTC)  # after CA's own turn-off when the door closed
_REENABLED = datetime(2026, 5, 12, 22, 59, tzinfo=UTC)  # automation back on (the command-only re-assert follows, #361)
_FALSE_ROWS = ("nat_vent_reconcile_exit", "whf_sealed_observe_notified", "fan_deactivated")


def _run(plan: generator.DayPlan) -> tuple[S.SurfaceProbe, Any]:
    holder: dict[str, Any] = {}
    real_session = generator.make_session

    def _probe(**kw: Any) -> S.SurfaceProbe:
        holder["probe"] = S.SurfaceProbe(sample_every=S.GENERATED_SAMPLE_EVERY, all_events=False, **kw)
        return holder["probe"]

    def _session(*a: Any, **k: Any) -> Any:
        holder["session"] = real_session(*a, **k)
        return holder["session"]

    with patch.object(generator, "PromiseProbe", _probe), patch.object(generator, "make_session", _session):
        generator.run_plan(plan)
    return holder["probe"], holder["session"]


def _after_door_closed(session: Any, *, until: datetime | None = None) -> tuple[list, list]:
    """Fan commands (before ``until``) and false fan rows (all day) after CA's own turn-off at the door close."""
    fan_calls = [
        a
        for a in session.fake_hass.action_log
        if a.get("domain") == "fan" and a["ts"] > _DOOR_CLOSED and (until is None or a["ts"] < until)
    ]
    rows = [
        e["type"]
        for e in session.coordinator._event_log
        if e.get("type") in _FALSE_ROWS and datetime.fromisoformat(e["time"]) > _DOOR_CLOSED
    ]
    return fan_calls, rows


def test_the_nightly_day_shows_the_fan_card_off_and_sends_no_false_alert() -> None:
    probe, session = _run(generator.build_plan(DAY, "coordinator"))
    assert not [S.format_divergence(d) for d in probe.divergences if d.surface == "fan_cards"]
    fan_calls, rows = _after_door_closed(session, until=_REENABLED)
    assert not fan_calls, "observe-only sent a fan command"
    assert not rows, f"rows for a whole-house fan run that never happened: {rows}"


def test_same_day_with_automation_on_sends_no_needless_fan_turn_off() -> None:
    plan = generator.build_plan(DAY, "coordinator")
    plan.timed[:] = [e for e in plan.timed if e[1] != "automation_toggle"]
    _probe, session = _run(plan)
    fan_calls, rows = _after_door_closed(session)
    assert not fan_calls, "the thermostat blower was read as the whole-house fan and 'stopped'"
    assert not rows, f"rows for a whole-house fan run that never happened: {rows}"
