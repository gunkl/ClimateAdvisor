# Changelog

All notable changes to Climate Advisor are documented here.
This project follows [Keep a Changelog](https://keepachangelog.com/en/1.0.0/) conventions.

## [0.3.50] — 2026-05-18

### Fixed

- **Thermal: `"samples": []` key removed from HVAC obs dict** (#156): `_start_hvac_observation`
  created the observation dict with both `"samples": []` and `"active_samples": []`. Because
  Python dicts return the first matching key, `obs.get("samples", ...)` always returned `[]`
  regardless of how many samples had accumulated in `active_samples`. All HVAC observations
  were silently discarded at commit time — `k_active_cool` and `k_active_heat` could never be
  learned despite AC or heat cycling normally. `"samples"` key removed; all HVAC commit paths
  now read `active_samples` and `post_heat_samples` explicitly.

- **Thermal: Startup recovery now correctly handles HVAC pending observations** (#156):
  The startup recovery loop (run on HA restart to continue or abandon in-flight observations)
  used `obs.get("samples", [])` for all types. For HVAC types, this always returned `[]` due
  to the key-shadow bug, so every pending HVAC observation was abandoned with `n=0` on every
  HA restart. Recovery is now phase-aware: `post_heat` phase reads `post_heat_samples`
  (min_s = `THERMAL_MIN_POST_HEAT_SAMPLES`); `active` phase reads `active_samples`
  (min_s = 1 — any sample worth recovering). Backward-compat fallback retained for
  pre-fix persisted observations.

- **Thermal: `_abandon_observation` now reports real sample count in rejection log** (#156):
  Rejection log `n` field was always computed from `obs.get("samples", [])` — the shadowed
  empty list — so all HVAC rejection entries showed `n=0` regardless of actual sample count.
  Fixed to read the correct key per type (`active_samples` for HVAC active-phase,
  `post_heat_samples` for post-heat, `samples` for rolling-window types).

### Added

- **Thermal: Event-driven sampling during active HVAC phase** (#156): `_async_thermostat_changed`
  now appends a sample to `active_samples` whenever a thermostat state change occurs while HVAC
  action is active. A 60-second decimation gate prevents duplicate samples. Short HVAC cycles
  (1–4 min) that complete between 5-min polling ticks previously accumulated only 1 sample
  (0 OLS pairs); they now accumulate 3–10 event-driven samples, making `compute_k_active_single_point`
  much more likely to succeed on short-cycling thermostats.

- **`learning_db.py --pending` flag** (#156): Shows in-flight observations from the
  `pending_observations` dict — type, phase (`active`/`post_heat`), elapsed time, sample
  counts, and peak indoor temperature. Run during a live HVAC cycle to confirm samples are
  accumulating correctly.

- **`learning_db.py --rejections` enhancements** (#156): The rejection log output now includes
  a top-reason summary table at the bottom (reason code, count, percentage). New `--type TYPE`
  filter narrows output to a specific obs_type (e.g., `--rejections --type hvac_cool`).

- **AI investigator: Thermal pipeline health coverage** (#156): A new
  `=== THERMAL OBSERVATION PIPELINE ===` context section is added to the investigator's
  context. Per-type rows show committed/rejected counts, top rejection reason codes, and
  `NEVER LEARNED` flags when `k_active_cool` or `k_active_heat` is `None`. Pending in-flight
  observations are listed with phase and sample count. `THERMAL PIPELINE HEALTH rules` in the
  system prompt instruct the AI to flag 0-committed HVAC types and repeated `new_session_started`
  abandonments as pipeline failures rather than leaving them implicit in null model fields.

## [0.3.49] — 2026-05-18

### Added

- **Chart: Automation Setpoints overlay** (#153): Replaces the "Thermostat Setpoint"
  overlay (which was empty all warm season because it read the hardware `target_temperature`
  attribute, null when HVAC is off). The new overlay reads two always-present defense lines
  derived from the target band schedule: a heat threshold (amber, lower bound) and a cool
  threshold (blue, upper bound). Both are on by default. The setback step at bedtime is now
  clearly visible as the heat line drops from `comfort_heat` to the configured sleep setpoint
  at `sleep_time` and rises again at `wake_time`.

- **Chart: Future activity bars** (#153): HVAC, Fan, and Windows Recommended activity bars
  now extend into the future with predicted state shown at 40% opacity. Predictions derive
  from today's classification (`hvac_mode` intent), natural ventilation conditions computed
  from the hourly forecast, and windows-recommended logic applied to forecast outdoor vs.
  predicted indoor temperatures. A vertical separator marks the now boundary between solid
  historical bars and faint future bars.

## [0.3.48] — 2026-05-17

### Added

- **Bedtime setback visibility** (#151): `handle_bedtime()` now emits `bedtime_setback` and
  `bedtime_setback_skipped` events to the structured event log, making all skip/fire paths
  observable by the AI investigator. `DailyRecord` gains five new fields:
  `setback_heat_applied_f`, `setback_cool_applied_f`, `setback_depth_f`,
  `setback_was_adaptive`, and `setback_skipped_reason`. Previously, the on-mode warm/mild
  nights took a silent pass (correct behavior); that pass is now logged as `reason="hvac_off"`.
  Doc error in §6a: Away row now correctly says "Skip" rather than "Apply bedtime setback".

- **`learning_db.py --daily [N]`** (#151): New `--daily` flag prints the last N nightly
  setback records (date, day type, mode, applied temp, depth, adaptive flag, skip reason).
  Default: 30 nights. Useful for diagnosing whether setback has been firing on heat/cool
  nights or silently skipping all warm-season nights.

- **Chart: Thermostat Setpoint overlay** (#151): The chart now captures the thermostat's
  `target_temperature` at every 30-min poll and exposes two new API fields:
  `historical_setpoint` (actual past setpoints) and `predicted_setpoint` (derived from
  the target band — lower bound in heat mode, upper in cool mode, null in off mode). The
  dashboard renders these as a stepped purple/magenta line with solid past, dashed future,
  and faint-dotted forward-fill during off-mode periods. Toggle via the Thermostat Setpoint
  overlay checkbox.

## [0.3.47] — 2026-05-17

### Fixed

- **AI activity report: k_active_hvac shows None** (#149): `_format_engine_status_for_ai`
  read `hvac_info.get("k_active_heat")` directly — always None. The real shape nests
  these values under `hvac_info["value"]["heat"]` and `hvac_info["value"]["cool"]`. Fixed
  to read nested keys; added chain tests covering the full `get_engine_status()` →
  formatter path.

- **AI activity report: comfort band false positives** (#149): The cross-validation check
  flagged any indoor temp below `comfort_heat` with zero tolerance. Thermostat deadband
  (±0.5–1.5°F) made these false alarms routine. The check now acquires
  `swing_heat_f_display` / `swing_cool_f_display` from the thermal model (default
  `THERMAL_SWING_DEFAULT_F` = 1.5°F) and only flags when the shortfall strictly exceeds
  the learned swing.

- **AI activity report: section repetition** (#149): Added `DEDUPLICATION RULE` to
  `_SYSTEM_PROMPT` with exclusive section role definitions. SUMMARY / TIMELINE /
  DECISIONS / ANOMALIES / DIAGNOSTICS each have a non-overlapping scope; one-line
  cross-references are allowed, verbatim restatement is not.

- **Thermal: HVAC swing peak capture at HVAC-off** (#149): `_end_hvac_active_phase`
  previously did not sample indoor temperature at the HVAC-off moment. `peak_indoor_f`
  was updated only at 30-min poll cycles, making swing measurements based on stale data.
  The method now appends a final active sample at HVAC-off and updates `peak_indoor_f`
  if the shutoff temperature exceeds the prior peak.

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
