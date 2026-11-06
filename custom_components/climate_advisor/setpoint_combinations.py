"""Setpoint combination checks and the vacation band resolver (Issue #1347).

Owner direction (2026-11-06): no hard limits on the values an occupant can enter beyond physical
sanity bounds. Instead the setpoints page WARNS when a combination would make the system switch
back and forth (heat and AC taking turns) or settle somewhere that does not make sense, and a
second identical submit saves it anyway (the #1144 two-pass pattern in ``config_flow.py``).

Pure module: no Home Assistant imports, every temperature internal Fahrenheit. The config flow
converts the submitted display values to °F, reads the thermostat's capabilities and limits, and
calls :func:`setpoint_combination_findings`.

Two band thresholds:

* **Inverted (heat at or above cool) - proven.** Tier-A generated-day harness (real production engine,
  ToyHouse, three seeds x two day types, 2026-11-06): heat<->cool switched 8-40 times a day (72/72
  equal: 8-10 on a warm day) against 0-3 for a normal 68/74 band.
* **Narrow (gap under ``MIN_HEAT_COOL_GAP_F`` = 4 °F) - owner decision, 2026-11-06.** Because it only
  warns, it is deliberately conservative. The harness ToyHouse models no equipment overshoot, so its
  result (extra flips only under a 1 °F gap) is NOT the basis. The basis is: below 3 °F the engine's own
  selector already loses interior switching (``comfort_family_decision.effective_margin()`` returns None
  under ``3 x COMFORT_MIN_USABLE_MARGIN_F``); thermostats commonly enforce 2-3 °F between heat and cool
  in auto mode (3 °F a common default); real compressors/furnaces overshoot and the learned swing
  defaults to 1.5 °F. 4 °F sits one degree above all of these. A test pins it above the engine's limit.
"""

from __future__ import annotations

from typing import Any

from .const import (
    CONF_VACATION_COOL,
    CONF_VACATION_HEAT,
    DEFAULT_SETBACK_COOL,
    DEFAULT_SETBACK_HEAT,
    VACATION_SETBACK_EXTRA,
)

# Physical sanity bounds for every setpoint field (°F). The only hard limit left on the setpoints
# page; the °C bounds are derived from these in config_flow.setpoint_slider_ranges().
SETPOINT_SANITY_MIN_F = 40.0
SETPOINT_SANITY_MAX_F = 95.0

# Warn when heat sits less than this far below cool in any band (see docstring; owner decision 2026-11-06).
MIN_HEAT_COOL_GAP_F = 4.0

# Vacation heat below this risks frozen pipes on a long trip (a safety advisory, not a thrash rule).
VACATION_FREEZE_ADVISORY_F = 45.0

# Comparisons tolerate this much so a °C value round-tripped through °F (18.5 °C = 65.3 °F) never
# reads as "warmer than" its own rounded display (Issue #1049 history).
COMPARE_EPSILON_F = 0.05

# Warning keys (config.error.* / options.error.* in strings.json). Every text ends with
# "Submit again to keep this." because HA renders these in the errors slot.
WARN_BAND_INVERTED = "setpoint_band_inverted_warning"
WARN_BAND_NARROW = "setpoint_band_narrow_warning"
WARN_OUTSIDE_THERMOSTAT = "setpoint_outside_thermostat_warning"
WARN_AWAY_HEAT_ABOVE_HOME = "away_heat_above_home_warning"
WARN_AWAY_COOL_BELOW_HOME = "away_cool_below_home_warning"
WARN_SLEEP_HEAT_BELOW_AWAY = "sleep_heat_below_away_warning"
WARN_SLEEP_COOL_ABOVE_AWAY = "sleep_cool_above_away_warning"
WARN_VACATION_HEAT_ABOVE_AWAY = "vacation_heat_above_away_warning"
WARN_VACATION_COOL_BELOW_AWAY = "vacation_cool_below_away_warning"
WARN_VACATION_FREEZE = "vacation_freeze_warning"

# Most severe first: when one field has several findings, the first in this list is shown.
WARNING_PRIORITY: tuple[str, ...] = (
    WARN_BAND_INVERTED,
    WARN_BAND_NARROW,
    WARN_OUTSIDE_THERMOSTAT,
    WARN_AWAY_HEAT_ABOVE_HOME,
    WARN_AWAY_COOL_BELOW_HOME,
    WARN_SLEEP_HEAT_BELOW_AWAY,
    WARN_SLEEP_COOL_ABOVE_AWAY,
    WARN_VACATION_HEAT_ABOVE_AWAY,
    WARN_VACATION_COOL_BELOW_AWAY,
    WARN_VACATION_FREEZE,
)

SETPOINT_FIELDS: tuple[str, ...] = (
    "comfort_heat",
    "comfort_cool",
    "setback_heat",
    "setback_cool",
    "sleep_heat",
    "sleep_cool",
    CONF_VACATION_HEAT,
    CONF_VACATION_COOL,
)


def _num(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except TypeError, ValueError:
        return None


def vacation_edges(config: dict) -> tuple[float, float]:
    """The vacation ``(heat, cool)`` setpoints in °F — the ONE place they are derived.

    The optional ``vacation_heat``/``vacation_cool`` when set, otherwise the away setpoint ∓
    ``VACATION_SETBACK_EXTRA`` (pre-#1347 behaviour, unchanged). Each side falls back on its own.
    Used by ``automation.select_comfort_band()``, ``scheduler`` TOU vacation phase and the chart.
    """
    heat = _num(config.get(CONF_VACATION_HEAT))
    cool = _num(config.get(CONF_VACATION_COOL))
    if heat is None:
        heat = float(config.get("setback_heat", DEFAULT_SETBACK_HEAT)) - VACATION_SETBACK_EXTRA
    if cool is None:
        cool = float(config.get("setback_cool", DEFAULT_SETBACK_COOL)) + VACATION_SETBACK_EXTRA
    return heat, cool


def setpoint_combination_findings(
    values_f: dict[str, Any],
    *,
    supports_heat: bool | None = None,
    supports_cool: bool | None = None,
    thermostat_min_f: float | None = None,
    thermostat_max_f: float | None = None,
) -> dict[str, str]:
    """Return ``{field: warning_key}`` for risky setpoint combinations; empty when all is coherent.

    ``values_f`` holds the setpoints in °F (missing keys are skipped; a missing vacation key means
    "derived from away"). ``supports_heat``/``supports_cool`` come from the thermostat; ``None``
    (unknown, e.g. entity unavailable) is treated as supported. The heat/cool band rules run only for
    a two-mode system — a heat-only or cool-only thermostat cannot flip between them. Heat-side
    ordering rules are skipped on a known cool-only system and cool-side rules on a known heat-only
    one. Warnings are keyed on the field the occupant would change; one per field, most severe first.
    """
    eps = COMPARE_EPSILON_F
    can_heat = supports_heat is not False
    can_cool = supports_cool is not False
    found: dict[str, list[str]] = {}

    def add(field: str, key: str) -> None:
        found.setdefault(field, []).append(key)

    v = {k: _num(values_f.get(k)) for k in SETPOINT_FIELDS}
    ch, cc = v["comfort_heat"], v["comfort_cool"]
    ah, ac = v["setback_heat"], v["setback_cool"]
    sh, sc = v["sleep_heat"], v["sleep_cool"]
    vh_set, vc_set = v[CONF_VACATION_HEAT], v[CONF_VACATION_COOL]
    vh = vh_set if vh_set is not None else (ah - VACATION_SETBACK_EXTRA if ah is not None else None)
    vc = vc_set if vc_set is not None else (ac + VACATION_SETBACK_EXTRA if ac is not None else None)

    # 1. Heat at or above cool, or too close, in each band (two-mode systems only).
    if can_heat and can_cool:
        bands = [("comfort_heat", ch, cc), ("setback_heat", ah, ac), ("sleep_heat", sh, sc)]
        if vh_set is not None or vc_set is not None:
            vac_field = CONF_VACATION_HEAT if vh_set is not None else CONF_VACATION_COOL
            bands.append((vac_field, vh, vc))
        for field, heat, cool in bands:
            if heat is None or cool is None:
                continue
            gap = cool - heat
            if gap <= eps:
                add(field, WARN_BAND_INVERTED)
            elif gap < MIN_HEAT_COOL_GAP_F - eps:
                add(field, WARN_BAND_NARROW)

    # 2. Outside what the thermostat accepts: it would hold a different temperature than commanded.
    for field in SETPOINT_FIELDS:
        value = v[field]
        if value is None:
            continue
        if (thermostat_min_f is not None and value < thermostat_min_f - eps) or (
            thermostat_max_f is not None and value > thermostat_max_f + eps
        ):
            add(field, WARN_OUTSIDE_THERMOSTAT)

    # 3. Ordering that makes the settled state incoherent (heat side, then the cool mirror).
    if can_heat:
        if ah is not None and ch is not None and ah > ch + eps:
            add("setback_heat", WARN_AWAY_HEAT_ABOVE_HOME)
        if sh is not None and ah is not None and ah > sh + eps:
            add("sleep_heat", WARN_SLEEP_HEAT_BELOW_AWAY)
        if vh_set is not None and ah is not None and vh_set > ah + eps:
            add(CONF_VACATION_HEAT, WARN_VACATION_HEAT_ABOVE_AWAY)
        if vh is not None and vh < VACATION_FREEZE_ADVISORY_F - eps:
            add(CONF_VACATION_HEAT if vh_set is not None else "setback_heat", WARN_VACATION_FREEZE)
    if can_cool:
        if ac is not None and cc is not None and ac < cc - eps:
            add("setback_cool", WARN_AWAY_COOL_BELOW_HOME)
        if sc is not None and ac is not None and ac < sc - eps:
            add("sleep_cool", WARN_SLEEP_COOL_ABOVE_AWAY)
        if vc_set is not None and ac is not None and vc_set < ac - eps:
            add(CONF_VACATION_COOL, WARN_VACATION_COOL_BELOW_AWAY)

    return {field: min(keys, key=WARNING_PRIORITY.index) for field, keys in found.items()}
