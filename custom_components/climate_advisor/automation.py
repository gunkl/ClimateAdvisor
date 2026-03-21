"""Automation engine for Climate Advisor.

Manages the creation and dynamic adjustment of Home Assistant automations
based on the day classification and learning state.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.event import async_call_later
from homeassistant.util import dt as dt_util

from .classifier import DayClassification
from .const import (
    CONF_AUTOMATION_GRACE_NOTIFY,
    CONF_AUTOMATION_GRACE_PERIOD,
    CONF_EMAIL_NOTIFY,
    CONF_FAN_ENTITY,
    CONF_FAN_MODE,
    CONF_MANUAL_GRACE_NOTIFY,
    CONF_MANUAL_GRACE_PERIOD,
    CONF_SENSOR_DEBOUNCE,
    DAY_TYPE_HOT,
    DEFAULT_AUTOMATION_GRACE_SECONDS,
    DEFAULT_MANUAL_GRACE_SECONDS,
    DEFAULT_SENSOR_DEBOUNCE_SECONDS,
    ECONOMIZER_EVENING_END_HOUR,
    ECONOMIZER_EVENING_START_HOUR,
    ECONOMIZER_MORNING_END_HOUR,
    ECONOMIZER_MORNING_START_HOUR,
    ECONOMIZER_TEMP_DELTA,
    FAN_MODE_BOTH,
    FAN_MODE_DISABLED,
    FAN_MODE_HVAC,
    FAN_MODE_WHOLE_HOUSE,
    REVISIT_DELAY_SECONDS,
    VACATION_SETBACK_EXTRA,
)

_LOGGER = logging.getLogger(__name__)


class AutomationEngine:
    """Manages HVAC automations based on daily classification."""

    def __init__(
        self,
        hass: HomeAssistant,
        climate_entity: str,
        weather_entity: str,
        door_window_sensors: list[str],
        notify_service: str,
        config: dict[str, Any],
        sensor_polarity_inverted: bool = False,
    ) -> None:
        """Initialize the automation engine."""
        self.hass = hass
        self.climate_entity = climate_entity
        self.weather_entity = weather_entity
        self.door_window_sensors = door_window_sensors
        self.notify_service = notify_service
        self.config = config
        self.sensor_polarity_inverted = sensor_polarity_inverted
        self._active_listeners: list[Any] = []
        self._current_classification: DayClassification | None = None
        self._paused_by_door = False
        self._pre_pause_mode: str | None = None

        # Dry-run mode: when True, all service calls are logged but skipped
        self.dry_run: bool = False

        # Grace period state
        self._manual_grace_cancel: Any | None = None
        self._automation_grace_cancel: Any | None = None
        self._grace_active = False
        self._last_resume_source: str | None = None

        # Economizer state (two-phase window cooling per Issue #27)
        # Phase "cool-down": AC runs to cool to set temp (outdoor air assists)
        # Phase "maintain": AC off, natural ventilation holds temp
        self._economizer_active: bool = False
        self._economizer_phase: str = "inactive"  # "inactive", "cool-down", "maintain"

        # Action tracking (Issue #37)
        self._last_action_time: str | None = None
        self._last_action_reason: str | None = None

        # Revisit scheduling — 5-min follow-up after any HVAC action
        self._revisit_cancel: Any | None = None
        self._revisit_callback: Any | None = None  # Set by coordinator

        # Manual override protection — prevents classification from
        # overriding user's manual thermostat changes
        self._manual_override_active: bool = False
        self._manual_override_mode: str | None = None
        self._manual_override_time: str | None = None

        # Fan state tracking (Issue #37)
        self._fan_active: bool = False
        self._fan_on_since: str | None = None  # ISO timestamp
        self._fan_override_active: bool = False
        self._fan_override_time: str | None = None
        self._fan_command_pending: bool = False  # transient: distinguishes integration vs manual changes
        self._hvac_command_pending: bool = False  # transient: distinguishes integration vs manual HVAC changes
        self._hvac_command_time: datetime | None = None  # last system-initiated HVAC command timestamp

        # Resume-from-pause tracking (Issue #47)
        self._resumed_from_pause: bool = False
        self._sensor_check_callback: Any | None = None  # Set by coordinator: returns True if any sensor open

    async def _notify(self, message: str, title: str) -> None:
        """Send a notification via the configured service, plus email if enabled."""
        if self.dry_run:
            _LOGGER.info("[DRY RUN] Would send notification: %s — %s", title, message)
            return
        service_name = self.notify_service.split(".")[-1] if "." in self.notify_service else self.notify_service
        await self.hass.services.async_call("notify", service_name, {"message": message, "title": title})
        if self.config.get(CONF_EMAIL_NOTIFY, True):
            await self.hass.services.async_call("notify", "send_email", {"message": message, "title": title})

    @property
    def is_paused_by_door(self) -> bool:
        """Whether HVAC is currently paused due to an open door/window."""
        return self._paused_by_door

    def _record_action(self, action: str, reason: str) -> None:
        """Record an HVAC action with timestamp and reason, and schedule a revisit."""
        self._last_action_time = dt_util.now().isoformat()
        self._last_action_reason = f"{action} — {reason}"
        _LOGGER.warning("Action recorded: %s", self._last_action_reason)
        self._schedule_revisit()

    def _schedule_revisit(self) -> None:
        """Schedule a follow-up re-evaluation after an HVAC action."""
        if self._revisit_cancel:
            self._revisit_cancel()
            self._revisit_cancel = None

        if not self._revisit_callback:
            return

        revisit_cb = self._revisit_callback

        @callback
        def _revisit_fired(_now: Any) -> None:
            self._revisit_cancel = None
            _LOGGER.info("Revisit check triggered (5-min follow-up after action)")
            self.hass.async_create_task(revisit_cb())

        self._revisit_cancel = async_call_later(self.hass, REVISIT_DELAY_SECONDS, _revisit_fired)

    def clear_manual_override(self) -> None:
        """Clear the manual override flag (called at transition points)."""
        if self._manual_override_active:
            _LOGGER.info(
                "Clearing manual override (was %s since %s)",
                self._manual_override_mode,
                self._manual_override_time,
            )
            self._manual_override_active = False
            self._manual_override_mode = None
            self._manual_override_time = None
        self._resumed_from_pause = False
        self.clear_fan_override()

    def _get_fan_runtime_minutes(self) -> float:
        """Return how many minutes the fan has been running, or 0.0 if inactive."""
        if not self._fan_active or not self._fan_on_since:
            return 0.0
        try:
            from datetime import datetime as _dt_cls

            on_since = _dt_cls.fromisoformat(self._fan_on_since)
            if on_since.tzinfo is None:
                on_since = on_since.replace(tzinfo=UTC)
            now = dt_util.now()
            if not isinstance(now, _dt_cls):
                return 0.0
            if now.tzinfo is None:
                now = now.replace(tzinfo=UTC)
            delta = (now - on_since).total_seconds() / 60.0
            return max(0.0, delta)
        except Exception:
            return 0.0

    def handle_fan_manual_override(self) -> None:
        """Handle a manual fan state change — sets fan override flag + grace."""
        self._fan_override_active = True
        self._fan_override_time = dt_util.now().isoformat()
        _LOGGER.warning(
            "Fan manual override activated at %s",
            self._fan_override_time,
        )
        self._start_grace_period("manual")

    def clear_fan_override(self) -> None:
        """Clear the fan override flag (called at transition points)."""
        if self._fan_override_active:
            _LOGGER.info(
                "Clearing fan manual override (since %s)",
                self._fan_override_time,
            )
            self._fan_override_active = False
            self._fan_override_time = None

    def handle_manual_override(self) -> None:
        """Handle a manual thermostat change (outside of door/window pause).

        Sets the manual override flag and starts a grace period so that
        classification does not fight the user's intent.
        """
        state = self.hass.states.get(self.climate_entity)
        self._manual_override_active = True
        self._manual_override_mode = state.state if state else "unknown"
        self._manual_override_time = dt_util.now().isoformat()
        _LOGGER.warning(
            "Manual override activated: mode=%s",
            self._manual_override_mode,
        )
        self._start_grace_period("manual")

    async def apply_classification(self, classification: DayClassification) -> None:
        """Apply a new day classification — adjust HVAC behavior accordingly.

        This is called once in the morning and can be called again if
        conditions change significantly mid-day.
        """
        self._current_classification = classification

        if self._manual_override_active:
            _LOGGER.info(
                "Manual override active (mode=%s since %s) — skipping HVAC mode change",
                self._manual_override_mode,
                self._manual_override_time,
            )
            return

        _LOGGER.warning(
            "Applying classification: %s (trend: %s %s°F)",
            classification.day_type,
            classification.trend_direction,
            classification.trend_magnitude,
        )

        # Set the base HVAC mode
        cls_reason = (
            f"daily classification — {classification.day_type} day,"
            f" trend {classification.trend_direction} {classification.trend_magnitude}°F"
        )
        if classification.hvac_mode in ("heat", "cool"):
            await self._set_hvac_mode(classification.hvac_mode, reason=cls_reason)
            await self._set_temperature_for_mode(classification, reason=cls_reason)
        elif classification.hvac_mode == "off":
            await self._set_hvac_mode(
                "off",
                reason=f"daily classification — {classification.day_type} day, HVAC not needed",
            )

        # Handle pre-conditioning
        if classification.pre_condition and classification.pre_condition_target:
            await self._schedule_pre_condition(classification)

    async def _set_hvac_mode(self, mode: str, *, reason: str) -> None:
        """Set the thermostat HVAC mode."""
        if self.dry_run:
            _LOGGER.info("[DRY RUN] Would set HVAC mode to %s — %s", mode, reason)
            return
        self._hvac_command_pending = True
        self._hvac_command_time = dt_util.now()
        try:
            await self.hass.services.async_call(
                "climate",
                "set_hvac_mode",
                {"entity_id": self.climate_entity, "hvac_mode": mode},
            )
            _LOGGER.warning("Set HVAC mode to %s — %s", mode, reason)
            self._record_action(f"Set HVAC to {mode}", reason)
        finally:
            self._hvac_command_pending = False

    async def _set_temperature(self, temperature: float, *, reason: str) -> None:
        """Set the thermostat target temperature."""
        if self.dry_run:
            _LOGGER.info("[DRY RUN] Would set temperature to %s°F — %s", temperature, reason)
            return
        await self.hass.services.async_call(
            "climate",
            "set_temperature",
            {"entity_id": self.climate_entity, "temperature": temperature},
        )
        _LOGGER.warning("Set temperature to %s°F — %s", temperature, reason)
        self._record_action(f"Set temp to {temperature}°F", reason)

    async def _set_temperature_for_mode(self, c: DayClassification, *, reason: str) -> None:
        """Set temperature based on the classification and current period."""
        if c.hvac_mode == "heat":
            target = self.config["comfort_heat"]
        elif c.hvac_mode == "cool":
            target = self.config["comfort_cool"]
            if c.pre_condition and c.pre_condition_target and c.pre_condition_target < 0:
                # Pre-cool: target is below comfort
                target = target + c.pre_condition_target
                reason = f"{reason} (pre-cool offset {c.pre_condition_target}°F)"
        else:
            return

        await self._set_temperature(target, reason=reason)

    async def _schedule_pre_condition(self, c: DayClassification) -> None:
        """Schedule pre-heating or pre-cooling based on trend.

        For warming trends: more aggressive setback (handled by setback_modifier)
        For cooling trends: pre-heat in the evening
        """
        if c.trend_direction == "cooling" and c.pre_condition_target and c.pre_condition_target > 0:
            # Pre-heat: schedule a bump for 7pm
            preheat_target = self.config["comfort_heat"] + c.pre_condition_target
            _LOGGER.info(
                "Scheduling pre-heat to %s°F for this evening (cold front coming)",
                preheat_target,
            )
            # In a full implementation, this would register a time-based listener
            # For now, store the intent for the coordinator to act on
            self.config["_pending_preheat"] = {
                "time": "19:00",
                "target": preheat_target,
                "duration_hours": 2,
            }

    async def handle_door_window_open(self, entity_id: str) -> None:
        """Handle a door/window being opened for longer than the debounce period.

        Called by the coordinator after the debounce period.
        """
        if self._paused_by_door:
            return  # Already paused

        if self._grace_active:
            _LOGGER.info(
                "Door/window open (%s) but %s grace period active — not pausing",
                entity_id,
                self._last_resume_source,
            )
            return

        # Get current mode before pausing
        state = self.hass.states.get(self.climate_entity)
        if state:
            self._pre_pause_mode = state.state

        if self._pre_pause_mode and self._pre_pause_mode != "off":
            self._paused_by_door = True
            await self._set_hvac_mode(
                "off",
                reason=f"door/window open — {entity_id}, was {self._pre_pause_mode} mode",
            )

            # Notify
            debounce_minutes = self.config.get(CONF_SENSOR_DEBOUNCE, DEFAULT_SENSOR_DEBOUNCE_SECONDS) // 60
            friendly_name = entity_id.split(".")[-1].replace("_", " ").title()
            await self._notify(
                f"🚪 HVAC paused — {friendly_name} has been open for "
                f"{debounce_minutes} minutes. "
                f"Heating/cooling will resume when it's closed.",
                "Climate Advisor",
            )

    async def handle_all_doors_windows_closed(self) -> None:
        """Resume HVAC after all monitored doors/windows are closed."""
        if not self._paused_by_door:
            return

        self._paused_by_door = False
        if self._pre_pause_mode:
            await self._set_hvac_mode(
                self._pre_pause_mode,
                reason=f"door/window closed — restoring {self._pre_pause_mode} mode",
            )
            if self._current_classification:
                await self._set_temperature_for_mode(
                    self._current_classification,
                    reason="door/window closed — restoring comfort",
                )
            self._start_grace_period("automation")
        self._pre_pause_mode = None

    async def handle_manual_override_during_pause(self) -> None:
        """Handle when user manually turns HVAC on during a sensor pause.

        Called by the coordinator when it detects a thermostat mode change
        from 'off' to something else while paused_by_door is True.
        """
        if not self._paused_by_door:
            return
        _LOGGER.info("Manual HVAC override detected during door/window pause")
        self._paused_by_door = False
        self._pre_pause_mode = None
        # Record the manual override so classification respects user intent
        state = self.hass.states.get(self.climate_entity)
        self._manual_override_active = True
        self._manual_override_mode = state.state if state else "unknown"
        self._manual_override_time = dt_util.now().isoformat()
        self._start_grace_period("manual")

    async def resume_from_pause(self) -> str | None:
        """Resume HVAC from contact sensor pause (user-initiated via dashboard).

        Clears the pause, restores the current classification's HVAC mode
        (not pre_pause_mode, since classification may have changed), and
        starts a manual override grace period to prevent immediate re-pause.

        Returns the restored mode string, or None if not currently paused.
        """
        if not self._paused_by_door:
            return None

        _LOGGER.info("User resumed HVAC from door/window pause via dashboard")
        self._paused_by_door = False
        self._pre_pause_mode = None
        self._resumed_from_pause = True

        restore_mode = None
        if self._current_classification:
            restore_mode = self._current_classification.hvac_mode
            if restore_mode and restore_mode != "off":
                await self._set_hvac_mode(
                    restore_mode,
                    reason="user resumed from door/window pause",
                )
                await self._set_temperature_for_mode(
                    self._current_classification,
                    reason="user resumed from door/window pause",
                )

        self._start_grace_period("manual")
        return restore_mode

    def _start_grace_period(self, source: str) -> None:
        """Start a grace period after HVAC is resumed.

        Args:
            source: "manual" for user-initiated overrides,
                    "automation" for Climate Advisor resumptions.
        """
        self._cancel_grace_timers()

        if source == "manual":
            duration = self.config.get(CONF_MANUAL_GRACE_PERIOD, DEFAULT_MANUAL_GRACE_SECONDS)
            should_notify = self.config.get(CONF_MANUAL_GRACE_NOTIFY, False)
        else:
            duration = self.config.get(CONF_AUTOMATION_GRACE_PERIOD, DEFAULT_AUTOMATION_GRACE_SECONDS)
            should_notify = self.config.get(CONF_AUTOMATION_GRACE_NOTIFY, True)

        if duration <= 0:
            return  # Grace period disabled

        self._grace_active = True
        self._last_resume_source = source

        @callback
        def _grace_expired(_now: Any) -> None:
            """Grace period has elapsed — re-check sensors before clearing."""
            # If any contact sensor is still open, re-pause instead of clearing
            if self._sensor_check_callback and self._sensor_check_callback():
                _LOGGER.info(
                    "%s grace expired but sensor(s) still open — re-pausing HVAC",
                    source,
                )
                self._grace_active = False
                self._last_resume_source = None
                self._manual_grace_cancel = None
                self._automation_grace_cancel = None
                self.clear_manual_override()
                self.hass.async_create_task(self._re_pause_for_open_sensor())
                return

            self._grace_active = False
            self._last_resume_source = None
            self._manual_grace_cancel = None
            self._automation_grace_cancel = None
            self.clear_manual_override()
            _LOGGER.info("%s grace period expired (%d seconds)", source, duration)

            if should_notify:
                self.hass.async_create_task(
                    self._notify(
                        f"⏱️ {source.capitalize()} grace period expired "
                        f"({duration // 60} minutes). HVAC will now respond "
                        f"normally to door/window sensor changes.",
                        "Climate Advisor",
                    )
                )

        cancel = async_call_later(self.hass, duration, _grace_expired)
        if source == "manual":
            self._manual_grace_cancel = cancel
        else:
            self._automation_grace_cancel = cancel

        _LOGGER.info("Started %s grace period (%d seconds)", source, duration)

    def _cancel_grace_timers(self) -> None:
        """Cancel any active grace period timers."""
        if self._manual_grace_cancel:
            self._manual_grace_cancel()
            self._manual_grace_cancel = None
        if self._automation_grace_cancel:
            self._automation_grace_cancel()
            self._automation_grace_cancel = None
        self._grace_active = False
        self._last_resume_source = None

    async def _re_pause_for_open_sensor(self) -> None:
        """Re-pause HVAC because a sensor is still open when grace expired."""
        state = self.hass.states.get(self.climate_entity)
        if state and state.state not in ("off", "unavailable", "unknown"):
            self._pre_pause_mode = state.state
            self._paused_by_door = True
            await self._set_hvac_mode(
                "off",
                reason="grace expired — door/window still open, re-pausing",
            )
            await self._notify(
                "Grace period expired but a door/window is still open. HVAC has been paused again.",
                "Climate Advisor",
            )
        elif state and state.state == "off":
            # HVAC already off, just set the pause flag
            self._paused_by_door = True

    async def handle_occupancy_away(self) -> None:
        """Handle everyone leaving — apply setback."""
        c = self._current_classification
        if not c:
            _LOGGER.warning("Occupancy away handler skipped — no day classification available")
            return

        if c.hvac_mode == "heat":
            setback = self.config["setback_heat"] + c.setback_modifier
            await self._set_temperature(
                setback,
                reason=(
                    f"occupancy away — heat setback"
                    f" (base {self.config['setback_heat']} + modifier {c.setback_modifier})"
                ),
            )
        elif c.hvac_mode == "cool":
            setback = self.config["setback_cool"] - c.setback_modifier
            await self._set_temperature(
                setback,
                reason=(
                    f"occupancy away — cool setback"
                    f" (base {self.config['setback_cool']} - modifier {c.setback_modifier})"
                ),
            )
        else:
            _LOGGER.info(
                "Occupancy away — HVAC mode is '%s', no setback needed",
                c.hvac_mode,
            )

    async def handle_occupancy_home(self) -> None:
        """Handle someone returning — restore comfort."""
        c = self._current_classification
        if not c:
            return

        if c.hvac_mode in ("heat", "cool"):
            await self._set_temperature_for_mode(c, reason=f"occupancy home — restoring {c.hvac_mode} comfort")

        # Notify with estimated recovery time
        await self._notify(
            "🏠 Welcome home! Restoring comfort temperature. Should feel normal in about 20–30 minutes.",
            "Climate Advisor",
        )

    async def handle_occupancy_vacation(self) -> None:
        """Handle vacation mode — apply deeper setback for extended away."""
        c = self._current_classification
        if not c:
            return

        if c.hvac_mode == "heat":
            setback = self.config["setback_heat"] + c.setback_modifier - VACATION_SETBACK_EXTRA
            await self._set_temperature(
                setback,
                reason=(
                    f"vacation mode — deep heat setback"
                    f" (base {self.config['setback_heat']} + modifier {c.setback_modifier}"
                    f" - vacation {VACATION_SETBACK_EXTRA})"
                ),
            )
        elif c.hvac_mode == "cool":
            setback = self.config["setback_cool"] - c.setback_modifier + VACATION_SETBACK_EXTRA
            await self._set_temperature(
                setback,
                reason=(
                    f"vacation mode — deep cool setback"
                    f" (base {self.config['setback_cool']} - modifier {c.setback_modifier}"
                    f" + vacation {VACATION_SETBACK_EXTRA})"
                ),
            )

    async def handle_bedtime(self) -> None:
        """Apply bedtime setback."""
        self.clear_manual_override()

        # Deactivate fan at bedtime (fan running overnight is noisy/wasteful)
        if self._fan_active and not self._fan_override_active:
            await self._deactivate_fan(reason="bedtime — fan off for night")
        if self._economizer_active:
            await self._deactivate_economizer(outdoor_temp=0)

        c = self._current_classification
        if not c:
            return

        if c.hvac_mode == "heat":
            bedtime_target = self.config["comfort_heat"] - 4 + c.setback_modifier
            await self._set_temperature(
                bedtime_target,
                reason=(
                    f"bedtime — heat setback"
                    f" (comfort {self.config['comfort_heat']} - 4 + modifier {c.setback_modifier})"
                ),
            )
        elif c.hvac_mode == "cool":
            bedtime_target = self.config["comfort_cool"] + 3
            await self._set_temperature(
                bedtime_target,
                reason="bedtime — cool setback (comfort {} + 3)".format(self.config["comfort_cool"]),
            )

    async def handle_morning_wakeup(self) -> None:
        """Restore comfort for morning wake-up."""
        self.clear_manual_override()

        # Deactivate fan if still running from overnight
        if self._fan_active:
            await self._deactivate_fan(reason="morning wakeup — resetting fan state")

        c = self._current_classification
        if not c:
            return

        if c.hvac_mode == "heat":
            await self._set_temperature(
                self.config["comfort_heat"],
                reason="morning wake-up — restoring heat comfort",
            )
        elif c.hvac_mode == "cool":
            await self._set_temperature(
                self.config["comfort_cool"],
                reason="morning wake-up — restoring cool comfort",
            )

    async def _activate_fan(self, *, reason: str) -> None:
        """Activate fan based on configured fan_mode."""
        fan_mode = self.config.get(CONF_FAN_MODE, FAN_MODE_DISABLED)
        if fan_mode == FAN_MODE_DISABLED:
            return

        if self._fan_override_active:
            _LOGGER.info("Fan override active — skipping fan activation")
            return

        if self.dry_run:
            _LOGGER.info("[DRY RUN] Would activate fan — %s", reason)
            return

        self._fan_command_pending = True
        try:
            if fan_mode in (FAN_MODE_WHOLE_HOUSE, FAN_MODE_BOTH):
                fan_entity = self.config.get(CONF_FAN_ENTITY)
                if fan_entity:
                    domain = fan_entity.split(".")[0]  # "fan" or "switch"
                    await self.hass.services.async_call(domain, "turn_on", {"entity_id": fan_entity})
                    _LOGGER.warning("Activated %s fan (%s) — %s", domain, fan_entity, reason)

            if fan_mode in (FAN_MODE_HVAC, FAN_MODE_BOTH):
                await self.hass.services.async_call(
                    "climate",
                    "set_fan_mode",
                    {"entity_id": self.climate_entity, "fan_mode": "on"},
                )
                _LOGGER.warning("Activated HVAC fan — %s", reason)

            self._fan_active = True
            self._fan_on_since = dt_util.now().isoformat()
            self._record_action("Fan activated", reason)
        finally:
            self._fan_command_pending = False

    async def _deactivate_fan(self, *, reason: str) -> None:
        """Deactivate fan based on configured fan_mode."""
        fan_mode = self.config.get(CONF_FAN_MODE, FAN_MODE_DISABLED)
        if fan_mode == FAN_MODE_DISABLED:
            return

        if self._fan_override_active:
            _LOGGER.info("Fan override active — skipping fan deactivation")
            return

        if self.dry_run:
            _LOGGER.info("[DRY RUN] Would deactivate fan — %s", reason)
            return

        self._fan_command_pending = True
        try:
            if fan_mode in (FAN_MODE_WHOLE_HOUSE, FAN_MODE_BOTH):
                fan_entity = self.config.get(CONF_FAN_ENTITY)
                if fan_entity:
                    domain = fan_entity.split(".")[0]
                    await self.hass.services.async_call(domain, "turn_off", {"entity_id": fan_entity})
                    _LOGGER.warning("Deactivated %s fan (%s) — %s", domain, fan_entity, reason)

            if fan_mode in (FAN_MODE_HVAC, FAN_MODE_BOTH):
                await self.hass.services.async_call(
                    "climate",
                    "set_fan_mode",
                    {"entity_id": self.climate_entity, "fan_mode": "auto"},
                )
                _LOGGER.warning("Deactivated HVAC fan — %s", reason)

            self._fan_active = False
            self._fan_on_since = None
            self._record_action("Fan deactivated", reason)
        finally:
            self._fan_command_pending = False

    async def check_window_cooling_opportunity(
        self,
        outdoor_temp: float,
        indoor_temp: float | None,
        windows_physically_open: bool,
        current_hour: int = -1,
    ) -> bool:
        """Two-phase window cooling strategy (Issue #27).

        Phase 1 — cool-down: When windows are open and outdoor temp has dropped
        near comfort, run AC to cool to set temp. Outdoor air assists, making
        AC more efficient.

        Phase 2 — maintain: Once indoor reaches comfort (or below), pause AC
        and let natural ventilation hold the temperature.

        Time-bounded to morning (6-9 AM) and evening (5 PM - midnight) hours.
        Respects aggressive_savings: when True, skip AC assist and rely on
        ventilation only.

        Returns True if economizer is active (either phase), False otherwise.
        """
        c = self._current_classification
        if not c or c.day_type != DAY_TYPE_HOT:
            if self._economizer_active:
                await self._deactivate_economizer(outdoor_temp)
            return False

        comfort_cool = self.config.get("comfort_cool", 75)
        delta = self.config.get("economizer_temp_delta", ECONOMIZER_TEMP_DELTA)
        aggressive_savings = self.config.get("aggressive_savings", False)

        # Time-bound check: only during morning (6-9) and evening (17-24) hours
        if current_hour < 0:
            # Default: allow (caller didn't pass hour, skip time gate)
            in_window = True
        else:
            in_window = (ECONOMIZER_MORNING_START_HOUR <= current_hour < ECONOMIZER_MORNING_END_HOUR) or (
                ECONOMIZER_EVENING_START_HOUR <= current_hour < ECONOMIZER_EVENING_END_HOUR
            )

        # Conditions for economizer eligibility
        eligible = windows_physically_open and outdoor_temp <= comfort_cool + delta and in_window

        if not eligible:
            if self._economizer_active:
                await self._deactivate_economizer(outdoor_temp)
            return False

        # --- Economizer is eligible ---
        self._economizer_active = True

        if aggressive_savings:
            # Savings mode: skip AC, rely purely on ventilation
            if self._economizer_phase != "maintain":
                self._economizer_phase = "maintain"
                await self._set_hvac_mode(
                    "off",
                    reason=f"economizer (savings) — outdoor {outdoor_temp:.0f}°F, ventilation only",
                )
                await self._activate_fan(reason="economizer maintain — fan assists ventilation")
                _LOGGER.info(
                    "Economizer (savings): ventilation only, outdoor=%.0f°F",
                    outdoor_temp,
                )
            return True

        # Comfort mode: two-phase strategy
        if indoor_temp is not None and indoor_temp > comfort_cool:
            # Phase 1: cool-down — run AC, outdoor air assists efficiency
            if self._economizer_phase != "cool-down":
                self._economizer_phase = "cool-down"
                await self._set_hvac_mode(
                    "cool",
                    reason=f"economizer cool-down — indoor {indoor_temp:.0f}°F > comfort {comfort_cool}°F, "
                    f"outdoor {outdoor_temp:.0f}°F assisting",
                )
                await self._set_temperature(
                    comfort_cool,
                    reason=f"economizer cool-down — target comfort {comfort_cool}°F",
                )
                _LOGGER.info(
                    "Economizer phase=cool-down: indoor=%.0f°F, target=%s°F, outdoor=%.0f°F",
                    indoor_temp,
                    comfort_cool,
                    outdoor_temp,
                )
            return True
        else:
            # Phase 2: maintain — indoor at or below comfort, AC off
            if self._economizer_phase != "maintain":
                self._economizer_phase = "maintain"
                await self._set_hvac_mode(
                    "off",
                    reason="economizer maintain — indoor %.0f°F at comfort, "
                    "ventilation holding" % (indoor_temp if indoor_temp is not None else 0),
                )
                await self._activate_fan(reason="economizer maintain — fan assists ventilation")
                _LOGGER.info(
                    "Economizer phase=maintain: indoor=%.0f°F, AC off",
                    indoor_temp if indoor_temp is not None else 0,
                )
            return True

    async def _deactivate_economizer(self, outdoor_temp: float) -> None:
        """Deactivate economizer and resume normal AC operation."""
        c = self._current_classification
        self._economizer_active = False
        self._economizer_phase = "inactive"
        await self._deactivate_fan(reason="economizer off — fan no longer needed")
        if c and c.hvac_mode == "cool":
            await self._set_hvac_mode(
                "cool",
                reason=f"economizer off — resuming normal AC (outdoor {outdoor_temp:.0f}°F)",
            )
            await self._set_temperature_for_mode(
                c,
                reason="economizer off — restoring comfort cooling",
            )
        _LOGGER.info("Economizer deactivated: outdoor=%.0f°F", outdoor_temp)

    def restore_state(self, state: dict[str, Any]) -> None:
        """Restore automation state from persisted data.

        Grace timers are cleared on restart (natural reset point).
        Only pause state is restored so HVAC resumes correctly.
        """
        self._paused_by_door = state.get("paused_by_door", False)
        self._pre_pause_mode = state.get("pre_pause_mode")
        self._economizer_active = state.get("economizer_active", False)
        self._economizer_phase = state.get("economizer_phase", "inactive")
        self._last_action_time = state.get("last_action_time")
        self._last_action_reason = state.get("last_action_reason")
        self._manual_override_active = state.get("manual_override_active", False)
        self._manual_override_mode = state.get("manual_override_mode")
        self._manual_override_time = state.get("manual_override_time")
        self._fan_active = state.get("fan_active", False)
        self._fan_on_since = state.get("fan_on_since")
        self._fan_override_active = state.get("fan_override_active", False)
        self._fan_override_time = state.get("fan_override_time")
        # Grace timers cannot be restored — clear on restart
        self._grace_active = False
        self._last_resume_source = None
        _LOGGER.info(
            "Restored automation state: paused=%s, pre_pause_mode=%s, "
            "last_action=%s, manual_override=%s, fan_active=%s, fan_override=%s",
            self._paused_by_door,
            self._pre_pause_mode,
            self._last_action_reason,
            self._manual_override_active,
            self._fan_active,
            self._fan_override_active,
        )

    def get_serializable_state(self) -> dict[str, Any]:
        """Return a JSON-serializable snapshot of the engine's internal state."""
        return {
            "paused_by_door": self._paused_by_door,
            "pre_pause_mode": self._pre_pause_mode,
            "grace_active": self._grace_active,
            "last_resume_source": self._last_resume_source,
            "dry_run": self.dry_run,
            "economizer_active": self._economizer_active,
            "economizer_phase": self._economizer_phase,
            "last_action_time": self._last_action_time,
            "last_action_reason": self._last_action_reason,
            "manual_override_active": self._manual_override_active,
            "manual_override_mode": self._manual_override_mode,
            "manual_override_time": self._manual_override_time,
            "fan_active": self._fan_active,
            "fan_on_since": self._fan_on_since,
            "fan_override_active": self._fan_override_active,
            "fan_override_time": self._fan_override_time,
            "current_classification": (
                {
                    "day_type": self._current_classification.day_type,
                    "hvac_mode": self._current_classification.hvac_mode,
                    "trend_direction": self._current_classification.trend_direction,
                }
                if self._current_classification
                else None
            ),
        }

    def cleanup(self) -> None:
        """Remove all active listeners and cancel pending timers."""
        self._cancel_grace_timers()
        if self._revisit_cancel:
            self._revisit_cancel()
            self._revisit_cancel = None
        for unsub in self._active_listeners:
            unsub()
        self._active_listeners.clear()
