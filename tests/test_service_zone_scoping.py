"""Tests for Issue #796 Gap 5/9: zone-scoped service addressing and teardown.

Covers Step 4 of docs/multi-zone-spec.md's implementation sequence: the five
domain-scoped Climate Advisor services (`respond_to_suggestion`,
`force_reclassify`, `resend_briefing`, `dump_diagnostics`,
`reset_learning_data`) previously closed over a `coordinator` local bound to
whichever zone's `async_setup_entry()` ran last — the single most severe
finding in the multi-zone spec (Gap 5), compounded by Gap 9 (no
`hass.services.async_remove()` on unload).

These tests drive the REAL `async_setup_entry()`/`async_unload_entry()` via
`build_headless_multi_zone()` — the harness built specifically to exercise
this code path (`build_headless_coordinator()` never touches it) — and the
`cross_zone_isolation`/`service_registry_binding`/`teardown_cleanup`
evaluators built for exactly this bug class (`multi_zone_assertions.py`).
"""

from __future__ import annotations

import pytest

from tools.sim_harness._loop import run_coro
from tools.sim_harness.build_coordinator import build_headless_multi_zone
from tools.sim_harness.multi_zone_assertions import check_multi_zone_assertion


def _service_validation_error():
    from homeassistant.exceptions import ServiceValidationError  # noqa: PLC0415

    return ServiceValidationError


class TestServiceCallsResolveByEntryId:
    """Each service call now targets the zone named by call.data["entry_id"]."""

    def test_reset_learning_data_only_affects_the_named_zone(self):
        """The literal Gap 5 scenario: two zones, a destructive call, correct targeting.

        Occupant-facing framing: before this fix, a user resetting the
        bedroom zone's thermal model after replacing that room's thermostat
        could silently wipe the living-room zone's weeks of learned data
        instead, with no error. This test confirms the fix: calling
        reset_learning_data with zone_0's entry_id resets ONLY zone_0, and
        zone_1's learning state is provably untouched.
        """
        from custom_components.climate_advisor.const import DOMAIN

        zones, fake_hass, _scheduler = build_headless_multi_zone(zone_count=2)
        zone_0_id = zones["zone_0"]["entry"].entry_id

        # Seed observable state on BOTH zones so "before != after" is meaningful.
        zones["zone_0"]["coordinator"].learning._state.dismissed_suggestions = ["zone_0_seed"]
        zones["zone_1"]["coordinator"].learning._state.dismissed_suggestions = ["zone_1_seed"]

        run_coro(fake_hass.services.async_call(DOMAIN, "reset_learning_data", {"entry_id": zone_0_id, "scope": "all"}))

        assert zones["zone_0"]["coordinator"].learning._state.dismissed_suggestions == []
        assert zones["zone_1"]["coordinator"].learning._state.dismissed_suggestions == ["zone_1_seed"]

    def test_cross_zone_isolation_assertion_now_passes(self):
        """Same check via the harness's own cross_zone_isolation evaluator.

        Mirrors test_sim_harness_multi_zone.py's
        test_cross_zone_isolation_detects_the_known_gap_5_bleed, which
        documented the PRE-fix behavior (passed is False, "VIOLATED" in
        detail). Post-fix, the same assertion — now given the required
        entry_id targeting zone_0 — must report the unaffected zone (zone_1)
        as genuinely unaffected.
        """
        zones, fake_hass, _scheduler = build_headless_multi_zone(zone_count=2)
        zones["zone_1"]["coordinator"].learning._state.dismissed_suggestions = ["seeded_suggestion_key"]

        zone_0_id = zones["zone_0"]["entry"].entry_id
        assertion = {
            "type": "cross_zone_isolation",
            "action_zone": "zone_0",
            "service": "reset_learning_data",
            "service_data": {"entry_id": zone_0_id},
            "unaffected_zone": "zone_1",
            "unaffected_field": "learning._state.dismissed_suggestions",
        }
        passed, detail = run_coro(check_multi_zone_assertion(zones, fake_hass, assertion))
        assert passed is True, detail

    def test_force_reclassify_targets_named_zone(self):
        from custom_components.climate_advisor.const import DOMAIN

        zones, fake_hass, _scheduler = build_headless_multi_zone(zone_count=2)
        zone_0_id = zones["zone_0"]["entry"].entry_id
        coord_0 = zones["zone_0"]["coordinator"]

        calls = []
        coord_0.async_request_refresh = _record_and_noop(calls)

        run_coro(fake_hass.services.async_call(DOMAIN, "force_reclassify", {"entry_id": zone_0_id}))

        assert calls == [True]

    def test_respond_to_suggestion_targets_named_zone(self):
        from custom_components.climate_advisor.const import DOMAIN

        zones, fake_hass, _scheduler = build_headless_multi_zone(zone_count=2)
        zone_0_id = zones["zone_0"]["entry"].entry_id
        zone_1_id = zones["zone_1"]["entry"].entry_id
        coord_0 = zones["zone_0"]["coordinator"]
        coord_1 = zones["zone_1"]["coordinator"]

        # Seed a real dismissible suggestion key on both zones' learning state.
        coord_0.learning._state.dismissed_suggestions = []
        coord_1.learning._state.dismissed_suggestions = []

        run_coro(
            fake_hass.services.async_call(
                DOMAIN,
                "respond_to_suggestion",
                {"entry_id": zone_0_id, "action": "dismiss", "suggestion_key": "some_suggestion"},
            )
        )

        assert coord_0.learning._state.dismissed_suggestions == ["some_suggestion"]
        assert coord_1.learning._state.dismissed_suggestions == []
        assert zone_1_id  # sanity: fixture var used


def _record_and_noop(calls):
    async def _fn():
        calls.append(True)

    return _fn


class TestServiceCallValidation:
    """Unknown/missing entry_id must fail loudly, not silently misdirect."""

    def test_reset_learning_data_rejects_unknown_entry_id(self):
        from custom_components.climate_advisor.const import DOMAIN

        zones, fake_hass, _scheduler = build_headless_multi_zone(zone_count=2)
        ServiceValidationError = _service_validation_error()

        with pytest.raises(ServiceValidationError):
            run_coro(
                fake_hass.services.async_call(
                    DOMAIN, "reset_learning_data", {"entry_id": "not_a_real_entry", "scope": "all"}
                )
            )

    def test_reset_learning_data_rejects_missing_entry_id(self):
        from custom_components.climate_advisor.const import DOMAIN

        zones, fake_hass, _scheduler = build_headless_multi_zone(zone_count=2)
        ServiceValidationError = _service_validation_error()

        with pytest.raises(ServiceValidationError):
            run_coro(fake_hass.services.async_call(DOMAIN, "reset_learning_data", {"scope": "all"}))

    def test_dump_diagnostics_rejects_unknown_entry_id(self):
        """handle_dump_diagnostics is the one handler that closes over `entry`,
        not `coordinator` — confirm its resolution path is validated the same way.
        """
        from custom_components.climate_advisor.const import DOMAIN

        zones, fake_hass, _scheduler = build_headless_multi_zone(zone_count=2)
        ServiceValidationError = _service_validation_error()

        with pytest.raises(ServiceValidationError):
            run_coro(fake_hass.services.async_call(DOMAIN, "dump_diagnostics", {"entry_id": "nope"}))

    def test_service_call_after_zone_unload_is_rejected_not_misdirected(self):
        """Gap 9's literal scenario: zone deleted, service call against its old entry_id.

        Occupant-facing framing: before this fix, deleting a zone left its
        services silently acting on a now-defunct LearningEngine with no
        error. After the fix, the unloaded zone's entry_id is no longer in
        hass.data[DOMAIN], so the call is rejected outright instead of
        appearing to succeed against dead state.
        """
        from custom_components.climate_advisor import async_unload_entry
        from custom_components.climate_advisor.const import DOMAIN

        zones, fake_hass, _scheduler = build_headless_multi_zone(zone_count=2)
        zone_1_entry = zones["zone_1"]["entry"]
        ServiceValidationError = _service_validation_error()

        run_coro(async_unload_entry(fake_hass, zone_1_entry))

        with pytest.raises(ServiceValidationError):
            run_coro(
                fake_hass.services.async_call(
                    DOMAIN, "reset_learning_data", {"entry_id": zone_1_entry.entry_id, "scope": "all"}
                )
            )

        # The surviving zone (zone_0) must still be reachable — Gap 9 fixed,
        # not overcorrected into tearing down services while a zone remains.
        zone_0_id = zones["zone_0"]["entry"].entry_id
        run_coro(
            fake_hass.services.async_call(DOMAIN, "reset_learning_data", {"entry_id": zone_0_id, "scope": "all"})
        )  # must not raise


class TestServiceTeardown:
    """Gap 9: services must be removed once the LAST zone unloads, not before."""

    def test_services_removed_once_last_zone_unloads(self):
        from custom_components.climate_advisor import async_unload_entry
        from custom_components.climate_advisor.const import DOMAIN
        from tools.sim_harness.multi_zone_assertions import KNOWN_SERVICE_NAMES

        zones, fake_hass, _scheduler = build_headless_multi_zone(zone_count=2)

        run_coro(async_unload_entry(fake_hass, zones["zone_0"]["entry"]))
        for name in KNOWN_SERVICE_NAMES:
            assert fake_hass.services.has_service(DOMAIN, name), (
                f"{name} was removed after only one of two zones unloaded"
            )

        run_coro(async_unload_entry(fake_hass, zones["zone_1"]["entry"]))
        for name in KNOWN_SERVICE_NAMES:
            assert not fake_hass.services.has_service(DOMAIN, name), (
                f"{name} still registered after the last zone unloaded"
            )

    def test_teardown_cleanup_assertion_confirms_services_survive(self):
        zones, fake_hass, _scheduler = build_headless_multi_zone(zone_count=2)
        passed, detail = run_coro(
            check_multi_zone_assertion(
                zones,
                fake_hass,
                {"type": "teardown_cleanup", "unload_entry": "zone_1", "expect_services_present": True},
            )
        )
        assert passed is True, detail
