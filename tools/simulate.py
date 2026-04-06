#!/usr/bin/env python3
"""Climate Advisor Behavior Simulator.

Replays activity scenarios through the automation decision logic
to verify expected behavior and detect regressions.

Scenario states:
  pending/       - ingested, awaiting human review
  golden/        - approved, passing — protected regression tests
  pending-fix/   - wrong outcome, code fix needed (linked to issue)
  unsupported/   - wrong outcome, intentionally out of scope
  synthetic/     - constructed (not from real events), lower authority

Usage:
  python3 tools/simulate.py              # run all golden scenarios
  python3 tools/simulate.py -s NAME      # run specific scenario (any state)
  python3 tools/simulate.py --pending    # run all pending scenarios (for review)
  python3 tools/simulate.py --list       # list all scenarios by state
  python3 tools/simulate.py --cases      # summary table of all scenarios across all states
  python3 tools/simulate.py -v           # verbose (show full decision timeline)

Lifecycle:
  Real event → /simulate add → pending/ → review → golden/ or pending-fix/ or unsupported/
  All golden scenarios must pass on every run — failures = regression.
"""

import argparse
import json
import sys
from dataclasses import dataclass, field
from datetime import time as dt_time
from pathlib import Path

SIMULATIONS_DIR = Path(__file__).parent / "simulations"
STATE_DIRS: dict[str, Path] = {
    "pending": SIMULATIONS_DIR / "pending",
    "golden": SIMULATIONS_DIR / "golden",
    "pending-fix": SIMULATIONS_DIR / "pending-fix",
    "unsupported": SIMULATIONS_DIR / "unsupported",
    "synthetic": SIMULATIONS_DIR / "synthetic",
}

# Constants mirrored from const.py — keep in sync.
VACATION_SETBACK_EXTRA = 3.0  # const.py VACATION_SETBACK_EXTRA = 3
DEFAULT_SETBACK_DEPTH_F = 4.0  # heat bedtime default depth (no thermal model)
DEFAULT_SETBACK_DEPTH_COOL_F = 3.0  # cool bedtime default depth (no thermal model)


@dataclass(frozen=True)
class SimClassification:
    """Mirrors DayClassification fields used in the simulator state machine.

    Keep in sync with classifier.py DayClassification.
    window_open_time and window_close_time are stored as "HH:MM" strings
    and parsed to datetime.time on demand by _parse_window_time().
    """

    day_type: str
    hvac_mode: str  # "heat" | "cool" | "off"
    setback_modifier: float = 0.0
    windows_recommended: bool = False
    window_open_time: str | None = None  # "HH:MM"
    window_close_time: str | None = None  # "HH:MM"


@dataclass
class SimState:
    """Mirrors AutomationEngine state variables.

    Keep this in sync with automation.py. When automation.py state variables
    change, update this class and the ClimateSimulator methods below.
    """

    # Door/window + temperature
    indoor_temp: float | None = None
    outdoor_temp: float | None = None
    sensors_open: set = field(default_factory=set)
    hvac_mode: str = "off"
    fan_mode: str = "auto"
    paused_by_door: bool = False
    natural_vent_active: bool = False
    grace_active: bool = False

    # Classification and occupancy
    classification: SimClassification | None = None
    occupancy: str = "home"  # "home" | "away" | "vacation"
    hvac_target_temp: float | None = None
    manual_override_active: bool = False

    # Fan state (Issue #37 + #77)
    fan_active: bool = False
    fan_min_runtime_active: bool = False

    # Economizer state (Issue #27)
    economizer_active: bool = False
    economizer_phase: str = "inactive"  # "inactive" | "cool-down" | "maintain"


@dataclass
class Decision:
    """A single automation decision recorded at a point in time."""

    time: str
    event: str
    outcome: str
    reason: str
    hvac_mode: str
    fan_mode: str
    target_temp: float | None = None  # set when a setback/restore temperature is applied


class ClimateSimulator:
    """Pure-Python state machine mirroring automation.py logic.

    This class intentionally duplicates the decision logic from automation.py
    so it can run without a Home Assistant instance. When automation.py logic
    changes, update the corresponding methods here. Golden scenario failures
    will alert you when the two diverge.
    """

    def __init__(self, config: dict) -> None:
        self.config = config
        self.state = SimState()
        self.decisions: list[Decision] = []

    def process_event(self, event: dict) -> Decision | None:
        """Process one scenario event and return any decision made."""
        etype = event["type"]
        ts = event["time"]

        if etype == "temp_update":
            if "indoor_f" in event:
                self.state.indoor_temp = event["indoor_f"]
            if "outdoor_f" in event:
                self.state.outdoor_temp = event["outdoor_f"]
            return self._check_conditions_on_temp_update(ts)

        if etype == "sensor_open":
            self.state.sensors_open.add(event.get("entity", "sensor"))
            return self._handle_sensor_open(ts)

        if etype == "sensor_close":
            self.state.sensors_open.discard(event.get("entity", "sensor"))
            if not self.state.sensors_open:
                return self._handle_all_closed(ts)
            return None

        if etype == "classification":
            return self._handle_classification(ts, event)

        if etype == "occupancy_away":
            return self._handle_occupancy_away(ts)

        if etype == "occupancy_home":
            return self._handle_occupancy_home(ts)

        if etype == "occupancy_vacation":
            return self._handle_occupancy_vacation(ts)

        if etype == "bedtime":
            return self._handle_bedtime(ts)

        if etype == "wakeup":
            return self._handle_wakeup(ts)

        if etype == "fan_cycle_on":
            return self._handle_fan_cycle_on(ts)

        if etype == "fan_cycle_off":
            return self._handle_fan_cycle_off(ts)

        if etype == "economizer_check":
            return self._handle_economizer_check(ts, event)

        if etype == "grace_start":
            self.state.grace_active = True
            return None

        if etype == "grace_end":
            self.state.grace_active = False
            return None

        return None

    # ------------------------------------------------------------------
    # Door/window + nat-vent handlers (existing logic)
    # ------------------------------------------------------------------

    def _handle_sensor_open(self, ts: str) -> Decision:
        """Mirror automation.py handle_door_window_open() logic."""
        if self.state.paused_by_door:
            d = Decision(ts, "sensor_open", "no_action", "already paused", self.state.hvac_mode, self.state.fan_mode)
            self.decisions.append(d)
            return d

        if self.state.grace_active:
            d = Decision(
                ts,
                "sensor_open",
                "no_action",
                "grace period active — skip pause",
                self.state.hvac_mode,
                self.state.fan_mode,
            )
            self.decisions.append(d)
            return d

        if self._is_within_planned_window_period(ts):
            d = Decision(
                ts,
                "sensor_open",
                "no_action",
                "within planned window period — not pausing",
                self.state.hvac_mode,
                self.state.fan_mode,
            )
            self.decisions.append(d)
            return d

        return self._apply_nat_vent_or_pause(ts, event_type="sensor_open")

    def _check_conditions_on_temp_update(self, ts: str) -> Decision | None:
        """Re-evaluate automation state when temperatures change.

        Mirrors the monitoring loop that runs on each coordinator update:
        - If natural vent active and outdoor climbed → exit to pause
        - If paused and outdoor dropped → activate natural vent
        """
        if self.state.natural_vent_active:
            return self._check_natural_vent_exit(ts)
        if self.state.paused_by_door and self.state.sensors_open:
            return self._check_natural_vent_entry(ts)
        return None

    def _apply_nat_vent_or_pause(self, ts: str, event_type: str) -> Decision:
        """Decide: natural ventilation or full pause? Returns and records the decision."""
        outdoor = self.state.outdoor_temp
        comfort_cool = float(self.config.get("comfort_cool", 75))
        delta = float(self.config.get("natural_vent_delta", 3.0))
        threshold = comfort_cool + delta

        if outdoor is not None and outdoor <= threshold:
            self.state.hvac_mode = "off"
            self.state.fan_mode = "on"
            self.state.natural_vent_active = True
            self.state.paused_by_door = False
            d = Decision(
                ts,
                event_type,
                "natural_ventilation",
                f"outdoor {outdoor}F <= target {comfort_cool}F + delta {delta}F",
                "off",
                "on",
            )
        else:
            self.state.hvac_mode = "off"
            self.state.paused_by_door = True
            self.state.natural_vent_active = False
            reason = (
                f"outdoor {outdoor}F > threshold {threshold}F"
                if outdoor is not None
                else "outdoor temp unknown — defaulting to pause"
            )
            d = Decision(ts, event_type, "paused", reason, "off", self.state.fan_mode)

        self.decisions.append(d)
        return d

    def _check_natural_vent_exit(self, ts: str) -> Decision | None:
        """Exit natural ventilation if outdoor climbed above threshold."""
        comfort_cool = float(self.config.get("comfort_cool", 75))
        delta = float(self.config.get("natural_vent_delta", 3.0))
        outdoor = self.state.outdoor_temp
        threshold = comfort_cool + delta

        if outdoor is not None and outdoor > threshold and self.state.sensors_open:
            self.state.natural_vent_active = False
            self.state.paused_by_door = True
            self.state.fan_mode = "auto"
            d = Decision(
                ts,
                "temp_update",
                "paused",
                f"outdoor {outdoor}F exceeded threshold {threshold}F — sensors still open",
                "off",
                "auto",
            )
            self.decisions.append(d)
            return d
        return None

    def _check_natural_vent_entry(self, ts: str) -> Decision | None:
        """Activate natural ventilation if outdoor dropped below threshold while paused."""
        outdoor = self.state.outdoor_temp
        comfort_cool = float(self.config.get("comfort_cool", 75))
        delta = float(self.config.get("natural_vent_delta", 3.0))
        threshold = comfort_cool + delta

        if outdoor is not None and outdoor <= threshold:
            return self._apply_nat_vent_or_pause(ts, event_type="temp_update")
        return None

    def _handle_all_closed(self, ts: str) -> Decision:
        """Resume automation when all sensors close."""
        was_nat_vent = self.state.natural_vent_active
        self.state.paused_by_door = False
        self.state.natural_vent_active = False
        self.state.fan_mode = "auto"
        reason = (
            "all sensors closed — exiting natural ventilation, resuming automation"
            if was_nat_vent
            else "all sensors closed — resuming automation"
        )
        d = Decision(ts, "sensor_close", "resumed", reason, self.state.hvac_mode, "auto")
        self.decisions.append(d)
        return d

    # ------------------------------------------------------------------
    # Planned window period gate (mirrors automation.py)
    # ------------------------------------------------------------------

    @staticmethod
    def _parse_window_time(time_str: str | None) -> dt_time | None:
        """Parse "HH:MM" string to datetime.time. Returns None on any failure."""
        if not time_str:
            return None
        try:
            h, m = time_str.split(":")
            return dt_time(int(h), int(m))
        except (ValueError, AttributeError):
            return None

    def _is_within_planned_window_period(self, ts: str) -> bool:
        """Return True when windows are recommended AND we are within the planned window time.

        Mirrors automation.py _is_within_planned_window_period(). Uses the event
        timestamp string instead of dt_util.now() since the simulator is not real-time.
        """
        c = self.state.classification
        if not c or not c.windows_recommended:
            return False
        if c.hvac_mode != "off":
            return False
        open_t = self._parse_window_time(c.window_open_time)
        close_t = self._parse_window_time(c.window_close_time)
        if not open_t or not close_t:
            return False
        try:
            from datetime import datetime as _dt

            now_time = _dt.fromisoformat(ts).time()
        except (ValueError, AttributeError):
            return False
        return open_t <= now_time <= close_t

    # ------------------------------------------------------------------
    # Classification handler
    # ------------------------------------------------------------------

    def _handle_classification(self, ts: str, event: dict) -> Decision:
        """Apply day classification — mirrors apply_classification().

        Sets HVAC mode and comfort target temp unless manual_override_active.
        Classification is always stored even when overridden.
        """
        c = SimClassification(
            day_type=event["day_type"],
            hvac_mode=event.get("hvac_mode", "off"),
            setback_modifier=float(event.get("setback_modifier", 0.0)),
            windows_recommended=bool(event.get("windows_recommended", False)),
            window_open_time=event.get("window_open_time"),
            window_close_time=event.get("window_close_time"),
        )
        self.state.classification = c

        if self.state.manual_override_active:
            d = Decision(
                ts,
                "classification",
                "no_action",
                f"manual override active — classification stored ({c.day_type}/{c.hvac_mode}), HVAC unchanged",
                self.state.hvac_mode,
                self.state.fan_mode,
            )
            self.decisions.append(d)
            return d

        self.state.hvac_mode = c.hvac_mode
        if c.hvac_mode == "heat":
            self.state.hvac_target_temp = float(self.config.get("comfort_heat", 70))
        elif c.hvac_mode == "cool":
            self.state.hvac_target_temp = float(self.config.get("comfort_cool", 75))
        else:
            self.state.hvac_target_temp = None

        d = Decision(
            ts,
            "classification",
            "classification_applied",
            f"daily classification — {c.day_type} day, hvac_mode={c.hvac_mode}",
            self.state.hvac_mode,
            self.state.fan_mode,
            target_temp=self.state.hvac_target_temp,
        )
        self.decisions.append(d)
        return d

    # ------------------------------------------------------------------
    # Occupancy handlers
    # ------------------------------------------------------------------

    def _handle_occupancy_away(self, ts: str) -> Decision:
        """Apply away setback — mirrors handle_occupancy_away().

        No HVAC mode change; temperature setback only.
        """
        self.state.occupancy = "away"
        c = self.state.classification
        if not c:
            d = Decision(
                ts,
                "occupancy_away",
                "no_action",
                "no classification — setback skipped",
                self.state.hvac_mode,
                self.state.fan_mode,
            )
            self.decisions.append(d)
            return d

        if c.hvac_mode == "heat":
            target = float(self.config.get("setback_heat", 60)) + c.setback_modifier
            reason = (
                f"occupancy away — heat setback"
                f" (base {self.config.get('setback_heat', 60)}"
                f" + modifier {c.setback_modifier})"
            )
        elif c.hvac_mode == "cool":
            target = float(self.config.get("setback_cool", 80)) - c.setback_modifier
            reason = (
                f"occupancy away — cool setback"
                f" (base {self.config.get('setback_cool', 80)}"
                f" - modifier {c.setback_modifier})"
            )
        else:
            d = Decision(
                ts,
                "occupancy_away",
                "no_action",
                f"hvac_mode='{c.hvac_mode}' — no setback needed",
                self.state.hvac_mode,
                self.state.fan_mode,
            )
            self.decisions.append(d)
            return d

        self.state.hvac_target_temp = target
        d = Decision(
            ts,
            "occupancy_away",
            "setback_applied",
            reason,
            self.state.hvac_mode,
            self.state.fan_mode,
            target_temp=target,
        )
        self.decisions.append(d)
        return d

    def _handle_occupancy_home(self, ts: str) -> Decision:
        """Restore comfort temperature — mirrors handle_occupancy_home()."""
        self.state.occupancy = "home"
        c = self.state.classification
        if not c:
            d = Decision(
                ts,
                "occupancy_home",
                "no_action",
                "no classification — restore skipped",
                self.state.hvac_mode,
                self.state.fan_mode,
            )
            self.decisions.append(d)
            return d

        if c.hvac_mode == "heat":
            target = float(self.config.get("comfort_heat", 70))
            reason = "occupancy home — restoring heat comfort"
        elif c.hvac_mode == "cool":
            target = float(self.config.get("comfort_cool", 75))
            reason = "occupancy home — restoring cool comfort"
        else:
            d = Decision(
                ts,
                "occupancy_home",
                "no_action",
                f"hvac_mode='{c.hvac_mode}' — no restore needed",
                self.state.hvac_mode,
                self.state.fan_mode,
            )
            self.decisions.append(d)
            return d

        self.state.hvac_target_temp = target
        d = Decision(
            ts,
            "occupancy_home",
            "comfort_restored",
            reason,
            self.state.hvac_mode,
            self.state.fan_mode,
            target_temp=target,
        )
        self.decisions.append(d)
        return d

    def _handle_occupancy_vacation(self, ts: str) -> Decision:
        """Apply vacation (deeper) setback — mirrors handle_occupancy_vacation()."""
        self.state.occupancy = "vacation"
        c = self.state.classification
        if not c:
            d = Decision(
                ts,
                "occupancy_vacation",
                "no_action",
                "no classification — setback skipped",
                self.state.hvac_mode,
                self.state.fan_mode,
            )
            self.decisions.append(d)
            return d

        if c.hvac_mode == "heat":
            target = float(self.config.get("setback_heat", 60)) + c.setback_modifier - VACATION_SETBACK_EXTRA
            reason = (
                f"vacation — deep heat setback"
                f" (base {self.config.get('setback_heat', 60)}"
                f" + modifier {c.setback_modifier}"
                f" - vacation {VACATION_SETBACK_EXTRA})"
            )
        elif c.hvac_mode == "cool":
            target = float(self.config.get("setback_cool", 80)) - c.setback_modifier + VACATION_SETBACK_EXTRA
            reason = (
                f"vacation — deep cool setback"
                f" (base {self.config.get('setback_cool', 80)}"
                f" - modifier {c.setback_modifier}"
                f" + vacation {VACATION_SETBACK_EXTRA})"
            )
        else:
            d = Decision(
                ts,
                "occupancy_vacation",
                "no_action",
                f"hvac_mode='{c.hvac_mode}' — no setback needed",
                self.state.hvac_mode,
                self.state.fan_mode,
            )
            self.decisions.append(d)
            return d

        self.state.hvac_target_temp = target
        d = Decision(
            ts,
            "occupancy_vacation",
            "setback_applied",
            reason,
            self.state.hvac_mode,
            self.state.fan_mode,
            target_temp=target,
        )
        self.decisions.append(d)
        return d

    # ------------------------------------------------------------------
    # Bedtime / wakeup handlers
    # ------------------------------------------------------------------

    def _compute_bedtime_setback(self, c: SimClassification) -> float | None:
        """Compute bedtime setback target using default depth (no thermal model).

        Mirrors the no-thermal-model fallback path of compute_bedtime_setback()
        in automation.py. Uses DEFAULT_SETBACK_DEPTH_F for heat (4°F) and
        DEFAULT_SETBACK_DEPTH_COOL_F for cool (3°F), clamped to setback bounds.
        """
        if c.hvac_mode == "heat":
            comfort = float(self.config.get("comfort_heat", 70))
            floor = float(self.config.get("setback_heat", 60))
            raw = comfort - DEFAULT_SETBACK_DEPTH_F + c.setback_modifier
            return max(raw, floor)
        if c.hvac_mode == "cool":
            comfort = float(self.config.get("comfort_cool", 75))
            ceiling = float(self.config.get("setback_cool", 80))
            raw = comfort + DEFAULT_SETBACK_DEPTH_COOL_F + c.setback_modifier
            return min(raw, ceiling)
        return None

    def _handle_bedtime(self, ts: str) -> Decision:
        """Apply bedtime setback + deactivate fan — mirrors handle_bedtime().

        May produce two Decision entries at the same timestamp:
          1. fan_off (if fan was active) — appended first
          2. setback_applied or no_action — appended last

        _outcome_at() returns the LAST matching decision, so asserting at
        the bedtime timestamp returns setback_applied (or no_action).
        """
        self.state.manual_override_active = False  # clear_manual_override()

        # Deactivate fan if running (mirrors the bedtime fan-off logic)
        if self.state.fan_active:
            self.state.fan_active = False
            self.state.fan_min_runtime_active = False
            self.state.fan_mode = "auto"
            fan_d = Decision(
                ts,
                "bedtime",
                "fan_off",
                "bedtime — fan off for night",
                self.state.hvac_mode,
                self.state.fan_mode,
            )
            self.decisions.append(fan_d)

        # Deactivate economizer silently (mirrors _deactivate_economizer; no Decision)
        if self.state.economizer_active:
            self.state.economizer_active = False
            self.state.economizer_phase = "inactive"

        c = self.state.classification
        if not c:
            d = Decision(
                ts,
                "bedtime",
                "no_action",
                "no classification — bedtime setback skipped",
                self.state.hvac_mode,
                self.state.fan_mode,
            )
            self.decisions.append(d)
            return d

        target = self._compute_bedtime_setback(c)
        if target is None:
            d = Decision(
                ts,
                "bedtime",
                "no_action",
                f"hvac_mode='{c.hvac_mode}' — no setback needed",
                self.state.hvac_mode,
                self.state.fan_mode,
            )
            self.decisions.append(d)
            return d

        self.state.hvac_target_temp = target
        reason = f"bedtime — {'heat' if c.hvac_mode == 'heat' else 'cool'} setback (default depth, no thermal model)"
        d = Decision(
            ts,
            "bedtime",
            "setback_applied",
            reason,
            self.state.hvac_mode,
            self.state.fan_mode,
            target_temp=target,
        )
        self.decisions.append(d)
        return d

    def _handle_wakeup(self, ts: str) -> Decision:
        """Restore comfort at morning wakeup — mirrors handle_morning_wakeup().

        May produce two Decision entries: fan_off (if fan active) then comfort_restored.
        _outcome_at() returns comfort_restored as the dominant outcome.
        """
        self.state.manual_override_active = False  # clear_manual_override()

        # Deactivate fan if still running from overnight
        if self.state.fan_active:
            self.state.fan_active = False
            self.state.fan_min_runtime_active = False
            self.state.fan_mode = "auto"
            fan_d = Decision(
                ts,
                "wakeup",
                "fan_off",
                "morning wakeup — resetting fan state",
                self.state.hvac_mode,
                self.state.fan_mode,
            )
            self.decisions.append(fan_d)

        c = self.state.classification
        if not c:
            d = Decision(
                ts,
                "wakeup",
                "no_action",
                "no classification — wakeup restore skipped",
                self.state.hvac_mode,
                self.state.fan_mode,
            )
            self.decisions.append(d)
            return d

        if c.hvac_mode == "heat":
            target = float(self.config.get("comfort_heat", 70))
            reason = "morning wake-up — restoring heat comfort"
        elif c.hvac_mode == "cool":
            target = float(self.config.get("comfort_cool", 75))
            reason = "morning wake-up — restoring cool comfort"
        else:
            d = Decision(
                ts,
                "wakeup",
                "no_action",
                f"hvac_mode='{c.hvac_mode}' — no restore needed",
                self.state.hvac_mode,
                self.state.fan_mode,
            )
            self.decisions.append(d)
            return d

        self.state.hvac_target_temp = target
        d = Decision(
            ts,
            "wakeup",
            "comfort_restored",
            reason,
            self.state.hvac_mode,
            self.state.fan_mode,
            target_temp=target,
        )
        self.decisions.append(d)
        return d

    # ------------------------------------------------------------------
    # Fan cycling handlers (Issue #77)
    # ------------------------------------------------------------------

    def _handle_fan_cycle_on(self, ts: str) -> Decision:
        """Fan min-runtime cycle on phase — mirrors _fan_cycle_on()."""
        fan_mode_cfg = self.config.get("fan_mode", "disabled")
        min_runtime = int(self.config.get("fan_min_runtime_per_hour", 0))

        if fan_mode_cfg == "disabled" or min_runtime <= 0:
            d = Decision(
                ts,
                "fan_cycle_on",
                "no_action",
                "fan_mode disabled or fan_min_runtime_per_hour=0",
                self.state.hvac_mode,
                self.state.fan_mode,
            )
            self.decisions.append(d)
            return d

        if self.state.fan_active:
            # Fan already running — production retries after 60 min; simulator records no_action
            d = Decision(
                ts,
                "fan_cycle_on",
                "no_action",
                "fan already active — cycle-on skipped",
                self.state.hvac_mode,
                self.state.fan_mode,
            )
            self.decisions.append(d)
            return d

        self.state.fan_active = True
        self.state.fan_min_runtime_active = True
        self.state.fan_mode = "on"
        d = Decision(
            ts,
            "fan_cycle_on",
            "fan_cycle_on",
            f"fan min-runtime cycle on (min_runtime={min_runtime} min/hr)",
            self.state.hvac_mode,
            self.state.fan_mode,
        )
        self.decisions.append(d)
        return d

    def _handle_fan_cycle_off(self, ts: str) -> Decision:
        """Fan min-runtime cycle off phase — mirrors _fan_cycle_off()."""
        if not self.state.fan_min_runtime_active:
            d = Decision(
                ts,
                "fan_cycle_off",
                "no_action",
                "fan_cycle_off received but fan_min_runtime_active=False — no state change",
                self.state.hvac_mode,
                self.state.fan_mode,
            )
            self.decisions.append(d)
            return d

        self.state.fan_min_runtime_active = False
        self.state.fan_active = False
        self.state.fan_mode = "auto"
        d = Decision(
            ts,
            "fan_cycle_off",
            "fan_cycle_off",
            "fan min-runtime cycle complete — fan off",
            self.state.hvac_mode,
            self.state.fan_mode,
        )
        self.decisions.append(d)
        return d

    # ------------------------------------------------------------------
    # Economizer handler (Issue #27)
    # ------------------------------------------------------------------

    def _handle_economizer_check(self, ts: str, event: dict) -> Decision:
        """Evaluate economizer eligibility — mirrors check_window_cooling_opportunity().

        Event fields:
          outdoor_temp  (float, required)
          indoor_temp   (float, optional)
          windows_open  (bool, default False)
          hour          (int, default -1; use -1 to skip the time-window gate)
        """
        outdoor_temp = float(event["outdoor_temp"])
        indoor_temp = event.get("indoor_temp")
        if indoor_temp is not None:
            indoor_temp = float(indoor_temp)
            self.state.indoor_temp = indoor_temp
        self.state.outdoor_temp = outdoor_temp
        windows_open = bool(event.get("windows_open", False))
        hour = int(event.get("hour", -1))

        c = self.state.classification

        # Guard: classification required and day must be hot
        if not c or c.day_type != "hot":
            if self.state.economizer_active:
                self.state.economizer_active = False
                self.state.economizer_phase = "inactive"
                d = Decision(
                    ts,
                    "economizer_check",
                    "economizer_disengaged",
                    "classification not hot — economizer deactivated",
                    self.state.hvac_mode,
                    self.state.fan_mode,
                )
                self.decisions.append(d)
                return d
            d = Decision(
                ts,
                "economizer_check",
                "no_action",
                f"day_type='{c.day_type if c else 'none'}' — economizer only activates on hot days",
                self.state.hvac_mode,
                self.state.fan_mode,
            )
            self.decisions.append(d)
            return d

        comfort_cool = float(self.config.get("comfort_cool", 75))
        delta = float(self.config.get("economizer_temp_delta", 3.0))
        aggressive_savings = bool(self.config.get("aggressive_savings", False))

        # Time gate: morning 6–9, evening 17–24; hour=-1 skips gate
        in_window = True if hour < 0 else (6 <= hour < 9) or (17 <= hour < 24)

        eligible = windows_open and outdoor_temp <= comfort_cool + delta and in_window

        if not eligible:
            if self.state.economizer_active:
                self.state.economizer_active = False
                self.state.economizer_phase = "inactive"
                self.state.fan_active = False
                self.state.fan_mode = "auto"
                self.state.hvac_mode = "cool"
                self.state.hvac_target_temp = comfort_cool
                d = Decision(
                    ts,
                    "economizer_check",
                    "economizer_disengaged",
                    f"conditions ineligible (outdoor={outdoor_temp}, windows={windows_open}, hour={hour})",
                    self.state.hvac_mode,
                    self.state.fan_mode,
                )
                self.decisions.append(d)
                return d
            d = Decision(
                ts,
                "economizer_check",
                "no_action",
                (
                    f"economizer ineligible"
                    f" (outdoor={outdoor_temp}, threshold={comfort_cool + delta},"
                    f" windows={windows_open}, hour={hour})"
                ),
                self.state.hvac_mode,
                self.state.fan_mode,
            )
            self.decisions.append(d)
            return d

        # Eligible — engage
        self.state.economizer_active = True

        if aggressive_savings:
            self.state.economizer_phase = "maintain"
            self.state.hvac_mode = "off"
            self.state.fan_active = True
            self.state.fan_mode = "on"
            d = Decision(
                ts,
                "economizer_check",
                "economizer_engaged",
                f"economizer (savings mode) — HVAC off, fan ventilating; outdoor={outdoor_temp}",
                self.state.hvac_mode,
                self.state.fan_mode,
            )
            self.decisions.append(d)
            return d

        # Comfort mode two-phase
        if indoor_temp is not None and indoor_temp > comfort_cool:
            self.state.economizer_phase = "cool-down"
            self.state.hvac_mode = "cool"
            self.state.hvac_target_temp = comfort_cool
            reason = (
                f"economizer cool-down — indoor={indoor_temp} > comfort={comfort_cool},"
                f" outdoor={outdoor_temp} assisting"
            )
        else:
            self.state.economizer_phase = "maintain"
            self.state.hvac_mode = "off"
            self.state.fan_active = True
            self.state.fan_mode = "on"
            reason = (
                f"economizer maintain — indoor at/below comfort={comfort_cool},"
                f" HVAC off, fan ventilating; outdoor={outdoor_temp}"
            )

        d = Decision(
            ts,
            "economizer_check",
            "economizer_engaged",
            reason,
            self.state.hvac_mode,
            self.state.fan_mode,
            target_temp=self.state.hvac_target_temp,
        )
        self.decisions.append(d)
        return d


# ------------------------------------------------------------------
# Scenario I/O
# ------------------------------------------------------------------


def _find_scenario(name: str) -> tuple[Path, str] | None:
    """Find a scenario file by name across all state directories."""
    for state, d in STATE_DIRS.items():
        p = d / f"{name}.json"
        if p.exists():
            return p, state
    return None


def _outcome_at(decisions: list[Decision], iso_time: str) -> str:
    """Return the most recent outcome at or before iso_time."""
    matching = [d for d in decisions if d.time <= iso_time]
    return matching[-1].outcome if matching else "no_decision"


def _temp_at(decisions: list[Decision], iso_time: str) -> float | None:
    """Return the target_temp from the most recent decision at or before iso_time."""
    matching = [d for d in decisions if d.time <= iso_time]
    return matching[-1].target_temp if matching else None


def run_scenario(scenario_file: Path, state: str | None = None) -> dict:
    """Run a single scenario file and return a results dict."""
    with open(scenario_file) as f:
        scenario = json.load(f)

    sim = ClimateSimulator(scenario.get("config", {}))
    for event in sorted(scenario.get("events", []), key=lambda e: e["time"]):
        sim.process_event(event)

    assertion_results = []
    any_real_assertion = False
    for a in scenario.get("assertions", []):
        if a.get("simulator_support") is False:
            assertion_results.append(
                {
                    "at": a["at"],
                    "expected": a.get("expect", ""),
                    "actual": None,
                    "pass": None,
                    "skipped": True,
                    "reason": "event type not simulated",
                }
            )
            continue

        any_real_assertion = True
        actual_outcome = _outcome_at(sim.decisions, a["at"])
        outcome_ok = actual_outcome == a["expect"]

        # Optional temperature assertion
        temp_pass = True
        temp_detail = None
        if "expect_temp" in a:
            actual_temp = _temp_at(sim.decisions, a["at"])
            expected_temp = float(a["expect_temp"])
            if actual_temp is None:
                temp_pass = False
                temp_detail = f"expect_temp={expected_temp} but no target_temp recorded"
            else:
                temp_pass = abs(actual_temp - expected_temp) < 0.01
                temp_detail = f"expect_temp={expected_temp}, actual_temp={actual_temp}"

        overall_pass = outcome_ok and temp_pass
        assertion_results.append(
            {
                "at": a["at"],
                "expected": a["expect"],
                "actual": actual_outcome,
                "pass": overall_pass,
                "skipped": False,
                "reason": a.get("reason", ""),
                "temp_detail": temp_detail,
            }
        )

    real_results = [r for r in assertion_results if not r.get("skipped")]
    passed = all(r["pass"] for r in real_results) if any_real_assertion else None

    return {
        "name": scenario.get("name", scenario_file.stem),
        "description": scenario.get("description", ""),
        "issue": scenario.get("issue"),
        "verdict": scenario.get("verdict"),
        "state": state,
        "decisions": [
            {
                "time": d.time,
                "outcome": d.outcome,
                "reason": d.reason,
                "target_temp": d.target_temp,
            }
            for d in sim.decisions
        ],
        "assertions": assertion_results,
        "passed": passed,
    }


# ------------------------------------------------------------------
# Output formatting
# ------------------------------------------------------------------


def _status_label(result: dict) -> str:
    """Return the display status string for a result, accounting for pending-fix expected fails."""
    passed = result["passed"]
    state = result.get("state")
    verdict_raw = result.get("verdict")
    verdict = verdict_raw if isinstance(verdict_raw, dict) else {}
    verdict_type = verdict.get("type")

    if state == "pending-fix" and verdict_type == "negative":
        if passed is False:
            return "EXPECTED FAIL"
        if passed is True:
            return "PASS"

    if passed is True:
        return "PASS"
    if passed is False:
        return "FAIL"
    return "SKIP (no assertions)"


def print_result(result: dict, verbose: bool = False) -> None:
    """Print simulation result in human-readable form."""
    status = _status_label(result)
    verdict_raw = result.get("verdict")
    verdict = verdict_raw if isinstance(verdict_raw, dict) else {}

    issue_tag = f" [#{result['issue']}]" if result.get("issue") else ""
    print(f"\n{'=' * 60}")
    print(f"Scenario: {result['name']}{issue_tag}")
    print(f"  {result['description']}")

    if verdict:
        verdict_type = verdict.get("type", "")
        summary = verdict.get("summary", "")
        print(f"  Verdict: {verdict_type} — {summary}")

    print(f"  Status: {status}")

    if status == "PASS" and result.get("state") == "pending-fix":
        print("  NOTE: pending-fix scenario now passes — consider promoting to golden/")

    if verbose and result["decisions"]:
        print("\nDecision timeline:")
        for d in result["decisions"]:
            temp_suffix = f"  → {d['target_temp']}°F" if d.get("target_temp") is not None else ""
            print(f"  {d['time']}  [{d['outcome']}]{temp_suffix}  {d['reason']}")

    if result["assertions"]:
        print("\nAssertions:")
        for a in result["assertions"]:
            if a.get("skipped"):
                print(f"  [SKIP] at {a['at']}: {a['reason']}")
            elif a["pass"]:
                print(f"  [OK]   at {a['at']}: expected={a['expected']!r} actual={a['actual']!r}")
                if a.get("temp_detail"):
                    print(f"         temp: {a['temp_detail']}")
            else:
                print(f"  [FAIL] at {a['at']}: expected={a['expected']!r} actual={a['actual']!r}")
                if a.get("temp_detail"):
                    print(f"         temp: {a['temp_detail']}")
                if a["reason"]:
                    print(f"         {a['reason']}")


# ------------------------------------------------------------------
# Cases summary
# ------------------------------------------------------------------


def print_cases_summary() -> None:
    """Scan all directories and print a summary table of all scenarios."""
    print("\nSCENARIO CASE SUMMARY")
    print("======================")

    for state, state_dir in STATE_DIRS.items():
        if not state_dir.exists():
            continue
        files = sorted(state_dir.glob("*.json"))
        if not files:
            continue

        print(f"\n{state.upper()} ({len(files)} scenario{'s' if len(files) != 1 else ''})")

        for f in files:
            try:
                result = run_scenario(f, state=state)
            except (json.JSONDecodeError, OSError, KeyError):
                print(f"  [ERROR] {f.stem}: unreadable or invalid")
                continue

            status = _status_label(result)
            verdict_raw = result.get("verdict")
            verdict = verdict_raw if isinstance(verdict_raw, dict) else {}
            verdict_type = verdict.get("type", "")
            issue_tag = f" #{result['issue']}" if result.get("issue") else ""

            verdict_tag = f" [{verdict_type}]" if verdict_type else ""
            print(f"  [{status}]{verdict_tag} {result['name']}{issue_tag}")
            print(f"         {result['description']}")

            if verdict_type == "negative":
                observed = verdict.get("observed_behavior", "")
                expected = verdict.get("expected_behavior", "")
                if observed:
                    print(f"         Observed: {observed}")
                if expected:
                    print(f"         Expected: {expected}")


# ------------------------------------------------------------------
# CLI
# ------------------------------------------------------------------


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Climate Advisor behavior simulator",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "-s", "--scenario", metavar="NAME", help="Run a specific scenario by name (searches all state dirs)"
    )
    parser.add_argument("--pending", action="store_true", help="Run all pending scenarios (for review)")
    parser.add_argument("--list", action="store_true", dest="list_all", help="List all scenarios by state")
    parser.add_argument("--cases", action="store_true", help="Show summary table of all scenarios across all states")
    parser.add_argument("-v", "--verbose", action="store_true", help="Show full decision timeline for each scenario")
    args = parser.parse_args()

    for d in STATE_DIRS.values():
        d.mkdir(parents=True, exist_ok=True)

    # Cases summary mode
    if args.cases:
        print_cases_summary()
        return 0

    # List mode
    if args.list_all:
        for state, d in STATE_DIRS.items():
            files = sorted(d.glob("*.json"))
            if files:
                print(f"\n{state.upper()} ({len(files)}):")
                for f in files:
                    try:
                        with open(f) as fh:
                            s = json.load(fh)
                        desc = s.get("description", "")[:70]
                        issue = f" [#{s['issue']}]" if s.get("issue") else ""
                        verdict_raw = s.get("verdict")
                        verdict = verdict_raw if isinstance(verdict_raw, dict) else {}
                        verdict_tag = f" [{verdict['type']}]" if verdict.get("type") else ""
                        print(f"  {f.stem}{issue}{verdict_tag}: {desc}")
                    except (json.JSONDecodeError, OSError):
                        print(f"  {f.stem}: [unreadable]")
        return 0

    # Single scenario
    if args.scenario:
        found = _find_scenario(args.scenario)
        if not found:
            print(f"Scenario not found: {args.scenario}")
            print("Available:")
            for state, d in STATE_DIRS.items():
                for f in sorted(d.glob("*.json")):
                    print(f"  [{state}] {f.stem}")
            return 1
        scenario_path, scenario_state = found
        result = run_scenario(scenario_path, state=scenario_state)
        print_result(result, verbose=args.verbose)
        status = _status_label(result)
        return 0 if status != "FAIL" else 1

    # Run a batch (golden by default, pending with --pending)
    source_key = "pending" if args.pending else "golden"
    source_dir = STATE_DIRS[source_key]
    files = sorted(source_dir.glob("*.json")) if source_dir.exists() else []

    if not files:
        print(f"No {source_key} scenarios found.")
        if source_key == "golden":
            print("  Promote a scenario from pending/ to golden/ after review.")
        return 0

    results = [run_scenario(f, state=source_key) for f in files]
    for r in results:
        print_result(r, verbose=args.verbose)

    total = len(results)
    passed = sum(1 for r in results if r["passed"] is True)
    failed = sum(1 for r in results if _status_label(r) == "FAIL")
    skipped = sum(1 for r in results if r["passed"] is None)

    print(f"\n{'=' * 60}")
    summary = f"{passed}/{total} {source_key} scenarios passed"
    if failed:
        summary += f" — {failed} FAILED"
    if skipped:
        summary += f" — {skipped} skipped (no assertions)"
    print(summary)
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
