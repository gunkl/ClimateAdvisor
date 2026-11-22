"""REST API views for the Climate Advisor dashboard panel."""

from __future__ import annotations

import functools
import json
import logging
from dataclasses import asdict
from datetime import datetime, timedelta
from typing import Any

from aiohttp import web
from homeassistant.components.http import HomeAssistantView
from homeassistant.core import HomeAssistant
from homeassistant.helpers.event import async_call_later
from homeassistant.util import dt as dt_util

from . import ai_limits, i18n, log_capture, prediction_accuracy, status_text, zone_registry
from .ai_settings_sync import provider_of
from .ai_skills_context import OUTDOOR_SENSOR_REASON_LABELS, build_event_timeline_table, build_event_timeline_wire
from .ai_skills_investigator import report_cut_short
from .const import (
    AI_CUT_SHORT,
    API_AI_INVESTIGATE,
    API_AI_LIMITS,
    API_AI_STATUS,
    API_AUTOMATION_STATE,
    API_BRIEFING,
    API_CANCEL_FAN_OVERRIDE,
    API_CANCEL_OVERRIDE,
    API_CHART_DATA,
    API_CONFIG,
    API_DELETE_REPORT,
    API_ENGINES,
    API_EVENT_LOG,
    API_FORCE_RECLASSIFY,
    API_INVESTIGATION_REPORTS,
    API_LEARNING,
    API_NIGHT_STRATEGY,
    API_PREDICTION_ACCURACY,
    API_RESPOND_SUGGESTION,
    API_RESUME_FROM_PAUSE,
    API_SEND_BRIEFING,
    API_STATUS,
    API_SUBMIT_GITHUB_ISSUE,
    API_TIMELINE,
    API_TOGGLE_AUTOMATION,
    API_ZONES,
    ATTR_AUTOMATION_STATUS,
    ATTR_COMPLIANCE_SCORE,
    ATTR_CONTACT_STATUS,
    ATTR_CURRENT_SETPOINT,
    ATTR_DAY_TYPE,
    ATTR_FAN_STATUS,
    ATTR_HVAC_ACTION,
    ATTR_HVAC_FAN_STATUS,
    ATTR_HVAC_RUNTIME_TODAY,
    ATTR_INDOOR_TEMP,
    ATTR_NEXT_ACTION,
    ATTR_NEXT_AUTOMATION_ACTION,
    ATTR_NEXT_AUTOMATION_TIME,
    ATTR_TREND,
    ATTR_TREND_MAGNITUDE,
    ATTR_WHF_STATUS,
    CONF_AI_ENABLED,
    CONF_AI_INVESTIGATOR_ENABLED,
    CONF_AI_TASK_ENTITY,
    CONF_ENERGY_RATE,
    CONF_GITHUB_REPO,
    CONF_GITHUB_TOKEN,
    CONFIG_METADATA,
    DEFAULT_AI_ENABLED,
    DEFAULT_AI_INVESTIGATOR_ENABLED,
    EVENT_LOG_CAP,
    TEMP_SOURCE_CLIMATE_FALLBACK,
    TEMP_SOURCE_INPUT_NUMBER,
    TEMP_SOURCE_SENSOR,
    TEMP_SOURCE_WEATHER_SERVICE,
    V4_ARCHIVE_LEADS,
    VERSION,
)
from .engine_inputs import decide, decide_sync
from .learning import SUGGESTION_KEYS
from .remote_offset import remote_offset_view_for
from .status_text import code_of
from .temperature import FAHRENHEIT, UNIT_SYMBOL, display_delta, display_temp

_LOGGER = logging.getLogger(__name__)

# Friendly labels for the "temp_source_label" display_transform (CONFIG_METADATA
# entries for indoor_temp_source/outdoor_temp_source) — without this, the Debug/
# Settings tab showed the raw stored enum string (e.g. "climate_fallback")
# verbatim instead of an explanation of what it means. Issue #1341: the stored value is a code; the label is the
# catalog message ``settings.temp_source.<code>`` (English: the words below, pinned by a test).
_TEMP_SOURCE_LABELS = {
    TEMP_SOURCE_SENSOR: "Dedicated sensor",
    TEMP_SOURCE_INPUT_NUMBER: "input_number helper",
    TEMP_SOURCE_CLIMATE_FALLBACK: "Thermostat's built-in sensor",
    TEMP_SOURCE_WEATHER_SERVICE: "Weather service",
}


_TEMP_SOURCE_ENTITY_KEYS = {
    "outdoor_temp_source": "outdoor_temp_entity",
    "indoor_temp_source": "indoor_temp_entity",
}


def _temp_source_display(coordinator: Any, key: str, raw_source: str, label: str, config: dict) -> str:
    """Settings-tab text (English) for a temperature source; ``_temp_source_msg`` is the same text as a message."""
    msg = _temp_source_msg(coordinator, key, raw_source, config)
    return i18n.render_en(msg) if msg is not None else label


def _temp_source_msg(coordinator: Any, key: str, raw_source: str, config: dict) -> i18n.Msg | None:
    """Settings-tab message for a temperature source: the label plus WHICH sensor, and its health (Issue #1032).

    Entity id only (never a friendly name, which may contain a personal name); the frontend escapes
    the value. A sensor source that is currently falling back says so, so the Settings tab cannot
    claim a dedicated sensor is in use while the weather service actually is. A saved entity next to
    a default source (a pre-#1032 mismatch) is called out as not in use. ``None`` for a source this
    table does not know (the raw value is then shown as it is).
    """
    if raw_source not in _TEMP_SOURCE_LABELS:
        return None
    label = i18n.Code(f"settings.temp_source.{raw_source}")
    entity_key = _TEMP_SOURCE_ENTITY_KEYS.get(key)
    if entity_key is None:
        return i18n.Msg(f"settings.temp_source.{raw_source}")
    entity = config.get(entity_key)
    entity = entity if isinstance(entity, str) and entity else None
    needs_entity = raw_source in (TEMP_SOURCE_SENSOR, TEMP_SOURCE_INPUT_NUMBER)
    is_outdoor = key == "outdoor_temp_source"
    # Issue #1033: indoor mirrors outdoor; the consequence differs (indoor decisions pause, not fall back).
    consequence = i18n.Code(
        "settings.temp_source.consequence_weather" if is_outdoor else "settings.temp_source.consequence_paused"
    )
    reason = getattr(
        coordinator, "_outdoor_sensor_fallback_reason" if is_outdoor else "_indoor_primary_fallback_reason", None
    )
    reason = reason if isinstance(reason, str) else None

    def _reason(text: str) -> i18n.Code | str:
        # a known failure reason is a catalog word (the same ``activity.sensor_reason_*`` the timeline uses)
        return (
            i18n.Code(f"activity.sensor_reason_{text}")
            if text in OUTDOOR_SENSOR_REASON_LABELS
            else text.replace("_", " ")
        )

    if needs_entity and entity is None:
        return i18n.Msg("settings.temp_source.none_selected", label=label, consequence=consequence)
    if raw_source == TEMP_SOURCE_CLIMATE_FALLBACK and not is_outdoor and reason is not None:
        return i18n.Msg("settings.temp_source.reason", label=label, reason=_reason(reason), consequence=consequence)
    # Issue #1230 R1b: the thermostat's finer companion sensor is the indoor reading right now.
    companion_in_use = getattr(coordinator, "_indoor_companion_in_use", None)
    if raw_source == TEMP_SOURCE_CLIMATE_FALLBACK and not is_outdoor and isinstance(companion_in_use, str):
        return i18n.Msg("settings.temp_source.finer", entity=companion_in_use)
    if entity is None:
        return i18n.Msg(f"settings.temp_source.{raw_source}")
    if not needs_entity:
        return i18n.Msg("settings.temp_source.unused", label=label, entity=entity)
    if reason is not None:
        return i18n.Msg(
            "settings.temp_source.entity_reason",
            label=label,
            entity=entity,
            reason=_reason(reason),
            consequence=consequence,
        )
    return i18n.Msg("settings.temp_source.entity", label=label, entity=entity)


def _zone_param(request: web.Request) -> tuple[str | None, bool]:
    """The zone a request names, from ``?zone=`` or ``?entry_id=`` (both mean the config entry id).

    Issue #1418: the views added in 0.9.4 (prediction_accuracy, timeline, night_strategy) documented ``zone`` while
    the other 18 read only ``entry_id``, so ``chart_data?zone=<id>`` silently answered for the first zone. Every view
    now reads both through here. Returns ``(zone_id, conflict)``; ``conflict`` is True when both are given and name
    different zones — the caller must refuse rather than pick one.
    """
    query = request.query
    zone = str(query.get("zone") or "").strip() or None
    entry_id = str(query.get("entry_id") or "").strip() or None
    if zone and entry_id and zone != entry_id:
        return None, True
    return zone or entry_id, False


def _get_coordinator(hass: HomeAssistant, request: web.Request):
    """Resolve the zone coordinator this request targets.

    Issue #796 Gap 4: previously always returned ``next(iter(entries.values()))``
    — an arbitrary "first" entry — regardless of how many zones were
    configured, meaning the entire REST/dashboard surface was blind to any
    zone beyond the first. Now reads an optional ``zone`` (or ``entry_id``) query parameter
    and resolves it via ``zone_registry.resolve_zone()``, which centralizes
    ambiguous-zone handling (Issue #885) for every one of this module's 21
    call sites: an explicit ``entry_id`` always resolves that zone; when
    absent and only one zone is loaded, single-zone behavior is unchanged;
    when absent and 2+ zones are loaded, a GET degrades to a deterministic,
    logged, Repairs-flagged guess (see "Transitional Safety Window" in
    docs/multi-zone-spec.md) but a POST refuses to guess entirely — silently
    actuating the wrong zone's HVAC control is a safety issue a display
    guess is not.

    Returns ``(coordinator, ambiguous_refused)``. Every call site must check
    ``ambiguous_refused`` and return ``_ambiguous_zone_payload(hass)`` before
    treating a ``None`` coordinator as "not loaded" — the two ``None`` causes
    require different responses.
    """
    return _resolve_request_zone(hass, request, allow_guess=(request.method != "POST"))


def _resolve_request_zone(hass: HomeAssistant, request: web.Request, *, allow_guess: bool):
    """``zone_registry.resolve_zone()`` for this request's ``zone``/``entry_id`` (see ``_zone_param()``).

    Conflicting ``zone`` and ``entry_id`` values are refused (``(None, True)``) like an ambiguous request: answering
    for either one could be the wrong zone.
    """
    zone_id, conflict = _zone_param(request)
    context = f"{request.method} {request.path} from {request.remote}"
    if conflict:
        _LOGGER.warning(
            "Climate Advisor: %s named two different zones (zone=%s, entry_id=%s) — refused. Send only one.",
            context,
            request.query.get("zone"),
            request.query.get("entry_id"),
        )
        return None, True
    return zone_registry.resolve_zone(hass, zone_id, context=context, allow_guess=allow_guess)


# Issue #1341: error code -> catalog key (one literal per key; the English lives in frontend/i18n/errors/en.json).
_ERROR_KEYS: dict[str, str] = {
    "not_loaded": "errors.not_loaded",
    "zone_selection_required": "errors.zone_selection_required",
    "invalid_verbosity": "errors.invalid_verbosity",
    "invalid_json_body": "errors.invalid_json_body",
    "feedback_value": "errors.feedback_value",
    "suggestion_key_required": "errors.suggestion_key_required",
    "suggestion_key_unknown": "errors.suggestion_key_unknown",
    "action_or_feedback_required": "errors.action_or_feedback_required",
    "action_value": "errors.action_value",
    "engine_status_unavailable": "errors.engine_status_unavailable",
    "ai_not_enabled": "errors.ai_not_enabled",
    "investigator_not_enabled": "errors.investigator_not_enabled",
    "ai_client_unavailable": "errors.ai_client_unavailable",
    "timeline_not_ready": "errors.timeline_not_ready",
    "night_strategy_not_ready": "errors.night_strategy_not_ready",
    "invalid_json": "errors.invalid_json",
    "missing_timestamp": "errors.missing_timestamp",
    "invalid_type": "errors.invalid_type",
    "not_configured": "errors.not_configured",
    "missing_title": "errors.missing_title",
    "network_error": "errors.network_error",
    "github_api": "errors.github_api",
    "investigation_failed": "errors.investigation_failed",
}


def _msg_fields(hass: HomeAssistant, msg: i18n.Msg, field: str = "message") -> dict[str, Any]:
    """``{field: <text in the backend language>, "<field>_msg": <wire Msg>}`` (Issue #1341, D3 "API shape").

    The prose stays in every body as before (rendered in ``hass.config.language``); the sibling is the same message as
    ``{key, params}`` so the dashboard can render it in the viewer's own language.
    """
    return {field: i18n.render(msg, i18n.hass_language(hass)), f"{field}_msg": msg.to_wire()}


def _error_body(hass: HomeAssistant, code: str, **params: Any) -> dict[str, Any]:
    """An ``{"error", "error_code", "error_msg"}`` body for a prose error (catalog ``errors``)."""
    body = _msg_fields(hass, i18n.Msg(_ERROR_KEYS[code], **params), "error")
    body["error_code"] = code
    return body


def _coded_extra(hass: HomeAssistant, code: str, **params: Any) -> dict[str, Any]:
    """The siblings to add beside an ``error`` that already IS a stable code (an existing contract, kept as is).

    ``error_code`` repeats the code, ``error_msg`` is the wire message and ``error_text`` the same sentence in the
    backend language (the code itself is not a sentence).
    """
    msg = i18n.Msg(_ERROR_KEYS[code], **params)
    return {
        "error_code": code,
        "error_msg": msg.to_wire(),
        "error_text": i18n.render(msg, i18n.hass_language(hass)),
    }


def _text_fields(field: str, value: Any, lang: str | None, *, code: bool = False) -> dict[str, Any]:
    """``{field, field_msg[, field_code]}`` for a status value (Issue #1341).

    ``value`` is a ``CodedText`` (its English text, a stable code and a message) or plain text. ``lang`` is the
    language the prose is rendered in; None keeps the English text (the documented enum-like fan values).
    ``<field>_msg`` is None for plain text no catalog message describes yet.
    """
    out: dict[str, Any] = {
        field: status_text.render_text(value, lang) if (lang and value is not None) else value,
        f"{field}_msg": status_text.wire_of(value),
    }
    if code:
        out[f"{field}_code"] = code_of(value)
    return out


def humid_air_view(engine: Any, unit: str, lang: str | None = None) -> dict[str, Any] | None:
    """The Status page line while the humid-air guard blocks fan starts (Issue #1691, #1507 H1), else None.

    Read live from the engine's ``humid_air_verdict()`` (the same predicate every fan start asks), never re-decided
    here. ``{code, outdoor_dew_point, indoor_dew_point, ceiling, text, text_msg}``; dew points in the display unit.
    """
    getter = getattr(engine, "humid_air_verdict", None)
    if not callable(getter):
        return None
    try:
        verdict = getter()
    except Exception:  # noqa: BLE001 - a status read must never break the endpoint
        return None
    if not getattr(verdict, "blocks", False) or verdict.outdoor_dp_f is None:
        return None
    outdoor = i18n.Temp(float(verdict.outdoor_dp_f), unit, 1)
    if verdict.reason == "ceiling" and verdict.ceiling_f is not None:
        msg = i18n.Msg(
            "status.humid_air.blocking_ceiling", outdoor=outdoor, ceiling=i18n.Temp(float(verdict.ceiling_f), unit, 1)
        )
    elif verdict.indoor_dp_f is not None:
        msg = i18n.Msg(
            "status.humid_air.blocking_wetter", outdoor=outdoor, indoor=i18n.Temp(float(verdict.indoor_dp_f), unit, 1)
        )
    else:
        return None
    return {
        "code": verdict.reason,
        "outdoor_dew_point": display_temp(verdict.outdoor_dp_f, unit),
        "indoor_dew_point": display_temp(verdict.indoor_dp_f, unit) if verdict.indoor_dp_f is not None else None,
        "ceiling": display_temp(verdict.ceiling_f, unit) if verdict.ceiling_f is not None else None,
        "text": i18n.render(msg, lang) if lang else i18n.render_en(msg),
        "text_msg": msg.to_wire(),
    }


def _ambiguous_zone_payload(hass: HomeAssistant) -> dict[str, Any]:
    """Response body for a POST request refused for ambiguous zone selection.

    Issue #885. Reuses the ``zone_selection_required``/``zones``/``zone_count``
    shape the frontend's ``loadStatus()`` already parses for its own bootstrap
    zone-discovery flow (see ``ClimateAdvisorStatusView.get()`` below), plus a
    ``message`` key — ``apiFetch()``/``apiPost()`` in ``frontend/index.html``
    already surface ``body.message`` as the thrown error's text for any
    non-2xx response, so this renders correctly in the existing UI error paths
    (e.g. ``doAction()``) with no frontend changes required.
    """
    zones = zone_registry.list_zones(hass)
    return {
        **_msg_fields(hass, i18n.Msg(_ERROR_KEYS["zone_selection_required"], hint="?zone=<entry_id>")),
        "zone_selection_required": True,
        "zones": zones,
        "zone_count": len(zones),
    }


def _current_hold_payload(coordinator: Any) -> dict | None:
    """``coordinator.current_hold_payload()`` (Issue #1404), or None when the coordinator has none (a dict only)."""
    fn = getattr(coordinator, "current_hold_payload", None)
    try:
        out = fn() if callable(fn) else None
    except Exception:  # noqa: BLE001 - a display field never breaks the status body
        _LOGGER.debug("current_hold payload unavailable", exc_info=True)
        return None
    return out if isinstance(out, dict) else None


class ClimateAdvisorStatusView(HomeAssistantView):
    """Return current system status."""

    url = API_STATUS
    name = "api:climate_advisor:status"
    requires_auth = True

    async def get(self, request: web.Request) -> web.Response:
        hass = request.app["hass"]

        # Issue #813: the dashboard's very first bootstrap call on a fresh
        # browser/device (no stored zone yet — see index.html's
        # _hadStoredZoneAtInit) has no entry_id to send, because it's the one
        # call that exists to DISCOVER what zones there are. Before this,
        # that call went straight into _get_coordinator() -> (no entry_id) ->
        # zone_registry.get_default_coordinator(), which — on a 2+-zone
        # install — silently picked an arbitrary "first" zone and logged the
        # "ambiguous zone selection" WARNING/Repairs signal. That is a real
        # guess, not just a loudly-logged one, and it happened on every
        # single genuine first visit (new browser, new device, cleared site
        # data) regardless of Issue #812's persistence fix, since persistence
        # can only help *after* a selection has been made once.
        #
        # Fix: resolve "how many zones exist" via zone_registry.list_zones()
        # — a pure, coordinator-free lookup — BEFORE ever calling
        # _get_coordinator(). When there's no entry_id AND more than one
        # zone, return a minimal zone-list-only payload instead of guessing;
        # the frontend picks a zone from it, persists the choice, and
        # immediately re-requests this same endpoint with an explicit
        # entry_id (see index.html's loadStatus()). zone_registry.
        # get_default_coordinator()'s own arbitrary-pick fallback is
        # untouched and still exists for defensive/edge-case callers — this
        # just ensures the dashboard's own bootstrap path never reaches it.
        if _zone_param(request) == (None, False):
            _bootstrap_zones = zone_registry.list_zones(hass)
            if len(_bootstrap_zones) > 1:
                # Issue #1418: no zone's data is guessed here, but the request still named no zone, and the owner's
                # rule is that every such request is flagged (dashboards on a device's first visit included).
                zone_registry.flag_unscoped_request(
                    hass,
                    request=f"{request.method} {request.path} from {request.remote}",
                    outcome="answered with the list of zones only",
                )
                return self.json(
                    {
                        "zone_selection_required": True,
                        "zones": _bootstrap_zones,
                        "zone_count": len(_bootstrap_zones),
                        "version": VERSION,
                        # Issue #1341: the dashboard's language fallback applies before a zone is chosen too.
                        "language": i18n.hass_language(hass),
                    }
                )

        coordinator, ambiguous_refused = _get_coordinator(hass, request)
        if ambiguous_refused:
            return self.json(_ambiguous_zone_payload(hass), status_code=400)
        if not coordinator:
            return self.json(_error_body(hass, "not_loaded"), status_code=503)

        with log_capture.zone_scope(coordinator.zone_label):
            data = coordinator.data or {}
            ae = coordinator.automation_engine
            # Issue #466: deliberately NOT read from coordinator.data (which is only refreshed
            # once per ~30-min update cycle) — this powers the ca_target_heat/cool divergence
            # check (#402/#462), whose entire purpose is comparing CA's computed target against
            # the REAL thermostat right now. Reading a stale cached setpoint here would mask
            # exactly the kind of stuck/frozen-target bug that check exists to catch.
            climate_state = hass.states.get(coordinator.config.get("climate_entity", ""))
            hvac_mode = climate_state.state if climate_state else "unknown"

            # Set point(s): only include when HVAC is actively running. Single-setpoint modes
            # (cool/heat) expose `temperature`; the heat_cool band (Issue #249/#266) exposes
            # `target_temp_low`/`target_temp_high` instead. Attributes are already in the thermostat's
            # display unit, so they are sent as-is (matching `current_setpoint`).
            setpoint = None
            target_temp_low = None
            target_temp_high = None
            if climate_state and hvac_mode != "off":
                setpoint = climate_state.attributes.get("temperature")
                target_temp_low = climate_state.attributes.get("target_temp_low")
                target_temp_high = climate_state.attributes.get("target_temp_high")

            indoor_temp = coordinator._get_indoor_temp()
            outdoor_temp = coordinator._last_outdoor_temp
            unit = coordinator.config.get("temp_unit", "fahrenheit")
            indoor_temp_display = display_temp(indoor_temp, unit)
            outdoor_temp_display = display_temp(outdoor_temp, unit)
            # Deliberately NOT read from coordinator.data (only refreshed once per ~30-min
            # update cycle) — same staleness class as the ca_target_heat/cool fields below,
            # and the same fix: read the live classification instead of the cached snapshot.
            # Before this, a report could show e.g. "warming 3.5°F" from a stale cycle while
            # the classifier's own current output was "stable" — see docs investigation for
            # the exact reproduction. Falls back to the coordinator.data snapshot only when no
            # classification has run yet (e.g. right after restart), matching the fallback
            # pattern already used for _ca_target_heat/_ca_target_cool just below.
            trend_direction_display = data.get(ATTR_TREND, "unknown")
            trend_magnitude_display = display_delta(data.get(ATTR_TREND_MAGNITUDE, 0), unit)

            # Issue #402: ca_target_heat/cool must reflect the sleep band during the sleep
            # window, not always the flat daytime comfort_heat/comfort_cool — otherwise the
            # single-setpoint divergence check below (and any heat_cool-mode consumer of these
            # fields) compares the real thermostat setpoint against the wrong intended value
            # all night, masking exactly the kind of stuck/frozen-target bug this issue covers.
            #
            # Issue #462: route through select_comfort_band() — the canonical resolver every
            # real setpoint-writing code path (apply_classification, handle_bedtime,
            # handle_pre_cool, handle_morning_wakeup, occupancy handlers) already uses — instead
            # of a third independent inline implementation of this branch. Deliberately NOT
            # compute_bedtime_setback() (the chart/briefing's adaptive resolver, a different,
            # documented split — see const.py's #333 changelog): this field exists to detect
            # divergence from what the thermostat is ACTUALLY being driven to, which is
            # select_comfort_band()'s job. Routing through it also fixes a real gap the old
            # inline branch had: it ignored occupancy mode entirely, so away/vacation setback
            # was never reflected here even though the thermostat was really being held at the
            # setback band — exactly the kind of divergence this field exists to catch.
            #
            # Issue #1345: the assembly lives in automation.active_thresholds_for(), which the Heat /
            # Cool Threshold sensors also read, so these fields and the sensors cannot drift
            # (#402/#1080 sibling-formulation class). Before the first classification it returns
            # the shared fallback_edges_without_classification() placeholder.
            from .automation import active_thresholds_for  # noqa: PLC0415

            classification = coordinator.current_classification
            _thresholds = active_thresholds_for(coordinator)
            _ca_target_heat = _thresholds["heat"]
            _ca_target_cool = _thresholds["cool"]

            if classification is not None:
                trend_direction_display = classification.trend_direction
                trend_magnitude_display = display_delta(classification.trend_magnitude, unit)

            # Issue #402 follow-up: surface the WHF fan's actual on/off cycling band so the
            # Natural Vent status card can show it instead of a single static midpoint number
            # with no visible range — this was part of why "target 71°F but indoor is 69°F,
            # why is the fan still on" read as contradictory (69°F is within the 70-72°F
            # cycling band, the fan just hasn't ticked past 70°F yet).
            #
            # nat_vent_active/nat_vent_ac_assist were never included in this endpoint at all
            # (only in the Debug tab's automation_state endpoint) — the frontend's "Natural
            # Vent" status-item has been unreachable dead code since it was written, since
            # data.nat_vent_active was always undefined here.
            _nat_vent_band = coordinator.compute_nat_vent_cycling_band()
            # Issue #903: compute_nat_vent_cycling_band() returns internal-Fahrenheit values
            # (same as _ca_target_heat/_ca_target_cool above) — convert to display units here,
            # matching the indoor_temp_display/outdoor_temp_display pattern, since the frontend
            # renders these directly with the configured unit symbol appended.
            _nat_vent_target_display = display_temp(_nat_vent_band["nat_vent_target"], unit)
            _nat_vent_on_threshold_display = display_temp(_nat_vent_band["nat_vent_on_threshold"], unit)
            _nat_vent_off_threshold_display = display_temp(_nat_vent_band["nat_vent_off_threshold"], unit)
            _nat_vent_active = bool(ae._natural_vent_active)
            # Issue #1142 D7: AC assist no longer exists (blower ventilation holds the thermostat Off); key kept, False.
            _nat_vent_ac_assist = False

            # Issue #527: pause_suppressed_classification had the identical "documented but
            # never wired in" gap as nat_vent_active above (see KNOWN_FIXES[367] in const.py) —
            # the frontend's Status-card line reading data.pause_suppressed_classification was
            # unreachable dead code because this endpoint never included the key. Computed live
            # (not from coordinator.data) for the same freshness reason as ca_target_heat/cool.
            # The text itself now lives here (backend) instead of hardcoded in index.html, so
            # the Status card's text has one source, like every other line on that card.
            _pause_suppressed_classification = (
                bool(ae.is_paused_by_door) and ae._last_classification_applied is not None
            )
            _pause_suppressed_msg = (
                i18n.Msg("status.pause_suppressed_classification") if _pause_suppressed_classification else None
            )
            _lang = i18n.hass_language(hass)
            _pause_suppressed_classification_text = (
                i18n.render(_pause_suppressed_msg, _lang) if _pause_suppressed_msg is not None else None
            )

            # Issue #480: gate on coordinator.last_update_success instead of silently
            # serving coordinator.data forever once updates start failing. HA's own
            # DataUpdateCoordinator retains the last successful snapshot indefinitely
            # after a failure — that's the correct behavior for entity state (avoids
            # flapping to unknown), but this status endpoint was reading that frozen
            # snapshot with zero indication anything was wrong. Same failure shape as
            # the ca_target_heat/cool staleness Issue #466 fixed above, applied to the
            # coordinator's overall health rather than those two fields specifically.
            _coordinator_healthy = bool(coordinator.last_update_success)
            # Issue #1194 (d): the remote-sensor outer loop, described once (comfort target vs the commanded setpoint,
            # the signed offset, the loop state, the sentence). Read live from the engine, never re-decided here.
            _remote_offset = remote_offset_view_for(ae, unit, _lang)
            _zones = zone_registry.list_zones(hass)
            _status_payload = {
                "version": VERSION,
                # Issue #1341: the backend language (hass.config.language resolved to a shipped one); the
                # dashboard's third fallback after ?lang= and the parent frame's hass.language.
                "language": i18n.hass_language(hass),
                "day_type": data.get(ATTR_DAY_TYPE, "unknown"),
                "trend_direction": trend_direction_display,
                "trend_magnitude": trend_magnitude_display,
                "hvac_mode": hvac_mode,
                ATTR_HVAC_ACTION: data.get(ATTR_HVAC_ACTION, ""),
                ATTR_HVAC_RUNTIME_TODAY: data.get(ATTR_HVAC_RUNTIME_TODAY, 0),
                ATTR_CURRENT_SETPOINT: setpoint,
                "target_temp_low": target_temp_low,
                "target_temp_high": target_temp_high,
                ATTR_INDOOR_TEMP: indoor_temp_display,
                "sleep_indoor_sensor_active": data.get("sleep_indoor_sensor_active", False),
                # Issue #1676: {since, entity, name} while the thermostat recalibrates after a heating stop, else None.
                "thermostat_recalibration": data.get("thermostat_recalibration"),
                "outdoor_temp": outdoor_temp_display,
                "unit": unit,
                # Issue #1341: the prose in the backend language, ``automation_status_msg`` ({key, params}) for the
                # viewer's language, and ``status_code`` (stable) for anything that branches on the state.
                **_text_fields("automation_status", data.get(ATTR_AUTOMATION_STATUS, "unknown"), _lang),
                "status_code": code_of(data.get(ATTR_AUTOMATION_STATUS)),
                # Issue #1404: the time-of-use bank holding the house right now (target, mode, end, the band it
                # goes back to, and the one sentence every surface quotes), live; null when no bank owns the
                # thermostat. ca_target_heat/ca_target_cool already quote the bank band while it does.
                "current_hold": _current_hold_payload(coordinator),
                # Issue #749: hard-invariant watchdog violations (e.g. AC and WHF both
                # physically on at once) — empty list when nothing is wrong. Surfaced on the
                # Status card only, per this project's Status Card Ontology: it answers "what's
                # happening right now, and why."
                "invariant_violations": data.get("invariant_violations", []),
                # Issue #1128 I8: household-promise counters (counts + last hit only), null when none missed;
                # shown on the Debug tab only, never on the Status cards.
                "promise_summary": data.get("promise_summary"),
                # Issue #1049: thermostat not following commands (open mode-alignment / command-failure
                # episode) — empty list when fine. Same Status Card Ontology as invariant_violations.
                "hvac_control_issues": data.get("hvac_control_issues", []),
                # Issue #805: entities (thermostat, weather source, sensors, fan, toggles,
                # notify service) currently missing or unavailable — empty list when
                # everything is fine. Same Status Card Ontology as invariant_violations above.
                "entity_health_issues": data.get("entity_health_issues", []),
                "compliance_score": data.get(ATTR_COMPLIANCE_SCORE, 1.0),
                **_text_fields("next_action", data.get(ATTR_NEXT_ACTION, ""), _lang, code=True),
                **_text_fields("next_automation_action", data.get(ATTR_NEXT_AUTOMATION_ACTION, ""), _lang, code=True),
                **_text_fields("next_automation_time", data.get(ATTR_NEXT_AUTOMATION_TIME, ""), _lang),
                "automation_enabled": coordinator.automation_enabled,
                "occupancy_mode": coordinator._occupancy_mode,
                # The three fan cards keep their documented English values (the "Fan Status Values" enum, plus the
                # rate-limit / blind notes): dashboards and automations compare them. ``*_code`` is the stable
                # token code and ``*_msg`` renders token and notes in the viewer's language.
                **_text_fields("fan_status", data.get(ATTR_FAN_STATUS, "disabled"), None, code=True),
                **_text_fields("whf_status", data.get(ATTR_WHF_STATUS), None, code=True),
                **_text_fields("hvac_fan_status", data.get(ATTR_HVAC_FAN_STATUS), None, code=True),
                **_text_fields("contact_status", data.get(ATTR_CONTACT_STATUS, "no sensors"), _lang, code=True),
                "contact_sensors": coordinator._compute_contact_details(),
                "manual_override_active": ae._manual_change_protected(),
                "fan_override_active": ae._fan_override_active,
                # Issue #1095: lets the dashboard offer "End Grace Period" for a grace that has no
                # override flag behind it (e.g. fan_off), which "Cancel Override" never enabled for.
                "grace_active": bool(ae._grace_active),
                # Issue #1205: the occupant's fan-off pause keeps its own deadline and can outlive every grace,
                # so the dashboard must be able to offer "End Fan-Off Pause" with no grace or override running.
                "fan_off_hold_active": ae._fan_off_hold_active() is True,
                # Issue #486: QuietCool RF remote timer selection, null unless a timer set the
                # active fan override's grace duration.
                "fan_remote_timer_hours": data.get("fan_remote_timer_hours"),
                "fan_remote_timer_ends": data.get("fan_remote_timer_ends"),
                # Issue #519: current QuietCool remote-reported speed, null when unknown (no
                # ambient sensor discoverable, or no press observed yet this session).
                "fan_remote_speed": data.get("fan_remote_speed"),
                "paused_by_door": ae.is_paused_by_door,
                "pause_suppressed_classification": _pause_suppressed_classification,
                "pause_suppressed_classification_text": _pause_suppressed_classification_text,
                "pause_suppressed_classification_text_msg": (
                    _pause_suppressed_msg.to_wire() if _pause_suppressed_msg is not None else None
                ),
                "ca_target_heat": display_temp(_ca_target_heat, unit),
                "ca_target_cool": display_temp(_ca_target_cool, unit),
                "remote_offset": _remote_offset,
                # Issue #1691: the humid-air guard's line while it blocks fan starts (None otherwise)
                "humid_air_guard": humid_air_view(ae, unit, _lang),
                "nat_vent_active": _nat_vent_active,
                "nat_vent_ac_assist": _nat_vent_ac_assist,
                "nat_vent_target": _nat_vent_target_display,
                "nat_vent_on_threshold": _nat_vent_on_threshold_display,
                "nat_vent_off_threshold": _nat_vent_off_threshold_display,
                **_text_fields("pre_cool_status", data.get("pre_cool_status"), _lang),
                # Issue #1699: "Cooling off: outdoor 3 °C (below 16 °C)" while a lockout / the season takes a side away.
                **_text_fields("zone_lockout_status", data.get("zone_lockout_status"), _lang),
                "coordinator_healthy": _coordinator_healthy,
                # Issue #796 PR9: dashboard zone selector. loadStatus() polls every
                # cycle regardless of which tab is active, so this is the one
                # endpoint guaranteed to have data by the time the page needs to
                # decide whether to render the selector row at all. zone_count is
                # the same "is this a multi-zone install" question the
                # Transitional Safety Window Repairs check answers — reusing
                # zone_registry.list_zones() here keeps that a single computation
                # instead of a second parallel counting implementation.
                "zones": _zones,
                "zone_count": len(_zones),
            }
            if not _coordinator_healthy:
                _status_payload["last_error"] = coordinator.last_update_error
                _status_payload["stale_since"] = coordinator.last_update_error_time

            return self.json(_status_payload)


class ClimateAdvisorBriefingView(HomeAssistantView):
    """Return the current daily briefing text.

    Optional query parameter:
        verbosity: "tldr_only" | "normal" (default) | "verbose"
            - "tldr_only": the short briefing (the TLDR table the push notification carries)
            - "normal": the full briefing
            - "verbose": accepted for compatibility; the same as "normal" (no longer briefing exists)
        Both texts are the ones the coordinator built for today's briefing; nothing is regenerated (Issue #1577).
    """

    url = API_BRIEFING
    name = "api:climate_advisor:briefing"
    requires_auth = True

    async def get(self, request: web.Request) -> web.Response:
        hass = request.app["hass"]
        coordinator, ambiguous_refused = _get_coordinator(hass, request)
        if ambiguous_refused:
            return self.json(_ambiguous_zone_payload(hass), status_code=400)
        if not coordinator:
            return self.json(_error_body(hass, "not_loaded"), status_code=503)

        with log_capture.zone_scope(coordinator.zone_label):
            verbosity = request.rel_url.query.get("verbosity", "normal")
            if verbosity not in ("tldr_only", "normal", "verbose"):
                return self.json(
                    _error_body(hass, "invalid_verbosity"),
                    status_code=400,
                )

            # Issue #1577: the coordinator builds both texts with every briefing (_build_briefing_text), so the short
            # one is served as is. There was never a regenerate method: the old hasattr() branch could not run, and
            # "tldr_only" silently returned the full briefing.
            briefing = coordinator._last_briefing
            if verbosity == "tldr_only" and coordinator._last_briefing_short:
                briefing = coordinator._last_briefing_short

            from .coordinator import _briefing_display  # noqa: PLC0415 - coordinator is heavy; same renderer as Status

            return self.json(
                {
                    "briefing": _briefing_display(hass, briefing),
                    # Issue #1341: the message the text renders from (None for a plain/legacy text).
                    "briefing_msg": (msg.to_wire() if (msg := getattr(briefing, "msg", None)) is not None else None),
                    "briefing_sent_today": coordinator._briefing_sent_today,
                    "verbosity": verbosity,
                }
            )


class ClimateAdvisorChartDataView(HomeAssistantView):
    """Return chart data for the temperature overlay chart."""

    url = API_CHART_DATA
    name = "api:climate_advisor:chart_data"
    requires_auth = True

    async def get(self, request: web.Request) -> web.Response:
        hass = request.app["hass"]
        coordinator, ambiguous_refused = _get_coordinator(hass, request)
        if ambiguous_refused:
            return self.json(_ambiguous_zone_payload(hass), status_code=400)
        if not coordinator:
            return self.json(_error_body(hass, "not_loaded"), status_code=503)

        with log_capture.zone_scope(coordinator.zone_label):
            range_str = request.rel_url.query.get("range", "24h")
            valid_ranges = {"6h", "12h", "24h", "3d", "7d", "30d", "1y"}
            if range_str not in valid_ranges:
                range_str = "24h"

            before_ts_str = request.rel_url.query.get("before_ts")
            before_ts: float | None = None
            if before_ts_str:
                try:
                    # Frontend sends milliseconds; coordinator expects seconds
                    before_ts = float(before_ts_str) / 1000.0
                except ValueError, TypeError:
                    before_ts = None

            # get_chart_data runs ODE prediction inline — offload to executor to avoid blocking event loop.
            chart_data = await hass.async_add_executor_job(
                functools.partial(coordinator.get_chart_data, range_str=range_str, before_ts=before_ts)
            )
            return self.json(chart_data)


class ClimateAdvisorAutomationStateView(HomeAssistantView):
    """Return automation engine debug state."""

    url = API_AUTOMATION_STATE
    name = "api:climate_advisor:automation_state"
    requires_auth = True

    async def get(self, request: web.Request) -> web.Response:
        hass = request.app["hass"]
        coordinator, ambiguous_refused = _get_coordinator(hass, request)
        if ambiguous_refused:
            return self.json(_ambiguous_zone_payload(hass), status_code=400)
        if not coordinator:
            return self.json(_error_body(hass, "not_loaded"), status_code=503)

        with log_capture.zone_scope(coordinator.zone_label):
            return self.json(coordinator.get_debug_state())


class ClimateAdvisorLearningView(HomeAssistantView):
    """Return learning engine state and today's record."""

    url = API_LEARNING
    name = "api:climate_advisor:learning"
    requires_auth = True

    async def get(self, request: web.Request) -> web.Response:
        hass = request.app["hass"]
        coordinator, ambiguous_refused = _get_coordinator(hass, request)
        if ambiguous_refused:
            return self.json(_ambiguous_zone_payload(hass), status_code=400)
        if not coordinator:
            return self.json(_error_body(hass, "not_loaded"), status_code=503)

        with log_capture.zone_scope(coordinator.zone_label):
            today_record = None
            if coordinator.today_record:
                today_record = asdict(coordinator.today_record)

            # Issue #1341: ``text`` renders in the backend language; ``text_msg`` is the wire form the
            # dashboard re-renders in the viewer's language.
            suggestion_texts = coordinator.learning.generate_suggestions(lang=i18n.hass_language(hass))
            suggestion_keys = coordinator.learning.get_last_suggestion_keys()
            suggestion_msgs = coordinator.learning.get_last_suggestion_msgs()
            suggestions = [
                {"key": k, "text": t, "text_msg": m.to_wire()}
                for k, t, m in zip(suggestion_keys, suggestion_texts, suggestion_msgs, strict=False)
            ]

            unit = coordinator.config.get("temp_unit", "fahrenheit")

            # Issue #1049: coordinator.tomorrow_plan is internal °F; convert a COPY at this
            # display boundary (the frontend appends the unit symbol and does no conversion).
            tomorrow_plan = coordinator.tomorrow_plan
            if tomorrow_plan is not None:
                tomorrow_plan = {
                    **tomorrow_plan,
                    "expected_high": display_temp(tomorrow_plan.get("expected_high"), unit),
                    "expected_low": display_temp(tomorrow_plan.get("expected_low"), unit),
                }

            return self.json(
                {
                    "today_record": today_record,
                    "yesterday_record": coordinator.yesterday_record,
                    "tomorrow_plan": tomorrow_plan,
                    "suggestions": suggestions,
                    "compliance": coordinator.learning.get_compliance_summary(),
                    "comfort_range_low": display_temp(coordinator.config.get("comfort_heat", 70), unit),
                    "comfort_range_high": display_temp(coordinator.config.get("comfort_cool", 75), unit),
                    "unit": unit,
                    # Analysis "Today" column: what holds now (the record latches and fills at end of day)
                    "today_live": coordinator.analysis_today_live()
                    if hasattr(coordinator, "analysis_today_live")
                    else {},
                    # Estimated cost needs a positive energy rate (Settings > Advanced); None = not set
                    "cost_configured": bool((coordinator.config.get(CONF_ENERGY_RATE) or 0) > 0),
                }
            )


class ClimateAdvisorForceReclassifyView(HomeAssistantView):
    """Force a coordinator data refresh (reclassification)."""

    url = API_FORCE_RECLASSIFY
    name = "api:climate_advisor:force_reclassify"
    requires_auth = True

    async def post(self, request: web.Request) -> web.Response:
        hass = request.app["hass"]
        coordinator, ambiguous_refused = _get_coordinator(hass, request)
        if ambiguous_refused:
            return self.json(_ambiguous_zone_payload(hass), status_code=400)
        if not coordinator:
            return self.json(_error_body(hass, "not_loaded"), status_code=503)

        with log_capture.zone_scope(coordinator.zone_label):
            await coordinator.async_request_refresh()
            return self.json({"status": "ok", **_msg_fields(hass, i18n.Msg("errors.reclassify_triggered"))})


class ClimateAdvisorSendBriefingView(HomeAssistantView):
    """Force-send the daily briefing."""

    url = API_SEND_BRIEFING
    name = "api:climate_advisor:send_briefing"
    requires_auth = True

    async def post(self, request: web.Request) -> web.Response:
        hass = request.app["hass"]
        coordinator, ambiguous_refused = _get_coordinator(hass, request)
        if ambiguous_refused:
            return self.json(_ambiguous_zone_payload(hass), status_code=400)
        if not coordinator:
            return self.json(_error_body(hass, "not_loaded"), status_code=503)

        with log_capture.zone_scope(coordinator.zone_label):
            # Issue #817 Part 4: optional {"notify": bool} body, default True — backward
            # compatible with the debug tab's "Send Briefing" button, which sends no body and
            # must keep always sending a real push/email. The dashboard's "Regenerate" button
            # sends {"notify": false} — the user is already looking at the screen, so a real
            # notification is unnecessary. Either way this is a manual invocation, so it never
            # sets respect_notification_mute — see _async_send_briefing()'s docstring.
            try:
                body = await request.json()
            except Exception:
                body = {}
            notify = bool(body.get("notify", True)) if isinstance(body, dict) else True

            coordinator._briefing_sent_today = False
            from homeassistant.util import dt as dt_util

            await coordinator._async_send_briefing(dt_util.now(), send_notifications=notify)
            return self.json(
                {
                    "status": "ok",
                    **_msg_fields(hass, i18n.Msg("errors.briefing_sent" if notify else "errors.briefing_regenerated")),
                }
            )


class ClimateAdvisorRespondSuggestionView(HomeAssistantView):
    """Accept or dismiss a learning suggestion."""

    url = API_RESPOND_SUGGESTION
    name = "api:climate_advisor:respond_suggestion"
    requires_auth = True

    async def post(self, request: web.Request) -> web.Response:
        hass = request.app["hass"]
        coordinator, ambiguous_refused = _get_coordinator(hass, request)
        if ambiguous_refused:
            return self.json(_ambiguous_zone_payload(hass), status_code=400)
        if not coordinator:
            return self.json(_error_body(hass, "not_loaded"), status_code=503)

        with log_capture.zone_scope(coordinator.zone_label):
            try:
                body = await request.json()
            except Exception:
                return self.json(_error_body(hass, "invalid_json_body"), status_code=400)

            action = body.get("action")
            suggestion_key = body.get("suggestion_key")
            feedback = body.get("feedback")

            # Validate feedback if present
            if feedback is not None and feedback not in ("correct", "incorrect"):
                return self.json(
                    _error_body(hass, "feedback_value"),
                    status_code=400,
                )

            # suggestion_key is required; action is required only when feedback is absent
            if not suggestion_key:
                return self.json(
                    _error_body(hass, "suggestion_key_required"),
                    status_code=400,
                )
            if action is None and feedback is None:
                return self.json(
                    _error_body(hass, "action_or_feedback_required"),
                    status_code=400,
                )
            if action is not None and action not in ("accept", "dismiss"):
                return self.json(
                    _error_body(hass, "action_value"),
                    status_code=400,
                )
            # Issue #1652: a key that doesn't exist used to come back "ok" and change nothing.
            if suggestion_key not in SUGGESTION_KEYS:
                return self.json(
                    _error_body(hass, "suggestion_key_unknown", suggestion_key=str(suggestion_key)),
                    status_code=400,
                )

            # Handle feedback recording (independent of action)
            if feedback is not None:
                coordinator.learning.record_feedback(suggestion_key, feedback)
                await hass.async_add_executor_job(coordinator.learning.save_state)

            # Handle accept/dismiss action
            if action == "accept":
                # Issue #1652: an acknowledgement (every suggestion is advice; no setting changes), through the one
                # accept path shared with the respond_to_suggestion service.
                from .services import async_accept_suggestion

                await async_accept_suggestion(hass, coordinator, suggestion_key)
                return self.json({"status": "ok", "acknowledged": suggestion_key, "changes": {}})
            elif action == "dismiss":
                coordinator.learning.dismiss_suggestion(suggestion_key)
                await hass.async_add_executor_job(coordinator.learning.save_state)
                return self.json({"status": "ok", "dismissed": suggestion_key})
            else:
                # feedback-only request
                return self.json({"status": "ok"})


class ClimateAdvisorConfigView(HomeAssistantView):
    """Return current configuration settings with metadata."""

    url = API_CONFIG
    name = "api:climate_advisor:config"
    requires_auth = True

    async def get(self, request: web.Request) -> web.Response:
        hass = request.app["hass"]
        coordinator, ambiguous_refused = _get_coordinator(hass, request)
        if ambiguous_refused:
            return self.json(_ambiguous_zone_payload(hass), status_code=400)
        if not coordinator:
            return self.json(_error_body(hass, "not_loaded"), status_code=503)

        with log_capture.zone_scope(coordinator.zone_label):
            config = coordinator.config
            settings = []

            for key, meta in CONFIG_METADATA.items():
                value = config.get(key)
                # Fall back to the metadata default so the UI shows the effective value,
                # not "not set", when a key was added after the config entry was created.
                if value is None:
                    value = meta.get("default")
                # Sanitize: replace notify service names (may reveal personal info)
                value_msg: i18n.Msg | None = None
                if key == "notify_service" or meta.get("sensitive"):
                    value = "configured" if value else "not set"
                    value_msg = i18n.Msg(
                        "settings.value_configured" if value == "configured" else "settings.value_not_set"
                    )
                # Convert time objects to strings
                if hasattr(value, "strftime"):
                    value = str(value)
                # Convert lists to counts for display
                if isinstance(value, list):
                    value_msg = i18n.Msg("settings.value_n_configured", n=len(value))
                    value = f"{len(value)} configured"
                # Apply display transforms (e.g., seconds → minutes for UI)
                transform = meta.get("display_transform")
                if transform == "seconds_to_minutes" and isinstance(value, (int, float)):
                    value = value // 60
                elif transform == "temp_source_label" and isinstance(value, str):
                    value_msg = _temp_source_msg(coordinator, key, value, config)
                    value = _temp_source_display(coordinator, key, value, _TEMP_SOURCE_LABELS.get(value, value), config)

                # Issue #1048: temperature settings are stored in internal degF and shown in the user's
                # unit: absolute ones via display_temp, the "degrees of tolerance" deltas (scale-only,
                # no offset) via display_delta -- the same conversion the options form uses (Issues
                # #1061/#1063). `unit` lives in the response (not CONFIG_METADATA) because it depends
                # on the installation's configured unit.
                setting_unit: str | None = None
                if (
                    isinstance(value, (int, float))
                    and not isinstance(value, bool)
                    and transform in ("temp_abs", "temp_delta")
                ):
                    user_unit = config.get("temp_unit", FAHRENHEIT)
                    convert = display_temp if transform == "temp_abs" else display_delta
                    value = convert(value, user_unit)
                    setting_unit = UNIT_SYMBOL.get(user_unit, UNIT_SYMBOL[FAHRENHEIT])

                entry = {
                    "key": key,
                    "value": value,
                    "label": meta["label"],
                    "description": meta["description"],
                    "label_msg": i18n.Msg(f"settings.{key}.label").to_wire(),
                    "description_msg": i18n.Msg(f"settings.{key}.description").to_wire(),
                    "category": meta["category"],
                }
                if value_msg is not None:
                    entry["value_msg"] = value_msg.to_wire()
                # Explicit units (Issue #1341): `unit` stays temperature-only (the symbol, as documented); the
                # static time unit ("min"/"s") rides in its own `time_unit` field. Labels carry no unit (Issue #1663:
                # they are the options-flow labels), so both dashboards show `time_unit` after the value.
                if setting_unit is not None:
                    entry["unit"] = setting_unit
                elif meta.get("time_unit"):
                    entry["time_unit"] = meta["time_unit"]
                settings.append(entry)

            return self.json({"settings": settings})


def _schedule_reclassify_after_cancel(hass: HomeAssistant, coordinator: Any, ae: Any) -> None:
    """Re-run the full decision path shortly after a user cancels an override (Issue #508).

    HVAC-mode-level counterpart to ``AutomationEngine.cancel_override()``'s fan-check/refresh
    calls — mirrors what a natural grace expiry converges on, via ``apply_classification()``.
    Shared by both cancel views so this scheduling logic can't drift between them a second time
    (it previously existed only in ``ClimateAdvisorCancelOverrideView``, never in the fan-cancel
    sibling).
    """
    from homeassistant.core import callback

    @callback
    def _apply_after_delay(_now):
        # Issue #911: fires ~10s after the request that scheduled it, well outside that
        # request handler's own zone_scope() block — needs its own explicit wrap so any
        # logging from apply_classification() attributes to the right zone.
        with log_capture.zone_scope(coordinator.zone_label):
            if coordinator._current_classification:
                # Issue #1231: carry the plan's close through, or the reset reopens the planned window period.
                hass.async_create_task(
                    decide(
                        coordinator,
                        ae.apply_classification,
                        coordinator._current_classification,
                        nat_vent_cutoff=ae._nat_vent_cutoff,
                        nat_vent_cutoff_already_reached=ae._nat_vent_cutoff_already_reached,
                    )
                )

    # Issue #1102 (F8): keep the handle on the coordinator so unloading it cancels this
    # follow-up. Exception to the release rule: one entry per cancel-override click,
    # never removed, a no-op after firing — growth is bounded by user clicks.
    coordinator._unsub_listeners.append(async_call_later(hass, 10, _apply_after_delay))


class ClimateAdvisorCancelOverrideView(HomeAssistantView):
    """Cancel manual override and resume automated HVAC control."""

    url = API_CANCEL_OVERRIDE
    name = "api:climate_advisor:cancel_override"
    requires_auth = True

    async def post(self, request: web.Request) -> web.Response:
        hass = request.app["hass"]
        coordinator, ambiguous_refused = _get_coordinator(hass, request)
        if ambiguous_refused:
            return self.json(_ambiguous_zone_payload(hass), status_code=400)
        if not coordinator:
            return self.json(_error_body(hass, "not_loaded"), status_code=503)

        with log_capture.zone_scope(coordinator.zone_label):
            ae = coordinator.automation_engine
            # Issue #1095: snapshot before cancel_override() clears the flags — a grace with no
            # override behind it (e.g. fan_off) is ended by this same call, but "Override
            # cancelled" would misdescribe it.
            had_override = ae._occupant_override_active()
            had_hold = ae._fan_off_hold_active()  # Issue #1205: a hold can run with no grace behind it
            cancelled = decide_sync(coordinator, ae.cancel_override, reason="user_cancel_override")
            if not cancelled:
                return self.json({"status": "ok", **_msg_fields(hass, i18n.Msg("errors.cancel_nothing"))})

            _schedule_reclassify_after_cancel(hass, coordinator, ae)

            what = "override" if had_override else ("hold" if had_hold else "grace")
            return self.json(
                {
                    "status": "ok",
                    **_msg_fields(hass, i18n.Msg("errors.cancel_done", what=what)),
                }
            )


class ClimateAdvisorResumeFromPauseView(HomeAssistantView):
    """Resume HVAC from a contact sensor pause (user override)."""

    url = API_RESUME_FROM_PAUSE
    name = "api:climate_advisor:resume_from_pause"
    requires_auth = True

    async def post(self, request: web.Request) -> web.Response:
        hass = request.app["hass"]
        coordinator, ambiguous_refused = _get_coordinator(hass, request)
        if ambiguous_refused:
            return self.json(_ambiguous_zone_payload(hass), status_code=400)
        if not coordinator:
            return self.json(_error_body(hass, "not_loaded"), status_code=503)

        with log_capture.zone_scope(coordinator.zone_label):
            ae = coordinator.automation_engine
            if not ae.is_paused_by_door:
                return self.json({"status": "ok", **_msg_fields(hass, i18n.Msg("errors.resume_not_paused"))})

            restored_mode = await decide(coordinator, ae.resume_from_pause)
            if ae.dry_run:
                # Issue #1306: observe-only -- the pause is cleared, but no restore was sent (and no grace started).
                return self.json(
                    {
                        "status": "ok",
                        **_msg_fields(hass, i18n.Msg("errors.resume_observe_only")),
                        "restored_mode": None,
                    }
                )
            if ae.resume_restore_pending:
                # Issue #1312: the restore did not land; it is being re-sent and the grace starts once it lands.
                return self.json(
                    {
                        "status": "ok",
                        **_msg_fields(hass, i18n.Msg("errors.resume_pending", mode=restored_mode or "none")),
                        "restored_mode": restored_mode,
                        "restore_pending": True,
                    }
                )
            return self.json(
                {
                    "status": "ok",
                    **_msg_fields(hass, i18n.Msg("errors.resume_done", mode=restored_mode or "none")),
                    "restored_mode": restored_mode,
                }
            )


class ClimateAdvisorCancelFanOverrideView(HomeAssistantView):
    """Clear the fan manual override and return fan to automation control."""

    url = API_CANCEL_FAN_OVERRIDE
    name = "api:climate_advisor:cancel_fan_override"
    requires_auth = True

    async def post(self, request: web.Request) -> web.Response:
        hass = request.app["hass"]
        coordinator, ambiguous_refused = _get_coordinator(hass, request)
        if ambiguous_refused:
            return self.json(_ambiguous_zone_payload(hass), status_code=400)
        if not coordinator:
            return self.json(_error_body(hass, "not_loaded"), status_code=503)

        with log_capture.zone_scope(coordinator.zone_label):
            ae = coordinator.automation_engine
            had_fan_override = ae._fan_override_active
            had_hold = ae._fan_off_hold_active()  # Issue #1205: the fan-off pause is a fan-side request too
            cancelled = decide_sync(coordinator, ae.cancel_override, reason="user_cancel_fan_override")
            if not cancelled:
                return self.json({"status": "ok", **_msg_fields(hass, i18n.Msg("errors.fan_cancel_nothing"))})

            _schedule_reclassify_after_cancel(hass, coordinator, ae)

            if had_hold and not had_fan_override:
                return self.json({"status": "ok", **_msg_fields(hass, i18n.Msg("errors.fan_pause_ended"))})
            return self.json({"status": "ok", **_msg_fields(hass, i18n.Msg("errors.fan_override_cleared"))})


class ClimateAdvisorToggleAutomationView(HomeAssistantView):
    """Toggle automation enabled/disabled state."""

    url = API_TOGGLE_AUTOMATION
    name = "api:climate_advisor:toggle_automation"
    requires_auth = True

    async def post(self, request: web.Request) -> web.Response:
        hass = request.app["hass"]
        coordinator, ambiguous_refused = _get_coordinator(hass, request)
        if ambiguous_refused:
            return self.json(_ambiguous_zone_payload(hass), status_code=400)
        if not coordinator:
            return self.json(_error_body(hass, "not_loaded"), status_code=503)

        with log_capture.zone_scope(coordinator.zone_label):
            new_state = not coordinator.automation_enabled
            coordinator.set_automation_enabled(new_state)

            return self.json(
                {
                    "status": "ok",
                    "automation_enabled": new_state,
                    **_msg_fields(
                        hass, i18n.Msg("errors.automation_toggled", state="enabled" if new_state else "disabled")
                    ),
                }
            )


class ClimateAdvisorAIStatusView(HomeAssistantView):
    """API endpoint for AI status and recent request history."""

    url = API_AI_STATUS
    name = "api:climate_advisor:ai_status"
    requires_auth = True

    async def get(self, request: web.Request) -> web.Response:
        hass = request.app["hass"]
        coordinator, ambiguous_refused = _get_coordinator(hass, request)
        if ambiguous_refused:
            return self.json(_ambiguous_zone_payload(hass), status_code=400)
        if not coordinator:
            return self.json(_error_body(hass, "not_loaded"), status_code=503)

        with log_capture.zone_scope(coordinator.zone_label):
            if coordinator.claude_client:
                status = coordinator.claude_client.get_status()
                history = coordinator.claude_client.get_request_history()
                # SECURITY: ensure API key is not in the response
                status.pop("api_key", None)
                return self.json(
                    {
                        "status": status,
                        "recent_requests": history[-10:],
                    }
                )
            return self.json(
                {
                    "status": {
                        "status": "disabled",
                        # Issue #1336: which provider the (install-wide) AI settings name, even while off
                        "provider": provider_of(coordinator.config),
                        "ai_task_entity": str(coordinator.config.get(CONF_AI_TASK_ENTITY) or ""),
                    },
                    "recent_requests": [],
                }
            )


class ClimateAdvisorAILimitsView(HomeAssistantView):
    """GET /api/climate_advisor/ai_limits — the AI provider's limits against the requirements (Issue #1336).

    The dashboard asks before starting an investigation: a known-too-small limit gets a warning with
    *Run anyway* / *Cancel*; an unreadable one gets a one-line note. Never blocks anything.
    """

    url = API_AI_LIMITS
    name = "api:climate_advisor:ai_limits"
    requires_auth = True

    async def get(self, request: web.Request) -> web.Response:
        hass = request.app["hass"]
        coordinator, ambiguous_refused = _get_coordinator(hass, request)
        if ambiguous_refused:
            return self.json(_ambiguous_zone_payload(hass), status_code=400)
        if not coordinator:
            return self.json(_error_body(hass, "not_loaded"), status_code=503)

        with log_capture.zone_scope(coordinator.zone_label):
            return self.json(await ai_limits.async_read_limits(hass, coordinator.config))


def _cut_short_fields(data: Any, truncated: bool) -> dict[str, Any]:
    """``{"cut_short", "cut_short_code"}`` for an investigation result (Issue #1336)."""
    cut_short = report_cut_short(data, truncated=bool(truncated))
    return {"cut_short": cut_short, "cut_short_code": AI_CUT_SHORT if cut_short else None}


def _oldest_event_time(event_log: list[dict]) -> str | None:
    """Return the earliest "time" value in event_log, or None if empty/unset.

    Uses min() rather than event_log[0] — the log is chronological in normal
    production operation, but restore/merge paths don't guarantee that, and this
    is a correctness-facing truncation signal, not a performance-critical path.
    """
    times = [e.get("time") for e in event_log if isinstance(e, dict) and e.get("time")]
    return min(times) if times else None


class ClimateAdvisorActivityRecordView(HomeAssistantView):
    """Deterministic activity record endpoint — no AI required."""

    url = "/api/climate_advisor/activity_record"
    name = "api:climate_advisor:activity_record"
    requires_auth = True

    async def get(self, request: web.Request) -> web.Response:
        hass = request.app["hass"]
        coordinator, ambiguous_refused = _get_coordinator(hass, request)
        if ambiguous_refused:
            return self.json(_ambiguous_zone_payload(hass), status_code=400)
        if not coordinator:
            return self.json(_error_body(hass, "not_loaded"), status_code=503)

        with log_capture.zone_scope(coordinator.zone_label):
            try:
                hours = float(request.query.get("hours", 12))
            except TypeError, ValueError:
                hours = 12.0
            hours = max(1.0, min(hours, 168.0))

            from homeassistant.util import dt as dt_util

            event_log = list(getattr(coordinator, "_event_log", []) or [])
            # Issue #920: this is a display-only endpoint with no LLM in its path, so
            # it doesn't need the LLM-facing 200-row token-budget cap — use the real
            # storage ceiling instead of an artificial display limit.
            table = build_event_timeline_table(
                event_log,
                coordinator.config or {},
                hours,
                dt_util.now(),
                newest_first=True,
                limit=EVENT_LOG_CAP,
            )

            # Issue #1341: the same rows as structured data (each carries ``event_msg``/``settings_msg`` beside its
            # English text) so the dashboard renders them in the viewer's language. Additive: ``table`` is unchanged.
            timeline = build_event_timeline_wire(
                event_log,
                coordinator.config or {},
                hours,
                dt_util.now(),
                newest_first=True,
                limit=EVENT_LOG_CAP,
            )

            # Issue #973: append the compact diagnostic footer so this view carries the
            # same "what does the thermostat actually report + CA version" context the
            # AI Investigator and raw log download already got in #968 — flows through
            # automatically to on-screen display, Copy, Download .md, and the GitHub
            # issue body, since all four reuse this same table string on the frontend.
            try:
                from .diagnostic_snapshot import (  # noqa: PLC0415
                    build_diagnostic_snapshot,
                    render_diagnostic_snapshot_compact,
                )

                footer = render_diagnostic_snapshot_compact(
                    build_diagnostic_snapshot(hass, coordinator, include_control_quality=False)
                )
                table = table + "\n\n" + footer
            except Exception:
                _LOGGER.warning("activity_record: diagnostic footer failed — skipping", exc_info=True)

            cutoff_iso = (dt_util.now() - timedelta(hours=hours)).isoformat()
            oldest_stored = _oldest_event_time(event_log)
            is_truncated = bool(oldest_stored) and oldest_stored > cutoff_iso

            return self.json(
                {
                    "table": table,
                    "rows": timeline["rows"],
                    "limited_msg": timeline["limited_msg"],
                    "no_events_msg": timeline["no_events_msg"],
                    "whf_warning_msg": timeline["whf_warning_msg"],
                    "hours": hours,
                    "generated_at": dt_util.now().isoformat(),
                    "is_truncated": is_truncated,
                    "oldest_available": oldest_stored,
                }
            )


class ClimateAdvisorInvestigateView(HomeAssistantView):
    """POST /api/climate_advisor/ai_investigate — run an investigation."""

    url = API_AI_INVESTIGATE
    name = "api:climate_advisor:ai_investigate"
    requires_auth = True

    async def post(self, request: web.Request) -> web.Response:
        hass = request.app["hass"]
        coordinator, ambiguous_refused = _get_coordinator(hass, request)
        if ambiguous_refused:
            return self.json(_ambiguous_zone_payload(hass), status_code=400)
        if not coordinator:
            return self.json(_error_body(hass, "not_loaded"), status_code=503)

        with log_capture.zone_scope(coordinator.zone_label):
            # Parse optional JSON body — body may be absent
            try:
                body = await request.json()
            except Exception:
                body = {}

            focus: str = str(body.get("focus", ""))
            # Issue #920: clamp to 168h (7 days) — the event log's own retention
            # ceiling (EVENT_LOG_MAX_AGE_HOURS), not the old 720h/30-day value that
            # silently did nothing beyond 7 days since the data was never retained.
            try:
                hours: int = max(1, min(int(body.get("hours", 24)), 168))
            except ValueError, TypeError:
                hours = 24
            deep: bool = bool(body.get("deep", False))

            if not coordinator.config.get(CONF_AI_ENABLED, DEFAULT_AI_ENABLED):
                return self.json(_msg_fields(hass, i18n.Msg(_ERROR_KEYS["ai_not_enabled"])), status_code=403)

            if not coordinator.config.get(CONF_AI_INVESTIGATOR_ENABLED, DEFAULT_AI_INVESTIGATOR_ENABLED):
                return self.json(_msg_fields(hass, i18n.Msg(_ERROR_KEYS["investigator_not_enabled"])), status_code=403)

            if coordinator.claude_client is None:
                return self.json(_msg_fields(hass, i18n.Msg(_ERROR_KEYS["ai_client_unavailable"])), status_code=503)

            allowed, reason = coordinator.claude_client.check_investigator_rate_limit()
            if not allowed:
                return self.json_message(reason, status_code=429)

            _LOGGER.info(
                "Investigation requested: focus_len=%d hours=%d",
                len(focus),
                hours,
            )

            accept = request.headers.get("Accept", "")
            if "text/event-stream" in accept:
                stream_resp = web.StreamResponse(
                    headers={
                        "Cache-Control": "no-cache",
                        "X-Accel-Buffering": "no",
                        "Access-Control-Allow-Origin": "*",
                    }
                )
                stream_resp.content_type = "text/event-stream"
                await stream_resp.prepare(request)

                final_result: dict | None = None
                chunk_count = 0
                async for event in coordinator.ai_skills.async_execute_streaming(
                    "investigator",
                    hass,
                    coordinator,
                    coordinator.claude_client,
                    focus=focus,
                    hours=hours,
                    deep=deep,
                ):
                    if event.get("type") == "done":
                        # Issue #1336: a reply missing required sections is flagged so the occupant is told
                        event.update(_cut_short_fields(event.get("data"), event.get("truncated", False)))
                    await stream_resp.write(("data: " + json.dumps(event) + "\n\n").encode())
                    await stream_resp.drain()
                    if event.get("type") == "chunk":
                        chunk_count += 1
                        if chunk_count == 1:
                            _LOGGER.debug("SSE first chunk flushed to client")
                    elif event.get("type") == "done":
                        _LOGGER.info("SSE stream complete: chunks=%d", chunk_count)
                        final_result = {
                            "success": event.get("success", True),
                            "source": event.get("source", "ai"),
                            "data": event.get("data", {}),
                            "error": None,
                            "input_context": event.get("input_context", ""),
                            "raw_response": event.get("raw_response", ""),
                            "truncated": event.get("truncated", False),
                            "truncated_empty": event.get("truncated_empty", False),
                            "error_code": event.get("error_code"),
                            "cut_short": event.get("cut_short", False),
                            "cut_short_code": event.get("cut_short_code"),
                        }
                        # Issue #925: activity_summary now arrives already populated in
                        # event["data"] — it's an ordinary parsed LLM section (see
                        # ai_skills_investigator.py's ACTIVITY SUMMARY prompt section)
                        # or, on the fallback path, set directly by investigation_fallback().
                        # No separate post-parse injection needed any more; this also
                        # structurally fixes the old bug where the live SSE stream never
                        # saw the enrichment that only the persisted copy received.
                        if final_result["truncated_empty"]:
                            # Issue #563 follow-on: distinct from ordinary truncation — the
                            # model produced zero visible answer text despite consuming the
                            # full response budget. "Raise max response length" is misleading
                            # here since there's no partial content that a bigger budget would
                            # have salvaged; the model itself may need a different
                            # reasoning_effort or a much larger ceiling to answer at all.
                            _LOGGER.warning(
                                "Investigation report produced zero visible output despite "
                                "consuming the full response budget — raising Maximum "
                                "Response Length may not fix this; consider trying a "
                                "different reasoning effort for this model"
                            )
                        elif final_result["truncated"]:
                            _LOGGER.warning(
                                "Investigation report truncated: hit max_tokens limit; "
                                "consider raising Maximum Response Length (AI settings)"
                            )
                        elif final_result["cut_short"]:
                            _LOGGER.warning("Investigation report cut short: required sections missing")

                if final_result and final_result.get("success"):
                    coordinator.claude_client.increment_investigator_counter()
                    await coordinator.async_store_investigation_report(final_result)
                    _LOGGER.info("Investigation (streaming) stored")

                await stream_resp.write_eof()
                return stream_resp

            result = await coordinator.ai_skills.async_execute(
                "investigator",
                hass,
                coordinator,
                coordinator.claude_client,
                focus=focus,
                hours=hours,
                deep=deep,
            )

            result.setdefault("error_code", None)
            result.update(_cut_short_fields(result.get("data"), result.get("truncated", False)))
            if result.get("success") or result.get("source") == "fallback":
                # Issue #925: activity_summary already arrives populated in result["data"]
                # — see streaming path above.
                if result.get("truncated_empty"):
                    _LOGGER.warning(
                        "Investigation report produced zero visible output despite "
                        "consuming the full response budget — raising Maximum "
                        "Response Length may not fix this; consider trying a "
                        "different reasoning effort for this model"
                    )
                elif result.get("truncated"):
                    _LOGGER.warning(
                        "Investigation report truncated: hit max_tokens limit; "
                        "consider raising Maximum Response Length (AI settings)"
                    )
                coordinator.claude_client.increment_investigator_counter()
                await coordinator.async_store_investigation_report(result)
                _LOGGER.info("Investigation complete: source=%s", result.get("source", "unknown"))
                return self.json(result)

            _failure = result.get("error")
            if _failure:  # the investigator's own text (produced outside this module): shown as it is
                return self.json_message(_failure, status_code=500)
            return self.json(_msg_fields(hass, i18n.Msg(_ERROR_KEYS["investigation_failed"])), status_code=500)


class ClimateAdvisorInvestigationReportsView(HomeAssistantView):
    """GET /api/climate_advisor/investigation_reports — list investigation history."""

    url = API_INVESTIGATION_REPORTS
    name = "api:climate_advisor:investigation_reports"
    requires_auth = True

    async def get(self, request: web.Request) -> web.Response:
        hass = request.app["hass"]
        coordinator, ambiguous_refused = _get_coordinator(hass, request)
        if ambiguous_refused:
            return self.json(_ambiguous_zone_payload(hass), status_code=400)
        if not coordinator:
            return self.json(_error_body(hass, "not_loaded"), status_code=503)

        with log_capture.zone_scope(coordinator.zone_label):
            return self.json(coordinator.get_investigation_report_history())


class ClimateAdvisorEventLogView(HomeAssistantView):
    """Return the in-memory automation event log for the requested time window (Issue #76)."""

    url = API_EVENT_LOG
    name = "api:climate_advisor:event_log"
    requires_auth = True

    async def get(self, request: web.Request) -> web.Response:
        from homeassistant.util import dt as dt_util

        hass = request.app["hass"]
        coordinator, ambiguous_refused = _get_coordinator(hass, request)
        if ambiguous_refused:
            return self.json(_ambiguous_zone_payload(hass), status_code=400)
        if not coordinator:
            return self.json(_error_body(hass, "not_loaded"), status_code=503)

        with log_capture.zone_scope(coordinator.zone_label):
            try:
                hours = float(request.rel_url.query.get("hours", "24"))
                hours = max(0.5, min(hours, 168))  # clamp: 30 min – 7 days
            except ValueError, TypeError:
                hours = 24.0

            cutoff = (dt_util.now() - timedelta(hours=hours)).isoformat()
            events = [e for e in coordinator._event_log if e.get("time", "") >= cutoff]

            oldest_stored = _oldest_event_time(coordinator._event_log)
            is_truncated = bool(oldest_stored) and oldest_stored > cutoff

            # Issue #968: always include the diagnostic snapshot (entity attributes + CA
            # version) so a downloaded log bundle is self-contained for triage without a
            # separate manual round-trip to ask what the thermostat reports.
            try:
                from .diagnostic_snapshot import async_build_diagnostic_snapshot  # noqa: PLC0415

                # Issue #1331: the control-quality report is built in the executor, not on the event loop.
                diagnostics = await async_build_diagnostic_snapshot(hass, coordinator)
            except Exception:
                diagnostics = None

            return self.json(
                {
                    "events": events,
                    "total": len(events),
                    "hours": hours,
                    "is_truncated": is_truncated,
                    "oldest_available": oldest_stored,
                    "diagnostics": diagnostics,
                }
            )


class ClimateAdvisorEnginesView(HomeAssistantView):
    """Return prediction engine status from the learning engine."""

    url = API_ENGINES
    name = "api:climate_advisor:engines"
    requires_auth = True

    async def get(self, request: web.Request) -> web.Response:
        hass = request.app["hass"]
        coordinator, ambiguous_refused = _get_coordinator(hass, request)
        if ambiguous_refused:
            return self.json(_ambiguous_zone_payload(hass), status_code=400)
        if not coordinator:
            return self.json(_error_body(hass, "not_loaded"), status_code=503)

        with log_capture.zone_scope(coordinator.zone_label):
            if not hasattr(coordinator, "learning") or not hasattr(coordinator.learning, "get_engine_status"):
                return self.json(_error_body(hass, "engine_status_unavailable"), status_code=503)

            status = coordinator.learning.get_engine_status()
            status["unit"] = coordinator.config.get("temp_unit", "fahrenheit")
            # Issue #1230 R1: the v4 shadow model beside the v3 engines (Debug visibility only).
            if hasattr(coordinator, "v4_model_summary"):
                status["v4"] = coordinator.v4_model_summary()
            # The rates the decisions read now and where each comes from (the rows above are v3's learned values)
            if hasattr(coordinator, "decision_rates_summary"):
                status["decisions"] = coordinator.decision_rates_summary()
            return self.json(status)


class ClimateAdvisorPredictionAccuracyView(HomeAssistantView):
    """GET /api/climate_advisor/prediction_accuracy - v4 vs v3 vs "no change" (Issue #1230 R1, read-only).

    Query: ``zone`` (entry id; ``entry_id`` also accepted like the other views), ``sensor`` (entity id or ``_ca``,
    default ``_ca``), ``lead`` (1h|4h|12h|24h, default 4h), ``range`` (6h|12h|24h|3d|7d|30d|1y, default 24h) and
    ``before`` (ISO time, optional: the window ends there instead of now, for scrolling back). A bad ``lead`` or
    ``range`` resets to its default and a bad ``before`` is ignored (clamped, never a 400, like chart_data).
    """

    url = API_PREDICTION_ACCURACY
    name = "api:climate_advisor:prediction_accuracy"
    requires_auth = True

    async def get(self, request: web.Request) -> web.Response:
        hass = request.app["hass"]
        query = request.rel_url.query
        # No guess with several zones and no ``zone`` (unlike the other GET views): a scorecard silently showing
        # another zone's numbers would mislead the owner's per-zone v4 decision. The preview page always sends
        # ``zone`` once ``/status`` has listed the zones; a single-zone install still resolves its only zone.
        coordinator, ambiguous_refused = _resolve_request_zone(hass, request, allow_guess=False)
        if ambiguous_refused:
            return self.json(_ambiguous_zone_payload(hass), status_code=400)
        if not coordinator:
            return self.json(_error_body(hass, "not_loaded"), status_code=503)

        with log_capture.zone_scope(coordinator.zone_label):
            range_str = query.get("range", prediction_accuracy.DEFAULT_RANGE)
            if range_str not in prediction_accuracy.RANGES:
                range_str = prediction_accuracy.DEFAULT_RANGE
            lead = query.get("lead", prediction_accuracy.DEFAULT_LEAD)
            if lead not in V4_ARCHIVE_LEADS:
                lead = prediction_accuracy.DEFAULT_LEAD
            sensor = query.get("sensor") or prediction_accuracy.CA_SENSOR
            before: datetime | None = None
            if query.get("before"):
                before = dt_util.parse_datetime(query["before"])
                if before is not None and before.tzinfo is None:
                    before = before.replace(tzinfo=dt_util.now().tzinfo)

            inputs = coordinator.v4_accuracy_inputs()
            config_dir = inputs.pop("config_dir")
            entry_id = inputs["zone"]
            # Issue #1431: the bedtime / wake-up swap rule's settings, read on the loop (ca_swaps in the payload).
            cfg = getattr(coordinator, "config", None)
            if isinstance(cfg, dict):
                inputs.setdefault("sleep_sensor", cfg.get("sleep_indoor_temp_entity"))
                inputs.setdefault("sleep_time", cfg.get("sleep_time"))
                inputs.setdefault("wake_time", cfg.get("wake_time"))

            def _build() -> dict[str, Any]:
                # Blocking: reads the backtest file and walks the chart log.
                return prediction_accuracy.build_prediction_accuracy(
                    sensor=sensor,
                    lead=lead,
                    range_str=range_str,
                    before=before,
                    now=dt_util.now(),
                    backtest=prediction_accuracy.load_backtest(config_dir, entry_id),
                    **inputs,
                )

            return self.json(await hass.async_add_executor_job(log_capture.bind_zone_for_executor(_build)))


class ClimateAdvisorTimelineView(HomeAssistantView):
    """GET /api/climate_advisor/timeline - the operation timeline every occupant-facing surface reads (Issue #1230 R3b).

    Query: ``zone`` (entry id; ``entry_id`` also accepted). With several zones and no ``zone`` the answer is 400 (the
    same refusal as ``prediction_accuracy``): a plan silently shown for another zone would mislead. The body is the
    frozen contract dict (``operation_timeline.to_dict``) with temperatures in the display unit, ``unit`` added, and
    the builder's working data under ``detail``. 503 when the zone has no timeline yet (before its first
    classification).
    """

    url = API_TIMELINE
    name = "api:climate_advisor:timeline"
    requires_auth = True

    async def get(self, request: web.Request) -> web.Response:
        hass = request.app["hass"]
        coordinator, ambiguous_refused = _resolve_request_zone(hass, request, allow_guess=False)
        if ambiguous_refused:
            return self.json(_ambiguous_zone_payload(hass), status_code=400)
        if not coordinator:
            return self.json(_error_body(hass, "not_loaded"), status_code=503)
        with log_capture.zone_scope(coordinator.zone_label):
            payload = coordinator.timeline_payload()
            if payload is None:
                return self.json(
                    {"error": "timeline_not_ready", **_coded_extra(hass, "timeline_not_ready")}, status_code=503
                )
            return self.json(payload)


class ClimateAdvisorNightStrategyView(HomeAssistantView):
    """GET /api/climate_advisor/night_strategy - tonight's bedroom strategy for the "Tonight in the bedroom" card
    (Issue #1230 R6). Read-only advice: the page shows it, nothing starts a fan or opens a window.

    Query: ``zone`` (entry id; ``entry_id`` also accepted); with several zones and no ``zone`` the answer is 400, the
    same refusal as ``timeline``. The body is
     ``NightStrategy.to_dict()`` with temperatures in the display unit, ``unit``,
    ``headline``, ``tradeoffs``, ``source_model`` ("v4") and ``source_note`` added. ``show_numbers`` is false (and every
    predicted figure ``null``) only while the bedroom model is below medium confidence; there is no accuracy gate.
    503 ``{"error": "night_strategy_not_ready", "reason": ...}`` before a
    night is in view and planned (daytime, no model yet, no forecast).
    """

    url = API_NIGHT_STRATEGY
    name = "api:climate_advisor:night_strategy"
    requires_auth = True

    async def get(self, request: web.Request) -> web.Response:
        hass = request.app["hass"]
        coordinator, ambiguous_refused = _resolve_request_zone(hass, request, allow_guess=False)
        if ambiguous_refused:
            return self.json(_ambiguous_zone_payload(hass), status_code=400)
        if not coordinator:
            return self.json(_error_body(hass, "not_loaded"), status_code=503)
        with log_capture.zone_scope(coordinator.zone_label):
            payload, reason = coordinator.night_strategy_payload()
            if payload is None:
                return self.json(
                    {
                        "error": "night_strategy_not_ready",
                        "reason": reason,
                        **_coded_extra(hass, "night_strategy_not_ready"),
                    },
                    status_code=503,
                )
            return self.json(payload)


class ClimateAdvisorZonesView(HomeAssistantView):
    """GET /api/climate_advisor/zones - the loaded zones: ``{zones: [{entry_id, title}], zone_count, version}``.

    Issue #1454: the dashboards ask this, not ``status``, for the zone list on a first load (no stored zone) and after
    a failed scoped call (a startup 503), so they never send an unscoped request. Before, they sent ``GET /status``
    with no zone, which raised the "Ambiguous zone selection" Repairs card after a restart. This view names no zone by
    design and reads nothing of any zone (``zone_registry.list_zones()`` only), so it never raises the card; every
    other view still flags an unscoped request (owner's rule, Issue #1418). An empty list means Climate Advisor is
    still starting. Read-only. Kept out of ``API_VIEWS`` (in ``ZONELESS_VIEWS``) because it is the one view with no
    zone.
    """

    url = API_ZONES
    name = "api:climate_advisor:zones"
    requires_auth = True

    async def get(self, request: web.Request) -> web.Response:
        zones = zone_registry.list_zones(request.app["hass"])
        return self.json({"zones": zones, "zone_count": len(zones), "version": VERSION})


class ClimateAdvisorDeleteReportView(HomeAssistantView):
    """POST /api/climate_advisor/delete_report — delete a report by type + timestamp."""

    url = API_DELETE_REPORT
    name = "api:climate_advisor:delete_report"
    requires_auth = True

    async def post(self, request: web.Request) -> web.Response:
        hass = request.app["hass"]
        coordinator, ambiguous_refused = _get_coordinator(hass, request)
        if ambiguous_refused:
            return self.json(_ambiguous_zone_payload(hass), status_code=400)
        if not coordinator:
            return self.json(_error_body(hass, "not_loaded"), status_code=503)

        with log_capture.zone_scope(coordinator.zone_label):
            try:
                body = await request.json()
            except Exception:
                return self.json({"error": "invalid_json", **_coded_extra(hass, "invalid_json")}, status_code=400)

            report_type = body.get("report_type", "")
            timestamp = body.get("timestamp", "")
            if not timestamp:
                return self.json(
                    {"error": "missing_timestamp", **_coded_extra(hass, "missing_timestamp")}, status_code=400
                )

            if report_type == "investigation":
                found = coordinator.delete_investigation_report(timestamp)
                if found:
                    await hass.async_add_executor_job(coordinator._save_investigation_reports)
            else:
                return self.json({"error": "invalid_type", **_coded_extra(hass, "invalid_type")}, status_code=400)

            return self.json({"success": True, "found": found})


class ClimateAdvisorSubmitGithubIssueView(HomeAssistantView):
    """POST /api/climate_advisor/submit_github_issue — create a GitHub issue from a report."""

    url = API_SUBMIT_GITHUB_ISSUE
    name = "api:climate_advisor:submit_github_issue"
    requires_auth = True

    async def post(self, request: web.Request) -> web.Response:
        import aiohttp

        hass = request.app["hass"]
        coordinator, ambiguous_refused = _get_coordinator(hass, request)
        if ambiguous_refused:
            return self.json(_ambiguous_zone_payload(hass), status_code=400)
        if not coordinator:
            return self.json(_error_body(hass, "not_loaded"), status_code=503)

        with log_capture.zone_scope(coordinator.zone_label):
            token = coordinator.config.get(CONF_GITHUB_TOKEN, "")
            repo = coordinator.config.get(CONF_GITHUB_REPO, "")
            if not token or not repo:
                return self.json({"success": False, "error": "not_configured", **_coded_extra(hass, "not_configured")})

            try:
                body = await request.json()
            except Exception:
                return self.json({"error": "invalid_json", **_coded_extra(hass, "invalid_json")}, status_code=400)

            title = str(body.get("title", "")).strip()
            issue_body = str(body.get("body", "")).strip()
            labels = body.get("labels", ["bug"])

            if not title:
                return self.json({"error": "missing_title", **_coded_extra(hass, "missing_title")}, status_code=400)
            if not isinstance(labels, list):
                labels = ["bug"]

            from homeassistant.helpers.aiohttp_client import async_get_clientsession

            session = async_get_clientsession(hass)
            try:
                resp = await session.post(
                    f"https://api.github.com/repos/{repo}/issues",
                    json={"title": title, "body": issue_body, "labels": labels},
                    headers={
                        "Authorization": f"Bearer {token}",
                        "Accept": "application/vnd.github+json",
                        "X-GitHub-Api-Version": "2022-11-28",
                    },
                    timeout=aiohttp.ClientTimeout(total=15),
                )
            except Exception:
                _LOGGER.exception("GitHub issue submission: network error")
                return self.json({"success": False, "error": "network_error", **_coded_extra(hass, "network_error")})

            if resp.status == 201:
                issue = await resp.json()
                return self.json(
                    {
                        "success": True,
                        "issue_url": issue.get("html_url", ""),
                        "issue_number": issue.get("number"),
                    }
                )

            # Never log the token — log only the HTTP status
            _LOGGER.warning("GitHub issue submission: API returned status %d", resp.status)
            if resp.status == 401:
                # Issue #1643: the token was revoked or expired; ask the owner for a new one.
                from .reauth import async_request_reauth  # noqa: PLC0415

                async_request_reauth(
                    hass, hass.config_entries.async_get_entry(getattr(coordinator, "_entry_id", "")), CONF_GITHUB_TOKEN
                )
            return self.json(
                {
                    "success": False,
                    "error": f"github_api_{resp.status}",
                    **_coded_extra(hass, "github_api", status=resp.status),
                }
            )


# All views to register
API_VIEWS = [
    ClimateAdvisorStatusView,
    ClimateAdvisorBriefingView,
    ClimateAdvisorChartDataView,
    ClimateAdvisorAutomationStateView,
    ClimateAdvisorLearningView,
    ClimateAdvisorForceReclassifyView,
    ClimateAdvisorSendBriefingView,
    ClimateAdvisorRespondSuggestionView,
    ClimateAdvisorConfigView,
    ClimateAdvisorCancelOverrideView,
    ClimateAdvisorCancelFanOverrideView,
    ClimateAdvisorResumeFromPauseView,
    ClimateAdvisorToggleAutomationView,
    ClimateAdvisorAIStatusView,
    ClimateAdvisorAILimitsView,
    ClimateAdvisorActivityRecordView,
    ClimateAdvisorInvestigateView,
    ClimateAdvisorInvestigationReportsView,
    ClimateAdvisorDeleteReportView,
    ClimateAdvisorSubmitGithubIssueView,
    ClimateAdvisorEventLogView,
    ClimateAdvisorEnginesView,
    ClimateAdvisorPredictionAccuracyView,
    ClimateAdvisorTimelineView,
    ClimateAdvisorNightStrategyView,
]

# Issue #1454: views that name no zone by design (registered next to API_VIEWS; they never flag an unscoped request).
ZONELESS_VIEWS = [ClimateAdvisorZonesView]
