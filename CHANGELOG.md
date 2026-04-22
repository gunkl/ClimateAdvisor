# Changelog

All notable changes to Climate Advisor are documented here.
This project follows [Keep a Changelog](https://keepachangelog.com/en/1.0.0/) conventions.

## [0.3.26] — 2026-04-22

### Added

- **Sleep temperatures** (#101): New `sleep_heat` and `sleep_cool` config fields give
  users independent overnight setpoints separate from the away setback. Config entry
  migrates from v14 to v15 automatically; defaults preserve prior adaptive setback
  depth.
- **AI Investigator: version context and GitHub issue awareness** (#105): The investigator
  now reads the running integration version at startup and has access to the project's
  open and closed GitHub issues, enabling it to correlate symptoms with known fixes.
  Live rolling status updates during investigation replace the static progress message.
- **Thermal modeling v2: physics-based prediction** (#114): OLS regression over the full
  post-heat decay curve replaces the broken single-point model. Parameters `k_passive`,
  `k_active_heat`, and `k_active_cool` are learned from observed data; a
  `PendingThermalEvent` state machine tracks observation windows across HA restarts.
  Legacy field aliases preserved for backward compatibility.
- **Natural ventilation directional guard** (#115): Activation now requires
  `outdoor < indoor` (directionally beneficial airflow). A symmetric exit condition
  (`outdoor ≥ indoor`) was added to all three activation sites and the continuous
  condition checker. `natural_vent_delta` is now solely a ceiling tolerance above
  `comfort_cool` when indoor is hot.
- **Temperature Setpoints settings section** (#112): New `"setpoints"` category in
  `CONFIG_METADATA` and a dedicated options wizard step group all six temperature targets
  (comfort, setback, sleep) together. Category order in the settings tab:
  Core → Temperature Setpoints → Sensors → Fan → Schedule → Advanced → AI Settings.

### Fixed

- **Predicted indoor spike at bucket boundary** (#106): Thermal lag treated as an index
  offset (wrong physics) combined with hard bucket boundaries at 60°F/70°F caused a
  7.6°F instant jump in predicted temps at 11 PM on cool nights. Fixed with first-order
  exponential smoothing (α = 1/lag_h) and linear interpolation over ±2°F transition
  zones in `_outdoor_conditional_diff`.
- **Wildly incorrect predicted indoor temperatures** (#104): `compute_predicted_temps`
  used `setback_cool = 80°F` for overnight hours on warm/mild days and re-anchored
  daytime drift to `comfort_cool` every hour instead of accumulating. Corrected setpoint
  logic and accumulation model.
- **Win Rec / Windows bars drop to zero on HVAC events** (#117): Three event-driven
  `_chart_log.append()` call sites omitted `windows_open` and `windows_recommended`,
  defaulting to `False` on every HVAC state change. All three now read current sensor
  and classification state.
- **Outdoor temperature spikes in chart** (#110): Short HVAC cycles under 30 minutes
  were missing from chart data, and override events were reading the climate entity's
  indoor sensor as the outdoor temperature.
- **HVAC bar shows continuous heating in fan circulation mode** (#109): `hvac_action=
  "fan"` remapped to "heating" even when `fan_mode="on"` (continuous circulation). Fix
  reads `fan_mode` attribute and skips remap for any non-auto fan mode.
- **HVAC bar time alignment** (#103): Bar chart start/end times now align with
  temperature curve swings and track zoom/reset correctly.
- **Sleep temperatures buried under Schedule in settings** (#112): `sleep_heat` and
  `sleep_cool` had `category: "schedule"` since v15, grouping them with time fields.
  Changed to `category: "setpoints"`.
- **Sleep temperature ordering constraints removed** (#108): Config flow no longer
  enforces that sleep temps must fall strictly between setback and comfort bounds.
- **Status page showing °F when °C configured** (#100): Status tab cards now respect
  the configured temperature unit.
- **Thermal observation pipeline broken on `hvac_action="fan"` thermostats** (#93):
  Running-detection guard `if new_action and old_action` never fired for thermostats
  reporting `hvac_action="fan"` during heating cycles. Fixed to check set membership.
  `state_contradiction_warning` events now emitted to the structured event log (not
  only to AI narrative text).
- **`windows_recommended` did not reflect current outdoor conditions** (#111): The
  recommendation now evaluates whether opening windows would keep or move indoor temp
  toward the comfort zone, and suppresses the recommendation during extreme conditions.
- **Fan running untracked, chart indicator missing, timezone inconsistency** (#113):
  Fan state reclaimed after HA restart; fan indicator restored in chart; AI report
  timestamp corrected to UTC; investigator awareness of thermostat swing added.
- **Timezone audit: UTC/local bugs across predicted indoor and forecast** (#107): Seven
  timezone bugs fixed. Critical: forecast builder was reading key `"time"` instead of
  HA's `"datetime"` — all predicted indoor data silently dropped. Also fixed:
  naive/aware datetime mix, UTC/local date mismatch in forecast day selection near
  midnight, and naive AI report timestamps.
- **HVAC bar displaying incorrect "heating" state** (#102): Resolved with #93/#100
  combined fix batch.

### Changed

- Config entry schema version: **v14 → v15** (sleep temperature fields; migration is
  idempotent and backward compatible).
- `compute_bedtime_setback()` now checks explicit sleep temp config first; adaptive
  fallback retained for installs without sleep temps configured.
- `_build_predicted_indoor_future` now uses HA's `"datetime"` forecast key (with `"time"`
  fallback), `dt_util.as_local()` conversion, and `sleep_heat`/`sleep_cool` for overnight
  setpoints.

### Infrastructure

- **Simulator occupancy and thermostat-mode support** (#98): Simulator models internal
  `_occupancy_mode` state driven by `occupancy_change` events; warm-day setback scenarios
  explicitly documented as `simulator_support: false` with rationale. Manifest signing
  enforced for golden scenarios.
- **10 golden scenarios promoted**: Natural ventilation directional guard scenarios from
  #115 and related regression cases promoted from `pending/` after production validation.
- Config entry VERSION bumped to 15 in `config_flow.py`.

---

## [0.3.18] — (prior release)

See [GitHub release history](https://github.com/gunkl/ClimateAdvisor/releases) for prior
versions.
