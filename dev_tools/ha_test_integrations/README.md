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
temperature, HVAC mode, and fan mode are restored from the last known state).

**`hvac_action`** reflects what the thermostat is actually doing each tick —
`heating`/`cooling` while the compressor is actively running, `idle` while
in heat/cool mode but not applying capacity, `fan` when `hvac_mode` is off
and `fan_mode` is `on`, `off` otherwise. This isn't cosmetic — 12 separate
places in `coordinator.py`/`automation.py` key real decisions off a
thermostat's `hvac_action` (thermal-observation gating, restart-cause
classification, etc.); a fixture that never reported it was silently
defeating all of them, independent of whether `hvac_mode` itself was being
tracked correctly (Issue #830 fixed that; Issue #833 fixed this).

The compressor no longer switches on/off at the exact setpoint every tick.
It now models the same two real-thermostat behaviors that made the earlier
tick-frequency switching unrealistic:

- **Hysteresis (deadband).** The compressor turns *on* only once indoor
  temperature crosses `target_temperature ± deadband_{heat,cool}_f`, and
  turns back *off* only once indoor temperature crosses that same deadband
  edge on the far side of setpoint (`target_temperature ± deadband_{heat,cool}_f`
  again, not the bare setpoint) — a symmetric round-trip swing band, not an
  exact-setpoint trigger on either side (Issue #887). `off_offset_{heat,cool}_f`
  (Issue #1234) move the stop point to `target_temperature + off_offset_heat_f`
  (cool: `- off_offset_cool_f`). Unset, they default to `-0.9` (Issue #1387): the
  compressor starts 1.5 F past the target and stops 0.9 F short of it, the owner's
  real thermostat as measured in #1234 (short runs of a few minutes, a small
  duty cycle). Set them to `deadband_{heat,cool}_f` for the old symmetric band.
- **Full capacity while running.** While the compressor is on, the room gets the
  configured `k_active_*` every tick until the state machine stops it, and
  `hvac_action` reports `heating`/`cooling` only then (Issue #1387). It used to
  apply capacity only below the bare setpoint, so with a stop point above it the
  room pinned at the setpoint while reporting `heating` for hours.
- **Short-cycle protection (dwell timers).** Once the compressor starts, it
  stays on for at least `min_run_seconds` even if it reaches setpoint
  sooner; once it stops, it stays off for at least `min_off_seconds` even
  if the deadband edge is crossed again sooner — equipment-protection dwell
  timers, matching real compressor behavior.

`hvac_action` correctly reports `idle` in **both** of these holding states —
while indoor temperature is sitting inside the deadband, and while a
deadband edge has been crossed but a dwell timer is still blocking the
transition — not just "at setpoint." Because the ODE step is only ever
given active capacity (`k_active_heat`/`k_active_cool`) while the compressor
is actually on, a "commanded but waiting on a dwell timer" tick correctly
falls back to pure passive decay, matching what a real thermostat's
envelope does while the compressor is off. The entity's `compressor_on`
debug attribute (see below) exposes this internal on/off state directly,
since it's no longer always inferable from `hvac_action` alone once dwell
timers can hold it in a state you might not expect from indoor temperature
alone.

**Fan mode** (`ClimateEntityFeature.FAN_MODE`, `auto`/`on`) is supported so
Climate Advisor's HVAC-fan-only feature (`fan_mode: hvac_fan` or `both` in a
zone's config) can be exercised against this fixture — those code paths call
`climate.set_fan_mode` directly on the thermostat entity, which previously
had no support for that service at all. No thermal effect is modeled for
fan-only mode (matches how a real furnace fan doesn't meaningfully heat/cool
either) — it only needs to accept and report the command correctly.

#### HVAC profile: rate, precision, companion sensor, lag, blower (owner request 2026-11-12)

The simulated zones used to be unable to teach Climate Advisor's cycle model a
heating or cooling rate: they reported whole °F, each maintenance run moved the
room ~0.6 °F (invisible at whole degrees), and every zone had the same small
system. Each of the options below is set in the config flow (and Reconfigure),
and the ones marked *runtime* also have an entity you can change while the zone
runs. A runtime change is written into the entry's data without a reload, so it
survives a restart and shows up pre-filled in Reconfigure. **Every option
defaults to today's behaviour; an entry saved before these existed loads
unchanged.** The pure logic lives in `ca_dev_thermostat_sim/hvac_profile.py`.

- **Heating / cooling rate** (*runtime*: `number.<zone>_heating_rate`,
  `number.<zone>_cooling_rate` in °F/h, and `select.<zone>_heating_rate_preset`,
  `select.<zone>_cooling_rate_preset`). The rate is the equipment's **gross**
  capacity, the ODE's `k_active`. Presets: `undersized` 2, `extreme_weather` 5,
  `normal` 12, `oversized` 18 °F/h, or `custom` = the entry's own
  `k_active_heat` / `k_active_cool` (the default, so old entries keep their
  6 °F/h). Picking a preset writes its rate into `k_active_*`; typing a preset's
  rate into the number selects that preset; any other rate shows `custom`.
- **Reported precision** (*runtime*: `select.<zone>_reported_precision`):
  `default` (Home Assistant's own: whole degrees on a °F system, tenths on °C —
  today's behaviour), `whole`, `half`, `tenth`. Sets the climate entity's
  `precision`, i.e. what `current_temperature` is rounded to.
- **Companion temperature sensor** (`companion_sensor`, config only): adds
  `sensor.<zone>_temperature` (`device_class: temperature`, °F, state rounded to
  0.1) on the **same device** as the climate entity — the shape Climate Advisor's
  `indoor_temp.discover_companion_sensors` looks for (Issue #1230), like an
  Ecobee's finer sensor. The climate entity can stay coarse. With the companion
  on, the zone gets a device (named after the zone) and the climate entity takes
  the device's name, so its entity id is unchanged; without it there is no
  device, as before.
- **Sensor lag** (`sensor_lag_min`, *runtime*: `number.<zone>_sensor_lag`): a
  first-order lag (time constant in minutes) between the simulated air and the
  reported reading — the climate entity's `current_temperature` and the
  companion. The thermostat's own start/stop decision also uses the lagged
  reading, as a real thermostat does, so lag makes the air overshoot the stop
  point. The true air is in the `air_temperature` attribute. `0` (default) = no
  lag: the reading is the air.
- **Blower lead-in and overrun** (per direction, minutes; overrun is *runtime*:
  `number.<zone>_heat_blower_overrun`, `number.<zone>_cool_blower_overrun`).
  Overrun: after the burner/compressor stops the blower keeps running and
  `hvac_action` reports `fan` for that long, then `idle` — the cycle reads
  `idle → heating → fan → idle` (cooling: `idle → cooling → fan → idle`), as an
  Ecobee reports its fan dissipation time. It also happens when the mode is
  switched off mid-run. Lead-in: the blower runs (`fan`, no capacity) for that
  long before the burner/compressor starts: `idle → fan → heating → fan → idle`.
  A call that returns during an overrun goes straight to `heating`. `min_run_seconds`
  counts from the call, lead-in included. Default `0` = no fan phase.
- **Coast** (per direction, config only): heat still delivered after the burner
  stops (the hot heat exchanger), or cooling after the compressor stops (the
  cold coil), decaying exponentially: `coast_*_rate_f_per_h` at the stop, time
  constant `coast_*_tau_min`. Total delivered = rate × τ (e.g. 6 °F/h × 4 min =
  0.4 °F before envelope loss). A new run clears any coast still decaying. Usually
  paired with a blower overrun (the blower is what carries it into the room).
  Default `0` = none.
- **Thermostat swing** (unchanged keys): `deadband_*_f` is the START distance
  from the target, `off_offset_*_f` the STOP point past the target (signed, in the
  direction of travel). **Rise per maintenance cycle = start distance − stop
  distance**, both measured on the side the room starts from: the defaults start
  1.5 °F below and stop 0.9 °F below (`off_offset -0.9`), so 1.5 − 0.9 = **0.6 °F**
  per run. `off_offset 0` (stop at the target) gives 1.5 °F; `off_offset +1.5`
  (the old symmetric band) gives 3.0 °F.

The climate entity's attributes expose all of it for debugging:
`heat_rate_f_per_h`, `cool_rate_f_per_h`, `heat_rate_preset`, `cool_rate_preset`,
`precision_option`, `sensor_lag_min`, `air_temperature`, `blower_phase`
(`off` / `lead_in` / `running` / `overrun`), `blower_lead_in_*_min`,
`blower_overrun_*_min`, `coast_f_per_h` (the coast applied on the last tick).

**What each preset means for learning.** The room's NET rate is the gross rate
minus the envelope loss, `|k_passive| × (indoor − outdoor)`: 3 °F/h at the
default `k_passive -0.15` and a 20 °F indoor–outdoor gap (a 50 °F winter day at
70 °F). Run length = rise ÷ net rate. Rounded:

| Preset | Gross | Net (20 °F gap) | Run for 0.6 °F (default swing) | Run for 1.5 °F (`off_offset 0`) | Run for 3.0 °F (symmetric) |
|---|---|---|---|---|---|
| `undersized` | 2 °F/h | −1 °F/h: cannot hold; the room falls | never stops | never stops | never stops |
| `extreme_weather` | 5 °F/h | 2 °F/h | 18 min | 45 min | 90 min |
| `normal` | 12 °F/h | 9 °F/h | 4 min | 10 min | 20 min |
| `oversized` | 18 °F/h | 15 °F/h | 2.4 min | 6 min | 12 min |

Notes for learning:
- The default `min_run_seconds` is 300 s: a run the swing would end sooner is
  held to 5 min, so at `normal` the room actually rises ~0.75 °F per run and at
  `oversized` ~1.25 °F (it overshoots the stop point). Lower `min_run_seconds`
  to see the pure swing.
- At whole-degree precision a 0.6 °F rise is often no visible change at all;
  give the zone `precision: tenth` or a companion sensor for a curve the cycle
  model can fit (6 readings of 0.1 °F per run instead of 0 or 1).
- `undersized` at a 20 °F gap is the "can't keep up" case (runs until the
  weather eases); at a 10 °F gap its net is 0.5 °F/h (72 min for 0.6 °F).

#### Occupancy scheduling and manual overrides (Issue #898)

Each zone (config entry) also gets a `switch` and a `number` platform, in
addition to `climate`:

- **Three occupancy switches** — `switch.<zone>_occupancy_home/vacation/guest`.
  Point `climate_advisor`'s own occupancy config
  (`CONF_HOME_TOGGLE`/`CONF_VACATION_TOGGLE`/`CONF_GUEST_TOGGLE`) at whichever
  of these your test scenario needs — this requires **zero** `climate_advisor`
  changes, confirmed directly from `_is_toggle_on()` (`coordinator.py:1802-1814`),
  which only ever checks `state.state == "on"` against whatever entity_id it's
  configured with. There is **no separate "Away" switch** — production has no
  `CONF_AWAY_TOGGLE` field; it derives Away from the Home switch being off
  (`occupancy_priority.py`'s guest > vacation > home/away chain), so simulating
  Away means turning the Home switch off, not pointing at a fourth entity. (An
  earlier version of this fixture shipped a standalone Away switch that
  production could never be configured to read — fixed live 2026-09-16.) These
  switches can always be toggled manually from a dashboard in addition to being
  driven by the scheduler below.
- **Occupancy scheduler** — configured via this integration's own Options flow
  (gear icon on the zone → manage occupancy schedules), day-of-week + time
  window + target occupancy state per entry (still 4 choices — home/away/
  vacation/guest — "away" is a valid schedule target, it just resolves onto the
  Home switch turning off rather than a switch of its own), mirroring the real
  TOU calendar's add/edit/remove UX. Reuses
  `custom_components/climate_advisor/scheduler.py`'s `Schedule`/
  `is_schedule_active_at()` directly — same DRY precedent as the ODE physics
  reuse above — rather than a second day-of-week/time matcher. Evaluated on the
  same physics tick as `_async_tick()`, not a second timer. Exactly one switch
  on at a time (none, for "away") — a schedule only ever sets its own target's
  switch on and the others off when it's covering "now"; it never forces a
  default state when nothing is scheduled.
- **Manual current-temperature override** — `number.<zone>_set_current_temperature`.
  A one-shot jump to a new simulated indoor temperature (e.g. to instantly test
  behavior at 78°F without waiting for physics to get there); the input resets
  to blank immediately after being applied, so it's visually clear this isn't a
  held override — the thermostat's own `current_temperature` is the real,
  physics-driven value going forward.
- **Manual setpoint override** needs no new entity — the existing thermostat's
  standard `climate.set_temperature` service (any dashboard, script, or
  Developer Tools call) already provides this.

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

To retune an already-created `ca_dev_thermostat_sim` entry (e.g. change
`k_active_heat`/`k_active_cool` or any other field) without deleting and
re-adding it: Settings → Devices & Services → find the entry → **⋮ →
Reconfigure**. This reloads the entry with the new values immediately.

## Config flow field reference

### `ca_dev_thermostat_sim`

| Field | Type | Default | Meaning |
|---|---|---|---|
| `name` | text | "Simulated Thermostat" | Entity name |
| `initial_temp_f` | number | 70.0 | Starting indoor temp (°F) — only used on first setup, later ticks restore from HA state |
| `k_passive` | number | -0.15 | Envelope decay rate (1/hr), always negative |
| `k_active_heat` | number | 6.0 | Heating contribution (°F/hr) |
| `k_active_cool` | number | -6.0 | Cooling contribution (°F/hr), negative |
| `comfort_heat` | number | 68 | Comfort floor (°F) — passed through to the ODE clamp logic |
| `comfort_cool` | number | 76 | Comfort ceiling (°F) — passed through to the ODE clamp logic |
| `outdoor_source` | entity (weather or sensor) | — required | Where outdoor temperature is read from each tick |
| `tick_seconds` | number | 30 | How often the simulation advances (real elapsed wall time drives the math, not this number) |
| `deadband_heat_f` | number | 1.5 | Heat mode: compressor turns **on** once indoor temp falls to `target_temperature - deadband_heat_f`, and **off** once indoor temp rises to `target_temperature + off_offset_heat_f` (default `-0.9`: 0.9 F short of the target) |
| `off_offset_heat_f` | number (optional) | -0.9 | Issue #1234: heat stop point as a signed distance past the target. Unset = `-0.9`, the measured real thermostat (Issue #1387); `+deadband_heat_f` = the old symmetric band |
| `off_offset_cool_f` | number (optional) | -0.9 | Cool mirror: the compressor stops at `target_temperature - off_offset_cool_f` (default 0.9 F short of the target) |
| `deadband_cool_f` | number | 1.5 | Cool mode: compressor turns **on** once indoor temp rises to `target_temperature + deadband_cool_f`, and **off** once indoor temp falls to `target_temperature - off_offset_cool_f` (default 0.9 F short of the target) |
| `min_run_seconds` | number | 300 | Equipment-protection dwell timer: minimum time the compressor stays on once started, even if it reaches setpoint sooner |
| `min_off_seconds` | number | 300 | Equipment-protection dwell timer: minimum time the compressor stays off once stopped, even if a deadband edge is crossed again sooner |
| `heat_rate_preset` / `cool_rate_preset` | select | `custom` | `undersized` 2 / `extreme_weather` 5 / `normal` 12 / `oversized` 18 °F/h gross capacity; `custom` = `k_active_heat` / `k_active_cool` |
| `precision` | select | `default` | Climate entity's reported precision: `default` (HA's: whole on °F), `whole`, `half`, `tenth` |
| `companion_sensor` | boolean | off | Add `sensor.<zone>_temperature` at tenths on the thermostat's device |
| `sensor_lag_min` | number | 0 | First-order lag (min) from the simulated air to the reported reading(s); the thermostat decides on the lagged reading |
| `blower_lead_in_heat_min` / `blower_lead_in_cool_min` | number | 0 | Blower runs (`hvac_action: fan`) this long before the burner / compressor starts |
| `blower_overrun_heat_min` / `blower_overrun_cool_min` | number | 0 | Blower keeps running (`hvac_action: fan`) this long after the burner / compressor stops |
| `coast_heat_rate_f_per_h` / `coast_cool_rate_f_per_h` | number | 0 | Heating (cooling) still delivered at the stop, decaying |
| `coast_heat_tau_min` / `coast_cool_tau_min` | number | 0 | Coast time constant (min); total coast = rate × τ |

`deadband_heat_f`/`deadband_cool_f` default to 1.5°F to match production's
`THERMAL_SWING_DEFAULT_F` — the same "real thermostat swing" value Climate
Advisor itself assumes when it has no better live-measured estimate
(`docs/08-COMPUTATION-REFERENCE.md` §5e-vii). `min_run_seconds`/
`min_off_seconds` default to 300s (5 min), a typical compressor short-cycle
protection interval. All four are new keys — existing config entries
created before these fields existed load the defaults automatically until
reconfigured. See "Matching a real home" below for tuning these to a
specific zone's measured swing.

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
carefully by HA convention (targeting the `homeassistant: 2026.3.0` minimum
pinned in `hacs.json`) and need to be installed and exercised on a real HA
instance by a human before being trusted. The one exception:
`ca_dev_thermostat_sim/test_sim_math.py` hand-verifies the ODE formula
transcribed from `_simulate_indoor_physics` (passive decay, active heating,
and a clamp-triggering long-dt cooling case), plus the deadband/dwell-timer
switching logic described above (compressor doesn't turn on before crossing
the near-side deadband edge, doesn't turn off before crossing the far-side
deadband edge, and `min_off_seconds`/`min_run_seconds` correctly delay a
transition the deadband alone would have triggered) — run it with
`python dev_tools/ha_test_integrations/ca_dev_thermostat_sim/test_sim_math.py`.
It is not a substitute for actually importing the real function once
`homeassistant` is installed; see that script's docstring for why the import
itself couldn't be tested here and what to do once it can be.

The HVAC profile has its own checks:
`python dev_tools/ha_test_integrations/ca_dev_thermostat_sim/test_hvac_profile.py`
imports the real `hvac_profile.py` (it has no Home Assistant import) and checks
the presets, that the defaults reproduce the pre-profile tick exactly, the rate
physics against the ODE's closed form, precision, lag, the blower sequences
(`idle → heating → fan → idle` and the cooling mirror, lead-in), and coast. The
real entity inside real Home Assistant is covered by
`tests_ha/test_dev_sim_hvac_profile.py`; real-HA tests build a zone with a given
profile through the `make_sim` fixture and `tests_ha/house/sim_profile.py`'s
`SimProfile` (e.g. `await make_sim(profile=SimProfile(heat_rate="normal",
companion_sensor=True, blower_overrun_heat_min=2))`).
