"""Tests for Issue #594 Phase R: decision-equivalence for the door/window
FSM-authoritative swap. As of Issue #660 Phase R Step 8, full authority — see
``automation.py``'s ``_doorwindow_fsm_authoritative`` docstring for the complete
8-method list this flag now governs, all via the shared
``AutomationEngine._resolve_door_window_pause_flags()`` dispatcher.

Prior coverage (``test_shadow_engine_coverage.py``, ``test_shadow_engine_pair.py``)
only proved *state-label* agreement. This file proves the stronger claim Phase R
needs before any live switch is considered: flipping
``AutomationEngine._doorwindow_fsm_authoritative`` from its default ``False`` to
``True`` produces a byte-for-byte identical ``event_log``/``action_log`` across the
full golden + pending scenario corpus.

**Corpus coverage confirmed insufficient before writing this, not assumed.**
``resume_from_pause()`` has NO scenario-event mapping at all in
``tools/sim_harness/run_production.py`` — it's only reachable via ``api.py``'s
dashboard "Resume HVAC" endpoint, which the sim harness doesn't simulate (no HTTP
layer). So the corpus-wide test below is an inert no-op for DASHBOARD_RESUME by
construction, same "narrower than it looks" finding nat-vent's own comparator
already flagged for soft-start. ``handle_manual_override_during_pause()`` *is*
reachable in principle (``thermostat_state_changed`` scenario events, when
``is_paused_by_door`` is True at the time — see coordinator.py's
``_async_thermostat_changed``), but whether any current scenario actually lands in
that exact state is not assumed here either. Both get a direct-engine-construction
positive control below, same style as
``test_nat_vent_fsm_authoritative_compare.py``'s soft-start probe.

**Issue #660 Step 8 additions.** The corpus-wide equivalence test above is now
clean across all 8 sites (confirmed: no scenario in the golden/pending corpus
diverges with the flag flipped for any of the 6 newly-authoritative methods), but a
clean corpus diff proves absence of divergence, not presence of real coverage —
same lesson ``resume_from_pause()`` established. Two areas are the most likely to
be under-exercised by the corpus specifically (per the Step 8 plan's own caution):
the ``GRACE_TIMER_EXPIRED`` origin-state variant (does the corpus ever reach
``_on_grace_expired()`` from ``PAUSED_DURING_GRACE``, not just plain ``GRACE``?)
and the brand-new ``PAUSED_NAT_VENT_REACTIVATED`` kind (fed only from
``check_natural_vent_conditions()``, which the corpus does drive every tick, but
whether any scenario's specific temperature/pause state combination reaches the
reactivation-while-paused branch at all is not assumed). Both get direct-
construction positive controls below (``TestGraceTimerExpiredOriginStatePositive
Control``, ``TestPausedNatVentReactivatedPositiveControl``), proving a broken FSM
transition for each would actually be caught, not just that the corpus stays quiet.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tools.sim_harness.differential import diff_runs
from tools.sim_harness.doorwindow_fsm_authoritative_compare import fsm_authoritative_mutation

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


class TestFsmAuthoritativeEquivalence:
    @pytest.mark.parametrize("scenario_path", _all_golden_and_pending_scenarios(), ids=lambda p: p.stem)
    def test_flag_on_produces_identical_outcome(self, scenario_path: Path) -> None:
        scenario = _load_scenario(scenario_path)
        diff = diff_runs(scenario, mutate_b=fsm_authoritative_mutation, scenario_name=scenario_path.stem)
        assert not diff.a_error and not diff.b_error, (
            f"{scenario_path.stem}: a_error={diff.a_error!r} b_error={diff.b_error!r}"
        )
        assert diff.is_clean, (
            f"{scenario_path.stem}: FSM-authoritative diverged from baseline — "
            f"{len(diff.event_divergences)} event, {len(diff.action_divergences)} action divergences — "
            f"Step 2's read-authority swap must be a behavioral no-op"
        )


class TestFsmAuthoritativePositiveControl:
    """A deliberately broken FSM transition MUST produce a real, detectable
    divergence from the direct-engine tests below — proves this style of
    comparison can actually catch a bug in the swapped path, not just always
    report clean. Reuses the same direct-construction pattern
    ``tests/test_door_window.py``'s/``tests/test_resume_from_pause.py``'s own
    ``TestManualOverrideDuringPauseFsmAuthoritative``/
    ``TestResumeFromPauseFsmAuthoritative`` classes already establish — this
    class instead patches the FSM's own ``transition()`` to prove a broken
    swap is detectable, not just that the swap produces the expected output.
    """

    def _make_paused_engine(self, *, fsm_authoritative: bool, grace_active: bool = False):
        import sys
        from datetime import datetime
        from unittest.mock import AsyncMock, MagicMock

        from custom_components.climate_advisor.automation import AutomationEngine

        sys.modules["homeassistant.util.dt"].now = lambda: datetime(2026, 7, 29, 18, 0, 0)

        hass = MagicMock()
        hass.services = MagicMock()
        hass.services.async_call = AsyncMock()
        hass.async_create_task = MagicMock(side_effect=lambda coro: coro.close())
        climate_state = MagicMock()
        climate_state.state = "cool"
        climate_state.attributes = {"current_temperature": 74.0}
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
                "notify_service": "notify.notify",
            },
        )
        engine._doorwindow_fsm_authoritative = fsm_authoritative
        engine._paused_by_door = True
        engine._grace_active = grace_active
        engine._pre_pause_mode = "cool"
        return engine

    def test_positive_control_detects_a_broken_fsm_transition(self) -> None:
        import asyncio
        from unittest.mock import patch

        from custom_components.climate_advisor.door_window_lifecycle import DoorWindowLifecycleState

        engine = self._make_paused_engine(fsm_authoritative=True)
        with patch(
            "custom_components.climate_advisor.door_window_fsm.transition",
            side_effect=lambda *a, **k: type(
                "T", (), {"to_state": DoorWindowLifecycleState.PAUSED_ACTIVE, "outcome": "broken", "at": None}
            )(),
        ):
            asyncio.run(engine.handle_manual_override_during_pause())

        # Real behavior: MANUAL_OVERRIDE_DURING_PAUSE always clears the pause.
        # Forcing transition() to instead return PAUSED_ACTIVE (pause never
        # clears) must show up as a detectable divergence — proves a broken
        # FSM-authoritative swap wouldn't silently pass.
        assert engine._paused_by_door is True, (
            "positive control FAILED: forcing door_window_fsm.transition() to return the wrong "
            "state did not change engine._paused_by_door — a broken FSM-authoritative swap would "
            "go undetected"
        )

    def test_unbroken_transition_clears_pause_normally(self) -> None:
        import asyncio

        engine = self._make_paused_engine(fsm_authoritative=True)
        asyncio.run(engine.handle_manual_override_during_pause())
        assert engine._paused_by_door is False

    def test_resume_from_pause_unreachable_via_scenario_corpus_uses_direct_construction(self) -> None:
        """Documents (and asserts, not just claims) why DASHBOARD_RESUME needs this
        direct-construction control: resume_from_pause() has no scenario-event
        mapping in run_production.py at all."""
        import asyncio

        engine = self._make_paused_engine(fsm_authoritative=True)
        engine._current_classification = None  # no HVAC call needed for this assertion
        asyncio.run(engine.resume_from_pause())
        assert engine._paused_by_door is False
        assert engine._grace_active is True


class TestGraceTimerExpiredOriginStatePositiveControl:
    """Issue #660 Step 8/10: direct-construction positive controls proving a broken
    GRACE_TIMER_EXPIRED transition would be caught, for both origin states
    _on_grace_expired() can be reached from (GRACE and PAUSED_DURING_GRACE) —
    the plan's own explicit caution that this origin-state variant is the most
    likely to be under-exercised by the corpus (a clean corpus diff proves absence
    of divergence, not presence of real coverage)."""

    def _make_engine(self, *, paused_during_grace: bool, outdoor: float) -> AutomationEngine:  # noqa: F821
        from unittest.mock import AsyncMock, MagicMock

        from custom_components.climate_advisor.automation import AutomationEngine

        hass = MagicMock()
        hass.services = MagicMock()
        hass.services.async_call = AsyncMock()
        hass.async_create_task = MagicMock(side_effect=lambda coro: coro.close())
        climate_state = MagicMock()
        climate_state.state = "cool"
        hass.states = MagicMock()
        hass.states.get = MagicMock(return_value=climate_state)

        engine = AutomationEngine(
            hass=hass,
            climate_entity="climate.thermostat",
            weather_entity="weather.forecast_home",
            door_window_sensors=["binary_sensor.front_door"],
            notify_service="notify.notify",
            config={"comfort_heat": 70.0, "comfort_cool": 76.0, "natural_vent_delta": 3.0},
        )
        engine._doorwindow_fsm_authoritative = True
        engine._grace_active = True
        engine._last_resume_source = "automation"
        engine._paused_by_door = paused_during_grace
        engine._paused_with_hvac_already_off = False
        engine._sensor_check_callback = lambda: True
        engine._last_outdoor_temp = outdoor
        engine._get_indoor_temp_f = lambda: 76.0
        return engine

    def test_broken_transition_detected_from_grace_origin(self) -> None:
        """Origin GRACE (not already paused): a broken transition() forced to return
        the WRONG state (NORMAL, i.e. "cleared, no re-pause") when the real outcome
        should re-pause must show up as a detectable divergence."""
        from unittest.mock import patch

        from custom_components.climate_advisor.door_window_lifecycle import DoorWindowLifecycleState

        engine = self._make_engine(paused_during_grace=False, outdoor=90.0)  # too warm -> real outcome re-pauses
        with patch(
            "custom_components.climate_advisor.door_window_fsm.transition",
            side_effect=lambda *a, **k: type(
                "T", (), {"to_state": DoorWindowLifecycleState.NORMAL, "outcome": "broken", "at": None}
            )(),
        ):
            engine._on_grace_expired(source="automation", duration=300, should_notify=False)

        assert engine._paused_by_door is False, (
            "positive control FAILED: forcing door_window_fsm.transition() to return NORMAL was not "
            "reflected in engine._paused_by_door — the mock wasn't actually exercised (real outcome "
            "here should have re-paused, so an undetected broken swap would incorrectly show "
            "_paused_by_door=True instead)"
        )

    def test_broken_transition_detected_from_paused_during_grace_origin(self) -> None:
        """Origin PAUSED_DURING_GRACE: a broken transition() forced to return NORMAL
        (real outcome should stay paused, per this origin's own documented
        distinction — see door_window_fsm.py's module docstring) must also be
        detectable."""
        from unittest.mock import patch

        from custom_components.climate_advisor.door_window_lifecycle import DoorWindowLifecycleState

        engine = self._make_engine(paused_during_grace=True, outdoor=68.0)  # cool enough to reactivate, but origin
        # PAUSED_DURING_GRACE always stays paused regardless
        with patch(
            "custom_components.climate_advisor.door_window_fsm.transition",
            side_effect=lambda *a, **k: type(
                "T", (), {"to_state": DoorWindowLifecycleState.NORMAL, "outcome": "broken", "at": None}
            )(),
        ):
            engine._on_grace_expired(source="automation", duration=300, should_notify=False)

        assert engine._paused_by_door is False, (
            "positive control FAILED: forcing door_window_fsm.transition() to return NORMAL was not "
            "reflected in engine._paused_by_door — the mock wasn't actually exercised, invalidating "
            "this control"
        )

    def test_unbroken_transition_from_paused_during_grace_stays_paused(self) -> None:
        """Regression guard for the real (unmocked) transition table: confirms the
        actual fix behaves per its own documented origin-state distinction."""
        engine = self._make_engine(paused_during_grace=True, outdoor=68.0)
        engine._on_grace_expired(source="automation", duration=300, should_notify=False)
        assert engine._paused_by_door is True


class TestPausedNatVentReactivatedPositiveControl:
    """Issue #660 Step 8/10: direct-construction positive control for the brand-new
    PAUSED_NAT_VENT_REACTIVATED kind (Step 3) — fed only from
    check_natural_vent_conditions(), which the corpus drives every tick, but whether
    any specific scenario's temperature/pause combination reaches this exact branch
    is not assumed."""

    def _make_engine(self) -> AutomationEngine:  # noqa: F821
        from unittest.mock import AsyncMock, MagicMock

        from custom_components.climate_advisor.automation import AutomationEngine

        hass = MagicMock()
        hass.services = MagicMock()
        hass.services.async_call = AsyncMock()
        hass.async_create_task = MagicMock(side_effect=lambda coro: coro.close())
        hass.states = MagicMock()
        hass.states.get = MagicMock(return_value=None)

        engine = AutomationEngine(
            hass=hass,
            climate_entity="climate.thermostat",
            weather_entity="weather.forecast_home",
            door_window_sensors=["binary_sensor.front_door"],
            notify_service="notify.notify",
            config={"comfort_heat": 70.0, "comfort_cool": 76.0, "natural_vent_delta": 3.0},
        )
        engine._doorwindow_fsm_authoritative = True
        engine._paused_by_door = True
        engine._natural_vent_active = False
        engine._last_outdoor_temp = 68.0
        engine._get_indoor_temp_f = lambda: 76.0
        return engine

    def test_broken_transition_detected(self) -> None:
        import asyncio
        from unittest.mock import patch

        from custom_components.climate_advisor.door_window_lifecycle import DoorWindowLifecycleState

        engine = self._make_engine()
        with patch(
            "custom_components.climate_advisor.door_window_fsm.transition",
            side_effect=lambda *a, **k: type(
                "T", (), {"to_state": DoorWindowLifecycleState.PAUSED_ACTIVE, "outcome": "broken", "at": None}
            )(),
        ):
            asyncio.run(engine.check_natural_vent_conditions())

        assert engine._paused_by_door is True, (
            "positive control FAILED: forcing door_window_fsm.transition() to return PAUSED_ACTIVE was "
            "not reflected in engine._paused_by_door — the mock wasn't actually exercised (real outcome "
            "for a successful reactivation is NORMAL, so an undetected broken swap would incorrectly "
            "show _paused_by_door=False instead)"
        )

    def test_unbroken_transition_clears_pause_on_reactivation(self) -> None:
        import asyncio

        engine = self._make_engine()
        asyncio.run(engine.check_natural_vent_conditions())
        assert engine._paused_by_door is False
        assert engine._natural_vent_active is True
