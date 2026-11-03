"""world - a toy house and a day of weather for closed-loop random days (Issue #1128 I3).

This is a physical plant, NOT Climate Advisor logic.  It knows nothing about promises, comfort
bands, settling guards or any past bug: it answers "given what the thermostat is set to and the
weather outside, what does the indoor thermometer read five minutes later, and is the compressor
or furnace running?".  FakeHass has no house (the thermostat is perfectly obedient and indoor
temperature is whatever a scenario injects), so promises about equipment running and the house
responding (P1, P7) need this loop.

Everything is degrees F internally; the unit conversion for Celsius installs happens only at the
injection boundary in ``generator.py``.  Pure stdlib, no Home Assistant imports, deterministic for
a given ``random.Random``.
"""

from __future__ import annotations

import math
import random
from dataclasses import asdict, dataclass
from datetime import datetime

TICK_MIN = 5  # one world step = one thermal sample tick (production's heartbeat)

_F_PER_C = 1.8


@dataclass(frozen=True)
class BedroomParams:
    """The bedroom: a second thermal node, the room a REMOTE (sleep) sensor measures (Issue #1194 e).

    Physical plant only -- it knows nothing about Climate Advisor's offset loop.  The hallway thermostat
    never sees this room, so the room can sit below the sleep temperature while the hallway is satisfied.

    * Coupling: ``dB/dt = k_hall * (hallway - B) + k_env * (outdoor - B)`` (1/hr each).  The steady-state gap below
      the hallway is ``k_env / (k_hall + k_env) * (hallway - outdoor)``; the time constant is ``1 / (k_hall + k_env)``
      (the lag of the room behind the hallway).
    * Plume: while the hallway equipment runs, and for ``fan_overrun_min`` after it stops, the bedroom register
      blows conditioned air straight onto the sensor, so the READING (not the room) jumps by up to ``plume_f``
      (heat: +, cool: -).  It starts after ``plume_dead_min`` of running, rises first-order with
      ``plume_rise_tau_min`` toward the peak and decays with ``plume_decay_tau_min`` once the air stops.  Defaults are
      the POC v2 fit to 12 measured cycles of the owner's home (max error 0.82 F, under the sensor quantum).
    * Sensor: reports on change, in steps of ``sensor_step_c`` (0.1 C = 0.18 F), so ``last_reported`` can sit
      still for an hour or more while the room is steady.
    """

    k_hall_per_hr: float = 1.7
    k_env_per_hr: float = 0.28
    plume_f: float = 7.0
    plume_dead_min: float = 4.0
    plume_rise_tau_min: float = 8.0
    plume_decay_tau_min: float = 11.0
    fan_overrun_min: float = 2.0
    sensor_step_c: float = 0.1


class BedroomNode:
    """State of the bedroom: the room, the register plume, and what its sensor last REPORTED."""

    def __init__(self, params: BedroomParams, start_f: float) -> None:
        self.p = params
        self.room_f = float(start_f)  # the room itself, without the register plume
        self.plume_f = 0.0  # the register plume currently on the sensor (signed)
        self.clock_min = 0.0  # minutes since the node was created
        self.silent = False  # True: the sensor stopped reporting (battery, radio); its last report stays
        self._blow_min = 0  # consecutive minutes the register has been blowing (equipment running or coasting)
        self._overrun = 0  # blower-overrun minutes still to go after the equipment cut out
        self._sign = 1.0
        self.reported_f = self._quantise(self.room_f)
        self.last_reported_min = 0.0

    def _quantise(self, f: float) -> float:
        step_c = self.p.sensor_step_c
        if step_c <= 0:
            return f
        return round(round((f - 32.0) / _F_PER_C / step_c) * step_c * _F_PER_C + 32.0, 4)

    @property
    def age_min(self) -> float:
        """Minutes since the sensor last reported (what ``last_reported`` ages by)."""
        return self.clock_min - self.last_reported_min

    def step(self, minutes: float, hall_f: float, outdoor_f: float, running: str | None) -> bool:
        """Advance ``minutes`` (in sub-steps of at most a minute) under the hallway equipment state.

        Returns True if the sensor made a new report during the interval (``reported_f`` / ``last_reported_min``).
        """
        p = self.p
        reported = False
        remaining = float(minutes)
        while remaining > 1e-9:
            dt = min(1.0, remaining)
            remaining -= dt
            self.room_f += (p.k_hall_per_hr * (hall_f - self.room_f) + p.k_env_per_hr * (outdoor_f - self.room_f)) * (
                dt / 60.0
            )
            if running in ("heating", "cooling"):
                self._sign = 1.0 if running == "heating" else -1.0
                self._overrun = int(p.fan_overrun_min)
                blowing = True
            elif self._overrun > 0:
                self._overrun -= 1  # the blower coasts on after the burner / compressor cut out
                blowing = True
            else:
                blowing = False
            if blowing:
                self._blow_min += 1
                if self._blow_min > p.plume_dead_min:  # before that the air has not reached the sensor yet
                    self.plume_f += (self._sign * p.plume_f - self.plume_f) * (
                        1.0 - math.exp(-dt / p.plume_rise_tau_min)
                    )
            else:
                self._blow_min = 0
                self.plume_f *= math.exp(-dt / p.plume_decay_tau_min)
            self.clock_min += dt
            if not self.silent:
                new = self._quantise(self.room_f + self.plume_f)
                if abs(new - self.reported_f) > 1e-6:
                    self.reported_f = new
                    self.last_reported_min = self.clock_min
                    reported = True
        return reported


@dataclass(frozen=True)
class HouseParams:
    """First-order RC house plus a short-cycling thermostat (the physics behind #1066/#1073)."""

    k_env_per_hr: float  # envelope coupling to outdoors, 1/hr
    solar_f_hr: float  # peak solar gain, F/hr
    heat_gain_f_hr: float  # furnace capacity, F/hr
    cool_gain_f_hr: float  # compressor capacity, F/hr
    swing_f: float  # on/off hysteresis either side of the setpoint
    overshoot_f: float  # coast past the stop point after the equipment cuts out
    whole_degree: bool  # thermostat reports whole degrees (else 0.5)
    window_k_mult: float = 5.0  # envelope coupling multiplier with a door/window open
    # Issue #1234: asymmetric deadband. ``swing_f`` is where the equipment STARTS (heat: setpoint -
    # swing_f; cool: setpoint + swing_f). ``off_offset_f`` is where it STOPS, as a signed distance past
    # the setpoint in the direction of travel (heat stops at setpoint + off_offset_f, cool at setpoint -
    # off_offset_f), so a real thermostat that starts at setpoint-1.4 and stops at setpoint-0.9 is
    # swing_f=1.4, off_offset_f=-0.9. None keeps the original symmetric deadband (stop at +swing_f).
    off_offset_f: float | None = None
    # Issue #1194 (e): an optional second node, the bedroom a remote sensor reads.  None (the default, and what every
    # drawn day uses) keeps the single-zone house and serialises exactly as before.
    bedroom: BedroomParams | None = None

    def __post_init__(self) -> None:
        if isinstance(self.bedroom, dict):  # a plan / replay file round-trips the nested node as a plain dict
            object.__setattr__(self, "bedroom", BedroomParams(**self.bedroom))

    def to_dict(self) -> dict:
        d = asdict(self)
        if d.get("off_offset_f") is None:  # the symmetric default serialises exactly as before Issue #1234
            d.pop("off_offset_f", None)
        if d.get("bedroom") is None:  # ... and as before Issue #1194 (e)
            d.pop("bedroom", None)
        return d

    @classmethod
    def draw(cls, rng: random.Random) -> HouseParams:
        return cls(
            k_env_per_hr=round(rng.uniform(0.03, 0.16), 3),
            solar_f_hr=round(rng.uniform(0.0, 3.5), 2),
            heat_gain_f_hr=round(rng.uniform(2.5, 6.0), 2),
            cool_gain_f_hr=round(rng.uniform(2.5, 6.0), 2),
            swing_f=round(rng.choice([0.5, 0.5, 1.0]), 2),
            overshoot_f=round(rng.uniform(0.0, 0.8), 2),
            whole_degree=rng.random() < 0.5,
        )


class Weather:
    """A day (and the start of the next) of outdoor temperature and sunshine.

    ``high``/``low`` are the forecast numbers the classifier sees; the curve peaks mid-afternoon
    and bottoms out before dawn, with a slow mean-reverting wobble (fronts, gusts) and a cloud
    factor that damps solar gain.  Past midnight the curve blends toward tomorrow's high/low.
    """

    def __init__(
        self,
        rng: random.Random,
        *,
        high: float,
        low: float,
        tomorrow_high: float,
        tomorrow_low: float,
        cloud: float,
        wobble_f: float,
        day0: datetime,
    ) -> None:
        self._rng = rng
        self.high, self.low = high, low
        self.tomorrow_high, self.tomorrow_low = tomorrow_high, tomorrow_low
        self.cloud = cloud
        self._wobble_sd = wobble_f
        self._day0 = day0.replace(hour=0, minute=0, second=0, microsecond=0)
        self._wobble = 0.0

    @staticmethod
    def _smooth(x: float) -> float:
        x = min(1.0, max(0.0, x))
        return x * x * (3 - 2 * x)

    def _mean_amp(self, t: datetime) -> tuple[float, float]:
        hours = (t - self._day0).total_seconds() / 3600.0  # hours since local midnight of day 0
        w = self._smooth((hours - 22.0) / 4.0) if hours >= 22.0 else 0.0  # blend 22:00 -> 02:00
        if hours >= 26.0:
            w = 1.0
        hi = self.high * (1 - w) + self.tomorrow_high * w
        lo = self.low * (1 - w) + self.tomorrow_low * w
        return (hi + lo) / 2.0, (hi - lo) / 2.0

    def outdoor_f(self, t: datetime) -> float:
        mean, amp = self._mean_amp(t)
        hour = t.hour + t.minute / 60.0
        base = mean + amp * math.cos(2 * math.pi * (hour - 15.5) / 24.0)
        self._wobble = 0.9 * self._wobble + self._rng.gauss(0.0, self._wobble_sd)
        return base + self._wobble

    def solar_factor(self, t: datetime) -> float:
        hour = t.hour + t.minute / 60.0
        sun = max(0.0, math.sin(math.pi * (hour - 6.0) / 13.0)) if 6.0 <= hour <= 19.0 else 0.0
        return sun * (1.0 - self.cloud)


class ToyHouse:
    """Indoor temperature plus the thermostat's own on/off control."""

    def __init__(self, params: HouseParams, indoor_f: float, bedroom_f: float | None = None) -> None:
        self.p = params
        self.indoor_f = float(indoor_f)
        self.running: str | None = None  # "heating" | "cooling" | None
        # Issue #1194 (e): the bedroom node exists only when the house is given one (default: none, nothing changes).
        self.bedroom: BedroomNode | None = (
            BedroomNode(params.bedroom, float(indoor_f if bedroom_f is None else bedroom_f))
            if params.bedroom is not None
            else None
        )

    def reported_f(self) -> float:
        """What the thermostat displays (whole degree or half degree)."""
        return float(round(self.indoor_f)) if self.p.whole_degree else round(self.indoor_f * 2) / 2.0

    def decide_action(self, mode: str | None, setpoint_f: float | None) -> str:
        """The thermostat's own control loop; returns the ``hvac_action`` it would report.

        Single-setpoint heat / cool with hysteresis ``swing_f`` around the setpoint (start point;
        the stop point is ``off_offset_f`` when set, else the same ``swing_f`` on the far side);
        any other mode (off, fan_only, unavailable, ...) runs nothing.
        """
        shown = self.reported_f()
        stop_f = self.p.swing_f if self.p.off_offset_f is None else self.p.off_offset_f
        if mode not in ("heat", "cool") or setpoint_f is None:
            self.running = None
            return "off" if mode == "off" else "idle"
        if mode == "heat":
            if self.running == "heating":
                if shown >= setpoint_f + stop_f:
                    self.running = None
            elif shown <= setpoint_f - self.p.swing_f:
                self.running = "heating"
            else:
                self.running = None
        else:
            if self.running == "cooling":
                if shown <= setpoint_f - stop_f:
                    self.running = None
            elif shown >= setpoint_f + self.p.swing_f:
                self.running = "cooling"
            else:
                self.running = None
        return self.running or "idle"

    def step(self, minutes: float, outdoor_f: float, solar_factor: float, windows_open: bool) -> float:
        """Advance the house by ``minutes`` under the equipment state decided last tick."""
        dt = minutes / 60.0
        k = self.p.k_env_per_hr * (self.p.window_k_mult if windows_open else 1.0)
        delta = k * (outdoor_f - self.indoor_f) * dt + self.p.solar_f_hr * solar_factor * dt
        if self.running == "heating":
            delta += self.p.heat_gain_f_hr * dt
        elif self.running == "cooling":
            delta -= self.p.cool_gain_f_hr * dt
        self.indoor_f += delta
        if self.bedroom is not None:  # the second node follows the hallway under the same equipment state
            self.bedroom.step(minutes, self.indoor_f, outdoor_f, self.running)
        return self.indoor_f

    def coast(self, previous: str | None) -> None:
        """Equipment just cut out: the coil/duct heat carries the house a little further."""
        if previous == "heating" and self.running is None:
            self.indoor_f += self.p.overshoot_f
        elif previous == "cooling" and self.running is None:
            self.indoor_f -= self.p.overshoot_f
