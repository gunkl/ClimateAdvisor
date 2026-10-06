<!-- Nav: ← [02-ARCHITECTURE-REFERENCE.md](02-ARCHITECTURE-REFERENCE.md) | → [temperature.py](../custom_components/climate_advisor/temperature.py) -->

# Temperature Conversion — Architecture Brief (Tier 2)

## Anchors

| Question | Short answer | → Full answer |
|---|---|---|
| When converting a temperature *rate* (e.g., k_active_heat in °F/hr) to Celsius for display, which function is correct? | `convert_delta()` or `format_temp_delta()` — these apply scale-only conversion (multiply by 5/9) without the +32/−32 offset. Using `from_fahrenheit()` on a delta is wrong. | [convert_delta and format_temp_delta](#convert_delta-and-format_temp_delta) |
| How is the user's display unit determined, and what is the fallback? | The unit is a user-selected config flow field (`CONF_TEMP_UNIT`), stored as `"fahrenheit"` or `"celsius"`. The fallback at every read site is `"fahrenheit"` via `config.get("temp_unit", "fahrenheit")`. | [Unit Detection](#unit-detection) |
| What happens when an unknown unit string (e.g., `"kelvin"`) is passed to any conversion function? | All functions treat unknown units as `"fahrenheit"` and return the value unchanged (passthrough). The `UNIT_SYMBOL` dict falls back to `"°F"` for unknown keys. | [Constants and Boundary Values](#constants-and-boundary-values) |
| Where is the canonical rule that all internal temperatures are stored in Fahrenheit? | Stated in the `temperature.py` module docstring: "All internal temperatures are stored and calculated in Fahrenheit. This module provides the only conversion boundary used throughout the integration." | [Scope](#scope) |
| Is the hourly forecast list in the provider's unit or internal °F, and how may a new consumer use it? | `_hourly_forecast_temps` is ALWAYS internal °F: `ClimateAdvisorCoordinator._get_hourly_forecast_data()` normalises a copy at the single source, and `_refresh_hourly_forecast()` is the only refresh path. New consumers must treat it as °F and never convert it again. | [Hourly forecast is normalised to internal °F at its source](#hourly-forecast-is-normalised-to-internal-f-at-its-source-issue-1015) |

## Scope

**Owns:**
- The single conversion boundary between internal °F and user-display units
- Four public symbols: `from_fahrenheit()`, `to_fahrenheit()`, `convert_delta()`, `format_temp_delta()`, `format_temp()`
- Two string constants: `FAHRENHEIT = "fahrenheit"`, `CELSIUS = "celsius"`
- One dict: `UNIT_SYMBOL` mapping unit strings to display symbols

**Explicitly does NOT own:**
- Unit detection or storage — `temp_unit` is read from `coordinator.config` by callers; this module is stateless
- Rendering decisions — callers choose when to use `format_temp` vs. raw `from_fahrenheit`
- Kelvin or any unit beyond `"fahrenheit"` / `"celsius"`

## Responsibilities

- Convert absolute temperatures from °F to the user's display unit (`from_fahrenheit`) and back (`to_fahrenheit`)
- Convert temperature *differences* and *rates* from °F scale to the user's display unit scale (`convert_delta`), without adding the offset that applies only to absolute temperatures
- Format an absolute temperature as a display string with symbol (`format_temp`)
- Format a temperature difference as a display string with symbol (`format_temp_delta`)

## Interfaces

```python
FAHRENHEIT: str = "fahrenheit"
CELSIUS: str = "celsius"
UNIT_SYMBOL: dict[str, str]  # {"fahrenheit": "°F", "celsius": "°C"}


def to_fahrenheit(value: float, unit: str) -> float:
    """Convert an absolute temperature from display unit to internal °F.
    Use at HA config read boundaries (user-entered setpoints in Celsius homes)."""


def from_fahrenheit(value: float, unit: str) -> float:
    """Convert an absolute temperature from internal °F to display unit.
    Use when displaying any stored temperature to the user."""


def format_temp(value_fahrenheit: float, unit: str, decimals: int = 0) -> str:
    """Format an internal °F temperature as a display string, e.g. '22°C'.
    Calls from_fahrenheit() internally."""


def convert_delta(value_fahrenheit: float, unit: str) -> float:
    """Convert a temperature delta or rate from °F scale to display unit scale.
    Scale only (×5/9 for Celsius) — no +32/−32 offset."""


def format_temp_delta(delta_fahrenheit: float, unit: str, decimals: int = 0) -> str:
    """Format a temperature delta as a display string, e.g. '5°C'.
    Calls convert_delta() internally."""
```

| Symbol | Caller(s) | Typical use |
|---|---|---|
| `from_fahrenheit` | `coordinator.py` (many sites), `briefing.py`, `api.py` | Display any stored °F temperature |
| `to_fahrenheit` | `coordinator.py`, config flow validation | Normalize user-entered Celsius setpoints to internal °F; `_get_hourly_forecast_data()` uses it to normalise the hourly forecast at its source (Issue #1015); `_get_outdoor_temp()` uses it for the sensor path and the `weather_attrs["temperature"]` fallback only |
| `format_temp` | `coordinator.py`, `briefing.py` | Human-readable temperature strings in briefings and dashboard |
| `convert_delta` | `coordinator.py` (thermal model display) | Display thermal rates (k_active_heat, swing_f) in correct unit |
| `format_temp_delta` | `coordinator.py`, `briefing.py` | Human-readable delta strings (e.g., "setback of 5°C") |

### from_fahrenheit and to_fahrenheit

Absolute temperature conversions. These include the +32/−32 offset.

```
from_fahrenheit(72.0, "celsius")  → 22.0   # (72 - 32) × 5/9
to_fahrenheit(22.0, "celsius")    → 71.6   # 22 × 9/5 + 32
from_fahrenheit(72.0, "fahrenheit") → 72.0  # passthrough
```

Use `from_fahrenheit` whenever displaying a temperature that is stored internally in °F.
Use `to_fahrenheit` when reading a user-entered or HA-reported temperature that may be in Celsius.

### convert_delta and format_temp_delta

Scale-only conversions for differences and rates. The +32/−32 offset does NOT apply.

```
convert_delta(9.0, "celsius")       → 5.0   # 9 × 5/9
format_temp_delta(9.0, "celsius")   → "5°C"
convert_delta(10.0, "fahrenheit")   → 10.0  # passthrough
```

**Critical rule:** Any value that represents a *difference* or *rate* must use `convert_delta` / `format_temp_delta`. Using `from_fahrenheit` on a delta produces incorrect results (e.g., `from_fahrenheit(9.0, "celsius")` → −12.8°C, which is wrong for a 9°F swing).

Affected values: `swing_f`, `k_active_heat` (°F/hr), `k_active_cool` (°F/hr), setback deltas, comfort band widths.

## Unit Detection

The user's display unit is NOT auto-detected from HA's unit system at runtime. It is:

1. Selected by the user in the config flow step that shows entity and sensor configuration (`CONF_TEMP_UNIT`, a `SelectSelector` offering `"fahrenheit"` / `"celsius"`)
2. Stored in the config entry under key `"temp_unit"`
3. Read at every display site via `coordinator.config.get("temp_unit", "fahrenheit")`

**Default:** `"fahrenheit"` — applies if the key is absent (e.g., fresh install before options flow is run, or config entry predates the field)

**Constant:** `DEFAULT_TEMP_UNIT = "fahrenheit"` in `const.py`

This means the unit does not automatically follow if the user changes their HA unit system after setup. They must update the Climate Advisor option explicitly.

## Hourly forecast is normalised to internal °F at its source (Issue #1015)

The weather entity reports its hourly forecast in the provider's native unit. Before 0.7.82 that list was stored and consumed unconverted while every consumer assumed °F, so Celsius installs compared °C forecast values against °F thresholds and predictions (nat-vent forecast-peak guard, the ODE predicted-indoor cache behind the ceiling guard, the nat-vent plan, the Next Automation card, and the chart's Predicted Outdoor/Predicted Indoor).

**Contract:**

- `ClimateAdvisorCoordinator._get_hourly_forecast_data()` returns a **normalised copy** of the service response: each entry's `temperature` (and `temp`, if present) is converted with `to_fahrenheit(float(value), config["temp_unit"])`. `None`, boolean and non-numeric values are left untouched; non-dict entries pass through; the provider's response is never mutated. For `"fahrenheit"` this is a passthrough, so Fahrenheit installs are unchanged.
- `_hourly_forecast_temps` (coordinator) and `automation_engine._hourly_forecast_temps` are therefore **always internal °F**. Any new consumer must treat the list as °F and must not convert it again.
- `_refresh_hourly_forecast()` is the **only** refresh path: it refetches into both the coordinator's and the automation engine's copy. The 30-minute cycle (`_async_update_data_impl`), the briefing-time refresh (`_async_send_briefing`) and `_async_end_of_day` all call it (Issue #1016 — the briefing path previously updated only the coordinator's copy).
- `_get_outdoor_temp()`'s hourly-interpolation branch does **not** convert (the interpolated value is already °F); only its sensor path and the `weather_attrs["temperature"]` fallback convert. Converting the interpolated value again would double-convert on Celsius installs.
- The chart converts forecast-derived series to the display unit at read time (`get_chart_data()`); see [chart-log-spec.md](chart-log-spec.md#temperature-units-and-display-conversion-issue-1015) for the chart-log field units and the pre-fix-history limitation.

## Constants and Boundary Values

| Symbol | Value | Location |
|---|---|---|
| `FAHRENHEIT` | `"fahrenheit"` | `temperature.py` |
| `CELSIUS` | `"celsius"` | `temperature.py` |
| `UNIT_SYMBOL["fahrenheit"]` | `"°F"` | `temperature.py` |
| `UNIT_SYMBOL["celsius"]` | `"°C"` | `temperature.py` |
| `DEFAULT_TEMP_UNIT` | `"fahrenheit"` | `const.py` |
| `CONF_TEMP_UNIT` | `"temp_unit"` | `const.py` |

**Unknown unit handling:** Every function treats an unrecognized unit string as `"fahrenheit"` and returns the value unchanged (passthrough). `UNIT_SYMBOL.get(unit, "°F")` ensures unknown units display as `°F` rather than crashing.

## Invariants

1. **All internal temperatures are °F.** No domain value stored in `LearningState`, `DailyRecord`, or the coordinator state dict is in Celsius. Conversion happens only at display and input boundaries.
2. **`from_fahrenheit` is never called on a delta or rate.** Any such call is a bug. `convert_delta` exists precisely to prevent this.
3. **All functions are pure and stateless.** No side effects, no logging, no I/O. Safe to call from any context.
4. **Unknown units never raise.** Passthrough behavior is guaranteed for any non-`"celsius"` input string.
5. **The hourly forecast list is internal °F.** `_hourly_forecast_temps` is normalised at its single source (`_get_hourly_forecast_data()`) and refreshed only via `_refresh_hourly_forecast()`; no consumer converts it again (Issue #1015). This is a specific instance of invariant 1.

## Disclosure Path

← Tier 1 parent: [02-ARCHITECTURE-REFERENCE.md](02-ARCHITECTURE-REFERENCE.md)
→ Tier 3 specs: none yet authored — candidate: `convert_delta` correctness contract (scale-only, no offset)
↔ Siblings: [docs/state-persistence.md](state-persistence.md)
