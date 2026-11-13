"""HVAC profiles for the simulated thermostat in real-HA tests (owner request 2026-11-12).

``SimProfile`` turns a readable equipment description (rate, precision, companion sensor, sensor lag, blower
overrun / lead-in, coast) into the ``ca_dev_thermostat_sim`` entry keys, for ``build_house(sim_config=...)`` or the
``make_sim`` fixture (tests_ha/conftest.py)::

    house = await make_sim(profile=SimProfile(heat_rate="normal", companion_sensor=True, blower_overrun_heat_min=2))
    sim = sim_entity(house)

A default ``SimProfile()`` adds no keys, so the thermostat is exactly the one ``build_house`` makes today. Rates are
gross capacity in F/h: a preset name (``undersized`` 2, ``extreme_weather`` 5, ``normal`` 12, ``oversized`` 18) or a
number. The net climb is that minus the envelope loss, ``|k_passive| x (indoor - outdoor)`` (0.15 x gap at
``build_house``'s closed-loop k_passive).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .builder import CLIMATE_ENTITY, SIM_DOMAIN

# The companion sensor sits on the thermostat's device ("House Thermostat") and is named "Temperature".
COMPANION_ENTITY = "sensor.house_thermostat_temperature"
RATE_PRESETS = ("undersized", "extreme_weather", "normal", "oversized")


@dataclass(frozen=True)
class SimProfile:
    """One simulated HVAC system. ``None`` / 0 / False fields leave the simulator's default."""

    heat_rate: float | str | None = None  # preset name or F/h
    cool_rate: float | str | None = None  # preset name or F/h (a magnitude; cooling is applied as negative)
    precision: str | None = None  # "whole" / "half" / "tenth" / "default"
    companion_sensor: bool = False
    sensor_lag_min: float = 0.0
    blower_lead_in_heat_min: float = 0.0
    blower_lead_in_cool_min: float = 0.0
    blower_overrun_heat_min: float = 0.0
    blower_overrun_cool_min: float = 0.0
    coast_heat_rate_f_per_h: float = 0.0
    coast_heat_tau_min: float = 0.0
    coast_cool_rate_f_per_h: float = 0.0
    coast_cool_tau_min: float = 0.0
    edge_report_delay_s: float = 0.0  # hvac_action reported this late (a cloud-polled thermostat); 0 = on time
    extra: dict[str, Any] = field(default_factory=dict)  # any other sim entry key (deadband_heat_f, ...)

    @classmethod
    def preset(cls, name: str, **kwargs: Any) -> SimProfile:
        """Both directions at one rate preset."""
        return cls(heat_rate=name, cool_rate=name, **kwargs)

    def to_sim_config(self) -> dict[str, Any]:
        """The ``ca_dev_thermostat_sim`` entry keys for this profile (only the non-default ones)."""
        out: dict[str, Any] = {}
        for direction, rate in (("heat", self.heat_rate), ("cool", self.cool_rate)):
            if rate is None:
                continue
            if isinstance(rate, str):
                assert rate in RATE_PRESETS, rate
                out[f"{direction}_rate_preset"] = rate
            else:
                out[f"{direction}_rate_preset"] = "custom"
                out[f"k_active_{direction}"] = abs(float(rate)) * (1 if direction == "heat" else -1)
        if self.precision is not None:
            assert self.precision in ("default", "whole", "half", "tenth"), self.precision
            out["precision"] = self.precision
        if self.companion_sensor:
            out["companion_sensor"] = True
        for key in (
            "sensor_lag_min",
            "blower_lead_in_heat_min",
            "blower_lead_in_cool_min",
            "blower_overrun_heat_min",
            "blower_overrun_cool_min",
            "coast_heat_rate_f_per_h",
            "coast_heat_tau_min",
            "coast_cool_rate_f_per_h",
            "coast_cool_tau_min",
            "edge_report_delay_s",
        ):
            if (value := getattr(self, key)) > 0:
                out[key] = float(value)
        out.update(self.extra)
        return out


def sim_entity(house: Any) -> Any:
    """The house's ``SimulatedThermostat`` entity object (read ``_current_temp`` for the air, ``hvac_action``...)."""
    return house.hass.data[SIM_DOMAIN][house.sim_entry.entry_id]["thermostat_entity"]


__all__ = ["CLIMATE_ENTITY", "COMPANION_ENTITY", "RATE_PRESETS", "SimProfile", "sim_entity"]
