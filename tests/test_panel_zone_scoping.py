"""Regression coverage for Issue #796 Gaps 6 and 8 (Step 5, PR5).

Gap 6: the REST API views (``api.py``'s ``API_VIEWS`` — every view's ``url``
class attribute is a fixed ``API_*`` constant from ``const.py``) and the
dashboard panel (``PANEL_URL``/``PANEL_FRONTEND_PATH``, also fixed constants)
are domain-wide shared resources, not per-zone resources. Fix: register them
ONCE, guarded, on whichever zone's ``async_setup_entry()`` runs first — see
``__init__.py``'s ``_PANEL_HASS_DATA_KEY`` guard, mirroring the existing
``has_service()`` guard already used for ``ZONE_SCOPED_SERVICES``.

Gap 8: ``async_unload_entry()`` previously called ``async_remove_panel()``
unconditionally on every unload, stranding a surviving zone with no
dashboard/API access. Fix: fold the panel removal into the same
``if not hass.data[DOMAIN]:`` guard already used for
``log_capture.uninstall()``/service teardown (Gap 9), so the panel is only
removed once the LAST zone unloads.

These tests drive the REAL ``async_setup_entry()``/``async_unload_entry()``
via ``build_headless_multi_zone()`` (Issue #796 Step 2's harness extension) —
not a mirror of the production guard logic — per this project's no-mirror-
tests doctrine (CLAUDE.md).
"""

from __future__ import annotations

from custom_components.climate_advisor.api import API_VIEWS
from custom_components.climate_advisor.const import DOMAIN, PANEL_FRONTEND_PATH
from tools.sim_harness._loop import run_coro
from tools.sim_harness.build_coordinator import build_headless_multi_zone
from tools.sim_harness.multi_zone_assertions import check_multi_zone_assertion


class TestPanelAndViewRegistrationScoping:
    """Gap 6: shared panel/views register once, not once per zone."""

    def test_two_zones_set_up_without_crashing(self):
        zones, fake_hass, _scheduler = build_headless_multi_zone(zone_count=2)

        assert len(zones) == 2
        assert PANEL_FRONTEND_PATH in fake_hass.data.get("_panels", {})

    def test_views_registered_exactly_once_across_two_zones(self):
        """The second zone's setup must not re-register the shared views.

        Before the Gap 6 fix, every zone's async_setup_entry() unconditionally
        looped over API_VIEWS and called hass.http.register_view() again —
        with two zones that means len(API_VIEWS) * 2 registrations. The guard
        (_PANEL_HASS_DATA_KEY) must make the second zone skip this loop
        entirely.
        """
        zones, fake_hass, _scheduler = build_headless_multi_zone(zone_count=2)

        assert len(zones) == 2
        assert len(fake_hass.http.registered_views) == len(API_VIEWS), (
            "expected the shared REST views to be registered exactly once "
            f"(len(API_VIEWS)={len(API_VIEWS)}), got "
            f"{len(fake_hass.http.registered_views)} — the second zone's setup "
            "likely re-ran the registration loop instead of being guarded"
        )

    def test_panel_registered_exactly_once_across_two_zones(self):
        zones, fake_hass, _scheduler = build_headless_multi_zone(zone_count=2)

        assert len(zones) == 2
        panels = fake_hass.data.get("_panels", {})
        # Only one frontend_url_path is ever used (PANEL_FRONTEND_PATH is a
        # fixed constant), so this also holds true even without the guard —
        # the guard's real effect is on registered_views/service registration
        # counts above and the "did the second zone even attempt it" check
        # below, not on this dict's size.
        assert set(panels.keys()) == {PANEL_FRONTEND_PATH}

    def test_second_zone_setup_does_not_attempt_panel_registration(self):
        """Direct proof the guard skips the second zone's attempt entirely.

        This is the real safety property Gap 6 cares about: PR3's spike (not
        run — see docs/multi-zone-spec.md) into whether a duplicate
        registration raises AFTER the first zone's control loop is already
        live was never answered. The guard sidesteps the question by never
        making the second zone's registration attempt in the first place.
        The harness's fake registration functions don't raise on a duplicate
        call (they just overwrite), so the only way to prove the second zone
        was actually skipped is the registered_views count above — this test
        makes that intent explicit via the same evidence.
        """
        zones, fake_hass, _scheduler = build_headless_multi_zone(zone_count=2)

        assert len(zones) == 2
        # If the second zone had attempted registration, registered_views
        # would hold 2 * len(API_VIEWS) entries (the fake appends, it never
        # deduplicates) — asserting the exact single-registration count here
        # is the direct evidence the guard fired for zone 2, not just that
        # the end state happens to look right.
        assert len(fake_hass.http.registered_views) == len(API_VIEWS)


class TestPanelTeardownScoping:
    """Gap 8: shared panel/services survive an unload as long as any zone remains."""

    def test_panel_and_services_survive_when_one_of_two_zones_unloads(self):
        zones, fake_hass, _scheduler = build_headless_multi_zone(zone_count=2)
        assert set(zones.keys()) == {"zone_0", "zone_1"}

        ok, reason = run_coro(
            check_multi_zone_assertion(
                zones,
                fake_hass,
                {
                    "type": "teardown_cleanup",
                    "unload_entry": "zone_1",
                    "expect_services_present": True,
                    "expect_panel_present": True,
                },
            )
        )
        assert ok, reason
        # The surviving zone must still be resolvable — its coordinator was
        # never touched by unloading the other zone.
        assert fake_hass.data[DOMAIN][zones["zone_0"]["entry"].entry_id] is zones["zone_0"]["coordinator"]

    def test_panel_and_services_removed_only_after_last_zone_unloads(self):
        zones, fake_hass, _scheduler = build_headless_multi_zone(zone_count=2)

        # Unload the first zone — panel/services must survive (zone_1 remains).
        ok, reason = run_coro(
            check_multi_zone_assertion(
                zones,
                fake_hass,
                {
                    "type": "teardown_cleanup",
                    "unload_entry": "zone_0",
                    "expect_services_present": True,
                    "expect_panel_present": True,
                },
            )
        )
        assert ok, reason

        # Unload the last remaining zone — panel/services must now be gone.
        ok, reason = run_coro(
            check_multi_zone_assertion(
                zones,
                fake_hass,
                {
                    "type": "teardown_cleanup",
                    "unload_entry": "zone_1",
                    "expect_services_present": False,
                    "expect_panel_present": False,
                },
            )
        )
        assert ok, reason
        assert fake_hass.data[DOMAIN] == {}

    def test_panel_registration_flag_cleared_after_last_zone_unloads(self):
        """The _PANEL_HASS_DATA_KEY guard flag must reset so a later zone can re-register.

        Without clearing this flag on last-zone teardown, adding a new zone
        after every prior zone was removed would silently skip panel/view
        registration forever (the guard would see a stale "already
        registered" flag from an instance that no longer exists).
        """
        from custom_components.climate_advisor import _PANEL_HASS_DATA_KEY

        zones, fake_hass, _scheduler = build_headless_multi_zone(zone_count=1)
        assert fake_hass.data.get(_PANEL_HASS_DATA_KEY) is True

        from custom_components.climate_advisor import async_unload_entry

        run_coro(async_unload_entry(fake_hass, zones["zone_0"]["entry"]))

        assert _PANEL_HASS_DATA_KEY not in fake_hass.data
        assert PANEL_FRONTEND_PATH not in fake_hass.data.get("_panels", {})
