"""Tests for Issue #869 — ``ClimateAdvisorCoordinator._stabilize_nat_vent_cutoff_reason()``.

Root cause (see coordinator.py's docstring on the method under test):
``compute_nat_vent_plan()`` races two independent crossing scans against each
other (``outdoor_crossing`` vs. ``floor_crossing``) and reports whichever comes
first as ``nat_vent_cutoff``/``nat_vent_cutoff_reason``. On a knife-edge day
where both crossings land within minutes of each other, small sensor-reading
shifts between the coordinator's frequent recomputes (event-driven, not just
the 30-min timer) flip which crossing wins — flapping the reported reason and
the wording shown to the occupant on the briefing/status cards.

``_stabilize_nat_vent_cutoff_reason()`` fixes this by requiring a *new* reason
to read consistently for ``NAT_VENT_CUTOFF_REASON_SUSTAIN_S`` (90s) before it's
accepted, via the shared ``confirmed_transition.py`` primitive. Every other key
in the raw plan dict (``comfort_floor_crossing_time``, ``ceiling_breach_time``,
etc.) must pass through fresh every cycle regardless — freezing those too would
silently stall ``ode_floor_guard.py``'s safety-relevant heating-escalation
input.

Per this project's CLAUDE.md testing doctrine, these tests invoke the real
``_stabilize_nat_vent_cutoff_reason()`` method directly (via
``object.__new__()`` + ``types.MethodType()``, the established coordinator
partial-instantiation pattern — see test_contact_status.py's
``_make_real_coordinator``, test_daily_record_accuracy.py's
``_get_coordinator_class``) rather than re-implementing the sustain-confirm
logic in the test body.
"""

from __future__ import annotations

import importlib
import sys
import types
from datetime import UTC, datetime, timedelta
from unittest.mock import patch

# ── HA module stubs (must happen before importing climate_advisor) ──
if "homeassistant" not in sys.modules:
    from conftest import _install_ha_stubs

    _install_ha_stubs()

from custom_components.climate_advisor.const import NAT_VENT_CUTOFF_REASON_SUSTAIN_S


def _get_coordinator_class():
    """Return the current ClimateAdvisorCoordinator class.

    test_occupancy.py deletes custom_components.climate_advisor.coordinator from
    sys.modules and re-imports it. Always importing fresh via importlib ensures
    the bound method's __globals__ match whatever module is currently patched
    (see test_daily_record_accuracy.py's identical helper).
    """
    mod = importlib.import_module("custom_components.climate_advisor.coordinator")
    return mod.ClimateAdvisorCoordinator


_DT_PATCH_TARGET = "custom_components.climate_advisor.coordinator.dt_util.now"

_T0 = datetime(2026, 9, 6, 8, 0, 0, tzinfo=UTC)


def _make_coord(nat_vent_plan: dict | None, candidate: str | None = None, candidate_since: datetime | None = None):
    """Bare coordinator bound only to _stabilize_nat_vent_cutoff_reason(), with
    the specific self.* attributes that method reads/writes set directly."""
    coord_cls = _get_coordinator_class()
    coord = object.__new__(coord_cls)
    coord._nat_vent_plan = nat_vent_plan
    coord._nat_vent_cutoff_reason_candidate = candidate
    coord._nat_vent_cutoff_reason_candidate_since = candidate_since
    coord._stabilize_nat_vent_cutoff_reason = types.MethodType(coord_cls._stabilize_nat_vent_cutoff_reason, coord)
    return coord


def _plan(
    reason: str | None,
    cutoff: datetime | None,
    *,
    comfort_floor_crossing_time: datetime | None = None,
    ceiling_breach_time: datetime | None = None,
    precool_start_time: datetime | None = None,
    any_nat_vent_window: bool = True,
    evening_open_time: datetime | None = None,
) -> dict:
    """Build a nat_vent_plan-shaped dict with distinguishable values for every
    key, so tests can prove non-cutoff fields pass through fresh/unfrozen."""
    return {
        "nat_vent_cutoff": cutoff,
        "nat_vent_cutoff_reason": reason,
        "comfort_floor_crossing_time": comfort_floor_crossing_time,
        "ceiling_breach_time": ceiling_breach_time,
        "precool_start_time": precool_start_time,
        "any_nat_vent_window": any_nat_vent_window,
        "evening_open_time": evening_open_time,
    }


class TestBootstrap:
    def test_first_call_accepts_raw_plan_regardless_of_reason(self):
        coord = _make_coord(nat_vent_plan=None)
        raw = _plan("comfort_floor", _T0)
        with patch(_DT_PATCH_TARGET, return_value=_T0):
            result = coord._stabilize_nat_vent_cutoff_reason(raw)
        assert result is raw or result == raw
        assert result["nat_vent_cutoff_reason"] == "comfort_floor"
        assert result["nat_vent_cutoff"] == _T0


class TestUnchangedReason:
    def test_same_reason_accepted_immediately_and_candidate_cleared(self):
        previous = _plan("outdoor_rise", _T0)
        coord = _make_coord(
            nat_vent_plan=previous,
            candidate="outdoor_rise",
            candidate_since=_T0 - timedelta(seconds=10),
        )
        new_cutoff = _T0 + timedelta(minutes=30)
        raw = _plan("outdoor_rise", new_cutoff, comfort_floor_crossing_time=_T0 + timedelta(minutes=5))
        with patch(_DT_PATCH_TARGET, return_value=_T0 + timedelta(minutes=30)):
            result = coord._stabilize_nat_vent_cutoff_reason(raw)
        assert result["nat_vent_cutoff_reason"] == "outdoor_rise"
        assert result["nat_vent_cutoff"] == new_cutoff
        # Candidate state cleared even though nothing was being tracked.
        assert coord._nat_vent_cutoff_reason_candidate is None
        assert coord._nat_vent_cutoff_reason_candidate_since is None


class TestAppearDisappearNoSustainRequired:
    def test_reason_appears_from_none_accepted_immediately(self):
        previous = _plan(None, None)
        coord = _make_coord(nat_vent_plan=previous)
        raw = _plan("outdoor_rise", _T0)
        # Called back-to-back with zero elapsed time — still accepted immediately.
        with patch(_DT_PATCH_TARGET, return_value=_T0):
            result = coord._stabilize_nat_vent_cutoff_reason(raw)
        assert result["nat_vent_cutoff_reason"] == "outdoor_rise"
        assert result["nat_vent_cutoff"] == _T0
        assert coord._nat_vent_cutoff_reason_candidate is None
        assert coord._nat_vent_cutoff_reason_candidate_since is None

    def test_reason_disappears_to_none_accepted_immediately(self):
        previous = _plan("comfort_floor", _T0)
        coord = _make_coord(nat_vent_plan=previous)
        raw = _plan(None, None)
        with patch(_DT_PATCH_TARGET, return_value=_T0):
            result = coord._stabilize_nat_vent_cutoff_reason(raw)
        assert result["nat_vent_cutoff_reason"] is None
        assert result["nat_vent_cutoff"] is None
        assert coord._nat_vent_cutoff_reason_candidate is None
        assert coord._nat_vent_cutoff_reason_candidate_since is None


class TestKnifeEdgeFlipNotYetConfirmed:
    """The critical blast-radius assertions: while a reason flip is unconfirmed,
    ONLY nat_vent_cutoff/nat_vent_cutoff_reason are held back — every other key
    passes through fresh from the raw/new plan."""

    def test_flip_moments_later_holds_old_cutoff_but_passes_through_other_fields(self):
        old_cutoff = _T0
        previous = _plan(
            "comfort_floor",
            old_cutoff,
            comfort_floor_crossing_time=_T0,
            ceiling_breach_time=None,
        )
        coord = _make_coord(nat_vent_plan=previous)

        new_cutoff = _T0 + timedelta(minutes=45)
        new_floor_crossing = _T0 + timedelta(minutes=10)
        new_ceiling_breach = _T0 + timedelta(minutes=200)
        new_precool_start = _T0 + timedelta(minutes=150)
        raw = _plan(
            "outdoor_rise",
            new_cutoff,
            comfort_floor_crossing_time=new_floor_crossing,
            ceiling_breach_time=new_ceiling_breach,
            precool_start_time=new_precool_start,
            any_nat_vent_window=False,
            evening_open_time=_T0 + timedelta(minutes=300),
        )

        # Called moments later — well under the 90s sustain window.
        later = _T0 + timedelta(seconds=5)
        with patch(_DT_PATCH_TARGET, return_value=later):
            result = coord._stabilize_nat_vent_cutoff_reason(raw)

        # Cutoff/reason held at the OLD confirmed values.
        assert result["nat_vent_cutoff_reason"] == "comfort_floor"
        assert result["nat_vent_cutoff"] == old_cutoff

        # Every other key passes through fresh from raw_plan, unchanged.
        assert result["comfort_floor_crossing_time"] == new_floor_crossing
        assert result["ceiling_breach_time"] == new_ceiling_breach
        assert result["precool_start_time"] == new_precool_start
        assert result["any_nat_vent_window"] is False
        assert result["evening_open_time"] == _T0 + timedelta(minutes=300)

        # Candidate state now tracks the not-yet-confirmed new reason.
        assert coord._nat_vent_cutoff_reason_candidate == "outdoor_rise"
        assert coord._nat_vent_cutoff_reason_candidate_since == later


class TestReasonHeldPendingDoesNotAffectOtherFields:
    """Explicit dedicated test (separate from the blast-radius assertion above,
    per the task): comfort_floor_crossing_time — the field ode_floor_guard.py
    depends on for a real safety-relevant decision — keeps updating fresh every
    single cycle even while nat_vent_cutoff_reason sits unconfirmed for several
    consecutive cycles in a row."""

    def test_comfort_floor_crossing_time_updates_every_cycle_while_reason_pending(self):
        previous = _plan("comfort_floor", _T0, comfort_floor_crossing_time=_T0)
        coord = _make_coord(nat_vent_plan=previous)

        crossing_times = [
            _T0 + timedelta(minutes=5),
            _T0 + timedelta(minutes=10),
            _T0 + timedelta(minutes=15),
            _T0 + timedelta(minutes=20),
        ]
        # All calls land within the 90s sustain window (seconds apart), so the
        # reason never confirms across this whole sequence.
        for i, crossing in enumerate(crossing_times):
            now = _T0 + timedelta(seconds=10 * (i + 1))
            raw = _plan("outdoor_rise", _T0 + timedelta(hours=1), comfort_floor_crossing_time=crossing)
            with patch(_DT_PATCH_TARGET, return_value=now):
                result = coord._stabilize_nat_vent_cutoff_reason(raw)
            # Reason/cutoff still held at the original comfort_floor values...
            assert result["nat_vent_cutoff_reason"] == "comfort_floor"
            assert result["nat_vent_cutoff"] == _T0
            # ...but the safety-relevant crossing time is always this cycle's fresh value.
            assert result["comfort_floor_crossing_time"] == crossing


class TestReproducedFlappingSequence:
    """Reproduces the exact observed flapping sequence from the GitHub issue:
    comfort_floor -> outdoor_rise -> comfort_floor within seconds. Since no
    single candidate holds long enough to confirm (each flip restarts the
    sustain clock via resolve_candidate_since()), the displayed/cached reason
    must never actually change throughout the whole burst."""

    def test_flapping_burst_never_changes_displayed_reason(self):
        previous = _plan("comfort_floor", _T0)
        coord = _make_coord(nat_vent_plan=previous)

        # Mirrors a live log timestamp burst: reason flips every ~15-20s, well
        # under NAT_VENT_CUTOFF_REASON_SUSTAIN_S (90s). The raw outdoor_rise
        # candidate always carries the SAME new cutoff (_T0 + 1h) — this is the
        # value under test: it must never leak into the displayed result on any
        # of the held (outdoor_rise) ticks, reproducing the reported 7:00->9:00
        # jump would be exactly this leaking through.
        new_cutoff = _T0 + timedelta(hours=1)
        sequence = [
            (_T0 + timedelta(seconds=15), "outdoor_rise"),
            (_T0 + timedelta(seconds=32), "comfort_floor"),
            (_T0 + timedelta(seconds=48), "outdoor_rise"),
            (_T0 + timedelta(seconds=61), "comfort_floor"),
            (_T0 + timedelta(seconds=79), "outdoor_rise"),
        ]

        # Tracks what the currently-confirmed cutoff is expected to be, updated
        # only on ticks that legitimately change it (the "unchanged reason" fast
        # path below) — not a re-implementation of the sustain logic itself,
        # just the test's own bookkeeping of what "held" should mean at each step.
        expected_confirmed_cutoff = _T0

        for now, reason in sequence:
            raw = _plan(reason, new_cutoff)
            with patch(_DT_PATCH_TARGET, return_value=now):
                result = coord._stabilize_nat_vent_cutoff_reason(raw)
            # The displayed/cached reason never changes from the original
            # comfort_floor value across the entire burst — no single candidate
            # (outdoor_rise) ever holds long enough to confirm before flipping
            # back.
            assert result["nat_vent_cutoff_reason"] == "comfort_floor"
            if reason == "outdoor_rise":
                # A held (not-yet-confirmed) tick: the new candidate's cutoff
                # must NOT leak through — this is the exact 7:00->9:00-style
                # jump the fix exists to prevent.
                assert result["nat_vent_cutoff"] == expected_confirmed_cutoff
            else:
                # A flip-back-to-the-already-displayed-reason tick legitimately
                # takes the "unchanged reason" fast path (accept raw immediately)
                # — the cutoff *timestamp* refreshing here is expected, separate,
                # already-covered behavior (the existing today_high/time-drift
                # threshold in _maybe_regenerate_briefing_for_drift() gates
                # whether that within-reason drift reaches the briefing text).
                # Only the reason string is this method's guarantee on this tick.
                assert result["nat_vent_cutoff"] == new_cutoff
                expected_confirmed_cutoff = new_cutoff
            # coord._nat_vent_plan is never reassigned by the method itself (the
            # caller, _compute_and_cache_nat_vent_plan(), owns that) — simulate
            # the caller's real behavior between cycles so previous_plan reflects
            # what would actually be cached (the stabilized result).
            coord._nat_vent_plan = result


class TestGenuineSustainedChangeAccepted:
    """A candidate reason held consistently across multiple calls whose
    timestamps span >= NAT_VENT_CUTOFF_REASON_SUSTAIN_S is eventually accepted."""

    def test_sustained_change_accepted_once_threshold_crossed(self):
        previous = _plan("comfort_floor", _T0)
        coord = _make_coord(nat_vent_plan=previous)

        new_cutoff = _T0 + timedelta(hours=1)

        # First call: candidate starts, not yet confirmed.
        t1 = _T0 + timedelta(seconds=5)
        raw1 = _plan("outdoor_rise", new_cutoff)
        with patch(_DT_PATCH_TARGET, return_value=t1):
            result1 = coord._stabilize_nat_vent_cutoff_reason(raw1)
        assert result1["nat_vent_cutoff_reason"] == "comfort_floor"
        coord._nat_vent_plan = result1

        # Second call: candidate held, still not confirmed (elapsed < sustain).
        t2 = t1 + timedelta(seconds=NAT_VENT_CUTOFF_REASON_SUSTAIN_S - 10)
        raw2 = _plan("outdoor_rise", new_cutoff)
        with patch(_DT_PATCH_TARGET, return_value=t2):
            result2 = coord._stabilize_nat_vent_cutoff_reason(raw2)
        assert result2["nat_vent_cutoff_reason"] == "comfort_floor"
        coord._nat_vent_plan = result2

        # Third call: elapsed since candidate first appeared (t1) now >= sustain window.
        t3 = t1 + timedelta(seconds=NAT_VENT_CUTOFF_REASON_SUSTAIN_S + 1)
        raw3 = _plan("outdoor_rise", new_cutoff)
        with patch(_DT_PATCH_TARGET, return_value=t3):
            result3 = coord._stabilize_nat_vent_cutoff_reason(raw3)
        assert result3["nat_vent_cutoff_reason"] == "outdoor_rise"
        assert result3["nat_vent_cutoff"] == new_cutoff
        # Candidate state cleared once confirmed.
        assert coord._nat_vent_cutoff_reason_candidate is None
        assert coord._nat_vent_cutoff_reason_candidate_since is None
