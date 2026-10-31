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

    def to_dict(self) -> dict:
        return asdict(self)

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

    def __init__(self, params: HouseParams, indoor_f: float) -> None:
        self.p = params
        self.indoor_f = float(indoor_f)
        self.running: str | None = None  # "heating" | "cooling" | None

    def reported_f(self) -> float:
        """What the thermostat displays (whole degree or half degree)."""
        return float(round(self.indoor_f)) if self.p.whole_degree else round(self.indoor_f * 2) / 2.0

    def decide_action(self, mode: str | None, setpoint_f: float | None) -> str:
        """The thermostat's own control loop; returns the ``hvac_action`` it would report.

        Single-setpoint heat / cool with hysteresis ``swing_f`` around the setpoint; any other
        mode (off, fan_only, unavailable, ...) runs nothing.
        """
        shown = self.reported_f()
        if mode not in ("heat", "cool") or setpoint_f is None:
            self.running = None
            return "off" if mode == "off" else "idle"
        if mode == "heat":
            if self.running == "heating":
                if shown >= setpoint_f + self.p.swing_f:
                    self.running = None
            elif shown <= setpoint_f - self.p.swing_f:
                self.running = "heating"
            else:
                self.running = None
        else:
            if self.running == "cooling":
                if shown <= setpoint_f - self.p.swing_f:
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
        return self.indoor_f

    def coast(self, previous: str | None) -> None:
        """Equipment just cut out: the coil/duct heat carries the house a little further."""
        if previous == "heating" and self.running is None:
            self.indoor_f += self.p.overshoot_f
        elif previous == "cooling" and self.running is None:
            self.indoor_f -= self.p.overshoot_f
