"""Constants for the CA Dev Thermostat Sim integration.

Dev-only, never shipped — see dev_tools/ha_test_integrations/README.md.
"""

from __future__ import annotations

DOMAIN = "ca_dev_thermostat_sim"

CONF_INITIAL_TEMP_F = "initial_temp_f"
CONF_K_PASSIVE = "k_passive"
CONF_K_ACTIVE_HEAT = "k_active_heat"
CONF_K_ACTIVE_COOL = "k_active_cool"
CONF_COMFORT_HEAT = "comfort_heat"
CONF_COMFORT_COOL = "comfort_cool"
CONF_OUTDOOR_SOURCE = "outdoor_source"
CONF_TICK_SECONDS = "tick_seconds"
CONF_DEADBAND_HEAT_F = "deadband_heat_f"
CONF_DEADBAND_COOL_F = "deadband_cool_f"
# Issue #1234: optional ASYMMETRIC deadband. ``deadband_*_f`` is where the compressor STARTS (heat:
# target - deadband; cool: target + deadband). ``off_offset_*_f`` is where it STOPS, as a signed distance
# past the target in the direction of travel (heat stops at target + off_offset_heat_f; cool at target -
# off_offset_cool_f). A real thermostat that starts heat at target-1.4 and stops at target-0.9 is
# deadband_heat_f=1.4, off_offset_heat_f=-0.9. Unset (the default) keeps the symmetric deadband.
CONF_OFF_OFFSET_HEAT_F = "off_offset_heat_f"
CONF_OFF_OFFSET_COOL_F = "off_offset_cool_f"
CONF_MIN_RUN_SECONDS = "min_run_seconds"
CONF_MIN_OFF_SECONDS = "min_off_seconds"

DEFAULT_INITIAL_TEMP_F = 70.0
DEFAULT_K_PASSIVE = -0.15
DEFAULT_K_ACTIVE_HEAT = 6.0
DEFAULT_K_ACTIVE_COOL = -6.0
DEFAULT_COMFORT_HEAT = 68.0
DEFAULT_COMFORT_COOL = 76.0
DEFAULT_TICK_SECONDS = 30
# 1.5°F matches production's THERMAL_SWING_DEFAULT_F (docs/08-COMPUTATION-REFERENCE.md
# §5e-vii) — the same "real thermostat swing" concept CA assumes about real hardware
# when it has no better live estimate yet.
DEFAULT_DEADBAND_HEAT_F = 1.5
DEFAULT_DEADBAND_COOL_F = 1.5
# Issue #1387: by default the compressor STOPS 0.9 F short of the target, the owner's real thermostat as measured in
# #1234 (heat on at setpoint-1.4, off at setpoint-0.9; runs of ~8 min, ~3% duty). The old default, the symmetric
# far-side edge (target+1.5), gave a 3 F swing that overshot the setpoint every cycle. The cool default mirrors the
# measured heat value (no cool cycles were measured). Set off_offset_*_f to deadband_*_f for the old symmetric band.
DEFAULT_OFF_OFFSET_HEAT_F = -0.9
DEFAULT_OFF_OFFSET_COOL_F = -0.9
# 300s (5 min) matches typical compressor short-cycle equipment protection.
DEFAULT_MIN_RUN_SECONDS = 300
DEFAULT_MIN_OFF_SECONDS = 300

# HVAC equipment profile (owner request 2026-11-12; logic and preset values in hvac_profile.py). Every key is
# optional and read with .get(): an entry saved before these existed loads with today's behaviour.
CONF_HEAT_RATE_PRESET = "heat_rate_preset"  # hvac_profile.RATE_PRESET_OPTIONS; unset/"custom" = k_active_heat
CONF_COOL_RATE_PRESET = "cool_rate_preset"  # same, for k_active_cool
CONF_PRECISION = "precision"  # hvac_profile.PRECISION_OPTIONS; unset/"default" = Home Assistant's own
CONF_COMPANION_SENSOR = "companion_sensor"  # bool: add a tenths temperature sensor on the thermostat's device
CONF_SENSOR_LAG_MIN = "sensor_lag_min"  # first-order lag, simulated air -> reported reading(s); 0 = none
CONF_BLOWER_LEAD_IN_HEAT_MIN = "blower_lead_in_heat_min"
CONF_BLOWER_LEAD_IN_COOL_MIN = "blower_lead_in_cool_min"
CONF_BLOWER_OVERRUN_HEAT_MIN = "blower_overrun_heat_min"
CONF_BLOWER_OVERRUN_COOL_MIN = "blower_overrun_cool_min"
CONF_COAST_HEAT_RATE = "coast_heat_rate_f_per_h"  # furnace heat delivered after the burner stops, decaying
CONF_COAST_HEAT_TAU_MIN = "coast_heat_tau_min"
CONF_COAST_COOL_RATE = "coast_cool_rate_f_per_h"  # cold coil still cooling after the compressor stops
CONF_COAST_COOL_TAU_MIN = "coast_cool_tau_min"

PLATFORMS = ["climate", "switch", "number", "select", "sensor"]

# Occupancy scheduling (Issue #898). Reuses production's scheduler.Schedule dataclass
# directly (see occupancy_schedule.py) — these are just the occupancy-state labels this
# fixture's schedules can target, matching climate_advisor's own OCCUPANCY_HOME/AWAY/
# VACATION/GUEST constants by value (not imported — those are plain strings in
# climate_advisor's const.py, duplicating the four literal values here is not a DRY
# violation the way reimplementing schedule-matching logic would be).
OCCUPANCY_HOME = "home"
OCCUPANCY_AWAY = "away"
OCCUPANCY_VACATION = "vacation"
OCCUPANCY_GUEST = "guest"
OCCUPANCY_STATES = (OCCUPANCY_HOME, OCCUPANCY_AWAY, OCCUPANCY_VACATION, OCCUPANCY_GUEST)

# Bug fix (reported live 2026-09-16): the switch PLATFORM must NOT create one entity
# per OCCUPANCY_STATES value. Production's real occupancy config
# (climate_advisor/const.py) only has three toggle fields — CONF_HOME_TOGGLE,
# CONF_VACATION_TOGGLE, CONF_GUEST_TOGGLE. There is no CONF_AWAY_TOGGLE: production
# derives "away" from the home toggle being OFF (occupancy_priority.py's
# guest > vacation > home/away priority chain), not from a fourth entity. The
# original switch.py created a standalone "Away" switch that production can never
# read (nothing to point CONF_AWAY_TOGGLE at, because it doesn't exist) and that
# could disagree with the Home switch (e.g. both on at once) in a way production's
# real model has no way to represent. SWITCH_OCCUPANCY_STATES is the three entities
# that actually get created; OCCUPANCY_STATES above stays four-valued because a
# *schedule* can still legitimately target "away" — it just resolves onto the Home
# switch being turned off, not a fourth switch (see occupancy_schedule.py).
SWITCH_OCCUPANCY_STATES = (OCCUPANCY_HOME, OCCUPANCY_VACATION, OCCUPANCY_GUEST)

CONF_OCCUPANCY_SCHEDULES = "occupancy_schedules"  # list[dict] — see occupancy_schedule.py
CONF_MAX_OCCUPANCY_SCHEDULES = 10  # generous vs. production's MAX_SCHEDULES=5 — dev tooling, not user config sprawl

# Manual current-temperature override (Issue #898) — a one-shot number input, not a
# held override. See number.py.
CONF_CURRENT_TEMP_OVERRIDE_MIN_F = 32.0
CONF_CURRENT_TEMP_OVERRIDE_MAX_F = 110.0
