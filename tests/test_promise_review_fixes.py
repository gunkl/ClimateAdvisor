"""Regression tests for the verification-pass defects on the household promises (Issue #1125).

Everything here drives the real ``PromiseJournal`` + ``run_promise_checks`` (the same two calls the
coordinator heartbeat makes); nothing re-implements a check.
"""

from __future__ import annotations

import sys
import time
from datetime import UTC, datetime, timedelta

import pytest

if "homeassistant" not in sys.modules:
    from conftest import install_ha_stubs

    install_ha_stubs()

from custom_components.climate_advisor.invariant_watchdog import (  # noqa: E402
    SCOPE_HOME,
    PromiseConfig,
    check_p4_override_respected,
    occupant_actions,
    run_promise_checks,
)
from custom_components.climate_advisor.promise_journal import (  # noqa: E402
    Command,
    PromiseJournal,
    Reading,
)

T0 = datetime(2026, 10, 30, 9, 0, tzinfo=UTC)
GRACE = {"source": "manual", "trigger": "manual", "duration_seconds": 7200}


def _tick(journal: PromiseJournal, cfg: PromiseConfig, ts: datetime, reading: Reading):
    """One heartbeat: record, window, check, dedup. Returns the NEW hits."""
    journal.record_reading(reading)
    return [h for h in run_promise_checks(journal.window(ts), cfg, scope=SCOPE_HOME) if journal.is_new(h.key)]


def _cold(ts: datetime, **kw) -> Reading:
    base = {
        "hvac_mode": "heat",
        "hvac_action": "idle",
        "indoor_f": 66.0,
        "setpoint_f": 62.0,
        "fan_on": False,
        "contacts_open": False,
        "forecast_high_f": 60.0,
    }
    base.update(kw)
    return Reading(ts=ts, **base)


COMFORT = {"comfort_heat": 70, "comfort_cool": 75}


# ── 1. a long episode is reported once, not once per tick after the 3 h window slides ──────────
class TestLongEpisodeReportedOnce:
    def test_p7_six_hour_cold_stretch_is_one_report(self):
        j, cfg, new = PromiseJournal(), PromiseConfig.from_config(COMFORT), []
        for i in range(12 * 6):
            ts = T0 + timedelta(minutes=5 * i)
            new += [h.promise for h in _tick(j, cfg, ts, _cold(ts))]
        assert new == ["comfort_kept"]

    def test_p7_a_new_episode_after_the_house_recovers_is_reported_again(self):
        j, cfg, new = PromiseJournal(), PromiseConfig.from_config(COMFORT), []
        for i in range(12 * 8):
            ts = T0 + timedelta(minutes=5 * i)
            cold = i < 12 * 4 or i >= 12 * 5  # cold 4 h, comfortable 1 h, cold again 3 h
            new += [h.promise for h in _tick(j, cfg, ts, _cold(ts, indoor_f=66.0 if cold else 72.0))]
        assert new == ["comfort_kept", "comfort_kept"]

    def test_p7_opposite_side_episode_is_its_own_report(self):
        j, cfg, new = PromiseJournal(), PromiseConfig.from_config(COMFORT), []
        for i in range(12 * 5):
            ts = T0 + timedelta(minutes=5 * i)
            r = _cold(ts) if i < 12 * 2 else _cold(ts, hvac_mode="cool", indoor_f=80.0)
            new += [dict(h.evidence)["side"] for h in _tick(j, cfg, ts, r)]
        assert new == ["below", "above"]

    def test_p5b_five_hour_blind_fan_run_is_one_report(self):
        j, cfg, new = PromiseJournal(), PromiseConfig(), []
        for i in range(12 * 5):
            ts = T0 + timedelta(minutes=5 * i)
            r = Reading(ts=ts, hvac_mode="off", hvac_action="idle", indoor_f=None, fan_on=True)
            new += [h.promise for h in _tick(j, cfg, ts, r) if h.promise == "blind_fan_continue"]
        assert new == ["blind_fan_continue"]

    def test_p5b_a_second_blind_run_after_a_readable_gap_is_reported(self):
        j, cfg, new = PromiseJournal(), PromiseConfig(), []
        for i in range(12 * 6):
            ts = T0 + timedelta(minutes=5 * i)
            blind = i < 12 * 2 or i >= 12 * 3
            r = Reading(ts=ts, hvac_mode="off", hvac_action="idle", indoor_f=None if blind else 71.0, fan_on=True)
            new += [h.promise for h in _tick(j, cfg, ts, r) if h.promise == "blind_fan_continue"]
        assert new == ["blind_fan_continue", "blind_fan_continue"]

    def test_p4_episode_identity_survives_older_marks_being_pruned(self):
        """Episode ids used to be mark-list indexes, so pruning an older episode's marks renamed a newer one."""
        j = PromiseJournal()
        j.on_event(T0, "override_detected", {"detected_mode": "heat"})
        j.on_event(T0 + timedelta(minutes=1), "grace_expired", {})
        t1 = T0 + timedelta(hours=2)
        j.on_event(t1, "override_detected", {"detected_mode": "cool"})
        j.on_event(t1 + timedelta(seconds=1), "grace_started", GRACE)
        j.record_command(
            Command(ts=t1 + timedelta(minutes=10), target="thermostat", service="set_hvac_mode", mode="off")
        )
        keys = set()
        # second look is after the first override's marks aged out of the 3 h window
        for now in (t1 + timedelta(minutes=20), T0 + timedelta(hours=3, minutes=30)):
            keys |= {h.key for h in check_p4_override_respected(j.window(now), PromiseConfig())}
            assert len(keys) == 1, keys


# ── 2. P4 only counts mode-changing services as a reversal ─────────────────────────────────────
def _override_journal() -> PromiseJournal:
    j = PromiseJournal()
    j.on_event(T0, "override_detected", {"detected_mode": "cool", "source": "manual"})
    j.on_event(T0 + timedelta(seconds=1), "grace_started", GRACE)
    return j


def _cmd(seconds: int, service: str, mode: str) -> Command:
    return Command(ts=T0 + timedelta(seconds=seconds), target="thermostat", service=service, mode=mode, indoor_f=72)


def _p4(j: PromiseJournal):
    return check_p4_override_respected(j.window(T0 + timedelta(minutes=25)), PromiseConfig())


class TestP4Reversal:
    def test_fan_mode_auto_during_a_cool_override_is_not_a_reversal(self):
        j = _override_journal()
        j.record_command(_cmd(1200, "set_fan_mode", "auto"))
        assert _p4(j) == []

    def test_set_hvac_mode_off_is_still_a_reversal(self):
        j = _override_journal()
        j.record_command(_cmd(1200, "set_hvac_mode", "off"))
        assert len(_p4(j)) == 1

    def test_mode_bundled_in_set_temperature_is_still_a_reversal(self):
        j = _override_journal()
        j.record_command(_cmd(1200, "set_temperature", "heat"))
        assert len(_p4(j)) == 1

    def test_one_reversal_followed_by_a_fan_auto_assert_is_counted_once(self):
        j = _override_journal()
        j.record_command(_cmd(1200, "set_hvac_mode", "off"))
        j.record_command(_cmd(1201, "set_fan_mode", "auto"))
        assert len(_p4(j)) == 1


class TestOccupantReplacesTheirOwnOverride:
    """Golden override_second_override_during_grace: a second manual change inside the first one's
    grace makes production emit override_cleared{new_override_during_grace} + override_detected at
    the same instant. That is the occupant replacing their change, not CA ending it early."""

    def test_second_manual_change_during_grace_is_not_an_early_end(self):
        j = _override_journal()
        t = T0 + timedelta(minutes=70)
        j.on_event(t, "override_cleared", {"was_mode": "cool", "reason": "new_override_during_grace"})
        j.on_event(t, "override_detected", {"detected_mode": "off", "source": "manual"})
        hits = check_p4_override_respected(j.window(t + timedelta(minutes=5)), PromiseConfig())
        assert hits == []

    def test_second_change_whose_detected_mark_was_deduped_is_not_an_early_end(self):
        """Generated days 5000078 / 9000063: production dedups override_detected for 5 min, so the
        replacement mark never arrives; the clear reason itself says the occupant replaced it."""
        j = _override_journal()
        t = T0 + timedelta(minutes=3)
        j.on_event(t, "override_cleared", {"was_mode": "cool", "reason": "new_override_during_grace"})
        hits = check_p4_override_respected(j.window(t + timedelta(minutes=5)), PromiseConfig())
        assert hits == []

    def test_a_clear_with_no_replacement_is_still_an_early_end(self):
        j = _override_journal()
        t = T0 + timedelta(minutes=70)
        j.on_event(t, "override_cleared", {"was_mode": "cool", "reason": "grace_superseded_by_ca"})
        hits = check_p4_override_respected(j.window(t + timedelta(minutes=5)), PromiseConfig())
        assert len(hits) == 1

    def test_fan_override_replaced_by_a_new_fan_override_is_not_an_early_end(self):
        j = PromiseJournal()
        j.on_event(T0, "fan_manual_override", {"fan_after": "on"})
        j.on_event(T0 + timedelta(seconds=1), "grace_started", {**GRACE, "trigger": "fan_manual_override"})
        t = T0 + timedelta(minutes=30)
        j.on_event(t, "fan_override_cleared", {"reason": "new_override", "source": "manual"})
        j.on_event(t, "fan_manual_override", {"fan_after": "off"})
        hits = check_p4_override_respected(j.window(t + timedelta(minutes=5)), PromiseConfig())
        assert hits == []


# ── 3. per-tick cost ───────────────────────────────────────────────────────────────────────────
def _full_journal() -> PromiseJournal:
    j = PromiseJournal()
    for i in range(400):
        ts = T0 + timedelta(seconds=27 * i)
        j.record_reading(
            Reading(
                ts=ts,
                hvac_mode="heat" if i % 2 else "cool",
                hvac_action="idle",
                indoor_f=60.0 + (i % 3),
                outdoor_f=50,
                forecast_high_f=70,
                setpoint_f=65 + (i % 4),
                fan_on=False,
                contacts_open=False,
            )
        )
    for i in range(128):
        j.record_command(
            Command(ts=T0 + timedelta(seconds=84 * i), target="thermostat", service="set_temperature", setpoint_f=70)
        )
    for i in range(64):
        j.on_event(
            T0 + timedelta(seconds=150 * i),
            "override_detected" if i % 2 == 0 else "grace_started",
            {"detected_mode": "heat", "source": "manual", "duration_seconds": 3600, "trigger": "manual"},
        )
    return j


class TestPerTickCost:
    def test_full_journal_tick_stays_cheap(self):
        j, cfg, now = _full_journal(), PromiseConfig.from_config({}), T0 + timedelta(hours=3)
        run_promise_checks(j.window(now), cfg, scope=SCOPE_HOME)
        best = float("inf")
        for _ in range(5):
            s = time.perf_counter()
            run_promise_checks(j.window(now), cfg, scope=SCOPE_HOME)
            best = min(best, time.perf_counter() - s)
        assert best < 0.04, f"{best * 1000:.1f} ms per tick"

    def test_occupant_actions_is_computed_once_per_input(self):
        j = PromiseJournal()
        for i in range(4):
            ts = T0 + timedelta(minutes=5 * i)
            j.record_reading(Reading(ts=ts, hvac_mode="heat" if i % 2 else "cool", hvac_action="idle", indoor_f=70.0))
        inp = j.window(T0 + timedelta(minutes=30))
        assert occupant_actions(inp) is occupant_actions(inp)
        assert occupant_actions(j.window(T0 + timedelta(minutes=31))) is not occupant_actions(inp)

    def test_unknown_scope_still_rejected(self):
        with pytest.raises(ValueError):
            run_promise_checks(PromiseJournal().window(T0), PromiseConfig(), scope="nope")
