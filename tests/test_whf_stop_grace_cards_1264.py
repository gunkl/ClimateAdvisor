"""A sealed-house WHF stop ends the occupant's grace AND the cards show it at once (Issue #1264).

Occupant story: she turns the whole-house fan on with every window closed; Climate Advisor stops it ~10 s later
(household rule 1). The engine ended the fan override and its grace, but the Status card kept reading
"grace period (manual) — WHF override — 3h" for another 15-26 s, until the fan power entity's lagging off reading
happened to refresh the coordinator. The cards read ``coordinator.data``, so ending a grace must push them
(display only).

Second gap on the same stop: when the sealed decision stops the fan and it already reads off
(``ALREADY_IN_STATE``), the override and grace were left running for the rest of the timer.
"""

from __future__ import annotations

from unittest.mock import MagicMock

from custom_components.climate_advisor.automation import FanCommandResult
from tests.test_whf_safety_enforcer import _engine, _fan_off, _run, clock  # noqa: F401  (clock is a fixture)


def _occupant_pressed_the_fan_on(engine) -> None:
    engine._fan_override_active = True
    engine._fan_override_time = engine._whf_press_time = "2026-10-30T21:32:00+00:00"
    assert engine._start_grace_period_action("manual", trigger="fan_manual_override")
    engine._grace_active = True  # the FSM flag-set runs in the coordinator; same convention as the #1204 tests
    engine._grace_protects_override = True


class TestSealedStopPushesTheCards:
    def test_executed_stop_ends_the_grace_and_pushes_the_cards(self, clock):  # noqa: F811
        engine, _ = _engine()
        _occupant_pressed_the_fan_on(engine)
        engine._push_card_status_callback = push = MagicMock()

        assert _run(engine, "settle") is FanCommandResult.EXECUTED

        assert len(_fan_off(engine)) == 1
        assert engine._grace_active is False, "the occupant's fan grace outlived the safety stop"
        assert engine._fan_override_active is False
        push.assert_called()

    def test_the_cards_are_pushed_only_once_the_ending_is_complete(self, clock):  # noqa: F811
        """A push made mid-ending would render the grace as still active and show it until the next refresh."""
        engine, _ = _engine()
        _occupant_pressed_the_fan_on(engine)
        seen: list[tuple[bool, bool]] = []
        engine._push_card_status_callback = lambda: seen.append((engine._grace_active, engine._fan_override_active))

        engine._end_fan_override_and_grace(reason="whf_safety_stop")

        assert seen and all(s == (False, False) for s in seen), seen

    def test_nothing_running_no_push(self, clock):  # noqa: F811
        engine, _ = _engine()
        engine._push_card_status_callback = push = MagicMock()
        engine._end_fan_override_and_grace(reason="whf_safety_stop")
        push.assert_not_called()

    def test_leaving_home_ending_a_grace_pushes_the_cards(self, clock):  # noqa: F811
        """Mirror (production 2026-11-02 10:23): the away setback ends the occupant's change and its grace (#1222)."""
        engine, _ = _engine()
        engine._grace_active = True
        engine._grace_protects_override = True
        engine._last_grace_trigger = "override_confirmed"
        engine._push_card_status_callback = push = MagicMock()

        engine._end_grace_left_protecting_nothing("occupancy_away")

        assert engine._grace_active is False
        push.assert_called_once()


class TestSealedStopOfAFanThatAlreadyReadsOff:
    def test_already_off_still_ends_the_override_and_grace(self, clock):  # noqa: F811
        engine, _ = _engine()
        _occupant_pressed_the_fan_on(engine)
        engine._push_card_status_callback = push = MagicMock()
        # The snapshot saw the fan on (sealed decision: stop) but the physical read is already off by the command.
        engine._get_fan_physical_state_callback = lambda: False

        assert _run(engine, "settle") is FanCommandResult.ALREADY_IN_STATE

        assert not _fan_off(engine), "nothing to command: the fan already reads off"
        assert engine._grace_active is False, "the grace ran on for the rest of the timer over a fan that was off"
        assert engine._fan_override_active is False
        push.assert_called()
        assert not getattr(engine, "_whf_safety_stop_times", []), (
            "no command went out: not a stop for the oscillation alert"
        )
