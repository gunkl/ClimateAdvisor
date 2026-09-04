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

DEFAULT_INITIAL_TEMP_F = 70.0
DEFAULT_K_PASSIVE = -0.15
DEFAULT_K_ACTIVE_HEAT = 6.0
DEFAULT_K_ACTIVE_COOL = -6.0
DEFAULT_COMFORT_HEAT = 68.0
DEFAULT_COMFORT_COOL = 76.0
DEFAULT_TICK_SECONDS = 30

PLATFORMS = ["climate"]
