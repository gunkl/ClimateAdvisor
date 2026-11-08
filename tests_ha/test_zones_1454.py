"""Issue #1454: ``GET /api/climate_advisor/zones`` raises no Repairs issue; an unscoped ``GET /status`` still does.

Occupant view: after a Home Assistant restart an "Ambiguous zone selection" Repairs card appeared although nothing was
misconfigured: the dashboards asked an unscoped ``status`` for the zone list. They now ask ``zones``, which names no
zone by design. Real Home Assistant, two loaded zones and real authenticated HTTP requests check that ``zones`` lists
both and leaves the card alone (repeatedly, and with a dismissed card left dismissed), and that the owner's rule still
holds: an unscoped ``status`` raises the card.
"""

from __future__ import annotations

from homeassistant.config_entries import ConfigEntryState
from homeassistant.helpers import issue_registry as ir

from tests_ha.house.builder import CA_DOMAIN
from tests_ha.test_zone_param_1418 import ISSUE_ID, _add_zone


async def test_zones_raises_no_card_and_unscoped_status_still_does(make_house, hass_client, hass_admin_user) -> None:
    house = await make_house(units="us", profile="scripted", initial_indoor_f=72.0)
    hass = house.hass
    added = []
    try:
        added.extend(await _add_zone(hass, house, "Upstairs", 66.0, 78.0))
        await house.advance(minutes=6)
        refresh = await hass.auth.async_create_refresh_token(hass_admin_user, "https://example.com/app")
        client = await hass_client(hass.auth.async_create_access_token(refresh))
        registry = ir.async_get(hass)
        assert registry.async_get_issue(CA_DOMAIN, ISSUE_ID) is None

        for _ in range(3):
            resp = await client.get("/api/climate_advisor/zones")
            assert resp.status == 200
            body = await resp.json()
            assert body["zone_count"] == 2
            assert [z["entry_id"] for z in body["zones"]] == [house.ca_entry.entry_id, added[1].entry_id]
            assert [z["title"] for z in body["zones"]] == [house.ca_entry.title, "Upstairs"]
        assert registry.async_get_issue(CA_DOMAIN, ISSUE_ID) is None, "GET /zones raised the ambiguous-zone card"

        # Scoped status (what the dashboards send next): no card either.
        resp = await client.get("/api/climate_advisor/status", params={"entry_id": added[1].entry_id})
        assert resp.status == 200
        assert "zone_selection_required" not in await resp.json()
        assert registry.async_get_issue(CA_DOMAIN, ISSUE_ID) is None

        # Owner's rule: an unscoped status is still flagged, naming the request.
        resp = await client.get("/api/climate_advisor/status")
        assert resp.status == 200
        assert (await resp.json())["zone_selection_required"] is True
        issue = registry.async_get_issue(CA_DOMAIN, ISSUE_ID)
        assert issue is not None
        assert "GET /api/climate_advisor/status" in (issue.translation_placeholders or {}).get("request", "")

        # A dismissed card is not brought back by /zones (it is not an unscoped request).
        registry.async_ignore(CA_DOMAIN, ISSUE_ID, True)
        await (await client.get("/api/climate_advisor/zones")).read()
        issue = registry.async_get_issue(CA_DOMAIN, ISSUE_ID)
        assert issue is not None and issue.dismissed_version is not None
        placeholders = issue.translation_placeholders or {}
        assert "/zones" not in placeholders.get("request", "")
    finally:
        for entry in reversed(added):
            if entry.state is ConfigEntryState.LOADED:
                await hass.config_entries.async_unload(entry.entry_id)
        await hass.async_block_till_done()
