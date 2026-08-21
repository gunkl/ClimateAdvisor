"""Tests for Phase G's "G-test" deliverable (Epic #594 Phase R): decision
equivalence and race-safety with ALL THREE FSM-authoritative switches
(``natvent_fsm_authoritative``, ``doorwindow_fsm_authoritative``,
``override_grace_fsm_authoritative``) flipped True together.

Occupant-first framing: Phase V flips all three switches in one deploy rather
than as two sequential live-verification windows. Before this file, nothing in
the repo — no test, golden scenario, or differential harness — had ever
exercised that combination; a 2026-08-20 audit confirmed its absence by
grepping every test file for co-occurring True assignments of all three flags
and finding none. This file closes that gap: (1) the corpus-wide differential
comparison proves the combined switch state doesn't change any real commanded
HVAC/fan action across the golden + pending scenario suite, beyond nat-vent's
own already-diagnosed divergences; (2) the positive-control class proves
Blocker F's fix (a manual override arriving mid-activation must not be
silently clobbered by a stale nat-vent decision) holds under the actual
combined condition Phase V deploys, not just with natvent_fsm_authoritative
alone.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from custom_components.climate_advisor.automation import AutomationEngine, FanCommandResult
from custom_components.climate_advisor.const import CONF_FAN_MODE, FAN_MODE_WHOLE_HOUSE

# Reuse nat-vent's own diagnosed divergence allowlist rather than duplicating
# it: door/window's and override/grace's individual comparators are clean
# across the ENTIRE corpus (no allowlist at all in their own test files), so
# any divergence the combined run produces beyond nat-vent's already-diagnosed
# set would be new — either a genuine interaction between the three switches,
# or a regression. Importing (not copying) means this list can never drift
# out of sync with the single-switch test's own diagnosis.
from tests.test_nat_vent_fsm_authoritative_compare import _KNOWN_DIVERGENT_SCENARIOS
from tools.sim_harness.combined_fsm_authoritative_compare import fsm_authoritative_mutation
from tools.sim_harness.differential import diff_runs

_REPO_ROOT = Path(__file__).resolve().parent.parent
_GOLDEN_DIR = _REPO_ROOT / "tools" / "simulations" / "golden"
_PENDING_DIR = _REPO_ROOT / "tools" / "simulations" / "pending"


def _load_scenario(path: Path) -> dict:
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def _all_golden_and_pending_scenarios() -> list[Path]:
    paths = [p for p in _GOLDEN_DIR.glob("*.json") if p.name != "MANIFEST.json"]
    paths += [p for p in _PENDING_DIR.glob("*.json") if p.name != "MANIFEST.json"]
    return sorted(paths)


class TestCombinedFsmAuthoritativeEquivalence:
    """All three switches on together must not diverge from production beyond
    the exact, already-diagnosed nat-vent divergences the single-switch
    comparator already accepts (Issue #698's deliberate fast-loop widening).
    A NEW divergence here — one not in nat-vent's own allowlist — means the
    three switches interact in a way no single-switch comparator could have
    caught, and must be diagnosed before Phase V's deploy, not silenced."""

    @pytest.mark.parametrize("scenario_path", _all_golden_and_pending_scenarios(), ids=lambda p: p.stem)
    def test_all_three_switches_produce_identical_or_known_outcome(self, scenario_path: Path) -> None:
        scenario = _load_scenario(scenario_path)
        diff = diff_runs(scenario, mutate_b=fsm_authoritative_mutation, scenario_name=scenario_path.stem)
        assert not diff.a_error and not diff.b_error, (
            f"{scenario_path.stem}: a_error={diff.a_error!r} b_error={diff.b_error!r}"
        )
        if scenario_path.stem in _KNOWN_DIVERGENT_SCENARIOS:
            known = _KNOWN_DIVERGENT_SCENARIOS[scenario_path.stem]
            assert not diff.is_clean, (
                f"{scenario_path.stem}: expected the same known nat-vent-only divergence "
                f"({known.description}) under the combined switch state but the run came back "
                "clean — the divergence shape may have changed under the combined condition, "
                "investigate before trusting this as still nat-vent-only"
            )
            assert len(diff.event_divergences) == known.event_count, (
                f"{scenario_path.stem}: under ALL THREE switches, expected the same "
                f"{known.event_count} event divergence(s) nat-vent-alone produces "
                f"({known.description}), got {len(diff.event_divergences)} — door/window or "
                f"override/grace may be interacting with nat-vent's known divergence; "
                f"re-diagnose: {[(d.index, d.a, d.b) for d in diff.event_divergences]}"
            )
            assert len(diff.action_divergences) == known.action_count, (
                f"{scenario_path.stem}: under ALL THREE switches, expected the same "
                f"{known.action_count} action divergence(s) nat-vent-alone produces "
                f"({known.description}), got {len(diff.action_divergences)} — re-diagnose: "
                f"{[(d.index, d.a, d.b) for d in diff.action_divergences]}"
            )
            return
        assert diff.is_clean, (
            f"{scenario_path.stem}: combining all three FSM-authoritative switches diverged from "
            f"production in a way no single-switch comparator predicted — "
            f"{len(diff.event_divergences)} event, {len(diff.action_divergences)} action "
            f"divergences. This is exactly the class of compound-switch interaction Phase G's "
            f"G-test exists to catch before Phase V's deploy — diagnose the root cause rather "
            f"than adding this scenario to an allowlist"
        )


class TestCombinedFsmAuthoritativePositiveControl:
    """Test the test: with a real divergence forced, the combined comparator
    must actually detect it — otherwise a clean result above would be
    meaningless."""

    def test_detects_a_forced_divergence(self) -> None:
        from contextlib import contextmanager
        from unittest.mock import patch

        from custom_components.climate_advisor import nat_vent_fsm as _nvfsm_mod
        from custom_components.climate_advisor.nat_vent_fsm import NatVentLifecycleState

        probe = _GOLDEN_DIR / "2026-03-28-overnight.json"
        scenario = _load_scenario(probe)
        original = _nvfsm_mod.transition

        def _broken_transition(*args, **kwargs):
            result = original(*args, **kwargs)
            # Force the escalation gate permanently shut so a real, detectable
            # divergence occurs under the combined mutation.
            if result.to_state == NatVentLifecycleState.ACTIVE_FULL_GATE:
                object.__setattr__(result, "to_state", NatVentLifecycleState.ACTIVE_SOFT_START)
            return result

        @contextmanager
        def _mutate_b_and_break():
            with fsm_authoritative_mutation(), patch.object(_nvfsm_mod, "transition", side_effect=_broken_transition):
                yield

        diff = diff_runs(scenario, mutate_b=_mutate_b_and_break, scenario_name=probe.stem)
        assert not diff.is_clean, (
            "the combined comparator failed to detect a deliberately forced divergence — "
            "it cannot be trusted to catch a real one either"
        )


class TestCombinedSwitchBugFRace:
    """Blocker F's fix (Issue #706) was originally proven with only
    natvent_fsm_authoritative=True. Phase V deploys with ALL THREE switches
    on together — this class re-proves the same race-safety property under
    the actual combined condition, since door/window's and override/grace's
    own authoritative code paths were never exercised alongside it before."""

    def _make_engine(self, **overrides) -> AutomationEngine:
        hass = MagicMock()
        hass.services = MagicMock()
        hass.services.async_call = AsyncMock()

        def _consume_coroutine(coro):
            coro.close()

        hass.async_create_task = MagicMock(side_effect=_consume_coroutine)

        climate_state = MagicMock()
        climate_state.state = "off"
        climate_state.attributes = {"current_temperature": 72.0}
        hass.states = MagicMock()
        hass.states.get = MagicMock(return_value=climate_state)

        config = {
            "comfort_heat": 68.0,
            "comfort_cool": 74.0,
            "setback_heat": 60,
            "setback_cool": 80,
            "natural_vent_delta": 3.0,
            "nat_vent_hysteresis_f": 1.0,
            "notify_service": "notify.notify",
            CONF_FAN_MODE: FAN_MODE_WHOLE_HOUSE,
        }
        engine = AutomationEngine(
            hass=hass,
            climate_entity="climate.thermostat",
            weather_entity="weather.forecast_home",
            door_window_sensors=["binary_sensor.front_door"],
            notify_service="notify.notify",
            config=config,
        )
        engine._natvent_fsm_authoritative = True
        engine._doorwindow_fsm_authoritative = True
        engine._override_grace_fsm_authoritative = True
        for key, value in overrides.items():
            setattr(engine, key, value)
        return engine

    def test_idle_open_reentry_does_not_clobber_override_with_all_three_switches_on(self) -> None:
        engine = self._make_engine(
            _paused_by_door=False,
            _natural_vent_active=False,
            _nat_vent_soft_start=False,
            _grace_active=False,
            _fan_override_active=False,
            _last_outdoor_temp=68.0,
        )
        engine._sensor_check_callback = lambda: True

        async def _activate_fan_races_override(*, reason: str, emit_event: bool = True) -> FanCommandResult:
            # Simulates a real manual override winning the race during this
            # await window, now with door/window's and override/grace's own
            # authoritative dispatchers also live (not just nat-vent's).
            engine._fan_override_active = True
            engine._grace_active = True
            return FanCommandResult.OVERRIDDEN

        engine._activate_fan = AsyncMock(side_effect=_activate_fan_races_override)
        asyncio.run(engine.check_natural_vent_conditions())

        assert engine._natural_vent_active is False, (
            "with all three FSM switches authoritative together (the real Phase V deploy "
            "condition), the pre-await ACTIVE_FULL_GATE decision must still not be applied "
            "once _activate_fan() reports the command was overridden mid-activation"
        )
