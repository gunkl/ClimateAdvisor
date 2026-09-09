"""Tests for Issue #796 Gap 4: api.py entry_id resolution across zones.

_get_coordinator(hass, request) (api.py) now resolves the request's target
coordinator via zone_registry, driven by an optional `entry_id` query
parameter, instead of always returning `next(iter(entries.values()))`. This
file drives a REPRESENTATIVE SAMPLE of the 21 view classes that call
`_get_coordinator` — not all 21 — because every one of them funnels through
the exact same two lines (`entry_id = request.query.get("entry_id")`, then
`get_coordinator`/`get_default_coordinator`); once the shared helper is
proven correct for GET and POST views, the remaining 18 sites differ only in
what they do with the resolved coordinator afterward (already covered by
their own existing per-view tests, unaffected by this change). Sampled here:

- ClimateAdvisorAutomationStateView (GET) — a simple GET view (one field
  passthrough from coordinator.get_debug_state()).
- ClimateAdvisorEnginesView (GET) — a second GET view with its own extra
  gating (hasattr checks) before reading from the resolved coordinator.
- ClimateAdvisorForceReclassifyView (POST) — a POST view with a coordinator
  side effect (async_request_refresh).
- ClimateAdvisorToggleAutomationView (POST) — a second POST view with a
  different coordinator side effect (set_automation_enabled).

Backward-compat guarantee (explicitly tested): entry_id absent → falls back
to get_default_coordinator(), which is a no-op change for single-zone
installs — this is the load-bearing test for every existing single-zone
deployment upgrading through this change.
"""

from __future__ import annotations

import asyncio
from unittest.mock import MagicMock

from custom_components.climate_advisor.api import (
    ClimateAdvisorAutomationStateView,
    ClimateAdvisorEnginesView,
    ClimateAdvisorForceReclassifyView,
    ClimateAdvisorToggleAutomationView,
)
from custom_components.climate_advisor.const import DOMAIN


def _make_request(coordinators: dict[str, MagicMock], entry_id: str | None = None) -> MagicMock:
    hass = MagicMock()
    hass.data = {DOMAIN: dict(coordinators)}
    # No second zone in these tests needs the async_entries()-driven fallback
    # (see test_zone_registry.py for that path in isolation) — an empty list
    # here would only matter if len(coordinators) > 1 and entry_id is absent,
    # which none of these cases exercise.
    hass.config_entries.async_entries = MagicMock(return_value=[])
    req = MagicMock()
    req.app = {"hass": hass}
    req.query = {"entry_id": entry_id} if entry_id else {}
    return req


def _get(view_cls, coordinators, entry_id=None):
    view = view_cls()
    request = _make_request(coordinators, entry_id)
    return asyncio.run(view.get(request))


def _post(view_cls, coordinators, entry_id=None):
    view = view_cls()
    request = _make_request(coordinators, entry_id)
    return asyncio.run(view.post(request))


class TestEntryIdResolutionAcrossZones:
    """A request naming entry_id must resolve THAT zone, not the other one."""

    def test_engines_view_resolves_named_zone(self):
        zone_a, zone_b = MagicMock(), MagicMock()
        zone_a.learning.get_engine_status.return_value = {"engine": "a"}
        zone_a.config = {"temp_unit": "fahrenheit"}
        zone_b.learning.get_engine_status.return_value = {"engine": "b"}
        zone_b.config = {"temp_unit": "fahrenheit"}

        resp = _get(
            ClimateAdvisorEnginesView,
            {"entry_a": zone_a, "entry_b": zone_b},
            entry_id="entry_b",
        )
        assert resp.json_data["engine"] == "b"

    def test_engines_view_resolves_the_other_named_zone(self):
        """Same setup, opposite entry_id — proves it's not incidentally always picking one."""
        zone_a, zone_b = MagicMock(), MagicMock()
        zone_a.learning.get_engine_status.return_value = {"engine": "a"}
        zone_a.config = {"temp_unit": "fahrenheit"}
        zone_b.learning.get_engine_status.return_value = {"engine": "b"}
        zone_b.config = {"temp_unit": "fahrenheit"}

        resp = _get(
            ClimateAdvisorEnginesView,
            {"entry_a": zone_a, "entry_b": zone_b},
            entry_id="entry_a",
        )
        assert resp.json_data["engine"] == "a"

    def test_automation_state_view_resolves_named_zone(self):
        zone_a, zone_b = MagicMock(), MagicMock()
        zone_a.get_debug_state.return_value = {"zone": "a"}
        zone_b.get_debug_state.return_value = {"zone": "b"}

        resp = _get(
            ClimateAdvisorAutomationStateView,
            {"entry_a": zone_a, "entry_b": zone_b},
            entry_id="entry_b",
        )
        assert resp.json_data == {"zone": "b"}
        zone_b.get_debug_state.assert_called_once()
        zone_a.get_debug_state.assert_not_called()

    def test_force_reclassify_view_acts_only_on_named_zone(self):
        from unittest.mock import AsyncMock

        zone_a, zone_b = MagicMock(), MagicMock()
        zone_a.async_request_refresh = AsyncMock()
        zone_b.async_request_refresh = AsyncMock()

        _post(
            ClimateAdvisorForceReclassifyView,
            {"entry_a": zone_a, "entry_b": zone_b},
            entry_id="entry_a",
        )

        zone_a.async_request_refresh.assert_called_once()
        zone_b.async_request_refresh.assert_not_called()

    def test_toggle_automation_view_acts_only_on_named_zone(self):
        zone_a, zone_b = MagicMock(), MagicMock()
        zone_a.automation_enabled = False
        zone_b.automation_enabled = False

        _post(
            ClimateAdvisorToggleAutomationView,
            {"entry_a": zone_a, "entry_b": zone_b},
            entry_id="entry_b",
        )

        zone_b.set_automation_enabled.assert_called_once_with(True)
        zone_a.set_automation_enabled.assert_not_called()

    def test_unknown_entry_id_returns_not_loaded_error(self):
        """An entry_id that resolves to nothing must fail closed, not silently pick another zone."""
        zone_a = MagicMock()

        resp = _get(
            ClimateAdvisorAutomationStateView,
            {"entry_a": zone_a},
            entry_id="entry_does_not_exist",
        )
        assert resp.status == 503
        assert resp.json_data["error"] == "Climate Advisor not loaded"
        zone_a.get_debug_state.assert_not_called()


class TestEntryIdAbsentBackwardCompat:
    """No entry_id sent (every pre-existing caller) — single-zone behavior unchanged."""

    def test_single_zone_no_entry_id_resolves_the_one_zone(self):
        zone_a = MagicMock()
        zone_a.get_debug_state.return_value = {"only": "zone"}

        resp = _get(ClimateAdvisorAutomationStateView, {"entry_a": zone_a}, entry_id=None)

        assert resp.json_data == {"only": "zone"}
        zone_a.get_debug_state.assert_called_once()

    def test_single_zone_no_entry_id_post_view_acts_on_the_one_zone(self):
        zone_a = MagicMock()
        zone_a.automation_enabled = False

        _post(ClimateAdvisorToggleAutomationView, {"entry_a": zone_a}, entry_id=None)

        zone_a.set_automation_enabled.assert_called_once_with(True)

    def test_zero_zones_no_entry_id_returns_not_loaded_error(self):
        resp = _get(ClimateAdvisorAutomationStateView, {}, entry_id=None)
        assert resp.status == 503
        assert resp.json_data["error"] == "Climate Advisor not loaded"


class TestAmbiguousZonePostRefusal:
    """Issue #885: a POST view with 2+ zones loaded and no entry_id must refuse
    to guess — not silently actuate whichever zone happens to be "first". This
    is the actual safety property the centralized resolve_zone() adds; the
    2-zone/no-entry_id/POST case had no coverage at all before this change
    (confirmed by reading this file in full during design).

    Uses its own request builder (not the shared _make_request() above) because
    it needs hass.config_entries.async_entries() to actually list both zones —
    _make_request()'s deliberate `return_value=[]` is correct for the other
    tests in this file (none of which exercise the 2-zone/no-entry_id path) but
    would make zone_registry.list_zones() (used to build the refusal payload's
    zone_count) report 0 zones instead of 2 here.
    """

    def _make_ambiguous_request(self, coordinators: dict[str, MagicMock]) -> MagicMock:
        hass = MagicMock()
        hass.data = {DOMAIN: dict(coordinators)}
        entries = []
        for entry_id in coordinators:
            entry = MagicMock()
            entry.entry_id = entry_id
            entry.title = entry_id
            entries.append(entry)
        hass.config_entries.async_entries = MagicMock(return_value=entries)
        req = MagicMock()
        req.app = {"hass": hass}
        req.query = {}
        req.method = "POST"
        req.path = "/api/climate_advisor/x"
        req.remote = "127.0.0.1"
        return req

    def test_toggle_automation_view_refuses_when_ambiguous(self):
        zone_a, zone_b = MagicMock(), MagicMock()
        zone_a.automation_enabled = False
        zone_b.automation_enabled = False

        view = ClimateAdvisorToggleAutomationView()
        request = self._make_ambiguous_request({"entry_a": zone_a, "entry_b": zone_b})
        resp = asyncio.run(view.post(request))

        assert resp.status == 400
        assert resp.json_data["zone_selection_required"] is True
        assert resp.json_data["zone_count"] == 2
        # The core safety property: no zone was actuated, not "the wrong one
        # was picked but at least it was logged."
        zone_a.set_automation_enabled.assert_not_called()
        zone_b.set_automation_enabled.assert_not_called()

    def test_force_reclassify_view_refuses_when_ambiguous(self):
        from unittest.mock import AsyncMock

        zone_a, zone_b = MagicMock(), MagicMock()
        zone_a.async_request_refresh = AsyncMock()
        zone_b.async_request_refresh = AsyncMock()

        view = ClimateAdvisorForceReclassifyView()
        request = self._make_ambiguous_request({"entry_a": zone_a, "entry_b": zone_b})
        resp = asyncio.run(view.post(request))

        assert resp.status == 400
        zone_a.async_request_refresh.assert_not_called()
        zone_b.async_request_refresh.assert_not_called()

    def test_get_view_still_guesses_when_ambiguous_not_refused(self):
        """A GET (display-only) view keeps the pre-existing degrade-with-warning
        behavior — only POST/action views are made strict by this change."""
        zone_a, zone_b = MagicMock(), MagicMock()
        zone_a.get_debug_state.return_value = {"zone": "a"}
        zone_b.get_debug_state.return_value = {"zone": "b"}

        view = ClimateAdvisorAutomationStateView()
        request = self._make_ambiguous_request({"entry_a": zone_a, "entry_b": zone_b})
        request.method = "GET"
        resp = asyncio.run(view.get(request))

        assert resp.status == 200
        assert "zone_selection_required" not in (resp.json_data or {})
