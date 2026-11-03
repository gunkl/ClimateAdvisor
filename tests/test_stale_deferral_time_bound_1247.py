"""Issue #1247: a toggle-floor deferral record far past its ``applies_at`` must not be reported as applied.

Occupant view: the min-runtime cycle's start was deferred by the 5-minute fan toggle floor; CA retries it only an hour
later. The "activate" record stayed set, so that retry (or any unrelated fan start) logged "5-minute floor expired --
applying deferred activation" up to an hour late -- a misleading log/Activity line. No fan or HVAC decision changes.

Both supported fan modes (``fan_mode: both`` is unsupported, #1175). The driver is the real engine through the
``_Home`` harness of the #1205/#1212 files.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import timedelta

import pytest

from custom_components.climate_advisor.automation import FanCommandResult
from custom_components.climate_advisor.fan_toggle_rate_limit import (
    FAN_DEFERRAL_STALE_AFTER_S,
    FanToggleRateLimitInputs,
    FanToggleRateLimitOutcome,
    decide_fan_toggle_rate_limit,
    deferral_record_is_stale,
)
from tests.test_stale_fan_deferral_1212 import _run_fan_then_defer_stop, clock  # noqa: F401  (fixture)
from tests.test_stale_fan_marker_1205 import _MODES, _Home

_LOG = "custom_components.climate_advisor.automation"
_ACT = "applying deferred activation"
_EXIT = "applying deferred exit"
_RETRY_S = 3600.0  # _MIN_RUNTIME_REFUSED_RETRY_S


def _defer_min_runtime_start(home: _Home):
    """CA toggled the fan 30 s ago; the min-runtime cycle start is deferred by the floor."""
    engine = home.engine
    engine._fan_toggle_command_time = home.clock.now - timedelta(seconds=30)
    result = asyncio.run(engine._activate_fan(reason="min_runtime_cycle"))
    home.drain()
    assert result is FanCommandResult.RATE_LIMITED_NEW, f"precondition: start deferred, got {result}"
    assert engine._fan_rate_limited_direction == "activate"
    return engine._fan_rate_limited_until


@pytest.mark.parametrize("fan_mode", _MODES)
def test_late_min_runtime_retry_is_not_logged_as_the_deferred_start(clock, caplog, fan_mode):  # noqa: F811
    home = _Home(fan_mode, clock)
    _defer_min_runtime_start(home)
    home.clock.advance(_RETRY_S)
    caplog.set_level(logging.INFO, logger=_LOG)

    result = asyncio.run(home.engine._activate_fan(reason="min_runtime_cycle"))
    home.drain()

    assert result is FanCommandResult.EXECUTED
    assert _ACT not in caplog.text, "an hour-late start was logged as the deferred one"
    assert home.engine._fan_rate_limited_until is None and home.engine._fan_rate_limited_direction is None


@pytest.mark.parametrize("fan_mode", _MODES)
def test_on_time_deferred_start_is_still_reported_as_applied(clock, caplog, fan_mode):  # noqa: F811
    home = _Home(fan_mode, clock)
    applies_at = _defer_min_runtime_start(home)
    home.clock.advance((applies_at - home.clock.now).total_seconds() + 1.0)
    caplog.set_level(logging.INFO, logger=_LOG)

    result = asyncio.run(home.engine._activate_fan(reason="min_runtime_cycle"))
    home.drain()

    assert result is FanCommandResult.EXECUTED
    assert _ACT in caplog.text
    assert home.engine._fan_rate_limited_until is None


@pytest.mark.parametrize("fan_mode", _MODES)
def test_start_within_one_coordinator_cycle_still_counts(clock, caplog, fan_mode):  # noqa: F811
    """A paused-reactivation retry on the next 30-minute cycle is still the deferred start (decision unchanged)."""
    home = _Home(fan_mode, clock)
    applies_at = _defer_min_runtime_start(home)
    home.clock.advance((applies_at - home.clock.now).total_seconds() + 1800.0)
    caplog.set_level(logging.INFO, logger=_LOG)

    asyncio.run(home.engine._activate_fan(reason="cycle retry"))
    home.drain()

    assert _ACT in caplog.text


@pytest.mark.parametrize("fan_mode", _MODES)
def test_stale_record_drops_the_pending_manual_origin_fact_and_logs(clock, caplog, fan_mode):  # noqa: F811
    home = _Home(fan_mode, clock)
    applies_at = _defer_min_runtime_start(home)
    home.engine._raise_fan_manual_origin_pending("test", "reactivation_rate_limited")
    assert home.engine._fan_manual_origin_pending is not None
    home.clock.advance(_RETRY_S)
    caplog.set_level(logging.INFO, logger=_LOG)

    asyncio.run(home.engine._activate_fan(reason="unrelated start"))
    home.drain()

    assert home.engine._fan_manual_origin_pending is None
    assert "stale_past_applies_at:activate" in caplog.text
    assert applies_at is not None


@pytest.mark.parametrize("fan_mode", _MODES)
def test_late_deactivate_is_not_logged_as_the_deferred_exit(clock, caplog, fan_mode):  # noqa: F811
    home = _Home(fan_mode, clock)
    _run_fan_then_defer_stop(home)
    home.clock.advance(_RETRY_S)
    caplog.set_level(logging.INFO, logger=_LOG)

    result = asyncio.run(home.engine._deactivate_fan(reason="unrelated stop"))
    home.drain()

    assert result is FanCommandResult.EXECUTED
    assert _EXIT not in caplog.text
    assert home.engine._fan_rate_limited_until is None


@pytest.mark.parametrize("fan_mode", _MODES)
def test_on_time_deferred_stop_is_still_reported_as_applied(clock, caplog, fan_mode):  # noqa: F811
    home = _Home(fan_mode, clock)
    _run_fan_then_defer_stop(home)
    home.clock.advance(300.0)
    caplog.set_level(logging.INFO, logger=_LOG)

    asyncio.run(home.engine._deactivate_fan(reason="retry"))
    home.drain()

    assert _EXIT in caplog.text


def test_dup_comparison_ignores_a_stale_record(monkeypatch):
    """The DUP match needs a record equal to the fresh applies_at, and the DUP branch is only reached while
    ``now < applies_at`` -- so a stale record there can only come from a forged clock. Forge it (``instant_after``
    returns the hour-old record) to exercise the guard itself: it must not call that record a duplicate."""
    from datetime import UTC, datetime

    from custom_components.climate_advisor import fan_toggle_rate_limit as frl

    now = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)
    stale = now - timedelta(seconds=FAN_DEFERRAL_STALE_AFTER_S + 60)
    monkeypatch.setattr(frl, "instant_after", lambda _s, *, now=None: stale)
    inputs = FanToggleRateLimitInputs(
        last_toggle_command_time=now - timedelta(seconds=30),
        action="activate",
        rate_limited_until=stale,
        rate_limited_direction="activate",
        min_interval_s=300.0,
        now=now,
    )
    assert decide_fan_toggle_rate_limit(inputs) == (FanToggleRateLimitOutcome.DEFER_NEW, stale)
    # Control: the same forged record, not yet stale, is still a duplicate.
    fresh = now + timedelta(seconds=270)
    monkeypatch.setattr(frl, "instant_after", lambda _s, *, now=None: fresh)
    inputs = FanToggleRateLimitInputs(
        last_toggle_command_time=now - timedelta(seconds=30),
        action="activate",
        rate_limited_until=fresh,
        rate_limited_direction="activate",
        min_interval_s=300.0,
        now=now,
    )
    assert decide_fan_toggle_rate_limit(inputs) == (FanToggleRateLimitOutcome.DEFER_DUPLICATE, fresh)


def test_staleness_bound_separates_on_time_retries_from_the_hourly_retry():
    assert 1801 < FAN_DEFERRAL_STALE_AFTER_S < _RETRY_S - 300
    from datetime import UTC, datetime

    t = datetime(2026, 1, 1, tzinfo=UTC)
    assert not deferral_record_is_stale(None, t)
    assert not deferral_record_is_stale(t, t - timedelta(seconds=5))
    assert not deferral_record_is_stale(t, t + timedelta(seconds=1))
    assert deferral_record_is_stale(t, t + timedelta(seconds=FAN_DEFERRAL_STALE_AFTER_S + 1))
