"""Issue #1454: ``GET /api/climate_advisor/zones`` lists the zones and never raises the Repairs card.

Occupant view: after a Home Assistant restart an "Ambiguous zone selection" Repairs card appeared although nothing
was misconfigured. Climate Advisor's own dashboards raised it: they asked an unscoped ``GET /status`` for the zone
list (first visit, or the retry after a startup 503). They now ask ``GET /zones``, which names no zone by design and
must not count as unscoped, while an unscoped ``GET /status`` still raises the card (owner's rule, Issue #1418).

Drives the REAL views against a REAL multi-zone setup (``build_headless_multi_zone()``), per the no-mirror doctrine.
"""

from __future__ import annotations

import asyncio
from unittest.mock import MagicMock, patch

from custom_components.climate_advisor import zone_registry
from custom_components.climate_advisor.api import (
    API_VIEWS,
    ZONELESS_VIEWS,
    ClimateAdvisorStatusView,
    ClimateAdvisorZonesView,
)
from custom_components.climate_advisor.const import API_ZONES, VERSION
from custom_components.climate_advisor.dashboard_api import DASHBOARD_VIEWS
from tools.sim_harness.build_coordinator import build_headless_multi_zone


def _request(fake_hass, query: dict | None = None, path: str = API_ZONES) -> MagicMock:
    request = MagicMock()
    request.app = {"hass": fake_hass}
    request.query = query or {}
    request.method = "GET"
    request.path = path
    request.remote = "10.0.0.5"
    return request


def _get(view, fake_hass, query=None, path=API_ZONES) -> dict:
    return asyncio.run(view.get(_request(fake_hass, query, path))).json_data


def test_zones_lists_every_zone_in_stable_order_and_raises_nothing():
    zones, fake_hass, _scheduler = build_headless_multi_zone(zone_count=3)
    with (
        patch.object(zone_registry, "_raise_ambiguous_issue") as raise_issue,
        patch.object(zone_registry, "_warn_once") as warn,
    ):
        body = _get(ClimateAdvisorZonesView(), fake_hass)
    assert body["zone_count"] == 3
    assert [z["entry_id"] for z in body["zones"]] == [zones[f"zone_{i}"]["entry"].entry_id for i in range(3)]
    assert [z["title"] for z in body["zones"]] == [zones[f"zone_{i}"]["entry"].title for i in range(3)]
    assert set(body["zones"][0]) == {"entry_id", "title"}  # nothing of any zone's state
    assert body["version"] == VERSION
    raise_issue.assert_not_called()
    warn.assert_not_called()


def test_zones_with_no_zone_loaded_is_an_empty_list_not_an_error():
    """Climate Advisor still starting: the dashboards read an empty list as "wait and retry"."""
    _zones, fake_hass, _scheduler = build_headless_multi_zone(zone_count=2)
    fake_hass.data.pop("climate_advisor", None)
    body = _get(ClimateAdvisorZonesView(), fake_hass)
    assert body["zones"] == [] and body["zone_count"] == 0


def test_unscoped_status_still_raises_the_card_on_a_multi_zone_install():
    """Owner's rule (Issue #1418) unchanged: the zones view is the only exemption."""
    _zones, fake_hass, _scheduler = build_headless_multi_zone(zone_count=2)
    with patch.object(zone_registry, "_raise_ambiguous_issue") as raise_issue:
        body = _get(ClimateAdvisorStatusView(), fake_hass, path="/api/climate_advisor/status")
    assert body["zone_selection_required"] is True
    raise_issue.assert_called_once()
    assert "GET /api/climate_advisor/status" in raise_issue.call_args.kwargs["request"]


def test_zones_view_is_registered_but_outside_api_views():
    """``tests_ha/test_zone_param_1418.py`` asserts every API_VIEWS / DASHBOARD_VIEWS view flags an unscoped call;
    the zone-less view lives in its own list so that check keeps covering every zone-scoped view."""
    assert ClimateAdvisorZonesView in ZONELESS_VIEWS
    assert ClimateAdvisorZonesView not in API_VIEWS and ClimateAdvisorZonesView not in DASHBOARD_VIEWS
    assert ClimateAdvisorZonesView.requires_auth is True
    assert not hasattr(ClimateAdvisorZonesView, "post")
