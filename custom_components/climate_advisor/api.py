"""REST API views for the Climate Advisor dashboard panel."""

from __future__ import annotations

import logging
from dataclasses import asdict
from datetime import timedelta

from aiohttp import web
from homeassistant.components.http import HomeAssistantView
from homeassistant.core import HomeAssistant

from .const import (
    API_AI_ACTIVITY,
    API_AI_INVESTIGATE,
    API_AI_REPORTS,
    API_AI_STATUS,
    API_AUTOMATION_STATE,
    API_BRIEFING,
    API_CANCEL_FAN_OVERRIDE,
    API_CANCEL_OVERRIDE,
    API_CHART_DATA,
    API_CONFIG,
    API_EVENT_LOG,
    API_FORCE_RECLASSIFY,
    API_INVESTIGATION_REPORTS,
    API_LEARNING,
    API_RESPOND_SUGGESTION,
    API_RESUME_FROM_PAUSE,
    API_SEND_BRIEFING,
    API_STATUS,
    API_TOGGLE_AUTOMATION,
    ATTR_AUTOMATION_STATUS,
    ATTR_COMPLIANCE_SCORE,
    ATTR_CONTACT_STATUS,
    ATTR_CURRENT_SETPOINT,
    ATTR_DAY_TYPE,
    ATTR_FAN_STATUS,
    ATTR_HVAC_ACTION,
    ATTR_HVAC_RUNTIME_TODAY,
    ATTR_INDOOR_TEMP,
    ATTR_NEXT_ACTION,
    ATTR_NEXT_AUTOMATION_ACTION,
    ATTR_NEXT_AUTOMATION_TIME,
    ATTR_TREND,
    ATTR_TREND_MAGNITUDE,
    CONF_AI_ENABLED,
    CONF_AI_INVESTIGATOR_ENABLED,
    CONFIG_METADATA,
    DEFAULT_AI_ENABLED,
    DEFAULT_AI_INVESTIGATOR_ENABLED,
    DOMAIN,
    VERSION,
)

_LOGGER = logging.getLogger(__name__)


def _get_coordinator(hass: HomeAssistant):
    """Get the first (and usually only) Climate Advisor coordinator."""
    entries = hass.data.get(DOMAIN, {})
    if not entries:
        return None
    return next(iter(entries.values()))


class ClimateAdvisorStatusView(HomeAssistantView):
    """Return current system status."""

    url = API_STATUS
    name = "api:climate_advisor:status"
    requires_auth = True

    async def get(self, request: web.Request) -> web.Response:
        hass = request.app["hass"]
        coordinator = _get_coordinator(hass)
        if not coordinator:
            return self.json({"error": "Climate Advisor not loaded"}, status_code=503)

        data = coordinator.data or {}
        ae = coordinator.automation_engine
        climate_state = hass.states.get(coordinator.config.get("climate_entity", ""))
        hvac_mode = climate_state.state if climate_state else "unknown"

        # Set point: only include when HVAC is actively running
        setpoint = None
        if climate_state and hvac_mode != "off":
            setpoint = climate_state.attributes.get("temperature")

        indoor_temp = coordinator._get_indoor_temp()

        return self.json(
            {
                "version": VERSION,
                "day_type": data.get(ATTR_DAY_TYPE, "unknown"),
                "trend_direction": data.get(ATTR_TREND, "unknown"),
                "trend_magnitude": data.get(ATTR_TREND_MAGNITUDE, 0),
                "hvac_mode": hvac_mode,
                ATTR_HVAC_ACTION: data.get(ATTR_HVAC_ACTION, ""),
                ATTR_HVAC_RUNTIME_TODAY: data.get(ATTR_HVAC_RUNTIME_TODAY, 0),
                ATTR_CURRENT_SETPOINT: setpoint,
                ATTR_INDOOR_TEMP: indoor_temp,
                "automation_status": data.get(ATTR_AUTOMATION_STATUS, "unknown"),
                "compliance_score": data.get(ATTR_COMPLIANCE_SCORE, 1.0),
                "next_action": data.get(ATTR_NEXT_ACTION, ""),
                "next_automation_action": data.get(ATTR_NEXT_AUTOMATION_ACTION, ""),
                "next_automation_time": data.get(ATTR_NEXT_AUTOMATION_TIME, ""),
                "automation_enabled": coordinator.automation_enabled,
                "occupancy_mode": coordinator._occupancy_mode,
                "fan_status": data.get(ATTR_FAN_STATUS, "disabled"),
                "contact_status": data.get(ATTR_CONTACT_STATUS, "no sensors"),
                "contact_sensors": coordinator._compute_contact_details(),
                "manual_override_active": ae._manual_override_active or ae._override_confirm_pending,
                "fan_override_active": ae._fan_override_active,
                "paused_by_door": ae.is_paused_by_door,
            }
        )


class ClimateAdvisorBriefingView(HomeAssistantView):
    """Return the current daily briefing text.

    Optional query parameter:
        verbosity: "tldr_only" | "normal" (default) | "verbose"
            Controls how much of the briefing body is returned.
            - "tldr_only": structured header + TLDR table only
            - "normal": header + TLDR table + trimmed conversational body
            - "verbose": header + TLDR table + full original body
    """

    url = API_BRIEFING
    name = "api:climate_advisor:briefing"
    requires_auth = True

    async def get(self, request: web.Request) -> web.Response:
        hass = request.app["hass"]
        coordinator = _get_coordinator(hass)
        if not coordinator:
            return self.json({"error": "Climate Advisor not loaded"}, status_code=503)

        verbosity = request.rel_url.query.get("verbosity", "normal")
        if verbosity not in ("tldr_only", "normal", "verbose"):
            return self.json(
                {"error": "verbosity must be one of: tldr_only, normal, verbose"},
                status_code=400,
            )

        briefing = coordinator._last_briefing

        # If a non-default verbosity is requested and the coordinator exposes
        # the data needed to regenerate, do so.  Otherwise return cached text.
        if verbosity != "normal" and briefing and hasattr(coordinator, "_regenerate_briefing"):
            try:
                briefing = await coordinator._regenerate_briefing(verbosity=verbosity)
            except Exception:
                _LOGGER.warning(
                    "Could not regenerate briefing for verbosity=%s; returning cached text",
                    verbosity,
                )

        return self.json(
            {
                "briefing": briefing,
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
        coordinator = _get_coordinator(hass)
        if not coordinator:
            return self.json({"error": "Climate Advisor not loaded"}, status_code=503)

        return self.json(coordinator.get_chart_data())


class ClimateAdvisorAutomationStateView(HomeAssistantView):
    """Return automation engine debug state."""

    url = API_AUTOMATION_STATE
    name = "api:climate_advisor:automation_state"
    requires_auth = True

    async def get(self, request: web.Request) -> web.Response:
        hass = request.app["hass"]
        coordinator = _get_coordinator(hass)
        if not coordinator:
            return self.json({"error": "Climate Advisor not loaded"}, status_code=503)

        return self.json(coordinator.get_debug_state())


class ClimateAdvisorLearningView(HomeAssistantView):
    """Return learning engine state and today's record."""

    url = API_LEARNING
    name = "api:climate_advisor:learning"
    requires_auth = True

    async def get(self, request: web.Request) -> web.Response:
        hass = request.app["hass"]
        coordinator = _get_coordinator(hass)
        if not coordinator:
            return self.json({"error": "Climate Advisor not loaded"}, status_code=503)

        today_record = None
        if coordinator.today_record:
            today_record = asdict(coordinator.today_record)

        suggestion_texts = coordinator.learning.generate_suggestions()
        suggestion_keys = coordinator.learning.get_last_suggestion_keys()
        suggestions = [{"key": k, "text": t} for k, t in zip(suggestion_keys, suggestion_texts, strict=False)]

        return self.json(
            {
                "today_record": today_record,
                "yesterday_record": coordinator.yesterday_record,
                "tomorrow_plan": coordinator.tomorrow_plan,
                "suggestions": suggestions,
                "compliance": coordinator.learning.get_compliance_summary(),
                "comfort_range_low": coordinator.config.get("comfort_heat", 70),
                "comfort_range_high": coordinator.config.get("comfort_cool", 75),
            }
        )


class ClimateAdvisorForceReclassifyView(HomeAssistantView):
    """Force a coordinator data refresh (reclassification)."""

    url = API_FORCE_RECLASSIFY
    name = "api:climate_advisor:force_reclassify"
    requires_auth = True

    async def post(self, request: web.Request) -> web.Response:
        hass = request.app["hass"]
        coordinator = _get_coordinator(hass)
        if not coordinator:
            return self.json({"error": "Climate Advisor not loaded"}, status_code=503)

        await coordinator.async_request_refresh()
        return self.json({"status": "ok", "message": "Reclassification triggered"})


class ClimateAdvisorSendBriefingView(HomeAssistantView):
    """Force-send the daily briefing."""

    url = API_SEND_BRIEFING
    name = "api:climate_advisor:send_briefing"
    requires_auth = True

    async def post(self, request: web.Request) -> web.Response:
        hass = request.app["hass"]
        coordinator = _get_coordinator(hass)
        if not coordinator:
            return self.json({"error": "Climate Advisor not loaded"}, status_code=503)

        coordinator._briefing_sent_today = False
        from homeassistant.util import dt as dt_util

        await coordinator._async_send_briefing(dt_util.now())
        return self.json({"status": "ok", "message": "Briefing sent"})


class ClimateAdvisorRespondSuggestionView(HomeAssistantView):
    """Accept or dismiss a learning suggestion."""

    url = API_RESPOND_SUGGESTION
    name = "api:climate_advisor:respond_suggestion"
    requires_auth = True

    async def post(self, request: web.Request) -> web.Response:
        hass = request.app["hass"]
        coordinator = _get_coordinator(hass)
        if not coordinator:
            return self.json({"error": "Climate Advisor not loaded"}, status_code=503)

        try:
            body = await request.json()
        except Exception:
            return self.json({"error": "Invalid JSON body"}, status_code=400)

        action = body.get("action")
        suggestion_key = body.get("suggestion_key")
        feedback = body.get("feedback")

        # Validate feedback if present
        if feedback is not None and feedback not in ("correct", "incorrect"):
            return self.json(
                {"error": "feedback must be 'correct' or 'incorrect'"},
                status_code=400,
            )

        # suggestion_key is required; action is required only when feedback is absent
        if not suggestion_key:
            return self.json(
                {"error": "Required: suggestion_key"},
                status_code=400,
            )
        if action is None and feedback is None:
            return self.json(
                {"error": "Required: action (accept/dismiss) or feedback (correct/incorrect)"},
                status_code=400,
            )
        if action is not None and action not in ("accept", "dismiss"):
            return self.json(
                {"error": "action must be 'accept' or 'dismiss'"},
                status_code=400,
            )

        # Handle feedback recording (independent of action)
        if feedback is not None:
            coordinator.learning.record_feedback(suggestion_key, feedback)
            await hass.async_add_executor_job(coordinator.learning.save_state)

        # Handle accept/dismiss action
        if action == "accept":
            changes = coordinator.learning.accept_suggestion(suggestion_key)
            await hass.async_add_executor_job(coordinator.learning.save_state)
            coordinator.config.update(changes)
            # Persist valid config keys to the config entry so changes survive reload
            valid_keys = set(CONFIG_METADATA.keys())
            entry_changes = {k: v for k, v in changes.items() if k in valid_keys}
            if entry_changes:
                entries = hass.data.get(DOMAIN, {})
                entry_id = next((eid for eid, c in entries.items() if c is coordinator), None)
                if entry_id:
                    config_entry = hass.config_entries.async_get_entry(entry_id)
                    if config_entry:
                        hass.config_entries.async_update_entry(
                            config_entry,
                            data={**config_entry.data, **entry_changes},
                        )
            return self.json({"status": "ok", "changes": changes})
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
        coordinator = _get_coordinator(hass)
        if not coordinator:
            return self.json({"error": "Climate Advisor not loaded"}, status_code=503)

        config = coordinator.config
        settings = []

        for key, meta in CONFIG_METADATA.items():
            value = config.get(key)
            # Sanitize: replace notify service names (may reveal personal info)
            if key == "notify_service" or meta.get("sensitive"):
                value = "configured" if value else "not set"
            # Convert time objects to strings
            if hasattr(value, "strftime"):
                value = str(value)
            # Convert lists to counts for display
            if isinstance(value, list):
                value = f"{len(value)} configured"
            # Apply display transforms (e.g., seconds → minutes for UI)
            transform = meta.get("display_transform")
            if transform == "seconds_to_minutes" and isinstance(value, (int, float)):
                value = value // 60

            settings.append(
                {
                    "key": key,
                    "value": value,
                    "label": meta["label"],
                    "description": meta["description"],
                    "category": meta["category"],
                }
            )

        return self.json({"settings": settings})


class ClimateAdvisorCancelOverrideView(HomeAssistantView):
    """Cancel manual override and resume automated HVAC control."""

    url = API_CANCEL_OVERRIDE
    name = "api:climate_advisor:cancel_override"
    requires_auth = True

    async def post(self, request: web.Request) -> web.Response:
        hass = request.app["hass"]
        coordinator = _get_coordinator(hass)
        if not coordinator:
            return self.json({"error": "Climate Advisor not loaded"}, status_code=503)

        ae = coordinator.automation_engine
        if not ae._manual_override_active:
            return self.json({"status": "ok", "message": "No active override to cancel"})

        # Clear override and cancel grace timers
        ae.clear_manual_override()
        ae._cancel_grace_timers()

        # Schedule re-application of current classification after 10 seconds
        from homeassistant.core import callback
        from homeassistant.helpers.event import async_call_later

        @callback
        def _apply_after_delay(_now):
            if coordinator._current_classification:
                hass.async_create_task(ae.apply_classification(coordinator._current_classification))

        async_call_later(hass, 10, _apply_after_delay)

        return self.json(
            {
                "status": "ok",
                "message": "Override cancelled. Automated control resumes in 10 seconds.",
            }
        )


class ClimateAdvisorResumeFromPauseView(HomeAssistantView):
    """Resume HVAC from a contact sensor pause (user override)."""

    url = API_RESUME_FROM_PAUSE
    name = "api:climate_advisor:resume_from_pause"
    requires_auth = True

    async def post(self, request: web.Request) -> web.Response:
        hass = request.app["hass"]
        coordinator = _get_coordinator(hass)
        if not coordinator:
            return self.json({"error": "Climate Advisor not loaded"}, status_code=503)

        ae = coordinator.automation_engine
        if not ae.is_paused_by_door:
            return self.json({"status": "ok", "message": "Not currently paused"})

        restored_mode = await ae.resume_from_pause()
        return self.json(
            {
                "status": "ok",
                "message": f"Resumed from pause. HVAC set to {restored_mode or 'N/A'}. Manual grace period started.",
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
        coordinator = _get_coordinator(hass)
        if not coordinator:
            return self.json({"error": "Climate Advisor not loaded"}, status_code=503)

        ae = coordinator.automation_engine
        ae.clear_fan_override()
        return self.json({"status": "ok", "message": "Fan override cleared."})


class ClimateAdvisorToggleAutomationView(HomeAssistantView):
    """Toggle automation enabled/disabled state."""

    url = API_TOGGLE_AUTOMATION
    name = "api:climate_advisor:toggle_automation"
    requires_auth = True

    async def post(self, request: web.Request) -> web.Response:
        hass = request.app["hass"]
        coordinator = _get_coordinator(hass)
        if not coordinator:
            return self.json({"error": "Climate Advisor not loaded"}, status_code=503)

        new_state = not coordinator.automation_enabled
        coordinator.set_automation_enabled(new_state)

        return self.json(
            {
                "status": "ok",
                "automation_enabled": new_state,
                "message": f"Automation {'enabled' if new_state else 'disabled'}.",
            }
        )


class ClimateAdvisorAIStatusView(HomeAssistantView):
    """API endpoint for AI status and recent request history."""

    url = API_AI_STATUS
    name = "api:climate_advisor:ai_status"
    requires_auth = True

    async def get(self, request: web.Request) -> web.Response:
        hass = request.app["hass"]
        coordinator = _get_coordinator(hass)
        if not coordinator:
            return self.json({"error": "Climate Advisor not loaded"}, status_code=503)

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
                "status": {"status": "disabled"},
                "recent_requests": [],
            }
        )


class ClimateAdvisorAIActivityView(HomeAssistantView):
    """API endpoint to execute the AI activity report skill."""

    url = API_AI_ACTIVITY
    name = "api:climate_advisor:ai_activity"
    requires_auth = True

    async def post(self, request: web.Request) -> web.Response:
        hass = request.app["hass"]
        coordinator = _get_coordinator(hass)
        if not coordinator:
            return self.json({"error": "Climate Advisor not loaded"}, status_code=503)

        if not coordinator.claude_client or not coordinator.ai_skills:
            return self.json({"error": "AI features are disabled"}, status_code=503)

        try:
            data = await request.json()
        except Exception:
            data = {}

        hours = data.get("hours", 24)
        detail_level = data.get("detail_level", "full")

        # Validate inputs from REST API (service call path uses vol.Schema)
        if not isinstance(hours, (int, float)) or not (1 <= hours <= 168):
            hours = 24
        if detail_level not in ("brief", "full"):
            detail_level = "full"

        result = await coordinator.ai_skills.async_execute(
            "activity_report",
            coordinator.hass,
            coordinator,
            coordinator.claude_client,
            hours=hours,
            detail_level=detail_level,
        )

        # Store the result for history
        await coordinator.async_store_ai_report(result)

        if result.get("rate_limited") or (not result.get("success") and "rate" in str(result.get("error", "")).lower()):
            return self.json({"error": "Rate limit exceeded"}, status_code=429)

        return self.json(result)


class ClimateAdvisorAIReportsView(HomeAssistantView):
    """API endpoint for persisted AI report history."""

    url = API_AI_REPORTS
    name = "api:climate_advisor:ai_reports"
    requires_auth = True

    async def get(self, request: web.Request) -> web.Response:
        hass = request.app["hass"]
        coordinator = _get_coordinator(hass)
        if not coordinator:
            return self.json({"error": "Climate Advisor not loaded"}, status_code=503)

        return self.json(
            {
                "reports": coordinator.get_ai_report_history(),
            }
        )


class ClimateAdvisorInvestigateView(HomeAssistantView):
    """POST /api/climate_advisor/ai_investigate — run an investigation."""

    url = API_AI_INVESTIGATE
    name = "api:climate_advisor:ai_investigate"
    requires_auth = True

    async def post(self, request: web.Request) -> web.Response:
        hass = request.app["hass"]
        coordinator = _get_coordinator(hass)
        if not coordinator:
            return self.json({"error": "Climate Advisor not loaded"}, status_code=503)

        # Parse optional JSON body — body may be absent
        try:
            body = await request.json()
        except Exception:
            body = {}

        focus: str = str(body.get("focus", ""))
        try:
            hours: int = max(1, min(int(body.get("hours", 48)), 168))
        except (ValueError, TypeError):
            hours = 48

        if not coordinator.config.get(CONF_AI_ENABLED, DEFAULT_AI_ENABLED):
            return self.json_message("AI features are not enabled", status_code=403)

        if not coordinator.config.get(CONF_AI_INVESTIGATOR_ENABLED, DEFAULT_AI_INVESTIGATOR_ENABLED):
            return self.json_message("Investigative agent is not enabled", status_code=403)

        if coordinator.claude_client is None:
            return self.json_message("AI client not available", status_code=503)

        allowed, reason = coordinator.claude_client.check_investigator_rate_limit()
        if not allowed:
            return self.json_message(reason, status_code=429)

        _LOGGER.info(
            "Investigation requested: focus_len=%d hours=%d",
            len(focus),
            hours,
        )

        result = await coordinator.ai_skills.async_execute(
            "investigator",
            hass,
            coordinator,
            coordinator.claude_client,
            focus=focus,
            hours=hours,
        )

        if result.get("success") or result.get("source") == "fallback":
            coordinator.claude_client.increment_investigator_counter()
            await coordinator.async_store_investigation_report(result)
            _LOGGER.info("Investigation complete: source=%s", result.get("source", "unknown"))
            return self.json(result)

        return self.json_message(result.get("error", "Investigation failed"), status_code=500)


class ClimateAdvisorInvestigationReportsView(HomeAssistantView):
    """GET /api/climate_advisor/investigation_reports — list investigation history."""

    url = API_INVESTIGATION_REPORTS
    name = "api:climate_advisor:investigation_reports"
    requires_auth = True

    async def get(self, request: web.Request) -> web.Response:
        hass = request.app["hass"]
        coordinator = _get_coordinator(hass)
        if not coordinator:
            return self.json({"error": "Climate Advisor not loaded"}, status_code=503)

        return self.json(coordinator.get_investigation_report_history())


class ClimateAdvisorEventLogView(HomeAssistantView):
    """Return the in-memory automation event log for the requested time window (Issue #76)."""

    url = API_EVENT_LOG
    name = "api:climate_advisor:event_log"
    requires_auth = True

    async def get(self, request: web.Request) -> web.Response:
        from homeassistant.util import dt as dt_util

        hass = request.app["hass"]
        coordinator = _get_coordinator(hass)
        if not coordinator:
            return self.json({"error": "Climate Advisor not loaded"}, status_code=503)

        try:
            hours = float(request.rel_url.query.get("hours", "24"))
            hours = max(0.5, min(hours, 168))  # clamp: 30 min – 7 days
        except (ValueError, TypeError):
            hours = 24.0

        cutoff = (dt_util.now() - timedelta(hours=hours)).isoformat()
        events = [e for e in coordinator._event_log if e.get("time", "") >= cutoff]

        return self.json({"events": events, "total": len(events), "hours": hours})


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
    ClimateAdvisorAIActivityView,
    ClimateAdvisorAIReportsView,
    ClimateAdvisorInvestigateView,
    ClimateAdvisorInvestigationReportsView,
    ClimateAdvisorEventLogView,
]
