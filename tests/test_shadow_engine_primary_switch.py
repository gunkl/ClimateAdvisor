"""Tests for Issue #727: the shadow-engine-primary promotion switch, and
restart persistence for it plus the 3 pre-existing FSM-authoritative switches.

Uses the real ``build_headless_coordinator()`` harness (production-equivalent
construction — see ``tools/sim_harness/build_coordinator.py``) rather than a
hand-mocked coordinator, per this project's doctrine against mirror tests:
``async_set_shadow_engine_primary()`` does real work (state carry-over,
callback/dry_run swap) that a bare MagicMock coordinator can't exercise
meaningfully, and the persistence tests need the REAL
``_build_state_dict()``/``async_restore_state()`` code paths, not a
reimplementation of their date-boundary logic.
"""

from __future__ import annotations

import asyncio

from tools.sim_harness.build_coordinator import build_headless_coordinator


def _run(coro):
    return asyncio.run(coro)


def _suppress_fire_and_forget_save(coord) -> None:
    """The FSM/promotion setters fire-and-forget a save via
    ``hass.async_create_task(self._async_save_state())`` for the live-HA case.
    Under the sim harness's FakeScheduler, that enqueued coroutine is never
    drained inside a short-lived ``asyncio.run()`` test, which would otherwise
    warn "coroutine was never awaited" (-W error territory). Tests that care
    about the save call ``_async_save_state()`` explicitly instead; this closes
    the redundant fire-and-forget one so it doesn't leak."""
    coord.hass.async_create_task = lambda coro: coro.close()


class TestShadowEnginePrimaryToggle:
    def test_default_is_off(self):
        coord, _, _, _ = build_headless_coordinator()
        _suppress_fire_and_forget_save(coord)
        assert coord.shadow_engine_primary is False
        assert coord.automation_engine.role == "production"
        assert coord.automation_engine.dry_run is False
        assert coord.shadow_automation_engine.role == "shadow"
        assert coord.shadow_automation_engine.dry_run is True

    def test_promote_swaps_which_engine_is_primary(self):
        coord, _, _, _ = build_headless_coordinator()
        _suppress_fire_and_forget_save(coord)
        original_production = coord.automation_engine
        original_shadow = coord.shadow_automation_engine

        _run(coord.async_set_shadow_engine_primary(True))

        assert coord.shadow_engine_primary is True
        # The routing property now resolves to the OTHER physical engine object.
        assert coord.automation_engine is original_shadow
        assert coord.shadow_automation_engine is original_production
        # And that engine is now wired to actually issue real commands.
        assert coord.automation_engine.dry_run is False
        assert coord.automation_engine.role == "production"
        assert coord.shadow_automation_engine.dry_run is True
        assert coord.shadow_automation_engine.role == "shadow"

    def test_demote_swaps_back(self):
        coord, _, _, _ = build_headless_coordinator()
        _suppress_fire_and_forget_save(coord)
        original_production = coord.automation_engine

        _run(coord.async_set_shadow_engine_primary(True))
        _run(coord.async_set_shadow_engine_primary(False))

        assert coord.shadow_engine_primary is False
        assert coord.automation_engine is original_production
        assert coord.automation_engine.dry_run is False
        assert coord.shadow_automation_engine.dry_run is True

    def test_no_op_when_already_in_requested_state(self):
        coord, _, _, _ = build_headless_coordinator()
        _suppress_fire_and_forget_save(coord)
        original_production = coord.automation_engine
        _run(coord.async_set_shadow_engine_primary(False))  # already off — no-op
        assert coord.automation_engine is original_production
        assert coord.shadow_engine_primary is False

    def test_command_tracking_and_fsm_flags_carry_over_on_promotion(self):
        """Issue #724/#727's core finding: the raw-copy sync (_sync_shadow_inputs)
        never covered command-tracking/echo-suppression fields or the 3
        FSM-authoritative flags — those were populated only inside the real
        (non-dry-run) branches production alone ever reaches. Verify the
        promotion path explicitly carries them over so the newly-primary
        engine doesn't start from a blank slate mid-cycle."""
        coord, _, _, _ = build_headless_coordinator()
        _suppress_fire_and_forget_save(coord)
        ae = coord.automation_engine
        ae._hvac_command_pending = True
        ae._last_commanded_hvac_mode = "cool"
        ae._write_seq = 7
        ae._pending_setpoint_single = 72.0
        ae._natvent_fsm_authoritative = True
        ae._doorwindow_fsm_authoritative = True
        ae._override_grace_fsm_authoritative = True

        _run(coord.async_set_shadow_engine_primary(True))

        new_primary = coord.automation_engine
        assert new_primary._hvac_command_pending is True
        assert new_primary._last_commanded_hvac_mode == "cool"
        assert new_primary._write_seq == 7
        assert new_primary._pending_setpoint_single == 72.0
        assert new_primary._natvent_fsm_authoritative is True
        assert new_primary._doorwindow_fsm_authoritative is True
        assert new_primary._override_grace_fsm_authoritative is True


class TestFsmAndShadowPersistenceRoundTrip:
    """Issue #727: FSM-authoritative + shadow-engine-primary are mode-like
    settings the user wants to survive ANY restart — verified against the
    REAL save/restore code path, both directions (on-survives, off-survives)."""

    def test_flags_persist_through_same_day_restart(self, tmp_path):
        coord, _, _, _ = build_headless_coordinator(config_dir=str(tmp_path))
        _suppress_fire_and_forget_save(coord)
        coord.automation_engine._natvent_fsm_authoritative = True
        coord.automation_engine._doorwindow_fsm_authoritative = True
        coord.automation_engine._override_grace_fsm_authoritative = True
        _run(coord.async_set_shadow_engine_primary(True))
        _run(coord._async_save_state())

        coord2, _, _, _ = build_headless_coordinator(config_dir=str(tmp_path))
        _run(coord2.async_restore_state())

        assert coord2.natvent_fsm_authoritative is True
        assert coord2.doorwindow_fsm_authoritative is True
        assert coord2.override_grace_fsm_authoritative is True
        assert coord2.shadow_engine_primary is True
        # Not just the boolean — the actual dry_run/role wiring must follow.
        assert coord2.automation_engine.dry_run is False
        assert coord2.automation_engine.role == "production"
        assert coord2.shadow_automation_engine.dry_run is True

    def test_flags_persist_even_when_saved_state_is_from_a_prior_day(self, tmp_path):
        """The whole point of Issue #727: these are mode-like settings, not
        daily ephemeral state — they must survive a restart on a DIFFERENT
        calendar day, unlike most of the rest of the persisted state dict
        (which async_restore_state() discards once state_date != today)."""
        coord, _, _, _ = build_headless_coordinator(config_dir=str(tmp_path))
        _suppress_fire_and_forget_save(coord)
        coord.automation_engine._natvent_fsm_authoritative = True
        _run(coord.async_set_shadow_engine_primary(True))
        state_dict = coord._build_state_dict()
        state_dict["date"] = "2020-01-01"  # force a stale date
        coord._state_persistence.save(state_dict)

        coord2, _, _, _ = build_headless_coordinator(config_dir=str(tmp_path))
        _run(coord2.async_restore_state())

        assert coord2.natvent_fsm_authoritative is True
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

        assert coord2.natvent_fsm_authoritative is False
        assert coord2.doorwindow_fsm_authoritative is False
        assert coord2.override_grace_fsm_authoritative is False
        assert coord2.shadow_engine_primary is False
