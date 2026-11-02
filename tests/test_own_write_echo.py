"""Issue #1232: the pure echo-recognition rule and the engine's bounded memory of its own setpoint writes."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from custom_components.climate_advisor import automation as automation_module
from custom_components.climate_advisor.automation import AutomationEngine, ClimateCallOutcome
from custom_components.climate_advisor.const import SETPOINT_ECHO_MEMORY_MAX, SETPOINT_ECHO_WINDOW_S
from custom_components.climate_advisor.own_write_echo import (
    OwnSetpointWrite,
    context_id_matches,
    decide_setpoint_echo,
    default_step,
    prune_own_writes,
    resolve_step,
    values_match,
)

T0 = datetime(2026, 11, 2, 1, 0, tzinfo=UTC)
CE = "climate.t"


def _w(attr="temperature", value=72.0, age=0.0, ctx="c1", entity=CE):
    return OwnSetpointWrite(entity, attr, value, T0 - timedelta(seconds=age), ctx)


def _decide(writes, observed, *, step=1.0, ctx=None, parent=None, user=None):
    return decide_setpoint_echo(
        writes=writes,
        entity_id=CE,
        observed=observed,
        now=T0,
        step=step,
        event_context_id=ctx,
        event_context_parent_id=parent,
        event_context_user_id=user,
    )


# --- context predicate (shared with the #482 fan check) ----------------------------------------------------------


def test_context_predicate():
    assert context_id_matches("a", "a", None)
    assert context_id_matches("a", "x", "a")  # parent id
    assert not context_id_matches("a", "x", "y")
    assert not context_id_matches(None, None, None)  # None never matches None
    assert not context_id_matches("a", None, None)


# --- rounding ----------------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("recorded", "observed", "step", "expected"),
    [
        (72.0, 72.0, 1.0, True),
        (72.0, 73.0, 1.0, False),  # on-grid: a one-step tweak is the occupant
        (72.4, 72.0, 1.0, True),  # off-grid: either neighbour is the thermostat's rounding
        (72.4, 73.0, 1.0, True),
        (72.4, 74.0, 1.0, False),
        (22.2222, 22.0, 0.5, True),
        (22.2222, 22.5, 0.5, True),
        (22.2222, 23.0, 0.5, False),
        (22.2222, 21.5, 0.5, False),
        (22.5, 22.5, 0.5, True),
        (22.5, 23.0, 0.5, False),
        (22.5, 22.6, 0.5, False),  # step assumed wrong (a 0.1 C dial): an on-grid write still has to come back exactly
        (72.0, 72.4, 1.0, False),
        (22.2778, 22.3, 0.1, True),
        (22.2778, 22.4, 0.1, False),
        (float("nan"), 22.0, 0.5, False),
        (22.0, float("inf"), 0.5, False),
        (22.0, 22.0, 0.0, False),
    ],
)
def test_values_match(recorded, observed, step, expected):
    assert values_match(recorded, observed, step) is expected


def test_resolve_step():
    assert resolve_step(0.5, "celsius") == 0.5
    assert resolve_step(None, "celsius") == default_step("celsius") == 0.5
    assert resolve_step("junk", "fahrenheit") == 1.0
    assert resolve_step(-1, "fahrenheit") == 1.0
    assert resolve_step(0, "celsius") == 0.5
    assert resolve_step(float("nan"), "celsius") == 0.5


# --- decide_setpoint_echo ------------------------------------------------------------------------------------------


def test_value_inside_window_is_an_echo_and_consumed():
    w = _w(age=100)
    v = _decide([w], {"temperature": 72.0})
    assert v.recognised and v.reason == "value" and v.consumed == (w,)


def test_window_boundary_is_exclusive():
    just_in = _w(age=SETPOINT_ECHO_WINDOW_S - 0.5)
    at = _w(age=SETPOINT_ECHO_WINDOW_S)
    assert _decide([just_in], {"temperature": 72.0}).recognised
    v = _decide([at], {"temperature": 72.0})
    assert not v.recognised and v.reason == "no_memory"


def test_different_value_is_not_an_echo():
    v = _decide([_w()], {"temperature": 75.0})
    assert not v.recognised and v.reason == "no_match" and v.consumed == ()


def test_nothing_changed_and_nothing_remembered():
    assert _decide([_w()], {}).reason == "no_change"
    assert _decide([], {"temperature": 72.0}).reason == "no_memory"


def test_other_entity_is_never_an_echo():
    assert not _decide([_w(entity="climate.other")], {"temperature": 72.0}).recognised


def test_context_match_wins_over_value_and_window_is_still_required():
    assert _decide([_w(ctx="c1", age=100)], {"temperature": 80.0}, ctx="c1").reason == "context"
    assert _decide([_w(ctx="c1", age=100)], {"temperature": 80.0}, parent="c1").recognised
    assert not _decide([_w(ctx="c1", age=500)], {"temperature": 72.0}, ctx="c1").recognised


def test_user_context_blocks_the_value_fallback_but_not_a_context_match():
    assert not _decide([_w()], {"temperature": 72.0}, user="u1").recognised
    assert _decide([_w(ctx="c1")], {"temperature": 72.0}, ctx="c1", user="u1").recognised


def test_every_changed_attribute_must_be_accounted_for():
    low = _w("target_temp_low", 69.0, ctx="a")
    high = _w("target_temp_high", 77.0, ctx="a")
    assert _decide([low, high], {"target_temp_low": 69.0, "target_temp_high": 77.0}).recognised
    assert not _decide([low, high], {"target_temp_low": 69.0, "target_temp_high": 80.0}).recognised
    assert _decide([low, high], {"target_temp_low": 69.0}).recognised  # partial reports are answered separately


def test_band_write_never_accounts_for_the_other_edge_or_plain_temperature():
    low = _w("target_temp_low", 69.0)
    assert not _decide([low], {"target_temp_high": 69.0}).recognised
    assert not _decide([low], {"temperature": 69.0}).recognised


def test_single_temperature_write_accounts_for_a_band_edge():
    assert _decide([_w("temperature", 77.0)], {"target_temp_high": 77.0}).recognised
    assert _decide([_w("temperature", 77.0)], {"target_temp_low": 77.0}).recognised


def test_unreadable_value_is_not_an_echo():
    assert not _decide([_w()], {"temperature": None}).recognised
    assert not _decide([_w()], {"temperature": "n/a"}).recognised


def test_duplicate_same_value_writes_are_one_echo():
    a, b = _w(age=50, ctx="a"), _w(age=20, ctx="b")
    v = _decide([a, b], {"temperature": 72.0})
    assert set(v.consumed) == {a, b}


def test_prune_drops_expired_and_caps_newest():
    old = _w(age=SETPOINT_ECHO_WINDOW_S + 1)
    fresh = [_w(value=60.0 + i, age=1.0) for i in range(SETPOINT_ECHO_MEMORY_MAX + 5)]
    kept = prune_own_writes([old, *fresh], T0)
    assert old not in kept and len(kept) == SETPOINT_ECHO_MEMORY_MAX and kept[-1] is fresh[-1]
    assert len(prune_own_writes([old, *fresh], T0, cap=None)) == len(fresh)


# --- engine memory ------------------------------------------------------------------------------------------------


@pytest.fixture
def clock():
    now = {"t": T0}
    with patch.object(automation_module.dt_util, "now", side_effect=lambda: now["t"]):
        yield now


def _engine(**extra) -> AutomationEngine:
    hass = MagicMock()
    hass.services.async_call = AsyncMock()
    hass.async_create_task = MagicMock(side_effect=lambda coro: coro.close())
    state = MagicMock()
    state.state = "heat"
    state.attributes = {"hvac_modes": ["off", "heat", "cool"], "temperature": 70.0}
    hass.states.get.return_value = state
    config = {"comfort_heat": 70, "comfort_cool": 75, "setback_heat": 60, "setback_cool": 80}
    config.update(extra)
    return AutomationEngine(
        hass=hass,
        climate_entity=CE,
        weather_entity="weather.x",
        door_window_sensors=[],
        notify_service="notify.notify",
        config=config,
    )


def test_set_temperature_call_is_remembered_with_its_context(clock):
    e = _engine()
    r = asyncio.run(
        e._call_climate("set_temperature", {"entity_id": CE, "temperature": 72.0, "hvac_mode": "heat"}, "l")
    )
    assert r.outcome is ClimateCallOutcome.OK
    ctx = e.hass.services.async_call.call_args.kwargs["context"]
    assert [(w.entity_id, w.attribute, w.value, w.context_id) for w in e._own_setpoint_writes] == [
        (CE, "temperature", 72.0, ctx.id)
    ]


def test_other_climate_services_are_not_remembered(clock):
    e = _engine()
    asyncio.run(e._call_climate("set_hvac_mode", {"entity_id": CE, "hvac_mode": "heat"}, "l"))
    asyncio.run(e._call_climate("set_fan_mode", {"entity_id": CE, "fan_mode": "on"}, "l"))
    assert e._own_setpoint_writes == []


def test_dual_setpoint_write_remembers_both_edges(clock):
    e = _engine()
    asyncio.run(
        e._call_climate("set_temperature", {"entity_id": CE, "target_temp_low": 68.0, "target_temp_high": 76.0}, "l")
    )
    assert {(w.attribute, w.value) for w in e._own_setpoint_writes} == {
        ("target_temp_low", 68.0),
        ("target_temp_high", 76.0),
    }


def test_observe_only_write_is_not_remembered(clock):
    e = _engine()
    e.dry_run = True
    asyncio.run(e._call_climate("set_temperature", {"entity_id": CE, "temperature": 72.0}, "l"))
    assert e._own_setpoint_writes == []


def test_failed_and_rejected_writes_are_forgotten(clock):
    e = _engine()
    e.hass.services.async_call = AsyncMock(side_effect=RuntimeError("boom"))
    r = asyncio.run(e._call_climate("set_temperature", {"entity_id": CE, "temperature": 72.0}, "l"))
    assert r.outcome is ClimateCallOutcome.FAILED
    assert e._own_setpoint_writes == []  # it never reached the thermostat: nothing will echo


def test_window_restarts_when_the_blocking_call_returns(clock):
    e = _engine()

    async def _slow(*a, **k):
        clock["t"] += timedelta(seconds=25)  # a cloud thermostat holds the blocking call

    e.hass.services.async_call = AsyncMock(side_effect=_slow)
    asyncio.run(e._call_climate("set_temperature", {"entity_id": CE, "temperature": 72.0}, "l"))
    assert e._own_setpoint_writes[0].written_at == T0 + timedelta(seconds=25)


def test_memory_is_bounded(clock):
    e = _engine()
    for i in range(SETPOINT_ECHO_MEMORY_MAX + 6):
        asyncio.run(e._call_climate("set_temperature", {"entity_id": CE, "temperature": 60.0 + i}, "l"))
    assert len(e._own_setpoint_writes) == SETPOINT_ECHO_MEMORY_MAX
    assert e._own_setpoint_writes[-1].value == 60.0 + SETPOINT_ECHO_MEMORY_MAX + 5


def test_recognise_consumes_the_echo_and_clears_on_window_end(clock):
    e = _engine()
    asyncio.run(e._call_climate("set_temperature", {"entity_id": CE, "temperature": 72.0}, "l"))
    clock["t"] += timedelta(seconds=60)
    assert e.recognise_own_setpoint_echo({"temperature": 72.0}) is True
    assert e._own_setpoint_writes == []  # cleared on echo arrival
    assert e.recognise_own_setpoint_echo({"temperature": 72.0}) is False  # nothing left to answer a second change
    asyncio.run(e._call_climate("set_temperature", {"entity_id": CE, "temperature": 73.0}, "l"))
    clock["t"] += timedelta(seconds=SETPOINT_ECHO_WINDOW_S + 1)
    assert e.recognise_own_setpoint_echo({"temperature": 73.0}) is False
    assert e._own_setpoint_writes == []  # cleared at window end


def test_non_echo_leaves_other_writes_in_flight(clock):
    e = _engine()
    asyncio.run(e._call_climate("set_temperature", {"entity_id": CE, "temperature": 72.0}, "l"))
    assert e.recognise_own_setpoint_echo({"temperature": 80.0}) is False
    assert len(e._own_setpoint_writes) == 1  # the real echo is still coming


def test_celsius_install_uses_unit_default_step(clock):
    e = _engine(temp_unit="celsius")
    asyncio.run(e._call_climate("set_temperature", {"entity_id": CE, "temperature": 22.2222}, "l"))
    assert e.recognise_own_setpoint_echo({"temperature": 22.5}) is True  # no advertised step: 0.5 C assumed


def test_naive_clock_mix_never_raises(clock):
    e = _engine()
    asyncio.run(e._call_climate("set_temperature", {"entity_id": CE, "temperature": 72.0}, "l"))
    clock["t"] = datetime(2026, 11, 2, 1, 1)  # naive
    assert e.recognise_own_setpoint_echo({"temperature": 72.0}) is False


# --- superseded writes (owner decision 2026-11-02, #1232 D3) -----------------------------------------------------


def test_recognised_echo_supersedes_older_writes_of_the_same_attribute():
    older, newer = _w(value=72.0, age=60, ctx="a"), _w(value=74.0, age=30, ctx="b")
    v = _decide([older, newer], {"temperature": 74.0})
    assert v.recognised and v.consumed == (newer,)
    assert v.superseded == (older,)  # the thermostat moved past 72: an occupant's 72 is no longer CA's echo


def test_in_order_echo_supersedes_nothing_newer():
    older, newer = _w(value=72.0, age=60, ctx="a"), _w(value=74.0, age=30, ctx="b")
    v = _decide([older, newer], {"temperature": 72.0})
    assert v.recognised and v.consumed == (older,) and v.superseded == ()  # 74's echo is still coming


def test_a_different_attribute_is_not_superseded():
    low_old = _w("target_temp_low", 68.0, age=60, ctx="a")
    high_old = _w("target_temp_high", 76.0, age=60, ctx="a")
    low_new = _w("target_temp_low", 69.0, age=20, ctx="b")
    v = _decide([low_old, high_old, low_new], {"target_temp_low": 69.0})
    assert v.recognised and v.consumed == (low_new,)
    assert v.superseded == (low_old,)  # high's own echo is still to be recognised


def test_unrecognised_change_supersedes_nothing():
    older, newer = _w(value=72.0, age=60, ctx="a"), _w(value=74.0, age=30, ctx="b")
    v = _decide([older, newer], {"temperature": 80.0})
    assert not v.recognised and v.superseded == ()


def test_engine_forgets_superseded_writes(clock):
    e = _engine()
    asyncio.run(e._call_climate("set_temperature", {"entity_id": CE, "temperature": 72.0}, "l"))
    clock["t"] += timedelta(seconds=60)
    asyncio.run(e._call_climate("set_temperature", {"entity_id": CE, "temperature": 74.0}, "l"))
    clock["t"] += timedelta(seconds=30)
    assert e.recognise_own_setpoint_echo({"temperature": 74.0}) is True
    assert e._own_setpoint_writes == []
    assert e.recognise_own_setpoint_echo({"temperature": 72.0}) is False  # the occupant dialling back
