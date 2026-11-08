"""Feels-like temperature for the Status charts (Issue #1414), by the National Weather Service methods.

Occupant view: on a 92 F afternoon at 60 % humidity the air feels like about 105 F, and a 30 F morning in a 20 mph wind
feels like about 17 F. The chart draws that next to the thermometer reading. CHART DISPLAY ONLY: no decision, setpoint
or control path reads anything here (pinned by ``tests/test_chart_humidity_1414.py``).

Heat index, NWS Weather Prediction Center (https://www.wpc.ncep.noaa.gov/html/heatindex_equation.shtml):

- First the simple formula ``HI = 0.5 * {T + 61.0 + [(T-68.0)*1.2] + (RH*0.094)}``, averaged with T.
- If that average is 80 F or more, the Rothfusz regression
  ``HI = -42.379 + 2.04901523*T + 10.14333127*RH - .22475541*T*RH - .00683783*T*T - .05481717*RH*RH
  + .00122874*T*T*RH + .00085282*T*RH*RH - .00000199*T*T*RH*RH``, adjusted by
  subtracting ``[(13-RH)/4]*SQRT{[17-ABS(T-95.)]/17}`` when RH < 13 % and 80 <= T <= 112 F, and adding
  ``[(RH-85)/10] * [(87-T)/5]`` when RH > 85 % and 80 <= T <= 87 F.
- Below that, NWS calls the regression "not appropriate"; this module returns None (no stand-in value).

Wind chill, NWS (https://www.weather.gov/safety/cold-wind-chill-chart): ``35.74 + 0.6215T - 35.75(V^0.16) +
0.4275T(V^0.16)``, T in F and V in mph, "only defined for temperatures at or below 50 F and wind speeds above 3 mph";
None otherwise.

Pure functions, no Home Assistant imports. Values are not rounded (native resolution).
"""

from __future__ import annotations

import math

_HI_REGRESSION_MIN_F = 80.0
_WC_MAX_TEMP_F = 50.0
_WC_MIN_WIND_MPH = 3.0

# Weather-entity ``wind_speed_unit`` strings (HA ``UnitOfSpeed``) -> factor to mph.
_TO_MPH: dict[str, float] = {
    "mph": 1.0,
    "km/h": 1.0 / 1.609344,
    "m/s": 3600.0 / 1609.344,
    "kn": 1852.0 / 1609.344,
    "ft/s": 3600.0 / 5280.0,
}


def _finite(v: object) -> float | None:
    if v is None or isinstance(v, bool):
        return None
    try:
        f = float(v)  # type: ignore[arg-type]
    except TypeError, ValueError:
        return None
    return f if math.isfinite(f) else None


def heat_index_f(temp_f: float, rh: float) -> float | None:
    """NWS heat index (F) when the full regression applies, else None (see module docstring)."""
    t = _finite(temp_f)
    h = _finite(rh)
    if t is None or h is None or not 0.0 <= h <= 100.0:
        return None
    simple = 0.5 * (t + 61.0 + ((t - 68.0) * 1.2) + (h * 0.094))
    if (simple + t) / 2.0 < _HI_REGRESSION_MIN_F:
        return None
    hi = (
        -42.379
        + 2.04901523 * t
        + 10.14333127 * h
        - 0.22475541 * t * h
        - 0.00683783 * t * t
        - 0.05481717 * h * h
        + 0.00122874 * t * t * h
        + 0.00085282 * t * h * h
        - 0.00000199 * t * t * h * h
    )
    if h < 13.0 and 80.0 <= t <= 112.0:
        hi -= ((13.0 - h) / 4.0) * math.sqrt((17.0 - abs(t - 95.0)) / 17.0)
    elif h > 85.0 and 80.0 <= t <= 87.0:
        hi += ((h - 85.0) / 10.0) * ((87.0 - t) / 5.0)
    return hi


def wind_chill_f(temp_f: float, wind_mph: float) -> float | None:
    """NWS wind chill (F) for T <= 50 F and wind > 3 mph, else None."""
    t = _finite(temp_f)
    v = _finite(wind_mph)
    if t is None or v is None or t > _WC_MAX_TEMP_F or v <= _WC_MIN_WIND_MPH:
        return None
    p = v**0.16
    return 35.74 + 0.6215 * t - 35.75 * p + 0.4275 * t * p


def feels_like_f(temp_f: float | None, rh: float | None, wind_mph: float | None = None) -> float | None:
    """The heat index when it applies, else the wind chill when ``wind_mph`` is given and it applies, else None.

    Indoor callers pass no wind (no wind chill indoors). A heat index needs a humidity; a wind chill does not.
    """
    t = _finite(temp_f)
    if t is None:
        return None
    if rh is not None:
        hi = heat_index_f(t, rh)
        if hi is not None:
            return hi
    if wind_mph is not None:
        return wind_chill_f(t, wind_mph)
    return None


def wind_speed_to_mph(value: object, unit: object) -> float | None:
    """A weather entity's ``wind_speed`` in its ``wind_speed_unit`` (mph, km/h, m/s, kn, ft/s) as mph.

    A missing or unknown unit -> None (never a guessed unit).
    """
    v = _finite(value)
    if v is None or v < 0 or not isinstance(unit, str):
        return None
    factor = _TO_MPH.get(unit)
    return v * factor if factor is not None else None
