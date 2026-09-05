<!-- Nav: ← [Learning Engine Design](05-LEARNING-ENGINE-DESIGN.md) -->

# Climate Advisor — Computation Reference

This document is the authoritative reference for every formula, threshold, and decision table used by Climate Advisor to automate HVAC control. It covers day classification, trend analysis, temperature setpoints, occupancy logic, window management, the economizer, fan control, door/window pausing, grace periods, and all configurable defaults.

For structural context — how these computations fit into the coordinator, automation engine, and classifier modules — see [`docs/02-ARCHITECTURE-REFERENCE.md`](02-ARCHITECTURE-REFERENCE.md).

### Temperature Units

- All internal thresholds and calculations use **Fahrenheit as the canonical unit** (e.g., `THRESHOLD_HOT = 85`, `comfort_heat = 70`).
- The `temp_unit` config key controls the display unit (`fahrenheit` or `celsius`, default: `fahrenheit`).
- Temperatures received from Home Assistant (weather entity forecast highs/lows, indoor/outdoor sensor readings) are **automatically converted to °F** before any classification, trend, or setpoint calculation.
- Temperatures sent to Home Assistant (thermostat setpoints via `climate.set_temperature`) are **converted back to the user's chosen unit** before the service call.
- Briefings and log messages display temperatures in the user's chosen unit.

The automation logic table and all threshold constants in this document are expressed in °F. The unit conversion layer is transparent to all downstream logic — automation behavior is identical regardless of which display unit the user has selected.

---

## Anchors
| Question | Short answer | → Full answer |
|---|---|---|
| What temperature thresholds map to each day type? | HOT ≥ 85°F, WARM ≥ 75°F, MILD ≥ 60°F, COOL ≥ 45°F, COLD < 45°F; all thresholds are °F constants in `const.py`. | [§1. Day Classification](08-COMPUTATION-REFERENCE.md#1-day-classification) |
| How is the setback modifier computed and what values can it take? | `avg_delta = ((tomorrow_high − today_high) + (tomorrow_low − today_low)) / 2`; modifier ranges from −3.0 (strong warming) to +3.0 (significant cold front); stable trend → 0. | [§3. Setback Modifier](08-COMPUTATION-REFERENCE.md#3-setback-modifier) |
| What is the bedtime setpoint formula and when does the thermal model change it? | Default: `comfort_heat − 4°F` (heat) / `comfort_cool + 3°F` (cool). When thermal model confidence ≥ "low", `compute_bedtime_setback()` scales depth from `heating_rate_f_per_hour × recovery_window_hours`, clamped to `[MIN_SETBACK_DEPTH, MAX_SETBACK_DEPTH]`. | [§5a. Adaptive Bedtime Setback](08-COMPUTATION-REFERENCE.md#5a-adaptive-bedtime-setback-compute_bedtime_setback) |
| When does nat-vent continue past bedtime instead of stopping, and what stops it afterward? | Bedtime no longer makes its own outdoor-vs-sleep_cool comparison (removed in Issue #498). It defers entirely — leaving the fan alone AND (Issue #764) leaving an active economizer session alone — whenever `decide_scheduled_band_gate()` returns `DEFER_NAT_VENT` (nat-vent active or WHF owns HVAC), emitting `nat_vent_bedtime_continue`. `outdoor_temp`/`sleep_cool` on that event are informational payload only, not a comparison. Through the sleep window the fan cycles on/off around `sleep_heat + hysteresis` (`nat_vent_temperature_check()`); the session itself only ends via the Priority 2 comfort-floor exit (`indoor ≤ sleep_heat − hysteresis`, event `nat_vent_comfort_floor_exit`) — the old Priority 0 `nat_vent_sleep_ceiling_reached` exit was removed in Issue #371. | [§5a nat-vent continuation gate](08-COMPUTATION-REFERENCE.md#5a-adaptive-bedtime-setback-compute_bedtime_setback) · [Exit Hierarchy](08-COMPUTATION-REFERENCE.md#exit-hierarchy) |
| When and how does CA pre-cool the home overnight? | Mid-night trigger (nat-vent close + 30 min, or wake − 4 h fallback), eligible on a warming trend OR when tomorrow is independently forecast hot (`resolve_pre_cool_modifier()`, Issue #558); target = `sleep_cool + modifier` floored at `sleep_heat + hysteresis` (`compute_pre_cool_target()`, single source of truth for all 5 call sites). AC suppressed if nat-vent already reached target. Morning guard emits `pre_cool_overshoot` if indoor < `comfort_heat` at wake-up. | [§5a-i. Overnight Pre-Cool Phase](08-COMPUTATION-REFERENCE.md#5a-i-overnight-pre-cool-phase-issue-258-trigger-broadened-in-558) |
| How does the physics ODE predict future indoor temperature? | `T(t+dt) = T_outdoor + (T − T_outdoor) × exp(k_p × dt) + (Q/k_p) × (exp(k_p × dt) − 1)`, where Q switches between k_active_heat, k_active_cool, and 0 per schedule period. | [§5c. Predicted Temperature Graph — Physics Path](08-COMPUTATION-REFERENCE.md#5c-predicted-temperature-graph--physics-path) |
| What is the dynamic target band and how does occupancy mode change it? | `_compute_target_band_schedule()` returns `[{ts, lower, upper}]` per forecast hour; away = setback today only, vacation = deep setback all days, home/guest = comfort with sleep/wake ramps. | [§5d. Dynamic Target Band](08-COMPUTATION-REFERENCE.md#5d-dynamic-target-band--_compute_target_band_schedule) |
| How does comfort score accumulate and what triggers a suggestion? | `comfort_score = 1 − (total_violation_minutes / (days_recorded × 1440))`; more than 5 days with > 30 violation minutes triggers the `comfort_violations` suggestion. | [§Metric Definitions — Comfort Score](05-LEARNING-ENGINE-DESIGN.md#comfort-score-comfort_score) |
| When does the ODE ceiling guard fire on a warm day and what activates AC? | The guard scans the predicted indoor curve on every 30-min cycle and sets HVAC to cool at `comfort_cool` when a breach is predicted within the lead time (or 120-min fallback). It is **dormant only when all 3 hold**: outdoor <= indoor AND nat-vent is actually running AND indoor still <= ceiling. So it also fires when indoor already exceeds the ceiling (even if outdoor < indoor) or when nat-vent is not running — clearing nat-vent on escalation. Guard skips when no calibrated model or occupancy is away/vacation. | [§6c. Warm-Day ODE Ceiling Guard](08-COMPUTATION-REFERENCE.md#6c-warm-day-ode-ceiling-guard-issue-136) |
| How does MILD day window scheduling change when the ODE is available (Fix C, Issue #147)? | Before Fix C: MILD days used hardcoded `time(10, 0)` open / `time(17, 0)` close. After Fix C: constants `MILD_WINDOW_OPEN_HOUR = 10` and `MILD_WINDOW_CLOSE_HOUR = 17` are fallbacks; when the ODE is available, `nat_vent_cutoff` drives the close time — the same dynamic logic as warm days. | [§6d. MILD Day Dynamic Window Close Time](08-COMPUTATION-REFERENCE.md#6d-mild-day-dynamic-window-close-time-fix-c-issue-147) |
| Why did the briefing say "hold the heat in" while Next Automation said "outdoor stops helping," for different times, on the same warm day (Issue #847)? | Two compounding bugs: the briefing froze `nat_vent_cutoff`/`nat_vent_cutoff_reason` at generation time with no staleness trigger for either field, while Next Automation recomputed both live every cycle; and the reason→sentence mapping was two independently-written inline branches (MILD had none at all). #847 unified both onto a shared `describe_nat_vent_cutoff_reason()` helper, added the missing staleness trigger, and added the live sanity check #430 had asked for. | [§6d. Issue #847 update](08-COMPUTATION-REFERENCE.md#6d-mild-day-dynamic-window-close-time-fix-c-issue-147) · [§7. Reason wording for the close time](08-COMPUTATION-REFERENCE.md#7-window-recommendations) |
| What invariant must `_async_send_briefing()` maintain when replacing `_today_record`? | It must copy all accumulated counters (`hvac_runtime_minutes`, `comfort_violations_minutes`, etc.) from the existing same-day record before constructing the new one. Creating a fresh `DailyRecord` unconditionally resets all counters to zero (Issue #176 bug). | [DailyRecord Persistence Invariant](08-COMPUTATION-REFERENCE.md#dailyrecord-persistence-invariant-issue-176) |
| Why must `_async_thermostat_changed()` check all three command-pending flags, not just `_hvac_command_pending`? | Automation sequences (e.g., nat vent exit) call `_deactivate_fan()` before `_set_hvac_mode()`. The fan command sets `_fan_command_pending` but leaves `_hvac_command_pending` False. Checking only `_hvac_command_pending` bypasses the override-detection guard during that window. | [§9b Compound command-pending guard](08-COMPUTATION-REFERENCE.md#compound-command-pending-guard-in-_async_thermostat_changed-issue-205206) |
| Why was a manual mode override not detected on dual-setpoint (`heat_cool`) thermostats? | CA commands `heat_cool` mode but the old code compared the thermostat's `hvac_mode` against `classification.hvac_mode` (e.g., `"cool"`). A user switching from `heat_cool` to `cool` evaluated as equal and was ignored. Fix: compare against `_last_commanded_hvac_mode` first. | [§9b Mode Override Detection — `_last_commanded_hvac_mode`](08-COMPUTATION-REFERENCE.md#mode-override-detection--_last_commanded_hvac_mode-issue-269-bug-c) |
| Why was a manual setpoint change not detected on dual-setpoint (`heat_cool`) thermostats? | In `heat_cool` mode the `temperature` attribute is `None`; only `target_temp_low`/`target_temp_high` are populated. The setpoint override check now reads those attributes when mode is `heat_cool`. | [§9b Dual Setpoint Override Detection](08-COMPUTATION-REFERENCE.md#dual-setpoint-override-detection--heat_cool-mode-issue-269-bug-d) |
| Why do cloud thermostats (Nest, Ecobee) falsely trigger fan overrides after HVAC mode changes? | Cloud polling echoes `fan_mode` attribute changes as delayed side-effects 30–120 s after the command, outside the 30 s `_is_recent_hvac_command` window. The `_is_expected_confirmation` flag extends suppression to 120 s for the `fan_mode` path. | [§9b `_is_expected_confirmation`](08-COMPUTATION-REFERENCE.md#_is_expected_confirmation-issue-269-bug-a) |
| What is the comfort-band programming model introduced in Issue #249? | CA programs a floor+ceiling band every 30 min via one `select_comfort_band` decision and one `_apply_comfort_band` actuation; the thermostat's own deadband holds the house inside the band continuously — no more off+supervisor pattern. | [§6e Comfort-Band Programming](08-COMPUTATION-REFERENCE.md#6e-comfort-band-programming-issue-249) |
| What command shape does `_apply_comfort_band` emit per thermostat capability? | Dual-capable: `heat_cool` mode + `set_temperature(target_temp_low=floor, target_temp_high=ceiling)`. Cool-only (active=ceiling): `cool` + `set_temperature(ceiling)`. Heat-only (active=floor): `heat` + `set_temperature(floor)`. | [§6e — `_apply_comfort_band` command shapes](08-COMPUTATION-REFERENCE.md#_apply_comfort_band-command-shapes) |
| Why does nat-vent no longer set HVAC off when windows open (Issue #249)? | The band stays armed when nat-vent activates; the thermostat self-arbitrates — free cooling is free, AC kicks in only if the breeze can't hold the ceiling. Turning HVAC off also disarmed the floor, making cold-snap escalation impossible. Fix #338 adds: when nat-vent activates (or re-activates from paused), `_apply_nat_vent_hvac_state()` immediately re-arms the appropriate band (full or floor-only per `aggressive_savings`), closing a gap where Path B re-activation deferred re-arming up to 30 min. | [§6e — Nat-vent and economizer with the band armed](08-COMPUTATION-REFERENCE.md#nat-vent-and-economizer-with-the-band-armed) |
| What band does `_apply_nat_vent_hvac_state()` arm when nat-vent activates, and how does `aggressive_savings` affect it? | `FAN_MODE_WHOLE_HOUSE`/`DISABLED` → no-op. `FAN_MODE_HVAC` + `aggressive_savings=False` → full band `[comfort_heat, comfort_cool]`. `FAN_MODE_HVAC` + `aggressive_savings=True` → floor-only (heat @ `comfort_heat`, ceiling disarmed — no compressor through open windows). Called at initial activation, paused re-activation, and every 30-min `apply_classification()` cycle. | [§6e — `_apply_nat_vent_hvac_state()`](08-COMPUTATION-REFERENCE.md#_apply_nat_vent_hvac_state--band-arming-on-nat-vent-activate-fix-338) |
| How does the solar phase offset resolver decide which EWMA to use, and what is the fallback? | Fresh primary wins; fresh secondary (≥ 3 obs) next; then stale primary; stale secondary; generic default only when nothing has ever been learned. Staleness = last_obs_date absent or > 90 days old (`THERMAL_PARAM_STALE_DAYS`). | [§5e-viii Two-EWMA Solar Phase Architecture](08-COMPUTATION-REFERENCE.md#5e-viii-solar-phase-offset--two-ewma-architecture-issue-312) |
| What quality gates must a chart_log day pass before the AC duty cycle solar phase method estimates an offset? | Five gates: setpoint_cool field present; setpoint in [68, 80]°F; spread < 1.5°F over 11:00–18:00; ≥ 4 cool entries in 11:00–16:00; at least one 11:00–16:00 entry has indoor > median setpoint. | [§5e-viii AC duty cycle quality filter](08-COMPUTATION-REFERENCE.md#5e-viii-solar-phase-offset--two-ewma-architecture-issue-312) |
| What are the two fan archetypes and how does each affect HVAC mode and fan-stops-on-close behavior? | `FAN_MODE_HVAC` (HVAC blower): band stays armed, HVAC unchanged when fan activates, fan does NOT stop when windows close unless `_natural_vent_active=True`. `FAN_MODE_WHOLE_HOUSE` (exhaust fan): HVAC set to off on activation (mode captured in `_pre_fan_hvac_mode`), restored on deactivation, fan stops when ALL sensors close even if `_natural_vent_active=False`. | [§9 Fan Archetype Behavioral Contract](08-COMPUTATION-REFERENCE.md#fan-archetype-behavioral-contract-issue-277) |
| Why does `_set_hvac_mode("off")` also set `_fan_command_time` (Issue #277 Bug A1)? | The `set_fan_mode(auto)` assertion inside `_set_hvac_mode("off")` sets `_fan_command_time = dt_util.now()` before the service call, so cloud thermostat echoes of the fan_mode attribute change are suppressed within the `_is_recent_fan_command` window instead of triggering a false manual override. | [§9b Race Guard — `_set_hvac_mode("off")` fan_command_time](08-COMPUTATION-REFERENCE.md#_set_hvac_mode-off-fan_command_time-guard-issue-277-bug-a1) |
| How does `_async_thermostat_changed()` prevent a single event from triggering both a setpoint and a fan override (Issue #277 Bug B)? | A local `_setpoint_override_detected` flag is initialized to `False` before Block 2 (setpoint detection) and Block 3 (fan_mode detection). If Block 2 fires and sets it `True`, Block 3 is suppressed via `and not _setpoint_override_detected`. One event → at most one override type. | [§9b Setpoint/Fan Mutual Exclusion](08-COMPUTATION-REFERENCE.md#setpointfan-override-mutual-exclusion-issue-277-bug-b) |
| What override and grace state is preserved vs discarded on HA restart (Issue #282/#306)? | Both pause state (`_paused_by_door`, `_pre_pause_mode`) and override state (`_manual_override_active`, `_grace_active`, `_override_confirm_pending`) are discarded — CA always starts in full clean-slate automation mode. Open sensors are re-detected within 30–90 s via the state-change listener (None → "on" transition). A 5-minute `_first_run` settling window provides startup debounce. | [§11 Clean-Slate Override State on HA Restart](08-COMPUTATION-REFERENCE.md#clean-slate-override-state-on-ha-restart-issue-282) |
| What notification does the user receive when PATH B (transient thermostat adjustment) fires (Issue #200)? | "Brief thermostat adjustment detected — treated as transient. Climate Advisor continues normal operation." No grace period starts; automation resumes immediately. | [§11 PATH B Notification](08-COMPUTATION-REFERENCE.md#path-b-notification--transient-thermostat-adjustment-issue-200) |
| What happens if the user changes to a different HVAC mode while a grace period is already active (Issue #201)? | The current override and grace are cleared, and a fresh 10-minute confirmation window starts for the new mode. Latest user action wins. | [§11 Second Override During Active Grace](08-COMPUTATION-REFERENCE.md#second-override-during-active-grace-issue-201) |
| How does `_run_solar_phase_chart_log_fit()` stay current without re-scanning months of history on every cycle (Issue #310)? | Two-tier schedule: one-shot backfill (30-day lookback, `backfill_done` flag) runs once on fresh install; periodic daily re-fit (2-day lookback, `_last_solar_phase_fit_date` gate) runs at most once per calendar day thereafter. | [§5e-v Two-tier fit scheduling](08-COMPUTATION-REFERENCE.md#two-tier-fit-scheduling-issue-310) |
| What guarantees that a running fan always has an owner after Issues #327 and #347? | Restart now clears `_fan_override_active` for a clean slate; `_do_startup_coalesce` reconciles the physical fan state (adopt-on / turn-off / no-fan); `fan_thermostat_check()` re-evaluates on every indoor or outdoor temp change; the economizer gains an `outdoor < indoor` direction guard. Post-startup `hvac_action` transitions to `"fan"` while CA does not own the fan are caught in `_async_thermostat_changed` and resolved by `reconcile_fan_on_startup` immediately (Issue #347). `"Running (untracked)"` remains only as a brief transient, not an indefinite limbo. | [§9e Thermostatic Fan Loop and Startup Reconciliation (Issue #327)](08-COMPUTATION-REFERENCE.md#9e-thermostatic-fan-loop-and-startup-reconciliation-issue-327) |
| Why did overnight nat-vent flap between `nat-vent` and `paused — door/window open` every ~5 min with the window never actually closing? | All 5 reactivation-gate call sites used the flat daytime `comfort_heat` floor even during sleep, but the sleep-aware cycling functions used `sleep_heat`. Fixed by a shared `_nat_vent_reactivation_floor()` helper (Issue #417). | [§17 Issue #417 — sleep-aware comfort_heat, and a 5th site folded in](08-COMPUTATION-REFERENCE.md#issue-417--sleep-aware-comfort_heat-and-a-5th-site-folded-in) |
| Which nat-vent exit sites still bypass the `_exit_nat_vent()` choke point, and what changed when the last two were fixed? | None. The last two sites (`handle_all_doors_windows_closed()` and `fan_thermostat_check()`'s fast-loop) were unified in Issue #418. | [Exit Hierarchy — Unified exit handoff](08-COMPUTATION-REFERENCE.md#exit-hierarchy) |
| Why did a whole-house fan stay "adopted" (`_fan_active=True`) for 3.5+ hours while physically off, showing `"active (unconfirmed)"` all night? | `reconcile_fan_on_startup()` checked thermostat attributes for WHF state without comparing to the real fan entity. Fixed by archetype-aware ground-truth detection and a 2-tick self-healing check in the 5-min backstop (Issue #423). | [§9e-B and §9e-E](08-COMPUTATION-REFERENCE.md#9e-thermostatic-fan-loop-and-startup-reconciliation-issue-327) |
| Why did an overnight nat-vent session get torn down and re-adopted every 5-15 minutes for hours, showing "fan running (untracked)" and repeated "startup reconcile" adoptions with no window ever closing? | Phase 2 exit logic used the flat daytime `comfort_heat` instead of `_nat_vent_reactivation_floor()`, causing overnight indoor-between-sleeps to trigger false exits repeatedly. Fixed by reading the sleep-aware floor and guarding exits to only fire when `time_to_floor >= 0` (Issue #427). | [Exit Hierarchy — Issue #427](08-COMPUTATION-REFERENCE.md#exit-hierarchy) |
| Why did the AC stay armed the whole time a user manually ran the whole-house fan, fighting it while windows were open? | HVAC suppression only lived in CA's `_activate_fan()`, not in `handle_fan_manual_override()` (manual override detection). Fixed by routing both through `_suppress_hvac_for_whf()` and re-classifying on exit rather than restoring stale mode (Issue #495). | [§9 Fan Archetype Behavioral Contract](08-COMPUTATION-REFERENCE.md#fan-archetype-behavioral-contract-issue-277) · [§9d Setpoint/Fan Status Reconciliation](08-COMPUTATION-REFERENCE.md#9d-reconciling-the-setpoint-override-and-fan-override-status-lines-issue-495) |
| Why did the whole-house fan snap on and back off within the same minute when a monitored sensor bounced open/closed rapidly, even with a debounce configured? | `check_natural_vent_conditions()`'s idle_open branch didn't use the same debounce timers as the pause path. Fixed by gating idle_open on `_door_open_timers` (Issue #504); default debounce bumped to 10 min. | [§10 Door/Window HVAC Pause — Issue #504](08-COMPUTATION-REFERENCE.md#10-doorwindow-hvac-pause) |
| Why did the house go ~4.5 hours with no active cooling on a 95°F day even after windows closed, and why did the AC that finally started get shut off 5 minutes later? | `_deactivate_fan()` didn't clear `_pre_fan_hvac_mode` when windows were open, stranding WHF ownership indefinitely. `reconcile_fan_on_startup()` also couldn't distinguish a normal blower-phase transition from a real unowned fan, so it force-cancelled AC that had just started. Fixed by `release_suppression` parameter in `_deactivate_fan()` and `recent_hvac_session_ended` check in `reconcile_fan_on_startup()`. | [§9a Fan State Tracking](08-COMPUTATION-REFERENCE.md#9a-fan-state-tracking) · [§9e Thermostatic Fan Loop and Startup Reconciliation](08-COMPUTATION-REFERENCE.md#9e-thermostatic-fan-loop-and-startup-reconciliation-issue-327) |
| Why did the AC get set to cool with a monitored window still open, 5 seconds after the user turned the WHF off manually? | Three gaps: (1) `_idle_open` never checked `_grace_active`, letting grace bypass (fixed by excluding it); (2) `fan_thermostat_check()` outcomes didn't route through `_exit_nat_vent()` (fixed in Issue #418); (3) `decide_scheduled_band_gate()` read event history, not live sensor state (fixed by `_sync_paused_by_door_with_live_sensors()` helper in Issue #498). | [§9. Structural WHF/AC Mutual Exclusion](08-COMPUTATION-REFERENCE.md#9-structural-whfac-mutual-exclusion) · [Shared Scheduled-Band Gate (Issue #498)](grace-periods-spec.md#shared-scheduled-band-gate-issue-498) |
| Why did briefly opening a monitored door (e.g. walking outside) trigger an instant "HVAC paused" notification, bypassing the configured 10-minute debounce window entirely? | `_sensor_debounce_pending()` checked `_door_open_timers` (timing-dependent) instead of the sensor's `last_changed` timestamp (timing-independent). Fixed by checking both, making the guard immune to scheduling order (Issue #623). | [§ Debounce-Timer Registration Race (Issue #623)](grace-periods-spec.md#debounce-timer-registration-race-issue-623) |
| Why did the AC get silently commanded into "cool" mode with a monitored window still open, over an hour after nat-vent released ownership, with no pause and no log line even mentioning the mode change? | `_apply_comfort_band()` (the single choke-point for all 7 comfort-band callers) had no independent live-sensor check; it relied only on upstream `_paused_by_door` bookkeeping which could lag. `_set_temperature()` also didn't log its bundled `mode` parameter. Fixed by (1) a choke-point guard refusing to arm while a window is genuinely open (Issue #629, mirroring the pre-existing WHF/AC mutex choke-point from Issue #392 Fix 1b), and (2) adding `mode=` to the log line. | [§9. Structural WHF/AC Mutual Exclusion](08-COMPUTATION-REFERENCE.md#9-structural-whfac-mutual-exclusion) |
| Why did HVAC mode flip to "cool" right after a redeploy/restart, with the dashboard still correctly showing "windows open" and the door/window sensor genuinely open the whole time? | A monitored sensor blipped `unavailable → on` during HA startup, resetting `last_changed` on an old open. The choke-point guard then saw "pending" for 10 min and skipped the pause. Fixed by teaching the listener to recognize `unavailable`/`unknown` → open as a reconnect blip and exclude it from debounce timing (Issue #645, which closed a gap the Issue #629 choke-point guard above didn't cover). | [§9. Structural WHF/AC Mutual Exclusion](08-COMPUTATION-REFERENCE.md#9-structural-whfac-mutual-exclusion) |

## 1. Day Classification

Today's high temperature is compared against fixed thresholds to assign a `day_type`. All downstream decisions (HVAC mode, setpoints, window advice, pre-conditioning) flow from this classification.

| today_high condition | day_type | HVAC mode (classifier) | Constant name |
|---|---|---|---|
| `today_high >= 85` | `hot` | `cool` | `THRESHOLD_HOT = 85` |
| `75 <= today_high < 85` | `warm` | `off` ¹ | `THRESHOLD_WARM = 75` |
| `60 <= today_high < 75` | `mild` | `off` ¹ | `THRESHOLD_MILD = 60` |
| `45 <= today_high < 60` | `cool` | `heat` | `THRESHOLD_COOL = 45` |
| `today_high < 45` | `cold` | `heat` | _(below all thresholds)_ |

¹ The `off` field in `DayClassification` is a historical label from the classifier's perspective (no active HVAC needed at peak). In practice, the automation engine programs a comfort band (floor = `comfort_heat`, ceiling = `comfort_cool` while occupied + awake) rather than issuing an actual `hvac_mode=off` command — the thermostat holds the band autonomously and runs the compressor only if natural ventilation can't keep up. See [§6e Comfort-Band Programming](#6e-comfort-band-programming-issue-249).

---

## 2. Trend Computation

The trend is computed from the difference between tomorrow's and today's forecast highs and lows:

```
avg_delta = ((tomorrow_high - today_high) + (tomorrow_low - today_low)) / 2
trend_magnitude = abs(avg_delta)
```

| avg_delta condition | trend_direction |
|---|---|
| `avg_delta > 2` | `warming` |
| `avg_delta < -2` | `cooling` |
| `-2 <= avg_delta <= 2` | `stable` |

---

## 3. Setback Modifier

The setback modifier adjusts how aggressively the system setbacks or pre-conditions based on the incoming trend. It is applied on top of base setback values during occupancy-away, vacation, and bedtime calculations (see Section 5).

| trend_direction | trend_magnitude condition | setback_modifier | pre_condition_target | Notes |
|---|---|---|---|---|
| `cooling` | `magnitude >= 10` (significant) | `+3.0` | `+3.0°F above comfort_heat` | Big cold front — don't set back far, pre-heat |
| `cooling` | `5 <= magnitude < 10` (moderate) | `+2.0` | `+2.0°F above comfort_heat` | Moderate cold front — slight pre-heat |
| `stable` | any | `0` | none | No adjustment |
| `warming` | `5 <= magnitude < 10` (moderate) | `-2.0` | none | Warming coming — set back further tonight |
| `warming` | `magnitude >= 10` (significant) | `-3.0` | none | Strong warming — aggressive setback tonight |

Threshold constants: `TREND_THRESHOLD_SIGNIFICANT = 10`, `TREND_THRESHOLD_MODERATE = 5`.

---

## 4. Pre-Conditioning

Pre-conditioning sets the HVAC system up ahead of an expected temperature change.

| Trigger | Target temperature formula | When applied | Exit condition |
|---|---|---|---|
| Moderate cold front (`cooling`, magnitude 5–9°F) | `comfort_heat + 2.0` | Scheduled at 7:00 PM | Not yet implemented. |
| Significant cold front (`cooling`, magnitude ≥ 10°F) | `comfort_heat + 3.0` | Scheduled at 7:00 PM | Not yet implemented. |
| ODE ceiling defense (`warm` or `mild` day, model calibrated, breach predicted) | `comfort_cool` | Reactive: passive safety backstop (§6c); naturally dormant when the comfort band is armed because the band's ceiling already holds the house below `comfort_cool` | N/A — fires only when a breach is predicted; not a sustained hold. |

> **Issue #249 — band model change:** Warm and mild days previously issued an `hvac_mode=off` command at classification time and relied on §6b/§6c guards to rescue the home if temperatures drifted. The automation engine now programs the occupied comfort band `[comfort_heat, comfort_cool]` (suppression to setback applies only away/asleep) instead. The thermostat holds both edges autonomously; the pre-conditioning column above reflects the new steady-state where the ODE ceiling guard is a passive backstop rather than the primary defense. See [§6e](#6e-comfort-band-programming-issue-249).

> **Issue #558 — hot-day daytime pre-condition removed.** Prior to Issue #558, hot days also set `pre_condition_target = -2.0`, lowering the *daytime* comfort ceiling to `comfort_cool - 2` until an achievement flag (`_pre_condition_achieved`) fired once indoor reached the target, gated correctly in `select_comfort_band()`/`apply_classification()` but **not** in `_set_temperature_for_mode()`, which 5 separate "resume comfort" event handlers (occupancy-home, door/window-resume, nat-vent-exit, dashboard-resume, economizer-off) called without checking the flag at all. This caused an audible, energy-expensive daytime AC chase toward the lower target on any resume event after the home had been away/paused overnight on a hot day — most visibly right after a multi-day trip. Root-cause investigation found the daytime offset was also largely redundant: the sleep band (`sleep_cool`, always active overnight regardless of day type) and the separate overnight pre-cool banking mechanism (§5a-i, below) already do the real work of getting the house cold before heat arrives. The daytime mechanism was removed entirely rather than gated — `classifier.py` no longer sets `pre_condition`/`pre_condition_target` for hot days, and `select_comfort_band()`/`_set_temperature_for_mode()` contain no pre-cool offset branch at all. All hot-day thermal-mass banking now happens exclusively through §5a-i's nighttime-only mechanism, whose trigger was simultaneously broadened to close a related gap — see below. Golden scenario `hot_day_precool_achieved_reverts_to_comfort` (Issue #295), which asserted the removed daytime ceiling-lowering behavior, was retired to `tools/simulations/unsupported/`.

**Cold-front pre-heat detail:** The pre-heat target is stored in `config["_pending_preheat"]` for the coordinator to schedule. The target is `comfort_heat + pre_condition_target` (e.g., 70 + 3 = **73°F** for a significant cold front). This is unaffected by Issue #558 — only the hot-day/cooling branch was removed.

---

## 5. Temperature Setpoints by Context

Default values used in examples: `comfort_heat = 70`, `comfort_cool = 75`, `setback_heat = 60`, `setback_cool = 80`.

| Context | Heat Mode Formula | Cool Mode Formula | Example (heat) | Example (cool) |
|---|---|---|---|---|
| Home (comfort) | `comfort_heat` | `comfort_cool` | 70°F | 75°F |
| Away | `setback_heat + setback_modifier` | `setback_cool - setback_modifier` | 60°F (modifier=0) | 80°F (modifier=0) |
| Vacation | `setback_heat + setback_modifier - VACATION_SETBACK_EXTRA` | `setback_cool - setback_modifier + VACATION_SETBACK_EXTRA` | 57°F (modifier=0) | 83°F (modifier=0) |
| Guest | Same as Home — dispatches to `handle_occupancy_home()` | Same as Home | 70°F | 75°F |
| Bedtime | `compute_bedtime_setback()` (see §5a) | `compute_bedtime_setback()` (see §5a) | 66°F (modifier=0, no model) | 78°F (no model) |
| Morning Wakeup | `comfort_heat` | `comfort_cool` | 70°F | 75°F |
| Pre-cool (hot day) | n/a | `comfort_cool - 2` | n/a | 73°F |
| Pre-heat (cold front, moderate) | `comfort_heat + 2` | n/a | 72°F | n/a |
| Pre-heat (cold front, significant) | `comfort_heat + 3` | n/a | 73°F | n/a |

**Notes:**
- Bedtime setback depth is now computed by `compute_bedtime_setback()` in `automation.py` (see §5a). When `sleep_heat` / `sleep_cool` are explicitly configured (#101), those values are used directly as the bedtime setpoint, bypassing the adaptive depth computation. The hardcoded defaults (`DEFAULT_SLEEP_HEAT = 66°F`, `DEFAULT_SLEEP_COOL = 78°F`) apply when neither sleep temps are configured nor thermal model data is available.
- Bedtime cool still applies the same `+3°F` offset logic at default; when the thermal model is active, the depth is scaled to ensure the house warms/cools back to comfort within the overnight recovery window.
- Bedtime heat continues to incorporate `setback_modifier` on top of the computed depth.
- `VACATION_SETBACK_EXTRA = 3` degrees beyond the normal setback.
- Guest mode calls `handle_occupancy_home()` directly — no separate handler.
- Morning wakeup is skipped when occupancy is `away` or `vacation` (Issue #85).
- Bedtime setback is skipped when occupancy is `vacation` (vacation setback is deeper).
- The daily briefing TLDR table shows setback temps and an occupancy status row when not home. The HVAC Mode row's setback temp no longer repeats "(setback — away/vacation)" — the Occupancy row directly below it already states that fact (Issue #555 dedup, needed to fit HA's 255-char sensor state limit).

### 5a. Adaptive Bedtime Setback (`compute_bedtime_setback()`)

Bedtime setback depth is computed from the thermal model HVAC rates and the overnight recovery window:

| Condition | Heat Mode | Cool Mode |
|---|---|---|
| Thermal model confidence is `"none"` | Fall back to `DEFAULT_SETBACK_DEPTH_F = 4°F` below `comfort_heat` | Fall back to `DEFAULT_SETBACK_DEPTH_COOL_F = 3°F` above `comfort_cool` |
| Model available | Depth = `heating_rate_f_per_hour` × recovery_window_hours; clamped to `[MIN_SETBACK_DEPTH, MAX_SETBACK_DEPTH]` | Same formula using `cooling_rate_f_per_hour` |

`heating_rate_f_per_hour` and `cooling_rate_f_per_hour` are the legacy alias fields returned by `get_thermal_model()` — they equal `abs(k_active_heat)` and `abs(k_active_cool)` respectively. Both are `None` when no model data is available, which triggers the fallback.

`setback_modifier` is always added to the heat setback result regardless of whether the model or the fallback was used.

**Cool-mode sign convention (Issue #258):** For cool-mode nights, `setback_modifier < 0` means a warming trend — the next day will be hotter. The modifier is applied as `sleep_cool + setback_modifier`, which _lowers_ the cool ceiling (more aggressive cooling, thermal mass banking). A _positive_ modifier (cooling trend) _raises_ the ceiling (relaxed setback, AC cycles less). No sign flip is applied in cool mode.

**Nat-vent continuation gate at bedtime (Issue #370):** `handle_bedtime()` evaluates the sleep band before deciding whether to deactivate the fan. When nat-vent is active (`_natural_vent_active=True`), the fan is running under CA control (`_fan_active=True`), no manual override is in effect, and `outdoor_temp < sleep_band.ceiling` (outdoor air is still cooler than the sleep target), bedtime skips fan deactivation and emits `nat_vent_bedtime_continue`. The fan then cycles on/off around the sleep-window midpoint via `nat_vent_temperature_check()`; the session only ends via the Priority 2 comfort-floor exit (see Exit Hierarchy below), which is itself sleep-aware as of Issue #402. If any gate fails, `_deactivate_fan()` is called and `_natural_vent_active` is cleared to `False`. This applies to all fan archetypes (WHF, HVAC fan, BOTH).

### 5a-i. Overnight Pre-Cool Phase (Issue #258, trigger broadened in #558)

On eligible nights, the coordinator schedules a second setpoint change mid-night — after nat-vent has had its window — to bank cold thermal mass before the afternoon peak. Eligibility and target modifier are decided by `resolve_pre_cool_modifier(classification, config)` in `automation.py`, the single source of truth for **all** 5 call sites (`handle_pre_cool()`, `_compute_pre_cool_trigger_time()`, `_maybe_schedule_pre_cool()`, `_maybe_reschedule_pre_cool_on_nat_vent_exit()`/`_decide_pre_cool_reschedule()`, the chart target-band dip, and the ODE predicted-indoor curve):

```
if classification.setback_modifier < 0:            # warming trend (original Issue #258 gate)
    modifier = classification.setback_modifier
elif classification.tomorrow_high >= threshold_hot: # Issue #558: tomorrow independently hot
    modifier = HOT_DAY_PRE_COOL_MODIFIER (-2.0)
else:
    modifier = None   # not eligible tonight
```

**Issue #558 rationale:** the original trend-only gate (`setback_modifier < 0`, requiring a ≥5°F day-over-day warming jump) misses a plateaued heat wave — several consecutive hot days with no single night trending sharply warmer than the last. Without the fallback, such nights got **zero** overnight banking beyond the flat `sleep_cool` floor, indefinitely, even though every one of those days was genuinely hot. The fallback reuses the exact magnitude of the daytime hot-day offset that Issue #558 removed (§4), just relocated permanently into this patient, nighttime-only mechanism instead of a daytime catch-up chase. A positive `setback_modifier` (cooling trend) falls through to the hot-day check exactly like `0` — a genuine cooling trend doesn't independently suppress the hot-day fallback if tomorrow is still forecast hot.

**Trigger timing** (coordinator `_compute_pre_cool_trigger_time()`):

| Condition | Trigger time |
|---|---|
| `classification.window_close_time` is set (nat-vent configured) | `window_close_time + PRE_COOL_POST_NAT_VENT_DELAY_MINUTES (30 min)` — gives nat-vent a complete window first |
| No nat-vent config | `wake_time − PRE_COOL_WAKE_OFFSET_HOURS (4 h)` — fallback |
| `resolve_pre_cool_modifier()` returns `None` | No trigger scheduled (not eligible tonight) |

**Target formula** (`compute_pre_cool_target()` in `automation.py`, unchanged by #558 — only the modifier fed into it now comes from `resolve_pre_cool_modifier()` instead of the raw `setback_modifier`):

```
raw_target  = sleep_cool + modifier           # modifier is negative → lower ceiling
floor       = sleep_heat + hysteresis         # same "+1 above the floor" convention as
                                               # nat_vent_temperature_check()'s sleep-window cycling
pre_cool_target = max(raw_target, floor)      # clamp prevents dropping below the sleep floor
```

The floor guard prevents the home from dropping below the sleep band's own floor (`sleep_heat`), so pre-cool can travel the full `[sleep_heat, sleep_cool]` range on a strong warming trend instead of being clamped near daytime `comfort_heat` (the pre-Issue-#436 formula, whose floor left little to no headroom once `sleep_cool` was reformatted to a flat, cooler-than-daytime household default).

**Nat-vent bypass condition:** If the pre-cool trigger fires with `nat_vent_just_closed=True` AND `indoor_temp ≤ pre_cool_target`, the AC service call is suppressed — free cooling via open windows already achieved the target. Event `pre_cool_suppressed_nat_vent` is emitted.

**Applied path:** `_apply_comfort_band(ComfortBand(floor=sleep_heat, ceiling=pre_cool_target, active="ceiling"))` — the heat floor is preserved from the current sleep band. Event `pre_cool_applied` is emitted with `{target, modifier, sleep_cool, floor, indoor, nat_vent_suppressed}`.

**Skip conditions:**

| Condition | Result |
|---|---|
| `resolve_pre_cool_modifier()` returns `None` (no warming trend and tomorrow not hot) | Skip silently |
| Occupancy is `away` or `vacation` | Skip (setback already active) |
| `_manual_override_active` | Skip (user in control) |
| `indoor_temp is None` with `nat_vent_just_closed=True` | No bypass possible → apply setpoint |

**Morning guard:** `handle_morning_wakeup(indoor_temp=...)` now accepts the current indoor temperature. If `indoor_temp < comfort_heat` at wake-up, event `pre_cool_overshoot` is emitted (diagnostic) and the heat may fire. The floor guard on `pre_cool_target` is the primary prevention; the morning guard is observability for cases where thermal drift exceeded the floor.

**Status visibility:** Coordinator exposes `pre_cool_status` string in `_async_update_data()` result dict → `api.py` status response → dashboard Automation Status card (secondary line when non-null). Values: `"pre-cool tonight (75°F @ 2:30 AM)"` / `"pre-cool active (75°F ceiling)"` / `"pre-cool suppressed · nat-vent cooled to 74°F"` / `null` (not eligible tonight).

**Chart:** `_compute_target_band_schedule()` accepts `pre_cool_trigger_h` and `pre_cool_target` params. When non-null, the band ceiling steps down from `sleep_cool` to `pre_cool_target` at the trigger hour and holds until `wake_time`.

**Briefing:** the hot-day conversational plan (`briefing._hot_day_plan()`) mentions overnight pre-cool only when `resolve_pre_cool_modifier()` returns non-`None` for tonight, phrased prospectively ("Tonight I'll pre-cool..."), never as a past-tense claim about a morning that may not have happened this way (Issue #558 — the prior hardcoded "I pre-cooled... this morning" line made a specific factual claim regardless of ground truth).

**Test coverage:** `tests/test_pre_cool.py`, `tests/test_pre_cool_reschedule.py`; golden scenarios `warming_trend_pre_cool_applied`, `warming_trend_pre_cool_nat_vent_bypass` (Issue #258), and `hot_plateau_pre_cool_applied` (Issue #558 — covers the hot-day-fallback-only case with `setback_modifier=0.0`).

### 5b. Adaptive Pre-heat Start Time

The pre-heat start time is computed from the thermal model heating rate and the temperature delta to be recovered:

| Condition | Pre-heat Start |
|---|---|
| No model data (`heating_rate_f_per_hour` is `None`) | Fall back to `DEFAULT_PREHEAT_MINUTES = 120` before wakeup |
| Model available | `minutes = (temp_delta / heating_rate_f_per_hour) × 60 × 1.3` (1.3× safety margin); clamped to `[MIN_PREHEAT_MINUTES=30, MAX_PREHEAT_MINUTES=240]` |

The temperature delta is `comfort_heat − bedtime_setpoint`. The safety margin of 1.3× ensures the house reaches comfort even on colder-than-average mornings.

### 5c. Predicted Temperature Graph — Physics Path

From Issue #114, when the thermal model has confidence ≥ `"low"` and `k_passive < 0`, the dashboard temperature forecast uses the ODE analytical solution to simulate future indoor temperatures instead of simple ramp interpolation:

```
T(t+dt) = T_outdoor + (T - T_outdoor) * exp(k_p * dt) + (Q/k_p) * (exp(k_p * dt) - 1)
```

`_simulate_indoor_physics()` in `coordinator.py` implements one ODE time step. `_build_predicted_indoor_future()` drives the simulation forward through the schedule, switching `Q` between `k_active_heat`, `k_active_cool`, and `0` depending on the HVAC mode in each period.

`_build_predicted_indoor_future()` accepts `occupancy_mode` (default `OCCUPANCY_HOME`) and `classification` parameters. It pre-computes the band schedule once via `_compute_target_band_schedule()` — passing `thermal_model`, `classification`, and `setback_modifier` — before iterating forecast hours. This means the predicted indoor curve uses the same adaptive sleep setpoints as the automation engine, and correctly targets setback temperatures on away/vacation days. Vacation mode propagates setback to all forecast days; away mode applies setback to today only.

**Gate bridge self-healing (Issue #126 Phase A):** When `k_passive` is `None` but
`k_vent_window` is available (homes with ventilated-only observations and no passive or
HVAC cycles), the coordinator promotes `k_vent_window` to stand in as the proxy decay
rate. Two bugs fixed:

- **Bug A:** The bridge now fires when `_conf_k_passive == "none"` (string equality), not
  only when `k_passive is None`. Pre-Issue #126 installs that stored `k_passive=None` with
  `confidence="none"` self-heal automatically on the next coordinator update — the bridge
  detects the "none" string and promotes `k_vent_window`.
- **Bug B:** The `_k_passive_via_bridge=True` flag bypasses the `_physics_eligible()`
  confidence check. Without this flag, bridge-provided k_passive would still fail the
  `conf != "none"` guard and fall through to the ramp path, defeating the purpose of the
  bridge.

Install states handled:

| Install state | k_passive | confidence | k_vent_window | Bridge fires? | Physics eligible? |
|---|---|---|---|---|---|
| Fresh — no data | `None` | `"none"` | `None` | No (nothing to promote) | No — ramp |
| Contaminated — old bug | `None` | `"none"` | valid | Yes — promotes k_vent_window | Yes — physics |
| Healed — bridge ran | promoted value | `"none"` (unchanged) | valid | Not needed (k_passive set) | Yes — bypass flag |
| Normal — HVAC obs | valid | `"low"`/`"medium"`/`"high"` | any | Not needed | Yes — normal path |

**Fallback (ramp interpolation):** When model confidence is `"none"` or `k_passive` is unavailable/non-negative, the legacy ramp path runs:

| Condition | Ramp Duration |
|---|---|
| No model data | Default 30-minute ramp |
| Model available (legacy path only) | `ramp_hours = temp_delta / rate`; minimum 15 minutes; computed by `_compute_ramp_hours()` |

`_compute_ramp_hours()` uses whichever rate applies to the transition direction (heating rate for rising ramps, cooling rate for falling ramps).

### 5d. Dynamic Target Band — `_compute_target_band_schedule()`

From Issue #119, the chart's "Target Band" overlay is no longer two static scalars. `get_chart_data()` calls `_compute_target_band_schedule()` once (pre-computed before the loop) to produce a time-series `[{ts, lower, upper}]` covering every forecast hour, and passes this as `target_band` in the API response.

**Function signature:** `_compute_target_band_schedule(hourly_timestamps, config, occupancy_mode, now, setback_modifier=0.0, thermal_model=None, classification=None) → list[{ts, lower, upper}]`

**Per-timestamp band logic:**

| Occupancy / time condition | lower | upper |
|---|---|---|
| Away — today only | `setback_heat + setback_modifier` | `setback_cool − setback_modifier` |
| Vacation — **all forecast days** | `setback_heat + setback_modifier − VACATION_SETBACK_EXTRA` | `setback_cool − setback_modifier + VACATION_SETBACK_EXTRA` |
| Home/guest — pre-wake (`h_n < wake_h`) | `sleep_heat` | `sleep_cool` |
| Home/guest — wake ramp (2h linear) | Interpolates `sleep_heat → comfort_heat` | Interpolates `sleep_cool → comfort_cool` |
| Home/guest — awake (`wake_h+2h ≤ h_n < sleep_h`) | `comfort_heat` | `comfort_cool` |
| Home/guest — sleep ramp (1h linear) | Interpolates `comfort_heat → sleep_heat` | Interpolates `comfort_cool → sleep_cool` |
| Home/guest — post-sleep (`h_n ≥ sleep_h+1h`) | `sleep_heat` | `sleep_cool` |
| Away — **future days** (tomorrow+) | Normal home/guest schedule (assumes return) | Same |

**`setback_modifier` parameter:** The trend-based offset from `DayClassification` (see §3). Positive values (cold front coming) narrow the setback; negative values (warm trend) widen it. Passing `setback_modifier` ensures the chart band and the automation engine use identical setback bounds on trend days.

**Vacation scope:** Vacation mode applies deep setback to **all** forecast days (today and future), not just today. This reflects that a vacationing household is away for the entire forecast window. Away mode applies setback to today only (assumes a return by tomorrow).

**Night-owl schedule normalization:** When `sleep_time < wake_time` (e.g., sleep=01:00, wake=09:00), the schedule wraps past midnight. The function normalises by adding 24 to `sleep_h` (making it e.g. 25) and computing `h_n = h + 24 if night_owl and h < wake_h else h` for each timestamp's local hour. This maps all timestamps onto a continuous `[wake_h, sleep_h]` number line regardless of the midnight boundary.

**Adaptive sleep temperatures (G1/G2):** When both `thermal_model` and `classification` are provided, `sleep_heat` and `sleep_cool` are derived from `compute_bedtime_setback(config, thermal_model, classification)` — the same function used by `automation.py`. This eliminates the three-implementation gap between chart band, physics prediction, and automation setpoints: all three now derive sleep temps from the same adaptive logic. When `thermal_model` or `classification` is `None`, the fallback values (`comfort_heat − DEFAULT_SETBACK_DEPTH_F`, `comfort_cool + DEFAULT_SETBACK_DEPTH_COOL_F`) are used.

**Notes:**
- `sleep_heat` and `sleep_cool` base fallbacks are `comfort_heat − 4°F` and `comfort_cool + 3°F` respectively, but are overridden when the user has explicitly configured sleep temperatures (Issue #101). Adaptive `compute_bedtime_setback()` output is used in preference to both when a thermal model is available.
- HVAC-off days (warm/mild) still display the full target band. The system actively monitors and will engage heating or cooling if indoor temperature wanders outside the target range.
- The chart layer was renamed from "Comfort Band" to "Target Band" in Issue #119 to reflect that the band now varies over time.
- `_build_predicted_indoor_future()` pre-computes the band schedule once via `_compute_target_band_schedule()` before iterating forecast hours (Issue #119 Phase 2 fix for B3 — eliminates redundant per-hour recomputation).

**Per-hour k selection — ventilation wiring (Issue #126 Phase 2C):** For forecast hours where `classification.windows_recommended=True` and `local_ts.time()` falls in `[window_open_time, window_close_time)`, the ODE uses `k_vent_window` as the effective decay rate instead of `k_passive`. `k_vent_window` is the **total** measured k during ventilated conditions (not an incremental addend) — so it replaces, not supplements, `k_passive`. Gate bridge guard: when `_k_passive_via_bridge=True` (k_passive was `None` and k_vent_window was already promoted to proxy k_passive for all hours), per-hour substitution does not fire — k_vent_window is already in play for the entire forecast and double-substitution would be incorrect. During sunny window-open hours, the combined ODE is `dT/dt = k_vent_window*(T_out − T_in) + k_solar*solar_factor`; for a thermally inert home (k_vent_window ≈ 0) this reduces to `dT/dt ≈ k_solar*solar_factor`, correctly predicting solar-driven warming even with windows open.

### 5e. Thermal Model v3 — Observation Types (Issue #121)

The thermal model collects observations from six parallel observation types, not just
HVAC heat/cool cycles. Multiple observation types can run concurrently in a
`_pending_observations` dict keyed by obs_type string.

| Type | Trigger | Measures | Min samples |
|------|---------|----------|-------------|
| `hvac_heat` | hvac_action=heating | k_active_heat, k_passive (via pre-heat buffer) | 10 post-heat |
| `hvac_cool` | hvac_action=cooling | k_active_cool | 10 post-heat |
| `passive_decay` | HVAC off, fan off, windows closed, \|ΔT\| ≥ 3°F | k_passive | 30 |
| `fan_only_decay` | Fan active, HVAC off, windows closed | k_vent | 15 |
| `ventilated_decay` | Any window open, HVAC off | k_vent_window | 20 |
| `solar_gain` | HVAC off, fan off, windows closed, T_in > T_out, daytime | k_solar | 20 |

**HVAC plateau guard**: reduced from 1.0°F to 0.3°F (`THERMAL_HVAC_MIN_DECAY_F`). The 1.0°F
guard rejected all observations on short-cycling thermostats (avg cycle < 1°F rise).

**ODE (v3)**: `dT/dt = (k_passive + k_vent_eff)*(T_out - T_in) + k_solar*solar_factor + Q_hvac`
where `k_vent_eff = k_vent` when ventilation is active, `solar_factor` = sinusoidal 0→1→0
over daylight hours (8–18 local), `Q_hvac = ±k_active` when HVAC is driving toward setpoint.

**Confidence grades**: `confidence_k_passive` is graded independently of `confidence_k_hvac`.
Physics prediction activates when either confidence is > "none", enabling prediction on
homes with passive-only observations (zero HVAC cycles recorded).

`confidence_k_solar` is graded from `observation_count_solar` (fixed in Issue #308 — was hardcoded `"none"`):

| Threshold | Grade |
|---|---|
| 0–19 observations | `"none"` |
| ≥ 20 observations | `"low"` |
| ≥ 50 observations | `"medium"` |
| ≥ 100 observations | `"high"` |

`confidence_k_solar` is exposed as an alias key in the dict returned by `get_thermal_model()`.

#### 5e-i. Sampling Cadence — Per-Type Decimation (Issue #122 H1)

The coordinator polls every 30 seconds. Sampling slow decay phenomena at poll rate yields
noise — inter-sample temperature change is dominated by sensor quantisation, not the
signal. A per-type wall-clock gate in `_sample_all_observations()` section A limits how
often a sample is appended to each observation's `samples` list:

| Type | Sample interval | Constant |
|------|----------------|----------|
| `hvac_heat` / `hvac_cool` active phase | Every poll (no gate) | — |
| `hvac_heat` / `hvac_cool` post-heat phase | 5 min | `THERMAL_HVAC_POST_HEAT_SAMPLE_INTERVAL_S` |
| `passive_decay` | 5 min | `THERMAL_PASSIVE_SAMPLE_INTERVAL_S` |
| `fan_only_decay` | 2 min | `THERMAL_FAN_SAMPLE_INTERVAL_S` |
| `ventilated_decay` | 5 min | `THERMAL_PASSIVE_SAMPLE_INTERVAL_S` |
| `solar_gain` | 5 min | `THERMAL_SOLAR_SAMPLE_INTERVAL_S` |

The gate timestamp is stored as `"last_sample_time"` in the observation dict. HVAC
active-phase sampling is ungated — fast HVAC dynamics benefit from maximum resolution.
`fan_only_decay` uses a 2-minute interval because fan-assisted heat transfer is faster
than pure passive drift.

**Convergence**: A 6-hour overnight passive window at 5-min decimation yields ~72 samples
— vs. 720 noise-dominated samples at poll rate. The 30-sample minimum for `passive_decay`
requires roughly 2.5 hours of clean uninterrupted signal to commit.

#### 5e-ii. Rolling-Window Commits (Issue #122 H2)

Long observation windows are accurate but slow to yield a commit. Rolling commits break
each long passive/vent/solar observation into consecutive 30-minute slices. When
`THERMAL_ROLLING_WINDOW_MINUTES (30 min)` elapses since the observation started (or
since the last rolling commit), `_commit_rolling_window_obs()` fires:

1. Requires at least 3 samples in the window.
2. For `passive_decay` and `solar_gain`: requires total indoor ΔT ≥
   `THERMAL_ROLLING_MIN_DELTA_T_F (0.2°F)`. This guards against noise-fitting on
   near-flat data in short windows (< 10 samples).
3. For `fan_only_decay` and `ventilated_decay`: the ΔT guard is skipped
   (`skip_delta_guard=True`) because the signal guarantee is the indoor–outdoor
   differential (already checked by the observation's trigger condition), not the
   temperature trend.
4. All rolling commits use `force_grade="low"` (EWMA α = 0.05).
5. After commit, the observation is popped from `_pending_observations`. Section B of
   `_sample_all_observations()` restarts it on the next poll if conditions still hold.

**Convergence impact**: Rolling windows yield ~16 `passive_decay` commits per 8-hour
overnight window (480 min ÷ 30 min) vs. 1 commit per full-night window in v2. The model
reaches 5% accuracy in ~4 nights (α = 0.05) vs. ~60 nights before.

#### 5e-iii. Wall-Clock Abandon Timeout (Issue #122 H4)

`ventilated_decay` and `fan_only_decay` abandon after `THERMAL_DECAY_MAX_WINDOW_MINUTES
(60 min)` if rolling commit has not fired and the signal has not met the minimum ΔT
threshold. Abandon reason logged: `"max_window_elapsed_low_signal"`. This prevents
stale near-equilibrium observations from persisting when a window is left open or the
fan is running with indoor and outdoor temps nearly equal.

`passive_decay` and `solar_gain` do not have this timeout — rolling commits bound their
window length naturally.

#### 5e-iv. `_update_thermal_model_cache()` — E6 Parameter Routing Fix (Issue #122)

Each committed observation updates the EWMA cache via `learning._update_thermal_model_cache()`.
The `hvac_mode` field in the observation dict determines which cache field is updated:

| `hvac_mode` | Updates cache field | Count field |
|---|---|---|
| `"heat"` | `k_active_heat`, `k_passive` | `observation_count_heat` |
| `"cool"` | `k_active_cool`, `k_passive` | `observation_count_cool` |
| `"passive"` | `k_passive` only | `observation_count_passive` |
| `"fan_only"` | `k_vent` (from obs `k_passive` field) | `observation_count_fan_only` |
| `"ventilated"` | `k_vent_window` (from obs `k_passive` field); also `k_solar` when 2-param OLS fires (see §5e-v) | `observation_count_vent` |
| `"solar"` | `k_solar` (from obs `k_solar` field) | `observation_count_solar` |

**E6 fix**: Before Issue #122, the `elif mode == "passive"` branch incorrectly wrote
`k_p` to `cache["k_vent"]`. The fix removes that line — passive observations no longer
contaminate the ventilation parameter. Only `fan_only` observations update `k_vent`.

#### 5e-v. Adaptive 2-Param Ventilated OLS (Issue #126)

`ventilated_decay` observations optionally upgrade from 1-parameter OLS (solving only
`k_vent_window`) to a 2-parameter joint solve (`k_env_vent` + `k_solar`) when solar
conditions during the window provide enough variation to separate the two effects.

**Trigger condition:** At commit time, if
`max(solar_factor across samples) − min(solar_factor across samples) ≥ THERMAL_SOLAR_FACTOR_MIN_RANGE (0.30)`,
`compute_k_env_solar(samples)` runs the 2×2 normal equations:

```
[Σδ²    Σδ·sf ] [k_env ] = [Σrate·δ ]
[Σδ·sf  Σsf²  ] [k_solar]   [Σrate·sf]
```

where `δ = T_out − T_in`, `sf = solar_factor`, `rate = ΔT/Δt` for each sample pair.

**Collinearity guard:** If `|det(A)| < 1e-12`, the solve is skipped and the standard
1-param OLS path runs instead. This protects against numerical instability when `δ` and
`sf` are nearly proportional (e.g., morning window observations where outdoor temperature
and solar position track together).

**Acceptance criteria:** The 2-param result is accepted only if:
- `k_env_vent` passes the same bounds check as `k_passive` (`[THERMAL_K_PASSIVE_MIN, THERMAL_K_PASSIVE_MAX]`)
- `k_solar ≥ 0` (solar must add heat, not remove it)
- R² of the 2-param fit ≥ `THERMAL_MIN_R_SQUARED (0.2)`

**On acceptance:** `k_vent_window` in the EWMA cache is updated with `k_env_vent`
(a cleaner ventilated estimate than the 1-param result, because solar contamination is
removed). `k_solar` in the EWMA cache is updated separately via the same EWMA mechanism.
**On rejection** (collinearity, bounds failure, or low R²): the standard 1-param OLS
result for `k_vent_window` is used and `k_solar` is not updated from this observation.

**`solar_factor` in samples:** From Issue #126, `solar_factor` is recorded in each
`ventilated_decay` sample dict at collection time (not computed at commit time). Old
sample dicts without a `solar_factor` key are treated as `0.0` — the 1-param fallback
fires because `sf_range` will be 0.0 < 0.30.

**Constant:** `THERMAL_SOLAR_FACTOR_MIN_RANGE = 0.30`

**Why adaptive (not a separate obs type):** Ventilated windows are often long-duration
open events. Splitting into separate obs types would require two concurrent windows that
start and stop on the same physical event, complicating the observation lifecycle.
Upgrading the existing `ventilated_decay` observation at commit time keeps the pipeline
simple — the 2-param path is a quality improvement, not a new signal collection mechanism.

**Thermal mass lag:** The clock-based `solar_factor` (sinusoidal, peaks at solar noon) is
an approximation. Real solar heat transfer lags the solar position by 30–90 minutes due
to thermal mass (walls, floors absorbing and re-radiating heat). This approximation is
acceptable because: (a) `k_solar` is used in predictions that integrate over hour-long
periods where lag averages out; (b) the EWMA smoothing (α = 0.05 at "low" grade) further
attenuates single-observation error; (c) a cloud-aware, lag-corrected solar model is
deferred to future scope.

**`_run_solar_phase_chart_log_fit()` — structured INFO logging (Issue #308):** This method
(`coordinator.py`) estimates `solar_phase_offset_h` from passive-daytime chart_log windows
(regime: HVAC off, fan off, windows closed, local hours 8–20). As of Issue #308 it emits
structured `INFO` log lines at three points useful for diagnosing solar phase offset
learning (Issue #185):

1. **Entry** — total chart_log entries available, date range scanned, and lookback window (2 days or 30 days for backfill).
2. **Window filtering** — count of passive-daytime windows found, or an "offset unchanged" message when zero qualify.
3. **EWMA update** — per-committed-window: observed offset, old→new EWMA value, and window size. Final summary: `N/M windows committed (K rejected)`.

Individual window rejections are logged at DEBUG level with the reject reason.

##### Two-tier fit scheduling (Issue #310)

`_run_solar_phase_chart_log_fit()` is invoked on two distinct schedules so the EWMA stays current without redundant computation.

1. **One-shot backfill** (`backfill=True`, lookback up to 30 days): runs once on fresh install via `_solar_phase_backfill`, gated by a `backfill_done` flag persisted in coordinator state. This captures the full available chart_log history and produces an initial `solar_phase_offset_h` estimate before the first daily cycle runs.
2. **Periodic daily re-fit** (`backfill=False`, lookback 2 days): gated by `_last_solar_phase_fit_date` (persisted in coordinator state). Runs at most once per calendar day, and only after the one-shot backfill has completed. Each daily run folds the two most recent days of chart_log windows into the EWMA, keeping the phase offset current as new observations accumulate.

The two-tier design avoids re-scanning months of history on every coordinator cycle while ensuring that a newly deployed instance learns a reasonable phase offset from its first day of data.

#### 5e-vi. HVAC Commit Path — Single-Point Estimator and Proxy-Aware Gating (Issue #130)

Issue #130 fixed HVAC observations producing zero commits despite 60 days of heat cycles.
The root causes were: (RC1) 10-sample post-heat minimum requiring 50 min — too long for
5–30 min cycles; (RC3) `outdoor=None` at state transitions blocking sample collection;
(RC4) bridge homes with `k_passive=None` blocking `k_active` computation; (RC5) no
backfill tool.

**Fixes applied:**

| Fix | Mechanism |
|---|---|
| D14: Lower post-heat minimum | `THERMAL_MIN_POST_HEAT_SAMPLES`: 10 → 4 |
| D15: Remove stabilization gate | `_check_hvac_stabilization()` commits as soon as min samples reached; no ±0.3°F stability wait |
| D16: Outdoor temp fallback | `_last_known_outdoor_f` caches the last non-None outdoor reading; used within a 30-min window when current reading is `None` |
| D17: k_vent_window proxy | `_commit_event_from_dict()` uses `k_vent_window` as k_passive when `k_passive=None` (bridge homes); marks grade `"low"` |

**Single-point `k_active` estimator (`compute_k_active_single_point()`):**

When `n_active < 2` (cycle is shorter than the 5-min sampling interval), OLS cannot fit a
heating rate. The single-point estimator uses exact HVAC on/off timestamps:

```
k_active = (T_peak − T_start) / elapsed_hours − k_passive × avg(T_in − T_out)
```

`elapsed_hours` comes from state-change timestamps, not sample spacing, so it reflects the
true HVAC-on duration. `post[0].ts` is used as the HVAC-off timestamp when `n_active=1`.

**Signal guard (`THERMAL_HVAC_MIN_SIGNAL_F = 0.5°F`):** If `|T_peak − T_start| < 0.5°F`,
the cycle is rejected as a setpoint-maintenance run — no learnable k_active information.

**Call path:** `_commit_event_from_dict()` first attempts OLS via `compute_k_active()`.
If OLS returns `None` (insufficient samples), it falls through to
`compute_k_active_single_point()`. When bridge proxy was used for k_passive, the obs dict
emits `k_passive=None` to prevent the proxy value from contaminating the k_passive EWMA
(D21).

**Proxy-aware `n_post` gating:** `_check_hvac_stabilization()` reads `k_vent_window` from
`thermal_model_cache` at commit time. When `k_vent_window` is available and negative
(proxy present), the `n_post` minimum drops from `THERMAL_MIN_POST_HEAT_SAMPLES` (4) to 1
and the plateau guard is bypassed. For all other homes, the thresholds are unchanged.

| Condition | `n_post` minimum | Plateau guard |
|---|---|---|
| No proxy (normal or fresh install) | 4 | Active — rejects if `peak − end < 0.3°F` |
| Proxy available (`k_vent_window < 0`) | 1 | Bypassed |

**`thermal_replay --hvac` mode:** `run_hvac_replay_ols()` in `tools/thermal_replay.py`
applies the same OLS → single-point fallback and proxy-aware gating to historical chart_log
data. Use for backfilling HVAC observations after deploying Issue #130 fixes.

```bash
python tools/thermal_replay.py --hvac --days 60 --dry-run   # inspect without writing
python tools/thermal_replay.py --hvac --days 60             # commit to learning DB
```

**Implementation references:**

| Component | Location |
|---|---|
| `compute_k_active_single_point()` | `learning.py` ~line 401 |
| Single-point fallthrough in `_commit_event_from_dict()` | `learning.py` ~line 1145 |
| Proxy-aware gate in `_check_hvac_stabilization()` | `coordinator.py` ~line 2951 |
| `run_hvac_replay_ols()` | `tools/thermal_replay.py` ~line 817 |

#### 5e-vii. Thermostat Swing — Deadband Auto-Detection (Issue #102)

**Formula:** `swing_f = abs(T_end - T_start) / 2`

**Bounds:**
| Parameter | Min | Max | Notes |
|---|---|---|---|
| `swing_heat_f` | 0.1°F | 5.0°F | `THERMAL_SWING_MIN_F` / `THERMAL_SWING_MAX_F` |
| `swing_cool_f` | 0.1°F | 5.0°F | Same bounds, independent EWMA |

**Minimum signal:** `abs(T_end - T_start) >= THERMAL_HVAC_MIN_SIGNAL_F` (0.5°F).
Cycles below this produce no swing observation.

**Unit conversion:** Swing is a temperature delta — use `convert_delta()` (multiply
by 5/9 for Celsius), never `from_fahrenheit()`. The +32 offset does not apply.

**Display rule:**
- `swing_heat_f is None` → show `±1.5°F (estimated)` in gray italic
- `swing_heat_f is not None` → show `±X.X°F` with no hint

**Constants:**
| Constant | Value | Purpose |
|---|---|---|
| `THERMAL_SWING_DEFAULT_F` | 1.5 | Default before any learning |
| `THERMAL_SWING_MIN_F` | 0.1 | Sanity lower bound |
| `THERMAL_SWING_MAX_F` | 5.0 | Sanity upper bound (rejects multi-cycle blur) |
| `THERMAL_SWING_CONF_LOW` | 1 | none → low threshold |
| `THERMAL_SWING_CONF_MEDIUM` | 3 | low → medium threshold |
| `THERMAL_SWING_CONF_HIGH` | 10 | medium → high threshold |

#### 5e-viii. Solar Phase Offset — Two-EWMA Architecture (Issue #312)

The solar phase offset (`solar_phase_offset_h`) corrects the `solar_factor` sinusoid so it peaks at the hour where heat actually reaches the interior rather than at a fixed 1 pm clock-noon. Two independent EWMAs learn this offset from different signal sources; a resolver selects the best available value at call time.

**Two-EWMA architecture:**

| EWMA | Cache key | Alpha | Source | Trust |
|---|---|---|---|---|
| Primary | `solar_phase_offset_h` | 0.10 (`THERMAL_SOLAR_PHASE_ALPHA`) | Passive-decay chart_log windows (`_run_solar_phase_chart_log_fit()`) | Higher — measures thermal response directly, no confound |
| Secondary | `solar_phase_offset_ac_h` | 0.07 (`THERMAL_SOLAR_PHASE_AC_ALPHA`) | AC duty cycle peak hour (`_run_ac_duty_solar_phase_fit()`) | Lower — AC cycling is an indirect proxy; alpha is slower to reflect this |

The two EWMAs never cross-update: `update_solar_phase_offset()` in `learning.py` writes only `solar_phase_offset_h`; `update_ac_duty_solar_phase_offset()` writes only `solar_phase_offset_ac_h`.

**Resolver — `_resolve_solar_phase_offset(cache)` (`learning.py`):**

Each EWMA stores a `last_obs_date` field (`solar_phase_offset_last_obs_date`, `solar_phase_offset_ac_last_obs_date`). A parameter is **stale** if its date is absent or older than `THERMAL_PARAM_STALE_DAYS` (90 days). Stale home-specific data is still preferred over a generic default — the default is only used when nothing has ever been learned.

```
1. primary = cache["solar_phase_offset_h"]
   if primary is not None AND fresh (within 90 days) → return primary   ← preferred

2. secondary = cache["solar_phase_offset_ac_h"]
   ac_obs = cache["solar_phase_offset_ac_obs_count"]
   if secondary is not None AND ac_obs >= 3 AND fresh → return secondary

3. if primary is not None (stale) → return primary                       ← best stale data

4. if secondary is not None AND ac_obs >= 3 (stale) → return secondary  ← next best stale

5. return THERMAL_SOLAR_PHASE_OFFSET_H_DEFAULT (2)                       ← nothing ever learned
```

The secondary requires at least 3 accepted observations (`THERMAL_SOLAR_PHASE_AC_MIN_OBS`) before it is trusted in either the fresh or stale tier. The default prior (`THERMAL_SOLAR_PHASE_OFFSET_H_DEFAULT = 2`, peak at 3 pm local) is only returned when both EWMAs have never received an accepted observation.

**Staleness principle (applies to all computed thermal parameters — see Issue #314):** A learned value is only "current" if it was recently observed. An EWMA frozen by seasonal inactivity is stale, but it still encodes home-specific geometry and is a better estimate than a generic prior. The resolver prefers fresh > stale > default, with primary (passive method) winning within each tier.

`get_thermal_model()` returns `solar_phase_offset_h` as the **resolved** value — call sites (ODE, chart) receive the best available estimate without needing to know which source was used. The raw secondary EWMA is also exposed as `solar_phase_offset_ac_h` for diagnostic inspection.

**AC duty cycle quality filter — `_is_ac_duty_solar_day(day_entries)` (`coordinator.py`):**

The AC duty method is only meaningful on days where the thermostat was cooling steadily in the midday window. Days that don't meet the quality criteria are rejected before estimation.

| Gate | Criterion | Constant | Reject code |
|---|---|---|---|
| 1 | At least one 11:00–18:00 chart_log entry has a `setpoint_cool` field | — | `ac_no_cool_setpoints` |
| 1b | All setpoints in [68, 80]°F | `THERMAL_SOLAR_PHASE_AC_SETPOINT_MIN/MAX_F` | `ac_setpoint_out_of_range` |
| 2 | Setpoint spread across 11:00–18:00 < 1.5°F | `THERMAL_SOLAR_PHASE_AC_SETPOINT_STABILITY_F` | `ac_setpoint_unstable` |
| 3 | ≥ 4 cool entries in 11:00–16:00 | `THERMAL_SOLAR_PHASE_AC_MIN_COOL_ENTRIES` | `ac_insufficient_midday_activity` |
| 4 | At least one 11:00–16:00 entry has indoor > median setpoint | — | `ac_no_setpoint_breach` |

Gates are evaluated in order; the first failure returns `(False, reject_code)`. A day passing all gates returns `(True, "")`. The function is a pure module-level helper — no coordinator state.

**Estimation — `_estimate_ac_duty_solar_phase(day_entries)` (`coordinator.py`):**

1. For each hour in 11:00–16:00, compute `duty_fraction = cool_entries / total_entries`.
2. Identify `peak_hour = argmax(duty_fraction)`.
3. `offset = peak_hour − 13`, clamped to `[THERMAL_SOLAR_PHASE_OFFSET_MIN (0), THERMAL_SOLAR_PHASE_OFFSET_MAX (4)]`.

A `peak_hour` of 14 (2 pm) yields `offset = 1`; a peak at 16 (4 pm) yields `offset = 3`. Returns `None` if no cool entries exist in the window (should not occur after gate 3, but guards the return).

**Integration — `_run_ac_duty_solar_phase_fit()` (`coordinator.py`):**

Called once per coordinator update cycle (inside the `_run_solar_phase_chart_log_fit()` block). Iterates chart_log entries grouped by date. For each date, calls `_is_ac_duty_solar_day()` then `_estimate_ac_duty_solar_phase()`; on success calls `learning.update_ac_duty_solar_phase_offset(offset, date_str)`. Rejection reasons are logged at DEBUG level; accepted estimates at INFO.

**Constants summary:**

| Constant | Value | Purpose |
|---|---|---|
| `THERMAL_SOLAR_PHASE_AC_ALPHA` | 0.07 | Secondary EWMA smoothing factor |
| `THERMAL_SOLAR_PHASE_AC_MIN_OBS` | 3 | Minimum observations before secondary is trusted by resolver |
| `THERMAL_SOLAR_PHASE_AC_SETPOINT_MIN_F` | 68.0 | Setpoint range lower bound |
| `THERMAL_SOLAR_PHASE_AC_SETPOINT_MAX_F` | 80.0 | Setpoint range upper bound |
| `THERMAL_SOLAR_PHASE_AC_SETPOINT_STABILITY_F` | 1.5 | Max allowed setpoint spread across 11:00–18:00 |
| `THERMAL_SOLAR_PHASE_AC_MIN_COOL_ENTRIES` | 4 | Min cool entries in 11:00–16:00 to qualify |
| `THERMAL_SOLAR_PHASE_OFFSET_H_DEFAULT` | 2 | Default prior (resolves to 3 pm peak) |
| `THERMAL_SOLAR_PHASE_OFFSET_MIN` | 0 | Offset clamp lower bound |
| `THERMAL_SOLAR_PHASE_OFFSET_MAX` | 4 | Offset clamp upper bound (5 pm peak) |

**Test coverage:** `tests/test_solar_phase.py` — `TestAcDutySolarPhase` (quality filter reject paths, estimation, EWMA update, resolver priority).

---

## 6. Occupancy Mode Priority

When multiple toggles are active simultaneously, the highest-priority mode wins.

| Priority | Mode | Handler called | Behavior |
|---|---|---|---|
| 1 (highest) | `guest` | `handle_occupancy_home()` | Comfort temps — guests always get full comfort |
| 2 | `vacation` | `handle_occupancy_vacation()` | Deep setback (`VACATION_SETBACK_EXTRA` beyond normal away) |
| 3 | `away` | `handle_occupancy_away()` | Normal setback |
| 4 (lowest) | `home` | `handle_occupancy_home()` | Comfort temps restored |

**Toggle resolution logic:**
1. Read home, vacation, and guest toggle entities (respecting any invert flags).
2. If **guest** toggle is on → mode = `guest`.
3. Else if **vacation** toggle is on → mode = `vacation`.
4. Else if **home** toggle is **off** → mode = `away`.
5. Else → mode = `home`.

### 6a. Occupancy-Aware Automation Guards (Issue #85)

The automation engine tracks `_occupancy_mode` internally (synced by the coordinator). All temperature-setting code paths check occupancy before applying comfort temps:

| Code Path | Home/Guest | Away | Vacation |
|---|---|---|---|
| `apply_classification()` (30-min cycle) | Apply comfort temps | Reapply away setback | Skip entirely |
| `handle_morning_wakeup()` | Restore comfort | Skip (no wakeup) | Skip (no wakeup) |
| `handle_bedtime()` | Apply bedtime setback | **Skip** (away setback maintained by 30-min `apply_classification()` cycle) | Skip (vacation setback preserved) |
| `_set_temperature_for_mode()` (safety net) | Apply comfort | Redirect → `handle_occupancy_away()` | Redirect → `handle_occupancy_vacation()` |

The `_set_temperature_for_mode()` safety net catches all indirect callers (door/window resume, grace expiry, economizer deactivation) so comfort temps are never applied while away/vacation.

**Paused-by-door guard (Fix #339):** When `_paused_by_door=True`, `handle_occupancy_away()` and `handle_occupancy_vacation()` record `_occupancy_mode` but skip the setback band call and return early. HVAC stays off. The setback is applied when sensors close and the resume path runs `_set_temperature_for_mode()`, which the safety net above redirects to the appropriate occupancy handler. Event emitted: `occupancy_setback_suppressed_paused` with payload `{occupancy: "away"|"vacation", reason: "paused_by_door"}`.

**`handle_bedtime()` skip paths — HVAC mode off (mild/warm nights):** When the current day classification has `hvac_mode = "off"` (mild or warm day, no heating/cooling required), `handle_bedtime()` logs a skip and emits a `bedtime_setback_skipped` event. No setpoint change is made — the comfort floor for the following morning is protected by the 30-min `apply_classification()` guard in §6b rather than a bedtime setpoint.

**Structured skip events (Issue #151):** All skip paths emit `bedtime_setback_skipped` to the event log with a `reason` field:

| `reason` value | Trigger condition |
|---|---|
| `"occupancy"` | `_occupancy_mode` is `away` or `vacation` at bedtime |
| `"manual_override"` | `_manual_override_active` is set (Issue #204) — bedtime setback is skipped to respect the user's revealed preference rather than fighting their manual adjustment |
| `"hvac_off"` | Classification `hvac_mode` is not `heat` or `cool` (mild/warm night) |
| `"no_classification"` | No current classification available at bedtime time |

Fire paths emit `bedtime_setback` with `{mode, target_f, depth_f, adaptive, modifier}`. Both event types are visible in the AI investigator's structured event log.

**Occupancy and wakeup events (Issue #240):** The following events are emitted by occupancy handlers when a setpoint change is actually applied, making these actions visible in the dashboard timeline and AI activity report:

| Event type | Handler | Condition | Payload |
|---|---|---|---|
| `occupancy_setback` | `handle_occupancy_away()` | Cool or heat thermostat mode — setpoint applied | `{mode: "cool"\|"heat", target_f: float, occupancy: "away"}` |
| `occupancy_setback` | `handle_occupancy_vacation()` | Cool or heat thermostat mode — setpoint applied | `{mode: "cool"\|"heat", target_f: float, occupancy: "vacation"}` |
| `occupancy_comfort_restored` | `handle_occupancy_home()` | Classification `hvac_mode` is `heat` or `cool` | `{mode: "cool"\|"heat", target_f: float}` (comfort setpoint) |
| `morning_wakeup` | `handle_morning_wakeup()` | Classification `hvac_mode` is `heat` or `cool` | `{mode: "cool"\|"heat", target_f: float}` (comfort setpoint) |

No event is emitted when HVAC is `off` (mild/warm day) — no setpoint change occurs in those cases. All four event types are categorised as `source_label=automation` by `_event_source_label()` in `ai_skills_context.py`. The skip path (HVAC off, occupancy away at wakeup) continues to emit `morning_wakeup_skipped` as before.

**DailyRecord setback fields (Issue #151):** `handle_bedtime()` writes the following fields to `DailyRecord` on every bedtime pass — fire or skip:

| Field | Type | Set when | Value |
|---|---|---|---|
| `setback_heat_applied_f` | `float \| None` | Fire path, heat mode | Applied heat setback setpoint (°F) |
| `setback_cool_applied_f` | `float \| None` | Fire path, cool mode | Applied cool setback setpoint (°F) |
| `setback_depth_f` | `float \| None` | Fire path | Depth of setback from comfort setpoint (°F) |
| `setback_was_adaptive` | `bool \| None` | Fire path | `True` when thermal model drove the depth; `False` for default |
| `setback_skipped_reason` | `str \| None` | Skip path | One of `"occupancy"`, `"manual_override"`, `"hvac_off"`, `"no_classification"` |

All five fields default to `None` at record creation. On a fire night, `setback_skipped_reason` stays `None`; on a skip night, all applied-value fields stay `None`. Accessible via `learning_db.py --daily` (see §Diagnostic Tools).

**Test coverage:** `tests/test_occupancy_automation.py` — 18 tests covering all cells above; `tests/test_bedtime_setback.py` — full fire/skip/field coverage.

---

> **DailyRecord Persistence Invariant (Issue #176)**
>
> `DailyRecord` counters accumulate throughout the day and are persisted to
> `climate_advisor_state.json`. When `_async_send_briefing()` creates an updated record
> after classification (e.g., after HA restart), it **MUST preserve all already-accumulated
> counters** from the existing same-day record before replacing it.
>
> Fields that must be preserved:
> `hvac_runtime_minutes`, `comfort_violations_minutes`, `manual_overrides`,
> `thermal_session_count`, `occupancy_away_minutes`, `windows_opened`,
> `window_open_actual_time`, `override_details`.
>
> **Violation:** creating a fresh `DailyRecord(...)` unconditionally resets all counters to
> zero, causing `hvac_runtime_today` to show `0.0` after a mid-day HA restart.
>
> **Fix pattern:** before constructing the new record, check whether `self._today_record`
> already exists for today's date, and carry forward all accumulated counter fields into
> the new `DailyRecord(...)` constructor call. Additionally, `_async_save_state()` must be
> called on every HVAC on→off transition (after `_flush_hvac_runtime()`) so that state is
> never more than one HVAC cycle stale at restart time.
>
> **Test coverage:** `tests/test_daily_record_accuracy.py` —
> `test_daily_record_survives_briefing_after_restart`

---

### 6b. Warm-Day Comfort-Floor Guard _(passive safety backstop — Issue #249)_

> **Issue #249 role change:** This guard is no longer the primary defense against the home falling below the comfort floor on warm/hot days. The comfort-band model (§6e) arms the thermostat with an explicit heat floor (`setback_heat` or `comfort_heat` depending on context) as part of every scheduled state update — the thermostat will heat the home back up without CA polling. §6b remains as a lightweight always-on safety net that fires if the band is somehow not in place or the floor is transiently breached during a transition.

When `apply_classification()` runs and the day type is `warm` or `hot` and the indoor temperature is below `comfort_heat`, the automation engine applies a comfort-floor guard to prevent the home from sitting below the comfort floor.

| Condition | Action | Event emitted |
|---|---|---|
| `day_type in (warm, hot)` AND `indoor_temp < comfort_heat` | Set HVAC to `heat`, target = `comfort_heat` (backstop) | `warm_day_comfort_gap` |
| `day_type in (warm, hot)` AND `indoor_temp >= comfort_heat` | Apply comfort band normally (§6e) | — |
| `day_type in (warm, hot)` AND indoor temp unavailable | Apply comfort band normally (fail-open) | — |

**Why this guard still exists (as backstop):** Even with the band armed, a mid-cycle transition (HA restart, manual mode change, thermostat reconnect) can briefly leave the home below the comfort floor before the next 30-minute cycle re-arms the band. §6b catches that window and fires a `warm_day_comfort_gap` event so the situation is visible in the event log.

**Primary defense (Issue #249):** The comfort-band model in §6e arms the heat floor on every `apply_classification()` call — `comfort_heat` while the occupant is home + awake (any day type), or the setback floor when away/asleep. The thermostat holds that floor autonomously between 30-minute cycles — no supervisor polling needed for normal operation. §6b activates only when the band has lapsed.

**Interaction with occupancy guards:** The comfort-floor heat command goes through `_set_temperature_for_mode()`, so occupancy-away and vacation redirection (§6a) still applies.

**Event frequency — `warm_day_state_confirmed` / `warm_day_setback_applied`:** `warm_day_state_confirmed` fires on every 30-minute coordinator update cycle while the thermostat is already in the correct warm-day state — not once per day. Sixty or more firings in 48 hours is expected on a sustained warm day; this is a heartbeat, not a loop or a bug. `warm_day_setback_applied` fires only when an actual setpoint or mode change is made, which is infrequent.

**Event frequency — `incident_detected`:** Emitted at most once per 30-min cycle per incident class (deduplicated within each call to `_detect_and_emit_incidents()`). The proactive variant (`setpoint_mode_inconsistency`) may fire at command time inside `_set_temperature()` rather than post-cycle, once per inconsistent command issued. See [Incident Classes](incident-classes.md) for the full list of classes and their detection timing.

**Test coverage:** `tests/test_warm_day_comfort_gap.py`

### 6c. Warm-Day ODE Ceiling Guard (Issue #136) _(passive safety backstop — Issue #249)_

> **Issue #249 role change:** This guard is no longer the primary defense against the home exceeding `comfort_cool` on warm/mild days. The comfort-band model (§6e) arms the thermostat with an explicit cool ceiling (`comfort_cool`) as part of every scheduled state update — the thermostat will cool the home back down without CA polling the ODE. §6c remains as a lightweight always-on safety net. In normal operation the ODE curve, built against the armed setpoint, predicts no breach — so the guard is naturally dormant. It activates only when the band has lapsed (HA restart, manual override, thermostat reconnect) or when outdoor conditions change sharply mid-cycle before the next 30-minute re-arm.

When the day classification is `warm` or `mild` and the thermal model has a calibrated `k_passive`, the automation engine evaluates a **ceiling guard** on every 30-minute coordinator cycle. The guard fires proactively to prevent indoor temperature from breaching `comfort_cool` in situations where the comfort band is not currently holding.

#### Purpose

The guard closes the "read-render split" gap: `_build_predicted_indoor_future()` feeds the chart every 30 min with an accurate indoor forecast, but prior to Issue #136 that forecast was never routed into `apply_classification()`. The ceiling guard routes it: if the ODE curve predicts a `comfort_cool` breach and free cooling cannot keep up, the guard sets HVAC to `cool` at `comfort_cool` before (or as soon as) the breach occurs.

With the comfort band armed (Issue #249), the ODE curve is constructed against the armed ceiling setpoint and therefore predicts no breach under normal conditions — the guard is dormant. It becomes active again if the band lapses for any reason.

#### Dormancy: when the guard defers to free cooling (3-condition — Issue #247)

The guard goes **dormant** (defers to natural ventilation) only when **all three** of these hold:

1. `outdoor <= indoor` — outdoor air can in principle cool the home, **and**
2. `self._natural_vent_active` — windows are actually open and nat-vent is running (not merely *eligible*), **and**
3. `indoor <= ceiling threshold` — indoor is still at/under the ceiling, so free cooling is keeping up.

If any condition fails, the guard **evaluates** (and fires if the breach scan confirms a breach):

- **indoor already exceeds the ceiling** — the #247 reactive case: solar/internal gains are out-pacing the breeze, so the guard escalates to AC **even though `outdoor < indoor`**. Free cooling stays the first remediation; AC fires only when ventilation is demonstrably losing.
- **nat-vent is NOT running** (windows closed, fan override) — the #215 case: do not defer to a ventilation that is not happening.
- **outdoor has risen above indoor** — the original #136/#218 path (airflow would add heat).

> **Regression note:** Issue #218 specified this 3-condition dormancy *plus* the escalation-on-fire that clears nat-vent, but the committed fix (`676daa4`) landed only the escalation half. The dormancy stayed one-condition (`outdoor <= indoor`), so on a day where outdoor stayed below indoor the guard never woke and the escalation code was unreachable — the home sat above the ceiling for hours (re-filed as #247). The escalation-on-fire is now reachable because the dormancy correctly lifts.

**`aggressive_savings` widens the ceiling threshold.** In normal mode the ceiling threshold is `comfort_cool`. In `aggressive_savings` mode it is `comfort_cool + CEILING_ESCALATION_SAVINGS_MARGIN_F` (2.0°F) — savings homes tolerate a small overshoot before paying for the compressor, but are still rescued from a real comfort failure once indoor exceeds that wider threshold.

#### `_ceiling_threshold()` is archetype-aware (Issue #392 Fix 1)

The ceiling threshold used in the dormancy check (condition 3 above) is computed by `_ceiling_threshold(comfort_cool)` in `automation.py`, not inlined. The helper returns a different answer depending on the configured fan archetype:

```python
def _ceiling_threshold(self, comfort_cool: float | None) -> float | None:
    fan_mode = self.config.get(CONF_FAN_MODE, FAN_MODE_DISABLED)
    if fan_mode in (FAN_MODE_WHOLE_HOUSE, FAN_MODE_BOTH):
        return None
    if comfort_cool is None:
        return None
    aggressive = bool(self.config.get("aggressive_savings", False))
    return comfort_cool + CEILING_ESCALATION_SAVINGS_MARGIN_F if aggressive else comfort_cool
```

| fan_mode | Return value | Why |
|---|---|---|
| `FAN_MODE_HVAC` | `comfort_cool` (or `comfort_cool + CEILING_ESCALATION_SAVINGS_MARGIN_F` if `aggressive_savings`) | The HVAC blower and the compressor **coexist** — the comfort band stays armed the whole time nat-vent is active (§6e / Issue #249), so handing off to AC once indoor crosses the ceiling is safe and correct. Nothing fights: the thermostat itself decides whether the compressor needs to run. |
| `FAN_MODE_WHOLE_HOUSE` / `FAN_MODE_BOTH` | `None` | A whole-house fan (WHF) is **mutually exclusive** with the compressor by construction (`_activate_fan()` forces HVAC to `off` while a WHF session is active; see §9). A WHF is also physically guaranteed to keep converging toward outdoor temperature for as long as `outdoor < indoor` — the ceiling number says nothing about whether the WHF *will* succeed, only about how long it will take. So there is no ceiling-based handoff point for WHF: convergence is governed purely by outdoor/indoor direction, not by how far indoor has drifted above `comfort_cool`. |

The ODE ceiling guard's dormancy check (condition 3, above) treats `ceiling_threshold_val is None` as "ceiling condition satisfied" — i.e. for WHF, dormancy collapses to `outdoor <= indoor AND _natural_vent_active` (no ceiling term at all). For `FAN_MODE_HVAC`, dormancy still requires `indoor <= ceiling_threshold` exactly as before Issue #392.

**Why this had to change (Root Cause of Issue #392):** before this fix, the guard applied the same ceiling-based dormancy rule to both archetypes. For `FAN_MODE_WHOLE_HOUSE`, this meant that once indoor ticked one degree past `comfort_cool`, the guard would escalate to `cool` — which deactivates the WHF and forces HVAC to `cool` (per the mutual-exclusion contract) — even though outdoor was still comfortably below indoor and the WHF would have converged on its own. The very next reactivation check (any of the four gate sites in §17) would then see `outdoor < indoor` still holds and turn the WHF back on, which forces HVAC back to `off`, undoing the guard's `cool` command. That produced the `off→cool→off→cool` oscillation reported in #392 (repeating roughly every 5 minutes between 18:53 and 18:58). Making `_ceiling_threshold()` archetype-aware removes the false ceiling trigger for WHF entirely — see §17 for the matching change to the four reactivation gate sites, and "Structural WHF/AC Mutual Exclusion" below (§9, Issue #392 Fix 1b) for the structural guard that also closes a related but separate gap (mutual exclusion not being enforced everywhere HVAC mode is written).

**Test coverage (Issue #392):** `tests/test_nat_vent_activation.py`, `tests/test_fan_control.py`, `tests/test_whole_house_fan_hvac_suppression.py` — exact function names pending as of this doc pass; see those files directly for current coverage of archetype-aware ceiling behavior.

**On escalation the guard clears nat-vent** (Issue #218 part 2): if `_natural_vent_active` is true when the guard fires, it deactivates the fan, sets `_natural_vent_active = False`, and emits `nat_vent_ceiling_escalation` before switching to `cool` — so free cooling does not fight the compressor.

#### Guard conditions

| Condition | Action |
|---|---|
| `k_passive is None` OR `k_passive >= 0` | Skip — no calibrated passive model |
| `confidence_k_passive == "none"` AND NOT bridge home | Skip — model not yet trustworthy |
| Occupancy away or vacation | Skip — handled by upstream occupancy guards (§6a) |
| `predicted_indoor` is empty or None | Skip — no ODE curve available (fresh install, no physics gate) |
| Outdoor temp unavailable or missing | Skip |
| `outdoor <= indoor` **AND** `_natural_vent_active` **AND** `indoor <= ceiling threshold` | Dormant — free cooling is actually viable; guard defers to nat-vent (see 3-condition dormancy below) |
| `_find_ceiling_breach_time()` returns None | Dormant — no breach predicted above threshold |
| Bridge home (`k_passive_via_bridge=True`) | Apply `+CEILING_BRIDGE_TOLERANCE_F (1.0°F)` tolerance; guard fires at `comfort_cool + 1.0°F` |
| `k_active_cool` not learned (None) | Guard fires with `CEILING_PRECOOL_FALLBACK_MIN = 120` min lead time |
| All conditions met | Evaluate lead time; fire if breach is within window |

#### `_find_ceiling_breach_time()` — module-level helper in `coordinator.py`

Scans `predicted_indoor` (a list of `{"ts": ISO-string, "temp": float}` dicts from the ODE curve) for the first entry where `temp > comfort_cool + tolerance`. Returns the `datetime` of that entry, or `None` if no entry exceeds the threshold or the curve is empty.

```
signature: _find_ceiling_breach_time(predicted_indoor, comfort_cool, tolerance=0.0) → datetime | None
```

The guard inlines this scan inside `automation.py`'s `apply_classification()` to avoid a circular import between `automation.py` and `coordinator.py`. The standalone function in `coordinator.py` is used by `tests/test_prediction.py` and the morning briefing path.

#### Lead time formula

When the breach timestamp is found, the guard computes how far in advance to start cooling:

```
if k_active_cool is not None and abs(k_active_cool) > 0:
    lead_time_min = ((comfort_cool − current_indoor) / abs(k_active_cool)) × 60 × 1.3
else:
    lead_time_min = CEILING_PRECOOL_FALLBACK_MIN  # 120 min

lead_time_min = clamp(30, 240)
```

The `1.3×` safety margin ensures cooling begins early enough even on hotter-than-modeled days. The clamp floor (30 min) prevents firing immediately on a trivially small delta; the clamp ceiling (240 min) prevents over-committing 4+ hours in advance.

**`k_active_cool = None` is the normal case** for any home in its first cooling season (including non-bridge homes that have never recorded a cooling cycle). The 120-minute fallback is the common path, not an edge case.

#### Fire condition

```
if hours_to_breach <= lead_time_min / 60:
    → set HVAC to "cool", target = comfort_cool
    → emit "ceiling_guard_fired" event
```

HVAC is set to `cool` at `comfort_cool` (not below — this is ceiling defense, not pre-cooling below comfort). The target deliberately avoids the `-2°F` offset used for hot-day pre-conditioning (§4).

#### Weather-change resilience (stateless design)

The guard is fully stateless — no `_ceiling_precool_scheduled` flag. On each 30-min cycle, `apply_classification()` recomputes the ODE curve from fresh forecast data and re-scans for breach. Consequences:

- **Forecast improves** (cold front arrives, outdoor temperature drops): `_find_ceiling_breach_time()` returns `None` → guard goes dormant automatically on the next cycle, no cancellation logic needed.
- **Forecast worsens** (heat dome arrives): breach crosses into the lead time window → guard fires on the cycle when it first qualifies.
- **HVAC already cooling** (guard fired on a prior cycle): warm-day classification (`hvac_mode="off"`) will naturally stop cooling on the next cycle once indoor drops below `comfort_cool`, because the comfort-floor guard (§6b) will not re-heat at that point.

#### Bridge home behavior

Bridge homes use `k_vent_window` as a proxy for `k_passive`. The `k_passive_via_bridge=True` flag causes the guard to apply `CEILING_BRIDGE_TOLERANCE_F = 1.0°F` tolerance, requiring the predicted curve to exceed `comfort_cool + 1.0°F` before the breach is recorded. This accounts for the proxy being measured under ventilated conditions, which is less accurate for the closed-window heat-approach phase.

#### Constants

| Constant | Value | Purpose |
|---|---|---|
| `CEILING_PRECOOL_FALLBACK_MIN` | `120` | Lead time (minutes) when `k_active_cool` is not learned |
| `CEILING_BRIDGE_TOLERANCE_F` | `1.0` | Extra °F threshold for bridge homes |
| `CEILING_ESCALATION_SAVINGS_MARGIN_F` | `2.0` | Overshoot tolerated above `comfort_cool` before escalating in `aggressive_savings` mode (Issue #247) |

All three are defined in `const.py`.

#### Interaction with §6b comfort-floor guard

The ceiling guard runs **after** the comfort-floor guard in `apply_classification()`. The comfort-floor guard runs inside the `hvac_mode == "off"` branch; the ceiling guard is a separate block also gated by `classification.hvac_mode == "off"`, so it evaluates regardless of whether the floor guard fired.

In practice the two guards do not conflict: if indoor is below `comfort_heat` (floor guard fires), indoor is well under `comfort_cool`, so `_find_ceiling_breach_time()` finds no breach above the ceiling and the ceiling guard is dormant via that row (regardless of the 3-condition dormancy). A home simultaneously below the comfort floor and predicted to breach the ceiling is a degenerate condition that resolves naturally — the floor guard heats, the next cycle re-evaluates both guards with updated temperatures.

#### Emitted event

`ceiling_guard_fired` — payload: `{breach_time: ISO, hours_to_breach: float, lead_time_min: int}`. Visible in the Daily Record's event list. Used by the morning briefing to determine pre-cool start time for the warm-day narrative (§Part 2 of the plan).

**Test coverage:** `tests/test_warm_day_comfort_gap.py` — `TestCeilingDefenseActive`, `TestCeilingPreCoolFallback`, `TestCeilingWeatherChange`, `TestCeilingBridgeTolerance`, `TestCeilingDefenseManualOverride`. `tests/test_prediction.py` — `TestFindCeilingBreachTime`.

---

### 6d. MILD Day Dynamic Window Close Time (Fix C, Issue #147)

Prior to Issue #147, MILD day window scheduling used hardcoded `time(10, 0)` (open) and `time(17, 0)` (close) in `classifier.py`. These values were magic literals that could not be overridden by the thermal model, even on days when the ODE could predict the actual indoor–outdoor crossover time.

#### Before Fix C

```python
# classifier.py (pre-v0.3.46) — lines 118–119
self.window_open_time = time(10, 0)  # always 10am
self.window_close_time = time(17, 0)  # always 5pm
```

These literals were correct as a starting guess but systematically incorrect for any home whose indoor–outdoor crossover does not fall at 5pm.

#### After Fix C

**Constants moved to `const.py`:**

```python
MILD_WINDOW_OPEN_HOUR = 10  # MILD-day window open fallback (was hardcoded in classifier.py)
MILD_WINDOW_CLOSE_HOUR = 17  # MILD-day window close fallback
```

**`classifier.py` now uses the constants:**

```python
self.window_open_time = time(MILD_WINDOW_OPEN_HOUR, 0)
self.window_close_time = time(MILD_WINDOW_CLOSE_HOUR, 0)
```

**`briefing.py` applies ODE timing when available:**

**Correction (Issue #534, 2026-07-28):** this section previously described a shared helper,
`_derive_natural_vent_events(predicted_indoor_future, predicted_outdoor_future, comfort_cool,
k_active_cool)`, as wired into the MILD day briefing path. That was aspirational, not actual —
the function existed and was unit-tested but had zero production call sites, and `_mild_day_plan()`
used the static `c.window_close_time` unconditionally. It has now been wired up, but via
`_derive_warm_day_events()` directly (the same function warm days use), not
`_derive_natural_vent_events()` — that helper's documented input shape (`list[float]`,
hour-indexed) does not match what `_build_predicted_indoor_future()` actually produces
(`list[{"ts", "temp"}]`, the same shape warm-day curves use), so it remained unused/dead code
until **Issue #535** removed it outright (see below). `generate_briefing()` computes
`mild_events` for MILD days the same way it already computes `warm_events` for warm days
(identical call shape), and passes it to both `_generate_tldr_table()` (header row) and
`_mild_day_plan()` (conversational body).

**Comfort-floor hardening (Issue #535, 2026-08-30):** `nat_vent_cutoff` previously came from a
bare `outdoor >= indoor - 1°F` scan (`_nat_vent_cutoff_reached()`) — only half of the real
activation gate's predicate. The live gate, `decide_nat_vent_gate()` in `nat_vent_gate.py`
(Issues #411/#417), requires **four** conditions, including `indoor > comfort_heat`.
`_derive_warm_day_events()` now takes optional `comfort_heat_raw`/`sleep_heat`/
`in_sleep_window_fn` params; when supplied, it also scans `predicted_indoor` for the first
timestamp where indoor drops to `resolve_comfort_heat(comfort_heat_raw, sleep_heat,
in_sleep_window_fn(ts))` (the same sleep-aware floor resolver `decide_nat_vent_gate()` uses,
extracted to a standalone function in `nat_vent_gate.py`). `nat_vent_cutoff` becomes whichever
of the outdoor-crossing or floor-crossing timestamps is earlier, and a new
`nat_vent_cutoff_reason` field (`"outdoor_rise"` / `"comfort_floor"`) records which one won —
`_warm_day_plan()` uses it to pick between two close-time sentences ("...after that the outdoor
air will be warmer than inside" vs. "...to hold the heat in"). `generate_briefing()` resolves
the three optional params from its `runtime_config` argument for both `warm_events` and
`mild_events`; when `runtime_config` is omitted (direct test calls), the floor scan is skipped
and behavior is identical to before #535.

**UPDATE — Issue #847 (2026-09-04):** the "no confirmed production incident" caveat below is
now historical, not current status. #535's own note that this branch shipped as *preemptive
hardening with no live-outdoor sanity check* turned out to predict exactly the drift class that
#847 found in production: a WARM-day briefing froze a `comfort_floor` reason at generation time
("hold the heat in," 8:00 AM) while the Next Automation card recomputed live every cycle and
had already moved to `outdoor_rise` ("outdoor will stop helping," 11:00 AM) — two contradictory
framings of one fact, on the same dashboard. #847's five-whys chain traced this to the same
recurring duplication-drift pattern documented in project memory
(`project_natvent_duplicate_threshold_logic`): the *time* half of this value had already been
unified onto one shared `self._nat_vent_plan` object (#814/#817/#818), but the *reason→phrase*
mapping was still two independently-written inline branches, and the briefing's staleness check
(`_maybe_regenerate_briefing_for_drift()`) never watched `nat_vent_cutoff`/`nat_vent_cutoff_reason`
for drift at all.

The fix, landed in #847:
- **One shared phrase helper**, `nat_vent_plan.describe_nat_vent_cutoff_reason(reason: str |
  None) -> str` (next to `compute_nat_vent_plan()`), is now the single source of truth for
  reason→sentence wording. It returns a phrase *fragment*, not a full sentence — each caller
  interpolates it into its own sentence shape: `"comfort_floor"` → `"to hold the heat in"`;
  `"outdoor_rise"` or `None` → `"before outdoor air warms past indoor"`. `_warm_day_plan()` and
  `_mild_day_plan()` (which previously had no reason branch at all — always said "to trap the
  warmth" regardless of the underlying cutoff reason) interpolate this same fragment instead of
  maintaining independent branches or wording. **`_compute_next_automation_action()` was a third
  call site until Issue #849 removed it**: the candidate it fed (`nat_vent_cutoff` → "Close
  windows...") told the occupant to physically operate windows, an action CA has no actuator for
  — a distinct ontology violation from the phrasing-drift bug this section fixes, not a reason to
  reintroduce the branch. The shared helper's two remaining call sites (`_warm_day_plan()`,
  `_mild_day_plan()`) are unaffected.
- **A live sanity check** on the `comfort_floor` reason reuses the **existing**
  `free_cooling_direction_ok(outdoor_temp, indoor_temp)` in `temperature.py` — the same #428
  guard already used by the `next_human_action` sensor and the economizer gate — no new
  predicate was written. Before the helper's call sites emit the `comfort_floor` fragment, they
  confirm the comfort-floor risk still holds against current live readings; if it no longer
  does, the phrase falls back to the `outdoor_rise` fragment instead of asserting a stale claim.
  Each override site logs a WARNING with the indoor/outdoor temps and the original→displayed
  reason. This is the check #430 asked for and never got — #847 closes #430 as its direct fix.
- **A third staleness trigger** in `_maybe_regenerate_briefing_for_drift()`, alongside the
  existing `day_type` and `today_high` triggers: new frozen-comparison state
  `self._briefing_nat_vent_cutoff` (datetime|None) / `self._briefing_nat_vent_cutoff_reason`
  (str|None) — mirroring the existing `_briefing_today_high` pattern exactly, persisted as
  `briefing_state.briefing_nat_vent_cutoff` / `briefing_state.briefing_nat_vent_cutoff_reason`
  in the state JSON — triggers regeneration when the live `nat_vent_cutoff` drifts by more than
  `const.BRIEFING_NAT_VENT_CUTOFF_DRIFT_THRESHOLD_MINUTES = 45.0` (half the ~60-min
  forecast-hour step size, well below the 3-hour drift the reported incident showed) or when
  `nat_vent_cutoff_reason` flips, so a frozen briefing can no longer silently fall behind the
  live card the way it did in the reported incident.
- **Issue #788 (reopened) — reopen/recovery sentence was a computation bug, not a wording
  gap:** an earlier round on this issue (see history below) treated the contradiction as a
  phrasing mismatch and patched `briefing.py`'s reopen sentence to say "once outdoor air cools
  back below indoor" for a `comfort_floor` cutoff instead of "this evening." That shipped a
  self-contradictory briefing anyway ("Close up at 8:00 AM to hold the heat in" ... "Reopen
  windows around 9:00 AM once outdoor air cools back below indoor") because the deeper bug was
  never in the wording — it was in `compute_nat_vent_plan()` (`nat_vent_plan.py`) computing
  `recovery_time`/`nat_vent_recovers` the same way for both cutoff reasons. A `comfort_floor`
  cutoff fires only in the branch where the `outdoor_rise` crossing did **not** win the race
  (`elif floor_crossing is not None`) — meaning outdoor is *already* below indoor at cutoff
  time. Scanning forward for "outdoor drops back below indoor" from that starting point finds a
  false "recovery" minutes to ~1 hour later, not because anything changed, but because the
  condition was never really unmet. No wording fix can repair this: reopening windows shortly
  after closing them specifically to protect indoor temperature from dropping further would
  undo the very reason they were closed.

  **Fix (this round):** `compute_nat_vent_plan()` now gates the `recovery_time` scan on
  `result["nat_vent_cutoff_reason"] == "outdoor_rise"` — `recovery_time`/`nat_vent_recovers`
  stay at their `None`/`False` defaults for `comfort_floor` cutoffs. `briefing.py`'s
  `_warm_day_plan()` needed no branch of its own once this landed: `_nat_vent_recovers` is
  simply `False` for `comfort_floor` cutoffs, so the reopen sentence is omitted entirely rather
  than reworded. The dead `_nat_vent_cutoff_reason == "comfort_floor"` phrasing branch this
  section previously described was removed as part of the same fix — see `nat_vent_plan.py`'s
  `compute_nat_vent_plan()` docstring and the comment above its `recovery_time` block for the
  authoritative statement of this restriction.

  **Known gap, intentionally not addressed here:** there is no `comfort_floor`-specific reopen
  signal in the codebase today (e.g., "reopen once indoor rises N° above the comfort floor") —
  reopening was never a modeled part of that gate's off-ramp. Suppressing the invalid
  `outdoor_rise`-shaped signal is the correct scope for this bug fix; designing a new,
  indoor-floor-based reopen signal for `comfort_floor` days is a separate feature/design
  decision, not attempted here.

*(Superseded text retained below for historical context — do not treat "no confirmed production
incident" as current status; see the Issue #847 update above.)*

No confirmed production incident motivated this fix —
see project memory `feedback_verify_before_confirmed_bug` — it closes a latent gap the
recurring nat-vent duplication-drift pattern (`project_natvent_duplicate_threshold_logic`)
predicted would eventually reappear in a forecast-curve consumer.

> **DOC RULE (Issue #847; scope corrected by #849):** any new `nat_vent_plan` field that is
> rendered as user-facing text in more than one place MUST go through
> `describe_nat_vent_cutoff_reason()` (or its successor, if the mapping's scope grows beyond
> cutoff reasons) — do not add a second inline `if nat_vent_cutoff_reason == "..."` branch
> anywhere else. This is the guardrail the #847 five-whys found missing after three prior rounds
> of fixes on this exact pair of briefing call sites (#428/#430, #518/#528,
> #535/#788/#814/#817/#818) — each prior fix solved its own reported symptom without leaving
> behind an enforcement mechanism for the next field. If you are adding a new field to
> `nat_vent_plan` and it needs user-facing phrasing in two places, extend the shared helper;
> if you are tempted to write a second branch "just for this one case," that temptation is
> exactly how this bug recurred four times. **The "Next Automation card" was originally listed
> as a consumer alongside the briefing body; Issue #849 removed the card's cutoff-reason
> candidate entirely** (an occupant-action ontology violation, not a phrasing bug this rule
> would have caught), so the phrasing-symmetry guarantee this rule provides now applies only
> within briefing.py's own two call sites (`_warm_day_plan()`, `_mild_day_plan()`) — a future
> third consumer would still need to route through this same helper, but the card is not one.

When a predicted indoor/outdoor forecast curve is available (thermal model calibrated):
- MILD day window close time = `nat_vent_cutoff` (earlier of: outdoor temp ≥ indoor − 1°F, or
  indoor ≤ the sleep-aware comfort floor — Issue #535)
- Fallback when no forecast curve is available = `time(MILD_WINDOW_CLOSE_HOUR, 0)` (5pm), unchanged
  from before this fix

#### Impact Cascade from Solar Phase Offset Correction

The following cascade applies to both warm and MILD days when `solar_phase_offset_h` is correctly learned:

1. `solar_phase_offset_h` corrects `_solar_factor` → ODE models solar input peaking at 3–5pm instead of 1pm
2. ODE predicts indoor rise more slowly through the morning (less solar input before 3pm)
3. `nat_vent_cutoff` (the hour when outdoor ≥ indoor − 1°F) shifts **~1–2 hours later** → windows stay open longer, more free cooling is captured
4. `ceiling_breach_time` (the hour when indoor > `comfort_cool`) also shifts later → AC starts later
5. `precool_start_time` shifts with it → no wasted early AC run while natural ventilation still has capacity
6. **Net effect:** extended natural ventilation window, reduced AC runtime, improved energy efficiency

#### Decision Table

| Condition | MILD day open time | MILD day close time | Source |
|---|---|---|---|
| ODE unavailable (fresh install, no physics gate) | `time(MILD_WINDOW_OPEN_HOUR, 0)` | `time(MILD_WINDOW_CLOSE_HOUR, 0)` | `const.py` constants |
| ODE available, `nat_vent_cutoff` computable | `time(MILD_WINDOW_OPEN_HOUR, 0)` | `nat_vent_cutoff` (dynamic, ~12–17 depending on solar offset) | ODE curve |
| ODE available, `nat_vent_cutoff` returns None (outdoor always > indoor) | `time(MILD_WINDOW_OPEN_HOUR, 0)` | `time(MILD_WINDOW_CLOSE_HOUR, 0)` | Fallback |

The open time is always `MILD_WINDOW_OPEN_HOUR` (10am). Only the close time is dynamic.

#### Constants

| Constant | Value | File | Notes |
|---|---|---|---|
| `MILD_WINDOW_OPEN_HOUR` | `10` | `const.py` | Was hardcoded literal in `classifier.py:118` |
| `MILD_WINDOW_CLOSE_HOUR` | `17` | `const.py` | Was hardcoded literal in `classifier.py:119` |

**Test coverage:** `tests/test_solar_phase.py` — `TestMildDayDynamicScheduling`:
- `test_mild_day_uses_const_fallback_when_no_ode`
- `test_mild_day_close_time_uses_ode_crossover`
- `test_mild_day_constants_in_const_py`

---

### 6e. Comfort-Band Programming (Issue #249)

The home is held inside the comfort band continuously by the thermostat itself — recurring afternoon ceiling drift (Issues #136/#218/#247) becomes structurally impossible because the ceiling setpoint is always armed, not re-armed reactively 30 minutes later.

> **`ComfortBand.active` is unchanged by Issue #827.** `select_comfort_band()`'s day-type-only `active` edge picker below is still the sole authority for `ComfortBand.floor`/`ComfortBand.ceiling`/`ComfortBand.active`. [§6f](#6f-comfort-family-defense-fsm-issue-827-consolidates-821823)'s comfort-family FSM layers a breach-driven escalation *on top of* the edge this section picks — it never changes which edge is `active` or what `floor`/`ceiling` resolve to, only (rarely) which HVAC mode actually gets commanded this cycle.

#### The One-Decision / One-Actuation Model

Every scheduled state handler (classification apply, bedtime, morning wakeup, occupancy change) does two things and only two things:

1. **Decide the band** — call `select_comfort_band(...)` to produce a `ComfortBand(floor, ceiling, active, reason)`.
2. **Actuate the band** — call `_apply_comfort_band(band)` to emit the right command shape for the thermostat's capabilities.

There is no `off` sentinel, no off+setback divergence, and no per-handler HVAC-mode branching. The thermostat's own deadband holds the home inside `[floor, ceiling]` between 30-minute cycles; CA's role is to keep the band programmed, not to supervise the thermostat every cycle.

#### `select_comfort_band` — Band-Edge Rules

`select_comfort_band(classification, config, *, occupancy_mode, in_sleep_window, aggressive_savings) → ComfortBand`

`ComfortBand(floor, ceiling, active, reason)` where `active ∈ {"ceiling", "floor"}`.

**Occupied + awake = the full comfort band.** While the occupant is home/guest and awake, the band is `[comfort_heat, comfort_cool]` on **any** day type — the "lazy posture" the thermostat runs itself with: it pre-heats the cold morning up to `comfort_heat` and cools the warm afternoon down to `comfort_cool`. Both edges are held at comfort; suppression to a setback edge happens **only** when away or asleep. The **`active`** field (`"ceiling"` on warm/hot/mild days, `"floor"` on cool/cold days) does **not** change the band for a dual thermostat — it only tells `_apply_comfort_band` which single edge a single-mode device should defend.

| Context | floor | ceiling | active | Notes |
|---|---|---|---|---|
| Home/guest — any day type (awake) | `comfort_heat` | `comfort_cool` | `"floor"` if heat day else `"ceiling"` | Full comfort band; thermostat pre-heats the morning and cools the afternoon |
| Home/guest — `aggressive_savings=True` | `comfort_heat − CEILING_ESCALATION_SAVINGS_MARGIN_F` | `comfort_cool + CEILING_ESCALATION_SAVINGS_MARGIN_F` | as above | BOTH edges widened so the system runs less |
| Home/guest — `hot` day | `comfort_heat` | `comfort_cool` | `"ceiling"` | No pre-cool offset — the daytime `hot` day ceiling-lowering mechanism was removed in Issue #558; hot-day thermal-mass banking happens exclusively via the overnight pre-cool phase (§5a-i), not this band |
| Sleep window (any day type) | `sleep_heat` | `sleep_cool` | `"floor"` (cool/cold) or `"ceiling"` (warm/hot) | Configured `sleep_heat`/`sleep_cool` band |
| Away occupancy | `setback_heat` | `setback_cool` | `"ceiling"` | Setback band — suppression only applies when nobody is home |
| Vacation occupancy | `setback_heat − VACATION_SETBACK_EXTRA` | `setback_cool + VACATION_SETBACK_EXTRA` | `"ceiling"` | Deep-setback band |

**`aggressive_savings` edge widening:** widens **both** comfort edges by `CEILING_ESCALATION_SAVINGS_MARGIN_F` (2.0°F) — `floor − margin`, `ceiling + margin` — so the system tolerates a wider band before heating or cooling. Setback and sleep bands are unaffected.

**Single-mode devices:** a cool-only thermostat defends the ceiling (it has no heat to give); a heat-only thermostat defends the floor. For these, `active` selects which comfort edge is armed; the other edge is simply not this device's job. A dual (`heat_cool`) thermostat holds both edges at comfort with one command.

#### `_apply_comfort_band` — Command Shapes

`_apply_comfort_band(band)` reads `self._get_thermostat_capabilities()` and emits exactly one service call (or none if the device cannot serve the active edge):

| Thermostat capability | Command shape |
|---|---|
| Dual (`heat_cool`) capable | `_set_hvac_mode("heat_cool")` (if mode changed) + `_set_temperature_dual(band.floor, band.ceiling)` — both edges sent every call; the unchanged side is reiterated automatically |
| Cool-capable, `active = "ceiling"` | `_set_hvac_mode("cool")` (if mode changed) + `_set_temperature(band.ceiling)` |
| Heat-capable, `active = "floor"` | `_set_hvac_mode("heat")` (if mode changed) + `_set_temperature(band.floor)` |
| Device cannot serve the active edge (e.g. heat-only thermostat on a warm day) | No-op — skip this cycle (defensive; not a fallback path) |

Mode changes are issued only when the thermostat is not already in the target mode — the existing idempotent `_set_hvac_mode` setter (line ~1258) enforces this. Dry-run mode is respected throughout.

**Emitted event:** `comfort_band_applied` — payload: `{floor, ceiling, active, mode, reason}`. Every call to `_apply_comfort_band` that results in a service call emits this event. Visible in the Daily Record's event list and the AI activity report.

**Bedtime / occupancy payloads updated:** `bedtime_setback`, `morning_wakeup`, `occupancy_setback` event payloads now also carry `floor/ceiling/active/mode` so the timeline shows the full band context, not just a single setpoint.

#### Nat-Vent and Economizer with the Band Armed

Natural ventilation and the economizer **no longer set `hvac_mode=off`** when they activate (Issue #249 Design §4). They manage only the fan; the comfort band remains armed throughout:

- **Nat-vent active (windows open, outdoor cooler than indoor):** fan on, `_natural_vent_active = True`, band re-armed via `_apply_nat_vent_hvac_state()` (see below). The thermostat self-arbitrates: if the breeze keeps the home below the ceiling, the compressor idles for free. If the breeze fails and indoor rises above `comfort_cool`, the thermostat cools without waiting for the next CA 30-minute cycle.
- **Economizer (both phases):** fan on (or HVAC fan mode), band unchanged. The band holds `comfort_cool`, so the economizer never sets the HVAC mode/setpoint (Issue #264) — cool-down assists with the fan while the band cools; maintain holds it via ventilation.
- **Escalation:** when the ODE ceiling guard (§6c) fires, nat-vent is cleared (`_natural_vent_active = False`) and a `nat_vent_ceiling_escalation` event is emitted — the band was already armed at the cool ceiling, so "escalation" means allowing the compressor to run rather than re-programming the setpoint.

**Why no more HVAC off on nat-vent:** Turning HVAC off on nat-vent activation disarmed the floor. If outdoor conditions changed mid-night (cold snap), CA would not re-heat until the next 30-minute cycle noticed the floor breach — up to 30 minutes of the home sitting below the comfort floor. With the band always armed, the thermostat heats immediately.

#### `_apply_nat_vent_hvac_state()` — Band Arming on Nat-Vent Activate (Fix #338)

`_apply_nat_vent_hvac_state()` is called at every nat-vent activation site — initial activation, re-activation from paused state, and on every 30-minute `apply_classification()` cycle while nat-vent is active — to ensure the correct band is armed alongside the running fan.

| Fan archetype | `aggressive_savings` | Sleep window? | Band armed | Rationale |
|---|---|---|---|---|
| `FAN_MODE_WHOLE_HOUSE` or `DISABLED` | any | any | No-op | HVAC already suppressed by fan activation path; no band to arm |
| `FAN_MODE_HVAC` only | `False` | **Yes** | No setpoint call — emits `nat_vent_ac_assist_armed` only | Sleep band applied by the subsequent `select_comfort_band(in_sleep_window=True)` call in `apply_classification()`; avoids redundant thermostat write at daytime comfort ceiling immediately overwritten by sleep ceiling (Issue #341) |
| `FAN_MODE_HVAC` only | `False` | No | Full comfort band at `[comfort_heat, comfort_cool]` ceiling | AC assists if the breeze cannot hold the ceiling; floor is re-armed on the next 30-min `apply_classification()` cycle |
| `FAN_MODE_HVAC` only | `True` | any | Floor-only: `heat` mode @ `comfort_heat`; ceiling disarmed | Running the compressor through open windows defeats the savings the user configured; occupant accepts ceiling drift if breeze fails |

**Sleep window deference (Issue #341):** When nat-vent is active during the sleep window and `aggressive_savings=False`, `_apply_nat_vent_hvac_state()` emits `nat_vent_ac_assist_armed` (so the status card and activity report show nat-vent active) but skips `_apply_comfort_band()`. The `select_comfort_band(in_sleep_window=True)` call immediately following in `apply_classification()` programs the thermostat with the sleep band (`sleep_heat`/`sleep_cool`). Without this guard, two conflicting setpoints were written every 30-min cycle all night: the daytime comfort ceiling first, then the sleep ceiling immediately after. The sleep ceiling won (applied last), but the thermostat received redundant writes and the activity report showed confusing dual entries.

**Path B fix (re-activation from paused state):** Before Fix #338, when nat-vent re-activated from a paused state (all conditions met again after the 300 s lockout), `_apply_nat_vent_hvac_state()` was not called on that path. The band was not re-armed until the next 30-minute `apply_classification()` cycle — a window of up to 30 minutes during which the thermostat ran with no CA-programmed ceiling. Fix #338 calls `_apply_nat_vent_hvac_state()` in `check_natural_vent_conditions()` at the re-activate node (§12b in the flowchart).

**Sensor-close fix (warm/mild days):** When all sensors close while nat-vent is active on a warm or mild day, `handle_all_doors_windows_closed()` re-arms the full comfort band immediately. Previously the warm/mild path skipped the re-arm because the `if c.hvac_mode in ("heat", "cool")` check failed for the classifier's `"off"` label — the thermostat ran without an armed ceiling for up to 30 minutes until the next `apply_classification()` cycle.

#### Scheduled Handlers That Use the Band

All scheduled state handlers now route through `select_comfort_band` + `_apply_comfort_band`. The old per-handler divergent off/heat/cool/setback bodies are replaced:

| Handler | Band context |
|---|---|
| `apply_classification()` (30-min cycle) | Daytime band, or sleep band when `_in_sleep_window()` matches |
| `handle_bedtime()` | Sleep band (`sleep_heat` / `sleep_cool` / adaptive) |
| `handle_morning_wakeup()` | Comfort band (home/guest) |
| `handle_occupancy_away()` | Setback band |
| `handle_occupancy_vacation()` | Deep-setback band |
| `_apply_current_scheduled_state()` | Comfort band for current time context |

#### Interaction with §6b and §6c

With the band armed, both the comfort-floor guard (§6b) and the ODE ceiling guard (§6c) are naturally dormant under normal conditions — the thermostat holds both edges between CA cycles. Both guards remain in place as lightweight always-on safety nets that activate if the band lapses (HA restart, manual override recovery, thermostat reconnect). Neither guard is gated or disabled; they simply find no condition to act on when the band is programmed.

#### Constants

| Constant | Value | Purpose |
|---|---|---|
| `CEILING_ESCALATION_SAVINGS_MARGIN_F` | `2.0°F` | Ceiling tolerance above `comfort_cool` for `aggressive_savings` mode |
| `VACATION_SETBACK_EXTRA` | `3°F` | Extra depth beyond normal away setback for vacation bands |

**Test coverage:** `tests/test_thermostat_program.py` (`select_comfort_band` band-edge rules across all occupancy / sleep / aggressive cases; `_apply_comfort_band` dual/cool/heat/no-op command shapes, idempotent mode, dry-run); `tests/test_warm_day_setback.py::TestWarmDayBandArming` + `tests/test_warm_day_comfort_gap.py` (warm-day band arming); `tests/test_occupancy_setback_mode.py`, `tests/test_occupancy_automation.py`, `tests/test_bedtime_setback.py` (handler band integration); `tests/test_window_hvac_interaction.py`, `tests/test_door_window.py`, `tests/test_fan_control.py`, `tests/test_economizer.py` (nat-vent/economizer band-stays-armed); `tests/test_production_harness.py` + `tools/simulations/golden/cold_morning_warm_day_no_breach.json`, `…/startup_indoor_below_heat_floor_warm_day.json` and the `p3_*` pending scenarios (end-to-end band arming on the real engine).

### 6f. Comfort-Family Defense FSM (Issue #827, consolidates #821/#823)

**History:** Issue #821 closed the original bug (§6f used to be titled "Comfort-Floor Defense on Cool-Classified Days") but its fix — a confidence-gated fallback resolver plus a separately-armed dwell-timer lockout (Issue #823 then patched a reassertion bug in that lockout) — turned out to be three inconsistent authorities layered on top of §6e's day-type-only `select_comfort_band()`: the band picker (never reads indoor temp), the confidence-gated resolver (no fallback between its two paths, no exit hysteresis, read a flat non-sleep-aware `comfort_heat`), and the dwell-timer lockout (a separately-armed clock, the direct site of #823's patch). A live incident after #823 shipped (a zone sitting 5°F+ below its heating floor for hours) still wasn't fixed by either prior patch — a fuller audit found the split-authority design itself was the recurring root cause, not any one bug within it. Issue #827 retires all three into a single strangler-fig pure-leaf/FSM pair, the same pattern already used for nat-vent, door/window, override/grace, fan/WHF, classification, occupancy, and the economizer.

**The consolidated FSM:** `comfort_family_fsm.py`'s `transition(current_state, event, dwell_state)` is the single decision point for "should the heating or cooling family be active right now," backed by the pure leaf `comfort_family_decision.py`'s `decide_comfort_family()`. Both modules are zero-side-effect, zero-logging, zero-HA-import — mirroring `ode_floor_guard.py`/`ode_ceiling_guard.py`'s frozen `Inputs`/`Outcome`/`Decision` contract shape. `_resolve_comfort_family_via_fsm()` in `automation.py` is the shell that wires them in, consumed by **both** `_apply_comfort_band()` (§6e) and `_set_temperature_for_mode()` — the latter is itself the sole choke point for its 7 callers (TOU pre-conditioning, door/window-all-closed resume, `check_natural_vent_conditions()`'s own COMFORT_FLOOR restore, `resume_from_pause()`, occupancy-home restore, economizer deactivation, and `_exit_nat_vent()`'s sensors-closed restore branch), so wiring the FSM in there once covers all 7 without per-site changes — **except** TOU pre-conditioning: a caller passing `target_override` bypasses the FSM entirely and uses `c.hvac_mode` directly (see below, unchanged from #821). `_resolve_comfort_family_mode()` and `_family_switch_locked_out()` (the two retired #821/#823 mechanisms) are **deleted**, not deprecated.

**`base_family`, not day-type "native" — a correctness fix found during this issue's own Verification pass.** An earlier draft of this consolidation used the day-type-derived native family (`DAY_TYPE_HOT`/`WARM` → cooling, `DAY_TYPE_COOL`/`COLD` → heating) as the *base* authority the FSM measures breaches against — the same shape §6e's `select_comfort_band()` uses for picking `active`. That premise is false against real code: `select_comfort_band()`'s `active` (and hence the family a cycle should be in, absent any breach) comes from `classification.hvac_mode`, an **independent** authority that routinely disagrees with the day-type heuristic — golden scenario `override_self_resolve_transient` is exactly that case (`day_type="cool"`, `hvac_mode="cool"`, i.e. native says heating but the classifier itself wants cooling). With native as the base, the FSM would return `ESCALATE → "heating"` on that scenario with indoor squarely mid-band and zero real breach — silently rewriting the classifier's own decision into a furnace command. `ComfortFamilyInputs.base_family` is therefore the classifier's own family for the cycle (derived from the caller's `day_mode`), and day type is used **only** to scale which direction is native vs. against-grain for deadband purposes — never as the target family itself. See `comfort_family_decision.py`'s module docstring and `ComfortFamilyInputs.base_family`'s own docstring for the full trace of this correction.

**Two-state model, per cycle:**
- **Native (or base-family) shape** — `current_family == base_family`: checks both directions for a NEW breach. The direction day type already favors (`_native_family(day_type)`) escalates at a near-zero deadband; the against-grain direction must clear the day-type-scaled deadband first (table below). The heating direction additionally consults `ode_floor_guard.decide_ode_floor_guard()` first: `ESCALATE` fires immediately, `STANDING_BY` is respected (no fallback this tick), and any "guard can't decide" outcome (`MODEL_INELIGIBLE`, `MISSING_TEMPS`, `NO_BREACH_PREDICTED`) falls through to the same sustain-confirm+deadband fallback the cooling direction always uses — the **universal fallback**: this is the permanent floor under the ODE guard, not gated on `confidence_k_passive == "none"` literally (the pre-#827 resolver's own dead end whenever confidence was non-`"none"` but no cached `comfort_floor_crossing_time` was available for this cycle — GAP-2 of the audit).
- **Against-grain shape** — `current_family` was reached by a genuine breach-driven `ESCALATE`: the only way out is a **recovery-margin**-gated, sustain-confirmed `REVERT` back to `base_family`. Indoor must clear the floor/ceiling by the *same* deadband value, but **in the comfort direction** (not merely stop breaching), then sustain-confirm — this is the new mechanism (neither ODE guard module had it before) that defeats the saw-tooth a same-threshold entry/exit pair would produce. A day-type change alone (yesterday hot, today cool) is **not** an escalation — `is_against_grain` is tracked separately and only set True by a genuine breach-driven `ESCALATE`, so a plain native-family handoff when the day itself changes stays a zero-friction pass-through with no hysteresis.

**Day-type deadband table** (`deadband_against_grain_f`, measured from the already-resolved `band.floor`/`band.ceiling` — the same anchor `aggressive_savings`' `CEILING_ESCALATION_SAVINGS_MARGIN_F` widening already uses, not a second, independently-drifting one):

| Day type | Config key | Default | Clamp |
|---|---|---|---|
| Hot | `comfort_deadband_hot_f` | 5.0°F | [2.0, 8.0] |
| Warm | `comfort_deadband_warm_f` | 2.0°F | [1.0, 5.0] |
| Mild / off | `comfort_deadband_mild_f` | 2.0°F | [1.0, 5.0] |
| Cool | `comfort_deadband_cool_f` | 2.0°F | [1.0, 5.0] |
| Cold | `comfort_deadband_cold_f` | 5.0°F | [2.0, 8.0] |

An off-classified day (`hvac_mode="off"`, `day_type` warm or mild) now gets real, if conservative, defense (GAP-1 of the audit — previously zero) at the mild/off tier, since `_native_family()` returns `None` for `DAY_TYPE_MILD` and the mild-tier deadband is the fallback for any unrecognized day type too. Away/vacation uses the same table against the setback floor/ceiling (GAP-6) — `select_comfort_band(occupancy_mode=away)` always resolves `active="ceiling"` regardless of day type, so `base_family` is `"cooling"` for any day while away, but the deadband *tier* is still day-type-scaled the same way. Overnight, the floor/ceiling inputs are the sleep-window-resolved `band.floor`/`band.ceiling` (`sleep_heat`/`sleep_cool`), not the flat daytime `comfort_heat`/`comfort_cool` (GAP-5) — the pre-#827 resolver read the flat config value even inside the sleep window, which could falsely arm (or fail to arm) a candidate against the wrong floor.

Named `deadband`, not `hysteresis` — deliberately distinct vocabulary from `NAT_VENT_HYSTERESIS_F` (a symmetric, day-type-independent, override-blind noise filter). This concept is asymmetric (native vs. against-grain), day-type-scaled, and override-aware in a way that term doesn't describe; see `const.py`'s comment at the 5 `comfort_deadband_*_f` `CONFIG_METADATA` entries for the one-line non-unification note.

**The against-grain deadband is now CONDITIONAL on recent opposite-family activity (Issue #843).** Reported incident: a house free-cooled via nat-vent, nat-vent ended, and indoor kept drifting several degrees below the comfort floor for hours before heat finally engaged — the deadband applied unconditionally, waiting for a large breach even though nothing had run in hours and there was nothing to short-cycle away from. `comfort_family_decision.py`'s `_against_grain_deadband()` now returns `0.0` (same as the native direction) whenever the opposite family's last recorded activity is outside `comfort_family_recency_window_min` (config key, default 120 min) or was never recorded — only when something recent to protect against actually exists does the full day-type deadband from the table above apply. "Recent" is tracked via two `AutomationEngine` fields: `_last_hvac_heating_active`/`_last_hvac_cooling_active` (pre-existing, Issue #835 — continuously refreshed by the coordinator's per-cycle `hvac_action` ground-truth read while active, frozen at the last-active moment once it stops) and two new siblings, `_last_fan_active` (WHF or HVAC-fan — both set `_fan_active`, so one field covers both) and `_last_natvent_active`, refreshed the same way. Asymmetric by design: WHF/HVAC-fan/nat-vent all count toward "recent cooling," but only actual HVAC-heat runtime counts toward "recent heating" — no fan equivalent exists on the heating side. The same recency gate also demotes the ODE floor guard's own `ESCALATE` outcome (which normally bypasses the deadband/sustain checks entirely via its own lead-time logic) when the heat direction is against-grain and something recent exists (`ode_floor_escalate_gated` in `_decide_entry()`), and gates the ODE ceiling guard's predictive `_apply_ode_ceiling_guard_decision()` the same way (previously a hard, ungated bypass — it now also arms `_arm_comfort_family()` on escalation, closing a gap where the FSM's own dwell/recency bookkeeping went stale relative to what the thermostat was actually doing). Test coverage: `tests/test_comfort_family_decision.py::TestRecencyGatedDeadband` (the leaf), `tools/simulations/pending/issue_843_mild_day_no_recent_cooling_escalates_at_boundary.json` (the reported incident, end-to-end via the real production engine). The nat-vent savings-mode floor guard inside `_apply_nat_vent_hvac_state()` (`fan_mode="hvac_fan"` + `aggressive_savings=True`) was removed entirely by the same issue — it force-committed heat while nat-vent (windows open) was still the active session, contradicting this project's "no HVAC while windows are open, absent a manual override" principle, and was redundant with `decide_nat_vent_exit()`'s already-existing, fan_mode-agnostic `COMFORT_FLOOR` exit reason, which already restores heat through this same FSM-routed (and now recency-gated) path once nat-vent's own thermostatic evaluation decides to end the session.

**Observability follow-up (Issue #843, post-deploy review):** the initial #843 ship logged every family switch to HA core logs (`_LOGGER.info`) but never persisted it as an activity event — every other consequential automation decision in this codebase (grace, override, nat-vent exits, pre-cool, bedtime, occupancy setback...) reaches the coordinator's `event_log` via `self._emit_event_callback()`, but a comfort-family switch didn't, so it was invisible to the daily briefing and AI investigator. The log line itself also only carried the static `reason` string (e.g. `"deadband cleared and sustain-confirmed"`), which reads identically whether the switch was a genuine full-breach escalation or the new recency-gated immediate switch — the one distinction this whole fix exists to make observable. `_resolve_comfort_family_via_fsm()`'s `result.changed` branch now also emits a `comfort_family_switch` event (and logs the same values) carrying `deadband_applied_f` (0.0 = recency-gated, nonzero = a real breach cleared the day-type deadband) plus `minutes_since_cooling_ended`/`minutes_since_heating_ended`. Rendered in the briefing/investigator via `ai_skills_context.py`'s `_render_comfort_family_switch()`. Test coverage: `tests/test_comfort_family_shell_events.py::TestComfortFamilySwitchEventContract`.

**Manual override changes what's intentional, not the safety backstop.** The pre-#827 resolver blocked escalation unconditionally whenever `_manual_override_active` was set — right for a small, deliberate deviation, wrong once indoor has cleared the floor/ceiling by a wide margin regardless of cause. The FSM instead doubles the effective deadband for a **new** against-grain escalation (`_OVERRIDE_DEADBAND_MULTIPLIER = 2.0`): a breach between 1x and 2x the configured deadband is held (`OVERRIDE_HELD`) while override is active; a breach past 2x always escalates regardless of override — `_check_direction()`'s override check simply doesn't apply once `breach_delta > effective_deadband`, so the decision proceeds exactly as if no override existed. Override never gates a `REVERT` (exiting an against-grain state) — only entry into a new against-grain escalation. **Documented limitation, not an assumption:** in the current wiring, the FSM is reachable while an active *mismatched* manual override exists only through nat-vent's floor-exit call sites (`_exit_nat_vent()`'s restore branch, `check_natural_vent_conditions()`'s COMFORT_FLOOR branch) — `apply_classification()`'s own inline override check (and `decide_scheduled_band_gate()`'s `DEFER_OVERRIDE` used by `handle_bedtime()`/`handle_morning_wakeup()`/`handle_pre_cool()`) bails before ever reaching the FSM whenever an override is active and mismatched, and occupancy transitions unconditionally clear an active override on entry (`decide_away_vacation_dispatch()`). Each nat-vent floor-exit fires at most once per session, so end-to-end proof of a full confirm-to-`ESCALATE` transition *past* an active override's 2x window is unit-tested at the leaf/FSM level (`tests/test_comfort_family_decision.py::TestManualOverride`) rather than re-provable via a second real production trigger today — see `tools/simulations/pending/issue_827_warm_day_override_exceeds_deadband_bypasses_hold.json`'s own notes for the full investigation trail.

**Min-dwell anti-flap folds into `transition()` itself**, not a separately re-armed timer (`comfort_mode_switch_min_interval_s`, default 600s, advanced config, unchanged config key from #821/#823). `dwell_since` only advances on a genuine state change (`ComfortFamilyTransition.changed is True`) — a HOLD/WITHIN_DEADBAND/SUSTAINING/OVERRIDE_HELD/RECOVERING tick never touches it. This is the structural fix for #823: there is only one place the dwell clock can move, and it moves only on real change, so the "reassertion resets a clock that should only move on real change" bug class is unrepresentable rather than patched around (see `tests/test_comfort_family_fsm.py::TestMinDwellAntiFlap::test_what_would_happen_if_dwell_reset_on_every_call_regression_control` for the direct #823-regression-class negative control). When the FSM wants a transition but the dwell timer blocks it, the shell logs and emits `comfort_family_switch_locked_out` (same event name/payload shape — `candidate_family`, `reason` — as the retired mechanism, per Design's preserved-contract requirement).

**Cold start:** `transition()` evaluates real thresholds immediately through the same leaf every real cycle uses — no separate cold-start branch, no assumed starting family. A fresh process's `dwell_since` is `None`, so the very first genuine transition is unconditionally allowed (matches the retired lockout's own "cold start always allowed" precedent). **Restart persistence:** deliberately NOT persisted, matching every other FSM's documented convention in this codebase (`economizer_fsm.py`, `nat_vent_fsm.py`).

**Preserved contracts (Design §2 — kept the diff bounded):** `ComfortBand.active` is unchanged, still computed by `select_comfort_band()`'s existing day-type logic — only the two call sites above swap their callee. `self._comfort_mode_family` stays a compatibility attribute, written on every FSM cycle via the same `_arm_comfort_family()` writer the 7 out-of-scope callers (nat-vent/WHF activation, `_exit_nat_vent()`, etc.) still use directly for their own bookkeeping — unaffected by this issue. TOU's `target_override` bypass stays a call-site-level ternary skip in `_set_temperature_for_mode()`, not an FSM input flag (unchanged from #821 Verification BLOCKING #3).

**`GAP-7`** (fixed timers not rate-scaled to climate severity) is explicitly out of scope for this pass — flagged in `comfort_family_decision.py`'s own docstring as a known limitation, no live evidence motivating it yet.

**Test coverage:** `tests/test_comfort_family_decision.py` (the pure leaf — `TestNotApplicable`, `TestNativeDirectionTightEscalation`, `TestAgainstGrainDeadbandHeld`/`TestAgainstGrainDeadbandCleared`, `TestManualOverride`, `TestOdeFloorGuardFallbackReachability`, `TestRecoveryMarginHysteresis`, `TestMildDayBothDirectionsAgainstGrain`, `TestColdStartEvaluatesImmediately`); `tests/test_comfort_family_fsm.py` (the FSM — `TestColdStart`, `TestMinDwellAntiFlap`, `TestLockedOutEventSignal`, `TestHoldAndEscalateWiring`, `TestDayTypeChangeIsNotTreatedAsAgainstGrain`); `tests/test_comfort_mode_family_lockout.py` (RETIRED contents — `TestArmComfortFamily`/`TestArmComfortFamilyOnlyIfChanged` still test the surviving compatibility writer directly; the module docstring maps every retired class to its replacement above); `tests/test_confirmed_transition.py` (sustain-confirmation primitive, unchanged); `tests/test_ode_floor_guard.py` (mirrors `test_ode_ceiling_guard.py`, unchanged — only referenced by this section, no behavior change); `tests/test_tou_precondition.py::TestTouPreconditionResolverBypass` (still passes unchanged — proves `target_override` callers bypass the FSM even when escalation would otherwise fire); `tools/simulations/pending/issue_821_comfort_floor_fallback_no_confidence.json` and `issue_821_exit_nat_vent_restore_defends_floor.json` (the original #821 incidents, retimed for #827's deadband where the original 1°F breach no longer clears the new hot-day 5°F tier — see the latter's own notes for the exact numeric-trajectory correction and why it keeps testing the same thing); plus 9 new end-to-end pending scenarios closing every gap the Issue #827 audit found: `issue_827_off_day_floor_breach_escalates.json` (GAP-1), `issue_827_missing_forecast_fallback_escalates.json` (GAP-2), `issue_827_sleep_window_floor_mismatch.json` (GAP-5), `issue_827_away_setback_floor_breach.json` (GAP-6), `issue_827_recovery_margin_prevents_sawtooth.json` (the recovery-margin hysteresis, real-engine, end-to-end saw-tooth negative control), `issue_827_hot_day_dawn_dip_within_deadband_no_escalate.json` / `issue_827_hot_day_deadband_cleared_escalates.json` (the deadband boundary, both directions), and `issue_827_warm_day_override_within_deadband_respected.json` / `issue_827_warm_day_override_exceeds_deadband_bypasses_hold.json` (the override-deadband interaction, both directions — the latter's own notes document the real-engine reachability limitation described above).

---

## 7. Window Recommendations

Window advice is set by the classifier at classification time, based on `day_type` and forecast lows.

| Day Type | Windows Recommended? | Open Time | Close Time | Condition |
|---|---|---|---|---|
| `hot` | Not a traditional recommendation — window *opportunities* only | 6:00 AM | 9:00 AM | Morning opportunity: `today_low <= 80` |
| `hot` | Evening opportunity | 5:00 PM | Midnight (00:00) | Evening opportunity: `tomorrow_low <= 80` |
| `warm` | Yes (if condition met) | 6:00 AM | 10:00 AM (`WARM_WINDOW_CLOSE_HOUR`) or `nat_vent_cutoff` when ODE available | `today_low <= comfort_cool - ECONOMIZER_TEMP_DELTA` = `today_low <= 72°F` (defaults) |
| `mild` | Always yes | 10:00 AM (`MILD_WINDOW_OPEN_HOUR`) | 5:00 PM (`MILD_WINDOW_CLOSE_HOUR`) or `nat_vent_cutoff` when ODE available | No condition — always recommended |
| `cool` | No | — | — | — |
| `cold` | No | — | — | — |

**Warm-day window condition formula:** `today_low <= DEFAULT_COMFORT_COOL - ECONOMIZER_TEMP_DELTA` = `75 - 3 = 72°F` at defaults. Constant: `WARM_WINDOW_OPEN_HOUR = 6`, `WARM_WINDOW_CLOSE_HOUR = 10`. **Like MILD (below), the static `WARM_WINDOW_CLOSE_HOUR` is only a fallback** — `briefing.py`'s `_derive_warm_day_events()` (§9e, Issue #528) overrides it with the ODE-derived `nat_vent_cutoff` whenever a forecast curve is available, exactly the same cascade §6d documents for MILD days. This table previously omitted that caveat for WARM specifically, which read as a contradiction between a correct-but-dynamic production briefing and this static reference.

**MILD-day window times (v0.3.46+):** Open time is always `MILD_WINDOW_OPEN_HOUR = 10` (10:00 AM). Close time uses `nat_vent_cutoff` when the ODE is calibrated, otherwise falls back to `MILD_WINDOW_CLOSE_HOUR = 17` (5:00 PM). See [§6d. MILD Day Dynamic Window Close Time](#6d-mild-day-dynamic-window-close-time-fix-c-issue-147).

**Reason wording for the close time (Issue #847; card consumer removed by #849):** both WARM and
MILD close-time sentences derive from the shared `describe_nat_vent_cutoff_reason()` helper (see
§6d's Issue #847 update above), not independent per-consumer branches — this is what keeps the
briefing body and the TL;DR header from drifting into contradictory framing of the same
`nat_vent_cutoff_reason` value. **The Next Automation status card was originally a third consumer
of this same helper; Issue #849 removed its cutoff-reason candidate entirely** (it instructed the
occupant to close/reopen windows, an action CA cannot execute — a distinct ontology violation, not
a phrasing bug this helper would have fixed), so the symmetry guarantee described here now applies
only within briefing.py's own two call sites. See the DOC RULE in §6d before adding user-facing
text for any new `nat_vent_plan` field.

---

## 8. Economizer (Window Cooling on Hot Days)

The economizer uses open windows on hot days to make the band's cooling cheaper. Under the #249 band
model it is **fan-assist only**: the comfort band (§6e) holds `comfort_cool`, so the economizer no
longer sets the HVAC mode or setpoint (Issue #264) — it runs the fan to pull cool outdoor air through
the open window. It never overrides the band; there is no separate economizer on/off toggle (it is
gated purely by the eligibility conditions below).

### Eligibility

All of the following must be true simultaneously:

| Condition | Formula / Value |
|---|---|
| Day type | `day_type == hot` |
| Windows open | `windows_physically_open == True` |
| **Free-cooling direction** | **`outdoor_temp < indoor_temp`** (Issue #327) — outdoor air must be cooler than indoor; if outdoor ≥ indoor the fan would heat the house rather than cool it |
| Outdoor temp ceiling | `outdoor_temp <= comfort_cool + ECONOMIZER_TEMP_DELTA` = `outdoor_temp <= 78°F` (defaults) |
| Time window | 6:00–9:00 AM **or** 5:00 PM–midnight |

The free-cooling-direction guard (Issue #327) mirrors the identical guard already required by nat-vent activation (§17). It prevents evening activation on hot days when outdoor temperatures remain above indoor well into the evening — a scenario where the economizer would work against comfort rather than assist it.

### Phase Behavior

| Mode | aggressive_savings | Phase | Condition | Action |
|---|---|---|---|---|
| Normal | `False` | Phase 1: cool-down | `indoor_temp > comfort_cool` | **Activate the fan only** — the #249 band already holds `comfort_cool`; the economizer pulls cool outdoor air through the open window to assist the band's cooling. It does **not** set the HVAC mode/setpoint (Issue #264 — that would flip the `heat_cool` band to single `cool`). |
| Normal | `False` | Phase 2: maintain | `indoor_temp <= comfort_cool` | Activate the fan; the band stays armed (no `hvac_mode=off` — Issue #249) |
| Savings | `True` | Maintain only (skip Phase 1) | Any eligible condition | Activate the fan; band stays armed; no AC assist (savings relies on ventilation) |

When the economizer deactivates (conditions no longer met), the fan is turned off; the comfort band continues to hold the thermostat — no HVAC mode change is issued (Issues #249/#264).

---

## 9. Fan Control

Fans activate during natural ventilation and during the economizer (both phases — cool-down assists the band's cooling, maintain holds it; Issue #264). Fan behavior is controlled by the `fan_mode` config setting.

| fan_mode value | Activate action | Deactivate action |
|---|---|---|
| `disabled` | No action | No action |
| `whole_house_fan` | `turn_on` the configured `fan_entity` (using the entity's own domain — `fan` or `switch`) | `turn_off` the configured `fan_entity` |
| `hvac_fan` | `climate.set_fan_mode` → `"on"` on the thermostat entity (subject to the `hvac_fan_restrict_mode` guard below) | `climate.set_fan_mode` → `"auto"` on the thermostat entity |
| `both` | Both `whole_house_fan` and `hvac_fan` actions (HVAC-fan leg subject to `hvac_fan_restrict_mode`; WHF leg unaffected) | Both deactivate actions |

### Fan Archetype Behavioral Contract (Issue #277)

`FAN_MODE_HVAC` and `FAN_MODE_WHOLE_HOUSE` have different behavioral roles. These contracts were implicit before Issue #277; they are now explicit.

#### `FAN_MODE_HVAC` — HVAC Blower / Air Circulation

The HVAC fan circulates indoor air through the duct system. It is an integral part of the thermostat and does not exchange air with the outdoors.

| Behavior | Detail |
|---|---|
| On activation | `climate.set_fan_mode(on)` issued; comfort band **stays armed**; HVAC mode unchanged; thermostat self-arbitrates (compressor runs if needed) |
| On deactivation | `climate.set_fan_mode(auto)` issued; comfort band unchanged |
| Stops when windows close? | **No** — unless `_natural_vent_active = True` at the time all sensors close. Fan-only circulation is independent of window state; only the nat-vent path stops the fan on sensor-close. |
| HVAC mode captured? | No — `_pre_fan_hvac_mode` is not set |

##### `hvac_fan_restrict_mode` — heat/cool/both restriction (Issue #835)

Orthogonal to `fan_mode` (which mechanism runs) and independently configured: `hvac_fan_restrict_mode` gates *when* the HVAC-fan leg (`fan_mode = hvac_fan` or `both`) is allowed to activate, based on which HVAC mode last ran. It does **not** affect the whole-house fan leg — a `FAN_MODE_BOTH`-configured install still activates WHF normally even while the HVAC leg is restricted. Purpose: running the blower shortly after a cooling cycle re-evaporates condensate off the still-wet evaporator coils, raising indoor humidity — a real problem in humid climates.

| Value | Behavior |
|---|---|
| `both` (default) | No restriction — current/legacy behavior, unchanged. |
| `heat` | Blocks HVAC-fan activation while the thermostat is currently in `cool` mode, or `off` with a cooling cycle (`hvac_action == "cooling"`) recorded within the last 2 hours. Recommended for humid climates. |
| `cool` | Symmetric restriction: blocks activation while currently in `heat` mode, or `off` with a heating cycle within the last 2 hours. No known real-world use case; included for selector symmetry. |

Implementation: `AutomationEngine._hvac_fan_restriction_block_reason()` is the single choke point, called from `_activate_fan()` right before the `climate.set_fan_mode(on)` write — both HVAC-fan trigger paths (the periodic duty-cycle circulation and the natural-vent/economizer free-cooling path) converge there before any hardware write happens, so no duplicated gate logic exists. When blocked, `_activate_fan()` returns `FanCommandResult.SUPPRESSED` and logs `"Feature suppressed: HVAC fan not activated — <reason>"` at INFO — no `fan_activated` event fires and `_fan_active` is not set, since nothing was actually commanded. This is a **start-gate only**: it does not force an already-running HVAC-fan cycle off mid-run if HVAC begins actively heating/cooling in the restricted direction partway through.

`AutomationEngine._last_hvac_heating_active` / `_last_hvac_cooling_active` (ISO timestamps) track the most recent live `hvac_action` read of `"heating"`/`"cooling"`, updated once per coordinator cycle (piggybacking on the existing unconditional `hvac_action` read in `_async_update_data()` rather than polling separately). Unlike override/grace state, these two fields are **restored** (not clean-slate) across an HA restart via `get_serializable_state()`/`restore_state()` — forgetting a recent cooling cycle on restart would let the guard immediately approve activation into a still-wet coil, recreating exactly the issue the feature exists to prevent. No recorded history for the restricted mode is treated as "not recently active" (allowed) — absence of evidence isn't itself a block.

#### `FAN_MODE_WHOLE_HOUSE` — Separate Exhaust / Air Exchange Fan

The whole-house fan is a dedicated appliance (e.g., `fan.*` or `switch.*` entity) that pulls outdoor air through the house. Running it with active heating or cooling wastes energy or fights the thermostat.

| Behavior | Detail |
|---|---|
| On activation | Fan entity turned on; **HVAC set to `off`**; current thermostat mode captured in `_pre_fan_hvac_mode`. Applies whether CA initiated the activation (`_activate_fan()`) **or** a manual/remote fan-on was detected (`handle_fan_manual_override()`, Issue #495) — both funnel through the shared `_suppress_hvac_for_whf()` helper, so the mutual-exclusion rule has exactly one suppression path. |
| On deactivation | Fan entity turned off; HVAC mode restored from `_pre_fan_hvac_mode` (then `_pre_fan_hvac_mode` cleared) for a CA-initiated session (`_deactivate_fan()`). A manual/remote session instead **reclassifies** (`_release_whf_and_reclassify()`, Issue #495) rather than blindly restoring the captured mode — an RF-remote-timer session can span hours (up to 12h), so the mode captured at activation is often stale by exit (e.g. it can straddle a sleep-setback transition). |
| Stops when windows close? | **Yes** — when ALL monitored sensors close, the fan deactivates and HVAC is restored, regardless of `_natural_vent_active` value |
| HVAC mode captured? | Yes — `_pre_fan_hvac_mode: str \| None` holds the thermostat mode at activation time (e.g., `"heat_cool"`, `"cool"`) |

#### `FAN_MODE_BOTH`

**As of v0.4.72 (Issue #424), `FAN_MODE_BOTH` is no longer selectable via config** — it was removed from the setup/options fan-mode selector, and existing configs with `fan_mode: "both"` are migrated to `FAN_MODE_WHOLE_HOUSE` on config-entry load. The behavior contract below remains accurate only because the branch logic is intentionally left in place in `automation.py`/`coordinator.py` (unreachable for new configs, not deleted) — not because users can still pick it.

Each component (HVAC fan + whole-house fan) follows its own archetype contract above. `_pre_fan_hvac_mode` is still set, because the whole-house fan component requires HVAC suppression.

### Structural WHF/AC Mutual Exclusion — `_whf_owns_hvac()` Choke-Point Guard (Issue #392 Fix 1b)

**Can the whole-house fan and the compressor ever both be commanded on at the same time? No — this is now a structural guarantee, not a per-caller convention.**

Before Issue #392, mutual exclusion was enforced only by convention inside `_activate_fan()`/`_deactivate_fan()` themselves (see the behavioral contract table above). Nothing stopped any of the ~13 other `_set_hvac_mode()` call sites, or the several `_apply_comfort_band()` call sites, from writing an active HVAC mode while a WHF session owned the thermostat. This was a real, confirmed gap: `apply_classification()`'s normal (non-`aggressive_savings`) fall-through called `_apply_comfort_band()` → `_set_temperature(..., mode="cool")` on every 30-minute cycle even while `_natural_vent_active` was `True` under `FAN_MODE_WHOLE_HOUSE` — re-arming the thermostat to `cool` every cycle while the WHF was physically running, fighting the fan CA itself had just turned on.

**The fix: one guard at the single choke point every HVAC write already passes through**, rather than patching each caller individually (per this project's "trust internal invariants, single choke point" philosophy).

```python
def _whf_owns_hvac(self) -> bool:
    fan_mode = self.config.get(CONF_FAN_MODE, FAN_MODE_DISABLED)
    return fan_mode in (FAN_MODE_WHOLE_HOUSE, FAN_MODE_BOTH) and self._pre_fan_hvac_mode is not None
```

`_whf_owns_hvac()` is `True` only when both hold: the configured archetype includes a whole-house fan, **and** a suppression session is currently active (`_pre_fan_hvac_mode is not None` — the same flag `_activate_fan()`/`_deactivate_fan()` already use to track an active suppression). It deliberately does not use `_natural_vent_active`, because that flag is also `True` for `FAN_MODE_HVAC` nat-vent sessions, where HVAC is *not* suppressed and writes must be allowed through.

Both HVAC-writing functions check this at their very top, before any service call:

```python
# inside _set_hvac_mode(mode, *, reason) and _set_temperature(temperature, *, reason, mode)
if mode != "off" and self._whf_owns_hvac():
    _LOGGER.warning("HVAC write blocked — whole-house fan owns thermostat (%s)", reason)
    if self._emit_event_callback:
        self._emit_event_callback("hvac_write_blocked_whf_active", {"attempted_mode": mode, "reason": reason})
    return
```

Key properties:
- **`mode == "off"` is never blocked** — the guard only intercepts attempts to arm an *active* mode (`heat`, `cool`, `heat_cool`) while WHF owns the thermostat. Turning HVAC off is always allowed (it's what a WHF session wants anyway).
- **Silent drops are made visible.** A blocked write logs a `WARNING` and emits `hvac_write_blocked_whf_active` (payload: `attempted_mode`, `reason`) so the Activity Log shows the interception rather than the write simply vanishing — per this project's Observability Requirements.
- **`apply_classification()` also short-circuits before reaching the guard.** For `FAN_MODE_WHOLE_HOUSE`/`FAN_MODE_BOTH`, the nat-vent branch — as of Issue #495, `if self._natural_vent_active or self._whf_owns_hvac():` — returns immediately after `_apply_nat_vent_hvac_state()` — the same early-return pattern already used for `aggressive_savings=True` — so the classification cycle does not even attempt (and log) a band-arm the choke-point guard would silently drop, and does not waste a cycle computing `select_comfort_band()` or running the ODE ceiling guard while WHF owns the thermostat. The `_whf_owns_hvac()` disjunct is additive, not a replacement: it covers a manual/remote WHF session (which sets `_pre_fan_hvac_mode` via `_suppress_hvac_for_whf()` but is not a nat-vent decision, so `_natural_vent_active` stays `False`) without weakening coverage for the pre-existing `reconcile_fan_on_startup()` adopted-session case, which sets `_natural_vent_active` directly without touching `_pre_fan_hvac_mode`. `FAN_MODE_HVAC` keeps falling through to the comfort-band write exactly as before, because fan and compressor coexist for that archetype (see §6c).

**This closes Root Cause #2 of Issue #392 directly.** Because both writer functions share this one choke point, no future caller — however it decides to call `_set_hvac_mode()` or `_set_temperature()` — can bypass WHF/AC mutual exclusion. The answer to "can WHF and AC ever both be on" is now enforced at exactly one place, not re-derived correctly (or incorrectly) at every call site.

**Dispatcher-synced audit trail for `_pre_fan_hvac_mode` (Issue #722).** `door_window_fsm.py` reads `_whf_owns_hvac()` directly as a cross-subsystem input (door/window needs to know whether HVAC is currently WHF-suppressed) — a real coupling Issue #717 identified but deliberately did not wire, since deriving it from the `NAT_VENT_SESSION_*` diff would have been wrong (HVAC-fan-mode nat-vent leaves HVAC unsuppressed even while active). Issue #722 gives it its own correctly-keyed wiring instead: a new `_resolve_whf_hvac_suppression()` chokepoint wraps every real write of `_pre_fan_hvac_mode` — there are **4**, not the 2 originally suspected: `_suppress_hvac_for_whf()` (sets it), `_release_whf_and_reclassify()` (clears it), and **both** of `_deactivate_fan()`'s stranded-suppression-release branches above (the already-inactive branch and the active-fan-being-turned-off branch — each independently clears the field and was missed in the original write-up). The chokepoint emits `WHF_HVAC_SUPPRESSED`/`WHF_HVAC_RELEASED` into a `_dispatched_whf_owns_hvac` mirror attribute, same audit-trail-only role as every other `_dispatched_*` mirror (see `nat-vent-lifecycle-spec.md` § Dispatcher wiring status) — `door_window_fsm.py`'s actual input still reads `_whf_owns_hvac()` directly, not the mirror, because several tests set `_pre_fan_hvac_mode` directly without going through the dispatcher (same test-fixture conflict that reverted the FSM-builder wiring for the other 3 cross-reads).

**Shadow-diagnostic parity for `_pre_fan_hvac_mode` (Issue #724).** Separate from the dispatcher wiring above: `coordinator.py`'s `_sync_shadow_inputs()` — the raw-copy mechanism keeping the diagnostic `shadow_automation_engine` in sync with production (see §9 shadow-engine notes and Issue #613/#631/#673/#716) — never copied `_pre_fan_hvac_mode`, so `shadow_automation_engine._whf_owns_hvac()` was permanently `False` regardless of production's real state. The issue as filed called this "dormant" (nothing reads the shadow's `_whf_owns_hvac()` directly or exposes it on a sensor), but that check stopped one hop short: `_sync_paused_by_door_with_live_sensors()` — called from 4 already-mirrored entry points (`apply_classification`, `handle_bedtime`, `handle_morning_wakeup`, `handle_pre_cool`) — reads `_whf_owns_hvac()` as part of an early-return guard before calling `_pause_for_door_window()`, which sets `_paused_by_door`, a field the shadow-diagnostic's `mirror_agrees`/`door_window_mirror_agrees` axes directly compare. Without the raw copy, a genuine WHF session with a monitored window open (WHF's designed use case) made the *shadow* engine incorrectly self-pause while production correctly did not — a real, live-reachable false-disagreement source on the shadow diagnostic, not merely a future risk. Fixed by adding `_pre_fan_hvac_mode` to `_sync_shadow_inputs()`'s raw-copy block, the same one-line-per-field pattern as `_fan_active` (Issue #716). Zero production/HVAC impact — the shadow engine is permanently `dry_run=True`.

**Restart persistence, engine-identity simplification, and reload-based promotion (Issues #727 → #729).** Three related changes to the runtime control model above the mechanisms described in this section, the last two shipped together in #729 as a redesign of #727's original approach:

1. **Restart persistence (Issue #727).** The 3 per-subsystem FSM-authoritative switches were originally designed to reset to legacy/off on every Home Assistant restart, as a Phase R rollout safety guarantee. Issue #727 reversed this at the owner's request. Issue #729 then retired the 3 switches entirely (see #2) — only the single remaining `shadow_engine_primary` boolean is persisted now, restored in `async_restore_state()` **before** the same-day gate (unlike most of that function's restore logic, which discards state from a prior calendar day), since it's a mode-like setting, not daily ephemeral state.

2. **Single engine-identity switch, replacing 3 independent per-subsystem switches (Issue #729).** `switch.climate_advisor_nat_vent_fsm_authoritative`/`..._door_window_fsm_authoritative`/`..._override_grace_fsm_authoritative` let each engine's 3 FSM-authoritative flags vary independently — 2 engines × 8 combinations each, far more control surface than was ever actually used. As of #729, each engine's flags are fixed at construction: `_engine_a` is always fully legacy, `_engine_b` is always fully FSM. `switch.climate_advisor_shadow_engine_primary` is the sole remaining axis, choosing which whole engine identity is primary.

3. **Reload-based promotion, replacing a live in-process swap (Issue #729).** `coordinator.automation_engine`/`shadow_automation_engine` were plain instance attributes fixed at construction under #727 — the shadow engine (§9 below) could never issue real commands, only compare forever. #727 first made them routing properties over two fixed engine handles (`_engine_a`/`_engine_b`), selected by `self._shadow_is_primary`, and had `async_set_shadow_engine_primary()` swap them live, carrying over command-tracking fields and FSM flags by hand. **#729 found this couldn't work**: `AutomationEngine` schedules 13 internal `async_call_later` timers whose closures capture `self` at schedule time — a live swap cannot migrate an in-flight timer to the newly-primary engine, so anything still pending (grace expiry, a pending setpoint retry, etc.) kept firing against the demoted engine. #729 replaced the live swap with persist-then-reload: `async_set_shadow_engine_primary()` now saves the choice to disk and calls `hass.config_entries.async_reload()` (fire-and-forget, matching the same safe pattern `repairs.py`'s own reload call uses for code that belongs to the entry being reloaded). The reload's teardown (`coordinator.async_shutdown()` → both engines' `cleanup()`) cancels every internal timer as a side effect of code that's already tested and already runs on every real restart; the rebuild path re-reads the persisted flag via the same `_apply_engine_roles()` helper used for restart-persistence. Along the way, #729 also found and fixed 5 of those 13 timers (the setpoint-retry chain and the two post-fan setpoint-verify timers) had never been tracked/cancelled by `cleanup()` at all — a real gap independent of the live-swap problem, since one of them can issue a real `_set_temperature()` call up to 15 minutes after its engine stops being primary. **Known, accepted limitation** (logged at WARNING every time the switch is used, not blocked on): the FSM engine's decision coverage is a strict subset of production's (see the un-mirrored entry-point list earlier in `coordinator.py`), so a decision only the demoted engine's un-mirrored code path made may not fire identically on the newly-primary engine until that coverage gap is separately closed. #729 also added `role=` to the 5 real-command log chokepoints (`_set_hvac_mode`/`_set_temperature`/`_activate_fan`/`_deactivate_fan`/`_notify`) — previously a single shared module logger made production's and shadow's log lines indistinguishable in raw HA log output.

**Follow-up direction (not yet implemented):** `_whf_owns_hvac()` is deliberately named and doc-commented in the code as the seed of a future `FanSession.may_run_hvac()` method. The Issue #392 shaping analysis found that `_natural_vent_active`, `_fan_active`, `_pre_fan_hvac_mode`, and `_fan_override_active` are one concept ("a fan/HVAC-suppression session with an owner and rules") fractured across four loose attributes with no single owner. A `FanSession` class that owns this state and exposes `activate()`/`deactivate()` (idempotent by construction) and `may_run_hvac(mode) -> bool` is tracked as a **separate, deferred follow-up issue** — it is not implemented as part of Issue #392. `_whf_owns_hvac()` and the idempotency guards in §9f below are the small, safe cuts taken now that are consistent with that future direction, without taking on the risk of a full extraction in a bugfix PR.

**Test coverage:** `tests/test_whole_house_fan_hvac_suppression.py`, `tests/test_fan_control.py` — exact function names pending as of this doc pass; both files carry the current coverage for this guard.

### 9a. Fan State Tracking

The coordinator maintains six internal fields to manage fan state across activate/deactivate calls and detect user overrides:

| Field | Type | Purpose |
|---|---|---|
| `_fan_active` | `bool` | Whether the integration currently considers the fan on |
| `_fan_on_since` | `datetime \| None` | Timestamp of when `_activate_fan()` last turned the fan on |
| `_fan_override_active` | `bool` | Whether a user manual fan override is in effect |
| `_fan_override_time` | `datetime \| None` | Timestamp of when the fan override was detected |
| `_fan_command_pending` | `bool` | Set to `True` immediately before the integration issues a fan command; cleared immediately after |
| `_fan_command_time` | `datetime \| None` | Timestamp recorded at the start of every `_activate_fan()` and `_deactivate_fan()` call; used by `_is_recent_fan_command()` as a timestamp-based secondary guard |
| `_pre_fan_hvac_mode` | `str \| None` | **`FAN_MODE_WHOLE_HOUSE` only.** Captures the thermostat's HVAC mode immediately before fan activation (e.g., `"heat_cool"`, `"cool"`). Restored to the thermostat on deactivation, then cleared to `None`. `None` when no whole-house fan session is active or when using `FAN_MODE_HVAC`. Persisted in state across HA restarts so HVAC restoration survives a restart during a fan session. Cleared whenever `_deactivate_fan()` is called with `release_suppression=True` (Issue #618 — see below), independent of whether that call also writes a restored mode right now. |

**`_activate_fan()`** sets `_fan_command_time = dt_util.now()` and `_fan_command_pending = True`, issues the fan-on service call, then sets `_fan_active = True` and records `_fan_on_since`. If `_fan_override_active` is `True` at activation time, the call is skipped so the integration does not fight the user's manual setting. For `FAN_MODE_WHOLE_HOUSE` (or `both`), the current thermostat HVAC mode is captured in `_pre_fan_hvac_mode` and HVAC is set to `off` before the fan is turned on.

**`_deactivate_fan()`** follows the same pattern in reverse: sets `_fan_command_time = dt_util.now()` and `_fan_command_pending = True`, issues the fan-off service call, then clears `_fan_active` and `_fan_on_since`. Override state is not checked on deactivation — the intent is always to stop the fan when the economizer or transition logic calls for it. For `FAN_MODE_WHOLE_HOUSE` (or `both`), HVAC mode is restored from `_pre_fan_hvac_mode` and that field is cleared to `None`, gated by two independent parameters (Issue #618):

- **`restore_hvac`** (existing, pre-#618): whether to *write* the restored mode right now. `False` during nat-vent cycling-off so the session can continue without re-engaging HVAC between cycles, and while `_paused_by_door=True` (Issue #523) so a restore never fires into an open window.
- **`release_suppression`** (new in #618, default tracks `restore_hvac` when not given, matching historical behavior for existing callers): whether this call ends WHF ownership of the thermostat (clears `_pre_fan_hvac_mode`) **independent of whether it also writes a mode right now**. Genuine session-end callers (`_exit_nat_vent()`'s sensor-open branch, `reconcile_fan_on_startup()`'s `no-fan` branch) pass `release_suppression=True` explicitly even when `restore_hvac=False` — don't write into an open window, but don't strand the snapshot either. Mid-session cycling-off (`nat_vent_temperature_check()`) leaves this at its `restore_hvac`-tracking default, since that stranding is intentional (session ongoing, expected to resume).

**Why the split was needed (Issue #618 incident, 2026-08-10):** before this fix, `restore_hvac=False` always skipped clearing `_pre_fan_hvac_mode` too — correct for mid-session cycling, but wrong for a genuine session end that merely couldn't write a mode right now (an open window). `_exit_nat_vent()`'s sensor-open branch and the reconcile `no-fan` branch (when `_paused_by_door=True`) both hit exactly this shape, leaving `_pre_fan_hvac_mode` stranded non-`None` for as long as the pause condition lasted. Because `_whf_owns_hvac()` (§9c below) reads only `_pre_fan_hvac_mode is not None`, `apply_classification()`'s `DEFER_NAT_VENT` gate kept reporting the WHF as still owning the thermostat for **~4.5 hours** after a window closed and the session had genuinely ended, silently blocking the HVAC-mode restore that should have happened the moment `handle_all_doors_windows_closed()` ran. See the Issue #618 tracking issue for the full incident writeup.

**`reconcile_fan_on_startup()`'s `no-fan` branch** (§9e-B, Issue #405) is the second release point for a session that ended via cycling-off and never reactivated — it now also always passes `release_suppression=True` (fixing the same #618 stranding for the case where a stale snapshot survived past a `_paused_by_door` window), and additionally accepts a `recent_hvac_session_ended: bool` parameter (Issue #618): `True` when the reconcile was triggered by a thermostat `hvac_action` transition directly out of `cooling`/`heating` into `fan` — the normal internal post-compressor blower phase, not an out-of-band fan appearance. When `True`, the branch releases any stranded suppression but never writes a restored mode, since a legitimate active/just-finished cooling or heating session must never be interrupted by this reconcile. (2026-08-10 incident: this exact branch force-set HVAC to `off` five minutes after AC had legitimately started cooling, because it misread the blower phase as "the WHF stopped.")

### 9b. Fan Override Detection

Fan override detection runs in two places:

1. **`_async_fan_entity_changed()`** — a state-change listener registered on the `fan_entity` (for `fan_mode == whole_house_fan` or `both`). When the entity state changes, the listener checks whether `_fan_command_pending` is set. If the flag is clear, the state change was user-initiated, not integration-initiated, and a fan override is recorded: `_fan_override_active = True`, `_fan_override_time = utcnow()`.

2. **`_async_thermostat_changed()`** — the existing thermostat state listener is extended to also inspect the thermostat's `fan_mode` attribute (for `fan_mode == hvac_fan` or `both`). If the fan_mode attribute changes while `_fan_command_pending` is clear **and** `_is_recent_fan_command(30.0)` returns `False`, a fan override is recorded using the same fields. The 30-second window is required because cloud-connected thermostats can echo the integration's own `climate.set_fan_mode` call seconds after `_fan_command_pending` has already been cleared (Issue #239).

The same guard applies in **`_async_fan_entity_changed()`** (belt-and-suspenders): `_fan_command_pending` is checked first; `_is_recent_fan_command(30.0)` is checked as the fallback.

#### Compound command-pending guard in `_async_thermostat_changed()` (Issue #205/206)

`_async_thermostat_changed()` contains two override-detection paths: the **normal path** (checks `hvac_mode` / `hvac_action` for HVAC changes) and the **pause-path** (checks for thermostat state changes while `_paused_by_door` is `True`). Both paths share the same suppression guard — before acting on any state change as a user override, the listener checks whether the change was automation-issued by testing:

```python
if self._hvac_command_pending or self._fan_command_pending or self._temp_command_pending:
    return  # change was automation-issued; ignore
```

All three flags must be tested together. Testing only `_hvac_command_pending` is incorrect because **automation sequences frequently call `_deactivate_fan()` before `_set_hvac_mode()`** (for example, natural ventilation exit). In that sequence:

1. `_deactivate_fan()` sets `_fan_command_pending = True` and issues the fan-off service call.
2. The thermostat state listener fires while `_fan_command_pending` is `True` but `_hvac_command_pending` is still `False`.
3. If only `_hvac_command_pending` is checked, the guard is bypassed — the listener misidentifies the automation's own fan-off as a user manual override and starts an unwanted grace period.

The fix (Issue #206) expands the guard at both the pause-path and normal-path detection sites to `_hvac_command_pending OR _fan_command_pending OR _temp_command_pending`. If **any** of the three flags is `True`, the state change is treated as automation-issued and suppressed.

**`_is_recent_hvac_command(threshold_seconds=3.0)`** is a secondary guard that inspects `_hvac_command_time` to catch race conditions where the HVAC flag was already cleared before the listener fired.

**`_is_expected_confirmation` (Issue #269 Bug A):** A third suppression layer for the `fan_mode` attribute-change path specifically. Cloud thermostats (e.g., Nest, Ecobee via cloud polling) sometimes echo a `fan_mode` attribute change as a delayed side-effect of an HVAC mode transition, arriving 30–120 seconds after the original command — outside the 30-second `_is_recent_hvac_command` window. When `_is_expected_confirmation` is `True`, the `fan_mode` change guard suppresses false override detection for up to 120 seconds after the last HVAC command.

**`_is_recent_fan_command(threshold_seconds=30.0)` (Issue #239):** A fourth suppression layer for direct fan service calls. `climate.set_fan_mode` calls do not update `_hvac_command_time`, so `_is_recent_hvac_command()` never fires for fan-mode echoes. This guard reads `_fan_command_time` (set at the start of `_activate_fan()` and `_deactivate_fan()`) and suppresses false overrides within 30 seconds of any fan command.

| Guard | Type | Applies to | Window | Purpose |
|---|---|---|---|---|
| `_hvac_command_pending OR _fan_command_pending OR _temp_command_pending` | Flag check (synchronous) | All command types | Until cleared | Primary: suppresses both paths during any automation-issued command |
| `_is_recent_hvac_command(threshold_seconds=30.0)` | Timestamp check | HVAC mode / setpoint changes | 30 s | Secondary: catches races where the HVAC flag cleared before the HA event arrived |
| `_is_expected_confirmation` | Boolean flag | Fan_mode attribute changes from HVAC mode transitions | 120 s | Tertiary: suppresses delayed fan_mode echoes from HVAC mode changes on cloud thermostats |
| `_is_recent_fan_command(threshold_seconds=30.0)` | Timestamp check | Fan mode changes (`climate.set_fan_mode`) | 30 s | Quaternary: suppresses fan echo races where `_fan_command_pending` cleared before the HA event arrived |

#### `_set_hvac_mode("off")` fan_command_time Guard (Issue #277 Bug A1)

`_set_hvac_mode("off")` includes an internal `set_fan_mode(auto)` assertion that resets the thermostat's fan mode as part of switching HVAC off. This fan-mode call produces a delayed echo on cloud thermostats — the same class of echo suppressed by `_is_recent_fan_command()` elsewhere.

Before Issue #277, this path did not set `_fan_command_time`, so the echo arrived outside the 30-second `_is_recent_fan_command()` window and was misdetected as a user manual fan override, triggering an unwanted grace period.

**Fix:** `_set_hvac_mode("off")` now sets `self._fan_command_time = dt_util.now()` immediately before the `set_fan_mode(auto)` service call. This stamps the command time into the same timestamp the Quaternary guard reads, extending echo suppression to 30 seconds from the HVAC-off command.

**Why here (not in `_activate_fan`/`_deactivate_fan`):** The `set_fan_mode(auto)` inside `_set_hvac_mode("off")` is not a fan activation/deactivation — it is a cleanup step bundled with the HVAC-mode command. It is therefore not routed through `_activate_fan()` or `_deactivate_fan()`, and those helpers' existing `_fan_command_time` stamps do not cover it.

#### Setpoint/Fan Override Mutual Exclusion (Issue #277 Bug B)

A single thermostat event can carry both a setpoint attribute change and a `fan_mode` attribute change simultaneously. Before Issue #277, both Block 2 (setpoint-override detection) and Block 3 (fan-mode override detection) in `_async_thermostat_changed()` evaluated independently — a single physical user action could trigger two simultaneous overrides (setpoint + fan), each starting its own grace timer.

**Fix:** A local boolean `_setpoint_override_detected` is initialized to `False` at the start of the function, before Block 2. If Block 2 fires (a setpoint override is detected and recorded), it sets `_setpoint_override_detected = True`. Block 3's fan-override condition is guarded by `and not _setpoint_override_detected`:

```python
_setpoint_override_detected = False  # initialized before Block 2

# Block 2 — setpoint detection
if <setpoint changed by user>:
    handle_manual_override(...)
    _setpoint_override_detected = True

# Block 3 — fan_mode detection
if <fan_mode changed> and not _setpoint_override_detected:
    handle_fan_manual_override(...)
```

**Invariant:** one thermostat event → at most one override type recorded. If a setpoint change and a fan_mode change arrive in the same event, only the setpoint override fires; the fan_mode change is treated as a correlated side-effect, not a separate user action.

#### Fan Override Detection Diagnostic Logging (Issue #277 Bug H)

When `handle_fan_manual_override()` fires from `_async_thermostat_changed()`, the INFO-level log line now includes the following fields to make false-positive investigations self-contained without requiring a debug log level:

| Field | Meaning |
|---|---|
| `old_fan_mode` | The thermostat's `fan_mode` attribute before the change |
| `new_fan_mode` | The thermostat's `fan_mode` attribute after the change |
| `fan_cmd` age (seconds) | `(now − _fan_command_time).total_seconds()` — time since the last fan command; `None` if `_fan_command_time` is unset |
| `hvac_cmd` age (seconds) | `(now − _hvac_command_time).total_seconds()` — time since the last HVAC command; `None` if unset |
| `expected_confirmation` | Current value of `_is_expected_confirmation` at the moment the override is recorded |

These values make it possible to determine, from the log alone, whether the override was a real user action or a delayed echo that arrived just outside a suppression window.

#### Mode Override Detection — `_last_commanded_hvac_mode` (Issue #269 Bug C)

The normal-path override detection in `_async_thermostat_changed()` compares the thermostat's reported `hvac_mode` against the expected mode. Prior to Issue #269, that comparison was always against `classification.hvac_mode`. For dual-setpoint thermostats, CA commands `heat_cool` mode (§6e), but `classification.hvac_mode` may be `"cool"` or `"heat"`. A user switching from `heat_cool` back to `cool` would evaluate as `"cool" != "cool"` = `False` and go undetected.

The fix replaces the comparison target with `ae._last_commanded_hvac_mode or classification.hvac_mode`:
- When CA has issued a mode command, `_last_commanded_hvac_mode` holds the actual mode sent to the thermostat (e.g., `"heat_cool"`).
- If no command has been issued in this session, it falls back to `classification.hvac_mode`.

This ensures mode overrides are correctly detected regardless of whether the thermostat is single- or dual-setpoint capable.

#### Dual Setpoint Override Detection — `heat_cool` Mode (Issue #269 Bug D)

Setpoint override detection reads the thermostat's temperature attributes to determine whether the user has manually changed a setpoint. When the thermostat is in `heat_cool` mode, `temperature` (the single-setpoint attribute) is `None` — only `target_temp_low` and `target_temp_high` are populated.

The fix gates attribute selection on the current thermostat mode:

| Thermostat mode | Attribute read for setpoint check |
|---|---|
| `heat_cool` | `target_temp_low` and `target_temp_high` |
| `heat`, `cool`, `off`, other | `temperature` (single-setpoint attribute) |

The grace-period trigger in the same block also now compares against `ae._last_commanded_hvac_mode` rather than `classification.hvac_mode`, consistent with the Bug C fix above.

#### `hvac_mode` in Coordinator Data (Issue #269 Bug B)

`hvac_mode` — the thermostat's current operating mode string (`"heat_cool"`, `"cool"`, `"heat"`, `"off"`) — is now included in the coordinator's data dict returned by `_async_update_data()`. `_detect_and_emit_incidents()` reads it from `coordinator.data` to populate incident records with the actual thermostat mode at detection time, rather than deriving it indirectly from other attributes.

**Test coverage:** `tests/test_override_automation_boundary.py` — compound guard invariant.

Fan override is **separate** from HVAC override. The two override states are tracked independently and do not interfere with each other. Fan override uses the same grace period duration as manual HVAC override (`DEFAULT_MANUAL_GRACE_SECONDS`), but the timers run independently.

Fan override **bookkeeping** is cleared at transition points where the integration takes deliberate control of the fan (bedtime, morning wakeup — see Section 9c) — but the *fan itself* is only deactivated when the user wasn't actively overriding it and nat-vent/WHF doesn't currently own HVAC (Issue #498).

### 9c. Fan Behavior at Transitions (updated Issue #498)

| Transition | Fan action | Override bookkeeping cleared? |
|---|---|---|
| Bedtime | `_deactivate_fan()` called **only if** the fan wasn't being actively overridden and nat-vent/WHF doesn't currently own HVAC (`decide_scheduled_band_gate()` != `DEFER_NAT_VENT`); economizer also deactivated in that case | Yes — `_fan_override_active` reset to `False` regardless (via `clear_manual_override()` → `clear_fan_override()`) |
| Morning wakeup | `_deactivate_fan()` called under the same condition as bedtime — this guard was **missing entirely** before Issue #498 (the reported 06:30 production bug: wake-up unconditionally killed a manually-overridden whole-house fan and armed AC) | Yes — same as bedtime |

At bedtime, the fan and economizer are shut down before the bedtime setpoints are applied — but only when there is no active nat-vent/WHF session and no active fan override to respect (Issue #498 deleted bedtime's own bespoke outdoor-vs-sleep_cool comparison; it now just defers to the shared gate). At morning wakeup, the same logic applies before comfort temperatures are restored.

**Capture-before-clear hazard (Issue #498):** both handlers must snapshot `_fan_override_active` into a local variable *before* calling `clear_manual_override()` — that call unconditionally clears the flag as a side effect via `clear_fan_override()`, so reading the live attribute afterward would always see it already cleared, silently defeating the override guard regardless of whether the code checks it correctly. See `docs/grace-periods-spec.md#shared-scheduled-band-gate-issue-498` for the full mechanism.

Clearing the override bookkeeping flag at these transitions means the integration will not skip fan activation during the next economizer cycle just because the user had manually adjusted the fan during the previous day — this bookkeeping reset is independent of whether the fan itself was touched during this same transition.

### 9c-i. Fan-ON and Fan-OFF Decision Table (Issue #359)

This table enumerates the six key fan lifecycle scenarios, including the new `on_fan_turned_off()` handler and `fan_cancel` event type introduced in Issue #359.

| Scenario | Trigger | CA decision | Flags / state change | Event emitted | Test ref |
|---|---|---|---|---|---|
| Fan-ON + nat-vent eligible | User turns fan on; `outdoor < indoor`, sensors open, gate passes | Adopt as nat-vent — do NOT set override | `_fan_active = True`, `_natural_vent_active = True`, `_fan_override_active` stays `False` | `fan_activated` (nat-vent adoption) | `test_fan_control.py` |
| Fan-ON + nat-vent ineligible | User turns fan on; conditions gate does not pass | Manual override — start grace timer. **Issue #495:** for `FAN_MODE_WHOLE_HOUSE`/`BOTH`, also suppresses HVAC (`_suppress_hvac_for_whf()`) — previously only the CA-initiated (`_activate_fan()`) path did this, leaving the AC armed for the life of a manual/remote override | `_fan_override_active = True`, `_fan_override_time = now()`; `_pre_fan_hvac_mode` captured + `set_hvac_mode("off")` for WHF/BOTH | `fan_manual_override` | `test_fan_control.py`, `test_whole_house_fan_hvac_suppression.py::TestManualWhfOnSuppressesHvac` |
| Fan-OFF (user) | User physically turns the fan off (fan_mode → auto) | `on_fan_turned_off()`: clear fan flags, start fan-off grace — **no** `_fan_override_active` set. **Issue #495:** if a WHF suppression session was active (`_pre_fan_hvac_mode is not None`, from either the nat-vent-adopted OR the manual-override case above), release it and reclassify (`_release_whf_and_reclassify()`) rather than leaving HVAC suppressed indefinitely | `_fan_active = False`, `_natural_vent_active = False`, `_fan_override_active = False`; fan-off grace timer starts; `_pre_fan_hvac_mode` released if set | `fan_cancel` | `test_fan_cancel.py`, `test_whole_house_fan_hvac_suppression.py::TestManualWhfOffReleasesAndReclassifies` |
| Fan-OFF + ecobee setpoint echo | Ecobee or cloud thermostat echoes setpoint change within 5 s of fan-off | Setpoint suppressed; re-assertion fires after 5 s delay | `_setpoint_reassert_pending = True`; scheduled callback re-applies commanded setpoint | _(none — suppression is silent)_ | `test_fan_cancel.py` |
| Post-grace reconciliation | Fan-off grace period expires | `reconcile_fan_on_startup()` called; re-evaluate physical state | Adopt fan as nat-vent (eligible) or confirm fan is off (ineligible) | `fan_activated` or _(no event if off)_ | `test_fan_cancel.py`, `test_fan_cancel.py::TestFanCancelCoordinator::test_post_grace_reconcile_whf_archetype_uses_physical_state_not_thermostat_gate` |
| Periodic backstop (`_async_update_data()`) | 30-min coordinator poll fires while fan is `"running (untracked)"` and no override or grace active | Same reconciliation path — adopt-on or turn-off | `_fan_active` and `_natural_vent_active` updated accordingly | `fan_activated` or `fan_deactivated` | `test_fan_cancel.py` |

**Issue #510 fixes to the two rows above:** the post-grace row's `reconcile_fan_on_startup()` call was previously gated by an outer condition computed from the thermostat's own `fan_mode`/`hvac_action` attributes, unconditionally — a permanent no-op for WHF-only installs (fan physically separate from the thermostat), now fixed to use the same archetype-aware helper the inner call already used. The periodic-backstop row's `"running (untracked)"` trigger condition now also fires for a stale `_natural_vent_active` flag disagreeing with confirmed-running physical state, not just the fully-untracked case — see §9e-F.

**Issue #571:** the periodic-backstop row's trigger condition no longer fires in the few seconds right after CA's own fan-off command (nat-vent exit or otherwise) — see §9e-G.

**Key semantic distinction for fan-off grace vs fan-manual-override grace:**
The `fan_off` grace (started by `on_fan_turned_off()`) gates nat-vent **re-activation** — CA backs off from immediately restarting the fan the user just stopped. The `fan_manual_override` grace (started when the user turns a fan on) gates CA **interference** with a fan the user is actively running. The two grace types have inverted blocking semantics. See `docs/grace-periods-spec.md` for the full grace period state machine.

### 9c-ii. WHF Feedback Mode (Issue #361)

`fan_state_feedback` (bool, default `False`) applies **only to the whole house fan** (`fan_entity`).
It has no effect when `fan_mode=hvac_fan` — the HVAC fan is controlled via the thermostat's own
`fan_mode` attribute; there is no separate entity to observe. The Activity Record warning banner and
AI context note only appear when `fan_mode` is `whole_house_fan` or `both` AND `fan_entity` is set.

`fan_state_feedback` controls whether CA reads physical WHF motor state or operates in command-only mode.

| fan_state_feedback | _fan_active (CA wants ON) | grace active | Action |
|---|---|---|---|
| True | True | No | Read physical state via `_get_fan_physical_state()`; command ON if off |
| True | False | No | Read physical state; command OFF if unexpectedly on |
| False | True | No | Command ON idempotently (skip state read); update `_last_commanded_fan_state` |
| False | False | No | Command OFF idempotently (skip state read); update `_last_commanded_fan_state` |
| False | True | Yes | No command — grace gates re-activation even without feedback |
| False | False | Yes | No command — grace prevents turn-on |

**Idempotency**: Commands are only re-issued when `_fan_active` (desired) diverges from
`_last_commanded_fan_state` (last issued command). This prevents command churn on every 30-min cycle.

**Override detection**: `_async_fan_entity_changed()` is suppressed when `fan_state_feedback=False`
(it only fires on CA's own command echo, not on physical user overrides). Wall-switch overrides are
undetectable without a state sensor.

**`_compute_fan_status()`** reads thermostat climate entity attributes (HVAC fan) — NOT the WHF entity.
WHF operational status is tracked separately via coordinator data fields:
- `whf_mode`: `"command-only"` | `"state-feedback"` | `"disabled"`
- `whf_last_commanded`: `"on"` | `"off"` | `None`
- `whf_desired`: `True` | `False` | `None`

**Auto-flip**: When `fan_state_entity` is configured in the options flow, `fan_state_feedback`
is auto-suggested as `True` (user can override).

### 9d. Fan Status Sensor Values

The `sensor.climate_advisor_fan_status` entity exposes one of seven state strings:

| Sensor state | Meaning |
|---|---|
| `disabled` | Fan control is not configured (`fan_mode = disabled`) |
| `inactive` | Fan is off; integration is in control |
| `active` | Fan is on; integration activated it (nat-vent or economizer); physical state confirmed for WHF |
| `active (unconfirmed)` | CA flag `_fan_active=True` but WHF physical state reads off — a stale flag left over after a manual stop. Logged at `WARNING` (Issue #374). `_reconcile_fan_physical_drift()` (the 5-min backstop) self-corrects the underlying `_fan_active` flag within 2 ticks (~10 min, Issue #423); since Issue #510 the *displayed* value resolves faster than that — it is only shown within the transient ~30s post-command window (`_is_recent_fan_command()`), and returns `"inactive"` directly past that window regardless of whether the backstop has ticked yet (ground truth wins). |
| `running (manual override)` | Fan is physically on under manual override. Two sub-cases: (a) `_fan_active=True` (CA-owned flag set) — CA has a record of activating it; (b) `_fan_active=False` but physical state is on — user-owned run, CA recorded the override but did not adopt it as nat-vent. Both sub-cases report the same sensor value. *(Issue #365)* |
| `off (manual override)` | `_fan_override_active=True` and `_fan_active=False` and physical state is off (or fan_mode is not WHF/BOTH). Override still in effect but the fan has been turned off before grace expired. *(Issue #365)* |
| `running (untracked)` | Fan is physically running but `_fan_active=False`. Detection path depends on fan mode: **HVAC/Both** — thermostat reports `fan_mode=on` or `hvac_action=fan`; **WHF/Both** — `_get_fan_physical_state()` reads `fan_state_entity` (Type 2) or `fan_entity` (Type 1). Typical after HA restart or user-initiated run from thermostat/wall switch. Returns `"inactive"` instead when `fan_state_feedback=False` (command-only mode, no physical feedback sensor), or when the physical signal simply hasn't caught up yet to CA's own very-recent off-command (`_is_recent_fan_command(threshold_seconds=30.0)` — Issue #571, §9e-G). *(WHF fallback added Issue #363.)* |

The sensor also exposes these attributes:
- `fan_runtime_minutes` — minutes since the integration last activated the fan (0.0 when inactive or in override)
- `fan_override_since` — ISO timestamp of when the manual override was detected (`null` when no override is active)
- `fan_running` — boolean; `true` when the fan is physically running regardless of who controls it

**HVAC-off + fan-on (fan-only circulation):** When the economizer enters the maintain phase, HVAC mode is set to `off` but `climate.set_fan_mode: on` is called separately. This is the intended "fan-only circulation" mode — most thermostats support running the fan for air circulation independently of heating or cooling. A `DEBUG`-level log entry is emitted whenever the integration activates the HVAC fan while the thermostat reports `hvac_mode = off`.

**`running (untracked)` after Issues #327 and #347:** `"running (untracked)"` is expected only as a brief transient in two cases: (1) between HA startup and the completion of `_do_startup_coalesce`; (2) between when the thermostat reports `hvac_action="fan"` mid-session and when `_async_thermostat_changed` calls `reconcile_fan_on_startup` to resolve it. In both cases any fan still running is either adopted as CA nat-vent or turned off — there is no persistent untracked limbo. A `"running (untracked)"` state that persists beyond these moments signals a coordinator setup failure (case 1) or a code-path regression (case 2). See `Fan reconcile:` log lines.

### 9e. Thermostatic Fan Loop and Startup Reconciliation (Issue #327)

#### The Principle: a Running Fan Always Has an Owner

Prior to Issue #327, four code paths could leave a fan running indefinitely with no CA owner and no shutdown mechanism:

1. `_compute_fan_status()` returned `"running (untracked)"` but no code path acted on it — the string was used only to suppress unrelated warnings.
2. Every shutdown path was gated on ownership (`_deactivate_fan()` requires `_fan_active=True`; nat-vent exit requires `_natural_vent_active=True`), so an unowned fan could never be turned off.
3. `restore_state()` on restart preserved `_fan_override_active=True` without rescheduling the grace-period expiry timer, leaving the override permanent and both `_activate_fan()` and `_deactivate_fan()` permanently skipped.
4. The only fast-loop temperature check ran on nat-vent only; the outdoor sensor had no state listener, so an outdoor temperature rise was invisible until the next 30-minute coordinator poll.

The occupant experienced this as: a fan running through the night while outdoor air was warmer than indoor — actively heating the house — with no automatic correction.

**Issue #327 enforces the invariant:** while the fan feature is enabled, a running fan is always one of:
- **CA nat-vent** — activated by `_activate_fan()`, held by the fast thermostatic loop (§9e below), exits the loop on `outdoor ≥ indoor`, comfort floor, or target reached.
- **Timed manual override** — detected by `_async_fan_entity_changed()` or `_async_thermostat_changed()`, reclaimed when the grace timer expires **or** on the next HA restart.
- **Off** — the default state when neither condition holds.

There is no fourth state. Any post-coalesce `fan_mode="on"` or fan-entity change that CA did not command is detected as a manual override (§9b) → timed, not indefinite. A post-coalesce `hvac_action="fan"` (thermostat-autonomous fan-on between AC cycles) is reconciled by `reconcile_fan_on_startup` via the post-startup detection path (Issue #347) → adopt-on or turn-off, never indefinite limbo.

"Post-coalesce" is enforced by `_suppress_during_startup_coalescing()` (Issue #491), a
single shared guard called from `_async_thermostat_changed()`, `_async_fan_entity_changed()`,
and `_async_fan_remote_changed()` — every listener capable of detecting a manual override
bails out while `_startup_coalesce_active` is True. Before #491, the two fan listeners had
no such guard, so a device re-announcing its last retained state during HA's restart/
reconnect sequence (confirmed for the QuietCool RF remote's `event.*` entity) could be
misread as a fresh manual override within the coalescing window, contradicting this
section's claim. Verify this claim against `_suppress_during_startup_coalescing()`'s call
sites directly if it is ever suspected of drifting again.

#### A. Restart = Clean Fan Slate

`restore_state()` now clears `_fan_override_active` and `_fan_override_time` on restart, matching the clean-slate treatment of HVAC override/grace state (§11). Fan ownership is fully reconsidered by the coalesce reconciliation step rather than reconstructed from stale persisted flags.

`_fan_active` and `_pre_fan_hvac_mode` are still preserved as hints for reconciliation, but their values do not gate any action — the reconcile step re-derives the correct decision from the live thermostat state.

#### B. Startup Coalesce: `reconcile_fan_on_startup`

After the existing nat-vent / `apply_classification` logic in `_do_startup_coalesce`, a dedicated fan reconciliation step reads a "physical fan running?" ground-truth signal and decides:

**Issue #423 — the ground-truth signal is now archetype-aware, not always the thermostat's attributes.** Prior to Issue #423, all 4 callers of `reconcile_fan_on_startup()` (this startup-coalesce path, the 30-min periodic backstop, the Issue #347 one-shot runtime trigger, and the post-grace-expiry reconcile) derived "is a fan running" purely from the thermostat's own `fan_mode`/`hvac_action` attributes — correct for `FAN_MODE_HVAC` (the thermostat's own blower IS the fan) but wrong for `FAN_MODE_WHOLE_HOUSE` (a physically separate switch/relay). A thermostat-internal fan-schedule blip (unrelated to the configured WHF entity) could cause reconcile to "adopt" a whole-house-fan session that was never physically turned on, permanently wedging `_fan_active=True` with no physical entity state ever changing to trigger the normal `_async_fan_entity_changed()` self-correction — a real production incident (a WHF stayed adopted-but-off for 3.5+ hours overnight while the dashboard showed `"active (unconfirmed)"`).

All 4 callers now derive this signal via `coordinator._derive_thermostat_fan_running_for_reconcile(fan_mode_attr, hvac_action_attr)`: `FAN_MODE_HVAC` still trusts the thermostat's attributes; `FAN_MODE_WHOLE_HOUSE` uses `_get_fan_physical_state()` (the real configured WHF entity) when `fan_state_feedback` is enabled, falling back to the thermostat signal only in command-only mode (no independent ground truth exists there). `FAN_MODE_BOTH` ORs both signals — a strict superset of the prior (wrong) behavior, not a true per-device model; representing "WHF off, HVAC blower on" independently is a known, tracked gap (see the follow-up issue referenced at the end of this section).

| Physical fan running? | Nat-vent eligible? | Decision | Action |
|---|---|---|---|
| No (and no fan command in the last 30s) | — | **no-fan** | `_fan_active`/`_fan_on_since`/`_natural_vent_active` cleared, then `_deactivate_fan(restore_hvac=not self._paused_by_door and not recent_hvac_session_ended, release_suppression=True)` is called to release any stranded WHF HVAC suppression (Issue #405, extended by Issue #618) |
| No, but a fan command was issued in the last 30s | — | **defer** (Issue #733) | No flags touched, no `_deactivate_fan()` call — the ground-truth read is stale/structurally-blind relative to a command CA itself just issued in this same pass; see below |
| Yes | Yes (`outdoor < indoor`, gate passes, sensors open) | **adopt-on** | `_fan_active = True`, `_natural_vent_active = True`; fast thermostatic loop started |
| Yes | No | **turn-off** | `_deactivate_fan()` or `set_fan_mode("auto")` (FAN_MODE_HVAC) / fan `turn_off` + HVAC restore (FAN_MODE_WHOLE_HOUSE) |

**Issue #405 — the `no-fan` path is a second `_pre_fan_hvac_mode` release point, not just a flag-clear.** A WHF nat-vent session can end via cycling-off (`nat_vent_temperature_check()` calling `_deactivate_fan(restore_hvac=False)` by design — see the `_pre_fan_hvac_mode` row above), which intentionally leaves `_pre_fan_hvac_mode` set so the session can resume. If the fan never reactivates and a later coalesce boundary observes it confirmed off, the `no-fan` branch must still release that suppression — otherwise `_pre_fan_hvac_mode` stays stranded non-`None` forever and `_whf_owns_hvac()` permanently blocks every future HVAC write with no recovery path.

**Issue #618 — `release_suppression=True` closes a second stranding path #405 missed.** Before #618, this branch called `_deactivate_fan(restore_hvac=not self._paused_by_door)` with no `release_suppression` argument, which defaulted to tracking `restore_hvac`. When `_paused_by_door` was `True` at the moment this fired (a window still open), `restore_hvac=False` meant the old code *also* skipped clearing `_pre_fan_hvac_mode` — silently reproducing the exact #405 stranding this branch exists to fix, just gated behind a door-pause instead of a restart. Passing `release_suppression=True` unconditionally here (independent of `restore_hvac`) means the snapshot is always released at this genuine session-end point — `thermostat_fan_running` is `False`, so ownership should never survive this call — while `restore_hvac` still correctly gates whether a mode gets *written* right now. The same call also gained `recent_hvac_session_ended`: `True` when this reconcile was triggered by a thermostat `hvac_action` transition directly out of `cooling`/`heating` into `fan` (the normal post-compressor blower phase), blocking the mode write so an active/just-finished cooling or heating session is never interrupted (the 2026-08-10 incident this issue traces to).

**Issue #790 — `adopt-on`'s eligibility gate now honors the reactivation lockout, and `turn-off` now arms it, for all 4 call sites.** Before this fix, `_reconcile_fan_on_startup_locked()` hardcoded `paused_by_door=False` into the FSM inputs feeding the `adopt-on`/`turn-off` decision above (this section's own table), on the claim (`nat_vent_reactivation_lockout.py`'s docstring) that the function "runs at most once per restart/30-min backstop, structurally incapable of sub-minute repeats." That claim was false for 2 of the 4 real callers — the `thermostat_state_change` (Issue #347, this section) and post-grace-expiry sites are event-driven, not cadence-bound, and can fire sub-minute. The hardcoded `False` meant the reactivation lockout (§ armed by every `_exit_nat_vent()` exit reason above `is_reactivation_locked_out()`) was structurally unreachable from this decision point, for any trigger. A symmetric gap existed on the `turn-off` branch's own `_exit_nat_vent()` call, which never passed `set_outdoor_exit_time=True` — so a turn-off issued from this call site left no lockout timer for a subsequent trigger to check, even after the check-side fix. Both are fixed: the eligibility check now reads `self._paused_by_door`'s real value, and the `turn-off` branch now arms the lockout like every other exit reason. Safe uniformly across all 4 triggers — including the 2 cadence-bound ones (`ha_restart`, `backstop_30min`) — because `_nat_vent_outdoor_exit_time` is never persisted across HA restarts (`state.py`), so the lockout can only fire when a real exit was armed earlier in the same running process. See `tools/simulations/pending/issue_790_reconcile_startup_bypasses_lockout.json` for the regression scenario.

The 5-minute `_first_run` coalesce window already suppresses override detection (coordinator's `_async_thermostat_changed` override guard), so the turn-off command is not misread as a user manual action.

**Issue #627 — the `backstop_30min` call site (below) was NOT covered by that suppression window, and that gap caused a real AC/whole-house-fan mutex violation.** `restore_state()`'s Issue #263/#327 clean-slate wipes `_fan_override_active` on every restart but correctly *preserves* `_pre_fan_hvac_mode` — the flag `_whf_owns_hvac()` actually depends on to keep HVAC suppressed. The coordinator's periodic `backstop_30min` "untracked fan" reconcile (a separate call site from `_do_startup_coalesce`'s own `reconcile_fan_on_startup(trigger="ha_restart")`, described below) used *only* `_fan_override_active` as its gate — not `_startup_coalesce_active`, unlike every sibling override-detection check in `coordinator.py` (the same "Startup coalescing active — suppressing X detection" idiom quoted throughout this section). It fired on the very first `_async_update_data()` cycle after restart, before the 300s coalescing window had any chance to settle, using the exact flag clean-slate had just zeroed. On a live install (2026-08-11), this misread a whole-house fan still legitimately running under a pre-restart RF-remote timer as "unwarranted," turned it off, and released `_pre_fan_hvac_mode` via `_deactivate_fan()` — which then let the next `apply_classification()` cycle commit the thermostat to Cool mode ~34 seconds later with nothing left to stop it. The fix: `coordinator._should_run_untracked_fan_backstop()` now also requires `not self._startup_coalesce_active`, so this call site is delayed until the same coalescing boundary `_do_startup_coalesce()`'s own `reconcile_fan_on_startup(trigger="ha_restart")` call already respects — no change to the clean-slate policy itself, the nat-vent-eligibility test, or the WHF/HVAC mutex's single-choke-point design (`_whf_owns_hvac()`). See `tools/simulations/pending/restart_whf_ac_mutex_backstop_race.json` for the regression scenario (uses the harness's new `simulate_restart`/`fan_backstop_tick` event types, added in the same PR, to drive the real production continuity boundary rather than a mirror of it).

**Issue #733 — the `no-fan` branch had no guard against clobbering a nat-vent session `_do_startup_coalesce` itself had just activated in the same pass, and the resulting flag-clear also orphaned the thermostatic backstop timer for the rest of the session.** `_do_startup_coalesce()` calls `handle_door_window_open()` (which can activate nat-vent via `_activate_fan()`, arming the 5-minute self-rescheduling thermostatic backstop timer) and then, unconditionally, `reconcile_fan_on_startup()` — using a "physical fan running?" read taken a few lines later in the same synchronous pass. For a real hardware WHF relay/RF device, that read can lag the command that was just issued; for a command-only archetype (no `fan_state_feedback`), the fallback signal (the thermostat's own `fan_mode`/`hvac_action` attributes) is *never* touched by a WHF command at all, so it reads `False` deterministically right after every WHF activation, not just under timing pressure. Before this fix, either case took the `no-fan` branch and force-cleared `_fan_active`/`_natural_vent_active`/`_nat_vent_soft_start` one line after they were correctly set — the physical fan kept running with no software oversight, and because `_fan_active` was already `False` by the time `_deactivate_fan()` ran, its "already inactive" early-return path (see `_pre_fan_hvac_mode` release logic above) never reached the backstop-timer cancellation the full deactivation path performs — the timer nat-vent's activation had just armed was left running but orphaned, ticked once, no-opped (`nat_vent_temperature_check()` exits immediately once `_natural_vent_active` is `False`), and never rescheduled again. A live incident (2026-08-22) lost nat-vent thermostatic control for ~7 hours overnight this way, drifting 5°F below the comfort floor before the next morning's classification cycle caught it.

The fix has two parts: (1) `_reconcile_fan_on_startup_locked()`'s `no-fan` branch now checks `self._is_recent_fan_command_callback(threshold_seconds=30.0)` — the same recent-command guard `_reconcile_fan_physical_drift()` (§9e, below) already applies to the identical class of stale-read-vs-fresh-command race — before clobbering the flags, and defers to the fresh command instead when a recent one exists; and (2) `_deactivate_fan()`'s "already inactive" early-return path now unconditionally calls `_cancel_fan_thermo_backstop()` (a safe no-op when nothing is scheduled), so a backstop timer can never outlive `_fan_active` reading `False` regardless of which code path cleared it. See `tools/simulations/pending/733_restart_reconcile_clobbers_fresh_nat_vent.json` for the regression scenario (command-only WHF archetype, chosen because it reproduces this defect deterministically within the harness's fidelity model rather than depending on hardware-timing simulation the harness doesn't have).

**Observability (startup validation):** the reconcile step emits one INFO line at the end of `_do_startup_coalesce`:

```
Fan reconcile: thermostat fan_mode=<x> hvac_action=<y> nat_vent_eligible=<bool> decision=<adopt-on|turn-off|no-fan> archetype=<mode>
```

This is the primary grep target for post-deploy validation: `python tools/ha_logs.py --filter "Fan reconcile"`. It confirms that the new behavior ran and what decision was made for the current physical state.

**Reentrancy guard (Issue #561):** `reconcile_fan_on_startup()` is called from 4 independent
sites (startup coalesce, the 30-min untracked-fan backstop, a live thermostat state-change, and
every grace-period expiry) with no coordination between them. Two overlapping calls previously
could each independently reach the `adopt-on` branch and each call
`_start_fan_thermo_backstop()` — which only tracks a single live timer handle via
`self._fan_thermo_cancel`, so whichever chain started first became permanently uncancellable
the moment the second started, leaving two self-rescheduling 5-minute backstop chains ticking
in parallel indefinitely (the root cause of Issue #561's reported incident: one of those
duplicate chains kept re-evaluating nat-vent cycling long after the legitimate session should
have ended). `self._reconcile_fan_in_progress` (a plain bool, not the shared
`self._decision_lock` — some of the 4 call sites may already hold that lock) now guards the
method's body (moved into `_reconcile_fan_on_startup_locked()`): a concurrent call simply skips
its tick and logs at DEBUG, rather than double-processing. As defense-in-depth,
`_start_fan_thermo_backstop()`/its tick callback also carry a `self._fan_thermo_generation`
counter — a tick whose stamped generation no longer matches the current counter belongs to a
superseded chain and self-terminates instead of rescheduling, so even if a duplicate chain were
ever started by some other future gap, only the most-recently-started one survives past its
next tick.

**Command-provenance recency set (Issue #561, hardens Issue #482):** the duplicate-chain defect
above produced two overlapping `_activate_fan()` calls, and `_call_fan_service_with_context()`'s
provenance mechanism (Issue #482) previously recorded only the single most-recently-issued
command context in `self._fan_command_context_id` — a second overlapping command could
overwrite the first's id before `coordinator._async_fan_entity_changed()` evaluated the first
command's resulting state-changed event against it, causing CA's own action to be misattributed
as a manual override (the "whole-house fan manually turned on" message reported in Issue #561).
`self._recent_fan_command_context_ids` is now a 30-second recency list instead of a scalar,
written by `_record_fan_command_context()` and checked via
`fan_command_context_matches(event_context_id, event_context_parent_id)` — either of two
overlapping commands' contexts can still be matched, regardless of which one the coordinator's
listener happens to see first.

**Listener registration observability:** at coordinator setup, one INFO line is emitted:

```
Fan control: watching indoor=<entity> outdoor=<entity> thermostat=<entity> for thermostatic re-eval
```

#### C. Thermostatic Fast Loop: `fan_thermostat_check`

`fan_thermostat_check(indoor, outdoor, trigger)` on `AutomationEngine` is the fast decision point for any CA-owned running fan. It generalizes the existing `nat_vent_temperature_check` — which ran only for nat-vent sessions — to cover any fan that `_fan_active=True`.

**Exit conditions evaluated on every call (priority order):**

| Priority | Condition | Action |
|---|---|---|
| 1 | `outdoor >= indoor` (using existing 1°F hysteresis for re-activation, equality kills) | Fan off; emit `nat_vent_outdoor_rise_exit` if nat-vent session; otherwise deactivate cleanly |
| 2 | `indoor <= comfort_heat` (comfort floor) | Exit nat-vent session; restore heat mode at `comfort_heat` |
| 3 | `outdoor > comfort_cool + nat_vent_delta` (ceiling exceeded) | Fan off; enter paused state |

If no exit condition fires, the fan continues running. The check is cheap and idempotent — frequent calls are safe.

**Trigger sources (all three active whenever the fan is CA-owned and running):**

| Source | Mechanism | Registered in |
|---|---|---|
| Indoor temperature change via thermostat | Existing `_async_thermostat_changed` dispatch → `fan_thermostat_check(trigger="indoor")` | coordinator.py (existing seam, extended) |
| Indoor temperature change via dedicated sensor | New state listener on `indoor_temp_entity` → `fan_thermostat_check(trigger="indoor")` | coordinator.py (new listener added by Issue #327) |
| Outdoor temperature change | New state listener on `outdoor_temp_entity` → `fan_thermostat_check(trigger="outdoor")` | coordinator.py (new listener — outdoor had no listener before Issue #327) |
| Backstop timer | Self-rescheduling timer started in `_activate_fan()`, cancelled in `_deactivate_fan()` and `cleanup()`; reuses the `_fan_min_cycle_cancel` pattern | automation.py |

The backstop timer catches sensors that update slowly or infrequently. The trigger name is passed through to observability logging.

**Observability (per-check):** `DEBUG` on every call:

```
Fan thermostat check: trigger=<indoor|outdoor|tick|timer> indoor=<t> outdoor=<t> active=<bool> decision=<keep|stop:reason>
```

#### D. Economizer Free-Cooling-Direction Guard

`check_window_cooling_opportunity()` (§8) now includes `outdoor < indoor` as an explicit eligibility condition, mirroring the guard already present in nat-vent activation (§17). This prevents the economizer from starting the fan on a hot evening when outdoor air is warmer than indoor — a condition that actively heats the house instead of cooling it.

The guard is a strict precondition: if `outdoor >= indoor`, the fan is not activated regardless of whether the time-window and temperature-ceiling conditions are met.

#### E. Physical-Drift Self-Healing (Issue #423)

Fixing the ground-truth signal (§B above) prevents the *reconcile* mechanism from wedging `_fan_active` on a WHF config, but it doesn't address a stale flag from any other future cause — and before Issue #423, nothing ever corrected `_fan_active` once it disagreed with physical reality. `_compute_fan_status()`/`_compute_whf_status()` (coordinator.py) already compared `_fan_active` against `_get_fan_physical_state()`, but only to render `"active (unconfirmed)"` in the dashboard — never to correct the flag. Three other mechanisms that might seem like they'd catch this were traced and confirmed NOT to: `_fan_running` (`_fan_active or _natural_vent_active`) is pure internal state; `fan_thermostat_check()`'s backstop only evaluates STOP conditions once it believes a fan is running, never independently re-verifies; and `nat_vent_temperature_check()`'s cycling-on branch requires `not self._fan_active` to fire, so a stuck-`True` flag blocks it from ever re-issuing a turn-on command.

`AutomationEngine._reconcile_fan_physical_drift()` closes this gap. It runs at the start of every `_thermo_backstop_task()` tick (the 5-minute self-rescheduling timer, §C above) — the only mechanism guaranteed to fire independent of external state-change events, which is exactly what was missing when the incident's physical WHF state never changed and so never triggered `_async_fan_entity_changed()`.

- No-ops immediately for `FAN_MODE_HVAC` (no separate physical entity to drift from) and for command-only mode (`_get_fan_physical_state()` returns `None` — no ground truth to compare against).
- For `FAN_MODE_WHOLE_HOUSE`/`FAN_MODE_BOTH` with feedback enabled: compares `_fan_active` against the real physical state. Skips if a CA fan command was issued in the last 30 seconds (`_is_recent_fan_command()`, the same echo/lag guard `_async_fan_entity_changed()` already uses). Requires the disagreement to persist across **2 consecutive ticks** (~10 minutes) before correcting, so a single transient sensor flap doesn't trigger a correct-then-immediately-re-adopt cycle.
- On confirmed drift, clears `_fan_active`/`_fan_on_since` via `_clear_fan_flags_and_start_grace(preserve_nat_vent_session=...)` — a helper shared with (and extracted from) `on_fan_turned_off()`. The nat-vent session (`_natural_vent_active`) is **preserved only if `_any_monitored_sensor_open()` confirms a sensor is still open** (Issue #561) — before this check existed, the session was preserved unconditionally, which could leave `_natural_vent_active=True` for hours after windows genuinely closed (no log line exists for "session preserved, no action taken" while indoor sits between the cycling thresholds), until temperature happened to cross `on_threshold` again and reactivated the fan against a sealed house — the root cause of Issue #561's reported incident. When the session is force-closed here, `_release_whf_and_reclassify()` is also called (mirroring `on_fan_turned_off()`'s genuine fan-off sequence) so any WHF HVAC suppression doesn't strand. When a sensor genuinely is still open, preserving the session lets the immediately-following `nat_vent_temperature_check()` call in the same `_thermo_backstop_task()` tick re-fire `_activate_fan()` — the real HA service call — if conditions still warrant it, closing the loop end-to-end within the same tick the drift is confirmed.
- Uses a distinct event `trigger` (`"physical_drift_correction"`, not `"fan_off"`) so Activity Report consumers can tell a self-correction apart from a genuine user action.

**Observability:** `INFO` on the first (unconfirmed) drift tick, `WARNING` with `"self-correcting stale flag (Issue #423)"` on the confirmed (second) tick — the grep target for post-deploy validation. Since Issue #510 (§F below), `"active (unconfirmed)"` itself now also settles to `"inactive"` on the *display* side once the transient post-command window passes, independent of whether this 5-minute backstop has ticked yet — the two mechanisms are complementary, not redundant: this backstop corrects `_fan_active` (the automation-decision flag); §F corrects only what's *displayed* while `_fan_active` is still catching up.

**Known gap, tracked separately:** `FAN_MODE_BOTH`'s OR-based signal (§B above) cannot represent two independent devices in different states. A proper per-device redesign (e.g. separate `whf_running`/`hvac_fan_running` signals with independent adopt/turn-off decisions) is out of scope for Issue #423 and tracked in a follow-up issue.

#### F. Display Ground-Truth Priority + Settled-State Resolution (Issue #510)

A live incident showed the dashboard displaying `"nat-vent (session active, fan idle)"` for hours while the whole-house fan was genuinely, physically running (confirmed via `input_boolean` power-detect entity history), and separately the `"active (unconfirmed)"` WARNING recurring 138 times over 24+ hours instead of resolving per §E's ~10-minute guarantee. Root-caused to three independent defects, all fixed together:

1. **Event-driven refresh was scoped too narrowly.** `_async_fan_entity_changed()` only called `async_request_refresh()` from inside its `_fan_override_active` branch — a genuine physical transition with NO override active (this incident's exact state) never prompted a recompute at all, leaving the display stale until the next 30-minute poll. Fixed by hoisting the refresh to fire unconditionally on every genuine transition, mirroring the existing Issue #489 door/window pattern exactly. The override-active branch no longer issues its own (now-redundant) refresh — it only skips re-running override *detection*, since the display refresh already happened.
2. **`_compute_fan_status()`/`_compute_whf_status()` checked `_natural_vent_active` before consulting physical ground truth.** The nat-vent-session branch returned `"nat-vent (session active, fan idle)"` unconditionally, two branches before the existing `_get_fan_physical_state()` fallback ever ran — a stale session flag masked a confirmed-running fan even though the ground-truth check existed in the same function. Fixed by reading physical state once, up front (via a lazily-memoized closure that preserves the pre-existing property that the `_fan_override_active`+`_fan_active` fast path never triggers a physical-state read at all — an existing, tested invariant), and consulting it inside the nat-vent branch too. When it reads `True`, the function now returns `"running (untracked)"` (the existing value, not a new one) instead of trusting the stale flag. **Direct consequence, no additional code needed:** the pre-existing "Issue #359 Fix D" periodic untracked-fan backstop (§ periodic-backstop table entry above; `_async_update_data_impl`, 30-min cadence) derives its trigger condition from this same `_compute_fan_status()` return value — so it now automatically also self-corrects the stale-nat-vent-flag direction, on top of the plain-untracked-fan case it already covered.
3. **`"active (unconfirmed)"` had no time-based resolution of its own.** It correctly meant "transient, right after a CA command" in the common case, but nothing distinguished that from "settled, genuinely stale" — so it could read as `"active (unconfirmed)"` indefinitely even once §E's backstop should have long since corrected the underlying flag (a display symptom, since the flag itself does get corrected by §E within ~10 min — but the settled disagreement window between "fan turned off" and "§E's next confirming tick" could still show the misleading transient-sounding label). Fixed by reusing `_is_recent_fan_command(threshold_seconds=30.0)` (already used a few lines away, for the same "was this CA's own command" purpose): within 30s of a CA command, still `"active (unconfirmed)"` (correctly transient); past that window, `"inactive"` — ground truth wins for display, with the underlying WARNING log preserved for diagnosis.

Also fixed in the same investigation: `_async_post_grace_fan_reconcile()`'s outer gate (§ post-grace table entry) was computed from the thermostat's own `fan_mode`/`hvac_action` attributes unconditionally — correct for `FAN_MODE_HVAC` but silently a permanent no-op for WHF-only installs (the fan is a physically separate device; the thermostat's own attributes normally show no activity at all). Fixed by using the same archetype-aware `_derive_thermostat_fan_running_for_reconcile()` helper already computed (but previously only passed as a call argument, never consulted for the gate itself) two lines below it.

**Observability:** a new `INFO` line fires whenever the nat-vent-stale-flag correction (item 2) actually applies, giving direct visibility into how often the underlying `_natural_vent_active` staleness occurs in practice.

#### G. Ground-Truth Fallback OFF-Direction Guard (Issue #571)

Occupant impact: a legitimate nat-vent exit — CA turning its own fan off — was being misread every single cycle as an external/untracked fan and force-corrected by the `backstop_30min` reconcile, producing a "Fan running (untracked) -- thermostat fan schedule/circulation" + "fan found running without a CA-owned session" pair in the Activity Report moments after CA's own clean exit, repeating every 5-9 minutes for hours.

§F item 3 above fixed the **ON**-direction settling (`"active (unconfirmed)"` → `"inactive"`) for a *just-commanded-on* fan whose physical confirmation is still pending. The three ground-truth fallbacks that return `"running (untracked)"` (`_compute_fan_status()`'s WHF and HVAC/BOTH branches, `_compute_whf_status()`, and — new as of this issue — `_compute_hvac_fan_status()`) had no symmetric **OFF**-direction guard: once CA cleared its own ownership flags and commanded the fan off, but the physical entity/thermostat attribute hadn't caught up yet, the fallback unconditionally read as an externally-owned fan. Since `_is_untracked` (periodic-backstop table entry above) is derived directly from `_compute_fan_status()`'s return value, this also spuriously triggered `reconcile_fan_on_startup()` moments after every legitimate exit.

Fixed by extracting the shared decision into `fan_status.py::resolve_untracked_fan_status(recent_fan_command: bool)` — `"inactive"` within `_is_recent_fan_command(threshold_seconds=30.0)`'s window, `"running (untracked)"` otherwise — and routing all four ground-truth fallback sites through it, rather than patching each site's `if physical_on/thermostat_signal: ... else: return "running (untracked)"` block independently (these functions have needed synchronized parallel fixes before — §F item 2 touched two of the same three).

**`_compute_hvac_fan_status()` also gained the ON-direction guard for the first time** (it never had §F's item-3 equivalent): previously `if ae._fan_active: return "active"` unconditionally, with no physical/thermostat cross-check at all, unlike its two siblings. Now mirrors `_compute_whf_status()`'s ON-direction shape exactly, including the stale-flag WARNING. This only affects `FAN_MODE_HVAC`/`FAN_MODE_BOTH` installs and only the `hvac_fan_status` dashboard/API display field — no automation-decision code reads it.

#### E. Manual Override = Timed, Not Indefinite

With (A) restart clearing `_fan_override_active` and (B) coalesce reconciling the physical state, every post-restart fan-on that CA did not command is fresh — detected as a new manual override by `_async_fan_entity_changed()` or the `fan_mode` block of `_async_thermostat_changed()`, and reclaimed when the grace timer expires. There is no path from a user action to a permanent, unreclaimed override.

**Override lifecycle observability (INFO):**
- Override set: logged by `handle_fan_manual_override()` with `old_fan_mode`, `new_fan_mode`, `fan_cmd` age, `hvac_cmd` age, `expected_confirmation` (§9b Fan Override Detection Diagnostic Logging).
- Grace expiry reclaim: logged by `_on_grace_expired` / `clear_fan_override`.
- Restart clean-slate: logged by `restore_state()` — `"Fan override cleared on restart (clean slate)"` when `_fan_override_active` was `True` at restore time.

#### Interaction with §11 Clean-Slate Restart Policy

The fan clean-slate introduced in (A) is consistent with §11: `_fan_override_active` joins `_manual_override_active`, `_grace_active`, and `_override_confirm_pending` as fields that are always cleared on restart. The coalesce step (B) performs the same role for fan state that the `_first_run` startup override check (§11 Startup Override Logic) performs for HVAC state — it re-derives the correct ownership decision from live conditions rather than trusting stale persisted flags.

| Field | Preserved across restart? | Notes |
|---|---|---|
| `_fan_active` | **Hint only** | Cleared or overwritten by coalesce reconciliation; does not gate any action on its own |
| `_fan_override_active` | **No** (Issue #327) | Cleared on restart — clean slate; coalesce re-derives from live state |
| `_fan_override_time` | **No** (Issue #327) | Cleared on restart |
| `_pre_fan_hvac_mode` | Yes | Still preserved — needed if coalesce decides to turn off a whole-house fan and restore the HVAC mode |
| `_natural_vent_active` | No | Cleared on restart (was already the case); coalesce adopt-on re-sets it |

### 9f. Idempotency Guards and the `_fan_running` Property (Issue #392 Fix 1c / Fix 1e)

**Occupant-facing symptom this fixes:** during the 18:53–19:04 burst reported in Issue #392, the Activity Log showed what looked like several different automation decisions "fighting" every few minutes — the user could not tell whether the system was actually deliberating or just re-logging the same decision repeatedly.

**Root cause:** `_activate_fan()` and `_deactivate_fan()` had no check for "is the fan already in the state I'm about to put it in." Four independent (re)activation gate sites (§17) can each independently conclude "conditions are met" within the same few seconds — a grace-expiry timer callback, a sensor-open debounce callback, the 30-minute classification cycle, and the ODE ceiling guard all evaluate their own trigger conditions with no coordination. Before this fix, every one of them that reached the same conclusion re-ran the *entire* activation/deactivation sequence: re-capturing `_pre_fan_hvac_mode` from whatever the thermostat showed at that instant (possibly already stale from a sibling handler's change moments earlier), reissuing the physical service call, and emitting a fresh `fan_activated`/`fan_deactivated` event — even when the fan was already in the target state from a decision made two seconds prior.

**Fix — idempotency guard at the top of both functions**, after the existing `FAN_MODE_DISABLED` and `_fan_override_active` checks, before any state mutation:

```python
# in _activate_fan()
if self._fan_active:
    _LOGGER.debug("_activate_fan: already active — no-op (%s)", reason)
    return
```
```python
# in _deactivate_fan()
if not self._fan_active:
    _LOGGER.debug("_deactivate_fan: already inactive — no-op (%s)", reason)
    return
```

Effect: the first caller to legitimately flip the fan state performs the work and logs the event (INFO/WARNING + Activity Log entry). Every other handler that reaches the same conclusion moments later finds nothing left to do and produces only a `DEBUG`-level line — traceable in the logs, but not a duplicate Activity Log row. Combined with the archetype-aware ceiling fix (§6c) and the choke-point guard (above), which make the *decision itself* stable, this makes the *execution* stable too: one real state transition per actual change, not one log line per handler that happened to fire in the same window.

**`_fan_running` property (Fix 1e — shaping cut):** a related but separate smell was the recurring pattern `self._fan_active or self._natural_vent_active` appearing inline at multiple call sites (e.g. `nat_vent_temperature_check()`) to answer "is CA's fan on right now" — needing to OR two fields together to answer one question is evidence the two flags are one concept fractured into two names. Collapsed into a derived property:

```python
@property
def _fan_running(self) -> bool:
    return self._fan_active or self._natural_vent_active
```

Purely a readability/correctness-by-construction cut (no behavior change) — every inline `_fan_active or _natural_vent_active` OR was replaced with this property. Like `_whf_owns_hvac()`, it is a small stepping stone toward the deferred `FanSession` extraction (see the follow-up note in the Structural WHF/AC Mutual Exclusion subsection above), not the extraction itself.

**Test coverage:** `tests/test_fan_control.py` — exact function names pending as of this doc pass.

### 9g. `_decision_lock` — Serializing the Six Automation Entry Points (Issue #392 Fix 3)

The `__init__` code comment for `self._decision_lock` in `automation.py` points readers here (§9g) for the deadlock-avoidance analysis below.

**Occupant-facing symptom this fixes:** the same 18:53–19:04 burst also showed decisions from genuinely different trigger sources interleaving within the same few seconds. Fixes in §6c, above (choke-point guard), and §9f (idempotency) make each *individual* decision correct and stop *redundant* re-execution — but neither one, by itself, prevents two independently-triggered handlers from reading and writing shared engine state (`_natural_vent_active`, `_fan_active`, `_pre_fan_hvac_mode`, `_paused_by_door`) while the other is mid-flight. Python's `asyncio` is single-threaded but not atomic across `await` points, so handler B can start acting on state handler A is in the middle of changing.

**The six automation decision-pass entry points**, each independently triggerable by a different event source (HA state-change listener, `async_call_later` timer callback, or the coordinator's periodic `_async_update_data()`):

| # | Method | Trigger source |
|---|---|---|
| 1 | `apply_classification()` | Coordinator's 30-minute classification cycle |
| 2 | `handle_door_window_open()` | Coordinator callback after sensor-open debounce |
| 3 | `handle_all_doors_windows_closed()` | Coordinator callback after all sensors close |
| 4 | `check_natural_vent_conditions()` | Called by the coordinator on every `_async_update_data()` |
| 5 | `_re_pause_for_open_sensor()` | Triggered via `hass.async_create_task(...)` from the grace-expiry callback |
| 6 | `nat_vent_temperature_check()` | Periodic re-evaluation while nat-vent is active or paused |

**Fix:** `self._decision_lock = asyncio.Lock()` is created once in `__init__`. Each of the six methods above wraps its entire body in `async with self._decision_lock:` — a second trigger firing while a decision pass is already in progress waits for the lock instead of interleaving and racing on shared state:

```python
async def apply_classification(self, classification, predicted_indoor=None, indoor_temp=None) -> None:
    async with self._decision_lock:
        ...  # entire method body
```

**Deadlock-avoidance pre-check (required before wrapping):** `asyncio.Lock` is not reentrant — if any of the six methods called another of the six directly within the same call stack, wrapping both with the same lock would deadlock. Before implementing, the code was searched for direct cross-calls among the six; **none were found** — no method in this list calls another method in this list synchronously in its own body. Because of that, a plain `async with self._decision_lock:` wrap around each method's existing body was sufficient; no `_impl` extraction (splitting each method into a locked wrapper plus an unlocked `_impl` twin for internal cross-calls) was needed for this PR. If a future change introduces a direct call between any two of these six methods, that pre-check must be repeated and the `_impl` pattern applied before merging.

**What this does NOT change:** this lock does not introduce new automation behavior. The semantic fixes (§6c archetype-aware ceiling, above choke-point guard, §9f idempotency) already make each individual decision correct and idempotent; the lock ensures those correct decisions are evaluated one at a time against a consistent snapshot of engine state, instead of several handlers reading/writing overlapping state concurrently. In a well-behaved system where the semantic fixes hold, the lock should rarely be contended — its purpose is to make the *absence* of interleaving-driven chaos structurally guaranteed rather than incidentally true.

**Test coverage:** `tests/test_nat_vent_activation.py::TestDecisionLockConcurrency::test_two_entry_points_do_not_interleave` — `asyncio.gather()` invokes two of the six entry points "concurrently" against a shared engine instance instrumented to record enter/exit order, asserting non-overlapping execution.

#### Holder tracking — diagnosing a stuck lock (Issue #396)

This lock shipped with WARNING-level logging for the *contended-and-blocked* case
(`hvac_write_blocked_whf_active`, §9 above) but nothing for "a method is waiting on this lock and
it isn't coming back" — the exact failure mode that caused startup coalescing to hang indefinitely
in production shortly after this lock was deployed (root cause not yet confirmed as of this doc
pass; see Issue #396). That gap is closed by `_decision_pass()`, an async context manager every one
of the six entry points now goes through instead of a bare `async with self._decision_lock:`:

```python
async def apply_classification(self, ...) -> None:
    async with self._decision_pass("apply_classification"):
        ...  # entire method body, unchanged
```

`_decision_pass()`:
- Logs (DEBUG) when a method starts waiting on an already-held lock, naming the current holder.
- Sets `self._decision_lock_holder` (method name) and `self._decision_lock_held_since` (timestamp)
  immediately after acquiring — cleared in a `finally` immediately before release, so this is
  accurate even on exception paths.
- Logs (DEBUG) the wait duration on acquire and the hold duration on release.

`_decision_lock_holder` / `_decision_lock_held_since` are also surfaced on the coordinator status
API (`coordinator.py`, alongside `startup_coalesce_active`) as `decision_lock_holder` /
`decision_lock_held_seconds`, so a stuck lock is visible from the dashboard — "waiting on
`check_natural_vent_conditions`, held 340s" — instead of a generic "waiting for coalescing" with no
further detail. This is purely additive observability; it does not change which method acquires the
lock or when.

**Root cause confirmed NOT the lock (Issue #396 resolution):** deploying this instrumentation and
querying `decision_lock_holder` live on a stuck instance showed it was `null` — nothing was holding
the lock. The actual cause: the coalesce check in `_async_update_data()` lives entirely inside `if
forecast:`, so it never runs at all while the weather entity is `unavailable` after a restart
(`_get_forecast()` returns falsy, `_current_classification` stays `None`). `_compute_automation_status()`
now distinguishes this: if `_startup_timer_fired` is `True` (the 5-minute suppression window has
elapsed) but `_current_classification` is still `None`, the status returns `"starting — waiting for
weather data"` instead of the generic `"starting — initializing"` — so this specific failure mode is
diagnosable from the status card alone next time, without needing the lock instrumentation at all.
See `_compute_automation_status()` in `coordinator.py`.

**Test coverage:** `tests/test_nat_vent_activation.py::TestDecisionLockHolderTracking` — holder set
during a pass and cleared after, cleared even when the pass body raises, and a second (waiting) pass
can see the first pass's holder name while blocked.

---

### 9h. Fan/WHF FSM Extraction and Shadow-Diagnostic Coverage (Issue #731)

Same "pull the decision logic out into a pure, independently-testable module, wire it in behind a
per-engine authoritative flag, validate against a live shadow engine" pattern already applied to
nat-vent (`nat_vent_fsm.py`, Issue #633), door/window pause/grace (`door_window_fsm.py`, Issue #637),
and override/grace (`override_grace_fsm.py`, Issue #639) — see `docs/nat-vent-lifecycle-spec.md` for
the fullest write-up of that shared shape. `fan_fsm.py` is the fan/WHF subsystem's version, built in
9 phases across Issue #731. Full spec: `docs/fan-lifecycle-spec.md`.

**Composed, not flat, state.** Unlike the other 3 lifecycles, the fan/WHF subsystem does not collapse
to one enum. `fan_lifecycle.py`'s `FanLifecycleState` is a 5-axis composed dataclass — physical
(off/on/on-drift-suspected), override (none/active/active-remote-timer), cycling
(idle/active/suspended), HVAC ownership (none/suppressing), and toggle rate-limit
(not-deferred/deferred-activate/deferred-deactivate) — because a WHF can genuinely occupy several of
these independently at once (physically on, under manual override, owning HVAC suppression, AND
rate-limited against its next toggle, simultaneously). `fan_fsm.py`'s `FanFsmEventKind` has 16
members, one per real production entry point (`_activate_fan`, `_deactivate_fan`,
`reconcile_fan_on_startup`, `handle_fan_manual_override`, `clear_fan_override`,
`on_fan_turned_off`, its RF-timer-boundary coalesce branch, `_clear_fan_flags_and_start_grace`,
`_fan_cycle_on`/`_fan_cycle_off`/`_stop_fan_min_runtime_cycles`, `_reconcile_fan_physical_drift`,
the thermo-backstop trio, `fan_thermostat_check`, and the WHF-suppression/-release pair) — matching
`override_grace_fsm.py`'s handler-triggered, dispatch-on-event-kind shape rather than nat-vent's
periodic, dispatch-on-current-state shape (fan/WHF is not re-evaluated on a fixed tick the way
nat-vent's gate/exit chain is).

**Wiring differs structurally from the other 3.** Nat-vent/door-window/override-grace's pure FSM
modules were never wired into either `AutomationEngine` instance — each stands apart as a third,
independently-tracked coordinator-level computation (`self._nat_vent_fsm_state` etc.), compared
against both the production and shadow engines' own legacy-derived states. `fan_fsm.py` is wired
directly *inside* `AutomationEngine` instead, through a single dispatch chokepoint,
`_resolve_fan_fsm_state()`, called from every one of the 16 real entry points. Which code path a
given engine instance actually runs is gated by that engine's own `_fan_fsm_authoritative` boolean,
fixed once at construction (never toggled at runtime, matching Issue #729's "engine identity, not a
per-subsystem switch" simplification for the other 3 lifecycles): `False` for `_engine_a`/production
(legacy body runs, FSM is evaluated in parallel for audit-trail purposes only, never applied) and
`True` for `_engine_b`/shadow (the FSM's `to_state` is applied for real via `_apply_fan_fsm_state()`,
writing back `_fan_active`/`_fan_override_active`/`_fan_min_runtime_active`/`_pre_fan_hvac_mode`).
Both engines expose the same read-only `fan_lifecycle_state` property (`derive_fan_lifecycle_state()`
over each engine's own live flags), so it is always safe to read either engine's current composed
state regardless of which code path produced it.

**Two intentionally-unreachable event kinds.** `fan_fsm.py`'s own module docstring documents that
`THERMO_BACKSTOP_TICK` and `THERMOSTAT_CHECK_TICK` deliberately never move `to_state` away from
`from_state` — both outcomes only inform a downstream routing decision (`_exit_nat_vent()` /
`_deactivate_fan()` selection between several possible event types, HVAC-suppression-release
decisions) that this FSM's 5 composed axes cannot represent without duplicating logic that belongs
to those other functions — modeling a partial version of that routing here would risk exactly the
"sibling threshold drift" failure mode this codebase has hit repeatedly (Issues #400/#402/#417/#456/
#458). `thermostat_outcome`/`thermo_backstop_should_be_armed` are still populated on the returned
`FanTransition` for a future wired caller to act on; the composed state simply doesn't change for
these two kinds by design, not by omission.

**Shadow-diagnostic coverage: `fan_mirror` axis (no `fan_fsm` sibling).** `coordinator.py`'s
`_update_shadow_engine_diagnostic()` — the same wall-clock-debounced production/shadow comparison
that already tracks `mirror`/`fsm` (nat-vent), `door_window_mirror`/`door_window_fsm`, and
`override_grace_mirror`/`override_grace_fsm` — gained a 7th axis, `fan_mirror`, comparing
`self.automation_engine.fan_lifecycle_state` against `self.shadow_automation_engine.fan_lifecycle_state`
(joined into a single `"physical/override/cycling/hvac_ownership/rate_limit"` string, the same
joint-string convention `override_grace`'s `"confirm/grace"` state already uses). There is
deliberately **no** paired `fan_fsm` axis: for the other 3 lifecycles, that second axis compares
production against a free-standing third FSM computation the coordinator tracks itself
(`self._nat_vent_fsm_state` etc.) — a genuinely independent third opinion. Fan/WHF has no such
free-standing computation to compare against, because `_engine_b`'s own `fan_lifecycle_state` IS the
FSM-derived state already (via `_fan_fsm_authoritative=True`, above) — a hypothetical `fan_fsm` axis
would always read identically to `fan_mirror` (both would be comparing production against the exact
same shadow-engine-computed value), carrying zero independent signal. `sensor.climate_advisor_shadow_engine_status`'s
`extra_state_attributes` exposes `fan_production_state`/`fan_shadow_state`/`fan_mirror_agrees`
alongside the existing 3 lifecycles' fields, plus a `debounce.fan_mirror` sub-dict matching the
existing per-axis shape (`disagreement_seconds`/`sustained`/`cumulative_seconds_today`).

**`_activate_fan`/`_deactivate_fan` shadow-coverage classification.** Both remain classified
`"internal"` (not `"mirrored"`) in `tests/test_shadow_engine_coverage.py`'s registry, same as before
this issue — `_sync_shadow_inputs()`'s raw-copy mechanism (not a `_mirror_to_shadow()` replay call
site) is the only coverage path for the flags these two methods write, because both are called from
inside the shadow engine's own `_resolve_fan_fsm_state()` dispatch already (via
`_fan_fsm_authoritative=True`), not replayed a second time from the coordinator the way nat-vent's
mirrored methods are.

**No behavior change to production in this phase.** `_fan_fsm_authoritative=False` for `_engine_a`
for its whole lifetime — the legacy body of all 16 entry points still runs unconditionally and
unchanged; `_resolve_fan_fsm_state()`'s FSM branch only ever executes for the shadow engine. Standalone
comparator tests (`test_fan_fsm_authoritative_compare.py`) and the combined-lifecycle comparator
(`test_combined_fsm_authoritative_compare.py`) both confirm zero divergence between legacy and FSM
outputs across the shared golden/pending scenario corpus.

---

### 9d. Reconciling the Setpoint-Override and Fan-Override Status Lines (Issue #495)

`_compute_automation_status()` and `_compute_next_action()` read two **independent** flags:
the setpoint-override confirmation window (`_override_confirm_pending`, set immediately on a
detected setpoint change, cleared only once PATH A's 10-minute confirm timer elapses) and the
fan-override grace (`_grace_active`, started immediately on a detected manual/remote WHF-on).
These can legitimately overlap — e.g. a user opens windows (triggering an external setpoint
change CA flags as pending confirmation) and then turns on the whole-house fan (starting its
own grace immediately, no confirmation delay).

Before this fix, `_compute_automation_status()` checked `_override_confirm_pending` (returning
`"override pending (confirming...)"`) but `_compute_next_action()` had no equivalent branch —
during the overlap window it fell through to the `_grace_active` check and returned `"Grace
period active — automation will resume shortly."`, describing the *fan's* grace as if it were
the still-unconfirmed setpoint override's own. The two dashboard lines then narrated two
different overrides as one. Fix: `_compute_next_action()` gained an `_override_confirm_pending`
branch, checked before `_grace_active` (mirroring `_compute_automation_status()`'s ordering),
that names a concurrent fan override honestly rather than conflating the two.

Display-only — the two override mechanisms (setpoint-confirm and fan-grace) remain independent;
this only makes what's already true readable on the dashboard.

**Test coverage:** `tests/test_coordinator.py::TestComputeNextAction` (`_make_ae_stub()` now also
defaults `_override_confirm_pending`/`_fan_override_active` to `False`, per the MagicMock-truthy
pitfall this project has hit before — see CLAUDE.md's async-mock testing rules).

**Superseded by Issue #527** — the branch-syncing fix above kept two functions manually in
agreement about mechanism state, but it didn't hold: a third function
(`_compute_next_automation_action()`) independently grew the same class of duplication, and
`_compute_next_action()`'s own mechanism-flag branches went on to shadow its real comfort
guidance. §9e documents the resolution: mechanism state (paused/grace/override/confirming) was
removed from `_compute_next_action()` and `_compute_next_automation_action()` entirely, not kept
in sync — it now lives only in `_compute_automation_status()`. The four-card ontology table this
established is also in `CLAUDE.md` under "Status Card Ontology."

### 9e. Status Card Ontology — Removing Mechanism Narration from Next User Action / Next Automation (Issue #527)

Three coordinator functions feed the Status tab: `_compute_automation_status()` (Status card),
`_compute_next_action()` (Next User Action card), and `_compute_next_automation_action()` (Next
Automation + Automation Time cards). Each answers a different question, but `_compute_next_action()`
and `_compute_next_automation_action()` had each accumulated early-return branches that read
automation-mechanism flags (`is_paused_by_door`, `_grace_active`, `_override_confirm_pending`,
`_manual_override_active`) and returned mechanism text — duplicating `_compute_automation_status()`
and, in `_compute_next_action()`'s case, pre-empting its own real comfort guidance lower in the
function (unreachable whenever any of those flags was set).

**Fix:** those branches were deleted outright, not synced. `_compute_next_action()` now always
falls through to comfort-based guidance (window/fan direction checks, heating/cooling-needed
logic) regardless of pause/grace/override state — the guidance is correct independent of whether
the automation itself is currently paused. `_compute_next_automation_action()` now always falls
through to the real schedule-candidate list (briefing/wake/bedtime/pre-cool) for the same reason:
the plan is simply deferred until the mechanism state clears, not different.

Wording was also tightened to drop redundant "the AC/heater/automation is handling it" tails and
a stray "Automation active —" lead-in in the in-band fallback — these restated the Status card's
job (confirming the automation is active) inside a card whose job is comfort guidance. Away/vacation
occupancy messages became a small date-seeded rotating pool (`_AWAY_ACTION_MESSAGES`/
`_VACATION_ACTION_MESSAGES`, `_pick_daily_line()`) instead of one fixed sentence.

Also fixed in the same issue: `pause_suppressed_classification` (and a new
`pause_suppressed_classification_text`) were wired into `ClimateAdvisorStatusView` in `api.py` for
the first time — the field existed in `get_serializable_state()` (the Debug tab) and was
documented as a known gap in `KNOWN_FIXES[367]`, but had never been added to the actual Status API
response, so the frontend's `pause_suppressed_classification` check in `index.html` was
unreachable dead code (identical shape to the `nat_vent_active` gap fixed in the same PR as #367).

**Issue #620 follow-up:** the countdown/reason gap this ontology predicts — mechanism-state
detail belonging on the Status card and nowhere else — had one more instance: `_format_grace_remaining()`
(added by #498, orphaned by #527's cleanup above since it was never re-homed) and
`_last_action_reason` were both already correctly shown on the **Debug tab**, but not on the
Status card itself. `_compute_automation_status()`'s grace-active branch appended both, e.g.
`"grace period (manual) — Fan cancel (user turned off) — ends 7:14 AM (18 min left)"`.

**Issue #625 follow-up (superseding the #620 wiring above):** appending the raw
`_last_action_reason` sentence turned out to be the wrong home for two reasons. First, for
fan-triggered grace periods (WHF manually turned on/off, CA-initiated WHF suppression) the
reason string duplicated what the Fan (WHF) card already says via `_compute_whf_status()`
(e.g. Status: `"whole-house fan manually turned on — suppressing HVAC to prevent AC/fan
fighting"` vs. the Fan card's own `"running (manual override)"` + `"remote timer: 12h (ends
10:09 PM)"`), producing a long duplicated sentence. Second, for a manual thermostat override
(`_confirm_override()`, the "user turns off cooling at the wall unit" case) `_last_action_reason`
was never populated at all — `_confirm_override()` doesn't call `_record_action()` — so the
Status card showed a blank or, worse, a stale reason left over from some unrelated earlier
action.

The fix: `_start_grace_period(source, trigger=...)`'s existing `trigger` string (previously used
only for logging/event-payload correlation) is now retained as `AutomationEngine._last_grace_trigger`,
cleared alongside `_last_resume_source` in `_cancel_grace_timers()` and the restart/reset path.
`_compute_automation_status()`'s grace branch looks it up in a module-level `_GRACE_TRIGGER_LABELS`
dict (`coordinator.py`) to get a short (2-3 word) cause label — unmapped/unknown triggers fall
back to no cause segment rather than leaking a raw internal string onto the UI. `_format_grace_remaining()`
was also reworked: instead of a "N min left" countdown, it now shows the **applied** duration
(from `_grace_duration_seconds`) alongside the end time — the same structural shape as the Fan
(WHF) card's own `"remote timer: 12h (ends 10:09 PM)"` line. Result:
`"grace period (manual) — WHF override — 12h (ends 10:09 PM)"` for the WHF case, and
`"grace period (manual) — thermostat override — 30 min (ends 7:14 AM)"` for the manual-override
case that previously showed nothing. The full free-text reason remains available on the Debug tab
(`_last_action_reason`), unaffected.

**Test coverage:** `tests/test_coordinator.py::TestComputeNextAction` (paused/grace/override tests
renamed `test_next_action_*_does_not_preempt_schedule`, asserting the real guidance now surfaces
and the mechanism word does NOT appear), `tests/test_status_sensors.py::TestComputeNextAutomationAction`
(`test_paused_by_door_still_shows_real_next_step`, `test_grace_period_active_still_shows_real_next_step`),
`tests/test_status_sensors.py::TestGraceStatusNoLongerLeaksLastActionReason` and
`::TestFormatGraceRemaining` (Issue #625 — cause-label lookup, duration+end-time formatting,
confirms `_last_action_reason` never leaks onto Status),
`tests/test_grace_period_desired_state_integration.py::test_start_grace_period_stores_trigger_for_status_card`.

### 9f. Timestamp-Correct Forecast-Curve Crossing Scan, and Predictive Next Automation Candidates (Issue #528)

**Bug:** `briefing.py`'s `_derive_warm_day_events()` — the function that derives WARM/MILD-day
window-close and reopen times shown in the daily briefing (§7) — paired the predicted-indoor and
predicted-outdoor forecast curves by **list index** (`zip(predicted_indoor, predicted_outdoor)`)
rather than by matching ISO timestamp. The two curves are built with different "now" filter
operators and, in production, at different times (indoor is cached from the last 30-min coordinator
cycle; outdoor is rebuilt fresh on every briefing send) — any drift between the two silently shifted
the pairing without either timestamp ever being cross-checked. This shipped whole-cloth with Issue
#518 (which fixed the two briefing surfaces *agreeing* with each other, not the derived values
themselves being correct) and had zero test coverage, since every existing test built both curves
starting at the same hour with the same length — i.e., perfectly aligned by construction. Live
production logs confirmed a real occurrence: a Warm 79°F day's briefing said "Close up at 8:00 AM"
and "Reopen windows around 2:00 PM," neither plausible relative to the same computation's own
5:30 PM `ceiling_breach_time`.

**Fix:** `find_temperature_crossing(indoor_curve, outdoor_curve, comparator, after=None)` in
`temperature.py` — aligns the two curves by building a `{ts: temp}` lookup from one and walking the
other, so an hour present in only one curve is skipped rather than mismatched against a neighboring
hour from the other. `comparator(ts, outdoor_temp, indoor_temp)` receives the timestamp (not just
the two temperatures) so time-of-day-aware conditions (e.g. a sleep-window-gated threshold) can be
expressed without a second scanning loop. `_derive_warm_day_events()`'s `nat_vent_cutoff` and
`recovery_time` (and, transitively, `any_nat_vent_window`) now go through this function instead of
the manual zip; `ceiling_breach_time` was never pairing-dependent (indoor-only) and is unchanged.
`recovery_time` was also added to the function's `_LOGGER.debug("WarmDayEvents: ...")` line — it was
the one field in that dict not logged, which is why this took live-log correlation across the other
three fields to catch instead of a direct log grep.

**Feature, built on the same primitive:** `_compute_next_automation_action()` gained three new
candidate types, none of which existed before this issue:
- **Nat-vent/WHF start prediction** — scans `self._last_predicted_indoor` against a freshly-built
  outdoor forecast curve (`_build_future_forecast_outdoor()`) using `decide_nat_vent_gate()` from
  `nat_vent_gate.py` as the comparator — the same pure, already-production-validated activation gate
  `automation.py`'s `check_natural_vent_conditions()` calls (differentially tested against
  `_nat_vent_may_reactivate()`). **Not** `compute_nat_vent_cycling_band()` — that function
  describes the fan's on/off cycling midpoint *while a session is already active* (its own docstring
  says so explicitly, and `nat_vent_temperature_check()` early-returns when inactive) — a materially
  different formula (no ceiling-margin/fan-mode/aggressive-savings awareness) that would have
  silently repeated this section's own bug class had it been used as the activation threshold. Gated
  on `ae.is_paused_by_door or self._any_sensor_open()`, mirroring `check_natural_vent_conditions()`'s
  own precondition — nat-vent cannot start with everything closed, so the candidate never promises a
  time contingent on the occupant opening a window first.
- **WARM/MILD warm-day-event candidates** — the same `nat_vent_cutoff`/`ceiling_breach_time`/
  `recovery_time` computed above (now timestamp-correct) were originally surfaced as three
  candidates: "Close windows — outdoor no longer helping" / "AC turns on to hold the ceiling" /
  "Reopen windows — outdoor helping again," gated on `c.windows_recommended` and a forecast curve
  being available. **The `nat_vent_cutoff` ("Close windows...") and `recovery_time` ("Reopen
  windows...") candidates were removed by Issue #849**: both instructed the occupant to physically
  operate windows — an action CA has no actuator for — duplicating what Next User Action already
  says, in violation of the Status Card Ontology (CLAUDE.md, "Status Card Ontology" section). The
  `ceiling_breach_time` candidate ("AC turns on to hold the ceiling") is unaffected — it describes
  an automation-executed setpoint change and remains a valid Next Automation candidate.
- **HOT-day window-cooling opportunities** — **removed by Issue #849.** `classifier.py`'s static
  `window_opportunity_morning`/`_evening` fields had been surfaced here as "Morning/Evening window
  cooling opportunity" candidates in addition to their pre-existing use on the Next User Action
  card; despite being "[d]eliberately worded to avoid implying the automation opens the window
  itself," the candidates still instructed an occupant action on a card whose contract is
  automation-executed actions only, duplicating Next User Action. See CLAUDE.md's Status Card
  Ontology section for the corrected invariant: every Next Automation candidate must be an action
  CA itself performs, independent of wording or time treatment.

**Test coverage:** `tests/test_temperature.py::TestFindTemperatureCrossing` (alignment, `after`
boundary, comparator receiving `ts`); `tests/test_briefing.py::TestDeriveWarmDayEvents`
(`test_misaligned_curves_pair_by_timestamp_not_index`, `test_misaligned_curves_recovery_time_after_true_cutoff`
— both fail against the pre-fix zip-based implementation, confirmed by reverting locally before
merge); `tests/test_status_sensors.py::TestHotDayWindowOpportunityCandidates`,
`TestNatVentStartCandidate` (including `test_uses_real_gate_ceiling_margin_not_naive_midpoint`,
proving the real gate's formula is used, not the cycling-band midpoint), `TestWarmDayForecastEventCandidates`.

---

## 10. Door/Window HVAC Pause

| Step | Behavior |
|---|---|
| Sensor opens | Debounce timer starts (`DEFAULT_SENSOR_DEBOUNCE_SECONDS = 600s / 10 min` as of Issue #504 — was 300s/5min, configurable) |
| During debounce | No HVAC action taken |
| Debounce expires (sensor still open) | `_hvac_command_pending` set; HVAC mode saved as `pre_pause_mode`; HVAC set to `off`; notification sent |
| Grace period active at debounce expiry | Pause **blocked** — no HVAC change, log message only |
| HVAC already `off` at pause time | No action (nothing to pause) |
| All monitored sensors close | Restore HVAC to `pre_pause_mode`; restore comfort temperature; start **automation** grace period |

**Issue #504 — the same debounce timer also gates nat-vent idle_open reactivation:** `check_natural_vent_conditions()`'s idle_open branch (§12b, Issue #244/#402) used to react to a sensor's raw instantaneous open state with no settle time at all — a real incident showed a monitored contact sensor group bouncing open/closed 7 times in 28 seconds, and each open transition independently re-armed the whole-house fan before the very next close tore it down via `_exit_nat_vent()`. The coordinator now exposes `_sensor_debounce_pending_callback` (reusing the same per-entity `_door_open_timers` this section describes — an entity is present in that dict for exactly the duration of its debounce window), and idle_open only reactivates once no currently-open monitored sensor still has a pending timer. Issue #244's actual scenario (a sensor open long past any debounce window, outdoor cooling later) is unaffected — by the time that case occurs the debounce timer has long since resolved regardless.
| User manually turns HVAC on during pause | Clears pause state; starts **manual** grace period; manual override activated |
| User clicks "Resume HVAC (override pause)" button | Clears pause state; restores classification's recommended HVAC mode; starts **manual** grace period; status set to `"resumed — door/window override"` |
| Command-pending flags (`_hvac_command_pending`, `_fan_command_pending`, `_temp_command_pending`) | Each flag is set `True` immediately before the integration issues the corresponding service call and cleared after it completes. `_async_thermostat_changed()` checks **all three** flags: if any is `True`, the state change is treated as automation-issued and both the pause-path and normal-path override detection are suppressed. This compound check is required because automation sequences (e.g., nat vent exit) call `_deactivate_fan()` before `_set_hvac_mode()` — the fan command sets `_fan_command_pending` but `_hvac_command_pending` is still `False`. Checking only `_hvac_command_pending` bypasses the guard. `_hvac_command_time` records the timestamp of the last HVAC command for the secondary `_is_recent_hvac_command()` timestamp guard. See §9b for the full guard specification. |

---

## 11. Grace Periods

| Type | Trigger | Default Duration | Configurable? | Effect | Notify on Expiry (default) |
|---|---|---|---|---|---|
| Manual | User overrides thermostat — mode change **or setpoint-only change** (v0.3.55+, Issue #197) — or clicks "Resume HVAC (override pause)" | `1800s` (30 min) | Yes — `CONF_MANUAL_GRACE_PERIOD` | Blocks door/window sensor from re-pausing HVAC; classification skips HVAC mode changes | Yes (`CONF_MANUAL_GRACE_NOTIFY = True`, Issue #282). Message: "Your manual thermostat override has expired. Climate Advisor has resumed automated control." |
| Automation | Climate Advisor resumes HVAC after all sensors close | `300s` (5 min) | Yes — `CONF_AUTOMATION_GRACE_PERIOD` | Blocks door/window sensor from immediately re-pausing HVAC | Yes (`CONF_AUTOMATION_GRACE_NOTIFY = True`) |

Only one grace timer of each type is active at a time; starting a new one cancels the previous.

**Grace expiry sensor re-check:** When either grace period expires, the system re-checks whether any monitored contact sensor is currently open. If one or more sensors are still open, HVAC is re-paused immediately (`_paused_by_door = True`, HVAC set to `off`) rather than restoring normal automation. This prevents the safety issue of running HVAC with a door or window open after the grace window closes. If instead the nat-vent reactivation gate is satisfied (sensor open but outdoor conditions now favor free cooling), `_re_pause_for_open_sensor()` activates nat-vent and clears `_paused_by_door` (Issue #637, v0.6.23) — matching every other nat-vent-activation call site, so the away/vacation setback guard and dashboard/API status don't keep reporting "paused by door" once nat-vent has taken over.

### Clean-Slate Override State on HA Restart (Issue #282 / #306)

CA always starts in full clean-slate automation mode after an HA restart. `restore_state()` does **not** restore override, grace, or pause state. All three categories are intentionally kept out of `get_serializable_state()` (override/grace since Issue #282; pause state since Issue #306).

**What is preserved across restarts:**

| Field | Preserved? | Notes |
|---|---|---|
| `_paused_by_door` | **No** | Cleared on restart (Issue #306). Open sensors are re-detected quickly via the state-change listener (entity transitions from `None` → `"on"` when HA reconnects); HVAC re-arms briefly and re-pauses after the configured debounce (default 5 min). |
| `_pre_pause_mode` | **No** | Cleared on restart (Issue #306). Re-captured when the re-detected open sensor triggers a fresh pause. |
| `_fan_active` | **Hint only** (Issue #327) | Preserved as a hint; overwritten or disregarded by the coalesce reconcile step (§9e) — does not gate any action on its own after restart |
| `_fan_override_active` | **No** (Issue #327) | Cleared on restart — clean slate. Coalesce re-derives fan ownership from live state. Previously preserved, which caused permanent override lockout when no grace-expiry timer was rescheduled. |
| `_fan_override_time` | **No** (Issue #327) | Cleared on restart |
| `_pre_fan_hvac_mode` | Yes | HVAC mode captured before whole-house fan activation; still needed for HVAC restoration if coalesce decides to turn off a whole-house fan |
| `_last_action_time` / `_last_action_reason` | Yes | Last automation action metadata |
| `_occupancy_mode` | Yes | Current occupancy state |
| `_manual_override_active` | **No** | Cleared on restart — clean slate |
| `_grace_active` / `_grace_end_time` | **No** | Cleared on restart — clean slate |
| `_override_confirm_pending` | **No** | Cleared on restart — clean slate |
| `_manual_override_mode` / `_manual_override_time` | **No** | Cleared on restart |

**Why pause state is not restored (Issue #306):** Persisting `_paused_by_door` risks leaving CA paused indefinitely if cloud services (weather, thermostat) reconnect slowly — the sensor may not fire a state-change callback unless it transitions away from `None`. Re-detecting via the normal `None → "on"` listener path is more reliable than trusting stale persisted state. The sensor entity registers quickly after HA startup, but the re-pause takes the configured debounce (default 5 min) before `handle_door_window_open()` fires. During that window HVAC briefly re-arms — a small trade-off that is strictly better than sitting paused indefinitely on a hot day. This matches the existing clean-slate policy for manual overrides and grace periods.

**A window already open BEFORE restart never fires that listener at all (Issue #523).** The `None → "on"` state-change listener only fires on a genuine transition — if the sensor was already `"on"` before HA restarted (the common case: the window was open, causing the "briefly re-arms" trade-off above, right when the restart happened), there is no transition for `_async_door_window_changed` to catch. `_do_startup_coalesce()`'s own raw `_is_sensor_open()` read at the 5-minute mark is the *only* mechanism that observes this case. Before this fix, `_do_startup_coalesce()` hand-rolled its own incomplete nat-vent gate purely to decide whether to call `handle_door_window_open()` at all — when that pre-check favored declining nat-vent (e.g. outdoor warmer than indoor), the call never happened, `_paused_by_door` was never (re)set, and `apply_classification()` armed a fresh comfort band against the still-open window. Fixed by having `_do_startup_coalesce()` delegate unconditionally to `handle_door_window_open()` whenever a sensor is open, letting it make the nat-vent-vs-pause decision with its own complete gate — the same single source of truth the normal runtime path already uses. A second, compounding gap: `handle_door_window_open()`'s pause branch only ever set `_paused_by_door=True` when the current HVAC mode was not already `"off"` — exactly the state `_do_startup_coalesce()` finds after a restart where the window was already open (HVAC had already been paused/suppressed before the restart). Both `handle_door_window_open()` and `_re_pause_for_open_sensor()` now share a `_pause_for_door_window()` helper that sets the flag in both branches (mode-change or not), closing a drift the two call sites already had once (`_re_pause_for_open_sensor()`, added in #47, already got this right; the older `handle_door_window_open()` never did).

**Settling window:** `restore_state()` sets `_first_run = True`. The coordinator's `_async_update_data()` delays the first full automation evaluation by 5 minutes (`_first_run` guard) to let the thermostat and HA state settle before CA takes any HVAC action. This replaces the role previously played by persisted override state in preventing false automations after restart.

### Startup Override Logic

On first data update after startup, Climate Advisor checks whether the HVAC's current mode matches the day classification's recommended mode before setting a manual override:

| HVAC state | Classification recommends | Result |
|---|---|---|
| `off` / `unavailable` / `unknown` | any | No override set |
| `heat` | `heat` | No override — modes match |
| `heat` | `cool` or `off` | Manual override set — respects current state |
| `cool` | `cool` | No override — modes match |
| `cool` | `heat` or `off` | Manual override set — respects current state |

This prevents unnecessary override lockouts after a Home Assistant restart when the HVAC is already in the mode that Climate Advisor would have set anyway. See Issue #42. This check runs after the 5-minute `_first_run` settling window.

### PATH B Notification — Transient Thermostat Adjustment (Issue #200)

When the thermostat self-reverts to the expected mode within the `CONF_OVERRIDE_CONFIRM_PERIOD` confirmation window (PATH B — `override_self_resolved`), a user notification is sent:

> "Brief thermostat adjustment detected — treated as transient. Climate Advisor continues normal operation."

This informs the occupant that a brief thermostat blip was observed but was not treated as an intentional override. No grace period starts; normal automation resumes immediately. Notification is sent only when a notify service is configured.

For the full confirmation-window state machine (PATH A vs PATH B), see [Grace Periods Spec — Override Confirmation Delay](grace-periods-spec.md#override-confirmation-delay).

### Second Override During Active Grace (Issue #201)

If the user changes the thermostat to a **different mode** while `_manual_override_active = True` (i.e., a grace period is already running), the engine treats this as a new, distinct override:

1. The current override and grace timer are cleared via `clear_manual_override()`.
2. A new 10-minute `CONF_OVERRIDE_CONFIRM_PERIOD` confirmation window starts for the new mode.
3. If the new mode is still divergent after the confirmation window (PATH A), a fresh 30-minute grace period begins.

This prevents the scenario where an occupant makes two sequential manual adjustments and the second one is silently ignored because grace from the first is still active. The net effect is that the latest user intent always wins: CA monitors the newest mode change with a fresh confirmation window.

**Invariant:** only one confirmation window is active at a time. Starting a new window cancels the previous one (via `clear_manual_override()` then `start_override_confirmation()`).

---

## 12. Revisit Mechanism

After any HVAC action (mode change or temperature set), the coordinator calls `_schedule_revisit()`, which posts a delayed `async_request_refresh()` for 5 minutes later (`REVISIT_DELAY_SECONDS = 300`). When the refresh fires, the full automation evaluation runs again — including re-checking eligibility for the economizer, any pending pre-conditioning, and the current occupancy and time context.

If that re-evaluation results in another HVAC action, `_schedule_revisit()` is called again, scheduling yet another follow-up 5 minutes out. The loop terminates naturally when an evaluation pass finds no action is needed. There is no explicit iteration cap; the exit condition is that the system has reached a stable state.

This mechanism ensures that a multi-step transition (for example: economizer detects indoor temp still high after fan activation, then re-evaluates whether to switch to Phase 1 AC assist) converges without requiring a separate scheduling path for each step. It also catches edge cases where conditions change in the minutes immediately following an automated action (e.g., a window is closed just after the economizer activated).

Only one pending revisit is active at a time. If `_schedule_revisit()` is called while a revisit is already scheduled, the previous scheduled call is cancelled and replaced by the new one.

---

## 13. Logging Level

**Issue #585 superseded the original #37 rationale below.** Issue #37 promoted routine HVAC-action log statements to `_LOGGER.warning()` purely so they'd survive HA's default (unconfigured) log level for custom components — a visibility hack, not a statement about severity. That conflated "needs to be visible" with "something is wrong." `WARNING` is reserved for an actual anomaly, a value clamped or overridden by a guard, or a safety guard firing that would otherwise leave the HVAC in an unexpected state (see `docs/06-LOGGING-GUIDELINES.md` §Level Semantics, and CLAUDE.md's Observability Requirements). A routine, correctly-executed mode change, setpoint write, classification application, override acceptance, or fan activation is not a malfunction, regardless of how visible operators want it — so as of #585 these all log at `_LOGGER.info()`:

- `_set_hvac_mode()` — successful mode writes (line ~2807); the guard-blocked branch when WHF owns the thermostat (line ~2772) is a genuine guard override and stays `WARNING`
- `_set_temperature()` — successful setpoint writes (line ~3090); its guard-blocked branch (line ~2845) likewise stays `WARNING`
- `_record_action()` — action history bookkeeping
- `apply_classification()` — day classification application announcement; its door/window-pause `DEFER_PAUSED` branch (a guard suppressing the band) stays `WARNING`
- `start_override_confirmation()` / `_confirm_override_action()` — override detected/confirmed/activated (the system correctly deferring to the user, not an error)
- Nat-vent/WHF manual-override conflict stand-downs (3 call sites) — correct conflict resolution
- Bedtime/morning-wakeup pending-override clearing — routine lifecycle transitions
- Fan activate/deactivate (WHF and HVAC fan-only), including the RF-remote-timer suppress/force cases (Issues #486/#748) — the mutex/timer logic working as designed is not a malfunction

**Still `WARNING`, because these describe an actual anomaly, a guard clamping/overriding a write, or a safety condition:** invalid occupancy mode input; a stale `_fan_override_active` flag being corrected; `_apply_comfort_band()`'s own door/window choke-point guard refusing to arm an active mode; a rejected setpoint write being retried; a nat-vent session force-closed due to an internal flag/sensor mismatch; a pre-cool target clamped to the comfort floor; a pre-cool overshoot below `comfort_heat`; a stale nat-vent FSM decision caught and corrected; a WHF control/physical-state disagreement being forced back into sync; a confirmed physical fan-state drift; a non-numeric sensor state; and `handle_occupancy_away()`'s "no day classification available" skip. Each of these is either malformed/missing input, an internal state inconsistency being corrected, or a guard actively preventing/clamping a write — genuinely warning-worthy, independent of HA's default log-viewer behavior.

Routine diagnostic messages (coordinator polling, entity state reads, skip-due-to-grace-period notices) remain at `_LOGGER.debug()` and are suppressed under normal operation.

**Visibility note:** the AI Investigator's SYSTEM LOG RECORDS block (`log_capture.py`) captures WARNING+ only, so the reclassified lines above no longer appear there — visibility is instead provided by the (deduplicated) Activity Timeline and the dashboard's Activity Record tab, both of which read `coordinator._event_log` directly, independent of Python log level. An install with an existing `logger:` override at info/debug for `custom_components.climate_advisor` sees no change in Settings → System → Logs either way — level is a filter threshold, not a data-loss operation.

---

## 14. "Prefer Savings Over Comfort" (aggressive_savings)

The `aggressive_savings` flag currently affects one system:

| System | Normal (False) | Savings (True) |
|---|---|---|
| Economizer | Two-phase: AC cool-down first, then ventilation-only maintain | Skip AC entirely — go straight to ventilation-only maintain phase |

Future versions may extend `aggressive_savings` to apply more aggressive setback values. At this time, setback formulas are identical regardless of this flag.

---

## 15. Defaults Reference

Complete list of all constants from `const.py` that affect runtime behavior.

| Constant Name | Default Value | Unit | Description |
|---|---|---|---|
| `DEFAULT_COMFORT_HEAT` | `70` | °F | Heating target when home/comfort |
| `DEFAULT_COMFORT_COOL` | `75` | °F | Cooling target when home/comfort |
| `DEFAULT_SETBACK_HEAT` | `60` | °F | Heating target when away |
| `DEFAULT_SETBACK_COOL` | `80` | °F | Cooling target when away |
| `DEFAULT_SLEEP_HEAT` | `66` | °F | Bedtime heating target (default: `comfort_heat − 4°F`); overrides adaptive depth when `sleep_heat` is explicitly configured (#101) |
| `DEFAULT_SLEEP_COOL` | `78` | °F | Bedtime cooling target (default: `comfort_cool + 3°F`); overrides adaptive depth when `sleep_cool` is explicitly configured (#101) |
| `THRESHOLD_HOT` | `85` | °F | today_high threshold for `hot` day type |
| `THRESHOLD_WARM` | `75` | °F | today_high threshold for `warm` day type |
| `THRESHOLD_MILD` | `60` | °F | today_high threshold for `mild` day type |
| `THRESHOLD_COOL` | `45` | °F | today_high threshold for `cool` day type |
| `TREND_THRESHOLD_SIGNIFICANT` | `10` | °F | avg_delta magnitude for significant trend |
| `TREND_THRESHOLD_MODERATE` | `5` | °F | avg_delta magnitude for moderate trend |
| `VACATION_SETBACK_EXTRA` | `3` | °F | Extra setback depth beyond normal away setback during vacation |
| `DEFAULT_SENSOR_DEBOUNCE_SECONDS` | `600` | seconds (10 min, Issue #504 — was 300s/5min) | Door/window sensor state must hold steady this long before Climate Advisor reacts — HVAC pause/resume, or nat-vent/WHF/HVAC-fan engage/exit |
| `DEFAULT_MANUAL_GRACE_SECONDS` | `1800` | seconds (30 min) | Duration of manual grace period after user override |
| `DEFAULT_AUTOMATION_GRACE_SECONDS` | `300` | seconds (5 min) | Duration of automation grace period after HVAC resumes |
| `DEFAULT_OVERRIDE_CONFIRM_SECONDS` | `600` | seconds (10 min) | Debounce window between detecting a thermostat mode change and formally accepting it as a manual override. During this window `apply_classification()` is blocked. Transient glitches (thermostat restart, HA echo) that resolve within the window are discarded without starting a grace period. Set to 0 to confirm overrides immediately. See [Grace Periods Spec — Override Confirmation Delay](grace-periods-spec.md#override-confirmation-delay). |
| `ECONOMIZER_TEMP_DELTA` | `3` | °F | Outdoor temp must be within this delta of comfort_cool for economizer eligibility |
| `ECONOMIZER_MORNING_START_HOUR` | `6` | hour (24h) | Economizer morning window start |
| `ECONOMIZER_MORNING_END_HOUR` | `9` | hour (24h) | Economizer morning window end |
| `ECONOMIZER_EVENING_START_HOUR` | `17` | hour (24h) | Economizer evening window start (5 PM) |
| `ECONOMIZER_EVENING_END_HOUR` | `24` | hour (24h) | Economizer evening window end (midnight) |
| `WARM_WINDOW_OPEN_HOUR` | `6` | hour (24h) | Warm-day window open time |
| `WARM_WINDOW_CLOSE_HOUR` | `10` | hour (24h) | Warm-day window close time |
| `REVISIT_DELAY_SECONDS` | `300` | seconds (5 min) | Follow-up re-evaluation delay after any HVAC action |
| `OCCUPANCY_SETBACK_MINUTES` | `15` | minutes | Delay before applying away setback temperature after departure |
| `MAX_CONTINUOUS_RUNTIME_HOURS` | `3` | hours | Reserved — maximum continuous HVAC runtime guard |
| `SUGGESTION_COOLDOWN_DAYS` | `7` | days | Learning engine: minimum days between repeat suggestions |
| `MIN_DATA_POINTS_FOR_SUGGESTION` | `14` | data points | Learning engine: minimum records before generating suggestions |
| `COMPLIANCE_THRESHOLD_LOW` | `0.3` | ratio | Learning engine: below 30% compliance triggers a suggestion |
| `COMPLIANCE_THRESHOLD_HIGH` | `0.8` | ratio | Learning engine: above 80% compliance means advice is working |
| `DEFAULT_FAN_MODE` | `disabled` | — | Fan control default (no fan control) |
| `DEFAULT_SETBACK_DEPTH_F` | `4` | °F | Bedtime heat setback depth fallback when thermal model confidence is `"none"` |
| `DEFAULT_SETBACK_DEPTH_COOL_F` | `3` | °F | Bedtime cool setback depth fallback when thermal model confidence is `"none"` |
| `DEFAULT_PREHEAT_MINUTES` | `120` | minutes | Pre-heat lead time fallback when no thermal model data |
| `MIN_PREHEAT_MINUTES` | `30` | minutes | Minimum clamped pre-heat lead time |
| `MAX_PREHEAT_MINUTES` | `240` | minutes | Maximum clamped pre-heat lead time |
| `THERMAL_POST_HEAT_TIMEOUT_MINUTES` | `45` | minutes | Maximum post-heat observation window before abandoning |
| `THERMAL_STABILIZATION_THRESHOLD_F` | `0.3` | °F | |ΔT| threshold for stabilization criterion |
| `THERMAL_STABILIZATION_WINDOW_MINUTES` | `5` | minutes | Duration |ΔT| must remain below threshold to count as stabilized |
| `THERMAL_SAMPLE_INTERVAL_SECONDS` | `60` | seconds | Active-phase HVAC sampling cadence (ungated; all polls recorded) |
| `THERMAL_PRE_HEAT_BUFFER_MINUTES` | `15` | minutes | Rolling pre-HVAC sample window included in k_passive regression |
| `THERMAL_MAX_ACTIVE_SAMPLES` | `120` | samples | Cap on active-phase samples (2 hours at 60s cadence) |
| `THERMAL_MAX_POST_HEAT_SAMPLES` | `45` | samples | Cap on post-heat samples (45 min at 60s cadence) |
| `THERMAL_MIN_R_SQUARED` | `0.2` | — | Minimum R² for k_passive OLS regression to accept an observation |
| `THERMAL_MIN_POST_HEAT_SAMPLES` | `4` | samples | Minimum post-heat samples required before committing an HVAC observation (Issue #130 D14: lowered from 10; enables short 5–30 min cycles) |
| `THERMAL_HVAC_MIN_SIGNAL_F` | `0.5` | °F | Minimum `|T_peak − T_start|` for a heating/cooling cycle to be treated as meaningful signal. Below this the cycle is a setpoint-maintenance run and is rejected (Issue #130 D23) |
| `THERMAL_K_PASSIVE_MIN` | `-0.5` | hr⁻¹ | Sanity lower bound for k_passive (very leaky envelope) |
| `THERMAL_K_PASSIVE_MAX` | `-0.001` | hr⁻¹ | Sanity upper bound for k_passive (very well insulated) |
| `THERMAL_K_ACTIVE_HEAT_MIN` | `0.5` | °F/hr | Minimum credible HVAC heating contribution |
| `THERMAL_K_ACTIVE_HEAT_MAX` | `15.0` | °F/hr | Maximum credible HVAC heating contribution |
| `THERMAL_K_ACTIVE_COOL_MIN` | `-15.0` | °F/hr | Maximum credible HVAC cooling contribution (magnitude) |
| `THERMAL_K_ACTIVE_COOL_MAX` | `-0.5` | °F/hr | Minimum credible HVAC cooling contribution (magnitude) |
| `THERMAL_DECAY_MAX_WINDOW_MINUTES` | `60` | minutes | Wall-clock limit before `ventilated_decay` / `fan_only_decay` abandon (H4) |
| `THERMAL_ROLLING_WINDOW_MINUTES` | `30` | minutes | Rolling commit+restart interval for all four non-HVAC decay types (H2) |
| `THERMAL_ROLLING_MIN_DELTA_T_F` | `0.2` | °F | Minimum total indoor ΔT to commit a short rolling window (H2 ΔT guard) |
| `THERMAL_PASSIVE_SAMPLE_INTERVAL_S` | `300` | seconds (5 min) | Sample gate for `passive_decay` and `ventilated_decay` (H1) |
| `THERMAL_FAN_SAMPLE_INTERVAL_S` | `120` | seconds (2 min) | Sample gate for `fan_only_decay` — faster than passive dynamics (H1) |
| `THERMAL_SOLAR_SAMPLE_INTERVAL_S` | `300` | seconds (5 min) | Sample gate for `solar_gain` (H1) |
| `THERMAL_HVAC_POST_HEAT_SAMPLE_INTERVAL_S` | `300` | seconds (5 min) | Sample gate for HVAC post-heat phase — passive dynamics (H1) |
| `THERMAL_SOLAR_FACTOR_MIN_RANGE` | `0.30` | — | Minimum solar_factor range (max−min) across ventilated_decay samples to trigger 2-param OLS (Issue #126) |

**User-facing config keys** (set via config flow, stored in the config entry):

| Config Key | Default | Description |
|---|---|---|
| `temp_unit` | `fahrenheit` | Temperature unit for display and input (`fahrenheit` or `celsius`). All internal calculations use Fahrenheit as the canonical unit; this setting controls conversion at the HA boundary (inbound sensor readings and outbound thermostat setpoints) and the display unit in briefings and logs. |

**AI settings** (set via config flow, affect AI feature behavior):

| Constant Name | Default Value | Unit | Description |
|---|---|---|---|
| `DEFAULT_AI_ENABLED` | `False` | — | AI features disabled by default; user must opt in |
| `DEFAULT_AI_MODEL` | `"claude-sonnet-5"` | — | Claude model used for all AI requests |
| `DEFAULT_AI_REASONING_EFFORT` | `"low"` | — | Reasoning effort level passed to the Claude API |
| `DEFAULT_AI_MAX_TOKENS` | `4096` | tokens | Maximum tokens per AI response |
| `DEFAULT_AI_TEMPERATURE` | `0.3` | — | Sampling temperature for AI responses (lower = more deterministic) |
| `DEFAULT_AI_MONTHLY_BUDGET` | `0` | USD | Monthly spend cap; `0` means no cap |
| `DEFAULT_AI_AUTO_REQUESTS_PER_DAY` | `5` | requests/day | Maximum automated AI requests per day |
| `DEFAULT_AI_MANUAL_REQUESTS_PER_DAY` | `20` | requests/day | Maximum user-triggered AI requests per day |
| `AI_CIRCUIT_BREAKER_THRESHOLD` | `5` | failures | Consecutive failures before the circuit breaker trips |
| `AI_CIRCUIT_BREAKER_COOLDOWN_SECONDS` | `300` | seconds (5 min) | Cooldown duration after circuit breaker trips before retrying |
| `AI_REQUEST_HISTORY_CAP` | `50` | entries | Maximum in-memory request history entries (prevents unbounded growth) |

**Fan state tracking fields** (runtime coordinator state, not configurable constants):

| Field | Initial Value | Description |
|---|---|---|
| `_fan_active` | `False` | Whether the integration currently has the fan on |
| `_fan_on_since` | `None` | UTC timestamp of last fan activation by the integration |
| `_fan_override_active` | `False` | Whether a user manual fan override is in effect |
| `_fan_override_time` | `None` | UTC timestamp of when the fan override was detected |
| `_fan_command_pending` | `False` | Set during integration-issued fan commands to suppress false override detection |
| `_fan_command_time` | `None` | UTC timestamp of the most recent `_activate_fan()` / `_deactivate_fan()` call; read by `_is_recent_fan_command()` |
| `_pre_fan_hvac_mode` | `None` | HVAC mode captured before whole-house fan activation; restored on deactivation (`FAN_MODE_WHOLE_HOUSE` / `both` only) |

---

## 16. Planned Window Period

`_is_within_planned_window_period()` is a predicate in `AutomationEngine` that returns `True` when opening sensors should be treated as expected — because the current classification recommends opening windows right now.

### The Three Conditions

All three must be true simultaneously for the check to return `True`:

| # | Condition | Details |
|---|---|---|
| 1 | `windows_recommended == True` | Classification set this flag at classification time — `warm` day (when `today_low` is low enough) or `mild` day (always) |
| 2 | Current local time is within the recommended open window | `warm`: 6:00 AM – 10:00 AM; `mild`: 10:00 AM – 5:00 PM (constants: `WARM_WINDOW_OPEN_HOUR`, `WARM_WINDOW_CLOSE_HOUR`, `MILD_WINDOW_OPEN_HOUR`, `MILD_WINDOW_CLOSE_HOUR`) |
| 3 | HVAC mode is `off` | The classification itself set HVAC to `off` for warm/mild days — if HVAC is running (e.g. classification changed to cool/heat), normal pause rules apply |

### What It Suppresses

When `_is_within_planned_window_period()` returns `True`, the following are suppressed:

- **Pause** — `handle_door_window_open()` logs "not pausing (windows recommended)" and returns without pausing
- **Re-pause after grace expiry** — `_grace_expired()` and `_re_pause_for_open_sensor()` clear grace and return without re-pausing
- **Duplicate open notifications** — no notification is sent when the open sensor is expected

### Where It Is Checked

| Call site | Purpose |
|---|---|
| `handle_door_window_open()` | Blocks initial pause when sensor opens |
| `_grace_expired()` | Blocks re-pause when grace timer fires with sensor still open |
| `_re_pause_for_open_sensor()` | Blocks re-pause called from the grace expiry path |
| `_compute_automation_status()` | Returns `"windows open (as planned)"` instead of a pause/warning status |
| `_compute_next_automation_action()` | Returns `"Windows open as recommended"` in the next-action field |

---

## 17. Natural Ventilation

### Philosophy

Natural ventilation is the cheap path. When outdoor air is cooler than indoor air, pulling it through an open door or window moves heat out of the house at zero energy cost. Running the HVAC system to achieve the same result burns electricity or gas. Climate Advisor treats outdoor air as a free resource to be used whenever three conditions are simultaneously true: the airflow is directionally beneficial, the house has not yet reached the comfort floor, and the outdoor air is not too warm to be useful. When any of those conditions fails, the system either suspends ventilation (if outdoor conditions have temporarily turned unfavorable) or restores heating (if the comfort floor has been reached). HVAC resumes only when outdoor air stops being the better option.

### Activation Conditions

All four must be true simultaneously for natural ventilation to activate.

| Condition | Guard | Rationale |
|---|---|---|
| `outdoor_temp < indoor_temp` | Directional — outdoor must be cooler than indoor | Pulling in warmer air heats the house instead of cooling it; nat vent would work against the goal |
| `indoor_temp > comfort_heat` | Floor guard | If indoor is already at or below the comfort floor, nat vent would immediately trigger a comfort-floor exit — no benefit from activating first |
| `outdoor_temp < comfort_cool + nat_vent_delta` | Ceiling | Outdoor air too warm (even for transitional cooling) should not enter; `nat_vent_delta` provides a configurable tolerance band above `comfort_cool` |
| At least one door/window sensor open | Physical prerequisite | Natural ventilation requires an open path for airflow |

When all conditions are met: the comfort band **stays armed** (HVAC is **not** set to `off` — Issue #249; the thermostat self-arbitrates with the open window), the fan is activated (per the configured `fan_mode`), and `_natural_vent_active` is set to `True`. Activation is gated on **fan configuration + temperature, not occupancy** — a configured fan is the user's opt-in to fan-assisted ventilation, so nat-vent runs for free cooling home or away (#231 handles the comfort-ceiling exit so an empty home is not over-cooled); a user opts out of nat-vent by not configuring a fan.

### Exit Hierarchy

Exit conditions are evaluated in priority order on every continuous-monitoring check (`check_natural_vent_conditions()`). The highest-priority matching condition wins.

| Priority | Trigger | Condition | Action | Event emitted | Notes |
|---|---|---|---|---|---|
| 1 | All monitored sensors close | — | Exit nat vent; resume HVAC from current classification | — | — |
| 2 | `indoor_temp ≤ comfort_heat` (daytime) or `indoor_temp ≤ sleep_heat − hysteresis` (sleep window) | — | Exit; restore heat mode at the applicable floor temperature | `nat_vent_comfort_floor_exit` | Sleep-window variant: `sleep_heat − hysteresis` is one step below the cycling-off threshold, so the session ends only after the fan has already paused — see Fan Cycling section below |
| 3 | `outdoor_temp ≥ indoor_temp` | — | Exit to paused state; fan off; start hysteresis lockout timer | `nat_vent_outdoor_rise_exit` | — |
| 4 | `outdoor_temp > comfort_cool + nat_vent_delta` | — | Exit to paused state; fan off | — | — |

**Priority 1 (sensor closes)** always wins. When the physical path for airflow is closed, nat vent ends immediately regardless of outdoor temperature comparisons. This priority's actual implementation is `handle_all_doors_windows_closed()`, a separate sensor-close listener — **not** `check_natural_vent_conditions()`. Until Issue #418, it did not route through `_exit_nat_vent()` (see below) at all; it hand-rolled a classification-aware immediate re-arm (comfort band for warm/mild days, direct mode restore for hot days) that finished before returning. Issue #418 unified it with the other exit sites, trading that instant re-arm for the same eventual state reached via the generic restore + grace period (`_apply_current_scheduled_state()` → `apply_classification()`, up to `DEFAULT_AUTOMATION_GRACE_SECONDS` = 5 min later) — an accepted, deliberate tradeoff for full unification, not a correctness gap.

**Priority 2 (comfort floor)** restores heat rather than simply pausing. During the daytime, the exit fires at `comfort_heat`; during the sleep window it fires at `sleep_heat − hysteresis` (one step below the cycling-off threshold). In both cases the right action is to restore heat, not wait for outdoor conditions to change. The sleep-window threshold is deliberately set below the cycling-off point so the fan can complete a graceful pause at `sleep_heat` before the session ends.

**Priority 3 (outdoor warms above indoor)** starts a hysteresis lockout timer (see Re-activation section below). Without this lockout, the system would oscillate at thermal equilibrium: outdoor rises above indoor → exit → cooling resumes → outdoor drops below indoor → re-activate → repeat.

**Duplicate floor check — keep in sync (Issue #402):** `fan_thermostat_check()` (called on every thermostat temperature tick, far more frequently than this 30-min-cycle function) implements its own independent copy of the Priority 2 comfort-floor exit, using the same sleep-aware formula. This exists because `fan_thermostat_check()` needs to react between classification cycles for the tick-level safety net it's designed for (Issue #327). Issue #374 updated `check_natural_vent_conditions()`'s copy to be sleep-aware but missed `fan_thermostat_check()`'s copy, which caused nat-vent to be permanently stopped at the flat `comfort_heat` floor every night regardless of sleep window — because the more-frequent tick-level check always won the race before the correct sleep-window cycling ever got a chance to run. Any future change to the Priority 2 floor formula must be applied to both functions.

**Unified exit handoff — `_exit_nat_vent()` (Issue #411, extended by Issue #418):** Every real nat-vent exit path now hands off through one function, `_exit_nat_vent(self, *, reason: str, set_outdoor_exit_time: bool = False) -> None`, instead of each call site independently deciding whether to restore HVAC or pause. `_exit_nat_vent()` clears `_natural_vent_active`, then checks the monitored sensor state itself: if a sensor is still open, it pauses (`_deactivate_fan(restore_hvac=False)`, sets `_paused_by_door=True`, captures `_pre_pause_mode`) rather than restoring HVAC into an open window; if sensors are closed, it restores HVAC (`_deactivate_fan(restore_hvac=True)`, the default) and starts a grace period. Only outdoor-reversal call sites pass `set_outdoor_exit_time=True`, since only that lockout timer (`_nat_vent_outdoor_exit_time`, consumed by the reactivation gate below) depends on it.

Call sites unified so far:
- Priority 2 comfort-floor hard exit, Priority 3 outdoor-rise exit (`check_natural_vent_conditions()`), and `apply_classification()`'s proactive/predictive floor exit — unified in Issue #411. Before that fix, the proactive/predictive exit hand-rolled its own restore (`_set_hvac_mode(c.hvac_mode)` using the day classification's mode, regardless of sensor state) on top of `_deactivate_fan()`'s already-correct restore, and never checked whether a monitored door/window sensor was still open — producing a contradictory log narrative (nat-vent "exiting" and re-entering repeatedly for hours) when a window stayed open overnight.
- Priority 1 sensor-all-close (`handle_all_doors_windows_closed()`) and the fast-loop mirror of the Priority 3 outdoor-rise exit inside `fan_thermostat_check()`'s Check 1 — unified in Issue #418. The fast-loop site had a live bug before this fix: it set `_paused_by_door=True` but called `_deactivate_fan()` with the default `restore_hvac=True`, restoring HVAC into a window it had just marked as "still open" — the exact contradiction `_exit_nat_vent()` exists to prevent — and never captured `_pre_pause_mode` or checked whether a sensor was genuinely still open. The sensor-all-close site's tradeoff is described above (Priority 1 note).

Away-mode ceiling exit is intentionally **not** routed through `_exit_nat_vent()` — it has no pause/grace state machine and remains a separate, simpler path by design.

### Re-activation from Pause

When nat vent has exited due to an outdoor-warm event (Priority 2 above), re-activation requires all three of the following simultaneously:

| Condition | Value | Rationale |
|---|---|---|
| `outdoor_temp < indoor_temp - 1.0°F` | 1°F hysteresis band | Prevents immediate re-activation when temperatures are nearly equal; outdoor must be meaningfully cooler |
| Time elapsed since last outdoor-warm exit ≥ 300 seconds | 5-minute lockout | Prevents oscillation when outdoor and indoor temperatures are at near-equilibrium; gives thermal conditions time to settle |
| `outdoor_temp < comfort_cool + nat_vent_delta` | Ceiling still valid | Ensures outdoor air is still within the useful temperature range |

If all three conditions are met, nat vent re-activates: HVAC remains off, fan turns on, `_natural_vent_active` is set back to `True`.

#### Archetype-aware reactivation gate (Issue #392 Fix 1; unified into `_nat_vent_may_reactivate()` in Issue #411) — cross-reference §6c and §9

The re-activation condition table above is the primary, direction/floor/ceiling-delta gate. As of Issue #392, all call sites that (re)activate nat-vent additionally require the **archetype-aware ceiling condition** from §6c — `self._ceiling_threshold(comfort_cool) is None OR indoor <= ceiling_threshold` — before proceeding, mirroring the ODE ceiling guard's own dormancy check so the guard and the reactivation gates can no longer disagree with each other. For `FAN_MODE_HVAC`, this blocks reactivation once indoor is already past the ceiling (same behavior as before Issue #392). For `FAN_MODE_WHOLE_HOUSE`/`FAN_MODE_BOTH`, `_ceiling_threshold()` returns `None`, so this condition is always satisfied and reactivation is governed purely by the direction/floor/ceiling-delta gate above — a WHF keeps running (or resumes) whenever outdoor is still cooler than indoor, regardless of how far indoor has drifted above `comfort_cool`. See §9 (Structural WHF/AC Mutual Exclusion) for why this is safe: mutual exclusion with the compressor is enforced structurally, not by this gate.

**Issue #411 — one choke point, not 4 hand-copies:** these sites originally hand-copied the identical 4-part boolean gate (`outdoor < indoor − hysteresis`, `indoor > comfort_heat`, `outdoor < threshold`, and the archetype-aware ceiling check above), each kept in sync by hand across past issues — a documented prior production bug (#402: rapid escalate/reactivate oscillation with redundant thermostat writes) came from exactly this duplication drifting out of sync. `_nat_vent_may_reactivate(self, *, outdoor, indoor, comfort_heat, comfort_cool, threshold, hysteresis=0.0) -> bool` now owns only that shared boolean gate — mirroring how `_ceiling_threshold()` is already scoped as a value helper, not a whole decision. Each call site keeps its own additional guards (e.g. the door-open path's rising-outdoor-forecast check) and its own post-gate actions (starting the fan, clearing `_paused_by_door`, calling `_apply_nat_vent_hvac_state()`) — the extraction does not touch those.

**Issue #417 — sleep-aware `comfort_heat`, and a 5th site folded in:** every call site below computed `comfort_heat` as the flat `self.config.get("comfort_heat", 70)`, with no sleep-window branch — unlike `nat_vent_temperature_check()`'s own cycling thresholds and `fan_thermostat_check()`'s Priority-2 floor check (both already sleep-aware per Issue #402, see the note above). Overnight, indoor temperatures that were perfectly fine relative to the (lower) `sleep_heat` floor but sitting below the (higher) flat `comfort_heat` floor caused the reactivation gate to repeatedly reject re-entry — producing an every-~5-minute flap between `nat-vent` and `paused — door/window open` while the window never actually changed state. All 5 call sites (the 4 below plus `reconcile_fan_on_startup()`, previously a 5th hand-rolled copy of this same gate that had never been folded into `_nat_vent_may_reactivate()` at all) now compute their floor via `_nat_vent_reactivation_floor()`, which returns `sleep_heat` during the sleep window and `comfort_heat` otherwise — mirroring the pattern already used correctly for the exit-hierarchy floor.

**Issue #427 — the exit-side proactive floor check had the same bug, one cycle later:** Issue #417 fixed the 6 *reactivation/entry* call sites above, but `check_natural_vent_conditions()`'s Phase 2 "proactive floor exit" — a *different* mechanism that predicts an imminent floor crossing from the thermal model's `k_passive` and pre-emptively ends the session (see Phase 2 Note below) — still read the flat `self.config.get("comfort_heat", 70)` directly, never migrated to `_nat_vent_reactivation_floor()`. During the sleep window, indoor sitting between `sleep_heat` and the flat `comfort_heat` made Phase 2's `time_to_floor` calculation go negative (the floor already "breached" by the wrong, higher reference point) hours before the real (sleep-aware) floor was anywhere close. Because `time_to_floor < MIN_VIABLE_NAT_VENT_HOURS` is true for any negative value, this fired every ~5 min all night, fully tearing down the session (`_exit_nat_vent()`) each time. The physical fan kept running independently in the meantime (thermostat circulation, or hardware stop lag), which the coordinator's untracked-fan detection then re-adopted as a **brand-new** session via `reconcile_fan_on_startup()` — which Phase 2 immediately re-exited on the very next tick, for a continuous multi-hour exit/untracked/reconcile-adopt loop (as opposed to #417's every-~5-min entry-side flap, which never got past a single paused/reactivate cycle). Fixed by routing Phase 2's floor read through the same `_nat_vent_reactivation_floor()` helper, plus guarding the block to only fire when `time_to_floor >= 0` (a negative value means the floor is already at/below indoor right now — not a *prediction* — so it correctly falls through to Priority 2's hard exit or `nat_vent_temperature_check()`'s in-session cycling instead).

**Issue #775 — the reactivation floor matched raw `comfort_heat`, not the daytime cycling band a live session already holds:** production incident 2026-08-29 — WHF reactivated at 08:15 with indoor=69°F, outdoor=63°F, comfort_heat=68°F, comfort_cool=72°F. The reactivation gate's own log line read `indoor > comfort_heat 68.0°F` — literally true (69 > 68), but looser than the ~69-71°F daytime cycling band `nat_vent_temperature_check()`'s own `off_threshold` (`(comfort_heat+comfort_cool)/2 - hysteresis`) already holds a *live* session to. A prior fix (Issue #696) closed a related-looking but distinct gap — a reactivation-lockout timer that wasn't consulted at all on one FSM path — and this incident confirmed that fix is still intact (the lockout ran correctly here); the remaining gap was the threshold *value* used once the lockout has already cleared. Fixed by changing `_nat_vent_reactivation_floor()`'s daytime branch from raw `comfort_heat` to the same `off_threshold` formula, so a stopped session can't restart any lower than a continuously-running one would have cycled off at. The sleep-window branch (`sleep_heat`) was already correct — it already equals the live session's own sleep-window `off_threshold`, which is why this gap only showed up during the day.

Scoped deliberately to *reactivation only*, not first-time daily activation: tightening the shared FSM entry gate universally was tried first and broke two existing golden regression tests (`733_restart_reconcile_clobbers_fresh_nat_vent`, `issue-359-fan-state-machine`) whose indoor values sit between the raw floor and the tightened one for a session that had never run yet that day — there is no live cycling band to protect on a fresh activation, so holding it to the tighter threshold was an unwanted side effect, not the reported bug. The fix is scoped via a new `apply_reactivation_floor` parameter on `_build_nat_vent_fsm_inputs()` (`automation.py`), set `True` only at the 3 call sites genuinely re-arming a session that already ran and exited — `check_natural_vent_conditions()`'s idle-open re-entry check, its paused-by-door reactivation block, and `_re_pause_for_open_sensor()` — left `False` (default, unchanged raw-floor behavior) at `handle_door_window_open()`'s entry gate and `reconcile_fan_on_startup()`'s adopt gate, which are genuine first-activation/adoption questions. Both changed and unchanged golden scenarios pass unmodified; see `tools/simulations/pending/issue_775_natvent_daytime_reactivation_floor.json`.

The call sites, all in `automation.py`:

| # | Function | Role |
|---|---|---|
| 1 | `handle_door_window_open()` | Sensor-open debounce callback — initial nat-vent activation |
| 2 | `check_natural_vent_conditions()` (initial gate) | Grace re-entry branch — reactivation after a grace period |
| 3 | `check_natural_vent_conditions()` (Issue #134 branch) | Comfort-ceiling re-entry check, folded into this same choke point in Issue #411 as a 4th duplicate found during the reactivation-gate extraction |
| 4 | `check_natural_vent_conditions()` (paused-by-door block) | Paused-state reactivation while `_paused_by_door=True` — corrected in Issue #417 docs from a prior mislabeling as `nat_vent_temperature_check()`, which is a different function entirely (the fan-cycling function, not a reactivation-gate caller) |
| 5 | `_re_pause_for_open_sensor()` | Re-pause-time reactivation check after grace expires with a sensor still open. Also calls `_apply_nat_vent_hvac_state()` after `_activate_fan()`, matching the other sites — this was previously the one site that skipped that call, an inconsistency independent of the ceiling logic, fixed alongside it (Issue #392). |
| 6 | `reconcile_fan_on_startup()` | Post-startup / post-hvac_action-transition reconcile (Issue #347) — previously a hand-rolled 5th copy of the gate with no sleep-awareness at all; folded into `_nat_vent_may_reactivate()` in Issue #417. Its turn-off branch now also routes through `_exit_nat_vent()` (Issue #411's choke point) instead of hand-rolling the pause/grace decision, emitting a new `nat_vent_reconcile_exit` event. |

`hysteresis` defaults to `0.0` for call sites that don't apply it (`handle_door_window_open`, `_re_pause_for_open_sensor`); the paused-state reactivation site passes the configured nat-vent hysteresis. `threshold` (the effective outdoor ceiling, typically `comfort_cool + nat_vent_delta`) is computed by each caller and passed in, keeping `_nat_vent_may_reactivate()` a pure boolean gate with no recomputation of its own.

The ODE ceiling guard's own dormancy check (§6c, a different decision — "escalate to AC," not "start nat-vent") intentionally still calls `_ceiling_threshold()` directly rather than `_nat_vent_may_reactivate()` — only the ceiling sub-condition is shared with it, not the full 4-part reactivation gate.

All are wrapped in `self._decision_lock` (§9g) as part of Issue #392 Fix 3, so a reactivation decision from any one of them cannot interleave with a decision from `apply_classification()` or another locked entry point.

**`apply_classification()` short-circuits for WHF (Issue #392 Fix 1b)** — see §9 (Structural WHF/AC Mutual Exclusion) for the full mechanism. In summary: when `_natural_vent_active` is `True` and `aggressive_savings` is `False` (the default), `apply_classification()` used to fall through to `_apply_comfort_band()` regardless of fan archetype, re-arming `cool` on the thermostat every 30-minute cycle even while a WHF session was actively suppressing HVAC. It now returns immediately after `_apply_nat_vent_hvac_state()` when `fan_mode` is `FAN_MODE_WHOLE_HOUSE`/`FAN_MODE_BOTH`, so the classification cycle never attempts a comfort-band write that the choke-point guard would otherwise silently block. `FAN_MODE_HVAC` keeps falling through unchanged, since fan and compressor coexist for that archetype.

**Issue #620 — `_idle_open` must respect an active grace period:** `check_natural_vent_conditions()`'s outer guard (line ~3021, distinct from the `_nat_vent_may_reactivate()` call-site table above — this is what decides whether to *enter* that block at all) computes `_idle_open = self._any_monitored_sensor_open() and _hvac_off_244 and not self._sensor_debounce_pending`, then proceeds if `(self._grace_active and indoor > comfort_cool) or _idle_open`. The first clause (Issue #134) is a deliberate, narrow exception letting overheating re-engage nat-vent even during grace; `_idle_open` (Issue #244/#402/#504) had no grace check at all, letting a fan-off grace (which `docs/grace-periods-spec.md` documents as unconditionally gating re-activation) be bypassed by this one path. Confirmed live 2026-08-11: a user turning the WHF off had it reactivated 5 seconds later via this exact gate. Fixed by adding `and not self._grace_active` to `_idle_open`'s definition — the Issue #134 clause is unaffected.

**Issue #620 — `fan_thermostat_check()`'s two other stop outcomes never checked live sensor state before restoring HVAC:** this function (the fast, tick-level thermostatic-backstop check, distinct from `check_natural_vent_conditions()`) has three stop outcomes. `STOP_VIA_NAT_VENT_EXIT` was already routed through `_exit_nat_vent()` (Issue #411's single choke point) by Issue #418, specifically because the un-migrated version "set `_paused_by_door=True` while still restoring HVAC via `_deactivate_fan()`'s default `restore_hvac=True`, contradicting the pause semantics." Its two siblings, `STOP_DEACTIVATE` ("free cooling gone", outdoor≥indoor) and `STOP_COOLED_TO_FLOOR` (comfort floor reached — the exact trigger in the 2026-08-11 incident), were never migrated and still had that identical bug. Both now route through `_exit_nat_vent()` too, with event emission made conditional on whether a genuine nat-vent session was active (these two outcomes, unlike `STOP_VIA_NAT_VENT_EXIT`, can also fire for a non-nat-vent CA fan, e.g. a min-runtime cycle) to avoid mislabeling that case's Activity Record entry — see `tests/test_fan_control.py::TestFanThermostatCheck::test_stops_non_natvent_fan_without_natvent_event`.

**Issue #620 — routine comfort-restore writes never checked live sensor state at all:** `decide_scheduled_band_gate()` (Issue #498, §9c above) only ever saw `_paused_by_door` as an input — a flag set exclusively by event-driven paths (`handle_door_window_open()` on a fresh sensor-open event, `_exit_nat_vent()`'s sensor-open branch). A sensor that had been open since *before* either of those ever ran (this incident's exact shape — open since bedtime, no fresh open event in the window) left `_paused_by_door=False` forever, so `apply_classification()`/`handle_bedtime()`/`handle_morning_wakeup()`/`handle_pre_cool()` — all 4 of `decide_scheduled_band_gate()`'s callers — could write an active HVAC mode into an open window with zero live check. Fixed with a new helper, `_sync_paused_by_door_with_live_sensors()` (automation.py), called at the top of all 4 functions before they build the gate's inputs: if a monitored sensor is open, debounce-settled (reusing the same `_sensor_debounce_pending` property `_idle_open` uses — not a new debounce mechanism), and not already claimed by nat-vent/WHF/a planned-window period, it calls the existing `_pause_for_door_window()` (Issue #523) to set `_paused_by_door=True` before the gate is evaluated. No change to `decide_scheduled_band_gate()` itself — once the flag is truthful, its existing `DEFER_PAUSED` handling takes over unchanged. See `issue_620_routine_classification_pauses_for_already_open_sensor` pending scenario.

**Issue #623 — the `_sensor_debounce_pending` signal Fix A introduced a regression via reuse:** the property both `_idle_open` and `_sync_paused_by_door_with_live_sensors()` share delegates to a coordinator callback that used to be `lambda: bool(self._door_open_timers)` — true only once `_async_door_window_changed()` (the `state_changed` event listener) has registered a debounce timer for the transition. But `_any_monitored_sensor_open()` reads `hass.states.get(entity_id).state` directly, reflecting a fresh open the instant HA's state machine commits it — independent of when the event loop schedules our listener coroutine. A concurrent, unrelated coordinator refresh cycle could reach `apply_classification()` before the listener ran, observe "open, no timer registered" and misread a *just-opened, still-transient* sensor as *settled*, bypassing the debounce window entirely. Confirmed live 2026-08-11: a user exiting through a door got an instant pause notification, and the log showed the pause fire 5ms before "debounce started" was logged for the same transition. Fixed in `coordinator.py`'s `_sensor_debounce_pending()` (now a bound method, not a lambda) by also checking each open sensor's HA-authoritative `state.last_changed` timestamp against `CONF_SENSOR_DEBOUNCE` — a signal immune to listener scheduling order. Five-whys root cause: `_door_open_timers` membership was validated only for its original caller (`_idle_open`, Issue #504); Issue #620 reused the same signal in a new caller context without re-verifying the "registration precedes evaluation" ordering assumption still held there — a caution for any future reuse of a timing-dependent shared signal. See `issue_623_debounce_race_transient_open_not_paused` pending scenario (revert-tested against the pre-fix lambda).

**Issue #629 — `_apply_comfort_band()` had no independent check of its own, and `_set_temperature()`'s bundled mode was invisible in logs:** Issues #620/#623 above both fixed how `_paused_by_door` gets *set* — but `_apply_comfort_band()` (the single function `apply_classification()`/`handle_bedtime()`/`handle_morning_wakeup()`/`handle_pre_cool()`/`handle_occupancy_away()`/`handle_occupancy_vacation()`/the post-fan-off reassert path all funnel through to actually write a mode) never re-checked live window state itself — it trusted the gate computed once at the top of the caller to still be accurate by the time it ran. A coordinator refresh that fires `apply_classification()` in the same tick nat-vent releases ownership can reach `_apply_comfort_band()` with `_paused_by_door` still `False` (correct at the moment it was checked — the thermostat genuinely was still off then) but a window still genuinely open, and `select_comfort_band()`'s two-way ternary (`active = "floor" if hvac_mode == "heat" else "ceiling"`, deliberate for the Issue #249 "lazy comfort band" safety net) treats an "off"-day classification the same as "cool" for edge selection — so the routine band-arm silently commands an active mode through the still-open window via `_set_temperature()`'s bundled `hvac_mode` parameter (Issue #301), with no `set_hvac_mode` call and no log line even naming the mode. Confirmed live 2026-08-13: WHF turned off at 06:13:44.097, `apply_classification()` fired 9ms later via the coordinator's `async_request_refresh()`, and `_set_temperature(mode="cool")` committed 14ms after that — `_paused_by_door` stayed `False` throughout because nothing re-checked it after the initial gate. Fixed by a structural choke-point guard directly in `_apply_comfort_band()` — mirroring the WHF/AC mutex choke-point already inside `_set_hvac_mode()` (Issue #392 Fix 1b) — that refuses to arm an active mode whenever a monitored sensor is genuinely (debounce-settled) open, reusing the existing `_pause_for_door_window()` machinery instead of inventing a parallel one. Exempted while nat-vent/WHF genuinely owns HVAC: `decide_scheduled_band_gate()` checks occupancy *before* nat-vent (Issue #498), so `handle_occupancy_away()`/`handle_occupancy_vacation()` legitimately arm a wide, usually-inert setback band while an active nat-vent session continues to own real HVAC behavior — confirmed against the `away_natvent_exits_at_comfort_ceiling`/`away_with_active_natvent_transition`/`bedtime_natvent_continuation` golden scenarios, all of which regressed without this exemption. `_set_temperature()`'s log line now includes `mode=` so a mode-changing write is visible without cross-referencing `_async_thermostat_changed` timing. See `issue_629_comfort_band_arm_through_open_window` golden scenario (revert-tested — fails on unfixed code with the exact `Set temperature to 74°F` / no-mode-logged signature this incident showed).

**Issue #645 — a restart-caused sensor reconnect blip fed a false-fresh `last_changed` into `_sensor_debounce_pending()`, defeating the Issue #629 choke-point guard:** a monitored group/helper contact-sensor entity blips `unavailable → on` during HA startup — confirmed via live `tools/ha_logs.py --history` REST data: `binary_sensor.group_contact_sensors_climate_advisor` transitioned `unavailable → on` at 07:54:53 → 07:56:05 local, precisely bracketing a 07:55:00 redeploy — which stamps a fresh `last_changed` on a window that had physically been open for hours, exactly like a genuine open would. `_sensor_debounce_pending()`'s `last_changed` fallback (Issue #623, above) then reads "pending" for up to `CONF_SENSOR_DEBOUNCE` (default 600s/10 min) even though nothing physically changed, and both `_apply_comfort_band()`'s choke-point guard and `_sync_paused_by_door_with_live_sensors()` (Issue #620) treat "pending" as a reason to *skip* the pause rather than to be *more* cautious. Confirmed live 2026-08-15: `apply_classification()` logged `wants='off', thermostat='off'` immediately followed by `Set temperature to 74°F (mode=cool)` at 08:00:11, 10 seconds after the post-restart startup-coalescing window closed at 08:00:01 — `hvac_action` stayed `idle` throughout (indoor 69°F was below the 74°F setpoint), so no energy was wasted this occurrence, but the safety check that should have caught it did not run. **First fix attempt (reverted):** dropping the `not self._sensor_debounce_pending` clause entirely from both arm-blocking guards — this broke the `issue_623_debounce_race_transient_open_not_paused` golden scenario, since debounce exists precisely to avoid an instant pause on a genuinely brief door-open, not just to protect against restart artifacts; blanket-removing it reintroduced that nuisance-pause regression. **Actual fix:** `coordinator._async_door_window_changed()` now inspects `old_state` when a monitored sensor reads open — if `old_state.state` is `"unavailable"`/`"unknown"` (a reconnect, not a genuine transition; `old_state is None` is deliberately *not* treated the same way, since that's not a confirmed blip signature), it records that specific `last_changed` value in a new `_sensor_reconnect_blip_last_changed` dict and skips debounce-timer registration entirely. `_sensor_debounce_pending()` excludes any `last_changed` value recorded as a known blip for that sensor — a *later* genuine off→on transition moves `last_changed` again, so this exclusion can never mask a real open. The two arm-blocking guards in `automation.py` are unmodified; they simply now read a correct signal. `_idle_open`'s debounce check (Issue #244/#402/#504, reactivation-widening, described below) was never touched — "wait and see" is the correct default for *reactivating* a fan regardless of blip-vs-genuine, so it doesn't need this distinction. Discovered in the same investigation: the sim harness's `simulate_restart` event (Issue #627) reuses the same live `AutomationEngine` instance across a simulated restart rather than constructing a fresh one, so `_paused_by_door`/`_pre_pause_mode`/`_natural_vent_active` (which `restore_state()` relies on a fresh `__init__` to zero, per its own docstring) were silently leaking across simulated restarts — `simulate_restart` now also explicitly resets those fields to match the real clean-slate guarantee. See `tests/test_sensor_reconnect_blip.py` and `issue_645_restart_debounce_bypass_open_window` pending scenario (both revert-tested — fail on unfixed code with the exact `Set temperature to 74°F (mode=cool)` signature this incident showed; the golden `issue_623_debounce_race_transient_open_not_paused` scenario continues to pass unmodified, confirming the fix is scoped to reconnect blips only).

### `natural_vent_delta` Semantics

`natural_vent_delta` is a ceiling tolerance: the number of degrees above `comfort_cool` that outdoor air is still considered acceptable for natural ventilation. The effective outdoor temperature ceiling is `comfort_cool + natural_vent_delta`.

**Worked example:** indoor = 78°F, outdoor = 74°F, comfort_heat = 70°F, comfort_cool = 72°F, delta = 3°F.

- Ceiling threshold = 72 + 3 = **75°F**
- `outdoor (74) < indoor (78)` ✓ — airflow is directionally beneficial
- `indoor (78) > comfort_heat (70)` ✓ — above comfort floor
- `outdoor (74) < ceiling (75)` ✓ — outdoor is within the useful range

All conditions met → natural ventilation activates.

If outdoor were 76°F instead, the ceiling check would fail (`76 ≥ 75`) and nat vent would not activate despite outdoor still being cooler than indoor.

Default value: `NAT_VENT_DELTA_DEFAULT = 3°F` (see §15 Defaults Reference).

### Soft-Start Sub-Mode (Issue #540, scoped from #533)

**Occupant impact:** without soft-start, the whole-house fan sits idle for the entire
approach-to-parity window in the evening — even once the home has clearly passed its
daily peak and outdoor air is falling toward indoor — because every other activation
path requires outdoor to be *measurably* cooler than indoor (see Activation Conditions
and Re-activation from Pause above). Soft-start closes that gap: it lets the WHF start
at outdoor/indoor **parity** for air-movement comfort and attic/thermal-mass purge,
distinct from bulk free-cooling.

**Opt-out, on by default** (`nat_vent_soft_start_enabled` config key). Issue #533
recommended opt-in given the comfort benefit is subjective and unverified by any
humidity/dew-point sensor (none exists in the integration today — tracked as an explicit
gap, not solved here); the project chose to default this on instead, so every WHF install
gets the benefit unless the user explicitly disables it in settings.

**Gate** (`decide_nat_vent_soft_start_gate()` in `nat_vent_gate.py`), all of the
following must hold:

| Condition | Rationale |
|---|---|
| `fan_mode in (whole_house_fan, both)` | The attic-purge claim is WHF-specific; HVAC-only fan archetypes don't qualify for v1 (a general comfort-fan mode is a separate, larger decision — see Related Condition (C) in Issue #533) |
| At least one door/window sensor open | Same physical prerequisite as the full gate |
| `indoor > comfort_heat` | Same floor guard as the full gate |
| `indoor > comfort_cool` | Still warm enough that purge/air-movement has value |
| `outdoor_sample_count >= 3` AND `outdoor < today's observed peak − PEAK_DECLINE_MARGIN_F (1.0°F)` | "Past peak and declining" — built from `coordinator._outdoor_temp_history` (already sampled every 30 min for forecast high/low correction), not a new sensor. The minimum-sample guard fails closed after a restart that crosses local midnight, when the history buffer is briefly thin (see Timezone/Day-Boundary note below) — it doesn't risk a false "already past peak" read from 0-2 samples |
| `outdoor <= indoor` | Parity, not the full gate's `outdoor < indoor − hysteresis` — this is the core relaxation |
| `not decide_nat_vent_gate(...)` for the same inputs | Soft-start only fires in the gap *before* the full bulk-cooling gate would already apply — the two gates never compete for the same activation |

**Qualifier flag, not a second session:** soft-start sets `_nat_vent_soft_start = True`
alongside `_natural_vent_active = True` — it is a qualifying sub-flag on the *same*
session (mirroring how `_grace_protects_override` coexists with `_grace_active`), not a
parallel state machine. It reuses the existing fan-activation machinery, HVAC-band
arming, and — critically — the existing Exit Hierarchy above completely unchanged: an
outdoor-rise exit, comfort-floor exit, ceiling exit, etc. all end a soft-start session
exactly the same way they end a full one, clearing both flags together.

**Upgrade path:** every cycle a soft-start session is active, the engine re-checks
whether the full bulk-cooling gate (`decide_nat_vent_gate()`) now independently holds. If
it does, `_nat_vent_soft_start` clears (logged as an upgrade) — the fan keeps running
uninterrupted; only the status label changes from soft-start to full nat-vent. There is
no downgrade path (full → soft-start): once the full gate has been satisfied, exiting and
re-entering soft-start on a later temperature dip is intentionally out of scope for v1.

**Status/logging:** surfaced via the existing Status card (`_compute_automation_status()`
returns `"nat-vent — soft-start (purge)"` instead of `"nat-vent"` — no new card, per the
Status Card Ontology in the root `CLAUDE.md`). Entry is logged at INFO and emits
`nat_vent_soft_start_entered` (rendered in the Activity Report via
`ai_skills_context.py`'s `EVENT_RENDERERS`).

**Timezone/day-boundary note:** `coordinator._outdoor_temp_history` is written and
cleared entirely in HA local time (`dt_util.now()`, `async_track_time_change` at
23:59:00 local) and restored on restart only when the persisted date matches today's
local calendar date — consistent with the Issue #190 local-date convention used
elsewhere in `coordinator.py`. The one edge case is an overnight outage that crosses
local midnight: the buffer starts thin until the next 30-min cycle, which is exactly
what the `outdoor_sample_count >= 3` guard protects against.

**Test coverage:** `tests/test_nat_vent_gate.py::TestDecideNatVentSoftStartGate` (pure
gate logic); `tests/test_nat_vent_soft_start.py` (engine-level entry, precedence over the
full gate, HVAC-only fan exclusion, thin-buffer fail-safe, upgrade, and exit-hierarchy
reuse).

### Fan Cycling Within an Active Session (Issues #321, #374)

Once `_natural_vent_active = True`, the fan does not simply stay on until the session ends. Instead, the engine targets a context-dependent temperature and cycles the fan on and off using a hysteresis band to prevent rapid toggling. The target and thresholds differ between the daytime and sleep windows.

**Target and threshold table** (constant `NAT_VENT_HYSTERESIS_F = 1.0°F`):

| Context | `nat_vent_target` | Fan cycles OFF (`off_threshold`) | Fan cycles ON (`on_threshold`) | Hard-exit floor |
|---|---|---|---|---|
| Daytime | `(comfort_heat + comfort_cool) / 2` | `target − hysteresis` | `target + hysteresis` | `comfort_heat` |
| Sleep window | `sleep_heat + hysteresis` | `sleep_heat` (= `target − hysteresis`) | `sleep_heat + 2 × hysteresis` (= `target + hysteresis`) | `sleep_heat − hysteresis` |

*Sleep-window note:* The sleep target is the sleep floor plus one hysteresis step, so the fan cools the home to `sleep_heat` (cycling off there) and then maintains it by re-activating at `sleep_heat + 2 × hysteresis`. The hard-exit threshold (`sleep_heat − hysteresis`) sits one step below the cycling-off point, so the session ends only if indoor temperature falls past `sleep_heat` — i.e., after the fan has already paused.

*Dashboard display note (Issue #415):* `nat_vent_target` is used to compute the cycling band shown on the Status card, but is never embedded as a number in the `automation_status` string itself. `automation_status` is cached for up to `update_interval` (30 min), while `api.py` recomputes `compute_nat_vent_cycling_band()` live on every dashboard poll — so a number embedded in the cached string can drift from the live band across a sleep-window boundary. Do not reintroduce a numeric target into `automation_status`; the live cycling-band line is the sole place this temperature is displayed.

**Fan cycles off (indoor ≤ off_threshold):**
- `_fan_active` is set to `False`; fan deactivated.
- `_natural_vent_active` remains `True` — the session is still active.
- `fan_status` sensor reports `"nat-vent (session active, fan idle)"`.
- The comfort band stays armed throughout; the thermostat continues to self-arbitrate.

**Fan cycles on again (indoor ≥ on_threshold):**
- Fan reactivates if `outdoor_temp < indoor_temp` (directional check still applies).
- The on_threshold guard prevents re-activation the moment the fan turns off (1°F dead band).
- **Sensor re-validation gate (Issue #561, `FAN_MODE_WHOLE_HOUSE`/`FAN_MODE_BOTH` only):** before
  reactivating, `nat_vent_temperature_check()` now calls
  `AutomationEngine._any_monitored_sensor_open()` — the single choke point for "is a monitored
  door/window sensor currently open" (also used by `_exit_nat_vent()`, the idle-open reactivation
  gate, and `resume_from_pause()`). If no sensor is open, the session is force-closed via
  `_exit_nat_vent()` (with a WARNING log) instead of cycling the fan on. `_natural_vent_active` is
  otherwise trusted as a proxy for "windows are open," and this closes the gap where that proxy
  could go stale — e.g. `_reconcile_fan_physical_drift()`'s `CORRECT` outcome preserving the
  session (see §E below) after windows had already closed. Not applied to `FAN_MODE_HVAC`: its
  fan-only mode has no separate physical-exterior-airflow requirement (it's the thermostat's own
  blower, not an exhaust fan), and its Issue #134 reactivation path is intentionally allowed to
  re-engage without an open sensor.
- **Outdoor-freshness contract (Issue #561):** `nat_vent_temperature_check()` takes `outdoor` as a
  required keyword-only parameter (mirroring `fan_thermostat_check()`'s existing convention)
  instead of reading `self._last_outdoor_temp` internally. Both real callers
  (`coordinator._async_thermostat_changed`, `automation._thermo_backstop_task`) source it fresh at
  the call site — this was previously the one nat-vent gate in this module that read the cached
  attribute directly rather than receiving a caller-sourced value.

**Hard exit (session ends) — takes priority over cycling:**
The exit hierarchy (§17 Exit Hierarchy above) is evaluated before the cycling logic. Priority 2 fires first if indoor drops to the applicable floor, ending the session (`_natural_vent_active = False`) and restoring heat mode. Fan cycling cannot keep the session alive past the hard-exit floor.

**Daytime example** (comfort band [68°F, 74°F], target = 71°F):
1. Indoor = 73°F → fan on, session active.
2. Indoor falls to 70°F (= off_threshold) → fan cycles off, session stays active.
3. Indoor drifts back to 72°F (= on_threshold) → fan cycles on again.
4. Indoor falls to 68°F (= comfort_heat = hard-exit floor) → hard exit; heat mode restored.

**Sleep-window example** (sleep band [65°F, 72°F], `sleep_heat=65`, `hysteresis=1°F`):
1. Indoor = 73°F (above on_threshold 67°F) → fan on, session active.
2. Indoor falls to 65°F (= off_threshold = sleep_heat) → fan cycles off, session stays active.
3. Indoor drifts back to 67°F (= on_threshold) → fan cycles on again.
4. Indoor falls to 64°F (= sleep_heat − hysteresis = hard-exit floor) → hard exit; heat mode restored.

**Fan event `fan_device` field (Issue #374):** All fan-related events — `nat_vent_fan_on`, `nat_vent_fan_off`, `fan_activated`, `fan_deactivated`, `nat_vent_bedtime_continue` — carry a `fan_device` field indicating which hardware was activated: `"whf"`, `"hvac_fan"`, `"both"`, or `"none"`.

**Removed event:** `nat_vent_sleep_ceiling_reached` is no longer emitted. The Priority 0 exit that fired when `indoor_temp ≤ sleep_cool` during the sleep window has been removed. The session now persists through the sleep window, cycling the fan to maintain the sleep floor.

**`fan_status` sensor values** (complete list, including the value added in Issue #374):

| Value | Meaning |
|---|---|
| `"active"` | CA commanded the fan on (nat vent or HVAC fan-only mode); physical state confirmed for WHF |
| `"active (unconfirmed)"` | CA flag `_fan_active=True` but WHF physical state reads off — stale flag after manual stop; WARNING logged. Since Issue #510, only shown within the transient ~30s post-command window (`_is_recent_fan_command()`); a settled disagreement past that window returns `"inactive"` instead — ground truth wins for display once the transient window has passed. |
| `"nat-vent (session active, fan idle)"` | Nat-vent session alive but fan has cycled off (indoor at or below off_threshold). Since Issue #510, this is only returned when physical ground truth ALSO confirms the fan is off — if `_natural_vent_active` is stale but the fan is physically confirmed running, `"running (untracked)"` is returned instead (ground truth wins over the session flag). |
| `"running (manual override)"` | Fan is running; CA's `_fan_override_active` flag is set |
| `"running (untracked)"` | Thermostat reports fan running but CA's `_fan_active=False` — typical after HA restart or user-initiated fan run. Since Issue #510, also returned when a stale `_natural_vent_active` flag disagrees with confirmed-running physical state (see above). Since Issue #571, NOT returned within 30s of CA's own most recent fan command — `"inactive"` instead, since the physical/thermostat signal simply hasn't caught up to CA's own off-command yet (§9e-G). |
| `"inactive"` | Fan is off and CA has no record of activating it |
| `"off (manual override)"` | Override still in effect but physical fan is off (`_fan_override_active=True AND _fan_active=False`) |
| `"disabled"` | Fan control feature is turned off in configuration |

**Test coverage:** `tests/test_nat_vent_thermostat.py`; golden scenario `nat_vent_thermostat_cycling` (Issue #321). Sleep-window cycling behavior added in Issue #374.

### Phase 2 Note

Trajectory-aware look-ahead — using the thermal model and short-range outdoor temperature forecast to project the activation window into the future — is deferred to Issue #116.

---

## 18. Automation Logic Table

This is the definitive reference for expected system behavior across all classification contexts and sensor/user events. Every cell describes what the automation engine does when a given event fires in a given classification context.

### Classification Contexts

| Code | Day Type | HVAC Mode / Band | windows_recommended | Window Period |
|------|----------|-----------|---------------------|---------------|
| C1 | Hot | cool | False | N/A |
| C2 | Warm | band `[comfort_heat, comfort_cool]` ¹ | True | In period (6–10 AM) |
| C3 | Warm | band `[comfort_heat, comfort_cool]` ¹ | True | Outside period |
| C4 | Warm | band `[comfort_heat, comfort_cool]` ¹ | False | N/A (today_low too high) |
| C5 | Mild | band `[comfort_heat, comfort_cool]` ¹ | True | In period (10 AM – 5 PM) |
| C6 | Cool | heat | False | N/A |
| C7 | Cold | heat | False | N/A |

¹ Issue #249: warm/mild days arm a comfort band rather than setting `hvac_mode=off`. The band values shown are for home/guest occupancy; setback bands apply when away/vacation. See [§6e Comfort-Band Programming](#6e-comfort-band-programming-issue-249).

### Events

| Code | Event |
|------|-------|
| E1 | Door/window sensor opens (after debounce) |
| E2 | All door/window sensors close |
| E3 | Grace period expires with sensor still open |
| E4 | Manual HVAC override during pause |
| E5 | Fan mode change |
| E6 | Classification changes (e.g., warm→hot) |
| E7 | User clicks "Resume HVAC (override pause)" |
| E8 | HA restart — coalesce reconciliation fires (Issue #327) |
| E9 | QuietCool RF remote timer event received (Issue #486) |

### Expected Outcomes

| | E1: Sensor Open | E2: All Closed | E3: Grace+Open | E4: Override | E5: Fan Change | E6: Class Change | E7: Resume | E8: Restart Reconcile |
|---|---|---|---|---|---|---|---|---|
| C1 (hot/cool) | Pause HVAC→off, notify | Resume to cool, auto grace | Re-pause, notify | Clear pause, manual grace | Fan override grace; thermostatic loop exits fan if `outdoor ≥ indoor` (§9e) | Re-apply classification | Resume cool, manual grace | Fan adopt-on (nat-vent eligible) or turn-off (not eligible); `Fan reconcile:` logged |
| **C2 (warm/band/win=T/in)** | **No pause** (planned window); reactivation gated by archetype-aware ceiling (§6c/§17 — WHF: direction-only; HVAC-fan: blocked once indoor > ceiling) | No-op (not paused) | **No re-pause** (planned); same archetype-aware ceiling gate applies | N/A (not paused) | Fan on, band stays armed for `FAN_MODE_HVAC`; for WHF, `apply_classification()` short-circuits before the band write (Issue #392 Fix 1b, §9) | Re-apply band `[comfort_heat, comfort_cool]` (`FAN_MODE_HVAC` only — WHF short-circuits per §9); §6b backstop fires if indoor < comfort_heat | N/A (not paused) | Fan adopt-on (nat-vent eligible) or turn-off; band re-armed by coalesce |
| C3 (warm/band/win=T/out) | No pause (band armed, not paused); same archetype-aware ceiling gate | No-op | N/A | N/A | Fan on, band stays armed for `FAN_MODE_HVAC`; WHF short-circuits per §9 | Re-apply band (`FAN_MODE_HVAC` only); §6b backstop fires if indoor < comfort_heat | N/A | Fan turn-off (outside window period → not nat-vent eligible) or no-fan |
| C4 (warm/band/win=F) | No pause (band armed, not paused) | No-op | N/A | N/A | Band stays armed | Re-apply band; §6b backstop fires if indoor < comfort_heat | N/A | Fan turn-off if physically running (no sensors open → not nat-vent eligible) |
| **C5 (mild/band/win=T/in)** | **No pause** (planned window); same archetype-aware ceiling gate | No-op | **No re-pause** (planned); same gate | N/A | Fan on, band stays armed for `FAN_MODE_HVAC`; WHF short-circuits per §9 | Re-apply band `[comfort_heat, comfort_cool]` (`FAN_MODE_HVAC` only) | N/A | Fan adopt-on (nat-vent eligible) or turn-off; band re-armed |
| C6 (cool/heat) | Pause HVAC→off, notify | Resume to heat, auto grace | Re-pause, notify | Clear pause, manual grace | Fan override grace; thermostatic loop exits fan if `outdoor ≥ indoor` | Re-apply | Resume heat, manual grace | Fan turn-off (heat day → not nat-vent eligible) or no-fan |
| C7 (cold/heat) | Pause HVAC→off, notify | Resume to heat, auto grace | Re-pause, notify | Clear pause, manual grace | Fan override grace; thermostatic loop exits fan if `outdoor ≥ indoor` | Re-apply | Resume heat, manual grace | Fan turn-off (cold day → not nat-vent eligible) or no-fan |

**Bolded cells** have corresponding test coverage in `tests/test_windows_recommended_integration.py`.

**Comfort-band model (Issue #249, §6e):** In C2–C5 contexts (warm/mild days), `apply_classification()` now programs a comfort band rather than setting `hvac_mode=off`. The band arms the thermostat with both a floor and a ceiling; the thermostat self-arbitrates between them. Nat-vent and economizer activate the fan only — the band remains armed throughout, so free cooling stays free and the compressor engages only if the breeze can't hold the ceiling.

**Comfort-floor guard (§6b — passive backstop):** In C2, C3, and C4 contexts, the band floor (`comfort_heat` while home + awake; `setback_heat` away/asleep) keeps the home from falling below the floor autonomously. The `warm_day_comfort_gap` event and §6b heat-up path remain as a safety backstop for situations where the band has lapsed (HA restart, thermostat reconnect). Test coverage: `tests/test_warm_day_comfort_gap.py`.

**Thermostatic fan loop (Issue #327, §9e):** In all C1–C7 contexts, once the fan is CA-owned and running, `fan_thermostat_check()` re-evaluates on every indoor or outdoor temperature change. The fan is turned off immediately when `outdoor ≥ indoor` — it does not wait for the next 30-minute coordinator poll. See §9e for the full exit hierarchy and trigger-source table.

**Restart reconciliation (E8, Issue #327, §9e):** `_fan_override_active` is always cleared on restart; `_do_startup_coalesce` decides adopt-on, turn-off, or no-fan based on live thermostat state. E8 applies uniformly to all contexts — the decision depends on current physical conditions, not the day classification.

**RF remote timer event (E9, Issue #486, precedence corrected by Issue #748):** Applies uniformly to all C1–C7 contexts — like E8, the decision does not depend on day classification. A recognized `timer_*` token calls the same `handle_fan_manual_override()` as E5 (fan manual change), with an optional `duration_override` that makes the grace period last exactly the remote-selected duration instead of the configured `manual_grace_seconds`. While that override is active, suppression is absolute (log-only WARNING) at `_deactivate_fan()` for every *routine* automation-driven shutoff (nat-vent cycling, bedtime, comfort-floor breach, economizer-off, etc.) — E1/E2/E3/E6's fan-off outcomes in every context above are suppressed exactly as they are for any other active manual fan override. **Exception (Issue #748):** the hard AC/WHF mutex always wins over this protection — a manual HVAC-mode override detected while the WHF owns HVAC ends the session immediately regardless of the RF timer, via `_deactivate_fan(bypass_absolute_override=True)` from the single shared helper `_stand_down_whf_for_override_conflict()`. See [grace-periods-spec.md](grace-periods-spec.md#invariants) invariant 10 for the full precedence rule, and [fan-remote-spec.md](fan-remote-spec.md) for the RF event contract.

**Archetype-aware nat-vent ceiling and structural WHF/AC exclusion (Issue #392):** In C2/C3/C5, E1/E3 (reactivation) now consistently apply the archetype-aware ceiling threshold from §6c/§17 across all four reactivation gate sites (`handle_door_window_open()`, `check_natural_vent_conditions()`, `nat_vent_temperature_check()`, `_re_pause_for_open_sensor()`) — `FAN_MODE_HVAC` blocks reactivation once indoor exceeds `comfort_cool` (unchanged from before #392); `FAN_MODE_WHOLE_HOUSE`/`FAN_MODE_BOTH` reactivates purely on outdoor/indoor direction. In E5/E6, `apply_classification()` now short-circuits before the comfort-band write when a WHF session owns the thermostat (§9), and the `_whf_owns_hvac()` choke-point guard in `_set_hvac_mode()`/`_set_temperature()` (§9) makes WHF/AC mutual exclusion structural for every cell in this table, not just the ones exercised by nat-vent. All six automation entry points relevant to this table (`apply_classification`, `handle_door_window_open`, `handle_all_doors_windows_closed`, `check_natural_vent_conditions`, `_re_pause_for_open_sensor`, `nat_vent_temperature_check`) are additionally serialized by `self._decision_lock` (§9g) so that concurrent E1/E3/E5/E6 triggers cannot interleave on shared engine state.

This logic table MUST be kept current for any changes to automation behavior.

### Test Reference Mapping

| Cell | Test File | Test Name |
|------|-----------|-----------|
| C2×E1 | test_windows_recommended_integration.py | test_no_pause_when_windows_recommended_warm_day |
| C5×E1 | test_windows_recommended_integration.py | test_no_pause_when_windows_recommended_mild_day |
| C1×E1 | test_windows_recommended_integration.py | test_pause_still_fires_for_hot_day |
| C2×E1 (grace) | test_windows_recommended_integration.py | test_no_grace_when_windows_recommended |
| C2×E3 | test_windows_recommended_integration.py | test_grace_expiry_no_repause_during_window_period |
| C2→C1×E6 | test_windows_recommended_integration.py | test_classification_change_warm_to_hot_enables_pause |
| C3×E1 | test_windows_recommended_integration.py | test_pause_fires_outside_window_period_with_active_hvac |
| C2×E6 (band armed) | test_warm_day_comfort_gap.py | TestWarmDayBandArmingReplacesComfortGap — band `[comfort_heat, comfort_cool]` armed; §6b backstop only if band lapses |
| C4×E6 (band armed) | test_warm_day_setback.py | TestWarmDayBandArming::test_warm_day_dual_thermostat_sets_dual_setpoints |
| C2×E5 / C3×E5 / C5×E5 (band stays armed on nat-vent) | test_window_hvac_interaction.py, test_door_window.py | Band remains armed when fan activates; no `hvac_mode=off` issued |
| C2×E6 / C5×E6 (band applied on re-classification) | test_thermostat_program.py, test_production_harness.py | `apply_classification` arms band `[comfort_heat, comfort_cool]` (occupied+awake, any day type) |
| All×E8 (coalesce: turn-off, no nat-vent) | _(test-ref pending)_ | restart clears `_fan_override_active`; coalesce turns off fan when nat-vent not eligible |
| All×E8 (coalesce: adopt-on) | _(test-ref pending)_ | coalesce adopts running fan as CA nat-vent when conditions hold; `_natural_vent_active=True` |
| C1×E5 / C6×E5 / C7×E5 (thermostatic exit: `outdoor ≥ indoor`) | _(test-ref pending)_ | `fan_thermostat_check` turns fan off on `outdoor ≥ indoor` before next 30-min poll |
| D×E5 (economizer: `outdoor ≥ indoor` blocked) | _(test-ref pending)_ | economizer `check_window_cooling_opportunity` rejects activation when `outdoor ≥ indoor` |
| C2/C3/C5×E1/E3 (archetype-aware ceiling, `FAN_MODE_HVAC` blocks reactivation past ceiling) | test_nat_vent_activation.py, test_fan_control.py | Issue #392 Fix 1 — function names pending as of this doc pass; see files directly |
| C2/C3/C5×E1/E3 (archetype-aware ceiling, `FAN_MODE_WHOLE_HOUSE` reactivates direction-only past ceiling, incl. #392 repro sequence) | test_nat_vent_activation.py, test_whole_house_fan_hvac_suppression.py | Issue #392 Fix 1 — function names pending as of this doc pass; see files directly |
| C2/C3/C5×E5/E6 (`_whf_owns_hvac()` choke-point guard blocks active-mode writes; `apply_classification()` WHF short-circuit) | test_whole_house_fan_hvac_suppression.py, test_fan_control.py | Issue #392 Fix 1b — function names pending as of this doc pass |
| All×E1/E2/E3/E5/E6 (idempotent `_activate_fan()`/`_deactivate_fan()`; no duplicate `fan_activated`/`fan_deactivated` on redundant calls) | test_fan_control.py | Issue #392 Fix 1c — function names pending as of this doc pass |
| All×E1/E2/E3/E5/E6 (`_decision_lock` serializes the six entry points; no interleaved execution under `asyncio.gather()`) | test_nat_vent_activation.py | Issue #392 Fix 3 — function names pending as of this doc pass |
| Fan archetype activity-log labels (`fan_activated`/`fan_deactivated`/`fan_manual_override`/`fan_cancel` render `fan_device`) | test_activity_renderers.py | Issue #392 Fix 2 — function names pending as of this doc pass |
| All×E9 (`event_type` → hours parse, all timer tokens + non-timer ignored) | test_fan_remote.py | TestParseRemoteTimerEvent |
| All×E9 (`duration_override` sets exact grace duration; `timer_none`/physical path use configured default) | test_fan_remote.py | TestDurationWiring |
| C1/C6/C7×E1/E3/E5/E6 (`_deactivate_fan`/`fan_thermostat_check` suppressed + WARNING while E9 timer active) | test_fan_remote.py | TestSuppressionAbsoluteWithRemoteTimer |
| All×E9 (last-wins refresh; grace expiry clears override and resumes) | test_fan_remote.py | TestLastWinsRefresh, TestGraceExpiryResumes |
| All×E8/E9 (RF timer does not survive restart — clean-slate) | test_fan_remote.py | TestRestartCleanSlate |
| All×E9 (coordinator dispatch: event → shared `handle_fan_manual_override()`) | test_fan_remote.py | TestCoordinatorFanRemoteDispatch |
| Any C×any E, during a configured `cost_period` schedule's lead-time window (TOU scheduler, Issue #786 — orthogonal to day classification, not a new C/E code) | test_scheduler.py, test_tou_precondition.py, test_target_band.py, test_status_sensors.py, tools/simulations/pending/issue_786_*.json | Pre-conditions toward the comfort-band floor/ceiling (whichever `resolve_comfort_heat()`/`resolve_comfort_cool()` resolves) ahead of a scheduled `cost_tag="high"` window; zero new "coast" code once the window starts — see [TOU Scheduler Spec](scheduler-spec.md) |

---

## 19. Chart Activity Bar Invariants

The temperature forecast chart displays four activity bars fed by `ChartStateLog.append()` in `coordinator.py`. All four append call sites must use these helper methods — do not substitute raw thermostat state strings.

| Bar | Field name | Required source | Frontend color |
|---|---|---|---|
| HVAC | `hvac` | `_read_chart_hvac_action()` | `"heating"` → red; `"cooling"` → blue; `"fan"` → green; others → no segment |
| Fan | `fan` | `_fan_is_running()` | `true` → green |
| Windows Recommended | `windows_recommended` | `bool(self._current_classification.windows_recommended) if self._current_classification else False` | `true` → amber |
| Windows Open | `windows_open` | `self._any_sensor_open()` | `true` → green |

**Critical invariants:**
- The `hvac` field MUST be the thermostat's `hvac_action` attribute string (`"heating"`, `"cooling"`, `"fan"`, `"idle"`, `"off"`) — never the `hvac_mode` state (`"heat"`, `"cool"`). Mode strings produce invisible segments.
- Use `_read_chart_hvac_action()` at every append site. It encapsulates the #109 fan→heating/cooling remap (only applies when `fan_mode` is auto).
- Use `_fan_is_running()` for the `fan` field — never `_fan_active` directly. The helper includes ground-truth thermostat fallback for untracked fan runs.

**Four append sites in coordinator.py:**
1. Classification change event (event-driven)
2. 30-minute poll (periodic)
3. Manual override event (event-driven)
4. HVAC action transition event (event-driven)

All four sites are covered by tests in `tests/test_coordinator_chart.py`.

---

## 20. Chart Log Write Guards

### Bug A — pred_indoor gated on indoor_temp availability

`pred_indoor` and `pred_outdoor` are only written to the chart log when
`indoor_temp` (the actual sensor/climate-entity read for that coordinator tick)
is also available. If the thermostat is in `unknown` or `unavailable` state —
as occurs during an HA restart — both `indoor` and `pred_indoor` are null for
that tick. This prevents restart artifacts from permanently corrupting the
predicted indoor trend line (`histPredIndoorPts` on the dashboard chart).

The guard lives in `_async_update_data()`:

```python
if _pred_in and _now_h < len(_pred_in) and indoor_temp is not None:
    _pred_indoor_val = _pred_in[_now_h]["temp"]
```

A `DEBUG`-level log is emitted when `indoor_temp` is `None` so the skip is
visible in HA logs without cluttering normal operation.

### Bug B — plausible indoor temperature range filter

Indoor temperatures read from the thermostat or a dedicated sensor entity are
validated against a physical plausibility range defined by module-level
constants:

| Constant | Value | Meaning |
|---|---|---|
| `_MIN_PLAUSIBLE_INDOOR_F` | 40.0 °F | Below this the reading is treated as a sensor glitch |
| `_MAX_PLAUSIBLE_INDOOR_F` | 110.0 °F | Above this the reading is treated as a sensor glitch |

Values outside this range are logged at `WARNING` level and cause
`_get_indoor_temp()` to return `None` rather than propagating the bad reading
into the chart log. The most common trigger is a thermostat that briefly echoes
its new setpoint into `current_temperature` during a setpoint-only transition;
if the 30-minute coordinator tick fires at that moment, the out-of-range value
would otherwise appear as a permanent spike on the actual indoor line.

The range check applies to both the `TEMP_SOURCE_SENSOR` /
`TEMP_SOURCE_INPUT_NUMBER` branch and the `TEMP_SOURCE_CLIMATE_FALLBACK`
branch of `_get_indoor_temp()`.

### Test coverage

| Test | File |
|---|---|
| `test_pred_indoor_not_written_when_indoor_temp_none` | `tests/test_coordinator_chart.py` |
| `test_pred_indoor_written_when_indoor_temp_available` | `tests/test_coordinator_chart.py` |
| `test_indoor_temp_range_check_rejects_extreme_low` | `tests/test_coordinator_chart.py` |
| `test_indoor_temp_range_check_rejects_extreme_high` | `tests/test_coordinator_chart.py` |
| `test_indoor_temp_range_check_accepts_normal` | `tests/test_coordinator_chart.py` |

---

## 21. Thermal Learning Health

### 21.1 Overview

The thermal learning engine uses OLS regression and quality gates to ensure only reliable observations update the model. Prior to Issue #124, rejections were logged as warnings with no persistent audit trail, making it impossible to distinguish "correctly rejecting noise" from "not learning anything" without SSH access. Issue #124 adds structured rejection events and a `learning_health` surface so the model's decision process is auditable without log access.

No OLS math, automation behavior, or thermal thresholds changed in Issue #124. The only behavioral difference is that `_abandon_observation()` now logs at `INFO` level (downgraded from `WARNING`) because rejections are expected steady-state behavior, not anomalies.

### 21.2 Rejection Reason Codes

Six `REJECT_*` constants in `const.py` identify every point where an observation can be discarded. Each constant is also stored as the `reason_code` field in the `ThermalRejectionEvent` emitted at that point.

| Constant | Value | When fired |
|---|---|---|
| `REJECT_TOO_FEW_SAMPLES` | `"too_few_samples"` | Sample count < required minimum before OLS runs |
| `REJECT_SMALL_DELTA` | `"small_delta"` | Total indoor ΔT below `THERMAL_ROLLING_MIN_DELTA_T_F` (0.2°F) |
| `REJECT_OLS_BAD_FIT` | `"ols_bad_fit"` | OLS R² < `THERMAL_MIN_R_SQUARED` (0.2) |
| `REJECT_OLS_WRONG_SIGN` | `"ols_wrong_sign"` | OLS produced a positive k_passive (physics violation) |
| `REJECT_OLS_BOUNDS` | `"ols_bounds"` | k_passive outside `[THERMAL_K_PASSIVE_MIN, THERMAL_K_PASSIVE_MAX]` = `[-0.5, -0.001]` hr⁻¹ |
| `REJECT_ABANDONED` | `"abandoned"` | Observation abandoned before OLS could run (e.g., HVAC mode change, wall-clock timeout) |

### 21.3 `ThermalRejectionEvent` Fields

`ThermalRejectionEvent` is a `TypedDict` defined in `learning.py`. An instance is emitted at every rejection point and appended to the per-obs-type rejection log.

| Field | Type | Description |
|---|---|---|
| `obs_type` | `str` | Observation type that was rejected (e.g., `"passive_decay"`) |
| `reason_code` | `str` | One of the `REJECT_*` constants |
| `n_samples` | `int` | Sample count at rejection time |
| `n_required` | `int` | Minimum required for this observation type |
| `r_squared` | `float \| None` | R² achieved; `None` when OLS never ran (e.g., `too_few_samples`, `abandoned`) |
| `r_squared_required` | `float \| None` | R² floor (`THERMAL_MIN_R_SQUARED = 0.2`); `None` when OLS never ran |
| `delta_t_f` | `float \| None` | Observed indoor ΔT in °F at rejection time |
| `delta_t_required` | `float \| None` | Required ΔT floor (`THERMAL_ROLLING_MIN_DELTA_T_F = 0.2°F`) |
| `elapsed_minutes` | `int \| None` | Wall-clock duration of the observation in minutes |
| `timestamp` | `str` | ISO 8601 datetime of the rejection |

### 21.4 `compute_k_passive()` 3-Tuple Return

`compute_k_passive()` in `learning.py` previously returned a 2-tuple `(k_passive, r_squared)` — returning `(None, 0.0)` for five distinct failure modes with no way for the caller to distinguish them. Issue #124 extends the return to a 3-tuple `(k_passive, r_squared, reason_code)`:

| Failure path | k_passive | r_squared | reason_code |
|---|---|---|---|
| Too few samples (< min + 1) | `None` | `0.0` | `REJECT_TOO_FEW_SAMPLES` |
| Too few valid rate/delta pairs | `None` | `0.0` | `REJECT_TOO_FEW_SAMPLES` |
| No variation (sum_d2 == 0) | `None` | `0.0` | `REJECT_SMALL_DELTA` |
| k_passive outside bounds | `None` | `0.0` | `REJECT_OLS_BOUNDS` |
| R² < minimum | `None` | r_squared | `REJECT_OLS_BAD_FIT` |
| Success | k_passive | r_squared | `None` |

All callers in `coordinator.py` unpack the 3-tuple and use the `reason_code` to populate the `ThermalRejectionEvent` before calling `_abandon_observation()`.

### 21.5 `THERMAL_MIN_DECAY_SAMPLES` Alignment Contract

`THERMAL_MIN_DECAY_SAMPLES = 4` is the single source of truth for OLS sample-pair floors on rolling decay observations.

The coordinator pre-gates on `THERMAL_MIN_DECAY_SAMPLES + 1 = 5` pairs before calling OLS. This guarantees that at least 4 pairs are available for rate-pair construction inside `compute_k_passive()`. The inner function's own floor check (`_min_s = THERMAL_MIN_DECAY_SAMPLES`) is therefore never reached unless the outer gate logic is bypassed.

`THERMAL_MIN_POST_HEAT_SAMPLES = 10` governs HVAC post-heat events and is a separate, independent constant. Do not change either constant independently — the `+1` offset between the outer gate and the inner floor is intentional and must be preserved.

### 21.6 `learning_health` Dict in `get_thermal_model()`

`get_thermal_model()` returns a `learning_health` key containing per-obs-type health summaries aggregated from the coordinator's `_rejection_log`:

```
learning_health: {
    obs_type: {
        "attempts":   int,          # total observation starts (committed + all rejections)
        "committed":  int,          # successful commits to LearningState
        "rejections": {
            "too_few_samples": int,
            "small_delta":     int,
            "ols_bad_fit":     int,
            "ols_wrong_sign":  int,
            "ols_bounds":      int,
            "abandoned":       int,
        },
        "last_rejection": ThermalRejectionEvent | None,
    }
    for obs_type in [
        "hvac_heat", "hvac_cool", "passive_decay",
        "fan_only_decay", "ventilated_decay", "solar_gain"
    ]
}
```

The coordinator builds this dict from `self._rejection_log` and passes it to `get_thermal_model()`, which includes it verbatim in the returned dict.

### 21.7 Persistence

- `self._rejection_log: dict[str, list[dict]]` is stored on the coordinator instance, keyed by obs_type.
- Each per-obs-type list is capped at **100 entries** (oldest evicted first when the cap is reached).
- Maximum total stored: **600 entries** across 6 obs types.
- Persisted across HA restarts via `LearningState.rejection_log`. The cap is enforced on load to guard against file corruption.

### 21.8 Sensor Attribute Exposure

`ClimateAdvisorComplianceSensor.extra_state_attributes` exposes a `thermal_learning_health` key. In compliance with the security rule against exposing raw behavior data in attributes, only summary counts and the last rejection reason code are exposed — not the full `ThermalRejectionEvent` dicts:

```
thermal_learning_health: {
    obs_type: {
        "attempts":              int,
        "committed":             int,
        "rejections":            {reason_code: int, ...},
        "last_rejection_reason": str | None,
    }
}
```

### 21.9 `tools/thermal_health.py` Usage

Standalone CLI tool. Requires `HA_URL` and `HA_TOKEN` environment variables (same pattern as `tools/validate.py`). Prints two sections.

**Section 1 — Historical Aggregates** (existing): reads `thermal_learning_health` from the compliance sensor attribute via HA REST API. Shows per-obs-type rejection counts and last rejection reason.

```
Thermal Learning Health Report
═══════════════════════════════
obs_type            attempts  committed  rejections  last rejection
─────────────────────────────────────────────────────────────────
passive_decay       12        3          9           too_few_samples (n=3/5)
hvac_heat           8         5          3           ols_bad_fit (R²=0.08/0.20)
hvac_cool           6         4          2           abandoned
fan_only_decay      2         0          2           too_few_samples (n=2/5)
ventilated_decay    0         0          0           —
solar_gain          1         0          1           small_delta (ΔT=0.1°F/0.2°F)
```

**Section 2 — Current Observations** (added in Issue #125): reads `thermal_pipeline` from `GET /api/climate_advisor/automation_state`. Shows a live table of every observation currently accumulating samples. Fields are sourced from `_build_thermal_pipeline_summary()` in `coordinator.py`.

```
Current Observations
--------------------
obs_type            status      elapsed   samples  last_smp  indoor           outdoor   delta
ventilated_decay    monitoring  164.3 min 6        2.1 min   71.8-72.1°F      69.0°F    0.3°F

(Rejection log entries: ventilated_decay=5)
```

If the debug-state endpoint is unreachable or returns no `thermal_pipeline` key, the tool prints a warning and skips Section 2 — it does not abort.

No new secrets or external dependencies. Run from the project root after setting env vars.

### 21.10 Test Coverage

| Test | File |
|---|---|
| `compute_k_passive()` 3-tuple reason codes (all 5 failure paths + success) | `tests/test_thermal_rejection.py` |
| Rejection log accumulation on abandon | `tests/test_thermal_rejection.py` |
| Rejection log per-type cap at 100 entries | `tests/test_thermal_rejection.py` |
| Rejection log persisted in `LearningState` and reloaded on startup | `tests/test_thermal_rejection.py` |
| `learning_health` present in `get_thermal_model()` with correct counts | `tests/test_thermal_rejection.py` |
| `last_rejection` populated after a rejection event | `tests/test_thermal_rejection.py` |
| `thermal_learning_health` in compliance sensor attributes | `tests/test_thermal_rejection.py` |
| Sensor attribute exposes counts/summary only (not raw events) | `tests/test_thermal_rejection.py` |

### 21.11 Log Taxonomy (Issue #125)

Issue #125 adds structured log lines at key points in the observation lifecycle. The following table documents every new log line, its level, source method, and field semantics.

| Log line format | Level | Source method | What it means |
|---|---|---|---|
| `Thermal rolling window: obs_type=<T> n=<N> elapsed=<E>min indoor=[<lo>..<hi>] (ΔT=<dt>°F) outdoor=<out>` | INFO | `_commit_rolling_window_obs()` | Fires immediately before every rolling-window commit attempt, including ones that will be rejected. `n` = sample count; `ΔT` = max−min indoor temp across samples; `outdoor` = last sample's outdoor temp or `?` if unavailable. |
| `Thermal pipeline: <N> pending observations active` | INFO | `_async_update_data()` | Emitted once per coordinator update cycle when at least one pending observation exists. Confirms the pipeline is alive without requiring full debug-state output. |
| `Thermal event commit failed (<T>): k_passive rejected (R²=<r2>, n=<N>, indoor_ΔT=<dt>°F) code=<code>` | INFO | `_commit_event_from_dict()` | Rejection of a decay observation after OLS. `indoor_ΔT` is the max−min span across all sample indoor temps. `code` is one of the `REJECT_*` constants. |
| `Thermal obs abandoned [type=<T> reason=<code> n=<N>/<req> dt=<dt>°F/? elapsed=<E>m]` | INFO | `_abandon_observation()` | Fires whenever an observation is discarded before commit. `elapsed` is now always populated from `obs["start_time"]` — the `?` value that appeared in Issue #124 logs no longer occurs. |
| `compute_k_passive: wrong sign k_p=<v> (must be < 0) n=<N>` | DEBUG | `compute_k_passive()` | OLS returned a positive k_passive — a physics violation. The observation is rejected with `REJECT_OLS_WRONG_SIGN`. |
| `compute_k_passive: out of bounds k_p=<v> (must be in [<min>, <max>]) n=<N>` | DEBUG | `compute_k_passive()` | OLS result is outside the `[THERMAL_K_PASSIVE_MIN, THERMAL_K_PASSIVE_MAX]` interval. Rejected with `REJECT_OLS_BOUNDS`. |

**Reading the rolling-window line during a flat-temperature episode:**

When indoor temperature is stable (HVAC holding setpoint, mild outdoor conditions), a sequence like this is normal and expected:

```
Thermal rolling window: obs_type=ventilated_decay n=6 elapsed=5.0min indoor=[72.0..72.0] (ΔT=0.00°F) outdoor=69.0
Thermal event commit failed (ventilated_decay): k_passive rejected (R²=0.000, n=6, indoor_ΔT=0.00°F) code=ols_bad_fit
Thermal obs abandoned [type=ventilated_decay reason=ols_bad_fit n=6/4 dt=0.00°F/? elapsed=35m]
```

`R²=0.000` with `indoor_ΔT=0.00°F` means the indoor temperature was effectively flat — there was no temperature excursion for OLS to fit. This is **not a bug**. The learning engine correctly refuses to extract a thermal decay rate from flat data; fitting a slope to a flat line would produce a meaningless or unstable k_passive. This condition occurs whenever indoor and outdoor temperatures are within 2–3°F of each other, or when HVAC is actively cycling to maintain a stable setpoint. Resolution: wait for a natural temperature excursion — a warm afternoon, a morning pre-heat, or an overnight cooldown — to provide the ≥ 0.2°F indoor ΔT the quality gate requires.

### 21.12 `thermal_pipeline` Key in `/api/climate_advisor/automation_state`

Issue #125 adds a `thermal_pipeline` key to the debug-state API response. This key is built on every call by `_build_thermal_pipeline_summary()` in `coordinator.py` and reflects the live state of all pending observations at the moment of the request.

**Response shape:**

```json
{
  "thermal_pipeline": {
    "pending": [
      {
        "obs_type": "ventilated_decay",
        "status": "monitoring",
        "elapsed_minutes": 164.3,
        "sample_count": 6,
        "last_sample_age_minutes": 2.1,
        "indoor_range_f": [71.8, 72.1],
        "indoor_delta_f": 0.3,
        "outdoor_f": 69.0
      }
    ],
    "rejection_log_counts": {
      "ventilated_decay": 5
    }
  }
}
```

**Field semantics:**

| Field | Type | Description |
|---|---|---|
| `pending` | `list` | One entry per obs_type currently in `_pending_observations`. Empty list when no observations are active. |
| `pending[].obs_type` | `str` | Observation type key (e.g., `"passive_decay"`, `"ventilated_decay"`) |
| `pending[].status` | `str` | Raw `status` field from the pending observation dict (e.g., `"monitoring"`) |
| `pending[].elapsed_minutes` | `float \| null` | Minutes since observation started; `null` if `start_time` is absent or unparseable |
| `pending[].sample_count` | `int` | Number of samples accumulated so far |
| `pending[].last_sample_age_minutes` | `float \| null` | Minutes since the most recent sample; `null` if `last_sample_time` is absent |
| `pending[].indoor_range_f` | `[float, float] \| null` | `[min, max]` of indoor temps across all samples; `null` if no samples have `indoor_temp_f` |
| `pending[].indoor_delta_f` | `float \| null` | `max − min` indoor temp; `null` if no samples |
| `pending[].outdoor_f` | `float \| null` | Outdoor temp from the last sample; falls back to coordinator's `_last_outdoor_temp`; `null` if neither is available |
| `rejection_log_counts` | `dict[str, int]` | Per-obs-type count of entries in `_rejection_log`. Mirrors the same data visible in `learning_health` but scoped to raw counts only, for quick triage without parsing `ThermalRejectionEvent` dicts. |

---

_Last Updated: 2026-06-12_
