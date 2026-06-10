"""
test_integration_boot.py — Tier B harness foundation milestone.

Proves that real Home Assistant boots in Docker with the climate_advisor
integration loaded and its entities visible via the REST API.

This test is NOT part of the normal ``pytest tests/`` run.  It lives under
``tools/integration_harness/`` and is gated by the ``integration`` mark +
Docker availability check in conftest.py.

RUN
---
    pytest tools/integration_harness/ -m integration -v

WHAT THIS TEST CHECKS
---------------------
1. HA is reachable (GET /api/ returns 200 with a valid token).
2. The climate_advisor config entry is loaded (config entry appears in
   GET /api/config/config_entries/entry and the entry state is "loaded").
3. At least one climate_advisor sensor entity is present in the state machine
   (GET /api/states returns an entity with entity_id starting with
   "sensor.climate_advisor_").

These three assertions together prove the foundation milestone: HA is up,
the pre-seeded config entry was accepted, and the integration's
async_setup_entry() ran to completion and created sensor entities.
"""

from __future__ import annotations

import pytest
import requests

pytestmark = pytest.mark.integration


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------


def _api(url: str, ha_url: str, ha_headers: dict, **kwargs) -> requests.Response:
    """GET a HA REST API path and return the response."""
    return requests.get(f"{ha_url}{url}", headers=ha_headers, timeout=15, **kwargs)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestHABoot:
    """Verify that Home Assistant boots and is reachable."""

    def test_api_endpoint_responds(self, ha_url, ha_headers):
        """GET /api/ returns 200 with a valid token — HA is alive."""
        resp = _api("/api/", ha_url, ha_headers)
        assert resp.status_code == 200, f"Expected 200 from /api/, got {resp.status_code}: {resp.text[:500]}"
        data = resp.json()
        # HA always returns {"message": "API running."} from /api/
        assert "message" in data, f"Unexpected /api/ response: {data}"

    def test_ha_version_present(self, ha_url, ha_headers):
        """GET /api/config returns HA version — confirms HA is fully initialised."""
        resp = _api("/api/config", ha_url, ha_headers)
        assert resp.status_code == 200, f"GET /api/config failed: {resp.text[:500]}"
        data = resp.json()
        version = data.get("version", "")
        assert version, f"No version in /api/config response: {data}"
        # We pinned 2025.5.x in the Dockerfile
        assert version.startswith("2025."), f"Unexpected HA version '{version}' — check the Dockerfile base image"


class TestClimateAdvisorLoaded:
    """Verify that the climate_advisor integration loaded successfully."""

    def test_config_entry_loaded(self, ha_url, ha_headers):
        """
        The pre-seeded config entry appears in /api/config/config_entries/entry
        and its state is 'loaded'.

        This confirms that async_setup_entry() completed without error.
        """
        resp = _api("/api/config/config_entries/entry", ha_url, ha_headers)
        assert resp.status_code == 200, f"GET /api/config/config_entries/entry failed: {resp.text[:500]}"
        entries = resp.json()
        ca_entries = [e for e in entries if e.get("domain") == "climate_advisor"]
        assert ca_entries, (
            "No climate_advisor config entry found in /api/config/config_entries/entry.\n"
            f"All domains: {sorted({e.get('domain') for e in entries})}"
        )
        entry = ca_entries[0]
        state = entry.get("state", "")
        assert state == "loaded", f"climate_advisor config entry state is '{state}', expected 'loaded'.\nEntry: {entry}"

    def test_climate_advisor_sensor_entities_present(self, ha_url, ha_headers):
        """
        At least one sensor.climate_advisor_* entity appears in the state machine.

        This confirms that the sensor platform was set up (PLATFORMS = ['sensor','switch']
        in __init__.py) and entities were registered.
        """
        resp = _api("/api/states", ha_url, ha_headers)
        assert resp.status_code == 200, f"GET /api/states failed: {resp.text[:200]}"
        states = resp.json()
        ca_sensors = [s["entity_id"] for s in states if s.get("entity_id", "").startswith("sensor.climate_advisor_")]
        assert ca_sensors, (
            "No sensor.climate_advisor_* entities found in /api/states.\n"
            "This suggests async_setup_entry() failed or the sensor platform "
            "did not register entities.\n"
            f"All entity_ids (first 30): {sorted(s['entity_id'] for s in states)[:30]}"
        )

    def test_demo_climate_entity_available(self, ha_url, ha_headers):
        """
        climate.ecobee is present — confirms the demo platform loaded and the
        climate entity configured in the pre-seeded config entry is available.

        HA's demo platform creates several climate entities: climate.ecobee,
        climate.heatpump, climate.hvac.  We use climate.ecobee in the
        pre-seeded config entry (see config-seed/.storage/core.config_entries).
        """
        resp = _api("/api/states/climate.ecobee", ha_url, ha_headers)
        assert resp.status_code == 200, (
            "climate.ecobee not found in HA state machine.\n"
            "The demo: platform may not have loaded correctly.\n"
            f"Response: {resp.text[:300]}"
        )
        state = resp.json()
        assert state.get("entity_id") == "climate.ecobee"

    def test_demo_weather_entity_available(self, ha_url, ha_headers):
        """
        weather.demo_weather_north is present — confirms the weather entity
        configured in the pre-seeded config entry is available.

        HA's demo platform creates weather.demo_weather_north and
        weather.demo_weather_south.  We use weather.demo_weather_north in the
        pre-seeded config entry.
        """
        resp = _api("/api/states/weather.demo_weather_north", ha_url, ha_headers)
        assert resp.status_code == 200, (
            "weather.demo_weather_north not found in HA state machine.\n"
            "The demo: platform may not have loaded correctly.\n"
            f"Response: {resp.text[:300]}"
        )
        state = resp.json()
        assert state.get("entity_id") == "weather.demo_weather_north"
