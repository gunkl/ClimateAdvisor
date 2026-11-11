"""Plain-English meaning of the heating/cooling run rejection codes (Issues #1512/#1516), shared by the tools."""

REJECT_LABELS = {
    # Issues #1512/#1516: the cycle model's reasons a heating or cooling cycle taught it nothing.
    "cycle_too_short": "cycle too short to measure",
    "cycle_signal_below_resolution": "room moved fewer than three sensor steps",
    "cycle_excluded_window_fan": "a window was open or the fan ran during the cycle",
    "cycle_setpoint_change": "the setpoint changed during the cycle",
    "cycle_gap_restart": "a restart cut the cycle",
    "cycle_thermostat_unavailable": "the thermostat was unavailable during the cycle",
    # Historical: the per-run estimator that wrote these is retired; old entries keep them until they age out.
    "hvac_run_too_short": "run too short to measure (historical)",
    "hvac_signal_below_resolution": "room moved less than the sensor can reliably show (historical)",
    "no_k_passive": "house leak rate not learned yet (historical)",
    "new_session_started": "a new run began before this one finished",
    "fan_activated": "fan started during the run",
    "hvac_mode_switch": "heat/cool switched during the run",
}
