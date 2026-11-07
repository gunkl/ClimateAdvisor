"""Issue #1418: every REST view honours ``?zone=``, and only genuinely unscoped requests raise the Repairs card.

Occupant view: the owner has several zones and opens a zone's chart, debug data or a diagnostic tool. Before this
fix, ``chart_data?zone=<zone 2>`` was answered with Zone 1's data (18 views read only ``entry_id``) and raised an
"Ambiguous zone selection" card that named no cause. Here real Home Assistant, three loaded zones and real HTTP
requests check that:

- ``zone=`` and ``entry_id=`` both reach the named zone, on the views behind ``_get_coordinator()`` and on the
  three newer views, and raise no card;
- a request with no zone still gets the first zone, and the card now names that request and that zone;
- a request naming two different zones is refused and changes nothing.
"""

from __future__ import annotations

from homeassistant.config_entries import ConfigEntryState
from homeassistant.helpers import issue_registry as ir
from pytest_homeassistant_custom_component.common import MockConfigEntry

from tests_ha.house.builder import CA_DOMAIN, SIM_DOMAIN

ISSUE_ID = "zone_resolution_ambiguous"


async def _add_zone(hass, house, name: str, heat: float, cool: float) -> tuple[MockConfigEntry, MockConfigEntry]:
    """A second simulated thermostat plus a Climate Advisor zone on it with its own comfort band."""
    before = {s.entity_id for s in hass.states.async_all("climate")}
    sim = MockConfigEntry(
        domain=SIM_DOMAIN, title=f"{name} Thermostat", data={**house.sim_entry.data, "name": f"{name} Thermostat"}
    )
    sim.add_to_hass(hass)
    assert await hass.config_entries.async_setup(sim.entry_id)
    await hass.async_block_till_done()
    climate = next(s.entity_id for s in hass.states.async_all("climate") if s.entity_id not in before)
    zone = MockConfigEntry(
        domain=CA_DOMAIN,
        title=name,
        version=house.ca_entry.version,
        minor_version=house.ca_entry.minor_version,
        data={**house.ca_entry.data, "climate_entity": climate, "comfort_heat": heat, "comfort_cool": cool},
        options=dict(house.ca_entry.options),
    )
    zone.add_to_hass(hass)
    assert await hass.config_entries.async_setup(zone.entry_id)
    await hass.async_block_till_done()
    return sim, zone


def _issue(hass):
    return ir.async_get(hass).async_get_issue(CA_DOMAIN, ISSUE_ID)


async def test_zone_param_reaches_the_named_zone_and_only_unscoped_requests_raise_the_card(
    make_house, hass_client, hass_admin_user
) -> None:
    house = await make_house(units="us", profile="scripted", initial_indoor_f=72.0)
    hass = house.hass
    added: list[MockConfigEntry] = []
    try:
        for name, heat, cool in (("Upstairs", 66.0, 78.0), ("Studio", 64.0, 80.0)):
            added.extend(await _add_zone(hass, house, name, heat, cool))
        await house.advance(minutes=6)
        zone1 = house.ca_entry
        upstairs, studio = added[1], added[3]
        # The house freezes the clock, so the fixture's token (issued at real time) is not yet valid: issue one now.
        refresh = await hass.auth.async_create_refresh_token(hass_admin_user, "https://example.com/app")
        client = await hass_client(hass.auth.async_create_access_token(refresh))
        assert _issue(hass) is None  # every zone setup clears it

        # status: ?zone= gets that zone's own band, not the bootstrap zone list (before: only entry_id did).
        for param in ("zone", "entry_id"):
            for entry, band in ((upstairs, (66.0, 78.0)), (studio, (64.0, 80.0))):
                resp = await client.get("/api/climate_advisor/status", params={param: entry.entry_id})
                assert resp.status == 200
                body = await resp.json()
                assert "zone_selection_required" not in body, f"{param}= ignored by status"
                assert (body["ca_target_heat"], body["ca_target_cool"]) == band

        # The views behind _get_coordinator() and the three newer ones: zone= is a zone, never a guess.
        for path in ("chart_data", "automation_state", "engines", "event_log", "learning", "briefing"):
            resp = await client.get(f"/api/climate_advisor/{path}", params={"zone": studio.entry_id})
            assert resp.status in (200, 503), (path, resp.status)
        for path in ("timeline", "night_strategy", "prediction_accuracy"):
            resp = await client.get(f"/api/climate_advisor/{path}", params={"entry_id": upstairs.entry_id})
            assert resp.status != 400, f"{path} refused an entry_id-scoped request"
        assert _issue(hass) is None, "a zone-scoped request raised the ambiguous-zone card"

        # Two different zones named: refused, nothing changed in either zone.
        before = {e.entry_id: hass.data[CA_DOMAIN][e.entry_id].automation_enabled for e in (zone1, upstairs, studio)}
        resp = await client.post(
            "/api/climate_advisor/toggle_automation",
            params={"zone": upstairs.entry_id, "entry_id": studio.entry_id},
        )
        assert resp.status == 400
        assert (await resp.json())["zone_selection_required"] is True
        after = {e.entry_id: hass.data[CA_DOMAIN][e.entry_id].automation_enabled for e in (zone1, upstairs, studio)}
        assert after == before

        # A genuinely unscoped GET: first zone's data, and the card says which request and which zone.
        resp = await client.get("/api/climate_advisor/automation_state")
        assert resp.status == 200
        issue = _issue(hass)
        assert issue is not None
        placeholders = issue.translation_placeholders or {}
        assert "GET /api/climate_advisor/automation_state" in placeholders.get("request", "")
        assert zone1.title in placeholders.get("outcome", "")

        # An unscoped POST is refused (no zone actuated) and the card says so.
        resp = await client.post("/api/climate_advisor/toggle_automation")
        assert resp.status == 400
        assert "?zone=" in (await resp.json())["message"]
        after = {e.entry_id: hass.data[CA_DOMAIN][e.entry_id].automation_enabled for e in (zone1, upstairs, studio)}
        assert after == before
        assert "refused" in (_issue(hass).translation_placeholders or {}).get("outcome", "")
    finally:
        for entry in reversed(added):
            if entry.state is ConfigEntryState.LOADED:
                await hass.config_entries.async_unload(entry.entry_id)
        await hass.async_block_till_done()


async def test_every_endpoint_flags_an_unscoped_request_every_time(make_house, hass_client, hass_admin_user) -> None:
    """Owner's rule (Issue #1418): "it still needs to be flagged the same way when it happens".

    Every registered Climate Advisor view and method, sent with no zone on a multi-zone install, raises the
    "Ambiguous zone selection" card, including a repeat of the same request after the card was cleared or dismissed.
    No caller is exempt: these requests come from the same client a tool or an agent would use. If a new view
    resolves its zone some other way, or a view returns before resolving one, this test names it.
    """
    from custom_components.climate_advisor.api import API_VIEWS
    from custom_components.climate_advisor.dashboard_api import DASHBOARD_VIEWS

    house = await make_house(units="us", profile="scripted", initial_indoor_f=72.0)
    hass = house.hass
    added: list[MockConfigEntry] = []
    try:
        added.extend(await _add_zone(hass, house, "Upstairs", 66.0, 78.0))
        await house.advance(minutes=6)
        refresh = await hass.auth.async_create_refresh_token(hass_admin_user, "https://example.com/app")
        client = await hass_client(hass.auth.async_create_access_token(refresh))
        registry = ir.async_get(hass)

        unflagged = []
        for view_cls in (*API_VIEWS, *DASHBOARD_VIEWS):
            for method in ("get", "post", "delete"):
                if not hasattr(view_cls, method):
                    continue
                for attempt in range(2):  # a repeat of the same request is flagged again
                    ir.async_delete_issue(hass, CA_DOMAIN, ISSUE_ID)
                    resp = await getattr(client, method)(view_cls.url, json={} if method != "get" else None)
                    await resp.read()
                    issue = registry.async_get_issue(CA_DOMAIN, ISSUE_ID)
                    request = (issue.translation_placeholders or {}).get("request", "") if issue else ""
                    if issue is None or f"{method.upper()} {view_cls.url}" not in request:
                        unflagged.append(f"{method.upper()} {view_cls.url} (attempt {attempt + 1}, HTTP {resp.status})")
        assert not unflagged, "unscoped requests not flagged: " + ", ".join(unflagged)

        # A card the owner dismissed comes back on the next unscoped request.
        registry.async_ignore(CA_DOMAIN, ISSUE_ID, True)
        assert registry.async_get_issue(CA_DOMAIN, ISSUE_ID).dismissed_version is not None
        await (await client.get("/api/climate_advisor/chart_data")).read()
        issue = registry.async_get_issue(CA_DOMAIN, ISSUE_ID)
        assert issue is not None and issue.dismissed_version is None, "a dismissed card stayed hidden"
    finally:
        for entry in reversed(added):
            if entry.state is ConfigEntryState.LOADED:
                await hass.config_entries.async_unload(entry.entry_id)
        await hass.async_block_till_done()
