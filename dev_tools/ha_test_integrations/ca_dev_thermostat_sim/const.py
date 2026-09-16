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
# 300s (5 min) matches typical compressor short-cycle equipment protection.
DEFAULT_MIN_RUN_SECONDS = 300
DEFAULT_MIN_OFF_SECONDS = 300

PLATFORMS = ["climate", "switch", "number"]

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
