"""Tests for the household-promise evidence journal (Issue #1128, Step 3).

The journal is a pure leaf: these tests drive the real class and assert on what comes out of
``window()``; nothing re-implements journal logic in the test body.
"""

from __future__ import annotations

import sys
from datetime import UTC, datetime, timedelta

import pytest

if "homeassistant" not in sys.modules:
    from conftest import install_ha_stubs

    install_ha_stubs()

from custom_components.climate_advisor.promise_journal import (  # noqa: E402
    EVENT_MARK_MAP,
    JOURNAL_MAX_AGE,
    MAX_COMMANDS,
    MAX_EPISODE_MARKS,
    MAX_MARKS,
    MAX_READINGS,
    MAX_SEEN_KEYS,
    Command,
    PromiseJournal,
    Reading,
    bump_promise_stats,
    derive_episodes,
    mark_from_event,
    sanitize_promise_stats,
)

T0 = datetime(2026, 7, 15, 14, 0, tzinfo=UTC)


def at(minutes: float) -> datetime:
    return T0 + timedelta(minutes=minutes)


def rd(minutes: float, **kw) -> Reading:
    base = {"hvac_mode": "cool", "hvac_action": "idle", "indoor_f": 72.0, "setpoint_f": 74.0}
    base.update(kw)
    return Reading(ts=at(minutes), **base)


class TestReadingStorage:
    def test_unchanged_reading_inside_five_minutes_is_not_stored(self):
        j = PromiseJournal()
        assert j.record_reading(rd(0)) is True  # first one is always stored
        assert j.record_reading(rd(2)) is False
        assert len(j.window(at(3)).readings) == 1

    def test_latest_is_updated_even_when_not_stored(self):
        j = PromiseJournal()
        j.record_reading(rd(0))
        j.record_reading(rd(2, indoor_f=72.2))
        assert j.window(at(3)).latest.ts == at(2)

    def test_discrete_change_is_stored(self):
        j = PromiseJournal()
        j.record_reading(rd(0))
        assert j.record_reading(rd(1, hvac_action="cooling")) is True
        assert j.record_reading(rd(2, hvac_mode="heat")) is True
        assert j.record_reading(rd(3, fan_on=True)) is True
        assert j.record_reading(rd(4, contacts_open=True)) is True

    def test_indoor_becoming_unreadable_is_stored(self):
        j = PromiseJournal()
        j.record_reading(rd(0))
        assert j.record_reading(rd(1, indoor_f=None)) is True

    def test_half_degree_indoor_move_is_stored_but_small_one_is_not(self):
        j = PromiseJournal()
        j.record_reading(rd(0))
        assert j.record_reading(rd(1, indoor_f=72.3)) is False
        assert j.record_reading(rd(2, indoor_f=72.6)) is True

    def test_five_minute_heartbeat_is_stored(self):
        j = PromiseJournal()
        j.record_reading(rd(0))
        assert j.record_reading(rd(5)) is True

    def test_setpoint_change_is_stored(self):
        j = PromiseJournal()
        j.record_reading(rd(0))
        assert j.record_reading(rd(1, setpoint_f=70.0)) is True

    @pytest.mark.parametrize("field", ["setpoint_low_f", "setpoint_high_f"])
    def test_dual_setpoint_change_is_stored_and_noise_is_not(self, field):
        """Issue #1260: P7's deadband exception reads the heat_cool setpoints, so a real change must be stored."""
        j = PromiseJournal()
        j.record_reading(rd(0, **{field: 68.0}))
        assert j.record_reading(rd(1, **{field: 68.2})) is False  # under the 0.5 F store threshold
        assert j.record_reading(rd(2, **{field: 66.0})) is True
        assert j.record_reading(rd(3, **{field: None})) is True  # becoming unknown is a change too


class TestCapsAndRetention:
    def test_every_store_is_capped(self):
        j = PromiseJournal()
        for i in range(MAX_READINGS * 3):
            j.record_reading(rd(i * 0.001, hvac_action="cooling" if i % 2 else "idle"))
        for i in range(MAX_COMMANDS * 3):
            j.record_command(Command(ts=at(i * 0.001), target="thermostat", service="set_hvac_mode"))
        for i in range(MAX_MARKS * 3):
            j.on_event(at(i * 0.001), "override_detected", {"detected_mode": "heat"})
        for i in range(MAX_SEEN_KEYS * 3):
            j.is_new(f"k{i}")
        sizes = j.sizes()
        assert sizes["readings"] <= MAX_READINGS
        assert sizes["commands"] <= MAX_COMMANDS
        assert sizes["marks"] <= MAX_MARKS
        assert sizes["episode_marks"] <= MAX_EPISODE_MARKS
        assert sizes["seen"] <= MAX_SEEN_KEYS

    def test_window_drops_evidence_older_than_three_hours(self):
        j = PromiseJournal()
        j.record_reading(rd(0))
        j.record_command(Command(ts=at(0), target="fan", service="turn_on", fan_on=True))
        j.on_event(at(0), "nat_vent_activated")
        later = at(0) + JOURNAL_MAX_AGE + timedelta(minutes=1)
        w = j.window(later)
        assert w.readings == () and w.commands == () and w.marks == ()

    def test_open_episode_survives_the_three_hour_window(self):
        j = PromiseJournal()
        j.on_event(at(0), "fan_manual_override", {"fan_before": "off", "fan_after": "on", "remote_timer_hours": 12})
        w = j.window(at(10 * 60))
        assert len(w.episodes) == 1 and w.episodes[0].end_ts is None

    def test_closed_episode_ages_out(self):
        j = PromiseJournal()
        j.on_event(at(0), "fan_manual_override", {"fan_after": "on"})
        j.on_event(at(30), "grace_expired", {})
        assert len(j.window(at(60)).episodes) == 1
        assert j.window(at(30 + 4 * 60)).episodes == ()

    def test_reset_is_a_clean_slate_but_keeps_dedup_memory(self):
        j = PromiseJournal()
        j.record_reading(rd(0))
        j.on_event(at(0), "override_detected", {"detected_mode": "heat"})
        assert j.is_new("a") is True
        j.reset()
        w = j.window(at(1))
        assert w.latest is None and w.readings == () and w.marks == () and w.episodes == ()
        assert j.is_new("a") is False

    def test_dedup_lru_forgets_oldest_only(self):
        j = PromiseJournal()
        for i in range(MAX_SEEN_KEYS + 5):
            assert j.is_new(f"k{i}") is True
        assert j.is_new("k0") is True  # evicted, so new again
        assert j.is_new(f"k{MAX_SEEN_KEYS + 4}") is False  # recent one still remembered


class TestMarks:
    def test_unmapped_event_is_ignored(self):
        assert mark_from_event(at(0), "weather_update", {"a": 1}) is None

    def test_mapped_event_keeps_only_listed_keys(self):
        m = mark_from_event(at(0), "override_cleared", {"was_mode": "heat", "reason": "bedtime", "secret": "x"})
        assert dict(m.attrs) == {"was_mode": "heat", "old_setpoint_f": None, "reason": "bedtime", "continues": None}

    def test_non_scalar_payload_values_are_stringified(self):
        m = mark_from_event(at(0), "override_detected", {"detected_mode": ["a"], "source": {"b": 1}})
        assert all(isinstance(v, str) for _, v in m.attrs)

    def test_non_dict_payload_does_not_raise(self):
        assert mark_from_event(at(0), "override_detected", None).kind == "override_detected"

    def test_every_mapped_event_type_is_a_real_ca_event(self):
        """A renamed event must fail loudly here instead of silently blinding promise 4."""
        from pathlib import Path

        import custom_components.climate_advisor as pkg

        root = Path(pkg.__file__).parent
        source = (root / "automation.py").read_text(encoding="utf-8") + (root / "coordinator.py").read_text(
            encoding="utf-8"
        )
        missing = sorted(t for t in EVENT_MARK_MAP if f'"{t}"' not in source)
        assert missing == [], missing


class TestEpisodes:
    def _marks(self, *events):
        return [mark_from_event(at(m), t, p) for m, t, p in events]

    def test_override_with_grace_expires_naturally(self):
        marks = self._marks(
            (0, "override_detected", {"detected_mode": "heat"}),
            (0, "grace_started", {"source": "manual", "trigger": "manual", "duration_seconds": 1800}),
            (30, "grace_expired", {}),
        )
        (ep,) = derive_episodes(marks)
        assert (ep.target, ep.kind, ep.value) == ("thermostat", "override", "heat")
        assert ep.grace_end_ts == at(30) and ep.end_ts == at(30) and ep.end_reason == "grace_expired"

    def test_override_remembers_the_occupants_setpoint_from_readings(self):
        marks = self._marks((0, "override_detected", {"detected_mode": "heat"}))
        (ep,) = derive_episodes(marks, [rd(0.1, setpoint_f=71.0)])
        assert ep.setpoint_f == 71.0

    def test_fan_off_grace_is_its_own_episode(self):
        marks = self._marks((0, "grace_started", {"trigger": "fan_off", "duration_seconds": 600}))
        (ep,) = derive_episodes(marks)
        assert (ep.target, ep.kind, ep.value) == ("fan", "fan_off", "off")
        assert ep.end_ts is None

    def test_cleared_with_reason_closes_thermostat_and_fan_override(self):
        marks = self._marks(
            (0, "override_detected", {"detected_mode": "heat"}),
            (1, "fan_manual_override", {"fan_after": "on"}),
            (10, "override_cleared", {"was_mode": "heat", "reason": "bedtime"}),
        )
        eps = derive_episodes(marks)
        assert {e.end_reason for e in eps} == {"bedtime"}

    def test_fan_only_clear_leaves_thermostat_override_open(self):
        marks = self._marks(
            (0, "override_detected", {"detected_mode": "heat"}),
            (1, "fan_manual_override", {"fan_after": "on"}),
            (10, "override_cleared", {"was_mode": None, "reason": "bedtime"}),
        )
        by_target = {e.target: e for e in derive_episodes(marks)}
        assert by_target["thermostat"].end_ts is None
        assert by_target["fan"].end_reason == "bedtime"

    def test_second_override_supersedes_the_first(self):
        marks = self._marks(
            (0, "override_detected", {"detected_mode": "heat"}),
            (20, "override_detected", {"detected_mode": "cool"}),
        )
        first, second = derive_episodes(marks)
        assert first.end_reason == "superseded" and second.end_ts is None

    def test_superseded_cancel_without_a_replacement_is_not_listed_as_superseded(self):
        marks = self._marks(
            (0, "override_detected", {"detected_mode": "heat"}),
            (20, "grace_cancelled", {"reason": "superseded", "protects_override": True}),
        )
        (ep,) = derive_episodes(marks)
        assert ep.end_reason == "superseded_unreplaced"

    def test_superseded_cancel_with_replacement_within_five_seconds_is_superseded(self):
        marks = self._marks(
            (0, "override_detected", {"detected_mode": "heat"}),
            (20, "grace_cancelled", {"reason": "superseded", "protects_override": True}),
        )
        marks.append(mark_from_event(at(20) + timedelta(seconds=2), "override_detected", {"detected_mode": "cool"}))
        first, _second = derive_episodes(marks)
        assert first.end_reason == "superseded"

    def test_unrelated_grace_cancelled_does_not_end_an_override(self):
        """A door/window-resume grace being cancelled says nothing about the occupant's override."""
        marks = self._marks(
            (0, "override_detected", {"detected_mode": "heat"}),
            (5, "grace_cancelled", {"reason": "door_closed", "trigger": "window_close", "protects_override": False}),
        )
        (ep,) = derive_episodes(marks)
        assert ep.end_ts is None

    def test_cancelled_grace_that_protected_the_override_ends_it_with_its_reason(self):
        marks = self._marks(
            (0, "override_detected", {"detected_mode": "heat"}),
            (5, "grace_cancelled", {"reason": "bedtime", "protects_override": True}),
        )
        (ep,) = derive_episodes(marks)
        assert ep.end_reason == "bedtime"

    # ── Issue #1205: a fan request (fan-off hold, occupant fan run) ends at its OWN deadline ──
    def test_1205_fan_off_hold_survives_its_grace_being_superseded_and_a_thermostat_grace_expiring(self):
        marks = self._marks(
            (0, "grace_started", {"trigger": "fan_off", "source": "manual", "duration_seconds": 1800}),
            (
                5,
                "grace_cancelled",
                {"reason": "superseded", "trigger": "fan_off", "protects_override": False, "continues": True},
            ),
            (16, "grace_expired", {}),  # the thermostat override's grace: not the hold's end
        )
        (ep,) = derive_episodes(marks)
        assert ep.kind == "fan_off" and ep.end_ts is None and ep.grace_end_ts == at(30)

    def test_1205_fan_off_hold_closes_at_its_deadline_when_a_later_mark_proves_it_passed(self):
        marks = self._marks(
            (0, "grace_started", {"trigger": "fan_off", "source": "manual", "duration_seconds": 1800}),
            (5, "grace_cancelled", {"reason": "superseded", "trigger": "fan_off", "continues": True}),
            (45, "grace_expired", {}),
        )
        (ep,) = derive_episodes(marks)
        assert ep.end_ts == at(30) and ep.end_reason == "grace_expired"

    def test_1205_fan_side_cancel_of_the_hold_still_closes_it(self):
        marks = self._marks(
            (0, "grace_started", {"trigger": "fan_off", "source": "manual", "duration_seconds": 1800}),
            (5, "grace_cancelled", {"reason": "user_cancel_override", "trigger": "fan_off", "continues": False}),
        )
        (ep,) = derive_episodes(marks)
        assert ep.end_reason == "user_cancel_override"

    def test_1205_pre_fix_superseded_fan_off_cancel_without_continues_still_closes(self):
        marks = self._marks(
            (0, "grace_started", {"trigger": "fan_off", "source": "manual", "duration_seconds": 1800}),
            (5, "grace_cancelled", {"reason": "superseded", "trigger": "fan_off"}),
        )
        (ep,) = derive_episodes(marks)
        assert ep.end_ts == at(5)

    # (the fan-run episode over the REAL engine event stream: tests/test_stale_fan_marker_1205.py::test_journal_*)

    def test_non_occupant_grace_start_does_not_set_the_overrides_protected_window(self):
        marks = self._marks(
            (0, "override_detected", {"detected_mode": "heat"}),
            (1, "grace_started", {"source": "automation", "trigger": "window_close", "duration_seconds": 900}),
        )
        (ep,) = derive_episodes(marks)
        assert ep.grace_end_ts is None

    def test_stuck_grace_recovery_and_restart_have_their_own_reasons(self):
        marks = self._marks(
            (0, "override_detected", {"detected_mode": "heat"}),
            (5, "stuck_grace_recovered", {"reason": "x"}),
            (6, "fan_manual_override", {"fan_after": "on"}),
            (7, "system_restarted", {}),
        )
        reasons = [e.end_reason for e in derive_episodes(marks)]
        assert reasons == ["stuck_grace_recovered", "restart"]


class TestPromiseStats:
    ALLOWED = frozenset({"settling_flip", "ac_whf_mutex"})

    def test_non_dict_is_dropped(self):
        assert sanitize_promise_stats(["x"], self.ALLOWED) == {}

    def test_unknown_keys_and_bad_values_are_dropped(self):
        raw = {
            "settling_flip": {"hits": 3, "first_ts": "a", "last_ts": "b"},
            "evil": {"hits": 1},
            "ac_whf_mutex": {"hits": "many"},
        }
        assert sanitize_promise_stats(raw, self.ALLOWED) == {
            "settling_flip": {"hits": 3, "first_ts": "a", "last_ts": "b"}
        }

    def test_bool_hits_rejected(self):
        assert sanitize_promise_stats({"settling_flip": {"hits": True}}, self.ALLOWED) == {}

    def test_bump_counts_and_tracks_first_and_last(self):
        stats: dict = {}
        bump_promise_stats(stats, "settling_flip", at(0))
        bump_promise_stats(stats, "settling_flip", at(5))
        assert stats["settling_flip"]["hits"] == 2
        assert stats["settling_flip"]["first_ts"] == at(0).isoformat()
        assert stats["settling_flip"]["last_ts"] == at(5).isoformat()


class TestExcusedStatsRoundTrip:
    """Issue #1260: excused time survives save -> JSON -> restore, apart from hits; bad rows are dropped."""

    ALLOWED = frozenset({"comfort_kept", "settling_flip"})

    def test_save_and_restore_keep_the_excused_counts(self):
        import json  # noqa: PLC0415

        from custom_components.climate_advisor.promise_journal import bump_promise_excused  # noqa: PLC0415

        stats: dict = {}
        bump_promise_excused(stats, "comfort_kept", "thermostat_deadband", at(10), 10.0, new_run=True)
        bump_promise_excused(stats, "comfort_kept", "thermostat_deadband", at(40), 30.0, new_run=False)
        bump_promise_excused(stats, "comfort_kept", "thermostat_deadband", at(200), 20.0, new_run=True)
        bump_promise_stats(stats, "settling_flip", at(5))
        restored = sanitize_promise_stats(json.loads(json.dumps(stats)), self.ALLOWED)
        assert restored == stats
        row = restored["comfort_kept"]["excused"]["thermostat_deadband"]
        assert row == {"runs": 2, "minutes": 60.0, "last_ts": at(200).isoformat()}
        assert restored["comfort_kept"]["hits"] == 0  # excused-only: never a hit
        assert "excused" not in restored["settling_flip"]

    def test_a_restored_entry_keeps_counting_from_where_it_left_off(self):
        from custom_components.climate_advisor.promise_journal import bump_promise_excused  # noqa: PLC0415

        stats: dict = {}
        bump_promise_excused(stats, "comfort_kept", "thermostat_deadband", at(10), 10.0, new_run=True)
        restored = sanitize_promise_stats(stats, self.ALLOWED)
        bump_promise_excused(restored, "comfort_kept", "thermostat_deadband", at(20), 5.0, new_run=True)
        assert restored["comfort_kept"]["excused"]["thermostat_deadband"]["runs"] == 2
        assert restored["comfort_kept"]["excused"]["thermostat_deadband"]["minutes"] == 15.0

    def test_malformed_excused_rows_are_dropped_and_the_hits_kept(self):
        good = {"runs": 1, "minutes": 5.0, "last_ts": "t"}
        raw = {
            "comfort_kept": {
                "hits": 3,
                "first_ts": "a",
                "last_ts": "b",
                "excused": {
                    "thermostat_deadband": good,
                    "bool_runs": {"runs": True, "minutes": 1.0},
                    "neg_runs": {"runs": -1, "minutes": 1.0},
                    "str_minutes": {"runs": 1, "minutes": "lots"},
                    "neg_minutes": {"runs": 1, "minutes": -2.0},
                    "nan_minutes": {"runs": 1, "minutes": float("nan")},
                    "inf_minutes": {"runs": 1, "minutes": float("inf")},
                    "not_a_row": 7,
                    5: good,
                },
            },
        }
        out = sanitize_promise_stats(raw, self.ALLOWED)
        assert out == {
            "comfort_kept": {"hits": 3, "first_ts": "a", "last_ts": "b", "excused": {"thermostat_deadband": good}}
        }
        assert sanitize_promise_stats({"comfort_kept": {"hits": 1, "excused": ["x"]}}, self.ALLOWED) == {
            "comfort_kept": {"hits": 1, "first_ts": None, "last_ts": None}
        }
        bad_ts = {
            "comfort_kept": {"hits": 0, "excused": {"thermostat_deadband": {"runs": 1, "minutes": 2, "last_ts": 9}}}
        }
        assert sanitize_promise_stats(bad_ts, self.ALLOWED)["comfort_kept"]["excused"]["thermostat_deadband"] == {
            "runs": 1,
            "minutes": 2.0,
            "last_ts": None,
        }

    def test_the_number_of_excused_reasons_is_bounded(self):
        from custom_components.climate_advisor.promise_journal import MAX_EXCUSED_REASONS  # noqa: PLC0415

        rows = {f"reason_{i}": {"runs": 1, "minutes": 1.0, "last_ts": None} for i in range(MAX_EXCUSED_REASONS + 5)}
        out = sanitize_promise_stats({"comfort_kept": {"hits": 0, "excused": rows}}, self.ALLOWED)
        assert len(out["comfort_kept"]["excused"]) == MAX_EXCUSED_REASONS
        assert list(out["comfort_kept"]["excused"]) == [f"reason_{i}" for i in range(MAX_EXCUSED_REASONS)]

    def test_the_excused_rows_cannot_smuggle_an_unknown_promise_in(self):
        raw = {"evil": {"hits": 0, "excused": {"thermostat_deadband": {"runs": 1, "minutes": 1.0}}}}
        assert sanitize_promise_stats(raw, self.ALLOWED) == {}


class TestObserveOnlySealedNotices:
    """Issue #1144: the observe-only notice opens a span that the fan stopping or a window opening closes."""

    def test_notice_opens_a_span_and_the_fan_stopping_closes_it(self):
        j = PromiseJournal()
        j.on_event(at(1), "whf_sealed_observe_notified", {})
        j.record_reading(Reading(ts=at(2), fan_on=True, contacts_open=False))
        assert j.window(at(3)).observe_notices == ((at(1), None),)
        j.record_reading(Reading(ts=at(4), fan_on=False, contacts_open=False))
        assert j.window(at(5)).observe_notices == ((at(1), at(4)),)

    def test_a_window_opening_also_closes_it_and_a_second_notice_starts_a_new_span(self):
        j = PromiseJournal()
        j.on_event(at(1), "whf_sealed_observe_notified", {})
        j.record_reading(Reading(ts=at(2), fan_on=True, contacts_open=True))
        j.on_event(at(10), "whf_sealed_observe_notified", {})
        spans = j.window(at(11)).observe_notices
        assert spans == ((at(1), at(2)), (at(10), None))

    def test_the_span_survives_the_window_sliding_past_it(self):
        j = PromiseJournal()
        j.on_event(at(1), "whf_sealed_observe_notified", {})
        later = at(1) + timedelta(hours=5)
        assert j.window(later).observe_notices == ((at(1), None),)


class TestIssue1325TwoChangesInsideFiveMinutes:
    """Occupant: Cool 74, then Cool 72 two minutes later (or Heat 70 then Heat 72), on a normal day (Issue #1325).

    Real event order (30 s confirm, 900 s grace): the supersede ``grace_cancelled`` of the FIRST change's grace lands
    at the second change's confirm delay, after the second ``override_detected``. Both changes emit
    ``override_detected`` (the engine's dedup suppresses only an identical repeat). The first episode ends
    ``superseded``; the second stays open and is protected to its own grace end.
    """

    @staticmethod
    def _events(mode: str, first: float, second: float, source: str = "setpoint", *, second_detected: bool = True):
        ev = [
            (0, "override_detected", {"detected_mode": mode, "source": source, "new_setpoint_f": first}),
            (0.5, "grace_started", {"source": "manual", "duration_seconds": 900, "trigger": "override_confirmed"}),
            (0.5, "override_confirmed", {"mode": mode, "source": source}),
        ]
        if second_detected:
            ev.append((2, "override_detected", {"detected_mode": mode, "source": source, "new_setpoint_f": second}))
        ev += [
            (
                2.5,
                "grace_cancelled",
                {"reason": "superseded", "trigger": "override_confirmed", "protects_override": True},
            ),
            (2.5, "grace_started", {"source": "manual", "duration_seconds": 900, "trigger": "override_confirmed"}),
            (2.5, "override_confirmed", {"mode": mode, "source": source}),
        ]
        return ev

    @staticmethod
    def _journal(events, first: float, second: float) -> PromiseJournal:
        j = PromiseJournal()
        for m, t, p in events:
            j.on_event(at(m), t, p)
        j.record_reading(rd(0.1, hvac_mode="cool", setpoint_f=first))
        j.record_reading(rd(2.1, hvac_mode="cool", setpoint_f=second))
        return j

    @staticmethod
    def _p4(j: PromiseJournal, now_min: float = 10):
        from custom_components.climate_advisor.invariant_watchdog import PromiseConfig, check_p4_override_respected

        return check_p4_override_respected(j.window(at(now_min)), PromiseConfig())

    @pytest.mark.parametrize(
        "mode,first,second,source",
        [("cool", 74.0, 72.0, "setpoint"), ("heat", 70.0, 72.0, "setpoint"), ("cool", 74.0, 72.0, "pause")],
    )
    def test_first_superseded_second_protected_to_its_own_grace_end_no_p4_hit(self, mode, first, second, source):
        ev = self._events(mode, first, second, source)
        first_ep, second_ep = derive_episodes([mark_from_event(at(m), t, p) for m, t, p in ev])
        assert first_ep.end_reason == "superseded" and first_ep.end_ts == at(2)
        assert second_ep.start_ts == at(2) and second_ep.end_ts is None
        assert second_ep.grace_end_ts == at(2.5) + timedelta(seconds=900)
        assert self._p4(self._journal(ev, first, second)) == []

    def test_the_second_change_is_protected_a_ca_write_inside_its_grace_is_a_p4_hit(self):
        """Protection is real: CA reversing the second change inside ITS grace (after the first one's end) is caught."""
        ev = self._events("cool", 74.0, 72.0)
        j = self._journal(ev, 74.0, 72.0)
        j.record_command(Command(ts=at(14), target="thermostat", service="set_temperature", setpoint_f=76.0))
        assert [h.promise for h in self._p4(j, 15)] == ["override_respected"]

    def test_without_the_second_override_detected_the_false_early_end_hit_appears(self):
        """What the swallowed event produced (pins why the second emit is needed): 'ended ... min early'."""
        ev = self._events("cool", 74.0, 72.0, second_detected=False)
        hits = self._p4(self._journal(ev, 74.0, 72.0))
        assert [h.promise for h in hits] == ["override_respected"] and "min early" in hits[0].detail

    def test_control_a_ca_cancel_of_the_new_changes_own_grace_still_closes_it_and_p4_fires(self):
        ev = self._events("cool", 74.0, 72.0)
        ev.append((6, "grace_cancelled", {"reason": "superseded", "protects_override": True}))
        eps = derive_episodes([mark_from_event(at(m), t, p) for m, t, p in ev])
        assert eps[1].end_ts == at(6) and eps[1].end_reason == "superseded_unreplaced"
        hits = self._p4(self._journal(ev, 74.0, 72.0))
        assert [h.promise for h in hits] == ["override_respected"] and "min early" in hits[0].detail

    def test_control_b_old_grace_cancelled_new_grace_never_starts_ca_write_is_still_caught(self):
        ev = [e for e in self._events("cool", 74.0, 72.0) if not (e[0] == 2.5 and e[1] != "grace_cancelled")]
        eps = derive_episodes([mark_from_event(at(m), t, p) for m, t, p in ev])
        assert eps[1].end_ts is None, "the newer change stays open; the old grace's cancel was never about it"
        j = self._journal(ev, 74.0, 72.0)
        j.record_command(Command(ts=at(8), target="thermostat", service="set_temperature", setpoint_f=76.0))
        assert [h.promise for h in self._p4(j, 9)] == ["override_respected"]


class TestIssue1325FanMirrorRemoteTimerChange:
    """Occupant (whole-house fan with the RF remote): presses the fan on with a 1 h timer, then two minutes later picks
    a 4 h timer on the remote. The fan keeps running on the occupant's 4 h request.

    Real engine order (traced through the HA tier, ``tests_ha/test_second_change_p4_1325.py``): the NEW
    ``fan_manual_override`` is emitted first, then the ``grace_cancelled`` (``superseded``, ``continues`` False) of
    the OLD press's grace, then the new ``grace_started``. Mirror of the #1325 thermostat rule: that cancel is about
    the old grace, which never protected the newer press, so the newer press stays open with its own grace end
    instead of being closed ``superseded`` at the instant it started (which left a CA fan-off during the 4 h run
    unchecked).
    """

    @staticmethod
    def _events():
        return [
            (0, "fan_manual_override", {"fan_after": "on", "remote_timer_hours": 1.0}),
            (0, "grace_started", {"source": "manual", "duration_seconds": 3600, "trigger": "fan_manual_override"}),
            (2, "fan_manual_override", {"fan_after": "on", "remote_timer_hours": 4.0}),
            (
                2,
                "grace_cancelled",
                {
                    "reason": "superseded",
                    "trigger": "fan_manual_override",
                    "protects_override": True,
                    "continues": False,
                },
            ),
            (2, "grace_started", {"source": "manual", "duration_seconds": 14400, "trigger": "fan_manual_override"}),
        ]

    @staticmethod
    def _p4(events, *commands, now_min: float = 30):
        from custom_components.climate_advisor.invariant_watchdog import PromiseConfig, check_p4_override_respected

        j = PromiseJournal()
        for m, t, p in events:
            j.on_event(at(m), t, p)
        for c in commands:
            j.record_command(c)
        return check_p4_override_respected(j.window(at(now_min)), PromiseConfig())

    def test_newer_press_stays_open_with_its_own_grace_end(self):
        first, second = derive_episodes([mark_from_event(at(m), t, p) for m, t, p in self._events()])
        assert first.end_reason == "superseded" and first.end_ts == at(2)
        assert second.start_ts == at(2) and second.end_ts is None
        assert second.grace_end_ts == at(2) + timedelta(seconds=14400)

    def test_a_ca_fan_off_during_the_newer_press_is_a_p4_hit(self):
        off = Command(ts=at(20), target="fan", service="turn_off", fan_on=False)
        assert [h.promise for h in self._p4(self._events(), off)] == ["override_respected"]

    def test_no_ca_write_no_p4_hit(self):
        assert self._p4(self._events()) == []

    def test_control_a_cancel_of_the_newer_presss_own_grace_still_closes_it(self):
        ev = self._events() + [(10, "grace_cancelled", {"reason": "superseded", "protects_override": True})]
        eps = derive_episodes([mark_from_event(at(m), t, p) for m, t, p in ev])
        assert eps[1].end_ts == at(10) and eps[1].end_reason == "superseded_unreplaced"

    def test_regression_on_off_on_real_order_keeps_the_newer_press_open(self):
        """Real order traced in both fan modes (wall switch / thermostat blower): on, off a minute later, on again."""
        ev = [
            (0, "fan_manual_override", {"fan_after": "on"}),
            (0, "grace_started", {"source": "manual", "duration_seconds": 1800, "trigger": "fan_manual_override"}),
            (
                1,
                "grace_cancelled",
                {
                    "reason": "superseded",
                    "trigger": "fan_manual_override",
                    "protects_override": True,
                    "continues": False,
                },
            ),
            (1, "grace_started", {"source": "manual", "duration_seconds": 1800, "trigger": "fan_off"}),
            (2, "fan_manual_override", {"fan_after": "on"}),
            (
                2,
                "grace_cancelled",
                {"reason": "superseded", "trigger": "fan_off", "protects_override": False, "continues": False},
            ),
            (2, "grace_started", {"source": "manual", "duration_seconds": 1800, "trigger": "fan_manual_override"}),
        ]
        eps = derive_episodes([mark_from_event(at(m), t, p) for m, t, p in ev])
        assert [(e.kind, e.end_reason) for e in eps] == [
            ("override", "superseded"),
            ("fan_off", "superseded"),
            ("override", None),
        ]
        assert eps[2].grace_end_ts == at(2) + timedelta(seconds=1800)
