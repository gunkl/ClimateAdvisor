"""Tests for Issue #727/#729: the shadow-engine-primary switch — the single
control axis for choosing between the fixed-legacy and fixed-FSM
``AutomationEngine`` identity — and its reload-based promotion mechanism.

Uses the real ``build_headless_coordinator()`` harness (production-equivalent
construction — see ``tools/sim_harness/build_coordinator.py``) rather than a
hand-mocked coordinator, per this project's doctrine against mirror tests.
``build_headless_coordinator()`` gives coordinators an empty ``_entry_id`` (the
documented sim-harness convention), which exercises
``async_set_shadow_engine_primary()``'s no-real-entry fallback path by
default; the real-entry-id (persist-then-reload) path is tested separately
with a mocked ``hass.config_entries``, since ``FakeHass`` doesn't model config
entries.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

from tools.sim_harness.build_coordinator import build_headless_coordinator


def _run(coro):
    return asyncio.run(coro)


def _suppress_fire_and_forget_save(coord) -> None:
    """The sim-harness fallback path fire-and-forgets a save via
    ``hass.async_create_task(self._async_save_state())``. Under the sim
    harness's FakeScheduler, that enqueued coroutine is never drained inside a
    short-lived ``asyncio.run()`` test, which would otherwise warn "coroutine
    was never awaited" (-W error territory). Tests that care about the save
    call ``_async_save_state()`` explicitly instead; this closes the redundant
    fire-and-forget one so it doesn't leak."""
    coord.hass.async_create_task = lambda coro: coro.close()


def _capture_create_task(coord) -> list:
    """Like ``_suppress_fire_and_forget_save`` but records what was scheduled,
    for tests that need to prove a reload was actually fire-and-forgotten
    rather than awaited inline."""
    captured: list = []

    def _capture(coro) -> None:
        captured.append(coro)
        coro.close()

    coord.hass.async_create_task = _capture
    return captured


class TestEngineIdentityFixedAtConstruction:
    """Issue #729: each engine's FSM-authoritative flags are now fixed at
    construction, replacing the independent per-subsystem switches — one
    engine is always fully legacy, the other always fully FSM. Cheap
    regression guard replacing the old switch-toggle tests' coverage intent.

    Was 5 flags total. 3 were session/lifecycle-shaped (nat-vent, door/window,
    override/grace) — override/grace's own flag was removed in Issue #757
    Phase 6 Step 3, door/window's in Step 4, and nat-vent's (the last of the
    three session/lifecycle-shaped flags) in Step 5, each once its dispatcher
    became unconditionally FSM-authoritative with no more legacy branch to
    switch away from. The per-flag `test_engine_a_is_fixed_legacy`/
    `test_engine_b_is_fixed_fsm` assertions this class used to carry are
    retired along with the last flag they checked. The remaining 2 flags
    (occupancy, classification) were both deliberately STATELESS FSMs (see
    each module's own docstring), so there was never an engine-identity-fixing
    test of this shape for either — `_occupancy_fsm_authoritative` was removed
    in Phase 6 Step 6, and `_classification_fsm_authoritative` — the last of
    all 5 `_*_fsm_authoritative` flags on either engine — was removed in Phase
    7 Step 7. As of Step 7, neither `_engine_a` nor `_engine_b` carries any
    `_*_fsm_authoritative` flag anymore; every subsystem's dispatcher is
    unconditionally FSM-authoritative on both engines.
    `test_default_primary_is_the_legacy_engine` below still covers the
    genuinely load-bearing claim (engine role/dry_run wiring)."""

    def test_default_primary_is_the_legacy_engine(self):
        coord, _, _, _ = build_headless_coordinator()
        assert coord.shadow_engine_primary is False
        assert coord.automation_engine is coord._engine_a
        assert coord.automation_engine.dry_run is False
        assert coord.automation_engine.role == "production"
        assert coord.shadow_automation_engine is coord._engine_b
        assert coord.shadow_automation_engine.dry_run is True
        assert coord.shadow_automation_engine.role == "shadow"


class TestSimHarnessFallbackPromotion:
    """No real config entry available (``build_headless_coordinator()``'s
    documented convention) — ``async_set_shadow_engine_primary()`` applies the
    routing change in-memory instead of scheduling a reload, so
    simulation/tests can still exercise the effect of a flip."""

    def test_promote_flips_routing_in_memory(self):
        coord, _, _, _ = build_headless_coordinator()
        _suppress_fire_and_forget_save(coord)
        legacy_engine = coord.automation_engine
        fsm_engine = coord.shadow_automation_engine

        _run(coord.async_set_shadow_engine_primary(True))

        assert coord.shadow_engine_primary is True
        assert coord.automation_engine is fsm_engine
        assert coord.automation_engine.dry_run is False
        assert coord.automation_engine.role == "production"
        assert coord.shadow_automation_engine is legacy_engine
        assert coord.shadow_automation_engine.dry_run is True
        assert coord.shadow_automation_engine.role == "shadow"

    def test_demote_flips_back(self):
        coord, _, _, _ = build_headless_coordinator()
        _suppress_fire_and_forget_save(coord)
        legacy_engine = coord.automation_engine

        _run(coord.async_set_shadow_engine_primary(True))
        _run(coord.async_set_shadow_engine_primary(False))

        assert coord.shadow_engine_primary is False
        assert coord.automation_engine is legacy_engine
        assert coord.automation_engine.dry_run is False
        assert coord.shadow_automation_engine.dry_run is True

    def test_no_op_when_already_in_requested_state(self):
        coord, _, _, _ = build_headless_coordinator()
        _suppress_fire_and_forget_save(coord)
        original_primary = coord.automation_engine
        _run(coord.async_set_shadow_engine_primary(False))  # already off — no-op
        assert coord.automation_engine is original_primary
        assert coord.shadow_engine_primary is False


class TestRealEntryReloadPath:
    """Issue #729's actual replacement for the live in-process swap: with a
    resolvable config entry, promotion persists the choice and schedules a
    config-entry reload rather than mutating engines in place."""

    def _attach_fake_entry(self, coord) -> MagicMock:
        coord._entry_id = "fake_entry_id"
        config_entries = MagicMock()
        config_entries.async_get_entry = MagicMock(return_value=MagicMock())
        config_entries.async_reload = AsyncMock()
        coord.hass.config_entries = config_entries
        return config_entries

    def test_promote_persists_and_schedules_reload_not_live_swap(self, tmp_path):
        coord, _, _, _ = build_headless_coordinator(config_dir=str(tmp_path))
        config_entries = self._attach_fake_entry(coord)
        captured = _capture_create_task(coord)
        original_primary = coord.automation_engine

        _run(coord.async_set_shadow_engine_primary(True))

        # Recheck §2: the in-memory flag is NOT flipped on this doomed
        # coordinator — only the rebuilt one (after the reload) should ever
        # set it. If this coordinator kept running, its routing must be
        # unchanged.
        assert coord.automation_engine is original_primary
        assert coord.shadow_engine_primary is False

        # The reload was scheduled fire-and-forget (Recheck §3 / Agent B's
        # confirmed-safe pattern), not awaited inline.
        config_entries.async_reload.assert_called_once_with("fake_entry_id")
        assert len(captured) == 1

        # And the choice was persisted to disk BEFORE the reload was
        # scheduled — the rebuilt coordinator must be able to read it back.
        saved = coord._state_persistence.load()
        assert saved["shadow_engine_primary"] is True

    def test_no_op_does_not_persist_or_reload(self, tmp_path):
        coord, _, _, _ = build_headless_coordinator(config_dir=str(tmp_path))
        config_entries = self._attach_fake_entry(coord)
        captured = _capture_create_task(coord)

        _run(coord.async_set_shadow_engine_primary(False))  # already off

        config_entries.async_reload.assert_not_called()
        assert captured == []


class TestShadowEnginePrimaryPersistenceRoundTrip:
    """Issue #727/#729: which engine is primary is a mode-like setting that
    must survive ANY restart, not just a same-day one — verified against the
    REAL save/restore code path, both directions (on-survives, off-survives).
    Uses the sim-harness fallback path to set up state (no real config entry
    available), then verifies the REAL restore path (which every real HA
    restart — reload-triggered or not — goes through)."""

    def test_persists_through_same_day_restart(self, tmp_path):
        coord, _, _, _ = build_headless_coordinator(config_dir=str(tmp_path))
        _suppress_fire_and_forget_save(coord)
        _run(coord.async_set_shadow_engine_primary(True))
        _run(coord._async_save_state())

        coord2, _, _, _ = build_headless_coordinator(config_dir=str(tmp_path))
        _run(coord2.async_restore_state())

        assert coord2.shadow_engine_primary is True
        # Not just the boolean — the actual dry_run/role wiring must follow.
        assert coord2.automation_engine.dry_run is False
        assert coord2.automation_engine.role == "production"
        assert coord2.shadow_automation_engine.dry_run is True

    def test_persists_even_when_saved_state_is_from_a_prior_day(self, tmp_path):
        """The whole point of Issue #727/#729: this is a mode-like setting,
        not daily ephemeral state — it must survive a restart on a DIFFERENT
        calendar day, unlike most of the rest of the persisted state dict
        (which async_restore_state() discards once state_date != today)."""
        coord, _, _, _ = build_headless_coordinator(config_dir=str(tmp_path))
        _suppress_fire_and_forget_save(coord)
        _run(coord.async_set_shadow_engine_primary(True))
        state_dict = coord._build_state_dict()
        state_dict["date"] = "2020-01-01"  # force a stale date
        coord._state_persistence.save(state_dict)

        coord2, _, _, _ = build_headless_coordinator(config_dir=str(tmp_path))
        _run(coord2.async_restore_state())

        assert coord2.shadow_engine_primary is True
        # Confirm the date-gated section was indeed skipped for this restore —
        # today_record (a same-day-only field) stays unset, proving this test
        # exercises the stale-date path, not an accidental same-day match.
        assert coord2._today_record is None

    def test_off_stays_off(self, tmp_path):
        coord, _, _, _ = build_headless_coordinator(config_dir=str(tmp_path))
        _suppress_fire_and_forget_save(coord)
        _run(coord._async_save_state())

        coord2, _, _, _ = build_headless_coordinator(config_dir=str(tmp_path))
        _run(coord2.async_restore_state())

        assert coord2.shadow_engine_primary is False
