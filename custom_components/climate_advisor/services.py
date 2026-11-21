"""Climate Advisor's five zone-scoped services.

Home Assistant quality scale (Issue #1637):

- ``action-setup``: the services are registered once, in ``async_setup`` (``__init__.py``), so they exist while
  Home Assistant runs, even with no zone loaded. A call names its zone (``entry_id``). If that zone isn't loaded,
  the call fails with a clear, translated error instead of the service being missing.
- ``action-exceptions``: bad input raises ``ServiceValidationError`` (an unknown zone, an unknown suggestion key),
  and a failure while doing the work raises ``HomeAssistantError``. Both are translated (``strings.json``
  ``exceptions``). A call never logs a warning and then reports success.

Issue #796 Gap 5: every handler resolves its zone at call time (``_resolve_zone_coordinator``), never a coordinator
captured when the service was registered.
"""

from __future__ import annotations

import json
import logging
from typing import Any

import homeassistant.helpers.config_validation as cv
import voluptuous as vol
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError

from . import log_capture, zone_registry
from .const import DOMAIN
from .coordinator import ClimateAdvisorCoordinator
from .learning import SUGGESTION_KEYS

_LOGGER = logging.getLogger(__name__)

# The single source of truth for the service-name set (also read by the sim harness and tests).
ZONE_SCOPED_SERVICES = (
    "respond_to_suggestion",
    "force_reclassify",
    "resend_briefing",
    "dump_diagnostics",
    "reset_learning_data",
)

RESPOND_SUGGESTION_SCHEMA = vol.Schema(
    {
        vol.Required("entry_id"): cv.string,
        vol.Required("action"): vol.In(["accept", "dismiss"]),
        vol.Required("suggestion_key"): cv.string,
    }
)
ENTRY_ID_ONLY_SCHEMA = vol.Schema({vol.Required("entry_id"): cv.string})
RESET_LEARNING_SCHEMA = vol.Schema(
    {
        vol.Required("entry_id"): cv.string,
        vol.Optional("scope", default="all"): vol.In(["thermal_model", "weather_bias", "suggestions", "all"]),
    }
)


def _resolve_zone_coordinator(hass: HomeAssistant, call: ServiceCall) -> ClimateAdvisorCoordinator:
    """Resolve the target zone's coordinator for a service call, at call time.

    Issue #796 Gap 5: previously each service was a closure over the ``coordinator`` bound inside one specific
    async_setup_entry() call. With two zones configured, the second zone's setup silently overwrote the first
    zone's handler in HA's global service registry, so every call (including the destructive
    reset_learning_data) acted on whichever zone set up last, with no error and no way to target the zone meant.

    Every service requires ``call.data["entry_id"]``, resolved fresh through ``zone_registry.get_coordinator()``
    (the same accessor api.py's REST views use). An unknown or unloaded ``entry_id`` raises a translated
    ``ServiceValidationError``, which the frontend and CLI render as an error, not an unhandled exception.
    """
    entry_id = call.data.get("entry_id")
    coordinator = zone_registry.get_coordinator(hass, entry_id) if entry_id else None
    if coordinator is None:
        # Issue #1341: the text is strings.json ``exceptions.unknown_zone`` (HA's own method, translated per language).
        raise ServiceValidationError(
            translation_domain=DOMAIN,
            translation_key="unknown_zone",
            translation_placeholders={"entry_id": str(entry_id)},
        )
    return coordinator


async def async_accept_suggestion(
    hass: HomeAssistant, coordinator: ClimateAdvisorCoordinator, suggestion_key: str
) -> None:
    """Acknowledge a suggestion for one zone (the one accept path of the service and the dashboard's REST view).

    Issue #1652 (owner decision): every suggestion is advice, so accepting it changes no setting. It records the
    acknowledgement (the learner holds the suggestion back for 30 days) and saves the learning state. ``accept``
    stays a valid action so existing automations keep working.
    """
    coordinator.learning.accept_suggestion(suggestion_key)
    await hass.async_add_executor_job(log_capture.bind_zone_for_executor(coordinator.learning.save_state))


def _failed(service: str, err: Exception) -> HomeAssistantError:
    """The translated error for a service whose work failed (the cause is chained and logged by HA)."""
    return HomeAssistantError(
        translation_domain=DOMAIN,
        translation_key="service_failed",
        translation_placeholders={"service": service, "error": f"{type(err).__name__}: {err}"},
    )


def async_setup_services(hass: HomeAssistant) -> None:
    """Register the five zone-scoped services (called once, from ``async_setup``)."""

    async def handle_suggestion_response(call: ServiceCall) -> None:
        """Accept or dismiss a learning suggestion."""
        zone_coordinator = _resolve_zone_coordinator(hass, call)
        action = call.data.get("action")  # "accept" or "dismiss"
        suggestion_key = call.data.get("suggestion_key")
        if suggestion_key not in SUGGESTION_KEYS:
            # Before #1637 an unknown key logged a WARNING and the call reported success.
            raise ServiceValidationError(
                translation_domain=DOMAIN,
                translation_key="unknown_suggestion",
                translation_placeholders={
                    "suggestion_key": str(suggestion_key),
                    "valid": ", ".join(sorted(SUGGESTION_KEYS)),
                },
            )
        # Issue #812: tag every _LOGGER call reached from this handler with the resolved zone in the log_capture
        # ring buffer. The executor job needs the explicit bind_zone_for_executor() wrapper too: a plain
        # zone_scope() does not propagate across the hass.async_add_executor_job() thread boundary.
        with log_capture.zone_scope(zone_coordinator.zone_label):
            try:
                if action == "accept":
                    # An acknowledgement: no suggestion changes a setting (#1652, shared with the REST view)
                    await async_accept_suggestion(hass, zone_coordinator, suggestion_key)
                    _LOGGER.info("Suggestion acknowledged: %s", suggestion_key)
                elif action == "dismiss":
                    zone_coordinator.learning.dismiss_suggestion(suggestion_key)
                    await hass.async_add_executor_job(
                        log_capture.bind_zone_for_executor(zone_coordinator.learning.save_state)
                    )
                    _LOGGER.info("Suggestion dismissed: %s", suggestion_key)
            except Exception as err:
                raise _failed("respond_to_suggestion", err) from err

    async def handle_force_reclassify(call: ServiceCall) -> None:
        """Force a coordinator refresh / reclassification."""
        zone_coordinator = _resolve_zone_coordinator(hass, call)
        with log_capture.zone_scope(zone_coordinator.zone_label):
            try:
                await zone_coordinator.async_request_refresh()
            except Exception as err:
                raise _failed("force_reclassify", err) from err

    async def handle_resend_briefing(call: ServiceCall) -> None:
        """Re-send the daily briefing."""
        from homeassistant.util import dt as dt_util

        zone_coordinator = _resolve_zone_coordinator(hass, call)
        with log_capture.zone_scope(zone_coordinator.zone_label):
            try:
                zone_coordinator._briefing_sent_today = False
                await zone_coordinator._async_send_briefing(dt_util.now())
            except Exception as err:
                raise _failed("resend_briefing", err) from err

    async def handle_dump_diagnostics(call: ServiceCall) -> None:
        """Log a comprehensive diagnostic snapshot for troubleshooting.

        Kept (not deprecated) alongside the native ``async_get_config_entry_diagnostics`` hook in diagnostics.py for
        continuity: some users may have automations that already call this service. Builds its payload through
        the same shared helper so the two surfaces stay a single source of truth for the payload shape.
        """
        from .diagnostics import async_get_diagnostics_payload

        zone_coordinator = _resolve_zone_coordinator(hass, call)
        with log_capture.zone_scope(zone_coordinator.zone_label):
            try:
                zone_entry = hass.config_entries.async_get_entry(zone_coordinator._entry_id)
                diag: dict[str, Any] = await async_get_diagnostics_payload(hass, zone_entry)
            except Exception as err:
                raise _failed("dump_diagnostics", err) from err
            _LOGGER.info("Diagnostic dump requested:\n%s", json.dumps(diag, indent=2, default=str))

    async def handle_reset_learning_data(call: ServiceCall) -> None:
        """Reset learned data for one zone."""
        zone_coordinator = _resolve_zone_coordinator(hass, call)
        with log_capture.zone_scope(zone_coordinator.zone_label):
            scope = call.data.get("scope", "all")
            try:
                await hass.async_add_executor_job(
                    log_capture.bind_zone_for_executor(zone_coordinator.learning.reset), scope
                )
            except Exception as err:
                raise _failed("reset_learning_data", err) from err
            _LOGGER.info("Learning data reset via service: entry_id=%s scope=%s", call.data.get("entry_id"), scope)

    hass.services.async_register(
        DOMAIN, "respond_to_suggestion", handle_suggestion_response, schema=RESPOND_SUGGESTION_SCHEMA
    )
    hass.services.async_register(DOMAIN, "force_reclassify", handle_force_reclassify, schema=ENTRY_ID_ONLY_SCHEMA)
    hass.services.async_register(DOMAIN, "resend_briefing", handle_resend_briefing, schema=ENTRY_ID_ONLY_SCHEMA)
    hass.services.async_register(DOMAIN, "dump_diagnostics", handle_dump_diagnostics, schema=ENTRY_ID_ONLY_SCHEMA)
    hass.services.async_register(
        DOMAIN, "reset_learning_data", handle_reset_learning_data, schema=RESET_LEARNING_SCHEMA
    )
