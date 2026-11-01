"""Issue #1135: a re-armed setpoint-check / fan-verify timer cancels the handle it replaces.

``cleanup()`` cancels only the LATEST handle in each slot, so a re-arm that overwrote a live handle left a
ghost timer that woke on a dead engine after a settings save or zone reload (HA "Lingering timer").

Real ``AutomationEngine``; ``async_call_later`` is replaced by a recorder that hands out one cancel mock per
armed timer, so a test can see exactly which handles were cancelled. One test per re-arm site (5 sites), each
driven through the real method that arms it.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock, patch

import pytest

from custom_components.climate_advisor.const import CONF_FAN_ENTITY, CONF_FAN_MODE, FAN_MODE_WHOLE_HOUSE
from tests.test_restore_resolved_band import _make_engine, _run

_AUTOMATION = "custom_components.climate_advisor.automation"


class _Armed:
    """Records every ``async_call_later``; each armed timer gets its own cancel mock."""

    def __init__(self) -> None:
        self.timers: list[tuple[float, object, MagicMock]] = []
        # Own clock: other test modules replace ``dt_util.now`` process-wide with a frozen value, which would
        # make the fan's minimum-runtime floor depend on test order.
        self.now = datetime(2026, 10, 28, 14, 0, 0, tzinfo=UTC)

    def __call__(self, _hass, delay, fn):
        cancel = MagicMock(name=f"cancel_{delay}")
        self.timers.append((delay, fn, cancel))
        return cancel

    def of(self, delay: float) -> list[tuple[float, object, MagicMock]]:
        return [t for t in self.timers if t[0] == delay]


@pytest.fixture
def armed():
    rec = _Armed()
    with (
        patch(f"{_AUTOMATION}.async_call_later", side_effect=rec),
        patch(f"{_AUTOMATION}.callback", side_effect=lambda fn: fn),
        patch(f"{_AUTOMATION}.dt_util.now", side_effect=lambda: rec.now),
        patch(f"{_AUTOMATION}.dt_util.parse_datetime", side_effect=datetime.fromisoformat),
    ):
        yield rec


def _engine(**cfg):
    engine = _make_engine(indoor=72.0, mode="heat", **cfg)
    tasks: list = []
    engine.hass.async_create_task = lambda coro: tasks.append(coro)
    engine._test_tasks = tasks
    return engine


def _fan_engine():
    return _engine(**{CONF_FAN_MODE: FAN_MODE_WHOLE_HOUSE, CONF_FAN_ENTITY: "switch.whf"})


def test_two_set_temperatures_in_one_tick_cancel_the_first_check(armed):
    """The golden's shape: one apply_classification issues _set_temperature twice at the same instant."""
    engine = _engine()

    _run(engine._set_temperature(70.0, reason="first", mode="heat"))
    _run(engine._set_temperature(71.0, reason="second", mode="heat"))

    checks = armed.of(10)
    assert len(checks) == 2
    checks[0][2].assert_called_once()
    checks[1][2].assert_not_called()
    assert engine._setpoint_retry_cancel is checks[1][2]


def _reject_the_check(engine, armed):
    """Write a setpoint, make the thermostat report something else, run the 10 s check."""
    _run(engine._set_temperature(70.0, reason="write", mode="heat"))
    engine._test_state.attributes["temperature"] = 55.0  # thermostat did not take it
    _, check_fn, _ = armed.of(10)[-1]
    check_fn(None)
    _run(engine._test_tasks.pop())  # _check_single_setpoint_accepted


def test_the_15_minute_retry_cancels_the_check_handle_it_replaces(armed):
    engine = _engine()

    _reject_the_check(engine, armed)

    check_cancel = armed.of(10)[-1][2]
    retry = armed.of(900)
    assert len(retry) == 1
    check_cancel.assert_called_once()
    assert engine._setpoint_retry_cancel is retry[0][2]


def test_the_30s_real_target_cancels_the_retry_handle_it_replaces(armed):
    engine = _engine()
    engine._setpoint_reject_streak = 1  # the check below makes it 2 -> nudge first, real target 30 s later
    _reject_the_check(engine, armed)
    _, retry_fn, retry_cancel = armed.of(900)[-1]

    retry_fn(None)
    _run(engine._test_tasks.pop())  # _retry_callback: nudge, then arm the 30 s real target

    target = armed.of(30)
    assert len(target) == 1
    retry_cancel.assert_called_once()
    assert engine._setpoint_retry_cancel is target[0][2]


def test_a_newer_write_during_the_nudge_keeps_its_check_and_skips_the_stale_30s_arm(armed):
    """The 30 s arm must not cancel the check of a write that superseded this retry while it awaited."""
    engine = _engine()
    engine._setpoint_reject_streak = 1
    _reject_the_check(engine, armed)
    _, retry_fn, _ = armed.of(900)[-1]

    retry_fn(None)
    retry = engine._test_tasks.pop()
    real_call = engine._call_climate

    fired = []

    async def _call_then_newer_write(*a, **kw):
        result = await real_call(*a, **kw)
        if not fired:  # one-shot: the newer write itself goes through _call_climate too
            fired.append(True)
            await engine._set_temperature(66.0, reason="newer", mode="heat")
        return result

    engine._call_climate = _call_then_newer_write
    _run(retry)

    assert armed.of(30) == [], "a superseded retry must not arm the real-target follow-up"
    newest_check = armed.of(10)[-1][2]
    newest_check.assert_not_called()
    assert engine._setpoint_retry_cancel is newest_check


def test_fan_on_verify_cancels_the_previous_handle(armed):
    engine = _fan_engine()
    stale = MagicMock(name="stale_fan_on_verify")
    engine._fan_on_verify_cancel = stale

    _run(engine._activate_fan(reason="test"))

    stale.assert_called_once()
    verify = armed.of(30.0)
    assert len(verify) == 1
    assert engine._fan_on_verify_cancel is verify[0][2]


def test_fan_off_verify_cancels_the_previous_handle(armed):
    engine = _fan_engine()
    _run(engine._activate_fan(reason="test"))
    armed.now += timedelta(minutes=20)  # past the fan's minimum-runtime floor
    stale = MagicMock(name="stale_fan_off_verify")
    engine._fan_off_verify_cancel = stale

    _run(engine._deactivate_fan(reason="test"))

    stale.assert_called_once()
    verify = armed.of(30.0)
    assert len(verify) == 2  # one on, one off
    assert engine._fan_off_verify_cancel is verify[1][2]


def test_cleanup_after_a_rearm_leaves_no_live_handle(armed):
    """End to end: every handle armed through the two-write shape is cancelled by rearm or by cleanup()."""
    engine = _engine()
    _run(engine._set_temperature(70.0, reason="first", mode="heat"))
    _run(engine._set_temperature(71.0, reason="second", mode="heat"))

    engine.cleanup()

    for delay, _fn, cancel in armed.of(10):
        assert cancel.call_count == 1, f"{delay}s check handle was left live"
