"""Tests for the household-promise evidence journal (Issue #1128, Step 3).

The journal is a pure leaf: these tests drive the real class and assert on what comes out of
``window()``; nothing re-implements journal logic in the test body.
"""

from __future__ import annotations

import sys
from datetime import UTC, datetime, timedelta

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
        assert dict(m.attrs) == {"was_mode": "heat", "old_setpoint_f": None, "reason": "bedtime"}

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
