"""Issue #1199: stored grace end instants must be true instants across a daylight-saving change.

Occupant stories (whole_house_fan and hvac_fan):

* Fall-back, 2026-11-01 01:01:30 PDT. The occupant turns the fan off and asks for it to stay off for an hour.
  The clocks go back at 02:00 PDT, so the hour ends at 01:01:30 PST. Climate Advisor used to add the hour to the
  WALL clock, stored 02:01:30, and kept the fan off an extra hour (the hold, the status text and the log all said
  2:01).
* Spring-forward, 2026-03-08 01:30 PST. The same one-hour request ends at 03:30 PDT. The old wall-clock end
  (02:30, a time that never exists) made the hold lapse at 03:00 PDT, 30 minutes early, so Climate Advisor could
  start the fan the occupant asked to keep off.
* The occupant's own fan run (RF timer or default) has the same deadline and the same flaw.
* A restart in the middle restores the same instant, from either an old-format (local offset) or a new (UTC) string.

The shared stub timezone defaults to UTC, which is why nothing caught this: every test here runs in
America/Los_Angeles (autouse fixture, scoped to this module; the shared ``_reset_ha_dt`` fixture in conftest
restores the default after each test, so no other module sees the change).

Prove the tests are not vacuous with ``scripts/revert_proof.py origin/main tests/test_dst_grace_end_1199.py``.
"""

from __future__ import annotations

import logging
import sys
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from custom_components.climate_advisor.const import CONF_MANUAL_GRACE_PERIOD
from custom_components.climate_advisor.time_utils import instant_after
from tests.test_restore_resolved_band import _make_engine
from tests.test_stale_fan_marker_1205 import _MODES, _Home, _unrelated_ordinary_ca_start

_LA = ZoneInfo("America/Los_Angeles")
_HOUR_S = 3600.0
# (id, instant of the occupant's request, what the correct local end clock reads)
_FALL_BACK = datetime(2026, 11, 1, 1, 1, 30, tzinfo=_LA)  # PDT; the clocks go back at 02:00 PDT
_SPRING_FORWARD = datetime(2026, 3, 8, 1, 30, 0, tzinfo=_LA)  # PST; the clocks jump at 02:00 PST
_CASES = [
    pytest.param(_FALL_BACK, "01:01", id="fall_back"),
    pytest.param(_SPRING_FORWARD, "03:30", id="spring_forward"),
]
# Setup the "ordinary CA start, then the occupant's off" scenario takes this long on the virtual clock.
_SETUP_S = 360.0 + 600.0  # toggle floor + 60, then the window debounce


class _LaClock:
    """A virtual clock holding one true UTC instant; ``dt_util.now()`` reads it in Los Angeles."""

    def __init__(self, now_utc: datetime) -> None:
        self.now = now_utc.astimezone(UTC)

    def advance(self, seconds: float) -> None:
        self.now += timedelta(seconds=seconds)


@pytest.fixture(autouse=True)
def _los_angeles():
    """The stubs default to UTC, which hides every wall-clock-vs-instant defect (Issue #1199)."""
    dt_mod = sys.modules["homeassistant.util.dt"]
    dt_mod.DEFAULT_TIME_ZONE = _LA
    yield
    # conftest._reset_ha_dt restores DEFAULT_TIME_ZONE to UTC after every test.


def _make_clock(monkeypatch, at: datetime) -> _LaClock:
    clk = _LaClock(at)
    dt_mod = sys.modules["homeassistant.util.dt"]
    monkeypatch.setattr(dt_mod, "now", lambda time_zone=None: clk.now.astimezone(time_zone or _LA))
    monkeypatch.setattr(dt_mod, "utcnow", lambda: clk.now)
    return clk


def _off_with_hold(monkeypatch, fan_mode: str, t_off: datetime, grace_s: float = _HOUR_S) -> tuple[_Home, _LaClock]:
    """A real engine; the occupant turns the fan off at ``t_off`` and the fan-off grace/hold starts."""
    clk = _make_clock(monkeypatch, t_off - timedelta(seconds=_SETUP_S))
    home = _Home(fan_mode, clk, **{CONF_MANUAL_GRACE_PERIOD: grace_s})
    home.close_window()
    _unrelated_ordinary_ca_start(home)
    assert clk.now == t_off.astimezone(UTC), "precondition: the occupant's off happens at the scenario instant"
    home.occupant_turns_fan_off()
    assert home.engine._last_grace_trigger == "fan_off"
    assert home.engine._fan_off_hold_until is not None
    return home, clk


# ---------------------------------------------------------------------------
# The helper itself
# ---------------------------------------------------------------------------


def test_instant_after_is_exact_utc_across_both_transitions(monkeypatch):
    for start in (_FALL_BACK, _SPRING_FORWARD):
        clk = _make_clock(monkeypatch, start)
        from_now = instant_after(_HOUR_S)
        from_local = instant_after(_HOUR_S, now=start)
        expected = start.astimezone(UTC) + timedelta(seconds=_HOUR_S)
        assert from_now == expected == from_local
        assert from_now.utcoffset() == timedelta(0), "an end instant is stored as UTC"
        assert (from_now - clk.now).total_seconds() == _HOUR_S


def test_instant_after_reads_a_naive_base_in_the_configured_zone():
    naive = datetime(2026, 11, 1, 1, 1, 30)
    assert instant_after(60, now=naive) == datetime(2026, 11, 1, 8, 2, 30, tzinfo=UTC)  # 01:01:30 PDT + 1 min


# ---------------------------------------------------------------------------
# The fan-off hold and its grace
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("fan_mode", _MODES)
@pytest.mark.parametrize(("t_off", "_ends"), _CASES)
def test_fan_off_hold_lasts_the_requested_real_time(monkeypatch, fan_mode, t_off, _ends):
    """The fan stays off for exactly the requested hour of real time, and CA may start it the second after."""
    home, clk = _off_with_hold(monkeypatch, fan_mode, t_off)
    engine = home.engine
    true_end = t_off.astimezone(UTC) + timedelta(seconds=_HOUR_S)

    assert engine._fan_off_hold_until == true_end
    clk.advance(_HOUR_S - 1)
    assert engine._fan_off_hold_active(), "the hour is not over yet: the fan must still be held off"
    clk.advance(2)
    assert not engine._fan_off_hold_active(), "the hour is over: the hold must have lapsed"


@pytest.mark.parametrize("fan_mode", _MODES)
@pytest.mark.parametrize(("t_off", "_ends"), _CASES)
def test_grace_end_time_is_a_utc_instant_and_parses(monkeypatch, fan_mode, t_off, _ends):
    home, _clk = _off_with_hold(monkeypatch, fan_mode, t_off)
    engine = home.engine
    true_end = t_off.astimezone(UTC) + timedelta(seconds=_HOUR_S)

    stored = engine._grace_end_time
    parsed = datetime.fromisoformat(stored)
    assert parsed == true_end
    assert parsed.utcoffset() == timedelta(0), f"grace_end_time must be a UTC instant, got {stored}"
    assert engine._parse_grace_end() == true_end
    assert engine._grace_duration_seconds == _HOUR_S


@pytest.mark.parametrize("fan_mode", _MODES)
@pytest.mark.parametrize(("t_off", "ends"), _CASES)
def test_ends_log_and_status_text_show_the_true_local_end(monkeypatch, caplog, fan_mode, t_off, ends):
    """The log says ``ends=HH:MM`` and the Status card says ``(ends H:MM AM)`` in the occupant's local time."""
    from custom_components.climate_advisor.coordinator import ClimateAdvisorCoordinator

    with caplog.at_level(logging.INFO):
        home, _clk = _off_with_hold(monkeypatch, fan_mode, t_off)
    started = [r.getMessage() for r in caplog.records if "Started manual grace period" in r.getMessage()]
    assert started, "the grace start line must be logged"
    assert f"ends={ends}" in started[-1], started[-1]

    coordinator = object.__new__(ClimateAdvisorCoordinator)
    text = ClimateAdvisorCoordinator._format_grace_remaining(coordinator, home.engine)
    hour, minute = (int(x) for x in ends.split(":"))
    expected = f"{hour % 12 or 12}:{minute:02d} {'AM' if hour < 12 else 'PM'}"
    assert text == f" — 1h (ends {expected})", text


# ---------------------------------------------------------------------------
# The occupant's own fan run (its own deadline)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("fan_mode", _MODES)
@pytest.mark.parametrize(("t_on", "_ends"), _CASES)
def test_fan_override_deadline_is_the_real_end_of_the_timer(monkeypatch, fan_mode, t_on, _ends):
    clk = _make_clock(monkeypatch, t_on)
    home = _Home(fan_mode, clk)
    engine = home.engine
    engine.handle_fan_manual_override(
        fan_before="off",
        fan_after="on",
        duration_override=_HOUR_S,
        remote_timer_hours=1.0,
        hvac_fan=home.hvac_fan,
    )
    home.drain()
    true_end = t_on.astimezone(UTC) + timedelta(seconds=_HOUR_S)

    assert engine._fan_override_active
    assert engine._fan_override_until == true_end
    assert datetime.fromisoformat(engine._grace_end_time).utcoffset() == timedelta(0)
    clk.advance(_HOUR_S - 1)
    assert engine._fan_override_deadline_active(), "the occupant's hour has a second left"
    clk.advance(2)
    assert not engine._fan_override_deadline_active(), "the occupant's hour is over"


# ---------------------------------------------------------------------------
# Restart restore: persisted old-format and new-format strings
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("fan_mode", _MODES)
@pytest.mark.parametrize(("t_off", "_ends"), _CASES)
def test_restart_restores_the_remaining_real_time(monkeypatch, fan_mode, t_off, _ends):
    """10 minutes into the hour a restart lands: 50 real minutes remain and the hold runs to the same instant."""
    home, clk = _off_with_hold(monkeypatch, fan_mode, t_off)
    state = home.engine.get_serializable_state()
    true_end = t_off.astimezone(UTC) + timedelta(seconds=_HOUR_S)

    clk.advance(600)
    restarted = _make_engine(indoor=72.0, mode="cool", **{CONF_MANUAL_GRACE_PERIOD: _HOUR_S})
    restarted.hass.async_create_task.side_effect = lambda coro: coro.close()
    restarted.restore_state(state)

    assert restarted._grace_active
    assert restarted._last_grace_trigger == "fan_off"
    assert restarted._grace_duration_seconds == pytest.approx(_HOUR_S - 600, abs=1)
    assert restarted._fan_off_hold_until == true_end
    clk.advance(_HOUR_S - 600 - 1)
    assert restarted._fan_off_hold_active()
    clk.advance(2)
    assert not restarted._fan_off_hold_active()


@pytest.mark.parametrize(
    "stored",
    [
        pytest.param("2026-11-01T02:01:30-08:00", id="utc_instant_written_as_pst_offset"),
        pytest.param("2026-11-01T09:01:30+00:00", id="new_utc_string"),
        pytest.param("2026-11-01T09:01:30Z", id="z_suffix"),
        pytest.param("2026-11-01T01:31:30-07:00", id="old_local_offset_string_pdt"),
    ],
)
def test_persisted_grace_end_time_in_either_format_still_parses(monkeypatch, stored):
    """Old local-offset strings (written before this fix) and new UTC strings both restore after a restart."""
    clk = _make_clock(monkeypatch, datetime(2026, 11, 1, 1, 1, 30, tzinfo=_LA) + timedelta(seconds=600))
    engine = _make_engine(indoor=72.0, mode="cool")
    engine.hass.async_create_task.side_effect = lambda coro: coro.close()
    engine.restore_state(
        {
            "grace_end_time": stored,
            "grace_duration_seconds": _HOUR_S,
            "last_grace_trigger": "fan_off",
            "last_resume_source": "manual",
        }
    )
    end = datetime.fromisoformat(stored)
    assert engine._grace_active
    assert engine._grace_duration_seconds == pytest.approx((end - clk.now).total_seconds(), abs=1)
    assert abs((engine._parse_grace_end() - end).total_seconds()) < 1, "restored grace ends at the persisted instant"


# ---------------------------------------------------------------------------
# Sibling end instants: debounce expiry, startup window, settle window, rate limit
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(("start", "_ends"), _CASES)
def test_other_stored_end_instants_are_true_utc_instants(monkeypatch, start, _ends):
    """Every other stored ``now + duration`` instant (#1199 sites) is a real UTC instant."""
    from custom_components.climate_advisor.desired_state import (
        decide_fan_thermo_backstop,
        decide_grace_start,
        decide_override_confirm,
        decide_revisit,
    )
    from custom_components.climate_advisor.fan_toggle_rate_limit import (
        FanToggleRateLimitInputs,
        decide_fan_toggle_rate_limit,
    )

    clk = _make_clock(monkeypatch, start)
    now = sys.modules["homeassistant.util.dt"].now()
    expect = start.astimezone(UTC) + timedelta(seconds=_HOUR_S)

    grace = decide_grace_start(
        source="manual",
        manual_duration_seconds=_HOUR_S,
        manual_should_notify=False,
        automation_duration_seconds=0,
        automation_should_notify=False,
        now=now,
    )
    assert grace is not None and grace.at == expect and grace.at.utcoffset() == timedelta(0)
    assert decide_revisit(has_revisit_callback=True, delay_seconds=_HOUR_S, now=now).at == expect
    assert decide_override_confirm(confirm_seconds=_HOUR_S, detected_mode="cool", now=now).at == expect
    assert decide_fan_thermo_backstop(fan_running=True, delay_seconds=_HOUR_S, now=now).at == expect

    _outcome, applies_at = decide_fan_toggle_rate_limit(
        FanToggleRateLimitInputs(
            last_toggle_command_time=now,
            action="activate",
            rate_limited_until=None,
            rate_limited_direction=None,
            min_interval_s=_HOUR_S,
            now=now + timedelta(seconds=1),
        )
    )
    assert applies_at == expect and applies_at.utcoffset() == timedelta(0)
    assert clk.now == start.astimezone(UTC)


@pytest.mark.parametrize(("start", "_ends"), _CASES)
def test_startup_coalesce_label_is_local_for_a_utc_expiry(monkeypatch, start, _ends):
    """Issue #1235 window: the stored expiry is a UTC instant; the Next-Automation label stays local.

    The stamping site (``async_setup``) needs a live Home Assistant; the real-HA test in
    ``tests_ha/test_dst_grace_end_1199.py`` covers it end to end.
    """
    from custom_components.climate_advisor.coordinator import ClimateAdvisorCoordinator

    _make_clock(monkeypatch, start)
    expiry = start.astimezone(UTC) + timedelta(seconds=300)
    coordinator = object.__new__(ClimateAdvisorCoordinator)
    coordinator._startup_coalesce_active = True
    coordinator._startup_coalesce_expiry = expiry.isoformat()
    _label, time_str = ClimateAdvisorCoordinator._compute_next_automation_action(coordinator, None)
    assert time_str == expiry.astimezone(_LA).strftime("%I:%M %p").lstrip("0")


@pytest.mark.parametrize("fan_mode", _MODES)
@pytest.mark.parametrize(("t_on", "_ends"), _CASES)
def test_rf_timer_boundary_settle_window_is_a_utc_instant(monkeypatch, fan_mode, t_on, _ends):
    """The 120 s settle window armed when an RF-timer grace expires ends 120 real seconds later, as UTC."""
    from custom_components.climate_advisor.const import TIMER_BOUNDARY_SETTLE_SECONDS

    clk = _make_clock(monkeypatch, t_on)
    home = _Home(fan_mode, clk)
    engine = home.engine
    engine.handle_fan_manual_override(
        fan_before="off", fan_after="on", duration_override=600, remote_timer_hours=1.0, hvac_fan=home.hvac_fan
    )
    home.drain()
    home.expire_grace()
    settle = engine._timer_boundary_settle_until
    assert settle == clk.now + timedelta(seconds=TIMER_BOUNDARY_SETTLE_SECONDS)
    assert settle.utcoffset() == timedelta(0)


@pytest.mark.parametrize(("start", "_ends"), _CASES)
def test_pre_cool_reschedule_candidate_and_comfort_hold_until_are_true_instants(monkeypatch, start, _ends):
    from custom_components.climate_advisor.coordinator import ClimateAdvisorCoordinator, _decide_pre_cool_reschedule

    _make_clock(monkeypatch, start)
    now = sys.modules["homeassistant.util.dt"].now()
    expect = start.astimezone(UTC) + timedelta(minutes=90)
    trigger = _decide_pre_cool_reschedule(
        current_trigger_at=now + timedelta(days=1),
        pre_cool_eligible=True,
        nat_vent_close_delay_minutes=90,
        now=now,
    )
    assert trigger == expect and trigger.utcoffset() == timedelta(0)

    class _Ae:
        _last_comfort_hold = {
            "candidate_family": "cooling",
            "depth_f": 1.0,
            "tolerance_f": 1.0,
            "opposite_ran_min_ago": 0,
            "window_min": 90,
        }

    label = ClimateAdvisorCoordinator._comfort_family_hold_label(_Ae, now)
    assert f"(until ~{expect.astimezone(_LA).strftime('%H:%M')})" in label, label


# ---------------------------------------------------------------------------
# Pre-cool trigger end instants (wake offset and window-close delay)
# ---------------------------------------------------------------------------


def _pre_cool_trigger(monkeypatch, now_local: datetime, *, window_close=None, wake="06:30"):
    from types import SimpleNamespace

    from custom_components.climate_advisor.coordinator import _compute_pre_cool_trigger_time_pure

    _make_clock(monkeypatch, now_local)
    classification = SimpleNamespace(setback_modifier=-3.0, tomorrow_high=70.0, window_close_time=window_close)
    config = {"wake_time": wake, "comfort_cool": 74.0, "sleep_cool": 78.0}
    return _compute_pre_cool_trigger_time_pure(classification, config, now_local.astimezone(_LA))


@pytest.mark.parametrize(
    ("now_local", "wake", "true_trigger_utc"),
    [
        # wake 05:30 PST on fall-back day: 4 h earlier is 01:30 PST (09:30Z), not the wall-clock 01:30 PDT (08:30Z)
        pytest.param(
            datetime(2026, 10, 31, 20, 0, tzinfo=_LA),
            "05:30",
            datetime(2026, 11, 1, 9, 30, tzinfo=UTC),
            id="fall_back",
        ),
        # wake 06:30 PDT after spring-forward: 4 h earlier is 01:30 PST (09:30Z); wall-clock 02:30 does not exist
        pytest.param(
            datetime(2026, 3, 7, 20, 0, tzinfo=_LA),
            "06:30",
            datetime(2026, 3, 8, 9, 30, tzinfo=UTC),
            id="spring_forward",
        ),
    ],
)
def test_pre_cool_wake_fallback_trigger_is_four_real_hours_before_wake(monkeypatch, now_local, wake, true_trigger_utc):
    trigger = _pre_cool_trigger(monkeypatch, now_local, wake=wake)
    assert trigger == true_trigger_utc
    assert trigger.utcoffset() == timedelta(0)


def test_pre_cool_window_close_trigger_is_thirty_real_minutes_after_close(monkeypatch):
    from datetime import time

    # Window closes 01:45 PDT on fall-back night; +30 min is 01:15 PST (09:15Z), not the wall-clock 02:15 PST (10:15Z)
    trigger = _pre_cool_trigger(monkeypatch, datetime(2026, 11, 1, 0, 30, tzinfo=_LA), window_close=time(1, 45))
    assert trigger == datetime(2026, 11, 1, 9, 15, tzinfo=UTC)
    assert trigger.utcoffset() == timedelta(0)


# ---------------------------------------------------------------------------
# Occupant-visible displays of a UTC end instant read in local time
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("fan_mode", _MODES)
@pytest.mark.parametrize(("start", "_ends"), _CASES)
def test_rate_limit_texts_show_the_local_clock_time(monkeypatch, fan_mode, start, _ends):
    """Fan card "applies at HH:MM:SS" and the fan_mode_change "deferred (5-min floor, applies ...)" are local."""
    from custom_components.climate_advisor.coordinator import ClimateAdvisorCoordinator

    clk = _make_clock(monkeypatch, start)
    home = _Home(fan_mode, clk)
    engine = home.engine
    applies_at = instant_after(300)  # the real rate-limit record is a UTC instant
    engine._fan_rate_limited_until = applies_at
    engine._fan_rate_limited_direction = "activate"
    expected = applies_at.astimezone(_LA).strftime("%H:%M:%S")

    assert engine._deferred_fan_mode_change_marker() == f"deferred (5-min floor, applies {expected})"
    coordinator = object.__new__(ClimateAdvisorCoordinator)
    suffix = ClimateAdvisorCoordinator._whf_rate_limit_suffix(coordinator, engine)
    assert f"applies at {expected}" in suffix, suffix


@pytest.mark.parametrize(("_start", "_ends"), _CASES)
def test_stuck_grace_recovered_text_shows_the_local_end(monkeypatch, _start, _ends):
    from custom_components.climate_advisor.ai_skills_context import _render_stuck_grace_recovered

    _make_clock(monkeypatch, _start)
    end_utc = _start.astimezone(UTC) + timedelta(hours=1)
    text, _settings = _render_stuck_grace_recovered({"grace_end_time": end_utc.isoformat()}, "fahrenheit")
    assert text == f"Grace period recovered (expired {end_utc.astimezone(_LA).isoformat()})"
