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

import logging
from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util

from .ai_backend import PROVIDERS
from .const import CONFIG_METADATA, DOMAIN, REDACTED_CONFIG_KEYS, VERSION
from .diagnostic_snapshot import build_diagnostic_snapshot, recent_hvac_events

_LOGGER = logging.getLogger(__name__)

# Redact REDACTED_CONFIG_KEYS ("notify_service" may reveal personal info; "github_token" is a
# credential, Issue #1062) plus every CONFIG_METADATA key flagged ``"sensitive": True``
# (currently just "ai_api_key") — reusing the same flag api.py already uses for the config
# API's sanitization.
#
# Thermostat attributes (serial/MAC/IP/location/owner name) are NOT handled by this
# exact-key set: their key names are third-party-defined, so `diagnostic_snapshot.py`
# redacts them inside `build_diagnostic_snapshot()` (Issue #1049) for every consumer at
# once. `async_redact_data(payload, TO_REDACT)` below remains the second layer for
# config secrets.
TO_REDACT = set(REDACTED_CONFIG_KEYS) | {key for key, meta in CONFIG_METADATA.items() if meta.get("sensitive")}


def _redaction_keys() -> set[str]:
    """``TO_REDACT`` plus each registered provider's own API-key setting (not in CONFIG_METADATA)."""
    return TO_REDACT | {spec.credential_setting for spec in PROVIDERS.values() if spec.credential_setting}


def _chart_data_summary(coordinator: Any) -> dict[str, Any]:
    """Counts plus the outdoor buffer's range/age (Issue #1015) — values only, None-safe when empty."""
    outdoor = list(coordinator._outdoor_temp_history)
    values = [t for _, t in outdoor]
    return {
        "outdoor_points": len(outdoor),
        "indoor_points": len(coordinator._indoor_temp_history),
        "outdoor_min": min(values) if values else None,
        "outdoor_max": max(values) if values else None,
        "outdoor_oldest_ts": outdoor[0][0] if outdoor else None,
        "outdoor_newest_ts": outdoor[-1][0] if outdoor else None,
    }


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
        # Issue #1049: what the thermostat supports (hvac_modes, features, min/max/step,
        # current attributes), what Climate Advisor last asked it to do, and what happened,
        # so a reporter does not have to be asked to paste thermostat details and logs.
        # Attribute redaction happens inside the builder; a failure here must not lose the
        # rest of the diagnostics file.
        try:
            thermostat_snapshot: Any = build_diagnostic_snapshot(hass, coordinator)
            hvac_events: Any = recent_hvac_events(coordinator)
        except Exception:
            _LOGGER.warning("diagnostics: thermostat snapshot failed -- omitting from payload", exc_info=True)
            thermostat_snapshot = None
            hvac_events = None
        payload.update(
            {
                "debug_state": coordinator.get_debug_state(),
                "thermostat_snapshot": thermostat_snapshot,
                "recent_hvac_events": hvac_events,
                "chart_data_summary": _chart_data_summary(coordinator),
                "learning_summary": coordinator.learning.get_compliance_summary(),
                "config": dict(coordinator.config),
                "briefing_state": {
                    "sent_today": coordinator._briefing_sent_today,
                    "briefing_length": len(coordinator._last_briefing),
                },
            }
        )

    return async_redact_data(payload, _redaction_keys())


async def async_get_config_entry_diagnostics(hass: HomeAssistant, entry: ConfigEntry) -> dict[str, Any]:
    """HA's native config-entry diagnostics hook.

    Implementing this function is the entire deliverable for this feature — HA
    shows its own standard "Download Diagnostics" menu item automatically once
    this hook exists; no frontend code is added or maintained by this
    integration for it (confirmed via UI mocking, see docs/multi-zone-spec.md).
    """
    return await async_get_diagnostics_payload(hass, entry)
