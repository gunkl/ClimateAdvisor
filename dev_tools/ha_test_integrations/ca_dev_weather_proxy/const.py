"""Constants for the CA Dev Weather Proxy integration.

Dev-only, never shipped — see dev_tools/ha_test_integrations/README.md.
"""

from __future__ import annotations

DOMAIN = "ca_dev_weather_proxy"

CONF_BASE_TEMP_F = "base_temp_f"
CONF_DIURNAL_SWING_F = "diurnal_swing_f"
CONF_PHASE_OFFSET_H = "phase_offset_h"
CONF_CONDITION = "condition"

DEFAULT_BASE_TEMP_F = 65.0
DEFAULT_DIURNAL_SWING_F = 15.0
DEFAULT_PHASE_OFFSET_H = 15.0
DEFAULT_CONDITION = "sunny"

CONDITION_OPTIONS = ["sunny", "cloudy", "rainy", "snowy"]

PLATFORMS = ["weather"]
