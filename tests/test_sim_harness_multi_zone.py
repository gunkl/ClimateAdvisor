"""Smoke tests for the multi-zone harness extension (Issue #796, Step 2).

Scope, per docs/multi-zone-spec.md's "Testing Without Multi-Zone Hardware":
confirm the HARNESS itself works — two real config entries set up against one
shared FakeHass via the REAL async_setup_entry(), without crashing, and
without the harness accidentally sharing mutable state between zones that it
shouldn't (distinct coordinator/config/learning instances, distinct climate
entities). This does NOT assert that production is bug-free — Gaps 5/6/8/9
(cross-zone service binding, panel teardown, etc.) are known-unfixed as of
this step; where the smoke test's own assertions incidentally demonstrate a
gap's current (broken) behavior, that is called out explicitly, not asserted
as "correct."

The three new multi-zone assertion-type evaluators (multi_zone_assertions.py)
are tested separately against this same harness, since exercising them here
is the only way to avoid becoming a mirror test (Issue #434 doctrine) of
their own logic.
"""

from __future__ import annotations

from tools.sim_harness._loop import run_coro
from tools.sim_harness.build_coordinator import build_headless_multi_zone
from tools.sim_harness.multi_zone_assertions import (
    ASSERTION_TYPES,
    check_multi_zone_assertion,
    check_service_registry_binding,
    resolve_dotted_field,
    validate_zones_schema,
)


class TestBuildHeadlessMultiZone:
    """build_headless_multi_zone() drives the REAL async_setup_entry() per zone."""

    def test_two_zones_come_up_without_crashing(self):
        zones, fake_hass, _scheduler = build_headless_multi_zone(zone_count=2)

        assert set(zones.keys()) == {"zone_0", "zone_1"}
        for label, info in zones.items():
            assert info["coordinator"] is not None
            assert info["entry"].entry_id.startswith(label)
            # Real async_setup_entry() populates hass.data[DOMAIN][entry_id] —
            # confirm the harness's returned coordinator IS that same object,
            # not a second hand-built stand-in.
            from custom_components.climate_advisor.const import DOMAIN

            assert fake_hass.data[DOMAIN][info["entry"].entry_id] is info["coordinator"]

    def test_zones_do_not_share_mutable_harness_state(self):
        """Each zone must get its own coordinator/config/learning objects.

        This is about HARNESS fidelity, not production correctness — e.g. if
        build_headless_multi_zone() accidentally reused one coordinator
        object or one config dict across zones, every multi-zone scenario
        built on top of it would be silently testing nothing.
        """
        zones, _fake_hass, _scheduler = build_headless_multi_zone(zone_count=2)
        coord_a = zones["zone_0"]["coordinator"]
        coord_b = zones["zone_1"]["coordinator"]

        assert coord_a is not coord_b
        assert coord_a.config is not coord_b.config
        assert coord_a.learning is not coord_b.learning
        assert coord_a.config["climate_entity"] != coord_b.config["climate_entity"]
        assert zones["zone_0"]["climate_entity"] == "climate.zone_a_thermostat"
        assert zones["zone_1"]["climate_entity"] == "climate.zone_b_thermostat"
        # Each coordinator's own event log (Issue #236 single-engine doctrine
        # — no separate flat log) must not be the same list instance.
        assert coord_a._event_log is not coord_b._event_log

    def test_custom_configs_and_zone_labels_respected(self):
        zones, _fake_hass, _scheduler = build_headless_multi_zone(
            configs=[
                {"climate_entity": "climate.living_room", "zone_label": "living_room", "zone_title": "Living Room"},
                {"climate_entity": "climate.bedroom", "zone_label": "bedroom", "zone_title": "Bedroom"},
            ]
        )
        assert set(zones.keys()) == {"living_room", "bedroom"}
        assert zones["living_room"]["entry"].title == "Living Room"
        assert zones["living_room"]["climate_entity"] == "climate.living_room"

    def test_real_setup_registers_services_and_panel(self):
        """Confirms this harness actually exercises the code path build_headless_coordinator() skips.

        docs/multi-zone-spec.md's stated gap: the direct-construction harness
        never touches async_setup_entry(), so service registration / panel
        registration have zero automated coverage today. This proves they now
        run — not that they run correctly per-zone (that's Gap 5/8, unfixed).
        """
        from custom_components.climate_advisor.const import DOMAIN, PANEL_FRONTEND_PATH

        zones, fake_hass, _scheduler = build_headless_multi_zone(zone_count=2)

        assert fake_hass.services.has_service(DOMAIN, "reset_learning_data")
        assert PANEL_FRONTEND_PATH in fake_hass.data.get("_panels", {})
        assert len(fake_hass.http.registered_views) > 0
        assert len(zones) == 2


class TestMultiZoneAssertionTypes:
    """Unit coverage for the three new assertion-type evaluators.

    Uses the real harness (not synthetic data) so these are not mirror tests
    of their own logic — each check exercises the real FakeHass service
    registry / panel tracking built above.
    """

    def test_assertion_type_names_match_spec(self):
        assert {"cross_zone_isolation", "service_registry_binding", "teardown_cleanup"} == ASSERTION_TYPES

    def test_validate_zones_schema_accepts_absent_key(self):
        validate_zones_schema({"assertions": []})  # no "zones" key — must be a no-op

    def test_validate_zones_schema_accepts_well_formed_zones(self):
        validate_zones_schema(
            {
                "zones": [
                    {"zone_label": "zone_a", "climate_entity": "climate.a"},
                    {"zone_label": "zone_b", "climate_entity": "climate.b", "config": {}, "events": []},
                ],
                "assertions": [
                    {
                        "type": "cross_zone_isolation",
                        "action_zone": "zone_a",
                        "service": "reset_learning_data",
                        "unaffected_zone": "zone_b",
                        "unaffected_field": "learning.thermal_model.k_passive",
                    },
                    {
                        "type": "service_registry_binding",
                        "service": "reset_learning_data",
                        "expected_target_entry_id": "zone_a_entry",
                    },
                    {"type": "teardown_cleanup", "unload_entry": "zone_b", "expect_services_present": True},
                ],
            }
        )

    def test_validate_zones_schema_rejects_duplicate_labels(self):
        import pytest

        with pytest.raises(ValueError, match="duplicate zone_label"):
            validate_zones_schema(
                {
                    "zones": [
                        {"zone_label": "zone_a", "climate_entity": "climate.a"},
                        {"zone_label": "zone_a", "climate_entity": "climate.b"},
                    ]
                }
            )

    def test_validate_zones_schema_rejects_incomplete_assertion(self):
        import pytest

        with pytest.raises(ValueError, match="missing required field"):
            validate_zones_schema(
                {
                    "zones": [{"zone_label": "zone_a", "climate_entity": "climate.a"}],
                    "assertions": [{"type": "teardown_cleanup"}],  # missing unload_entry
                }
            )

    def test_resolve_dotted_field_walks_nested_attrs_and_dicts(self):
        class Inner:
            k_passive = -0.5

        class Outer:
            thermal_model = Inner()

        assert resolve_dotted_field(Outer(), "thermal_model.k_passive") == -0.5
        assert resolve_dotted_field({"a": {"b": 3}}, "a.b") == 3

    def test_resolve_dotted_field_missing_path_is_distinguishable_from_none(self):
        from tools.sim_harness.multi_zone_assertions import _FIELD_NOT_FOUND

        assert resolve_dotted_field({"a": None}, "a") is None
        assert resolve_dotted_field({"a": None}, "a.b") is _FIELD_NOT_FOUND
        assert resolve_dotted_field({}, "nope") is _FIELD_NOT_FOUND

    def test_cross_zone_isolation_detects_the_known_gap_5_bleed(self):
        """Documents current (unfixed) production behavior, not a desired outcome.

        Two zones share the global HA service namespace (confirmed by direct
        harness output — see build_headless_multi_zone()'s docstring): zone
        B's async_setup_entry() overwrites zone A's `reset_learning_data`
        handler. Calling the service therefore resets ZONE B's learning data
        (whoever's bound last) even when a caller's mental model is "zone A".
        This assertion type is built to CATCH that once a fix scopes the
        service — right now it correctly reports "VIOLATED" because the bug
        is real and unfixed. Asserting `passed is False` here is intentional:
        flipping to True with no code change would mean this evaluator quietly
        stopped detecting the bug it exists to catch.
        """
        zones, fake_hass, _scheduler = build_headless_multi_zone(zone_count=2)
        # A freshly-built coordinator's learning state has nothing populated
        # yet, so reset(scope="all") would trivially look like "no change" —
        # seed a real, observable value first so before != after actually
        # means something (learning.reset("all") replaces `_state` with a
        # fresh LearningState(), which clears this back to []).
        zones["zone_1"]["coordinator"].learning._state.dismissed_suggestions = ["seeded_suggestion_key"]
        assertion = {
            "type": "cross_zone_isolation",
            "action_zone": "zone_0",
            "service": "reset_learning_data",
            "unaffected_zone": "zone_1",
            "unaffected_field": "learning._state.dismissed_suggestions",
        }
        passed, detail = run_coro(check_multi_zone_assertion(zones, fake_hass, assertion))
        assert passed is False, f"expected the known Gap 5 bleed to still reproduce; got: {detail}"
        assert "VIOLATED" in detail

    def test_service_registry_binding_reports_last_zone_wins(self):
        """Same underlying gap as above, from the binding-introspection side:
        the currently-bound handler is zone_1's (set up second), never zone_0's.
        """
        zones, fake_hass, _scheduler = build_headless_multi_zone(zone_count=2)
        zone_1_entry_id = zones["zone_1"]["entry"].entry_id

        passed, detail = check_service_registry_binding(
            zones,
            fake_hass,
            {"service": "reset_learning_data", "expected_target_entry_id": zone_1_entry_id},
        )
        assert passed is True, detail

        zone_0_entry_id = zones["zone_0"]["entry"].entry_id
        passed_wrong, detail_wrong = check_service_registry_binding(
            zones,
            fake_hass,
            {"service": "reset_learning_data", "expected_target_entry_id": zone_0_entry_id},
        )
        assert passed_wrong is False, detail_wrong

    def test_teardown_cleanup_unloads_and_reports_panel_state(self):
        """Unloading zone_1 (of two) removes the panel unconditionally today (Gap 8) —
        confirmed via the real async_unload_entry(), not asserted as desired behavior.
        """
        zones, fake_hass, _scheduler = build_headless_multi_zone(zone_count=2)

        passed, detail = run_coro(
            check_multi_zone_assertion(
                zones,
                fake_hass,
                {"type": "teardown_cleanup", "unload_entry": "zone_1", "expect_panel_present": True},
            )
        )
        # expect_panel_present=True fails today because async_unload_entry()
        # removes the panel unconditionally (Gap 8) — this is the harness
        # correctly detecting a real, currently-unfixed gap.
        assert passed is False
        assert "panel_present" in detail
