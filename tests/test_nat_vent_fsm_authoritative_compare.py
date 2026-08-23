"""Tests for Issue #594 Phase R, Step 3: decision-equivalence for the nat-vent
FSM-authoritative swap (Step 2).

Prior coverage (``test_shadow_engine_pair.py``, ``test_nat_vent_fsm_shadow_wiring.py``)
only proved *state-label* agreement — would the FSM's independently-derived
state match production's flags. This file proves the stronger claim Phase R
actually needs before any live switch is considered: flipping
``AutomationEngine._natvent_fsm_authoritative`` from its default ``False`` to
``True`` produces a byte-for-byte identical ``event_log``/``action_log`` — the
real commanded fan/HVAC actions, not just a derived label — across the full
golden + pending scenario corpus.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

import pytest

from tools.sim_harness.differential import diff_runs
from tools.sim_harness.nat_vent_fsm_authoritative_compare import fsm_authoritative_mutation

_REPO_ROOT = Path(__file__).resolve().parent.parent
_GOLDEN_DIR = _REPO_ROOT / "tools" / "simulations" / "golden"
_PENDING_DIR = _REPO_ROOT / "tools" / "simulations" / "pending"

# Known to exercise soft-start entry + the upgrade-to-full-gate escalation
# (test_nat_vent_soft_start.py's TestSoftStartUpgrade covers the same math in
# isolation) — used as the positive-control probe below.
_PROBE_GOLDEN = "2026-03-28-overnight"


def _load_scenario(path: Path) -> dict:
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def _all_golden_and_pending_scenarios() -> list[Path]:
    paths = [p for p in _GOLDEN_DIR.glob("*.json") if p.name != "MANIFEST.json"]
    paths += [p for p in _PENDING_DIR.glob("*.json") if p.name != "MANIFEST.json"]
    return sorted(paths)


@dataclass(frozen=True)
class _KnownDivergence:
    """A pinned, diagnosed divergence shape for one allowlisted scenario.

    ``event_count``/``action_count`` are the EXACT number of positional
    divergences ``diff_runs()`` must report — not just "non-zero". Issue #698
    Verification review (Defect 4) found the original allowlist accepted ANY
    divergence in these scenarios forever via a bare ``assert not diff.is_clean``,
    so a future, unrelated regression inside one of these 6 scenarios would pass
    silently instead of failing the differential harness. Pinning exact counts
    means any change to the divergence shape (more, fewer, or different) fails
    loudly and must be re-diagnosed, per this project's CLAUDE.md rule on
    changing harness assertion/outcome semantics.
    """

    description: str
    event_count: int
    action_count: int


# Issue #698 (Epic #594 Phase R, Phase 2d, Decision 1): the fast per-tick hard-exit
# check inside nat_vent_temperature_check() was upgraded, while FSM-authoritative,
# from a comfort-floor-only check to the full 5-check decide_nat_vent_exit() chain
# — an intentional, approved behavior change (a session may now end within the fast
# loop's own tick cadence instead of waiting for the next slow-loop cycle). This is
# no longer a byte-for-byte no-op for every scenario in the corpus — the docstring's
# "must be a behavioral no-op" claim above was true through Phase 2c (wiring-only,
# read-authority swaps) but is now only true for scenarios that never reach the fast
# loop's newly-widened exit chain. Each entry below was individually diagnosed (not
# just muted) — see its comment for the specific mechanism, and its pinned
# event_count/action_count for the exact divergence shape (Defect 4, see
# _KnownDivergence docstring above).
_KNOWN_DIVERGENT_SCENARIOS: dict[str, _KnownDivergence] = {
    # Cosmetic-only: the fast-loop exit now stamps event_payload["source"]="temp_check"
    # on whichever exit event fires (same field this call site's pre-existing
    # comfort-floor-only exit already added) — zero action-log divergence, zero
    # occupant-visible timing/outcome change.
    "away_natvent_exits_at_comfort_ceiling": _KnownDivergence(
        description=(
            "AWAY_CEILING now also reachable from the fast loop, producing TWO event "
            "divergences (0 action divergences): (a) cosmetic — payload gains "
            "source=temp_check; (b) a real same-tick reactivation — right after the "
            "away-ceiling exit, the authoritative run also emits a "
            "sensor_opened -> result: natural_ventilation event, i.e. nat-vent re-enters "
            "within the same tick it just exited. The action log stays clean only because "
            "this scenario's fan_device config is 'none' — that is an artifact of this "
            "scenario's config, not evidence the reactivation itself is harmless. Same "
            "root cause as the issue-411/issue-641 same-tick reactivation race below, now "
            "also reachable in away mode. Tracked as its own follow-up: GitHub issue #699 "
            '("Nat-vent same-tick reactivation race after a proactive/away-ceiling exit"). '
            "docs/08-COMPUTATION-REFERENCE.md still describes AWAY_CEILING's own "
            "no-pause/no-grace handling, unchanged; #699 owns fixing the reactivation race."
        ),
        event_count=2,
        action_count=0,
    ),
    "fan_fast_stop_on_outdoor_rise": _KnownDivergence(
        description=(
            "OUTDOOR_RISE fires at the identical timestamp via the fast loop instead of "
            "the slow loop; payload gains source=temp_check; 0 action divergences."
        ),
        event_count=1,
        action_count=0,
    ),
    "nat-vent-outdoor-rises-above-indoor-exit": _KnownDivergence(
        description=(
            "Same OUTDOOR_RISE cosmetic divergence as fan_fast_stop_on_outdoor_rise above; 0 action divergences."
        ),
        event_count=1,
        action_count=0,
    ),
    "warm_day_ceiling_breach_ac_defense": _KnownDivergence(
        description=(
            "CEILING_THRESHOLD fires via the fast loop instead of the slow loop; payload "
            "gains source=temp_check; 0 action divergences."
        ),
        event_count=1,
        action_count=0,
    ),
    # Cosmetic-only as of Issue #696: the same-tick reactivation race this entry used to
    # document is gone. #696 wired the idle-open re-entry path (both its FSM and legacy
    # branches) to actually consult is_reactivation_locked_out() — PROACTIVE_FLOOR already
    # armed _nat_vent_outdoor_exit_time (Issue #641), so with the lockout now enforced at
    # this call site, the baseline (legacy) run no longer reactivates within the same tick
    # either, matching the authoritative run. This also resolves #699's manifestation for
    # this scenario (see GitHub issue #699 for the still-open AWAY_CEILING variant, which
    # #696 did not touch — that exit reason still never arms the lockout).
    "issue-641-whf-proactive-exit-reactivation-flip-flop": _KnownDivergence(
        description=(
            "1 event divergence, 0 action divergences: payload gains source=temp_check "
            "(cosmetic) on the PROACTIVE_FLOOR exit event — k_passive is present and "
            "identical in both runs (Issue #698 Verification Defect 1 fix). No retiming, no "
            "extra reactivation event/action pair — Issue #696 closed the same-tick "
            "reactivation race this entry previously documented."
        ),
        event_count=1,
        action_count=0,
    ),
    # Resolved by Issue #696: this used to be a genuine (bounded) behavior difference —
    # check_natural_vent_conditions()'s idle-open gate never consulted the reactivation
    # lockout, so when it re-ran later in the same tick as a PROACTIVE_FLOOR exit (which
    # DOES arm _nat_vent_outdoor_exit_time, Issue #641) with _actively_paused False
    # (exit paused with _paused_with_hvac_already_off=True), the gate saw indoor
    # unchanged since the exit and re-armed instantly, producing one extra
    # activate/deactivate cycle. #696 wired is_reactivation_locked_out() into that same
    # idle-open gate (both FSM and legacy branches) — the lockout now blocks this
    # same-tick re-check the same way it already blocked the separate
    # paused-by-door-reactivation call site. Bounded, no-occupant-impact race is now
    # eliminated rather than merely bounded. Also resolves this scenario's manifestation
    # of GitHub issue #699 (the AWAY_CEILING variant above remains open — that exit
    # reason still never arms the lockout, #696 did not touch it).
    "issue-411-natvent-floor-exit-consistency": _KnownDivergence(
        description=(
            "1 event divergence, 0 action divergences: payload gains source=temp_check "
            "(cosmetic) on the PROACTIVE_FLOOR exit event. No extra fan activate/deactivate "
            "pair — Issue #696 closed the same-tick reactivation race this entry previously "
            "documented."
        ),
        event_count=1,
        action_count=0,
    ),
}


class TestFsmAuthoritativeEquivalence:
    @pytest.mark.parametrize("scenario_path", _all_golden_and_pending_scenarios(), ids=lambda p: p.stem)
    def test_flag_on_produces_identical_outcome(self, scenario_path: Path) -> None:
        scenario = _load_scenario(scenario_path)
        diff = diff_runs(scenario, mutate_b=fsm_authoritative_mutation, scenario_name=scenario_path.stem)
        assert not diff.a_error and not diff.b_error, (
            f"{scenario_path.stem}: a_error={diff.a_error!r} b_error={diff.b_error!r}"
        )
        if scenario_path.stem in _KNOWN_DIVERGENT_SCENARIOS:
            known = _KNOWN_DIVERGENT_SCENARIOS[scenario_path.stem]
            assert not diff.is_clean, (
                f"{scenario_path.stem}: expected a KNOWN divergence "
                f"({known.description}) but the run came back clean — "
                "remove this scenario from _KNOWN_DIVERGENT_SCENARIOS, its cause may have been fixed"
            )
            # Defect 4 (Issue #698 Verification): pin the EXACT divergence shape, not just
            # "some divergence happened" — a bare `not diff.is_clean` accepts any future,
            # undiagnosed divergence in this scenario forever, silently defeating the
            # differential harness for exactly the 6 scenarios most likely to regress again.
            # If these counts change, the divergence shape changed too — re-diagnose (see
            # this scenario's comment above) and update both the counts and the description
            # rather than just bumping the numbers to make the test pass.
            assert len(diff.event_divergences) == known.event_count, (
                f"{scenario_path.stem}: expected exactly {known.event_count} event "
                f"divergence(s) ({known.description}), got {len(diff.event_divergences)} — "
                f"divergence shape changed, re-diagnose before updating this count: "
                f"{[(d.index, d.a, d.b) for d in diff.event_divergences]}"
            )
            assert len(diff.action_divergences) == known.action_count, (
                f"{scenario_path.stem}: expected exactly {known.action_count} action "
                f"divergence(s) ({known.description}), got {len(diff.action_divergences)} — "
                f"divergence shape changed, re-diagnose before updating this count: "
                f"{[(d.index, d.a, d.b) for d in diff.action_divergences]}"
            )
            return
        assert diff.is_clean, (
            f"{scenario_path.stem}: FSM-authoritative diverged from baseline — "
            f"{len(diff.event_divergences)} event, {len(diff.action_divergences)} action divergences — "
            f"Step 2's read-authority swap must be a behavioral no-op (or, if this is a NEW, "
            f"diagnosed Issue #698 fast-loop divergence, add it to _KNOWN_DIVERGENT_SCENARIOS above "
            f"with its root cause, not just to silence this failure)"
        )


class TestFsmAuthoritativePositiveControl:
    """Test the test: a deliberately broken FSM gate MUST produce a real,
    detectable divergence — proves this comparator can actually catch a bug in
    the swapped path, not just always report clean.

    Note on scope: the corpus-wide test above is honest but narrower than it
    first looks. Step 2's swap only executes when ``_nat_vent_soft_start`` is
    True at the top of the active-session block — none of the 88 current
    golden/pending scenarios exercise soft-start (it's a newer, less common
    entry path; see Issue #540). For every scenario in the corpus test, the
    flag flip is therefore an inert no-op by construction (branch never
    taken), not evidence the FSM path itself computes correctly under load.
    This positive control exercises the actual swapped branch directly
    (bypassing the corpus, same direct-engine-construction style as
    ``test_nat_vent_soft_start.py``) so a broken FSM gate is provably
    detectable, and flags the corpus gap rather than silently relying on
    scenarios that never touch the new code."""

    def _make_soft_start_engine(self, *, fsm_authoritative: bool):
        import sys
        from datetime import datetime
        from unittest.mock import AsyncMock, MagicMock

        from custom_components.climate_advisor.automation import AutomationEngine
        from custom_components.climate_advisor.const import CONF_FAN_MODE, FAN_MODE_WHOLE_HOUSE

        sys.modules["homeassistant.util.dt"].now = lambda: datetime(2026, 7, 29, 18, 0, 0)

        hass = MagicMock()
        hass.services = MagicMock()
        hass.services.async_call = AsyncMock()
        hass.async_create_task = MagicMock(side_effect=lambda coro: coro.close())
        climate_state = MagicMock()
        climate_state.state = "cool"
        climate_state.attributes = {"current_temperature": 76.0}
        hass.states = MagicMock()
        hass.states.get = MagicMock(return_value=climate_state)

        engine = AutomationEngine(
            hass=hass,
            climate_entity="climate.thermostat",
            weather_entity="weather.forecast_home",
            door_window_sensors=["binary_sensor.front_door"],
            notify_service="notify.notify",
            config={
                "comfort_heat": 70.0,
                "comfort_cool": 76.0,
                "setback_heat": 60,
                "setback_cool": 80,
                "natural_vent_delta": 3.0,
                "notify_service": "notify.notify",
                CONF_FAN_MODE: FAN_MODE_WHOLE_HOUSE,
            },
        )
        engine._natvent_fsm_authoritative = fsm_authoritative
        engine._natural_vent_active = True
        engine._nat_vent_soft_start = True
        engine._paused_by_door = False
        # outdoor well below indoor - hysteresis -> full gate clears for real.
        engine._last_outdoor_temp = 70.0
        engine._outdoor_temp_today_peak = 90.0
        engine._outdoor_temp_today_sample_count = 5
        return engine

    def test_positive_control_detects_a_broken_fsm_gate(self) -> None:
        import asyncio
        from unittest.mock import patch

        engine = self._make_soft_start_engine(fsm_authoritative=True)
        with patch("custom_components.climate_advisor.nat_vent_fsm.decide_nat_vent_gate", lambda inputs: False):
            asyncio.run(engine.check_natural_vent_conditions())

        # Real full-gate conditions hold (outdoor 70 vs indoor 76), so without
        # the injected break the session would upgrade (_nat_vent_soft_start ->
        # False, per TestSoftStartUpgradeFsmAuthoritative above). Forcing the
        # FSM's gate to always return False must suppress that upgrade —
        # proves this comparison style can catch a real regression in the
        # swapped path, not just report clean by construction.
        assert engine._nat_vent_soft_start is True, (
            "positive control FAILED: forcing nat_vent_fsm.decide_nat_vent_gate() to always "
            "return False did not suppress the soft-start upgrade — a broken FSM gate in the "
            "FSM-authoritative path would go undetected"
        )

    def test_unbroken_fsm_gate_upgrades_normally(self) -> None:
        import asyncio

        engine = self._make_soft_start_engine(fsm_authoritative=True)
        asyncio.run(engine.check_natural_vent_conditions())
        assert engine._nat_vent_soft_start is False
