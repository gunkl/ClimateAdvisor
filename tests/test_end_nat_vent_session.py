"""Tests for Issue #931: ``_end_nat_vent_session()``, the sole writer of the
``_natural_vent_active``/``_nat_vent_soft_start`` flag pair in connection with a
fan-stop command.

Background: those two flags used to be hand-cleared unconditionally, immediately
before the paired ``_deactivate_fan()`` call, at 9 separate places in
``automation.py``. 8 of those 9 were genuinely buggy (most never checked the
command's result at all) and are migrated onto ``_end_nat_vent_session()`` by this
fix; the 9th (fan-reconcile "no fan running") is a different situation with no
command in flight to race with, and is deliberately left alone — see that method's
own docstring. When ``_deactivate_fan()`` returned
``RATE_LIMITED_NEW``/``RATE_LIMITED_DUP`` (Issue #641's 300s minimum-toggle-interval
rate limiter deferring the real command), the flags still cleared as if the fan had
actually stopped — silently disarming ``nat_vent_temperature_check()``'s only retry
mechanism (it opens with ``if not self._natural_vent_active: return``) while the fan
kept physically running. This produced a real 2.5h WHF overshoot in production
(Issue #931 investigation).

This is a direct unit test on the extracted method itself — deterministic, no
simulator/virtual clock involved. Since ``_end_nat_vent_session()`` is now the sole
writer, this one parametrized test covers the decision for all 8 migrated call sites
at once, independent of any specific thermal/timing scenario.
"""

from __future__ import annotations

import re
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from custom_components.climate_advisor.automation import AutomationEngine, FanCommandResult
from custom_components.climate_advisor.const import (
    CONF_FAN_ENTITY,
    CONF_FAN_MODE,
    FAN_MODE_WHOLE_HOUSE,
)

_AUTOMATION_PY = Path(__file__).resolve().parent.parent / "custom_components" / "climate_advisor" / "automation.py"


def _consume_coroutine(coro):
    coro.close()


def _make_engine() -> AutomationEngine:
    hass = MagicMock()
    hass.services = MagicMock()
    hass.services.async_call = AsyncMock()
    hass.async_create_task = MagicMock(side_effect=_consume_coroutine)
    hass.states = MagicMock()

    config = {
        "comfort_heat": 68.0,
        "comfort_cool": 74.0,
        "setback_heat": 60,
        "setback_cool": 80,
        "notify_service": "notify.notify",
        CONF_FAN_MODE: FAN_MODE_WHOLE_HOUSE,
        CONF_FAN_ENTITY: "fan.attic",
    }
    engine = AutomationEngine(
        hass=hass,
        climate_entity="climate.thermostat",
        weather_entity="weather.forecast_home",
        door_window_sensors=["binary_sensor.front_door"],
        notify_service=config["notify_service"],
        config=config,
    )
    return engine


class TestEndNatVentSessionClearsOnRealResults:
    """Any result other than a rate-limit deferral means the command actually
    reached the fan — the session really did end, so both flags clear."""

    @pytest.mark.parametrize(
        "result",
        [
            FanCommandResult.EXECUTED,
            FanCommandResult.ALREADY_IN_STATE,
            FanCommandResult.OVERRIDDEN,
            FanCommandResult.DISABLED,
            FanCommandResult.SUPPRESSED,
        ],
    )
    def test_clears_both_flags(self, result: FanCommandResult) -> None:
        engine = _make_engine()
        engine._natural_vent_active = True
        engine._nat_vent_soft_start = True

        engine._end_nat_vent_session(result)

        assert engine._natural_vent_active is False
        assert engine._nat_vent_soft_start is False


class TestEndNatVentSessionPreservesOnRateLimit:
    """RATE_LIMITED_NEW/RATE_LIMITED_DUP means the fan-stop command never actually
    reached the fan — clearing the flags anyway would strand a physically-running
    fan with nothing left to retry stopping it (the exact production defect)."""

    @pytest.mark.parametrize(
        "result",
        [FanCommandResult.RATE_LIMITED_NEW, FanCommandResult.RATE_LIMITED_DUP],
    )
    def test_leaves_both_flags_set(self, result: FanCommandResult) -> None:
        engine = _make_engine()
        engine._natural_vent_active = True
        engine._nat_vent_soft_start = True

        engine._end_nat_vent_session(result)

        assert engine._natural_vent_active is True
        assert engine._nat_vent_soft_start is True

    @pytest.mark.parametrize(
        "result",
        [FanCommandResult.RATE_LIMITED_NEW, FanCommandResult.RATE_LIMITED_DUP],
    )
    def test_leaves_already_false_flags_false(self, result: FanCommandResult) -> None:
        # A rate-limited result should never itself flip the flags on — only ever
        # decide whether to clear an already-True state.
        engine = _make_engine()
        engine._natural_vent_active = False
        engine._nat_vent_soft_start = False

        engine._end_nat_vent_session(result)

        assert engine._natural_vent_active is False
        assert engine._nat_vent_soft_start is False


class TestEndNatVentSessionIsSoleWriter:
    """Issue #931 1b: a grep-based guard against a *future* call site reintroducing
    the raw hand-written assignment pattern this fix just consolidated. A code-review
    read-through won't reliably catch a 10th hand-rolled copy; this test fails the
    moment a second raw assignment appears anywhere in automation.py.

    Exactly 3 raw ``self._natural_vent_active = False`` assignments are expected to
    remain, and no more:

    1. Inside ``_end_nat_vent_session()`` itself — the sole writer this fix creates.
    2. The fan-reconcile "no fan running" site — clears the flag to match reality
       when no fan command is in flight to race with (not this bug's shape at all;
       see the Issue #931 plan's inventory table).
    3. ``_clear_fan_flags_and_start_grace()``'s ``preserve_nat_vent_session`` gate —
       a general-purpose flag-reset helper whose callers explicitly decide whether to
       preserve the nat-vent session; it is not tied to a specific fan command's
       result the way the other 8 (now-migrated) sites were.
    """

    _RAW_ASSIGNMENT = re.compile(r"^\s*self\._natural_vent_active = False\s*$", re.MULTILINE)

    def test_exactly_three_raw_assignments_remain(self) -> None:
        source = _AUTOMATION_PY.read_text(encoding="utf-8")
        matches = self._RAW_ASSIGNMENT.findall(source)
        assert len(matches) == 3, (
            "Expected exactly 3 raw 'self._natural_vent_active = False' assignments "
            f"(sole writer + 2 excluded sites), found {len(matches)}. A new call site "
            "must route through _end_nat_vent_session() instead of hand-writing this "
            "assignment — see Issue #931."
        )

    def test_sole_writer_method_contains_one_of_them(self) -> None:
        source = _AUTOMATION_PY.read_text(encoding="utf-8")
        method_start = source.index("def _end_nat_vent_session(")
        # Bound the slice at the start of the next same-indentation method definition
        # (rather than a fixed character count) so this stays correct regardless of how
        # long _end_nat_vent_session()'s own docstring/body grows.
        next_method = re.search(r"\n    (?:async )?def ", source[method_start + 1 :])
        method_end = method_start + 1 + next_method.start() if next_method else len(source)
        method_body = source[method_start:method_end]
        assert self._RAW_ASSIGNMENT.search(method_body), (
            "_end_nat_vent_session() itself no longer contains the raw assignment — has the method been restructured?"
        )
