"""Thermostat recalibration after a heating shutdown (Issue #1676): detect the false high reading and time it.

Occupant view: on some homes the furnace overheats and trips its high-limit safety, so the heating stops while the
room is still below the setpoint. In the same second the thermostat (an Ecobee on the reference home) starts
reporting a reading 6.7-7.4 F too high, holds it for 2-4 minutes, steps down and decays back over 9-42 minutes. The
thermostat's own reading and its finer companion sensor jump together, so swapping between them does not help; a
sensor elsewhere in the house (the bedroom) does not move. While that lasts Climate Advisor would read a warm room
that is not warm, and the readings would poison thermal learning.

This module is a pure state machine (its only Home Assistant dependency is the shared ``time_utils`` clock helper).
The coordinator owns one ``ThermostatRecal`` per zone and shares it with its ``AutomationEngine`` (like
``indoor_temp.CompanionSensors``), feeds it every reading of
the thermostat's side of the room (``IndoorTempReading.primary_value`` on the ``climate_fallback`` source) with the
thermostat's ``hvac_action``, and announces the transitions it returns. The resolver reads ``active`` to take the
comfort-facing reading from another configured sensor; thermal learning reads it to skip samples.

Rules (constants in ``const.py``, chosen from the reference home's history):

* starts when the reading rises by ``RECAL_JUMP_F`` or more from the previous reading, the two at most
  ``RECAL_JUMP_WINDOW_S`` apart, and the thermostat reported ``heating`` within ``RECAL_HEATING_RECENT_S`` (only
  ``hvac_action`` counts as heating). A real room cannot warm that fast (largest normal step 0.36 F, largest normal
  5-minute rise 1.44 F), and sun on the thermostat or a warm-up never did. ``baseline_f`` is the reading before the
  jump.
* ends ``settled`` once the reading has stayed within ``RECAL_SETTLE_MARGIN_F`` of ``baseline_f`` for
  ``RECAL_SETTLE_HOLD_S`` (a reading above it restarts the hold, which keeps a second jump inside one episode), or
  ``timed_out`` after ``RECAL_MAX_S``.
* a missing reading (None: unavailable, or implausible like the 32 F the thermostat reported for 3 s at one trip)
  changes nothing: the outage has its own announcement, and the reading before it stays the reference.
* the state lives in memory: a Home Assistant restart in the middle of an episode forgets it (the reading after the
  restart has no reference to jump from).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from .const import (
    RECAL_HEATING_RECENT_S,
    RECAL_JUMP_F,
    RECAL_JUMP_WINDOW_S,
    RECAL_MAX_S,
    RECAL_SETTLE_HOLD_S,
    RECAL_SETTLE_MARGIN_F,
)
from .time_utils import seconds_since

STARTED = "started"
SETTLED = "settled"
TIMED_OUT = "timed_out"


@dataclass(frozen=True)
class RecalStep:
    """One transition of the recalibration state: ``kind`` is ``started``, ``settled`` or ``timed_out``."""

    kind: str
    since: datetime
    baseline_f: float
    reading_f: float
    peak_f: float
    minutes: float = 0.0


class ThermostatRecal:
    """The recalibration state of one zone's thermostat; see the module docstring for the rules."""

    __slots__ = (
        "active",
        "baseline_f",
        "count",
        "last_at",
        "last_f",
        "last_heating_at",
        "peak_f",
        "settle_since",
        "since",
    )

    def __init__(self) -> None:
        self.active: bool = False
        self.since: datetime | None = None
        self.baseline_f: float | None = None
        self.peak_f: float | None = None
        self.settle_since: datetime | None = None
        self.last_f: float | None = None
        self.last_at: datetime | None = None
        self.last_heating_at: datetime | None = None
        self.count: int = 0  # episodes since the coordinator started

    def note_heating(self, now: datetime) -> None:
        """The thermostat reported ``heating`` at ``now`` (also used for the state a listener event leaves)."""
        self.last_heating_at = now

    def _heating_recent(self, now: datetime) -> bool:
        if self.last_heating_at is None:
            return False
        return 0.0 <= seconds_since(self.last_heating_at, now=now) <= RECAL_HEATING_RECENT_S

    def observe(self, now: datetime, reading_f: float | None, hvac_action: str | None) -> RecalStep | None:
        """Fold one reading of the thermostat in; return the transition it caused, else None."""
        if str(hvac_action).lower() == "heating":
            self.note_heating(now)
        if reading_f is None:
            return None
        if not self.active:
            return self._observe_idle(now, reading_f)
        return self._observe_active(now, reading_f)

    def _observe_idle(self, now: datetime, reading_f: float) -> RecalStep | None:
        last_f, last_at = self.last_f, self.last_at
        self.last_f, self.last_at = reading_f, now
        if last_f is None or last_at is None:
            return None
        gap_s = seconds_since(last_at, now=now)
        if reading_f - last_f < RECAL_JUMP_F or not 0.0 <= gap_s <= RECAL_JUMP_WINDOW_S:
            return None
        if not self._heating_recent(now):
            return None
        self.active = True
        self.since = now
        self.baseline_f = last_f
        self.peak_f = reading_f
        self.settle_since = None
        self.count += 1
        return RecalStep(STARTED, now, last_f, reading_f, reading_f)

    def _observe_active(self, now: datetime, reading_f: float) -> RecalStep | None:
        since, baseline = self.since or now, self.baseline_f if self.baseline_f is not None else reading_f
        self.peak_f = max(self.peak_f if self.peak_f is not None else reading_f, reading_f)
        self.last_f, self.last_at = reading_f, now
        if reading_f <= baseline + RECAL_SETTLE_MARGIN_F:
            if self.settle_since is None:
                self.settle_since = now
            if seconds_since(self.settle_since, now=now) >= RECAL_SETTLE_HOLD_S:
                return self._end(SETTLED, now, reading_f)
        else:
            self.settle_since = None
        if seconds_since(since, now=now) >= RECAL_MAX_S:
            return self._end(TIMED_OUT, now, reading_f)
        return None

    def _end(self, kind: str, now: datetime, reading_f: float) -> RecalStep:
        since = self.since or now
        baseline = self.baseline_f if self.baseline_f is not None else reading_f
        peak = self.peak_f if self.peak_f is not None else reading_f
        self.active = False
        self.since = None
        self.baseline_f = None
        self.peak_f = None
        self.settle_since = None
        return RecalStep(kind, since, baseline, reading_f, peak, round(seconds_since(since, now=now) / 60.0, 1))
