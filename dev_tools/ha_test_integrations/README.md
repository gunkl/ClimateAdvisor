# HA Test Integrations (dev-only — never shipped)

Two synthetic Home Assistant custom integrations for locally testing Climate
Advisor's multi-zone support, built for GitHub issue #809.

## What these are (and are not)

- **Dev-only.** They exist to give a developer a fake climate zone and a fake
  weather source to point Climate Advisor at, without needing real hardware
  or multiple physical HVAC zones.
- **Never shipped.** They live at `dev_tools/ha_test_integrations/`, outside
  `custom_components/`, which is the real shipped integration. Confirmed:
  `tools/deploy.py` hardcodes `COMPONENT_DIR = REPO_ROOT / "custom_components" / "climate_advisor"`
  (see `tools/deploy.py:27`) — it never looks at `dev_tools/`, so `deploy.py`
  cannot accidentally push these to a production HA instance. They are
  likewise outside the scope HACS and hassfest validate (both only see
  `custom_components/climate_advisor/`).
- **Not covered by any CLAUDE.md release/versioning process** — they have
  their own tiny `manifest.json` `version: "0.1.0"` that has nothing to do
  with Climate Advisor's own version in `const.py`/`manifest.json`.

## What they do

### `ca_dev_thermostat_sim`

A `climate` entity (`SimulatedThermostat`) whose indoor temperature evolves
using the **real** Climate Advisor thermal ODE — it imports and calls
`_simulate_indoor_physics` directly from
`custom_components/climate_advisor/coordinator.py` (a pure, module-level
function, `coordinator.py:9313-9363`) rather than reimplementing the physics.
This means the simulator can never drift out of sync with production's
actual thermal model, but it also means **this integration only works when
`climate_advisor` is installed on the same HA instance** — if the import
fails, `async_setup_entry` raises `ConfigEntryNotReady` with a clear log
message instead of silently running wrong math.

Every tick (`tick_seconds`, default 30s) it:
1. Computes real elapsed wall time since the last tick (not the configured
   tick interval — this keeps the simulation correct across HA restarts,
   event-loop delays, or missed ticks over a multi-day run).
2. Reads outdoor temperature from the configured `outdoor_source` entity
   (tries the `temperature` attribute first for weather entities, falls back
   to `.state` for plain sensors).
3. Calls `_simulate_indoor_physics(...)` with the configured `k_passive`,
   `k_active_heat`/`k_active_cool` (selected by current `hvac_mode`), and the
   current target temperature.
4. Writes the new simulated indoor temperature to HA state.

State survives HA restarts via `RestoreEntity` (current temperature, target
temperature, and HVAC mode are restored from the last known state).

### `ca_dev_weather_proxy`

A `weather` entity (`SyntheticWeatherEntity`) that produces a smooth,
deterministic diurnal outdoor temperature curve:

```
T(hour) = base_temp_f + diurnal_swing_f * sin(2*pi*(hour - phase_offset_h + 6)/24)
```

With the default `phase_offset_h=15`, temperature peaks at 3pm and troughs at
3am — a typical outdoor diurnal shape. `async_forecast_daily`/
`async_forecast_hourly` project the same curve forward so Climate Advisor's
forecast-dependent logic (classification, target-band scheduling) has
something realistic to read. The `condition` (sunny/cloudy/rainy/snowy) is
static, set at config time.

## Manual install

1. Copy each integration folder into your HA config's `custom_components/`
   directory:
   ```
   <ha_config>/custom_components/ca_dev_thermostat_sim/
   <ha_config>/custom_components/ca_dev_weather_proxy/
   ```
2. Restart Home Assistant.
3. Settings → Devices & Services → Add Integration → search **"Ca Dev
   Weather Proxy"** first (thermostat sim's `outdoor_source` needs an entity
   to point at) → fill out the form → Submit.
4. Settings → Devices & Services → Add Integration → search **"Ca Dev
   Thermostat Sim"** → set `outdoor_source` to the weather entity created in
   step 3 (or any real/synthetic sensor) → fill out the rest of the form →
   Submit.
5. Point Climate Advisor's config flow at the new `climate.*` and
   `weather.*` entities the same way you would point it at real hardware.

## Config flow field reference

### `ca_dev_thermostat_sim`

| Field | Type | Default | Meaning |
|---|---|---|---|
| `name` | text | "Simulated Thermostat" | Entity name |
| `initial_temp_f` | number | 70.0 | Starting indoor temp (°F) — only used on first setup, later ticks restore from HA state |
| `k_passive` | number | -0.15 | Envelope decay rate (1/hr), always negative |
| `k_active_heat` | number | 3.0 | Heating contribution (°F/hr) |
| `k_active_cool` | number | -3.0 | Cooling contribution (°F/hr), negative |
| `comfort_heat` | number | 68 | Comfort floor (°F) — passed through to the ODE clamp logic |
| `comfort_cool` | number | 76 | Comfort ceiling (°F) — passed through to the ODE clamp logic |
| `outdoor_source` | entity (weather or sensor) | — required | Where outdoor temperature is read from each tick |
| `tick_seconds` | number | 30 | How often the simulation advances (real elapsed wall time drives the math, not this number) |

### `ca_dev_weather_proxy`

| Field | Type | Default | Meaning |
|---|---|---|---|
| `name` | text | "Simulated Weather" | Entity name |
| `base_temp_f` | number | 65.0 | Midline of the diurnal sine curve (°F) |
| `diurnal_swing_f` | number | 15.0 | +/- amplitude around `base_temp_f` (°F) |
| `phase_offset_h` | number | 15.0 | Hour-of-day the curve peaks (15 = 3pm) |
| `condition` | select | sunny | Static weather condition (sunny/cloudy/rainy/snowy) |

## Matching a real home

To make `ca_dev_thermostat_sim` behave like your actual house instead of the
generic defaults, pull your real learned thermal parameters and use them as
the config flow values:

```bash
python tools/learning_db.py --model
```

Copy the printed `k_passive`, `k_active_heat`/`heating_rate_f_per_hour`, and
`k_active_cool`/`cooling_rate_f_per_hour` values (see
`custom_components/climate_advisor/learning.py`'s `get_thermal_model()`) into
the thermostat sim's config flow.

## Verification note

There is no `homeassistant` pip package in this repo/venv, so nothing here
was importable or runnable in this session — these files were written
carefully by HA convention (targeting the `homeassistant: 2024.6.0` minimum
pinned in `hacs.json`) and need to be installed and exercised on a real HA
instance by a human before being trusted. The one exception:
`ca_dev_thermostat_sim/test_sim_math.py` hand-verifies the ODE formula
transcribed from `_simulate_indoor_physics` (three cases: passive decay,
active heating, and a clamp-triggering long-dt cooling case) — run it with
`python dev_tools/ha_test_integrations/ca_dev_thermostat_sim/test_sim_math.py`.
It is not a substitute for actually importing the real function once
`homeassistant` is installed; see that script's docstring for why the import
itself couldn't be tested here and what to do once it can be.
