"""Config flow for CA Dev Thermostat Sim.

Dev-only, never shipped — see dev_tools/ha_test_integrations/README.md.
Follows the selector conventions used by custom_components/climate_advisor's
own config_flow.py (vol.Schema + homeassistant.helpers.selector).

NOTE ON HA VERSION: async_step_reconfigure()/_get_reconfigure_entry()/
async_update_reload_and_abort() are the modern (2024.x+) unified reconfigure-flow
API. Like climate.py's own note, this was NOT verified against a locally-installed
`homeassistant` package — none exists in this repo/venv. Test on a real Home
Assistant instance before relying on it.
"""

from __future__ import annotations

import logging
import uuid
from typing import Any

import voluptuous as vol
from homeassistant import config_entries
from homeassistant.core import callback
from homeassistant.helpers import selector

from .const import (
    CONF_BLOWER_LEAD_IN_COOL_MIN,
    CONF_BLOWER_LEAD_IN_HEAT_MIN,
    CONF_BLOWER_OVERRUN_COOL_MIN,
    CONF_BLOWER_OVERRUN_HEAT_MIN,
    CONF_COAST_COOL_RATE,
    CONF_COAST_COOL_TAU_MIN,
    CONF_COAST_HEAT_RATE,
    CONF_COAST_HEAT_TAU_MIN,
    CONF_COMFORT_COOL,
    CONF_COMFORT_HEAT,
    CONF_COMPANION_SENSOR,
    CONF_COOL_RATE_PRESET,
    CONF_DEADBAND_COOL_F,
    CONF_DEADBAND_HEAT_F,
    CONF_EDGE_REPORT_DELAY_S,
    CONF_HEAT_RATE_PRESET,
    CONF_IDLE_BEFORE_OVERRUN_S,
    CONF_INITIAL_TEMP_F,
    CONF_K_ACTIVE_COOL,
    CONF_K_ACTIVE_HEAT,
    CONF_K_PASSIVE,
    CONF_MAX_OCCUPANCY_SCHEDULES,
    CONF_MIN_OFF_SECONDS,
    CONF_MIN_RUN_SECONDS,
    CONF_OCCUPANCY_SCHEDULES,
    CONF_OFF_OFFSET_COOL_F,
    CONF_OFF_OFFSET_HEAT_F,
    CONF_OUTDOOR_SOURCE,
    CONF_PRECISION,
    CONF_REPORT_HVAC_ACTION,
    CONF_SENSOR_LAG_MIN,
    CONF_TICK_SECONDS,
    DEFAULT_COMFORT_COOL,
    DEFAULT_COMFORT_HEAT,
    DEFAULT_DEADBAND_COOL_F,
    DEFAULT_DEADBAND_HEAT_F,
    DEFAULT_INITIAL_TEMP_F,
    DEFAULT_K_ACTIVE_COOL,
    DEFAULT_K_ACTIVE_HEAT,
    DEFAULT_K_PASSIVE,
    DEFAULT_MIN_OFF_SECONDS,
    DEFAULT_MIN_RUN_SECONDS,
    DEFAULT_TICK_SECONDS,
    DOMAIN,
    OCCUPANCY_STATES,
)
from .hvac_profile import (
    BLOWER_MAX_MIN,
    COAST_RATE_MAX_F_PER_H,
    COAST_TAU_MAX_MIN,
    DEFAULT_EDGE_REPORT_DELAY_S,
    DEFAULT_IDLE_BEFORE_OVERRUN_S,
    DEFAULT_PRECISION,
    DEFAULT_RATE_PRESET,
    DEFAULT_SENSOR_LAG_MIN,
    EDGE_REPORT_DELAY_MAX_S,
    IDLE_BEFORE_OVERRUN_MAX_S,
    PRECISION_OPTIONS,
    RATE_PRESET_OPTIONS,
    SENSOR_LAG_MAX_MIN,
    resolve_rate,
)

_LOGGER = logging.getLogger(__name__)

try:
    # Same defensive-import shape climate.py already uses for its production import
    # (Issue #898 DRY reuse) — config flow discovery can happen before
    # async_setup_entry ever runs its own ConfigEntryNotReady check, so an
    # unguarded top-level import failure here would break the config UI entirely
    # rather than surfacing climate.py's clearer "install climate_advisor
    # alongside this" error.
    from custom_components.climate_advisor.scheduler import WEEKDAY_ABBREVS, _parse_hhmm
except ImportError:
    WEEKDAY_ABBREVS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")

    def _parse_hhmm(value: str) -> float:
        parts = value.split(":")
        return int(parts[0]) + int(parts[1]) / 60.0


def _num(
    *, min_: float, max_: float, step: float, unit: str | None = None, mode: str = "box"
) -> selector.NumberSelector:
    return selector.NumberSelector(
        selector.NumberSelectorConfig(min=min_, max=max_, step=step, unit_of_measurement=unit, mode=mode)
    )


def _build_schema(*, defaults: dict[str, Any]) -> vol.Schema:
    """Build the identity/physics/tick-rate schema shared by creation and reconfigure.

    ``defaults`` is either the module's DEFAULT_* constants (initial creation) or an
    existing entry's current ``data`` (reconfigure) — one schema definition either
    way, so the two flows can't silently drift apart on field set, bounds, or units.
    """
    return vol.Schema(
        {
            vol.Required("name", default=defaults.get("name", "Simulated Thermostat")): selector.TextSelector(),
            vol.Required(CONF_INITIAL_TEMP_F, default=defaults.get(CONF_INITIAL_TEMP_F, DEFAULT_INITIAL_TEMP_F)): _num(
                min_=32, max_=110, step=0.1, unit="°F"
            ),
            vol.Required(CONF_K_PASSIVE, default=defaults.get(CONF_K_PASSIVE, DEFAULT_K_PASSIVE)): _num(
                min_=-2.0, max_=-0.01, step=0.01, unit="1/hr"
            ),
            vol.Required(CONF_K_ACTIVE_HEAT, default=defaults.get(CONF_K_ACTIVE_HEAT, DEFAULT_K_ACTIVE_HEAT)): _num(
                min_=0.1, max_=20.0, step=0.1, unit="°F/hr"
            ),
            vol.Required(CONF_K_ACTIVE_COOL, default=defaults.get(CONF_K_ACTIVE_COOL, DEFAULT_K_ACTIVE_COOL)): _num(
                min_=-20.0, max_=-0.1, step=0.1, unit="°F/hr"
            ),
            vol.Required(CONF_COMFORT_HEAT, default=defaults.get(CONF_COMFORT_HEAT, DEFAULT_COMFORT_HEAT)): _num(
                min_=32, max_=110, step=0.5, unit="°F"
            ),
            vol.Required(CONF_COMFORT_COOL, default=defaults.get(CONF_COMFORT_COOL, DEFAULT_COMFORT_COOL)): _num(
                min_=32, max_=110, step=0.5, unit="°F"
            ),
            vol.Required(
                CONF_OUTDOOR_SOURCE, default=defaults.get(CONF_OUTDOOR_SOURCE, vol.UNDEFINED)
            ): selector.EntitySelector(selector.EntitySelectorConfig(domain=["weather", "sensor"])),
            vol.Required(CONF_TICK_SECONDS, default=defaults.get(CONF_TICK_SECONDS, DEFAULT_TICK_SECONDS)): _num(
                min_=5, max_=3600, step=1, unit="s"
            ),
            vol.Required(
                CONF_DEADBAND_HEAT_F, default=defaults.get(CONF_DEADBAND_HEAT_F, DEFAULT_DEADBAND_HEAT_F)
            ): _num(min_=0.1, max_=10.0, step=0.1, unit="°F"),
            vol.Required(
                CONF_DEADBAND_COOL_F, default=defaults.get(CONF_DEADBAND_COOL_F, DEFAULT_DEADBAND_COOL_F)
            ): _num(min_=0.1, max_=10.0, step=0.1, unit="°F"),
            # Issue #1234: optional asymmetric deadband - where the compressor STOPS relative to the target
            # (signed, in the direction of travel). Leave empty for the default: 0.9 F short of the target, the
            # measured real thermostat (Issue #1387). Enter deadband_*_f for the old symmetric band.
            vol.Optional(
                CONF_OFF_OFFSET_HEAT_F, description={"suggested_value": defaults.get(CONF_OFF_OFFSET_HEAT_F)}
            ): _num(min_=-10.0, max_=10.0, step=0.1, unit="°F"),
            vol.Optional(
                CONF_OFF_OFFSET_COOL_F, description={"suggested_value": defaults.get(CONF_OFF_OFFSET_COOL_F)}
            ): _num(min_=-10.0, max_=10.0, step=0.1, unit="°F"),
            vol.Required(
                CONF_MIN_RUN_SECONDS, default=defaults.get(CONF_MIN_RUN_SECONDS, DEFAULT_MIN_RUN_SECONDS)
            ): _num(min_=0, max_=1800, step=10, unit="s"),
            vol.Required(
                CONF_MIN_OFF_SECONDS, default=defaults.get(CONF_MIN_OFF_SECONDS, DEFAULT_MIN_OFF_SECONDS)
            ): _num(min_=0, max_=1800, step=10, unit="s"),
            # HVAC profile (owner request 2026-11-12, hvac_profile.py). Defaults reproduce today's behaviour: the
            # "custom" preset uses k_active_heat / k_active_cool above, "default" precision is Home Assistant's.
            vol.Required(
                CONF_HEAT_RATE_PRESET, default=defaults.get(CONF_HEAT_RATE_PRESET) or DEFAULT_RATE_PRESET
            ): _select(RATE_PRESET_OPTIONS),
            vol.Required(
                CONF_COOL_RATE_PRESET, default=defaults.get(CONF_COOL_RATE_PRESET) or DEFAULT_RATE_PRESET
            ): _select(RATE_PRESET_OPTIONS),
            vol.Required(CONF_PRECISION, default=defaults.get(CONF_PRECISION) or DEFAULT_PRECISION): _select(
                PRECISION_OPTIONS
            ),
            vol.Required(
                CONF_COMPANION_SENSOR, default=bool(defaults.get(CONF_COMPANION_SENSOR, False))
            ): selector.BooleanSelector(),
            vol.Required(CONF_SENSOR_LAG_MIN, default=defaults.get(CONF_SENSOR_LAG_MIN, DEFAULT_SENSOR_LAG_MIN)): _num(
                min_=0, max_=SENSOR_LAG_MAX_MIN, step=0.5, unit="min"
            ),
            vol.Required(
                CONF_EDGE_REPORT_DELAY_S, default=defaults.get(CONF_EDGE_REPORT_DELAY_S, DEFAULT_EDGE_REPORT_DELAY_S)
            ): _num(min_=0, max_=EDGE_REPORT_DELAY_MAX_S, step=5, unit="s"),
            vol.Required(
                CONF_IDLE_BEFORE_OVERRUN_S,
                default=defaults.get(CONF_IDLE_BEFORE_OVERRUN_S, DEFAULT_IDLE_BEFORE_OVERRUN_S),
            ): _num(min_=0, max_=IDLE_BEFORE_OVERRUN_MAX_S, step=1, unit="s"),
            vol.Required(
                CONF_REPORT_HVAC_ACTION, default=bool(defaults.get(CONF_REPORT_HVAC_ACTION, True))
            ): selector.BooleanSelector(),
            **{
                vol.Required(key, default=defaults.get(key, 0.0)): _num(min_=0, max_=max_, step=step, unit=unit)
                for key, max_, step, unit in (
                    (CONF_BLOWER_LEAD_IN_HEAT_MIN, BLOWER_MAX_MIN, 0.5, "min"),
                    (CONF_BLOWER_OVERRUN_HEAT_MIN, BLOWER_MAX_MIN, 0.5, "min"),
                    (CONF_COAST_HEAT_RATE, COAST_RATE_MAX_F_PER_H, 0.1, "°F/hr"),
                    (CONF_COAST_HEAT_TAU_MIN, COAST_TAU_MAX_MIN, 0.5, "min"),
                    (CONF_BLOWER_LEAD_IN_COOL_MIN, BLOWER_MAX_MIN, 0.5, "min"),
                    (CONF_BLOWER_OVERRUN_COOL_MIN, BLOWER_MAX_MIN, 0.5, "min"),
                    (CONF_COAST_COOL_RATE, COAST_RATE_MAX_F_PER_H, 0.1, "°F/hr"),
                    (CONF_COAST_COOL_TAU_MIN, COAST_TAU_MAX_MIN, 0.5, "min"),
                )
            },
        }
    )


def _select(options: tuple[str, ...]) -> selector.SelectSelector:
    return selector.SelectSelector(
        selector.SelectSelectorConfig(options=list(options), mode=selector.SelectSelectorMode.DROPDOWN)
    )


def _apply_rate_presets(data: dict[str, Any]) -> dict[str, Any]:
    """A chosen preset also writes its rate into k_active_heat / k_active_cool, so the stored rate is the one used."""
    out = dict(data)
    if CONF_K_ACTIVE_HEAT in out:
        out[CONF_K_ACTIVE_HEAT] = resolve_rate(out.get(CONF_HEAT_RATE_PRESET), out[CONF_K_ACTIVE_HEAT], cooling=False)
    if CONF_K_ACTIVE_COOL in out:
        out[CONF_K_ACTIVE_COOL] = resolve_rate(out.get(CONF_COOL_RATE_PRESET), out[CONF_K_ACTIVE_COOL], cooling=True)
    return out


class CaDevThermostatSimConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a config flow for CA Dev Thermostat Sim."""

    VERSION = 1

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> Any:
        """Collect the simulated thermostat's identity, physics params, and tick rate."""
        errors: dict[str, str] = {}

        if user_input is not None:
            return self.async_create_entry(title=user_input["name"], data=_apply_rate_presets(user_input))

        return self.async_show_form(step_id="user", data_schema=_build_schema(defaults={}), errors=errors)

    async def async_step_reconfigure(self, user_input: dict[str, Any] | None = None) -> Any:
        """Retune an existing entry's physics/comfort/tick-rate without deleting it.

        Previously this integration had no reconfigure step at all — the only way to
        change, say, k_active_heat/k_active_cool after creation was to delete and
        re-add the entry, losing its restored simulation state. This reuses the exact
        same schema async_step_user() builds (see _build_schema() above), pre-filled
        from the entry's current data instead of the module defaults.
        """
        entry = self._get_reconfigure_entry()
        errors: dict[str, str] = {}

        if user_input is not None:
            return self.async_update_reload_and_abort(
                entry, data=_apply_rate_presets(user_input), title=user_input["name"]
            )

        return self.async_show_form(
            step_id="reconfigure", data_schema=_build_schema(defaults=dict(entry.data)), errors=errors
        )

    @staticmethod
    @callback
    def async_get_options_flow(
        config_entry: config_entries.ConfigEntry,
    ) -> CaDevThermostatSimOptionsFlow:
        """Get the options flow handler — occupancy schedule management (Issue #898)."""
        return CaDevThermostatSimOptionsFlow()


class CaDevThermostatSimOptionsFlow(config_entries.OptionsFlow):
    """Manage this zone's occupancy schedules (Issue #898).

    Mirrors custom_components/climate_advisor/config_flow.py's own
    async_step_scheduler()/async_step_scheduler_edit() list-management shape —
    same add/edit/remove UX the user already knows from the real TOU calendar,
    applied to occupancy-state scheduling instead of cost-period scheduling.
    """

    def __init__(self) -> None:
        self._editing_schedule_id: str | None = None

    @staticmethod
    def _format_schedule_summary(schedule: dict[str, Any]) -> str:
        days_label = "/".join(d.capitalize() for d in schedule.get("days", []))
        window = f"{schedule.get('start')}-{schedule.get('end')}"
        target = schedule.get("target", "?").capitalize()
        name = schedule.get("name", "(unnamed)")
        return f"{name} — {days_label} {window} → {target}"

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> config_entries.ConfigFlowResult:
        """List existing occupancy schedules; choose one to edit, or add a new one."""
        schedules: list[dict[str, Any]] = list(self.config_entry.data.get(CONF_OCCUPANCY_SCHEDULES, []))

        if user_input is not None:
            selection = user_input["manage"]
            self._editing_schedule_id = None if selection == "__add__" else selection
            return await self.async_step_edit()

        options = [selector.SelectOptionDict(value=s["id"], label=self._format_schedule_summary(s)) for s in schedules]
        if len(schedules) < CONF_MAX_OCCUPANCY_SCHEDULES:
            options.append(selector.SelectOptionDict(value="__add__", label="+ Add a new schedule"))

        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema(
                {
                    vol.Required("manage"): selector.SelectSelector(
                        selector.SelectSelectorConfig(options=options, mode=selector.SelectSelectorMode.LIST)
                    ),
                }
            ),
            description_placeholders={"count": str(len(schedules)), "max": str(CONF_MAX_OCCUPANCY_SCHEDULES)},
        )

    async def async_step_edit(self, user_input: dict[str, Any] | None = None) -> config_entries.ConfigFlowResult:
        """Add, edit, or delete one occupancy schedule."""
        errors: dict[str, str] = {}
        schedules: list[dict[str, Any]] = list(self.config_entry.data.get(CONF_OCCUPANCY_SCHEDULES, []))
        editing_id = self._editing_schedule_id
        existing = next((s for s in schedules if s["id"] == editing_id), None) if editing_id else None

        if user_input is not None:
            if existing is not None and user_input.get("delete_schedule"):
                schedules = [s for s in schedules if s["id"] != editing_id]
                self.hass.config_entries.async_update_entry(
                    self.config_entry, data={**self.config_entry.data, CONF_OCCUPANCY_SCHEDULES: schedules}
                )
                self._editing_schedule_id = None
                return await self.async_step_init()

            if not user_input.get("days"):
                errors["days"] = "schedule_days_required"
            start_raw = user_input.get("start")
            end_raw = user_input.get("end")
            # Same start==end degenerate-window check production's own scheduler
            # edit step uses, comparing parsed hour/minute (seconds ignored) to
            # match is_schedule_active_at()'s own comparison semantics exactly.
            if start_raw is not None and end_raw is not None and _parse_hhmm(start_raw) == _parse_hhmm(end_raw):
                errors["end"] = "schedule_start_end_equal"

            if not errors:
                new_schedule = {
                    "id": existing["id"] if existing else uuid.uuid4().hex,
                    "name": user_input["name"],
                    "days": list(user_input["days"]),
                    "start": user_input["start"],
                    "end": user_input["end"],
                    "target": user_input["target"],
                }
                if existing is not None:
                    schedules = [new_schedule if s["id"] == editing_id else s for s in schedules]
                elif len(schedules) < CONF_MAX_OCCUPANCY_SCHEDULES:
                    schedules = [*schedules, new_schedule]
                self.hass.config_entries.async_update_entry(
                    self.config_entry, data={**self.config_entry.data, CONF_OCCUPANCY_SCHEDULES: schedules}
                )
                self._editing_schedule_id = None
                return await self.async_step_init()

        day_options = [selector.SelectOptionDict(value=d, label=d.capitalize()) for d in WEEKDAY_ABBREVS]
        target_options = [selector.SelectOptionDict(value=t, label=t.capitalize()) for t in OCCUPANCY_STATES]
        defaults = user_input if user_input is not None else (existing or {})

        schema: dict[Any, Any] = {
            vol.Required("name", default=defaults.get("name", "")): selector.TextSelector(),
            vol.Required("days", default=defaults.get("days", [])): selector.SelectSelector(
                selector.SelectSelectorConfig(options=day_options, multiple=True, mode=selector.SelectSelectorMode.LIST)
            ),
            vol.Required("start", default=defaults.get("start", "09:00:00")): selector.TimeSelector(),
            vol.Required("end", default=defaults.get("end", "17:00:00")): selector.TimeSelector(),
            vol.Required("target", default=defaults.get("target", OCCUPANCY_STATES[1])): selector.SelectSelector(
                selector.SelectSelectorConfig(options=target_options, mode=selector.SelectSelectorMode.DROPDOWN)
            ),
        }
        if existing is not None:
            schema[vol.Optional("delete_schedule", default=False)] = selector.BooleanSelector()

        return self.async_show_form(step_id="edit", data_schema=vol.Schema(schema), errors=errors)
