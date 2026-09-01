"""Home Assistant native diagnostics hook for Climate Advisor.

Implements HA's diagnostics integration point
(https://developers.home-assistant.io/docs/core/integration-quality-scale/rules/diagnostics/)
so a per-zone bug report can be produced with HA's native "Download Diagnostics"
button (Settings -> Devices & Services -> entry -> kebab menu) instead of manually
calling the `dump_diagnostics` service and digging the payload out of the log
viewer. See docs/multi-zone-spec.md "Diagnostics and Field Feedback" (PR1).

`async_get_diagnostics_payload()` is the single source of truth for the payload
shape — both this module's native hook and `__init__.py`'s `dump_diagnostics`
service build their output through it, per the spec's "keep it, redirect it,
don't deprecate it" decision for the legacy service.
"""

from __future__ import annotations

from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util

from .const import CONFIG_METADATA, DOMAIN, VERSION

# Redact "notify_service" (may reveal personal info, per the existing
# api.py:542 convention) plus every CONFIG_METADATA key flagged
# ``"sensitive": True`` (currently just "ai_api_key") — reusing the same flag
# api.py already uses for the config API's sanitization, not a second
# redaction list.
TO_REDACT = {"notify_service"} | {key for key, meta in CONFIG_METADATA.items() if meta.get("sensitive")}


async def async_get_diagnostics_payload(hass: HomeAssistant, entry: ConfigEntry) -> dict[str, Any]:
    """Build the shared, redacted diagnostics payload for one config entry."""
    zones = hass.data.get(DOMAIN, {})
    coordinator = zones.get(entry.entry_id)

    all_entries = hass.config_entries.async_entries(DOMAIN)
    entry_setup_order = next(
        (index for index, candidate in enumerate(all_entries) if candidate.entry_id == entry.entry_id),
        None,
    )

    payload: dict[str, Any] = {
        "version": VERSION,
        "timestamp": dt_util.now().isoformat(),
        "zone_count": len(zones),
        "this_entry_id": entry.entry_id,
        "entry_title": entry.title,
        "entry_setup_order": entry_setup_order,
        # Issue #796 PR4 (Gap 5) made this question moot rather than
        # answering it: the five domain-scoped services (respond_to_suggestion,
        # force_reclassify, resend_briefing, dump_diagnostics,
        # reset_learning_data) no longer close over any one zone's
        # `coordinator` at registration time, so there is no static
        # per-entry "binding" left to introspect or report. Each call now
        # resolves its target zone dynamically, at call time, from the
        # call's own required `entry_id` field via `_resolve_zone_coordinator()`
        # in `__init__.py` — which raises `ServiceValidationError` for an
        # unknown/unloaded entry_id rather than silently acting on the wrong
        # zone. Correctness is enforced per-call, not reportable as a
        # point-in-time snapshot. See docs/multi-zone-spec.md Gap 5's
        # "(as built, PR4)" note.
        "active_service_bindings": (
            "not applicable — since PR4, zone-scoped services are registered "
            "once, domain-wide, and resolve their target zone per-call from "
            "the required 'entry_id' field rather than being bound to a "
            "single zone at registration time (see docs/multi-zone-spec.md Gap 5)"
        ),
    }

    if coordinator is not None:
        payload.update(
            {
                "debug_state": coordinator.get_debug_state(),
                "chart_data_summary": {
                    "outdoor_points": len(coordinator._outdoor_temp_history),
                    "indoor_points": len(coordinator._indoor_temp_history),
                },
                "learning_summary": coordinator.learning.get_compliance_summary(),
                "config": dict(coordinator.config),
                "briefing_state": {
                    "sent_today": coordinator._briefing_sent_today,
                    "briefing_length": len(coordinator._last_briefing),
                },
            }
        )

    return async_redact_data(payload, TO_REDACT)


async def async_get_config_entry_diagnostics(hass: HomeAssistant, entry: ConfigEntry) -> dict[str, Any]:
    """HA's native config-entry diagnostics hook.

    Implementing this function is the entire deliverable for this feature — HA
    shows its own standard "Download Diagnostics" menu item automatically once
    this hook exists; no frontend code is added or maintained by this
    integration for it (confirmed via UI mocking, see docs/multi-zone-spec.md).
    """
    return await async_get_diagnostics_payload(hass, entry)
