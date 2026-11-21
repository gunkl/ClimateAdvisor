"""What accepting a learning suggestion changes (Issue #1652). Pure: no Home Assistant imports.

Occupant view: on the dashboard (or through ``climate_advisor.respond_to_suggestion``) the person accepts a
suggestion such as "door pauses are frequent". Before #1652 the card went away and nothing in the house changed,
because every suggestion mapped to keys no code reads (``door_pause_seconds``, ``setback_modifier``, ...). Now:

* An **actionable** suggestion changes a real setting (a ``CONFIG_METADATA`` key), computed from the zone's
  CURRENT value, by a fixed step, inside bounds, and never in the wrong direction. When the setting is already at
  its bound the change is empty and the suggestion is shown as advice (the dashboard then offers "Got it").
* An **advice-only** suggestion has no setting to change. Its text gives the advice; accepting it only records
  that the person saw it (the learner then holds it back for 30 days, as for any accepted suggestion).

Mapping (all temperatures internal Fahrenheit, as stored in the config entry):

=========================  =========================  ==================================================
suggestion                 setting                    change
=========================  =========================  ==================================================
``frequent_door_pauses``   ``sensor_debounce_seconds`` +5 min, up to 20 min; never shortened
``comfort_violations``     ``setback_heat``            up by 2 °F (1 °C on a Celsius zone), never above home
                                                       heat or sleep heat
                           ``setback_cool``            down by the same, never below home cool or sleep cool
=========================  =========================  ==================================================

The away band never ends up narrower than ``MIN_HEAT_COOL_GAP_F`` when it was not narrow already, and a band that
is already narrow or inverted is left alone (the setpoints page warns about it; Accept does not make it worse).
The rest (``low_window_compliance``, ``frequent_overrides``, ``short_departures``, ``high_runtime_mild_days``,
``thermal_model_ready``, ``forecast_bias_significant``) are advice-only: there is no setting for "stop window
tips", "analyze my overrides" or the fixed 15-minute away delay (``OCCUPANCY_SETBACK_MINUTES``), and the
pre-heat settings no code reads (#1377) are not used.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from . import i18n
from .const import (
    CONF_SENSOR_DEBOUNCE,
    CONF_SLEEP_COOL,
    CONF_SLEEP_HEAT,
    DEFAULT_COMFORT_COOL,
    DEFAULT_COMFORT_HEAT,
    DEFAULT_SENSOR_DEBOUNCE_SECONDS,
    DEFAULT_SETBACK_COOL,
    DEFAULT_SETBACK_HEAT,
    DEFAULT_SLEEP_COOL,
    DEFAULT_SLEEP_HEAT,
)
from .setpoint_combinations import COMPARE_EPSILON_F, MIN_HEAT_COOL_GAP_F

# frequent_door_pauses: how much one Accept lengthens the door/window debounce, and the most it ever sets.
# The cap is twice the default (600 s): owner rule 3 (HVAC off while a window is open) applies only after this
# debounce, so an automatic step never goes further than that; the Sensors page still allows up to 60 minutes.
DOOR_PAUSE_DEBOUNCE_STEP_SECONDS = 300
DOOR_PAUSE_DEBOUNCE_CAP_SECONDS = 1200

# comfort_violations: how much one Accept makes each away setpoint milder, per display unit (1 °C = 1.8 °F, so a
# Celsius zone's setpoints stay on whole/half degrees Celsius).
SETBACK_STEP_F = 2.0
SETBACK_STEP_C_IN_F = 1.8

# Suggestions whose Accept can change a setting (the change may still be empty at a bound).
ACTIONABLE_SUGGESTION_KEYS = frozenset({"frequent_door_pauses", "comfort_violations"})


def _num(config: Mapping[str, Any], key: str, default: float) -> float:
    value = config.get(key)
    if value is None or value == "":
        return float(default)
    try:
        return float(value)
    except TypeError, ValueError:
        return float(default)


def _round_f(value: float) -> float:
    # The options flow keeps stored °F at 4 dp (config_flow._display_default); do the same so 65.3 + 1.8 is 67.1.
    return round(value, 4)


def _door_pause_changes(config: Mapping[str, Any]) -> dict[str, Any]:
    current = int(_num(config, CONF_SENSOR_DEBOUNCE, DEFAULT_SENSOR_DEBOUNCE_SECONDS))
    if current >= DOOR_PAUSE_DEBOUNCE_CAP_SECONDS:
        return {}  # already at (or above, set by hand) the automatic cap: never shortened
    return {CONF_SENSOR_DEBOUNCE: min(current + DOOR_PAUSE_DEBOUNCE_STEP_SECONDS, DOOR_PAUSE_DEBOUNCE_CAP_SECONDS)}


def _comfort_violation_changes(config: Mapping[str, Any]) -> dict[str, Any]:
    step = SETBACK_STEP_C_IN_F if config.get("temp_unit") == "celsius" else SETBACK_STEP_F
    eps = COMPARE_EPSILON_F
    away_heat = _num(config, "setback_heat", DEFAULT_SETBACK_HEAT)
    away_cool = _num(config, "setback_cool", DEFAULT_SETBACK_COOL)
    heat_limit = min(
        _num(config, "comfort_heat", DEFAULT_COMFORT_HEAT), _num(config, CONF_SLEEP_HEAT, DEFAULT_SLEEP_HEAT)
    )
    cool_limit = max(
        _num(config, "comfort_cool", DEFAULT_COMFORT_COOL), _num(config, CONF_SLEEP_COOL, DEFAULT_SLEEP_COOL)
    )
    gap = away_cool - away_heat
    if gap < MIN_HEAT_COOL_GAP_F - eps:
        return {}  # already narrow or inverted: the setpoints page warns; Accept must not make it worse

    heat_move = max(0.0, min(step, heat_limit - away_heat))  # milder = up, never down, never past home/sleep
    cool_move = max(0.0, min(step, away_cool - cool_limit))  # milder = down, never up, never past home/sleep
    room = gap - MIN_HEAT_COOL_GAP_F
    if heat_move + cool_move > room:
        # Keep the away band at least MIN_HEAT_COOL_GAP_F wide: share what room is left between the two sides.
        total = heat_move + cool_move
        heat_move, cool_move = room * heat_move / total, room * cool_move / total

    changes: dict[str, Any] = {}
    if heat_move > eps:
        changes["setback_heat"] = _round_f(away_heat + heat_move)
    if cool_move > eps:
        changes["setback_cool"] = _round_f(away_cool - cool_move)
    return changes


def suggestion_setting_changes(suggestion_key: str, config: Mapping[str, Any] | None) -> dict[str, Any]:
    """The real settings (``CONFIG_METADATA`` keys) accepting ``suggestion_key`` would set, from ``config`` now.

    Empty for an advice-only suggestion, an unknown key, or an actionable one already at its bound.
    """
    cfg = config or {}
    if suggestion_key == "frequent_door_pauses":
        return _door_pause_changes(cfg)
    if suggestion_key == "comfort_violations":
        return _comfort_violation_changes(cfg)
    return {}


def suggestion_change_msgs(changes: Mapping[str, Any], before: Mapping[str, Any] | None) -> list[i18n.Msg]:
    """One message per changed setting, "<setting>: <before> → <after>", in the zone's unit (``before`` = config
    as it was before the change)."""
    cfg = before or {}
    unit = cfg.get("temp_unit", "fahrenheit")
    msgs: list[i18n.Msg] = []
    if CONF_SENSOR_DEBOUNCE in changes:
        msgs.append(
            i18n.Msg(
                "learning.change.sensor_debounce_seconds",
                before=round(_num(cfg, CONF_SENSOR_DEBOUNCE, DEFAULT_SENSOR_DEBOUNCE_SECONDS) / 60),
                after=round(float(changes[CONF_SENSOR_DEBOUNCE]) / 60),
            )
        )
    for key, default in (("setback_heat", DEFAULT_SETBACK_HEAT), ("setback_cool", DEFAULT_SETBACK_COOL)):
        if key in changes:
            decimals = 1 if unit == "celsius" else 0
            msgs.append(
                i18n.Msg(
                    f"learning.change.{key}",
                    before=i18n.Temp(_num(cfg, key, default), unit, decimals),
                    after=i18n.Temp(float(changes[key]), unit, decimals),
                )
            )
    return msgs
