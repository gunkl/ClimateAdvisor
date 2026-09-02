"""Regression coverage for Issue #796 Step 9 (PR9): dashboard zone selector backend.

``ClimateAdvisorStatusView.get()`` (api.py) gained two fields —
``zones: [{entry_id, title}]`` and ``zone_count`` — so the frontend has enough
information, on the one endpoint ``loadStatus()`` polls every cycle regardless
of which tab is active, to decide whether to render the zone-selector row at
all (only when ``zone_count > 1``, per docs/multi-zone-spec.md's "Dashboard: a
zone selector" section and Mock 5). Both fields are computed by
``zone_registry.list_zones()`` — the single source of truth this view, and any
future caller, shares (see zone_registry.py's own docstring for why this
wasn't duplicated as a second parallel counting implementation).

These tests drive the REAL ``ClimateAdvisorStatusView.get()`` against a REAL
multi-zone setup built by ``build_headless_multi_zone()`` (Issue #796's
harness extension) — not a hand-rolled mirror of ``list_zones()`` — per this
project's no-mirror-tests doctrine (CLAUDE.md).
"""

from __future__ import annotations

import asyncio
from unittest.mock import MagicMock

from custom_components.climate_advisor.api import ClimateAdvisorStatusView
from custom_components.climate_advisor.const import DOMAIN
from tools.sim_harness.build_coordinator import build_headless_multi_zone


def _get_status(fake_hass, entry_id: str | None = None) -> dict:
    """Drive the real ClimateAdvisorStatusView.get() and return its JSON body."""
    view = ClimateAdvisorStatusView()
    request = MagicMock()
    request.app = {"hass": fake_hass}
    request.query = {"entry_id": entry_id} if entry_id else {}
    resp = asyncio.run(view.get(request))
    return resp.json_data


class TestZoneCountAndListSingleZone:
    """A single-zone install must report zone_count == 1 — the majority case,
    and the one the frontend's `zone_count > 1` gate must keep silent for."""

    def test_single_zone_reports_count_one(self):
        zones, fake_hass, _scheduler = build_headless_multi_zone(zone_count=1)
        assert len(zones) == 1

        body = _get_status(fake_hass)

        assert body["zone_count"] == 1
        assert len(body["zones"]) == 1
        assert body["zones"][0]["entry_id"] == zones["zone_0"]["entry"].entry_id
        assert body["zones"][0]["title"] == zones["zone_0"]["entry"].title


class TestZoneCountAndListTwoZones:
    def test_two_zones_reports_count_two_with_both_titles(self):
        zones, fake_hass, _scheduler = build_headless_multi_zone(zone_count=2)
        assert set(zones.keys()) == {"zone_0", "zone_1"}

        body = _get_status(fake_hass)

        assert body["zone_count"] == 2
        assert [z["title"] for z in body["zones"]] == ["Zone A", "Zone B"]
        assert [z["entry_id"] for z in body["zones"]] == [
            zones["zone_0"]["entry"].entry_id,
            zones["zone_1"]["entry"].entry_id,
        ]

    def test_zones_field_unaffected_by_which_zone_the_request_targets(self):
        """The zones/zone_count fields describe the WHOLE install, not just the
        resolved coordinator — a request scoped to zone_1 must still see both
        zones listed, otherwise the frontend could never discover the zone it
        isn't currently viewing."""
        zones, fake_hass, _scheduler = build_headless_multi_zone(zone_count=2)

        body = _get_status(fake_hass, entry_id=zones["zone_1"]["entry"].entry_id)

        assert body["zone_count"] == 2
        assert {z["entry_id"] for z in body["zones"]} == {
            zones["zone_0"]["entry"].entry_id,
            zones["zone_1"]["entry"].entry_id,
        }


class TestZoneCountAndListThreeZones:
    def test_three_zones_reports_count_three_in_stable_setup_order(self):
        """Title ordering must match hass.config_entries.async_entries(DOMAIN)'s
        stable (insertion/setup) order — NOT hass.data[DOMAIN] dict-iteration
        order, which is not guaranteed stable across restarts. This mirrors the
        exact precedent zone_registry.get_default_coordinator() already
        established at zone_registry.py:135 (see that function's docstring)."""
        zones, fake_hass, _scheduler = build_headless_multi_zone(zone_count=3)
        assert set(zones.keys()) == {"zone_0", "zone_1", "zone_2"}

        body = _get_status(fake_hass)

        assert body["zone_count"] == 3
        assert [z["title"] for z in body["zones"]] == ["Zone A", "Zone B", "Zone C"]
        expected_order = [fake_hass.config_entries.async_entries(DOMAIN)[i].entry_id for i in range(3)]
        assert [z["entry_id"] for z in body["zones"]] == expected_order

    def test_dict_iteration_order_is_not_what_drives_the_result(self):
        """Insert hass.data[DOMAIN] in a deliberately scrambled key order and
        confirm the response order still follows async_entries(DOMAIN), not
        dict order — proving list_zones() doesn't fall back to dict iteration.
        """
        zones, fake_hass, _scheduler = build_headless_multi_zone(zone_count=3)
        loaded = fake_hass.data[DOMAIN]
        scrambled = dict(reversed(list(loaded.items())))
        loaded.clear()
        loaded.update(scrambled)

        body = _get_status(fake_hass)

        assert [z["title"] for z in body["zones"]] == ["Zone A", "Zone B", "Zone C"]


class TestBootstrapNeverGuesses:
    """Issue #813: the dashboard's first-ever-visit /status call (no entry_id
    yet — nothing to send until a zone has been discovered/selected) must
    never resolve an arbitrary coordinator on a 2+-zone install. Before this
    fix, that call went through _get_coordinator() -> zone_registry.
    get_default_coordinator()'s ambiguous fallback, silently picking a zone
    and only telling the operator via a throttled log line / Repairs card.
    Now it returns a zone-list-only payload with no coordinator resolved at
    all, and the frontend re-requests with an explicit entry_id."""

    def test_no_entry_id_multi_zone_returns_zone_selection_required_not_a_guess(self):
        zones, fake_hass, _scheduler = build_headless_multi_zone(zone_count=2)

        body = _get_status(fake_hass)

        assert body.get("zone_selection_required") is True
        assert body["zone_count"] == 2
        assert {z["entry_id"] for z in body["zones"]} == {
            zones["zone_0"]["entry"].entry_id,
            zones["zone_1"]["entry"].entry_id,
        }
        # No coordinator-dependent fields present — proves this response never
        # resolved (guessed) a coordinator.
        assert "hvac_mode" not in body
        assert "day_type" not in body

    def test_no_entry_id_multi_zone_never_calls_get_default_coordinator(self):
        from unittest.mock import patch

        zones, fake_hass, _scheduler = build_headless_multi_zone(zone_count=2)

        with patch("custom_components.climate_advisor.api.zone_registry.get_default_coordinator") as mock_default:
            body = _get_status(fake_hass)

        mock_default.assert_not_called()
        assert body.get("zone_selection_required") is True

    def test_no_entry_id_single_zone_still_resolves_normally(self):
        """Single-zone installs are unaffected — no zone_selection_required,
        full status payload, matching today's behavior exactly."""
        zones, fake_hass, _scheduler = build_headless_multi_zone(zone_count=1)

        body = _get_status(fake_hass)

        assert "zone_selection_required" not in body
        assert body["zone_count"] == 1
        assert "hvac_mode" in body

    def test_explicit_entry_id_multi_zone_returns_full_status_not_bootstrap(self):
        zones, fake_hass, _scheduler = build_headless_multi_zone(zone_count=2)

        body = _get_status(fake_hass, entry_id=zones["zone_1"]["entry"].entry_id)

        assert "zone_selection_required" not in body
        assert body["zone_count"] == 2
        assert "hvac_mode" in body


class TestZoneCountAfterTeardown:
    def test_zone_count_drops_after_one_of_three_unloads(self):
        from tools.sim_harness._loop import run_coro

        zones, fake_hass, _scheduler = build_headless_multi_zone(zone_count=3)

        from custom_components.climate_advisor import async_unload_entry

        run_coro(async_unload_entry(fake_hass, zones["zone_1"]["entry"]))

        body = _get_status(fake_hass)
        assert body["zone_count"] == 2
        assert sorted(z["title"] for z in body["zones"]) == ["Zone A", "Zone C"]
