<!-- Nav: ← [02-ARCHITECTURE-REFERENCE.md](02-ARCHITECTURE-REFERENCE.md) | → [temperature.py](../custom_components/climate_advisor/temperature.py) -->

# Temperature Conversion — Architecture Brief (Tier 2)

## Anchors

| Question | Short answer | → Full answer |
|---|---|---|
| When converting a temperature *rate* (e.g., k_active_heat in °F/hr) to Celsius for display, which function is correct? | `convert_delta()` or `format_temp_delta()` — these apply scale-only conversion (multiply by 5/9) without the +32/−32 offset. Using `from_fahrenheit()` on a delta is wrong. | [convert_delta and format_temp_delta](#convert_delta-and-format_temp_delta) |
| How is the user's display unit determined, and what is the fallback? | The unit is a user-selected config flow field (`CONF_TEMP_UNIT`), stored as `"fahrenheit"` or `"celsius"`. A new install's `unit` step pre-selects Home Assistant's unit system (since 0.7.86, Issue #1023) and the user may still pick either. The fallback at every read site is `"fahrenheit"` via `config.get("temp_unit", "fahrenheit")`. | [Unit Detection](#unit-detection) |
| What happens when an unknown unit string (e.g., `"kelvin"`) is passed to any conversion function? | All functions treat unknown units as `"fahrenheit"` and return the value unchanged (passthrough). The `UNIT_SYMBOL` dict falls back to `"°F"` for unknown keys. | [Constants and Boundary Values](#constants-and-boundary-values) |
| Where is the canonical rule that all internal temperatures are stored in Fahrenheit? | Stated in the `temperature.py` module docstring: "All internal temperatures are stored and calculated in Fahrenheit. This module provides the only conversion boundary used throughout the integration." | [Scope](#scope) |
| Is the hourly forecast list in the provider's unit or internal °F, and how may a new consumer use it? | `_hourly_forecast_temps` is ALWAYS internal °F: `ClimateAdvisorCoordinator._get_hourly_forecast_data()` normalises a copy at the single source, and `_refresh_hourly_forecast()` is the only refresh path. New consumers must treat it as °F and never convert it again. | [Hourly forecast is normalised to internal °F at its source](#hourly-forecast-is-normalised-to-internal-f-at-its-source-issue-1015) |
| Does Climate Advisor read the weather provider's temperature unit? | No. CA assumes its configured unit equals the provider's and never reads the provider's unit for conversion. A one-shot WARNING `Weather unit mismatch` (plus debug-state fields `unit_mismatch`/`provider_unit`/`configured_unit`) flags a disagreement. Config values stay canonical °F and are unit-invariant. | [Provider unit must equal the configured unit](#provider-unit-must-equal-the-configured-unit-issue-1015) |
| What happens to same-day readings when the user changes the unit? | On the next restore (any day, Issue #1360) the saved `temp_unit` differs from the configured unit, so pending thermal observations, `kept_forecast` and `pred_archive` are discarded, plus outdoor/indoor temp history and `classification` on a same-day restore (they are not restored across days anyway) (WARNING + `unit_changed` event). Chart-log and learning records written under the wrong unit are not repaired. | [Unit change handling](#unit-change-handling-issue-1015) |
| Does the climate-entity unit check (Issue #968) honour `temp_unit`? | Yes, since 0.7.84 (Issue #1018). `_check_climate_unit()` compares the entity's `temperature_unit` attribute (if exposed) with the configured `temp_unit` and logs one ERROR on mismatch; it no longer assumes Fahrenheit. Log-only. | [Climate-entity unit check](#climate-entity-unit-check-issue-968-issue-1018) |
| Does CA check that its unit matches Home Assistant's unit system and its configured temperature sensors' units? | Yes, since 0.7.85 (Issues #1021, #1020). `_check_unit_sources()` logs one WARNING if `temp_unit` differs from HA's system unit (`hass.config.units.temperature_unit`), and one per role (`outdoor_temp`, `indoor_temp`, `sleep_indoor_temp`) if a sensor's `unit_of_measurement` differs. Log-only; debug-state fields `ha_system_unit`, `ha_unit_mismatch`, `sensor_unit_mismatches`. | [Home Assistant unit system and sensor unit checks](#home-assistant-unit-system-and-sensor-unit-checks-issue-1020-issue-1021) |
| Where are Tomorrow's Plan temperatures converted to the display unit, and how do the Celsius setpoint sliders avoid range errors? | `api.py` `ClimateAdvisorLearningView` converts a copy of `coordinator.tomorrow_plan` (`expected_high`/`expected_low`, internal °F) with `display_temp()`; the frontend only appends the symbol. In `config_flow.py`, `_display_default()` keeps the stored value (rounded to 4 dp) and the Celsius slider bounds cover the full converted °F span, so a plain re-save no longer fails the range check (Issue #1049). | [Scope](#scope) |

## Scope

**Owns:**
- The single conversion boundary between internal °F and user-display units
- Public functions: `from_fahrenheit()`, `to_fahrenheit()`, `convert_delta()`, `display_temp()`, `display_delta()` (None-safe rounded API-boundary wrappers, Issue #1049), `format_temp_delta()`, `format_temp()`, `reminder_due()`, `next_fallback_episode()`, `read_state_temp_f()` / `read_attribute_temp_f()` / `read_sensor_state_f()`, `unit_key_from_attr()`, `unit_mismatch()`, `ha_system_unit()`
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
- Provide None-safe, rounded wrappers for API/payload boundaries: `display_temp(value_f, unit, ndigits=1)` (absolute, via `from_fahrenheit`) and `display_delta(value_f, unit, ndigits=1)` (delta/rate, via `convert_delta`); `None` stays `None`. `format_temp_delta` now delegates to `convert_delta` (Issue #1049).
- Boundary note (Issue #1049): `GET /api/climate_advisor/learning` returns `tomorrow_plan.expected_high`/`expected_low` already converted to the configured unit (`api.py`); the Options flow derives slider defaults with `config_flow._display_default()` (only fp-noise-sized overshoots are clamped to the slider bound; a genuinely out-of-range value is shown, not rewritten).

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


def unit_key_from_attr(attr: object) -> str | None:
    """Map an entity's unit attribute ("°F"/"F"/"fahrenheit", etc.) to FAHRENHEIT/CELSIUS, else None.
    Used by the weather, climate, Home Assistant system-unit and sensor unit checks."""


def unit_mismatch(attr: object, configured: str) -> str | None:
    """Reported unit key when recognised AND different from `configured`; None otherwise.
    Used by the sensor unit check (Issue #1020)."""
```

| Symbol | Caller(s) | Typical use |
|---|---|---|
| `display_temp` / `display_delta` | `api.py` (status `indoor_temp`/`outdoor_temp`/`ca_target_*`/`nat_vent_*`/`trend_magnitude`, learning `comfort_range_*`/`tomorrow_plan`, config `temp_abs`), `coordinator.py` (`get_chart_data` `_conv`, 2-dp `swing_heat`/`swing_cool`, `trend_magnitude`, `pre_condition_target`, `setback_modifier`, `today_low`/`tomorrow_low`), `sensor.py` (`comfort_range_low/high`, `trend_magnitude`), `ai_skills_context.py`, `ai_skills_investigator.py` | The single None-safe, rounded API-boundary conversion (Issues #1049/#1050). Replaces ~13 hand-rolled `round(from_fahrenheit(x, unit), 1) if x is not None else None` copies. `ndigits` selects 0/1/2 decimals |
| `from_fahrenheit` | `temperature.py` (wrapped by `display_temp`), `briefing.py`, remaining internal display math | Display any stored °F temperature; new API-boundary code should prefer `display_temp` |
| `read_sensor_state_f` | `coordinator.py` (`_read_outdoor_sensor`, used by `_get_outdoor_temp()` and the outdoor-sensor listener via `_refresh_outdoor_temp`) | Read a dedicated sensor / input_number entity's *state* as °F (Issue #1032): returns `(value_f, None)` or `(None, reason)`; rejects `unavailable`/`unknown`, non-numeric and non-finite (`nan`/`inf`) states. The state-based sibling of `read_state_temp_f` (attribute-based) |
| `read_attribute_temp_f` | `indoor_temp.py` (climate `current_temperature` path); `read_state_temp_f` delegates to it | Read a temperature *attribute* off an HA state as °F with the failure reason (Issue #1033): returns `(value_f, None)` or `(None, reason)` with `reason` in `entity_missing` / `no_reading` / `non_numeric` / `non_finite`. Attribute-reading sibling of `read_sensor_state_f` |
| `is_sensor_stale` | `indoor_temp.py` (indoor `sensor` source and sleep sensor, `INDOOR_SENSOR_STALE_HOURS` = 12 h; `remote_reading_fresh()` for the remote-sensor outer loop, `REMOTE_OFFSET_FRESHNESS_MINUTES` = 120 min, Issue #1194); `coordinator._read_outdoor_sensor` (`OUTDOOR_SENSOR_STALE_HOURS` = 6 h) | True when no report for more than `stale_hours` (age from `last_reported`, falling back to `last_updated`; fails open to fresh when neither is a real `datetime`) (Issues #1032/#1033) |
| `next_fallback_episode` | `coordinator._note_outdoor_sensor_state` (outdoor), `coordinator._refresh_indoor_sensor_health` (indoor) | Pure once-per-episode transition `(reason, announced, since, now) -> (kind, announced, since, minutes_down)`, `kind` in `none` / `fallback` / `changed` / `recovered`; the caller owns logging and event emission (Issue #1033) |
| `to_fahrenheit` | `coordinator.py`, config flow validation | Normalize user-entered Celsius setpoints to internal °F; `_get_hourly_forecast_data()` uses it to normalise the hourly forecast at its source (Issue #1015); `_get_outdoor_temp()` uses it for the sensor path and the `weather_attrs["temperature"]` fallback only |
| `format_temp` | `coordinator.py`, `briefing.py`, `automation.py` (engine `_t()`), `learning.py`, `ai_skills_context.py` | Human-readable temperature strings in briefings, Activity Log reasons and the dashboard |
| `convert_delta` | `coordinator.py` (thermal model display), `temperature.format_temp_delta()` and `display_delta()` (both delegate to it), `sensor.py` | Display thermal rates (k_active_heat) and the swing (setpoint-to-start distance, Issue #1234) in the correct unit |
| `format_temp_delta` | `coordinator.py` (stall alert gap), `automation.py` (engine `_td()`), `learning.py` (override-magnitude and forecast-bias suggestion text), `ai_skills_context.py`, `briefing.py` | Human-readable delta strings (e.g., "setback of 5°C"); built on `convert_delta` |

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

Affected values: `swing_heat_f`/`swing_cool_f` (and their resolution/off-point), `k_active_heat` (°F/hr), `k_active_cool` (°F/hr), setback deltas, comfort band widths.

**Remote-sensor offset step and cap (Issue #1194 a) are deltas.** `REMOTE_OFFSET_STEP_F/_C` (2 F / 1 C) and `REMOTE_OFFSET_CAP_F/_C` (4 F / 2 C) are display-unit deltas; `remote_offset.step_f(unit)` / `cap_f(unit)` return the internal-°F delta (Celsius: ×9/5, so 1.8 F and 3.6 F), never via `to_fahrenheit()`. The offset is added to the internal-°F target and the sum is converted once, by `from_fahrenheit()`, when the setpoint is sent, so a Celsius install moves exactly 1 °C per step. The learned swing used as the hysteresis is likewise a delta.

## Unit Detection

The user's display unit is NOT re-detected from HA's unit system at runtime. It is:

1. Selected by the user in the dedicated `unit` step of the setup wizard (`CONF_TEMP_UNIT`, a `SelectSelector` offering `"fahrenheit"` / `"celsius"`). Since 0.7.86 (Issue #1023) a new install's step pre-selects Home Assistant's unit system, read by `temperature.ha_system_unit(hass)` from `hass.config.units.temperature_unit` (returns `"fahrenheit"`, `"celsius"` or `None` when unreadable); when it returns `None` the pre-selection falls back to `DEFAULT_TEMP_UNIT` (`"fahrenheit"`). The user can still choose either unit on the form. Existing entries are untouched and the options flow is unchanged
2. Stored in the config entry under key `"temp_unit"`
3. Read at every display site via `coordinator.config.get("temp_unit", "fahrenheit")`

**Default:** `"fahrenheit"` — applies if the key is absent (e.g., fresh install before options flow is run, or config entry predates the field)

**Constant:** `DEFAULT_TEMP_UNIT = "fahrenheit"` in `const.py` — the read-site fallback and the setup-wizard pre-selection fallback when `ha_system_unit()` cannot read HA's unit system. The coordinator's `_check_unit_sources()` uses the same `ha_system_unit()` helper to read HA's unit system for its mismatch warning.

This means the unit does not automatically follow if the user changes their HA unit system after setup. They must update the Climate Advisor option explicitly. Since 0.7.85 a mismatch is no longer silent: a Home Assistant install on a different unit system than the configured `temp_unit` (for example a metric install left on the default Fahrenheit) logs the `Temperature unit mismatch with Home Assistant` WARNING. That is intended, because such a setup really does misread every thermostat value, and a `Temperature sensor unit mismatch` WARNING usually fires alongside it since Home Assistant temperature sensors report in the system unit.

## Display sites and reason strings (Issue #1050, Part of #1048)

**Occupant outcome:** a Celsius household used to see Fahrenheit-looking numbers in the Activity Log reasons, band text, override records, suggestions and the AI report ("natural vent exit: outdoor 64.0°F > threshold 75.0°F", "up by about 2°F", a Target Temperature of about -6 for a 21 °C setting). Every user-facing temperature is now produced from internal °F by one of the helpers above at its boundary.

| Boundary | How it converts |
|---|---|
| REST/status (`api.py`) | `display_temp` / `display_delta` (absolute vs scale-only); config `display_transform` `temp_abs` / `temp_delta` -- see [rest-api.md](rest-api.md#config-display-transforms) |
| Chart/debug payloads (`coordinator.get_chart_data()` / `get_debug_state()`) | Local `_conv(v)` = `display_temp(v, unit)`; deltas via `display_delta` (`swing_*` at 2 dp, `trend_magnitude`, `pre_condition_target`, `setback_modifier`) |
| Sensor attributes (`sensor.py`) | `comfort_range_low/high` via `display_temp`; `trend_magnitude` via `display_delta` -- both were raw °F before |
| Engine reason strings and event text (`automation.py`) | `AutomationEngine._temp_unit` with thin `_t(value_f, decimals=1)` / `_td(delta_f, decimals=1)` wrappers over `format_temp` / `format_temp_delta` and shared phrase builders (e.g. `_outdoor_ge_indoor`); band text via `display_temp(..., 0)`. Always pass `decimals=1` for thresholds so reasons keep their pre-#1050 precision |
| Override records | Producers store `old_setpoint_f` / `new_setpoint_f` as real °F floats (`read_attribute_temp_f` / `read_state_temp_f`, 2 dp) -- never display-unit numbers under an `_f` name; `learning.py` (`LearningEngine(storage_path, entry_id, temp_unit)`) and `ai_skills_context.py` format them with `format_temp` / `format_temp_delta` |
| Suggestions and AI text | `LearningEngine._temp_unit` words suggestion text ("up by about 1°C"); `ai_skills_context.py` / `ai_skills_investigator.py` use `format_temp`, `format_temp_delta`, `display_temp`, and read attributes through `read_attribute_temp_f` (no hand-converted x5/9) |
| Dashboard JS (`index.html`) | One `unitSymbol(unit)` helper; settings entries append their own `unit` |

**Logger lines stay °F.** `_LOGGER` messages (and the structured event payload fields named `*_f`) keep the internal Fahrenheit value so logs are comparable across installs; only strings shown to the occupant (Activity Log reason/settings text, push notifications, Status text, AI context) follow `temp_unit`.

**Protected reason substrings.** Several consumers match fragments of reason strings, so the unit migration must not alter them: `airflow reversed`, `cooled to floor`, `free cooling gone`, `manual override to`, `{trigger} reconcile — fan already running` (`tools/sim_harness/outcomes.py`), the prefixes `economizer cool-down` / `maintain` / `off`, and `sleep band [`, `comfort band [`, `> threshold`. Only the numeric/unit portion inside them changes. Fahrenheit homes are otherwise unchanged except one missing `°` added and some AI-text spacing. Tests: `tests/test_automation_unit_display.py`, `tests/test_override_record_units.py`, `tests/test_phase2_display_units.py`.

## Provider unit must equal the configured unit (Issue #1015)

Climate Advisor **assumes** the weather provider reports in the same unit as the configured `temp_unit`. It never reads the provider's unit to decide how to convert: `to_fahrenheit(value, config["temp_unit"])` is applied to provider readings using the *configured* unit only. If the two disagree, readings are silently misinterpreted (e.g. a provider's 16.7 °C read as 16.7 °F is really −8.5 °C), and those samples enter the same-day outdoor history, driving `today_low`/`today_high` and the day trend.

Config values (comfort, setback, thresholds) are stored canonical °F (`config_flow.py` converts on entry) and are **unit-invariant**: changing `temp_unit` does not corrupt them.

**Detection (one-shot WARNING):** in `_get_forecast()`, right after the weather entity's attributes are read, the entity's `temperature_unit` attribute is compared with the configured unit (via `temperature.unit_key_from_attr()`, which accepts `°F`/`°C`/`F`/`C`, case-insensitive). On disagreement:

```
Weather unit mismatch: provider_unit=celsius configured_unit=fahrenheit — readings will be misread until corrected
```

It fires once per run (`_weather_unit_checked` is set only once the attribute is present, so a late-arriving entity is still checked). No entity ids are logged. `get_debug_state()` (and therefore diagnostics) exposes `unit_mismatch` (bool), `provider_unit` and `configured_unit`. There is no dashboard card or Repairs issue for this (deferred).

## Unit change handling (Issue #1015)

Changing `temp_unit` in the options flow does **not** reload the integration. `_commit_section()` (`config_flow.py`, Issue #573) only writes the config entry and raises a `reload_needed` Repairs issue; the new unit takes effect after the user presses the Repairs "Fix", reloads the integration manually, or Home Assistant restarts. The running coordinator's `config` is fixed for its lifetime, so every state save before that point (periodic saves, and `async_shutdown()` on a reload) still records the **old** unit in the state file's top-level `temp_unit`. On an HA restart `async_shutdown()` is not called (the STOP listener only saves learning diagnostics), so the file holds the last periodic save, also in the old unit (see [state-persistence.md](state-persistence.md#temp_unit-key-and-restore-rule-issue-1015)). Either way, on the restore that applies the new unit (any restore, including one that comes back on a later day, Issue #1360), if the saved unit is a string and differs from the configured unit:

- **Not restored:** `temp_history` (outdoor and indoor), `pred_archive`, `classification` (recomputed on the first cycle).
- **Kept:** `today_record`, briefing, `automation_state`, occupancy, flags.
- Learning data is untouched. No in-progress thermal observation can span the switch: observations are never persisted across a restart or reload (Issue #1437).
- A WARNING and a `unit_changed` event (persisted event log) are emitted. WARNING: `Temperature unit changed since last run: same-day readings discarded from=<saved_unit> to=<current_unit> dropped_outdoor=<n> dropped_indoor=<n> dropped_pred_archive=<n> dropped_classification=<True|False>`. Event payload: `from`, `to`, `dropped_outdoor`, `dropped_indoor`, `dropped_pred_archive`, `dropped_classification` (before 0.9.8 also `dropped_pending_observations`, retired by Issue #1437).

The rule discards on **any** unit change, not only a mismatch. The restore itself converts nothing (history is stored in internal °F); the reason for discarding is that samples taken while the configured unit disagreed with the provider's unit are wrong. The rule cannot tell good samples from bad, so it also drops good pre-switch samples (harmless: the buffers refill on the next polls).

Consequence: once the new unit is applied (Repairs "Fix", manual reload or HA restart), Forecast Low/High and the day trend recover straight away instead of staying wrong until the 23:59 history clear. An already-written morning briefing may keep showing the old wording until the next one (the restored briefing text is not regenerated).

**Known limitations:** chart-log entries and learning/thermal records written while the unit was wrong are **not** repaired (old chart history stays as recorded). A unit change on the same restart as the first upgrade to 0.7.83 is not detected, because the pre-upgrade state file has no `temp_unit` key (missing key means unchanged behaviour).

## Climate-entity unit check (Issue #968, Issue #1018)

**Invariant:** Climate Advisor writes thermostat setpoints via `from_fahrenheit(value, config temp_unit)` and reads thermostat temperatures via `to_fahrenheit(...)`. Home Assistant converts climate temperatures to its own system unit (`hass.config.units`) on both the state attributes and the `set_temperature` service, so the unit that actually matters on the thermostat path is the HA system unit, which the configured `temp_unit` is expected to equal (checked by a one-shot WARNING since 0.7.85, see [Home Assistant unit system and sensor unit checks](#home-assistant-unit-system-and-sensor-unit-checks-issue-1020-issue-1021)). The `temperature_unit` check below compares an exposed attribute with the configured unit as a best-effort guard; such an attribute may be the device's native unit rather than the converted one, so a mismatch is a prompt to investigate, not proof that readings are wrong.

**Behaviour (since 0.7.84, Issue #1018):** `ClimateAdvisorCoordinator._check_climate_unit()` runs on each update cycle that has a forecast (it is skipped when the weather entity is missing or unavailable) until the climate entity exists and is available, then latches (`_climate_unit_checked`) and never runs again that run. (Before 0.7.84 the Issue #968 check hardcoded "CA assumes Fahrenheit" and could false-ERROR a Celsius install.)

- If the entity exposes a `temperature_unit` attribute whose value, via `temperature.unit_key_from_attr()`, differs from the configured `temp_unit` (an unrecognised value also counts as a mismatch), it logs ONE ERROR:
  `Climate entity <id> reports temperature_unit=<raw> but Climate Advisor is configured for <configured_unit> — setpoints written by CA will be wrong until this is resolved`
- If the attribute is absent the check is silent. That is the normal case: HA core `ClimateEntity` does not expose `temperature_unit` in state or capability attributes (verified against home-assistant/core), so the check only matters for custom thermostat integrations that add it.
- Log-only: no auto-correction. Fahrenheit installs are unchanged.

Temperature *sensor* entities and Home Assistant's own unit system are checked separately, see [Home Assistant unit system and sensor unit checks](#home-assistant-unit-system-and-sensor-unit-checks-issue-1020-issue-1021).

## Home Assistant unit system and sensor unit checks (Issue #1020, Issue #1021)

**Why:** Home Assistant converts climate temperatures to its system unit on both the climate entity's state attributes and the `climate.set_temperature` service, while Climate Advisor reads and writes using its own configured `temp_unit`. If the two differ, every thermostat read and setpoint write is off by the C/F factor on any install. Likewise a configured temperature sensor that reports in a different unit than `temp_unit` is misread by `to_fahrenheit(..., config temp_unit)`. Since 0.7.85 both are flagged by log-only WARNINGs; nothing is auto-corrected and there is no UI card or Repairs issue.

**Behaviour:** `ClimateAdvisorCoordinator._check_unit_sources()` runs each update cycle that has a forecast, called right after `_check_climate_unit()`. It uses the helpers `temperature.unit_mismatch(attr, configured)` and `temperature.ha_system_unit(hass)`. No entity ids are logged.

- **HA system unit (Issue #1021):** one-shot compare of the configured `temp_unit` with `hass.config.units.temperature_unit`. Latched in `_ha_unit_checked` once HA's unit is recognised. WARNING:
  `Temperature unit mismatch with Home Assistant: ha_unit=<unit> configured_unit=<unit> — thermostat readings and setpoints will be misread until corrected`
- **Sensor units (Issue #1020):** one-shot per role, latched per role in `_sensor_unit_checked` once the entity is available. Each configured temperature sensor's `unit_of_measurement` is compared with `temp_unit`. Roles: `outdoor_temp` (only when `outdoor_temp_source` is `sensor` or `input_number`), `indoor_temp` (only when `indoor_temp_source` is `sensor` or `input_number`), `sleep_indoor_temp` (whenever configured). WARNING:
  `Temperature sensor unit mismatch: role=<role> sensor_unit=<unit> configured_unit=<unit> — readings from this sensor will be misread until corrected`
  The role label is logged, never the entity id. A sensor with no or an unrecognised `unit_of_measurement` is silent (CA cannot know).
- **Debug state:** `get_debug_state()` (and therefore diagnostics) exposes `ha_system_unit`, `ha_unit_mismatch` and `sensor_unit_mismatches` (dict of role to the sensor's unit, `{}` when none), next to the existing `unit_mismatch`/`provider_unit`/`configured_unit`.
- Matching setups are unchanged.

## Hourly forecast is normalised to internal °F at its source (Issue #1015)

The weather entity reports its hourly forecast in the provider's native unit. Before 0.7.82 that list was stored and consumed unconverted while every consumer assumed °F, so Celsius installs compared °C forecast values against °F thresholds and predictions (nat-vent forecast-peak guard, the ODE predicted-indoor cache behind the ceiling guard, the nat-vent plan, the Next Automation card, and the chart's Predicted Outdoor/Predicted Indoor).

**Contract:**

- `ClimateAdvisorCoordinator._get_hourly_forecast_data()` returns a **normalised copy** of the service response: each entry's `temperature` (and `temp`, if present) is converted with `to_fahrenheit(float(value), config["temp_unit"])`. `None`, boolean and non-numeric values are left untouched; non-dict entries pass through; the provider's response is never mutated. For `"fahrenheit"` this is a passthrough, so Fahrenheit installs are unchanged.
- `_hourly_forecast_temps` (coordinator; the engine holds no copy; the coordinator passes the 2-hour peak in at each decision, Issue #1443) is therefore **always internal °F**. Any new consumer must treat the list as °F and must not convert it again.
- `_refresh_hourly_forecast()` is the **only** refresh path: it refetches into the coordinator's list (the only copy since Issue #1443). The 30-minute cycle (`_async_update_data_impl`), the briefing-time refresh (`_async_send_briefing`) and `_async_end_of_day` all call it (Issue #1016 — the briefing path previously updated only the coordinator's copy).
- `read_state_temp_f` (Issue #1033) is now a thin delegate over `read_attribute_temp_f`, and additionally rejects `nan`/`inf` attribute values (returns None) — previously a `nan` slipped through `float()` and surfaced downstream as an out-of-range value. The indoor resolver (`indoor_temp.py`) contains no hand-rolled parses any more: states go through `read_sensor_state_f`, climate attributes through `read_attribute_temp_f`; a dedicated `sensor` source or the sleep sensor with no report for more than `INDOOR_SENSOR_STALE_HOURS` (12 h, uncalibrated) is treated as unavailable (`reason=stale`).
- `_get_outdoor_temp()`'s dedicated-sensor path (Issue #1032) goes through `temperature.read_sensor_state_f()` and then range-checks the converted °F value (`MIN/MAX_PLAUSIBLE_OUTDOOR_F`, -60..150°F) and — `sensor` source only — a staleness check (`OUTDOOR_SENSOR_STALE_HOURS`, 6 h, from `last_reported` falling back to `last_updated`, failing open when neither exists). Any failure falls back to the weather path and is announced once per episode (Activity Log `outdoor_sensor_fallback` / `outdoor_sensor_recovered`). A wrong-unit reading that is still numerically plausible is **not** blocked (only the one-time unit-mismatch warning above fires).
- `_get_outdoor_temp()`'s hourly-interpolation branch does **not** convert (the interpolated value is already °F); only its sensor path and the `weather_attrs["temperature"]` fallback convert. Converting the interpolated value again would double-convert on Celsius installs.
- The chart converts forecast-derived series to the display unit at read time (`get_chart_data()`); see [chart-log-spec.md](chart-log-spec.md#temperature-units-and-display-conversion-issue-1015) for the chart-log field units and the pre-fix-history limitation.

## Constants and Boundary Values

| Symbol | Value | Location |
|---|---|---|
| `FAHRENHEIT` | `"fahrenheit"` | `temperature.py` |
| `CELSIUS` | `"celsius"` | `temperature.py` |
| `UNIT_SYMBOL["fahrenheit"]` | `"°F"` | `temperature.py` |
| `UNIT_SYMBOL["celsius"]` | `"°C"` | `temperature.py` |
| `DEFAULT_TEMP_UNIT` | `"fahrenheit"` (read-site fallback; also the setup-wizard pre-selection when `ha_system_unit()` is `None`) | `const.py` |
| `CONF_TEMP_UNIT` | `"temp_unit"` | `const.py` |

**Unknown unit handling:** Every function treats an unrecognized unit string as `"fahrenheit"` and returns the value unchanged (passthrough). `UNIT_SYMBOL.get(unit, "°F")` ensures unknown units display as `°F` rather than crashing.

## Invariants

1. **All internal temperatures are °F.** No domain value stored in `LearningState`, `DailyRecord`, or the coordinator state dict is in Celsius. Conversion happens only at display and input boundaries.
2. **`from_fahrenheit` is never called on a delta or rate.** Any such call is a bug. `convert_delta` exists precisely to prevent this.
3. **All functions are pure and stateless.** No side effects, no logging, no I/O. Safe to call from any context.
4. **Unknown units never raise.** Passthrough behavior is guaranteed for any non-`"celsius"` input string.
5. **The hourly forecast list is internal °F.** `_hourly_forecast_temps` is normalised at its single source (`_get_hourly_forecast_data()`) and refreshed only via `_refresh_hourly_forecast()`; no consumer converts it again (Issue #1015). This is a specific instance of invariant 1.
6. **Provider unit is assumed, not read.** Provider readings are converted using the configured `temp_unit` only; a disagreement is surfaced by the one-shot `Weather unit mismatch` WARNING but never auto-corrected (Issue #1015).
7. **Configured unit == HA system unit == provider/sensor units.** CA assumes all of these agree; a disagreement with HA's system unit or a configured temperature sensor is surfaced by a one-shot WARNING, never auto-corrected (Issues #1020, #1021).
8. **Engine Activity Log indoor values use the shared resolver (Issue #1038).** Indoor temperatures that `AutomationEngine` attaches to events (`indoor_f` / `indoor_temp` in `override_detected`, `comfort_band_applied`, occupancy setback, `occupancy_comfort_restored`, nat-vent manual-override exit) come from `AutomationEngine._get_indoor_temp_f()` — the same sleep-aware, configured-source resolver the engine uses for decisions, which converts to internal °F and returns `None` for implausible/NaN values. The former `_indoor_f_for_event()` helper (a bare `float(current_temperature)` of the climate attribute) was deleted; on a Celsius install it labelled raw °C as °F, so 21.5 °C rendered as -5.8 °C. `occupancy_comfort_restored` uses the value the welcome-home decision was made from (`HomeDecision.indoor_temp_f`).

## Disclosure Path

← Tier 1 parent: [02-ARCHITECTURE-REFERENCE.md](02-ARCHITECTURE-REFERENCE.md)
→ Tier 3 specs: none yet authored — candidate: `convert_delta` correctness contract (scale-only, no offset)
↔ Siblings: [docs/state-persistence.md](state-persistence.md)
