"""After a settings save, nothing from the old zone instance may keep running (Issue #1102).

Occupant view: saving Climate Advisor settings reloads the zone. Before #1102 the
old instance's 300 s startup timer survived the unload, revived the old coordinator,
and that hidden copy kept turning the whole-house fan back on after the occupant
switched it off.

The Tier A tests (tests/test_reload_lifecycle.py) check this against CA's own fakes.
This test asks real Home Assistant instead: HA's autouse ``verify_cleanup`` fixture
fails the test if any timer, task or thread is still alive at the end. It is
deliberately generic and names nothing, so a future leak anywhere in setup fails it.

The test ends without advancing time, so a leaked timer is still pending when
``verify_cleanup`` looks. Expected on 1df0246 (pre-fix): "Lingering timer after job"
for the coordinator's startup-coalesce timer.
"""

from __future__ import annotations

import json
from datetime import timedelta
from pathlib import Path

from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.climate_advisor.const import DOMAIN

# One source of truth for a known-good zone config: the Tier B harness seed
# (tools/integration_harness/), which real HA already boots CA from.
_SEED = (
    Path(__file__).resolve().parent.parent
    / "tools"
    / "integration_harness"
    / "config-seed"
    / ".storage"
    / "core.config_entries"
)


def _seed_entry() -> dict:
    entries = json.loads(_SEED.read_text(encoding="utf-8"))["data"]["entries"]
    return next(e for e in entries if e["domain"] == DOMAIN)


def _add_entities(hass: HomeAssistant, data: dict) -> None:
    """The two entities a zone needs, as plain states (no demo platform timers)."""
    today = dt_util.start_of_local_day()
    hass.states.async_set(
        data["climate_entity"],
        "heat",
        {
            "hvac_modes": ["off", "heat", "cool", "heat_cool"],
            "hvac_action": "idle",
            "current_temperature": 70,
            "temperature": 70,
            "fan_mode": "auto",
            "fan_modes": ["auto", "on"],
        },
    )
    hass.states.async_set(
        data["weather_entity"],
        "sunny",
        {
            "temperature": 60,
            # Dated from today so no "past days" filter can empty the forecast.
            "forecast": [
                {
                    "datetime": (today + timedelta(days=d)).isoformat(),
                    "temperature": 72 + d,
                    "templow": 55 + d,
                    "condition": "sunny",
                }
                for d in range(3)
            ],
        },
    )


async def test_unload_leaves_nothing_running(hass: HomeAssistant, tmp_path) -> None:
    """Set up one zone, unload it as a settings save does, and leave nothing behind."""
    hass.config.config_dir = str(tmp_path)  # CA writes state/diagnostics files here
    seed = _seed_entry()
    _add_entities(hass, seed["data"])

    entry = MockConfigEntry(
        domain=DOMAIN,
        version=seed["version"],
        minor_version=seed.get("minor_version", 1),
        data=seed["data"],
        options=seed.get("options", {}),
    )
    entry.add_to_hass(hass)

    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    # A setup retry would arm HA's own retry timer and make this test meaningless.
    assert entry.state is ConfigEntryState.LOADED

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.NOT_LOADED
    # verify_cleanup (autouse) now fails the test on any lingering timer, task or thread.
