"""Plain-English meaning of the heating/cooling run rejection codes (Issues #1512/#1516), shared by the tools."""

REJECT_LABELS = {
    "hvac_run_too_short": "run too short to measure",
    "hvac_signal_below_resolution": "room moved less than the sensor can reliably show",
    "no_k_passive": "house leak rate not learned yet",
    "new_session_started": "a new run began before this one finished",
    "fan_activated": "fan started during the run",
    "hvac_mode_switch": "heat/cool switched during the run",
}
