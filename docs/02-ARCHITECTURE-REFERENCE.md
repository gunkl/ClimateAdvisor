<!-- Nav: ← [Strategy and Design](01-STRATEGY-AND-DESIGN.md) → [Learning Engine Design](05-LEARNING-ENGINE-DESIGN.md) -->

# Climate Advisor — Architecture Reference

## Anchors
| Question | Short answer | → Full answer |
|---|---|---|
| Who owns the forecast, thermal model, today's outdoor peak and TOU window the engine's decisions read? May anything outside `automation.py` assign an `AutomationEngine` private? | The coordinator is the single owner and passes them in: it builds a frozen `EngineFacts` snapshot (`engine_facts(now)`) and runs each engine decision call inside it (`engine_inputs.decide`); the engine keeps no copy. Single facts reach the pure decisions as arguments (`forecast_peak_2h`, the soft-start peak). Accessors stay only for engine-internal timers/readers (`inputs.build_facts`) and the thermal model. Engine-owned state is changed only through public engine methods. `tests/test_engine_state_ownership_static.py` fails on any other write or on a direct decision call. Issue #1443. | [Engine-State Ownership](#engine-state-ownership-issue-1443) |
| What are the source files and what does each own? | 96 files total as of v0.9.4 (2026-11-07; re-verify with `ls custom_components/climate_advisor/*.py`, this count drifts as features ship). The original 16 own one responsibility each: `coordinator.py` orchestrates, `classifier.py` classifies, `automation.py` executes HVAC calls, `learning.py` persists and analyses behavior, etc. Block 5's FSM migration (epic #594) added dozens more — mostly small pure `decide_*()` decision-core modules plus the lifecycle FSMs and the dispatcher pair — and multi-zone support (Issue #796, v0.7.0) added `zone_registry.py`, `storage_paths.py`, and `diagnostics.py`. The File Structure tree below predates several of these additions and is not exhaustive — see [Multi-Zone Spec](multi-zone-spec.md) for the multi-zone-specific modules. | [§File Structure](02-ARCHITECTURE-REFERENCE.md#file-structure) |
| What is the FSM Decision Layer — the lifecycle FSMs, and is anything actually driving HVAC through them? | Seven subsystems (nat-vent, door-window, override-grace, fan, economizer, classification, occupancy) each have a dedicated FSM (two — classification, occupancy — are deliberately stateless). All are now the sole authoritative implementations — the legacy branches and `_*_fsm_authoritative` cutover flags were permanently removed in Phase 6 graduation (Issue #757). Each FSM is directly wired into `AutomationEngine` and drives real HVAC decisions. | [§FSM Decision Layer](02-ARCHITECTURE-REFERENCE.md#fsm-decision-layer) |
| How does data flow from the weather entity to an HVAC service call? | Weather entity → coordinator (every 30 min) → `classify_day()` → `DayClassification` → `apply_classification()` in the automation engine → `climate.set_temperature` / `climate.set_hvac_mode`. | [§Data Flow](02-ARCHITECTURE-REFERENCE.md#data-flow) |
| What are the five coordinator-scheduled daily events and when do they fire? | Briefing (default 6:00 AM), morning wakeup (6:30 AM), bedtime setback (10:30 PM), end-of-day save (11:59 PM), and the 30-minute forecast refresh loop. | [§Coordinator Scheduled Events](02-ARCHITECTURE-REFERENCE.md#coordinator-scheduled-events) |
| What is the debounce / grace period system and how do the three timers interact? | Debounce (default 10 min as of Issue #504) delays *any* reaction to a sensor state change until it holds steady for the configured time — pause/resume HVAC, and, since #504, nat-vent/WHF/HVAC-fan engage/exit. Manual grace (default 30 min) blocks new pauses after a user override. Automation grace (default 5 min) blocks re-pause after system resumes. Manual override always wins. | [§Debounce and Grace Period System](02-ARCHITECTURE-REFERENCE.md#debounce-and-grace-period-system) |
| What sensors does Climate Advisor expose and what do their attributes carry? | Eight sensor entities: day type, trend, next action, daily briefing, comfort score, status, occupancy mode, and AI status — each with diagnostic attributes. | [§Sensors Exposed](02-ARCHITECTURE-REFERENCE.md#sensors-exposed) |
| How does the thermal state machine (v3) work and which methods own each phase? | Ten coordinator methods drive six concurrent observation types in `_pending_observations` (Issue #121); observations are in memory only, so a run in progress at a restart is learned from only after it (Issue #1437). | [§Coordinator Thermal State Machine Methods](02-ARCHITECTURE-REFERENCE.md#coordinator-thermal-state-machine-methods) |
| How does state persistence work — what is stored, where, and in what format? | Two JSON files: `climate_advisor_state.json` (runtime coordinator state, atomic write) and `climate_advisor_learning.json` (thermal model + observation history). State version mismatch discards and resets; no migration chain. | [§State Persistence Brief](state-persistence.md) |
| How does unit conversion work between °F and °C? | `from_fahrenheit()` for absolute temperatures (subtracts 32, ×5/9); `convert_delta()` for differences and rates (×5/9 only, no offset). Unit is user-selected in config flow, not auto-detected from HA. | [§Temperature Conversion Brief](temperature-conversion.md) |
| How does the REST API work — endpoints, auth, and data access pattern? | 26 views (24 in `api.py` `API_VIEWS`, the zone list `zones` in `ZONELESS_VIEWS`, plus `settings_extra` in `dashboard_api.py` `DASHBOARD_VIEWS`), all requiring a HA long-lived token. GETs read from `coordinator.data`; POSTs delegate to the coordinator or automation engine. Two config fields are redacted; out-of-range numerics are silently clamped. | [§REST API Brief](rest-api.md) |
| How does the Claude AI integration work — circuit breaker, rate limiting, skills, budget? | `ClaudeAPIClient` wraps all API calls with a three-state circuit breaker, per-month budget cap, and exponential-backoff retry. `AISkillRegistry` provides a pluggable skill framework; two skills are registered: `activity_report` and `investigator`. | [§AI Integration Brief](ai-integration.md) |
| How does multi-zone support work — what is a "zone", and how does the coordinator/API/services resolve which one a call is for? | A zone is a second Climate Advisor config entry, not a new schema. `zone_registry.py` resolves/enumerates zone coordinators by `entry_id`; all 5 HA services require an `entry_id` field; the REST endpoints accept an optional `entry_id` query param (with several zones and none given a GET degrades to a logged, Repairs-flagged guess and a POST refuses with 400); `timeline`, `night_strategy` and `prediction_accuracy` take `zone` and refuse with 400 instead of guessing. Shipped v0.7.0 (2026-09-01, Issue #796). | [Multi-Zone Spec](multi-zone-spec.md) |
| Where is the full Tier 3 occupancy dispatch spec — priority resolution, state transitions, setback formulas? | The Territory spec covers all four occupancy modes, GUEST > VACATION > HOME/AWAY priority, toggle entity wiring, 7-row state transition table, setback formulas, and persistence across HA restarts. | [§Occupancy Dispatch Spec](occupancy-dispatch-spec.md) |
| Which setpoint combinations warn on the setpoints page, and what range limits remain (Issue #1347)? | One 40–95 °F sanity range; thrash/incoherent combinations warn and save on an identical resubmit; pure rules in `setpoint_combinations.py`, vacation resolver `vacation_edges()`. | [Setpoint Combination Warnings](setpoint-combination-warnings.md) |
| Where is the Tier 3 spec for the chart state log — entry schema, ring buffer contracts, retention, persistence? | The Territory spec covers the raw/hourly/daily entry schemas, append ordering invariant, 8-path load error table, atomic write contract, downsampling tiers, and the 17,520-entry (≈1 year) cap. New `fan_running`/`nat_vent_active` fields and merged Vent bar documented in §Vent Bar Fields (Issue #331). | [Chart State Log — Territory Spec](chart-log-spec.md) |
| Where is the Tier 3 spec for the forecast archive (persisted hourly-forecast snapshots for error scoring)? | `forecast_archive.py` `ForecastArchive`: entry-scoped JSON, one snapshot per local hour, 72 h horizon, 365-day retention, recorded from `_refresh_hourly_forecast()` on successful fetches only; `get_snapshots()` is the R1 read contract. | [Forecast Archive Spec](forecast-archive-spec.md) |
| Where is the Tier 3 spec for prediction engine v4 (the model, fit, confidence gates)? | `predictor_v4.py`: pure-Python hidden-mass thermal model with thermostat emulation and forecast correction; fitted nightly from chart_log; `predict()` returns `None` at confidence `none`. Since 0.9.4 (R6b) v4 is the default PRIMARY engine and drives the prediction-dependent decisions through the coordinator's control feed; see the prediction-engines row below. |
| Which prediction engine drives the house and the charts, how many run, and what happens while it is learning? | `prediction_engines.py` registry (open; v3 and v4 registered, a v5 is one `register()`): per zone a primary (`primary_engine`, default v4) and an optional shadow (`shadow_engine`, default none, must differ). At most two run. The PRIMARY drives the pre-heat/pre-cool TOU lead, the ODE ceiling guard, the nat-vent window-close time and the setback depth; the shadow only draws a line. With no usable primary fit (`USABLE_CONFIDENCE`) the decisions use today's "not learned yet" defaults and the chart shows the schedule estimate, never the other engine; switching to an engine without a model makes it fit first. | [Prediction Engines Spec](prediction-engines-spec.md) | [Predictor v4 Spec](predictor-v4-spec.md) |
| Where is the Tier 3 spec for the bedroom night strategy (advise only)? | `night_strategy.py`: pure planner over the v4 rollout; options per fan mode, DP-2 score, evidence gate, `apply_night_strategy(timeline, strategy)`. Wired in R6: the coordinator applies tonight's strategy to the timeline as advice (it never starts a fan), `night_replay.py` refreshes the evidence nightly after the v4 fit, `GET /api/climate_advisor/night_strategy` feeds the "Tonight in the bedroom" card. | [Night Strategy Spec](night-strategy-spec.md) |
| Where is the Tier 3 spec for how v4 runs in shadow (schedule, files, failure handling, "no decision module reads v4")? | `v4_shadow.py` / `v4_archive.py` / coordinator `_v4_*`: fit 03:15 nightly and once after startup, predict per 30-min cycle, three entry-scoped files, hourly-throttled archive save plus a shutdown flush, AST test that no decision module mentions v4 (a v4 primary reaches the decisions only as plain values from the control feed). | [v4 Shadow Spec](v4-shadow-spec.md) |
| Where is the Tier 3 spec for the operation timeline (steps, window candidates, latch and regeneration, replay check)? | `operation_timeline.py` (pure) with `coordinator._timeline_*` (built once per cycle and on triggers), `timeline_surfaces.py` (since R3b the Next Action / Next Automation cards, the briefing's window times and Today's Steps, the Analysis tomorrow column and `GET /api/climate_advisor/timeline` all read it; no automation decision does) and `tools/timeline_replay.py`; the frozen output contract is `Timeline`/`TimelineStep`/`PathPoint`. | [Operation Timeline Spec](operation-timeline-spec.md) |
| What is the "Prediction accuracy (preview)" page and what does it call? | `frontend/prediction_accuracy.html` (the view is the shared `frontend/pa_view.js`, also drawn on the new dashboard's Analysis tab, Issue #1497): sensor / look-ahead / range controls, drag-zoom chart of actual vs v4 vs v3, tiles scoring the time in view, the information-only DP-8 "How each model has scored" table. It calls only `GET /api/climate_advisor/prediction_accuracy` (plus `/status` for the zone list). | [Prediction Accuracy Preview Page](#prediction-accuracy-preview-page-issue-1230-r1) |
| How is v4 scored over history, and does anything have to pass before it is shown or used? | `tools/predict_backtest.py`: walk-forward (fit only on data before each test week), v3 replayed with production code, "no change" baseline; seasons by HVAC mode; DP-8 rows at 1/4/12 h per season. Since 0.9.4 they are information only: nothing is shown, hidden or switched on because of a pass mark (which engine runs is the Settings choice). | [Debugging Guide: prediction backtest](09-DEBUGGING-GUIDE.md#prediction-backtest-toolspredict_backtestpy-issue-1230-r1) |
| Where is the Tier 3 spec for the Activity Report per-event table — event vocabulary, setpoint convention, renderer runbook? | The Territory spec covers the catalogued event types in 6 groups (authoritative count lives in its Event Catalog; every `EVENT_RENDERERS` key has a row, test-enforced), `_format_band_setpoint` single-setpoint convention, `EVENT_RENDERERS` registry, `_default_renderer` contract, dedup mechanics, and the four-step runbook for adding a new renderer. | [Activity Report Table Spec](activity-report-table.md) |
| What replaced the ClimateSimulator and how does the production harness work? | Issue #236 eliminated the standalone simulator engine. `tools/sim_harness/` drives the real `AutomationEngine` headless via FakeHass + FakeScheduler. `tools/simulate.py` is now a CLI/MANIFEST shell only. | [Sim Harness Brief](sim-harness-brief.md) |
| What are the full contracts for FakeHass, FakeScheduler, run_production, and outcomes? | FakeHass service-bus interception + state-feedback loop; FakeScheduler virtual clock patching contract; run_production event dispatch table including predicted_indoor ODE re-classification; outcomes custom assertion types. | [Sim Harness Spec](sim-harness-spec.md) |
| Was there a shadow engine during the FSM migration? | During Phase 5 (Issues #613-#731), `coordinator.shadow_automation_engine` provided A/B comparison of production vs. FSM implementations. It was permanently removed in Phase 6 graduation (Issue #757) once all subsystems had migrated and proven zero divergence across the full test corpus. | [§FSM Decision Layer](02-ARCHITECTURE-REFERENCE.md#fsm-decision-layer) |
| Is there anything that catches a hard invariant violation (e.g. AC and WHF both physically on) even if the automation engine's own bookkeeping looks fine? | Yes — `invariant_watchdog.py` (Issue #749), a deliberately minimal detect-and-alert module that reads ground-truth sensor/thermostat state only (never internal override/grace flags) once per coordinator update cycle. Never issues a corrective command itself. | [Invariant Watchdog Brief](invariant-watchdog-brief.md) |
| How is `coordinator._event_log` retention bounded, and how do live emit and restart/restore stay in sync? | `EVENT_LOG_MAX_AGE_HOURS` (168h) is primary retention; `EVENT_LOG_CAP` (4000) is a memory-safety backstop applied after. Both are enforced by one shared function, `_prune_event_log()`, called identically from `_emit_event()` and `async_restore_state()`. | [§Event Log Retention](02-ARCHITECTURE-REFERENCE.md#event-log-retention-issue-432) |
| Where is the `incident_detected` payload built, and why is there no second emitter? | `incident_payload.build_incident_payload()` builds the common envelope for the coordinator's `_emit_incident()` and the engine's `_emit_engine_incident()`; each owner still emits through its own event callback. Issue #1049. | [Incident Payload Brief](incident-payload-brief.md) |
| Which pure functions decide an explicit mode switch and the mode read-back? | `desired_state.decide_explicit_mode_switch()` (switch before a heat/cool setpoint write) and `setpoint_verify_decision.decide_mode_alignment()` (10 s / 130 s read-back); the shell lives in `automation.py` `_ensure_hvac_mode_for_write()` / `_check_mode_alignment()`. | [Command shapes since #301](08-COMPUTATION-REFERENCE.md#command-shapes-since-301--explicit-mode-switch-issue-1049-part-of-1048) |
| What does the diagnostics download / investigator snapshot contain about the thermostat? | `diagnostic_snapshot.build_diagnostic_snapshot()`: live climate attributes, a parsed `capabilities` block, a `last_command` block, plus `recent_hvac_events`, all redacted inside the builder. | [Debugging Guide](09-DEBUGGING-GUIDE.md#reading-the-thermostat-diagnostics-blocks-and-confirming-an-ignored-mode-issue-1049) |
| Which edge of the comfort band does the thermostat guard (heat or cool), and which modules own that decision? | `comfort_family_decision.decide_comfort_family()` (pure leaf) + `comfort_family_fsm.transition()` (dwell anti-chatter), shelled by `AutomationEngine._resolve_comfort_family_via_fsm()`; reactive on every thermostat temperature change via `comfort_family_temperature_check()`. Nearest-edge arming, Issue #1066 (supersedes the #827 day-type-default-edge / sustain machinery and the ODE floor guard; Issue #1073 then restores a recency-gated against-grain tolerance as the settling guard). | [Comfort Edge Selector](#comfort-edge-selector-issue-1066-settling-guard-issue-1073) |
| Where is the history of commanded HVAC mode changes kept, and what reads it? | A 200-entry ring buffer (`AutomationEngine.comfort_mode_changes`, written by `_record_commanded_mode()`, persisted in the state file); `control_quality.py` derives the `control_quality` block of `build_diagnostic_snapshot()` from it. | [Comfort Edge Selector](#comfort-edge-selector-issue-1066-settling-guard-issue-1073) and [Debugging Guide](09-DEBUGGING-GUIDE.md#control-quality-report-issue-1066) |
| What happens to the old coordinator and engine when a config entry reloads or unloads (settings save, Repairs fix)? | `async_shutdown()` latches both (`_shut_down`), runs HA's base shutdown, saves, then releases every timer and listener it registered; HA calls it a second time from the entry's on_unload, and that call is a no-op. A shut-down instance refuses every command, notification and arm with one WARNING. Issue #1102. | [§Teardown Contract](#teardown-contract-issue-1102) |
| Which pure function decides the "house off-target, equipment not running" stall alert, and where do the shared notify/reminder helpers live? | `comfort_stall_decision.decide_comfort_stall()` (clock + exclusions; shell `coordinator._update_comfort_stall()`); `coordinator._notify_in_background(title, message, type, *, email=True)` is the one coordinator-side fire-and-forget owner-alert routine (entity health, invariant watchdog, stall; the engine-side indoor-blind alert in `automation.py` still has its own `_push_indoor_blind_alert` / `_notify_indoor_blind`) and `temperature.reminder_due()` its pure reminder predicate; `desired_state.decide_open_sensor_hold()` is the one open-window predicate. | [House-off-target stall alert](08-COMPUTATION-REFERENCE.md#house-off-target-stall-alert-issue-1050-part-of-1048) |

## File Structure

```
custom_components/climate_advisor/
├── __init__.py          # Integration setup, service registration
├── manifest.json        # HA integration metadata (domain, dependencies, version)
├── const.py             # Constants: thresholds, defaults, attribute names
├── config_flow.py       # Setup flow (user → unit → setpoints → temperature_sources → sensors → occupancy → schedule) + options menu
├── strings.json         # UI text for config flow steps
├── coordinator.py       # Central brain: scheduling, events, data flow, state
├── classifier.py        # Day type + trend classification from forecast
├── briefing.py          # Daily email/notification text generation
├── automation.py        # HVAC service calls, door/window pause, occupancy
├── learning.py          # Pattern tracking, suggestion generation, persistence
├── sensor.py            # HA sensor entities for dashboards
├── switch.py            # Automation enable/disable switch (observe-only mode)
├── claude_api.py        # Centralized Claude API client: auth, retry, circuit breaker, rate limiting, budget tracking. Provides async_request() for all AI features.
├── ai_skills.py         # AI skills framework: lightweight registry for pluggable AI analysis capabilities. Skills register a context builder, response parser, and optional fallback.
├── chart_log.py         # Chart state log: persistent 1-year ring buffer of HVAC/fan/temp data points and event markers, used by the Temperature Forecast chart.
├── cycle_model.py       # Pooled heating/cooling cycle model (Issues #1512/#1516): fit_cycles, clean_series, infer_resolution, grade, typical_prior, prior_from_cache. Pure math, no HA imports; shared with tools/thermal_replay.py and tools/predict_backtest.py. See 05-LEARNING-ENGINE-DESIGN.md.
├── cycle_recorder.py    # CycleRecorder: pure state machine turning thermostat edges, band-sensor readings, flags, setpoint changes and gaps into closed cycle dicts.
├── cycle_store.py       # CycleStore: per-zone climate_advisor_cycles.json (90 days back from the mode's newest cycle, cap 3000 cycles, native-resolution integer deltas (v2; v1 migrated), compact in memory, saved periodically + at HA stop, atomic 0600 write). Separate from the chart_log (chart-log-spec.md).
├── feels_like.py        # Issue #1414: pure NWS heat index / wind chill (feels_like_f) and weather wind-unit -> mph, chart display only. See chart-log-spec.md "Humidity and Feels-Like Fields".
├── humidity_sources.py  # Issue #1414: one humidity resolver, three roles (indoor_day, indoor_sleep, outdoor); chart display only, never the climate TARGET humidity.
├── forecast_archive.py  # Forecast archive (Issue #1230 R0): persisted hourly-forecast snapshots, one per local hour, for forecast-error scoring. Record-only; entry-scoped JSON. See forecast-archive-spec.md.
├── predictor_v4.py      # Prediction engine v4 (Issue #1230 R1): pure-Python indoor-temperature predictor (hidden-mass thermal model, thermostat emulation, forecast correction, bounded Levenberg-Marquardt fit). Default PRIMARY engine since 0.9.4: as primary its baseline path and control_rates() reach the decisions through the coordinator's control feed (R6b); no decision module imports it. See predictor-v4-spec.md.
├── night_strategy.py    # Bedroom night strategy (Issue #1230 R4): pure planner (options per fan mode, DP-2 score, evidence gate) + apply_night_strategy(timeline). ADVISE ONLY (applied to the timeline since R6; never commands). See night-strategy-spec.md.
├── v4_shadow.py         # v4 shadow wiring, pure/executor halves (Issue #1230 R1): run_fit, run_cycle, build_inputs, model_summary. See v4-shadow-spec.md.
├── v4_archive.py        # v4 shadow persistence (Issue #1230 R1): V4Archive (first-write-wins 1/4/12/24 h and 2/3/5-day predictions, 400 days) and V4ModelStore (fitted model + hidden-mass state); entry-scoped JSON. See v4-shadow-spec.md.
├── operation_timeline.py # Operation timeline (Issue #1230 R3a): pure plan of CA steps and occupant (window) steps, predicted path, window candidates, per-day latch. Read by the display surfaces (R3b), never by a decision. See operation-timeline-spec.md.
├── timeline_surfaces.py # Issue #1230 R3b: pure rules turning the timeline into the Next Action / Next Automation / briefing text (card_user_step, next_automation, timeline_windows). See operation-timeline-spec.md.
├── night_replay.py      # Issue #1230 R6: nightly recorded-night replay behind the night-strategy evidence gate (executor, after the v4 fit). See night-strategy-spec.md.
├── dashboard_api.py     # Issue #1230 R5: DASHBOARD_VIEWS (GET settings_extra: changed-from-default, TOU schedules, GitHub status; the token is reduced to token_set), registered with API_VIEWS. See rest-api.md.
├── prediction_engines.py   # Prediction-engine registry (Issue #1230): PredictionEngine contract, v3/v4 entries, primary/shadow settings, at-most-two rule, engine_state for the chart payload. Display + v4 orchestration + the control feed (R6b: the primary's path and rates handed to the decisions as plain values; no decision module imports it). See prediction-engines-spec.md
├── prediction_accuracy.py  # Payload builder for GET /api/climate_advisor/prediction_accuracy (Issue #1230 R1): v4 vs v3 vs "no change" scoring, live archive + published backtest merge, downsampling. See rest-api.md.
├── repairs.py           # HA Repairs integration: surfaces actionable fix prompts when CA detects config or data problems.
├── ai_skills_investigator.py  # The sole registered AI skill ("investigator"): system prompt, response parser, deterministic fallback, thin context-assembly orchestrator. Serves both silent/scheduled narration and on-demand investigation — the former separate "Activity Report" skill (ai_skills_activity.py) was retired and merged into this one, Issue #563.
├── ai_skills_context.py  # ContextProviderRegistry + all 15 context providers the investigator's context is assembled from, including the event-timeline/renderer functions ported from the retired ai_skills_activity.py.
├── api.py               # REST API views for the dashboard panel (24 in API_VIEWS, plus ZONELESS_VIEWS = the `zones` list, Issue #1454; Issue #1230 added `prediction_accuracy`, `timeline` and `night_strategy`; `dashboard_api.py` adds `settings_extra`): GETs read from coordinator.data, POSTs delegate to the coordinator or automation engine.
├── state.py              # Operational state persistence: climate_advisor_state.json, atomic write (.tmp + os.replace).
├── temperature.py        # Temperature unit utilities: from_fahrenheit() (absolute, subtracts 32 then ×5/9) vs convert_delta() (differences/rates, ×5/9 only); display_temp()/display_delta() None-safe rounded API-boundary wrappers used at every display site (Issue #1050); format_temp()/format_temp_delta(); reminder_due() shared reminder/debounce predicate.
│
│                         # --- Block 5 FSM migration (epic #594) — 30 files below, all added after the original 16 above ---
├── desired_state.py      # Shared pure decide_scheduled_band_gate() gate used by every scheduled/cyclical comfort-band call site (Issue #498) — replaced four independently hand-copied gate checks.
│                         # Issue #1049 adds `decide_explicit_mode_switch()` + `MODE_SWITCH_DEDUPE_SECONDS` (120 s): true when a heat/cool setpoint write must be preceded by an explicit awaited `climate.set_hvac_mode` (Sensi/ecobee ignore the `hvac_mode` kwarg of `set_temperature`).
├── lifecycle_dispatcher.py  # Generic cross-lifecycle pub/sub event dispatcher for the three FSMs below. Built Issue #633, wired into production Issue #717 — see §FSM Decision Layer.
├── lifecycle_events.py   # Cross-lifecycle event vocabulary: LifecycleEventType (DOOR_PAUSE_*, GRACE_*, OVERRIDE_*, NAT_VENT_SESSION_*) + LifecycleEvent dataclass (Issue #633, extended #717).
│
├── nat_vent_fsm.py       # Nat-vent lifecycle FSM — unified (state, event) -> Transition table (Issue #633). Defines no state itself; drives NatVentLifecycleState from nat_vent_lifecycle.py.
├── nat_vent_lifecycle.py # Pure session-state derivation for nat-vent: NatVentLifecycleState (4 states — INACTIVE, ACTIVE_FULL_GATE, ACTIVE_SOFT_START, PAUSED_REACTIVATION_LOCKOUT) (Issue #606).
├── engine_inputs.py      # Issue #1443: EngineFacts (the frozen snapshot the coordinator passes in at each decision), the engine's facts source for timers/readers (CoordinatorEngineInputs; StaticEngineInputs for a bare engine) and the pure forecast_peak_next_2h.
├── nat_vent_gate.py      # Pure decision core: the nat-vent reactivation gate (architecture-reset Step 2).
├── nat_vent_exit.py      # Pure decision core: the nat-vent active-session exit chain, all 5 exit reasons in priority order (Issue #608).
├── nat_vent_cycling.py   # Pure decision core: mid-session nat-vent fan cycling (Issue #698).
├── nat_vent_reactivation_lockout.py  # Pure decision core: the 300s nat-vent reactivation lockout predicate.
│
├── door_window_fsm.py    # Door/window pause/grace lifecycle FSM — unified transition table (Issue #637). Drives DoorWindowLifecycleState from door_window_lifecycle.py.
├── door_window_lifecycle.py  # Pure session-state derivation for door/window pause/grace: DoorWindowLifecycleState (5 states — NORMAL, PAUSED_ACTIVE, PAUSED_IDLE, GRACE, PAUSED_DURING_GRACE) (Issue #637).
├── door_window_open_response.py   # Pure decision chain of handle_door_window_open(): the handler dispatches on its enum (Issue #637, #1142).
├── door_window_close_response.py  # Pure decision core: all-doors-windows-closed (Issue #637).
├── door_window_pause_entry.py     # Pure decision core: entering a door/window pause (Issue #637).
├── door_window_grace_expiry.py    # Pure decision core: grace-period expiry (Issue #637).
│
├── override_grace_fsm.py     # Override/grace joint lifecycle FSM — unified transition table (Issue #639). Drives the composite (OverrideConfirmState, GraceState) from override_grace_lifecycle.py.
├── override_grace_lifecycle.py  # Pure session-state derivation for override/grace: OverrideConfirmState (IDLE/PENDING) × GraceState (NONE/ACTIVE_PROTECTING_OVERRIDE/ACTIVE_UNPROTECTED) (Issue #639).
├── override_grace_start.py   # Pure predicate: does this grace trigger protect a real override? (Issue #639, generalizes the #530 _GRACE_TRIGGERS_PROTECTING_OVERRIDE fix).
├── override_confirm_split.py # Pure decision core: override confirmation's PATH A (confirmed)/PATH B (self-resolved transient) split (Issue #639).
├── override_cancel_outcome.py  # Pure outcome classification for cancel_override() (Issue #639).
├── override_match.py     # Pure decision core: does the active override already match automation's current decision? (Issue #639).
├── override_orphaned_grace.py  # Pure predicates for the two mirror-image grace/override watchdogs — Issue #508 (grace without override; skipped while a manual change is pending in its confirmation window, Issue #1216) and Issue #321 (override stuck past due grace-end) (Issue #639).
├── override_supersession.py  # Pure decision core: override detection and Issue #201/#282's "second override during active grace" replace-not-stack behavior.
│
├── fan_thermostat_decision.py  # Pure decision core: the tick-level fan thermostatic stop check (architecture-reset Step 2).
├── fan_drift_reconciliation.py # Pure decision core: fan physical-state drift reconciliation (Issue #423).
├── fan_status.py          # Fan-status suppression predicate shared by the WHF/HVAC fan status cards (§ Fan Status Values, CLAUDE.md); fan_entity_on() -- the single True/False/None reading of a fan command entity's state (Issue #1050).
├── comfort_stall_decision.py  # Pure decision core: the "house off-target for 3 h with no equipment running in the needed direction" stall alert -- decide_comfort_stall() / StallDecision / StallReason (Issue #1050, #1048 change 9). Shell: coordinator._update_comfort_stall(). See docs/08-COMPUTATION-REFERENCE.md "House-off-target stall alert".
├── comfort_family_decision.py  # Pure leaf: decide_comfort_family() / effective_margin() -- nearest-edge arming of the heat or cool family (Issue #1066; supersedes the #827 day-type-default-edge machinery; Issue #1073 adds the recency-gated settling guard). No HA imports.
├── comfort_family_fsm.py  # Thin transition layer around the leaf: transition() / ComfortFamilyDwellState -- the 600 s anti-chatter dwell (breaches bypass it). The dwell clock reads the persisted mode-change ring buffer (Issue #1073), so it survives a restart.
├── control_quality.py     # Pure builder of the `control_quality` diagnostic block from the commanded-mode ring buffer + chart log (Issue #1066). See Debugging Guide.
├── remote_offset.py             # Pure core of the remote-sensor outer loop (Issue #1194 a): two-sided hysteresis decision, step/cap in internal-F deltas, startup wait, opposite-band-edge cap. Engine owns state and writes.
├── setpoint_verify_decision.py  # Pure decision cores: the post-fan setpoint verify check (architecture-reset Step 2), plus `decide_mode_alignment()` / `ModeAlignmentOutcome` -- the "did the thermostat take the requested mode?" read-back (Issue #1049). See docs/08-COMPUTATION-REFERENCE.md "Command shapes since #301".
│
├── classification_fsm.py  # Classification decision FSM — stateless, composes decide_scheduled_band_gate() + decide_ode_ceiling_guard() (Issue #742). See §FSM Decision Layer.
├── ode_ceiling_guard.py    # Pure decision core: the ODE ceiling guard (proactive-cooling escalation) — Issues #136/#247/#392 Fix 1, extracted Issue #742.
│
├── economizer_fsm.py      # Economizer lifecycle FSM — unified (state, event) -> Transition table (Issue #746). Drives EconomizerLifecycleState from economizer_lifecycle.py. See §FSM Decision Layer.
├── economizer_lifecycle.py  # Pure session-state derivation for the economizer: EconomizerLifecycleState (3 states — INACTIVE, COOL_DOWN, MAINTAIN) (Issue #746).
├── economizer_gate.py     # Pure decision core: the economizer eligibility/phase-selection gate (Issue #746).
├── log_capture.py         # Real WARNING+/ERROR log-record capture for the AI Investigator (Issue #578).
│
│                         # --- Multi-zone support (Issue #796, v0.7.0) ---
├── zone_registry.py       # get_coordinator()/iter_coordinators()/get_default_coordinator()/list_zones() — resolves/enumerates zone coordinators by entry_id. See Multi-Zone Spec.
├── storage_paths.py       # resolve_entry_scoped_path()/migrate_legacy_storage_file() — entry-scoped persistence filenames for learning.py/state.py/chart_log.py/forecast_archive.py; remove_entry_scoped_files() deletes (with const.ENTRY_SCOPED_FILES) a removed zone's files (0.9.1, State Persistence spec). See Multi-Zone Spec.
├── diagnostics.py         # Native HA diagnostics download hook (async_get_diagnostics_payload()).
├── diagnostic_snapshot.py # Shared redacted diagnostic snapshot: build_diagnostic_snapshot() (thermostat live attributes + parsed capabilities + last_command block), recent_hvac_events(), redact_sensitive(). Consumed by diagnostics.py, the api.py log downloads and the AI investigator (Issue #1049).
├── incident_payload.py    # Shared `incident_detected` payload builder (build_incident_payload) and climate-error classifier (is_climate_validation_error) used by both the coordinator and the engine emitters (Issue #1049). Tier 2: incident-payload-brief.md.
└── frontend/            # Dashboard panel (iframe): index.html (classic) + new_dashboard.html (nd_*.js, the default) + entry.html (the panel entry) + the shared chart_v2.js/.css + the shared Prediction accuracy view pa_view.js (prediction_accuracy.html and the Analysis tab) + locally bundled Chart.js v4 + zoom plugin + HammerJS
```

## Data Flow

```
Weather Entity ──► Coordinator (every 30 min)
                       │
                       ├──► Classifier → DayClassification
                       │         │
                       │         ├──► Automation Engine (apply HVAC changes)
                       │         └──► Briefing Generator (daily email)
                       │
                       ├──► Door/Window Events → Automation Engine (pause/resume)
                       ├──► Thermostat Events → Learning Engine (track overrides)
                       ├──► Time Events → Automation Engine (bedtime/morning)
                       │
                       ├──► End of Day → Learning Engine (save DailyRecord)
                       │                      │
                       │                      └──► Suggestions (after 14+ days)
                       │
                       └──► AI Service Calls ──► claude_api.py (circuit breaker, budget)
                                                       │
                                                  ai_skills.py (skill registry)
                                                       │
                                       ai_skills_investigator.py ("investigator" — merged, Issue #563)
                                             + ai_skills_context.py (context providers)
                                                       │
                                             AI Status Sensor + Report History
```

## Key Data Structures

### ForecastSnapshot (classifier.py)
Contains today_high, today_low, tomorrow_high, tomorrow_low, current_outdoor_temp, current_indoor_temp, current_humidity, timestamp.

### DayClassification (classifier.py)
Produced by `classify_day()`. Contains day_type, trend_direction, trend_magnitude, and computed recommendations: hvac_mode, pre_condition, windows_recommended, window_open/close times, setback_modifier.

### DailyRecord (learning.py)
One day's tracked data: what was recommended, what actually happened, outcomes (runtime, overrides, comfort violations). Stored as JSON, rolling 90-day window. v0.3.48 adds five setback-visibility fields: `setback_heat_applied_f`, `setback_cool_applied_f`, `setback_depth_f`, `setback_was_adaptive`, `setback_skipped_reason` (written by the engine's bedtime handler into the coordinator's live record through `engine.inputs.today_record()` (`CoordinatorEngineInputs`); before 0.9.16 they were never recorded, Issue #1474). Since 0.9.16 (Issue #1475) the learning payload also carries `compliance.window_days` (the scores cover the last 14 records), `today_live` (`coordinator.analysis_today_live()`: the live window answer, the plan's remaining window periods, and the outdoor extremes and indoor average so far) and `cost_configured`. See [§6a DailyRecord setback fields](08-COMPUTATION-REFERENCE.md#6a-occupancy-aware-automation-guards-issue-85).

## Coordinator Scheduled Events

| Time | Event | Handler |
|------|-------|---------|
| Briefing time (default 6:00 AM) | Send daily briefing | `_async_send_briefing` |
| Wake time (default 6:30 AM) | Restore comfort setpoint | `_async_morning_wakeup` |
| Sleep time (default 10:30 PM) | Apply bedtime setback | `_async_bedtime` |
| 11:59 PM | Save daily record, reset | `_async_end_of_day` |
| Every 30 minutes | Refresh forecast + classification | `_async_update_data` |

## Coordinator State Listeners

| Entity | Event | Handler |
|--------|-------|---------|
| Door/window sensors | State change (open/closed) | `_async_door_window_changed` |
| Climate entity | State change (temp, mode) | `_async_thermostat_changed` |
| Occupancy toggle entities (home/vacation/guest) | State change (on/off) | `_async_occupancy_changed` |

## Event Log Retention (Issue #432)

`coordinator._event_log` is the in-memory list of automation events consumed by the dashboard Activity tab, the `event_log`/`activity_record` REST endpoints, and the AI investigator's `event_log` context provider. Retention is enforced by two bounds, checked in this order:

- **`EVENT_LOG_MAX_AGE_HOURS`** (168h / 7 days, `const.py`) — primary retention. Entries older than this are evicted regardless of count.
- **`EVENT_LOG_CAP`** (4000, `const.py`) — memory-safety backstop only, applied after the age eviction. Prior to this issue `EVENT_LOG_CAP` (then 500) was the *only* bound, which meant a busy day could silently shrink the retained history to a few hours.

Both bounds are enforced by a single function, `_prune_event_log(event_log, now)` in `coordinator.py`, called identically from the live event-emit path (`_emit_event()`) and from restart/restore (`async_restore_state()`). There is no separate re-cap logic on either path, so the two can't drift out of sync with each other — and no state-file migration/version bump was needed, since both paths already funnel through the same function.

**Consumer-side docs:** [Activity Report Table Spec](activity-report-table.md) covers the event timeline table renderer (used by both the dashboard and `activity_record`). [AI Skills Spec](ai-skills-spec.md#event-log-provider) covers the `event_log` context provider, including the `filter_events_by_window()` filter-then-limit helper and the distinction between a context-budget trim (`limited`) and the log not holding enough history (`is_truncated`, surfaced by the REST layer — see [REST API Brief](rest-api.md#event-log-truncation-signal)).

## Coordinator Thermal State Machine Methods

These methods on `ClimateAdvisorCoordinator` implement the Issue #121 v3 concurrent-observation pipeline. They are driven by thermostat state changes detected in `_async_thermostat_changed` and by the 30-minute coordinator tick.

| Method | Role |
|--------|------|
| `_start_hvac_observation(session_mode)` | Begins an HVAC (heat or cool) observation; records the on-edge indoor reading |
| `_start_decay_observation(obs_type)` | Begins one of the four rolling-decay observation types |
| `_sample_all_observations()` | Per-tick: samples all active observations in `_pending_observations`; starts new decay types when trigger conditions first become true |
| `_end_hvac_active_phase(obs_type)` | At the HVAC off edge closes the pending HVAC observation (swing/settle bookkeeping); no rate is committed here, the cycle recorder closes the cycle and the cycle model refits (Issues #1512/#1516) |
| `_on_cycle_closed(cycle)` (cycle path) | Stores the closed cycle in `CycleStore`, refits that mode with `cycle_model.fit_cycles` in the executor, then `learning.apply_cycle_fit(mode, result, primary_is_v3)`; an ineligible or excluded cycle records a `cycle_*` rejection code |
| `_evaluate_rolling_window(obs_type)` | Commit/keep-alive decision for rolling-window decay types (30-min min, 240-min cap) |
| `_commit_rolling_window_obs(obs_type)` | Calls `_commit_observation()` for a rolling obs that has passed the signal check |
| `_commit_observation_if_sufficient(obs_type)` | Commits if `len(samples) >= min_samples`, else abandons — used on HVAC-start contamination flush |
| `_abandon_observation(obs_type, reason)` | Removes obs from `_pending_observations`; appends to rejection log; logs WARNING |
| `_commit_observation(obs_type)` | Calls `learning.record_thermal_observation()` and pops obs from `_pending_observations` |

All six concurrent types are tracked in `_pending_observations: dict[str, PendingObservation]`. Observations are never persisted: after an HA restart or zone reload the Issue #96 late start opens a fresh HVAC observation, and the pre-restart part of the run is not learned from, by design (Issue #1437: a fit across the blind gap would be invalid). See [Thermal Model v3 Spec](thermal-model-v3-spec.md) for the full observation-type matrix and lifecycle.

> **v2 pipeline (retired):** the v2 HVAC-specific methods (`_start_thermal_event`, `_sample_thermal_event`, `_end_active_phase`, `_check_stabilization`, `_commit_thermal_event`, `_abandon_thermal_event`) no longer exist, and since Issue #1512 neither do the pre-heat buffer (`_update_pre_heat_buffer`) and the v3 `_check_hvac_stabilization`: a heating/cooling run is committed or rejected at its off edge (`_end_hvac_active_phase`), so nothing after the off edge is sampled.

## FSM Decision Layer


**One owner for "ventilate or pause" an open door/window (Issue #1142).** The nat-vent FSM (`nat_vent_fsm._transition_from_inactive`: override, grace, lockout, full gate, soft start, "a fan exists", forecast-peak and thermal-floor guards, `fan_off_hold_active`) is the sole owner of "may a fan session start?". One shell helper, `AutomationEngine._nat_vent_start_decision(profile)`, builds its inputs from a `NatVentStartProfile` (`NAT_VENT_START_WINDOW_OPEN`, `_GRACE_RECHECK`, `_IDLE_OPEN`, `_PAUSED_REACTIVATION`, `_ROUTINE_RECHECK`, `NAT_VENT_START_PREDICTION`) and returns the FSM's own transition; it is glue, not a second decision function. The door/window FSM only RECORDS a pause it is told about (`SENSOR_OPENED` = "the caller chose to pause"; `GRACE_TIMER_EXPIRED` reads `nat_vent_will_start` from the same helper). `door_window_open_response.decide_door_open_response()` is the handler's real guard chain (paused, protected change, grace, gate, fan-off hold, lockout, veto, start). The safety nets that pause outside the handler (scheduled-routine re-check, comfort-band guard) ask the helper and pause quietly when a start is due. Blower mode never heats/cools into an open window (`_apply_nat_vent_hvac_state` holds the thermostat Off; exits with a window open switch it Off at once). The planned window gates the pause (lazy sync only), never the start (#1139, #1211).
Block 5's FSM migration (epic #594) built seven FSMs (five named-state, two deliberately stateless) — one per lifecycle already governed by ad-hoc boolean flags — driven by Issues #633, #637, #639, #742, #744, #746, and #731. During the migration, `_*_fsm_authoritative` flags (Issue #729) were introduced as an intermediate step to run legacy and FSM paths in parallel via `switch.climate_advisor_shadow_engine_primary`. Phase 6 graduation (Issue #757) completed in seven steps, removing all flags and the shadow engine infrastructure: each FSM is now the sole authoritative implementation with no toggle and no comparison. All automation decisions flow directly through the FSMs with zero legacy branches remaining.

**Note on all rows below:** Each FSM path was permanently authoritative in production for weeks with zero corpus divergence before its legacy branch was removed in Phase 6 graduation (Issue #757).

| FSM module | State type | Cutover switch | Authoritative today? |
|---|---|---|---|
| `nat_vent_fsm.py` (Issue #633, legacy removed Issue #757) | `NatVentLifecycleState` (`nat_vent_lifecycle.py`) — `INACTIVE`, `ACTIVE_FULL_GATE`, `ACTIVE_SOFT_START`, `PAUSED_REACTIVATION_LOCKOUT` | *(none — see below)* | Sole implementation. Removal of the `_natvent_fsm_authoritative` flag surfaced two pre-existing bugs (a broken grace-bypass and missing reactivation-lockout arm); both fixed in Phase 6 graduation (Issue #757). |
| `door_window_fsm.py` (Issue #637, legacy removed Issue #757) | `DoorWindowLifecycleState` (`door_window_lifecycle.py`) — `NORMAL`, `PAUSED_ACTIVE`, `PAUSED_IDLE`, `GRACE`, `PAUSED_DURING_GRACE` | *(none — see below)* | Sole implementation. `_resolve_door_window_pause_flags()` now unconditionally dispatches through the FSM. Removal of the `_doorwindow_fsm_authoritative` flag surfaced two pre-existing bugs (wrong `fan_mode` default and SENSOR_OPENED redundant re-derivation); both fixed in Phase 6 graduation (Issue #757). |
| `override_grace_fsm.py` (Issue #639, legacy removed Issue #757) | Composite `(OverrideConfirmState, GraceState)` (`override_grace_lifecycle.py`) — `OverrideConfirmState`: `IDLE`/`PENDING`; `GraceState`: `NONE`/`ACTIVE_PROTECTING_OVERRIDE`/`ACTIVE_UNPROTECTED` | *(none — see below)* | Sole implementation. `_resolve_override_grace_fsm_state()` (12 call sites) now unconditionally dispatches through the FSM via `_apply_override_grace_fsm_state()`. Legacy `_override_grace_fsm_authoritative` flag removed in Phase 6 graduation (Issue #757). |
| `fan_fsm.py` (Issue #731, legacy removed Issue #757) | Composed `FanLifecycleState` (`fan_lifecycle.py`) — 5 independent axes: `physical`, `override`, `cycling`, `hvac_ownership`, `rate_limit` (because a WHF session occupies several axes simultaneously) | *(none — see below)* | Sole implementation. `_resolve_fan_fsm_state()` (17 call sites) now unconditionally dispatches through the FSM via `_apply_fan_fsm_state()`. Legacy `_fan_fsm_authoritative` flag removed in Phase 6 graduation (Issue #757). |
| `classification_fsm.py` (Issue #742, legacy removed Issue #757) | **None** — deliberately stateless; composes `desired_state.decide_scheduled_band_gate()` with `ode_ceiling_guard.decide_ode_ceiling_guard()` | *(none — see below)* | Sole implementation. `apply_classification()` now unconditionally dispatches through `_resolve_classification_fsm_state()` via `_apply_ode_ceiling_guard_decision()`. This was the final subsystem to graduate in Phase 7 (Issue #757); all FSM authoritative flags removed. |
| `occupancy_fsm.py` (Issue #744, legacy removed Issue #757) | **None** — deliberately stateless; two decision functions (`decide_away_vacation_dispatch()` and `decide_home_dispatch()`) instead of one `transition()` since the 3 call sites are distinct method shapes. Priority resolution via `occupancy_priority.py`'s `decide_occupancy_priority()`. | *(none — see below)* | Sole implementation. The three occupancy handlers now unconditionally dispatch through `_resolve_occupancy_away_vacation_fsm_state()` and `_resolve_occupancy_home_fsm_state()`. Legacy `_occupancy_fsm_authoritative` flag removed in Phase 6 graduation (Issue #757). |
| `economizer_fsm.py` (Issue #746, legacy removed Issue #757) | `EconomizerLifecycleState` (`economizer_lifecycle.py`) — `INACTIVE`, `COOL_DOWN`, `MAINTAIN` (a genuine multi-tick session, unlike stateless classification/occupancy) | *(none — see below)* | Sole implementation. `check_window_cooling_opportunity()` now unconditionally calls `_check_window_cooling_opportunity_fsm()` via `_apply_economizer_fsm_state()`. Legacy `_economizer_fsm_authoritative` flag removed in Phase 6 graduation (Issue #757). |

During the Phase 5 migration (Issues #613-#731), a shadow `AutomationEngine` instance (`coordinator.shadow_automation_engine`) ran parallel to production to A/B-test FSM implementations via mirror axes and differential comparators. This provided live validation that each new FSM produced identical decisions to the legacy path before removing the legacy code. Phase 6 graduation (Issue #757) completed the removal in seven sequential steps (Steps 1–7), deleting: all legacy code branches, all `_*_fsm_authoritative` flags, all per-FSM mirror axes and their comparators, all shadow-FSM-replay machinery, and finally the entire shadow engine instance itself. As of Phase 6 Step 7 completion, only the canonical production `AutomationEngine` remains — each FSM is the sole authoritative implementation for its lifecycle, and zero shadow/diagnostic machinery persists. See [Nat-Vent Lifecycle Spec](nat-vent-lifecycle-spec.md), [Grace Periods Spec](grace-periods-spec.md), [Fan Lifecycle Spec](fan-lifecycle-spec.md), [Classification Lifecycle Spec](classification-lifecycle-spec.md), [Occupancy Dispatch Spec](occupancy-dispatch-spec.md), and each module's own docstring for the final transition tables and per-lifecycle scope boundaries.

### Dispatcher Wiring (Issue #717)

`lifecycle_dispatcher.py`'s generic pub/sub router (built Issue #633) was wired into a real production decision path by Issue #717/PR #720. `AutomationEngine` owns its own `LifecycleDispatcher` instance and registers as controller for all 8 event types (`DOOR_PAUSE_STARTED/ENDED`, `GRACE_STARTED/ENDED`, `OVERRIDE_CONFIRMED/CLEARED`, `NAT_VENT_SESSION_STARTED/ENDED`). Real `emit()` calls sit at the chokepoints every genuine transition funnels through: `_resolve_door_window_pause_flags()` and `_resolve_override_grace_fsm_state()` (before/after diffs of `_paused_by_door`/`_grace_active`), `_apply_nat_vent_fsm_state()` (nat-vent's primary state writer), `_confirm_override_action()`/`_clear_manual_override_active()` (single unconditional sites for `OVERRIDE_CONFIRMED`/`CLEARED`), and a before/after diff of `_natural_vent_active` wrapped around `_decision_pass()` — the one serialization point all ~18 scattered nat-vent write sites already pass through, chosen instead of instrumenting each site individually.

**This is a same-instance emit/consume round-trip today, not a cross-instance mirror — and that distinction is deliberate, not a shortcut.** An earlier draft of this change routed `_build_nat_vent_fsm_inputs()`/`_build_door_window_fsm_inputs()` through dispatcher-synced mirror attributes (`_dispatched_paused_by_door`, `_dispatched_grace_active`, etc.) instead of the canonical `_paused_by_door`/`_grace_active`/`_manual_override_active`/`_natural_vent_active` attributes. It was reverted: `AutomationEngine` both emits and consumes every one of these events on itself, so the canonical attributes can never actually go stale relative to a same-object mirror the way `coordinator.py`'s (now-deleted) `_sync_shadow_inputs()` mirror genuinely could (that one existed precisely because production and shadow *were* separate instances — see the historical shadow-engine paragraph above; the mechanism was removed in Phase 6 graduation, Issue #757). Routing the FSM builders through the dispatcher-only mirror also broke the established direct-attribute-assignment fixture convention used across 40+ existing test files, for no real staleness benefit. The dispatcher's mirror attributes (`_dispatched_*`) still exist and are still populated by `_on_lifecycle_event()` — they're an observability/diagnostic round-trip proof (exercised by `check_registry_completeness()` and the dispatcher's own `event_log`), not the FSM builders' source of truth. The FSM input builders read the canonical attributes directly, unchanged by this wiring. Zero decision logic, zero authoritative-switch behavior, and zero HA service call path changed — see PR #720's commit message for the full before/after reasoning.

## Coordinator Chart Helper Functions

Pure module-level functions called by `get_chart_data()` to build the dashboard temperature forecast payload.

| Function | Role |
|----------|------|
| `_compute_target_band_schedule(hourly_timestamps, config, occupancy_mode, now)` | Returns `[{ts, lower, upper}]` — the occupancy-aware dynamic target band for each forecast hour. Away/vacation applies flat setback for today only; home/guest uses wake/sleep ramp schedule. Future days always use the home schedule. (Issue #119) |
| `_build_predicted_indoor_future(hourly_forecast, config, now, ...)` | Returns `[{ts, temp}]` — predicted future indoor temperatures. Uses physics ODE when model has confidence ≥ `"low"` and `k_passive < 0`; falls back to ramp interpolation otherwise. Accepts `occupancy_mode` parameter and calls `_compute_target_band_schedule()` internally so prediction and target band share a single source of truth. (Issues #114, #119) |
| `_build_future_forecast_outdoor(hourly_forecast)` | Returns `[{ts, temp}]` — raw hourly outdoor forecast temperatures. |
| `_simulate_indoor_physics(t_current, t_outdoor, q, k_passive, dt_hours)` | Single ODE time step for the physics path. |
| `_compute_ramp_hours(temp_delta, rate)` | Computes ramp duration for the legacy fallback path. |
| `_synthesize_hourly_day(high, low, day_date)` | Returns 24 `{ts, temp}` entries for one calendar date, cosine-anchored to that day's forecast high/low. Extracted from `_build_predicted_indoor_future()`'s existing no-hourly-forecast fallback so the same synthesis logic is shared by both that fallback and `_build_extended_hourly_forecast()` below. Timestamps are built via `dt_util.as_local(datetime.combine(day_date, time(hour=h)))` (DST-safe per-date offset), not timedelta arithmetic on an already-localized datetime. (Issue #906) |
| `_build_extended_hourly_forecast(hourly_forecast, daily_forecast, now, unit)` | Returns `hourly_forecast` extended with synthetic days beyond the weather integration's native hourly horizon (e.g. met.no's ~48h), one synthesized via `_synthesize_hourly_day()` per additional day present in the longer-range `daily_forecast`. Each synthetic entry is tagged `"synthetic": True`. Chart-display-only — feeds `get_chart_data()`'s `predicted_indoor`/`forecast_outdoor`/target-band/activity-bar series exclusively; never assigned back onto `self._hourly_forecast_temps` (the engine holds no copy, Issue #1443), so no live control decision (nat-vent gate, ceiling guard, nat-vent plan/cutoff) is affected. Falls back to the unmodified real list (no-op) if `daily_forecast` entries don't look day-spaced (median spacing < 20h — guards against the deprecated hourly-shaped `forecast` fallback attribute). (Issue #906) |

`get_chart_data(range_str: str = "24h", before_ts: float | None = None)` — the `before_ts` parameter (Unix milliseconds, Issue #160) enables historical navigation. When supplied, the data window is anchored at that timestamp instead of the current clock: `get_entries(range_str, before=datetime.fromtimestamp(before_ts/1000, UTC))`. The frontend passes `before_ts` to scroll the chart to any past window without losing the current live view.

**Data window vs viewport:** `before_ts` shifts the *data window* (what the API fetches from the chart log). The frontend *viewport* is a separate concern — range preset buttons and drag-to-zoom control the display without necessarily changing `before_ts`. When `before_ts` is absent, the window ends at now (live mode).

`get_chart_data()` also returns two setpoint arrays added in v0.3.48 (Issue #151):

| Response key | Content |
|---|---|
| `historical_setpoint` | `[{ts, temp}]` — actual thermostat `temperature` attribute captured at each 30-min poll; `null` entries when HVAC mode is off |
| `predicted_setpoint` | `[{ts, temp}]` — future setpoint derived from target band (lower bound in heat mode, upper bound in cool mode, null in off mode) |

Both are rendered in the dashboard as a stepped purple/magenta line: solid past, dashed future, faint-dotted forward-fill during off-mode periods. Toggle via the "Thermostat Setpoint" overlay checkbox (hidden by default).

## Sensors Exposed

| Entity ID | Value | Extra Attributes |
|-----------|-------|-----------------|
| `sensor.climate_advisor_day_type` | hot/warm/mild/cool/cold | trend_direction, trend_magnitude |
| `sensor.climate_advisor_trend` | warming/cooling/stable | (dynamic icon) |
| `sensor.climate_advisor_next_action` | Human-readable next action | — |
| `sensor.climate_advisor_daily_briefing` | TLDR summary, capped at 250 chars (HA's hard limit is 255 — Issue #555) | full_briefing (complete text), tldr (untruncated TLDR) |
| `sensor.climate_advisor_comfort_score` | 0–100% | `pending_suggestions`, `comfort_violations_minutes_today`, `comfort_range_low`, `comfort_range_high` |
| `sensor.climate_advisor_status` | active/inactive | — |
| `sensor.climate_advisor_occupancy_mode` | home/away/vacation/guest | occupancy_entity_states (raw toggle states) |
| `sensor.climate_advisor_ai_status` | active/inactive/error/disabled/circuit_open | last_request_time, error_count, total_requests, model_in_use, circuit_breaker, monthly_cost_estimate, auto_requests_today, manual_requests_today |

## Services Registered

| Service | Data | Purpose |
|---------|------|---------|
| `climate_advisor.respond_to_suggestion` | action (accept/dismiss), suggestion_key | User responds to learning suggestion |

## Configuration Data (from config flow)

The initial setup flow (`ClimateAdvisorConfigFlow`) runs `user` → `unit` → `setpoints` → `temperature_sources` (+ optional `outdoor_temp_entity` / `indoor_temp_entity` steps) → `sensors` → `occupancy` → `schedule`; `schedule` creates the entry. The options flow (`ClimateAdvisorOptionsFlow`: `init` menu → `core`, `setpoints`, `temperature_sources`, `sensors`, `occupancy`, `schedule`, `scheduler`, `notifications`, `advanced`, `classification_thresholds`, `ai_settings`, `github_settings`) is a separate set of steps. Since Issue #1061 the initial flow no longer asks timing delays, HVAC fan restriction, advanced fan settings, the welcome-home period, or the sleep indoor sensor — those are options-only and read with `config.get(KEY, DEFAULT)`. Field-building rules (labels, descriptions, units, setup-vs-options split) live in the CLAUDE.md "Config UI Field Standard (Issue #1061)" section. #1230 R5c (DP-10): the options-flow `advanced` step also holds three OPTIONAL energy fields, `energy_rate` (currency per kWh), `heat_power_kw` and `cool_power_kw`; none has a default and none is migrated (blank = not set, cleared through `_commit_section(clearable_keys=...)`). The end-of-day record fills `DailyRecord.estimated_cost` only when the rate and the power rating for each mode that ran are all set (`learning.estimate_daily_cost`, from the per-mode `hvac_heat_runtime_minutes` / `hvac_cool_runtime_minutes`); otherwise it stays `None`. Every option-flow setting has a default in `CONFIG_METADATA` (`default`) or is marked `no_default`; the dashboard's changed-from-default markers read only that (`dashboard_api.default_for`), enforced by `tests/test_config_metadata.py`.

```
weather_entity: weather.home
climate_entity: climate.living_room
outdoor_temp_entity: sensor.outdoor_temp (optional)
indoor_temp_entity: sensor.indoor_temp (optional)
thermostat_companion_sensor: auto | off (Issue #1230 R1b; default auto - use the thermostat's finer same-device
  temperature sensor for indoor control once proven; see docs/08-COMPUTATION-REFERENCE.md "Bug D")
sleep_indoor_temp_entity: sensor.bedroom_temp (optional — Issue #895; used for comfort/automation
  instead of the primary indoor sensor while the sleep schedule is active, never for thermal learning;
  since Issue #1194 (a) it is also the remote sensor of the setpoint outer loop, see
  docs/08-COMPUTATION-REFERENCE.md "Remote-sensor outer loop"; since (c) bedtime and wake are explicit context
  entry and exit: the bedtime band is written at offset 0 BEFORE the loop's first decision of the night, the wake
  band is written at offset 0 (the offset is cleared first), and if no wake write comes the loop itself writes the
  plain target once after a 120 s grace -- see "Context entry and exit (Issue #1194 c)" there)
indoor_humidity_entity: sensor.hallway_humidity (optional, Issue #1414; chart display only, blank = detect
  automatically: a humidity sensor on the thermostat's or comfort sensor's device, else the thermostat's
  current_humidity)
sleep_indoor_humidity_entity: sensor.bedroom_humidity (optional, Issue #1414; used for the chart while the sleep
  sensor is the comfort reading; blank = a humidity sensor on the sleep sensor's device, else none)
outdoor_humidity_entity: sensor.patio_humidity (optional, Issue #1414; blank = a humidity sensor on the outdoor
  sensor's device, else the weather entity's humidity)
comfort_heat: 70 (°F)
comfort_cool: 75 (°F)
setback_heat: 60 (°F)
setback_cool: 80 (°F)
notify_service: notify.mobile_app_phone
door_window_sensors: [binary_sensor.back_door, binary_sensor.all_windows, ...]  # any binary_sensor, including groups
sensor_polarity_inverted: false  # true if sensors report on=closed instead of on=open
sensor_debounce_seconds: 600    # how long a door/window sensor's state must hold steady before Climate Advisor reacts — pause/resume HVAC or nat-vent/WHF/HVAC-fan engage/exit (default 10 min as of Issue #504, was 5 min; UI: 0–60 min)
manual_grace_seconds: 1800      # hands-off window after user manually turns HVAC on (default 30 min, UI: 0–240 min)
manual_grace_notify: false      # send notification when manual grace period expires
automation_grace_seconds: 300   # settling period after Climate Advisor auto-resumes HVAC (default 5 min, UI: 0–240 min)
automation_grace_notify: true   # send notification when automation grace period expires
# Note: Config UI displays minutes; values are stored internally as seconds
wake_time: "06:30"
sleep_time: "22:30"
briefing_time: "06:00"
occupancy_home_entity: binary_sensor.someone_home (optional)
occupancy_home_inverted: false          # true if on=away instead of on=home
occupancy_vacation_entity: input_boolean.vacation_mode (optional)
occupancy_vacation_inverted: false
occupancy_guest_entity: input_boolean.guest_mode (optional)
occupancy_guest_inverted: false
```

### Debounce and Grace Period System

**Debounce** (`sensor_debounce_seconds`): A door or window sensor's state must hold steady for this duration before Climate Advisor reacts to it. This applies to every controlled device, not just HVAC — pausing/resuming HVAC (`handle_door_window_open()`), and, as of Issue #504, engaging/exiting natural-ventilation fan control (`check_natural_vent_conditions()`'s idle_open reactivation, gated on the same coordinator-tracked debounce timers via `_sensor_debounce_pending_callback`). Quick pass-throughs or a flaky sensor bouncing open/closed within the window have no effect on either. Default: 10 minutes (was 5 minutes prior to Issue #504).

**Manual grace period** (`manual_grace_seconds`): After the user manually turns HVAC back on during a door/window pause, Climate Advisor stays hands-off for this duration. Door/window sensors cannot trigger another pause during this window — the user just overrode the system and should not be immediately overridden back. Default: 30 minutes. Notification on expiry: off by default.

**Automation grace period** (`automation_grace_seconds`): After Climate Advisor itself resumes HVAC (all doors/windows closed), it waits this duration before door/window sensors can trigger another pause. This prevents rapid cycling when someone is moving in and out. Default: 5 minutes. Notification on expiry: on by default so the user knows normal sensing has resumed.

Setting either grace period to 0 disables it entirely.

**Timer priority**: Manual override always takes highest priority. When a user manually turns HVAC on during a door/window pause:
1. The pause is immediately lifted (`paused_by_door` → False)
2. All pending debounce timers for still-open sensors are cancelled
3. A manual grace period starts, blocking any new pause events for its configured duration
4. After the grace period expires, normal door/window sensing resumes

This ensures the user's explicit action is never overridden by a stale or pending debounce timer.

```
Sequence: sensor opens → debounce timer → HVAC paused → user turns on
           → manual grace starts (all debounce timers cancelled)
           → grace expires → normal sensing resumes
```

**Briefing integration**: The daily briefing automatically mentions active grace periods so users understand why door/window sensing may behave differently than expected. The fresh air section also shows the actual configured debounce duration (e.g., "5 minutes" instead of a hardcoded value) so the briefing always reflects the user's settings.

## Integration Version

- **Canonical version**: `manifest.json` `"version"` field (shown in HA integrations UI)
- **Python constant**: `const.VERSION` (used in startup logs, API responses, diagnostics)
- **Format**: semantic versioning (`MAJOR.MINOR.PATCH`)
- **Sync rule**: both locations MUST match. A test in `tests/test_version_sync.py` enforces this automatically.
- **When releasing**: update both `const.py` and `manifest.json`.

Note: `config_flow.VERSION` (config entry schema) and `state.STATE_VERSION` (state file format) are separate internal versioning concerns and do not track the integration release version.

## Constants (const.py)

### Day Type Thresholds
- HOT: ≥ 85°F
- WARM: ≥ 75°F
- MILD: ≥ 60°F
- COOL: ≥ 45°F
- COLD: < 45°F

### Trend Thresholds
- Significant: 10°F+ change
- Moderate: 5°F+ change

### Timing Defaults
- Sensor debounce: 300 seconds (5 min)
- Manual grace period: 1800 seconds (30 min)
- Automation grace period: 300 seconds (5 min)
- Occupancy setback delay: 15 minutes
- Max continuous runtime alert: 3 hours

### Learning Parameters
- Min data points before suggesting: 14 days
- Suggestion cooldown: 7 days
- Low compliance threshold: 30%
- High compliance threshold: 80%
- Data retention: 90-day rolling window
- Storage: JSON file in HA config dir

### Thermal Model Parameters (Issue #114)
- Cycle model gates (Issues #1512/#1516): cycle ≥ 3 min and readings moved ≥ 3 resolution steps; prior from the typical house or the stored rate by evidence; evidence gives way only to newer cycles (`THERMAL_EVIDENCE_HALF_N` 20, contradicting 5; never with time); at most the last 60 eligible cycles (`cycle_model.py`, see 05-LEARNING-ENGINE-DESIGN.md § Parameter Extraction)
- Rate ageing: weighted median of retained observations, each weight halved per 20 newer observations of its rate (`THERMAL_EVIDENCE_HALF_N`; no calendar clock, Issue #1523), legacy-method weight 0.25 (`THERMAL_LEGACY_OBS_WEIGHT`); observations kept per kind (`THERMAL_OBS_KIND_CAPS`, sharing `THERMAL_OBS_CAP` 200), none leaves with time
- Sample interval: 60 seconds (`THERMAL_SAMPLE_INTERVAL_SECONDS`)
- Minimum R² for k_passive acceptance: 0.2 (`THERMAL_MIN_R_SQUARED`)
- Default sample floor of the passive `compute_k_passive` regression: 4 (`THERMAL_MIN_POST_HEAT_SAMPLES`); heating/cooling runs no longer use a post-heat regression (Issue #1512)
- k_passive sanity bounds: −0.5 to −0.001 hr⁻¹
- k_active_heat sanity bounds: 0.5 to 15.0 °F/hr
- k_active_cool sanity bounds: −15.0 to −0.5 °F/hr

### Chart Log Parameters
- Entry cadence: every coordinator tick (~30 min)
- Retention cap: 365 days rolling (~17,500 entries ≈ 2MB)
- Downsampling: raw points ≤3 days; hourly averages 4–30 days; daily summaries >30 days
- Storage: `climate_advisor_chart_log.json` in HA config dir
- Row fields added in 0.9.1 (Issue #1230 R0): `indoor_sensors` (per-sensor °F map; see [Chart Log Spec](chart-log-spec.md#indoor-sensors-field-issue-1230-r0)); 0.9.2 adds the climate entity's device-companion temperature sensors to that map

### Forecast Archive Parameters
- Snapshot cadence: at most one per local clock hour (latest changed fetch in the hour wins; an unchanged refetch writes nothing), only from a successful fetch that returned entries
- Horizon: 72 hours from the issue hour; retention 365 days (~3.9 MB)
- Storage: `climate_advisor_forecast_archive_<entry_id>.json` in HA config dir (see [Forecast Archive Spec](forecast-archive-spec.md))

### v4 Shadow Parameters (Issue #1230 R1)
- Fit: nightly 03:15 local (`V4_FIT_HOUR`/`V4_FIT_MINUTE`), first fit after startup coalescing when no model exists, retry of a raised first fit at most every 6 h (`V4_FIT_RETRY_MIN_INTERVAL_S`)
- Predict: every 30-min cycle, 24 h horizon (`V4_PREDICT_HORIZON_HOURS`) on a 15-min grid; archive leads `1h`/`4h`/`12h`/`24h` (`V4_ARCHIVE_LEADS`), retention 400 days (`V4_ARCHIVE_MAX_DAYS`), save at most hourly (`V4_ARCHIVE_SAVE_MIN_INTERVAL_S`), hidden-mass state re-seeded after a 2 h gap (`V4_MASS_MAX_GAP_S`)
- Storage: `climate_advisor_v4_archive_<entry_id>.json`, `climate_advisor_v4_model_<entry_id>.json`, `climate_advisor_v4_backtest_<entry_id>.json` (published by the backtest tool); all in `ENTRY_SCOPED_FILES` (see [v4 Shadow Spec](v4-shadow-spec.md#files))

## Prediction Accuracy Preview Page (Issue #1230 R1)

Occupant view: an owner who wants to know "is the new prediction any good?" opens a read-only page that draws what the room actually read next to what the main engine and the comparison engine (Settings, Prediction engine) said a few hours earlier. It changes no setting and no decision.

- **Files:** `custom_components/climate_advisor/frontend/pa_view.js` (Issue #1497: the view itself, shared: markup, styles scoped to `.pa-view`, auth, requests, chart, tiles, scorecard; `window.CAPredictionAccuracy.mount(root, {zone, embedded, onZones})` and `.setZone(entryId)`; one view per document) and `custom_components/climate_advisor/frontend/prediction_accuracy.html` (the stand-alone page: its header with the zone picker and "Back to dashboard", around `#pa-root`), served by the same static path as the dashboard (`PANEL_URL`, registered in `__init__.py` `async_setup_entry`) together with the bundled `chart.umd.min.js`, `chartjs-plugin-zoom.min.js` and `hammer.min.js`. Payload: `prediction_accuracy.py`; tests: `tests/test_prediction_accuracy_page.py`, `tests/test_prediction_accuracy_1431.py`, Playwright `tests/ui/accuracy-1431.spec.js`.
- **Entry:** classic dashboard `frontend/index.html` has a card with the link "Prediction accuracy: how each engine has scored" (`id="prediction-accuracy-link"`, relative `prediction_accuracy.html`); the new dashboard's Analysis card "Prediction accuracy" draws the full view (Issue #1497: `pa_view.js` mounted into `#an-pa-view` with `embedded: true` by `loadAccuracy()` in `nd_analysis.js` on the tab's first show, on the dashboard's zone; later shows and the `zone` event call `setZone(ND.zone)`; embedded, the two parts are blocks inside the card instead of cards, the view's own title is visually hidden and it has no zone picker or Back of its own) and links the page ("Open on its own page", `nd_analysis.js` adds `?zone=`; subtitle "How well each prediction engine guessed this home ...", no "preview"/"shadow" wording since Issue #1431); Debug's "Backtest lab" links it too ("Open the full accuracy view"); the Status chart note links "How each engine has scored". The page has "Back to dashboard".
- **Never framed, never a new tab (Issue #1491):** the dashboard is already inside Home Assistant's panel iframe. Until Issue #1491 the Analysis card embedded this page in a second iframe (`#pa-frame`, from R5) and the two links opened a new tab (`target="_blank"`). One frame deeper, the page's login lookup found no `<home-assistant>` in `window.parent` and fell back to `localStorage['hassTokens']`, which desktop browsers keep ("keep me logged in") but the iOS app (external auth) never writes: on a phone the card showed a 401 and no data, and the new tab had no HA around it at all. Now both links navigate the dashboard's own frame (no `target`), each remembers the tab it left (`ND.rememberReturnTab()`, `sessionStorage['climate_advisor_return_tab']`), and the next dashboard boot with no `#tab` (the page's "Back to dashboard" goes to `entry.html`) opens that tab and clears the key (every boot and every back/forward-cache restore clears it, so it is used at most once). As a second safety net each page's HA-login lookup (`hassAuth()` here, `haAuth()` in `nd_core.js`, `_getHassAuth()` in classic `index.html`) tries `window.parent`, then `window.top`. Tests: `tests/test_new_dashboard.py` (`..._1491`), Playwright `tests/ui/accuracy-same-frame-1491.spec.js` (same-frame links, Back to the tab, a page one frame below HA sends the top window's token). Issue #1491 itself left only a summary on the card; Issue #1497 put the full view back on the tab by drawing it in the dashboard's own document (the shared `pa_view.js`), so it uses the same login lookup as the rest of the dashboard and still never sits in a frame (`tests/test_new_dashboard.py` `..._1497`, `tests/test_prediction_accuracy_page.py`, Playwright `tests/ui/accuracy-analysis-tab-1497.spec.js`: drawn in the card, controls work in place, follows the dashboard's zone, a panel-frame geometry sends HA's own token, the stand-alone page unchanged, phone width).
- **Controls:** a zone picker (shown only with more than one zone; persisted under `localStorage` key `climate_advisor_selected_zone`, shared with the dashboard; `?zone=` overrides); **Sensor** group (`CA reading` first, then one button per configured indoor sensor from the response's `sensors`; Issue #1431: a sensor with `has_v4: false` reads "<name> · no v4 data yet", one with neither v4 nor readings is `aria-disabled` and does nothing, each with its reason in the hover title; `_ca` is always selectable); **Predicted** group (`1h`, `4h`, `12h`, `24h`; "what a model said N hours earlier"); **range** group (`6h 12h 24h 3d 7d 30d 1y`, default `3d`) with scroll buttons `‹` / `›` (move the window by one range using `before`; forward snaps back to live and is disabled when live); chart drag (or pinch) zoom with a **Reset zoom** button that appears when zoomed.
- **Requests:** exactly one fetch per control state, `GET /api/climate_advisor/prediction_accuracy?zone=&sensor=&lead=&range=[&before=]` (the dashboard's authed-fetch pattern: HA bearer token, one refresh-and-retry on 401); an in-flight request is aborted when a newer one starts. Zone list: `GET /api/climate_advisor/zones` at load (Issue #1454: never an unscoped `status`; the picker shows only when `zone_count > 1`); with no zone loaded yet, or a failed request, the page re-reads it and retries the same zone after 2 s, 5 s, 10 s, then says Climate Advisor is starting. Nothing else is read; nothing is written. Zoom is client-side only (no request); tiles are re-scored locally from the points in view.
- **Display:** actual (solid), v4, v3 (dashed; only for `CA reading` at `4h`) and the "no change" guess (dotted, muted) on one time axis, heat runs shaded. The key names the engines by their role from `primary_engine`/`shadow_engine`: "v4 (main engine)", "v3 (comparison)", "(not running now)". **Lines (Issue #1431):** each series is drawn from its own points only and joined into a line; it breaks only at a real gap (more than 1.5x the expected spacing: the slot length - 30 min raw, 1 h hourly, 1 day daily - or the series' own median spacing capped at 1 h, so the backtest's hourly points still join) and, for `CA reading`, just before every time in `ca_swaps` (bedtime / wake-up; a dashed mark and a "Sensor swap" key entry show them), like the Status chart's `_withBreaks`. A point with no neighbour is drawn as a dot. Under the chart one plain line (`#pa-explain`) says each point is a past prediction drawn at the time it was about and that lone dots mean spaced-out predictions; `#pa-nov4` says why there is no v4 line ("v4 hasn't learned this sensor yet (see #1422)", "v4 is not running", or "No v4 predictions for this sensor in this view"); at `7d`/`30d` the lines are hourly averages and at `1y` daily averages (the response's `downsample`). Hover/keyboard readout (focus the chart, Left/Right arrows step through points). Light/dark follows the system; temperatures are shown in the response's `unit` with no client conversion; phone width supported.
- **Tiles:** v4 average miss, v3 average miss, v4 runs warm or cold (mean signed error), and the "No change" guess; they cover the time in view (server `stats` for the whole window, local scoring once zoomed).
- **"How each model has scored" table (DP-8 rows, information only since 0.9.4; live since Issue #1492):** headed "Last 30 days, live (dates)" from `gate_summary`; one row per zone x season (Heating / Summer) x look-ahead (1/4/12/24 h, all scored) from the response's `gate`, computed by the endpoint over the last 30 days on every load (not from the backtest); columns v4 / v3 / "no change" average miss, bias, Points (n), "v4 beats v3", "v4 beats no change", "Bias within +-0.5 F" (+-0.28 C). A row with fewer than 72 points reads "Not enough data yet (n of 72 points)"; a v3 cell with no recorded v3 yet reads "collecting" ("v3 off" when v3 is not running). The server's `beats_v3` / `beats_flat` / `bias_ok` / `pass` are authoritative for the row. The verdict is descriptive only: "v4 beats both v3 and \"no change\" in N of M scored rows" (from `gate_summary`), never ready/not-ready; nothing on the page switches a model on or off. Seasons are by HVAC mode (heating day / cooling-or-fan day / shoulder).
- **States:** *loading* (overlay "Loading..." until the first response); *error* (alert box with the message and a Retry button, e.g. HTTP 503 when Climate Advisor is not loaded); *empty model* (no points and model confidence `none`/absent: "v4 has not been fitted yet; it fits nightly at 3:15. This page fills in after the first fit, or after the backtest results are published."); *no data in window* (overlay "Nothing recorded in this window: scroll back or choose a longer range"); *no gate rows* ("No scored results yet." and verdict "Nothing scored yet").
- **Invariant:** the page is a pure reader of the endpoint; no decision code reads it (a v4 primary reaches decisions only through the control feed, [Prediction Engines Spec](prediction-engines-spec.md#control-feed-r6b)).

## Panel Entry and Default Dashboard (Issue #1230 R6a)

- **Entry:** the sidebar panel (`__init__.py`, an iframe over the whole `frontend/` static path) loads `frontend/entry.html?v=<hash of entry + classic + new>`. `entry.html` renders nothing but a placeholder and calls `location.replace` to `new_dashboard.html` (default) or `index.html` (classic), carrying `location.search` and `location.hash` so deep links (`#analysis`, `#settings/...`) land. Both dashboards stay served and directly addressable; a bookmark of `index.html` opens classic.
- **Choice:** `localStorage['climate_advisor_dashboard']` (per browser): `classic` opens classic; unset, `new`, any other value, or storage that throws opens new. `?classic=1` opens classic for that visit only. The new dashboard writes `new` on load and its "Back to classic" link writes `classic` on click; classic's "Try the new dashboard" link sits in the page header above the tab bar (every tab), and loading the new dashboard re-remembers `new`.
- **Not done:** an install-wide `default_dashboard` setting. It would need a new options-flow write path and a `/status` field; per-browser memory covers the occupant case. Classic is not removed (removal is R7).
- **Tests:** `tests/test_panel_entry.py`, `tests/test_new_dashboard.py` (parity table, 0 pending), Playwright `tests/ui/entry.spec.js` (decision table, hash, storage throwing, remembered back-and-forth). `MOCK_PORT` runs the UI specs on another port when 7531 is taken.

## Status Tab: Shared Chart Module, Plan List and Plan Overlay (Issue #1230 R2, R3, R5, R6fe)

Occupant view: on Status the person sees the same chart, the same "Next 24 hours" list and the same plan marks whether they use the classic dashboard or the opt-in new one. What is planned for the next day (what Climate Advisor will do, what they are asked to do) is read from the operation timeline; the page decides nothing.

- **One chart implementation:** `frontend/chart_v2.js` (the R2 chart: split indoor/outdoor charts, per-sensor lines, v3/v4, key, strips, ranges, scroll, zoom, hover, table view, remembered choices) and `frontend/chart_v2.css`. `index.html` and `new_dashboard.html` both load `chart.umd.min.js`, hammer, the zoom plugin, then `chart_v2.js`, and carry the same chart markup (ids `temp-chart`, `out-chart`, `activity-chart`, `chart-key`, `chart-hover-panel`, `chart-stack`, ...; the hover readout `chart-hover-panel` comes right after `chart-stack`, below the chart; it grows to fit every row in a grid (never scrolls), holds the tallest height it has reached (released on a key toggle or when its width changes), and a click (tap on a phone) pins it until the next click or Esc). With the pointer off the chart and nothing pinned the readout rests on NOW, built by the same row builder as a hover and labelled "Now · <time>" with a small hover/pin hint, re-rendered on every redraw or data load; when NOW is outside the view it rests on the view's right edge, labelled "<weekday> <time> (end of view)" ("<Mon d>, <time> (end of view)" beyond six days) (Issue #1408). A page adapts the module through `CAChartV2.init({zone(), api(path), onData(chartData)})`; the module exports the `window.*` handlers the markup's `onclick` attributes call. Other API: `load(beforeTs)`, `setStatus(payload)`, `data()`, `redraw()`, `loadTimeline()`, `timeline()`, `mountPlan(el, {extra})`, `renderPlanList(el, {extra})`.
- **Styling:** `chart_v2.css` reads the classic token names (`--text`, `--text-secondary`, `--border`, `--primary`, `--card-bg`, `--bg`); `new_dashboard.html` maps its own tokens onto them. Chart colours (`--c-*`) follow the system theme and an explicit `data-theme`.
- **Plan list ("Next 24 hours"):** `GET /api/climate_advisor/timeline` (see [Operation Timeline Spec](operation-timeline-spec.md#authoritative-surfaces-r3b)). Steps from now to now+24 h in time order, `meta.status == "done"` and `briefing_send` left out, a step dropped ten minutes after its time. Each row: time, **You** / **CA** (a word and a shape), the step's own `text`. A "you" step shows "Reminder at" `ts - lead_minutes` (30 min, DP-4); a `confidence: "low"` step says **may help** (decision C13). The page adds no wording of its own (Status Card Ontology): empty (no steps), "after Climate Advisor has classified the day" (HTTP 503) and error states only. The new dashboard adds the nat-vent cycling range under the list (`#status-natvent-line`); classic keeps it in the Status card note.
- **Plan overlay:** one chip "Plan" in the key's Overlays group (`ov:plan`; on by default, remembered with the other choices under `ca_chart_v2:<zone>`). It draws a marker per future step on the indoor chart (circle = CA, diamond = you, hollow = low confidence), the plan path (timeline `path`, the `_ca` sensor or the first one, from NOW on; a `model: "v4"` path only when the chart payload's `v4.ready` is true, i.e. v4 has lines to draw; there is no accuracy gate since 0.9.4) and names the step within 20 minutes of the cursor in the hover readout. The list is the text twin of the markers.
- **Activity strips (Issue #1436):** solid = what happened (chart log rows, each holding until the next row, never past NOW), faded = what the plan expects: the timeline payload's `strips` (`timeline_surfaces.strip_spans`), drawn verbatim; `chart_data.predicted_activity` draws nothing. A faded Heating/Cooling bar is the run's expected minutes where the plan places them, never the run's whole window (owner decision); its hover covers the window. Each expected on-period is its own sliver, drawn at least 3 px wide (draw time only); hovering one says "about 1 min of 12 min cooling, 6:05 PM–8:35 PM". A line under the strips (`#chart-strip-legend`, added by the module on both dashboards) reads "Solid: what happened · Faded: what the plan expects"; the hover readout's "Activity" row names the plan step of each faded strip at the cursor ("Win plan plan · You 7:14 PM: Close the windows ..."). Rules: [Operation Timeline Spec](operation-timeline-spec.md#chart-activity-strips-issue-1436). A strip row comes and goes with its data (owner decision in the #1459 review): it is drawn when its switch is on and it has something in the loaded time. Its switch stays clickable and remembered even when the row is absent (Issue #1459), so a strip switched on before anything happens appears as soon as it does; a strip the home cannot have (see the switchable rule below) has no chip and no row. Tests: `tests/test_chart_strips_1436.py`, Playwright `tests/ui/strips-plan.spec.js`, `tests/ui/controls-always-switchable-1459.spec.js`.
- **Remembered chart choices:** every key chip (sensor lines, prediction lines, outdoor lines, overlays, strips), the past/ahead range, the v4 look-back, the table view and whether the key is open (`keyOpen`) are stored per browser and per zone in `localStorage['ca_chart_v2:<zone entry id or "default">']` (per zone because each zone has its own sensors; every read/write is in try/catch, so blocked storage only means defaults). Defaults: of the indoor readings only "CA reading" (`ind:_ca`) is on (each sensor's own line starts off unless the CA reading has no data); the "no plan" baseline starts off, and so do the four humidity / feels-like lines (Issue #1414); a stored `ov:humidity` / `ov:feelslike` choice (the old overlay ids) moves to `ind:humidity` / `ind:feelslike` on read and the prefs are re-saved once. Engine lines are remembered by role, not engine: `role:<primary|shadow|other>:<line>` (`_ca` for the v3 line, the sensor id for a v4 sensor line, `out:corrected`), and a v4 sensor line with no choice of its own follows its role's `_ca` choice, so switching engines in Settings keeps "main line off" off. Older stores (`v3`, `v4:<sensor>` keys plus `model: "<primary>+<shadow>"`) are moved to their role on read; without a `model` they are dropped.
- **Key fold:** the key (`<details id="chart-key">`) folds at every width into one summary line, "Showing: CA reading · v3 · … (n) ▸ Edit", built from the lines and overlays that are on; its `<summary>` carries `aria-expanded` and works from the keyboard. Folded on a first visit; open/closed is remembered (`keyOpen`). It sits above `#chart-stack`, so the chart keeps its size.
- **Predicted rows by engine role (Issue #1430):** under the header row `#chart-main-model` ("Engines: Main prediction: v4 · Comparison: v3 — change it in Settings … How each engine has scored."), the Predicted group is one block per engine in role order from `_engines()`: `#chart-role-main` ("Main · <primary>") first, then `#chart-role-comparison` ("Comparison · <shadow>", absent with no comparison engine), then any engine that is neither (`data-role="other"`, "Not selected · <engine>"; its lines start off but stay in the key). Each block is `.key-role` with `data-engine` / `data-role` and an `aria-label` naming the role and engine. v4's block: "Forecast with plan" (`v4:<id>`), "Forecast if Climate Advisor did nothing" (`base:<id>`), "Past predictions only" (the `v4:<id>` chip of each history-only sensor), the "Past v4 lines" row and the `#chart-v4-learning` line. v3's block: one "Prediction" row with the `v3` chip, labelled "Climate Advisor reading" (v3's prediction of the reading CA uses). The schedule estimate (`schedule`, "While learning") sits in the Main block. A prediction chip's accessible name is "<label>, <what the line is> (<main prediction | comparison | not selected in Settings>)". The hover readout's Predicted row, the folded "Showing:" summary and the table view's columns list the prediction lines in the same role order (`_byRole`). Only labels, grouping and order follow the roles: the plotted lines, colours, dashes and series ids (`v3`, `v4:<id>`, `base:<id>`, `schedule`) are the same whichever engine is main, so stored chip choices keep working.
- **"Past v4 lines: predicted" row:** `1 h / 4 h / 12 h / 24 h earlier` picks which archived v4 forecast (`v4.past[sensor][lead]`) is drawn left of NOW, i.e. what v4 predicted that long before each moment; the line from NOW on is unchanged. The row is judged on the VIEW, not the payload (0.9.6 Lane F). Issue #1459: every lead button is always choosable (locked only when `capabilities.v4_running` is false, with "v4 is switched off (Settings → Prediction engine)" in place of the help text); a lead with no past point in the view is labelled "(none in view)" in the faint no-data style, and choosing it writes "<N> h earlier: none in this view; choose …" on the row (`#chart-lead-empty`, next to the unchanged help text `#chart-lead-help`); with none in the view at all `#chart-lead-empty` says "No past v4 predictions in this view; v4 has them from <first> to <last>" from `v4.past_span` (the span of every past v4 prediction this zone has, archive and backtest, any look-ahead, independent of the fetched window; `build_chart_v4`), or "No past v4 predictions in this view" from an older backend in a scrolled view, and "v4 has no past predictions to show yet" only when v4 has none anywhere (`data-lead-empty` = `view` / `yet`). Root cause of the owner's report: `v4.past` holds only the fetched window, so scrolled history (or a zoom) before the oldest prediction read as "none yet". The row exists only while there is a v4 chip.
- **Which chips exist, and the switchable rule (0.9.6 Lane F; Issue #1459):** a v4 plan / no-plan chip only for an indoor sensor v4 has data for (any `v4.past` lead, `v4.future` or `v4.baseline`), plus "Climate Advisor reading" (`_ca`) when `v4.sensors` has it; the other sensors are named on one muted line `#chart-v4-learning` ("v4 is still learning: thermostat, bedroom sensor": the role when it names the sensor, else its name; "v4 is switched off (Settings, Prediction engine)" when it is not running). A sensor with past predictions only (the backtest) and no forecast gets one chip, on the v4 block's "Past predictions only" row, and no no-plan chip (the no-plan line is a forecast; Issue #1430). **The rule (Issue #1459, replacing 0.9.6's "every chart control draws something or says why not"): every chart control can be switched on unless the home can't have it; an empty view says why.** Time, weather and the current view never lock or hide a control. Two separate reasons per chip: `_chipEmpty(id)` (why its line has nothing in the current view: no data in the loaded payload, data only at other times "scroll or change the range", for a v4 line only other look-aheads in view, the humidity / feels-like `emptyReason`, the band / target / plan texts, or a "not yet" `note`: v4 still learning, no no-plan line yet, schedule estimate "Not needed: …") shows in its `title` and `aria-label` with the faint `.no-data` style, but the chip stays enabled, shows its real on/off state, and a click toggles and saves it; it draws once there is something in view. `_chipLock(id)` (= the series' `disabledReason`) is the only thing that disables a chip, and it comes only from `chart_data.capabilities` (`_capLock`; the reason says where to set it up): `v4_running` false locks every v4 line, `out:corrected` and the look-ahead buttons ("v4 is switched off (Settings → Prediction engine)"); `humidity_indoor` false locks `ind:humidity` / `ind:feelslike` and `humidity_outdoor` false locks `out:humidity` ("No indoor/outdoor humidity source found (Settings → Sensors)"); `out:feelslike` never locks (wind chill needs no humidity). Lines and strips a home can never have get NO chip and no row: `out:sensor` when `outdoor_sensor` is false, the Fan and Nat-vent strips when `fan` is false, Windows open / Windows plan when `door_sensors` is false, Away / Vacation when `occupancy` is false; one muted line per group (`#chart-out-not-set-up`, `#chart-strips-not-set-up`) names them with the reason, e.g. "Fan, Nat-vent: No fan configured (Settings → Equipment)". Without `capabilities` (an older backend) nothing locks. "Disabled" everywhere downstream means locked: the readout, the table, the datasets' `hidden`, the folded "Showing:" summary and `__chartState().series[].disabled` skip a locked line; an on line with nothing in view simply draws nothing. The key is re-rendered after the charts are built and on every zoom, so the reasons follow the view. A lone point between gaps (e.g. one daily mean in view) is drawn as a dot. `tests/browser/check_chart_chips.py` fails if an enabled chip with data in view changes no chart pixels when switched on, an empty one gives no reason or does not switch, or a locked one gives no reason (both pages, the live 0.9.5 Zone 1 shape and the full mock, 24 h / 6 h / scrolled history).
- **Humidity and feels-like (Issue #1414):** four ordinary series built in `_buildSeries`, so the key, the hover readout, the folded summary and the table list them with no special case. `ind:humidity` and `ind:feelslike` sit in the Indoor group and on the indoor chart; their past comes from `state_log` `humidity` / `feelslike`. `out:humidity` and `out:feelslike` sit in the Outdoor group and on the outdoor chart; their past comes from `state_log` `outdoor_humidity` / `outdoor_feelslike`, and their future from `outdoor_humidity_forecast` / `outdoor_feelslike_forecast` (`{ts, value}`). Payload contract: [Chart Log Spec](chart-log-spec.md). A row without the value is a gap, drawn by the ordinary `_mkSeries` gap rule. All four start off. Colours reuse `--c-hum` / `--c-feels` (light and dark); the outdoor lines are dashed (humidity long dash, feels-like dash-dot) so they never read as the indoor ones.
  - **Axes:** a series may carry `axis` (`'y'` default, `'yRight'`) and `unit` (`'%'` for humidity). `_dsFor` uses the axis. The readout and the table use the unit (else the temperature unit), and the table caption adds "humidity in %". Both charts have a `yRight` % axis exactly `RIGHT_PX` wide (the outdoor chart no longer pads its right side), so the two plot areas stay aligned. Each axis's labels are blank unless a `yRight` dataset on that chart is drawn; the tick callback reads the datasets' live `hidden`, so a toggle shows or blanks them.
  - **Empty-view reasons (Issue #1459: the chip stays switchable):** with nothing to draw, a chip says "No humidity reading recorded in this view". For feels-like the reason is "Indoor feels-like only shows as a heat index (80°F and up)" or, outdoors, "Feels-like only shows as a heat index (80°F and up) or wind chill (50°F and below with wind)", with the thresholds in the display unit (27°C / 10°C).
  - **Old overlays:** the `ov:humidity` / `ov:feelslike` datasets and chips, `HIDDEN_OVERLAYS` and the readout's name list are gone; the readout's Overlays row reads only the `ov:target` datasets.
  - **Tests:** Playwright `tests/ui/humidity-feelslike-1414.spec.js` (both pages: series and axes, gaps, right-axis visibility and plot alignment, readout units, table columns, the prefs migration, a °C payload).
- **New Status tab (`nd_status.js`):** For you / CA next (`next_action`, `next_automation_action`, `next_automation_time` of `/status`; the timeline is authoritative for them since R3b), reading cards, the chart, the plan list, "Tonight in the bedroom" and the briefing (collapsed; Regenerate = `send_briefing {notify:false}`, Send = `send_briefing`). Refresh: status each minute, plan each minute, chart/night plan/briefing every fifth, everything on zone change, Refresh and after a re-plan or briefing action.
- **Night card:** `GET /api/climate_advisor/night_strategy?zone=` (`NightStrategy.to_dict()`, temperatures in the display unit, `unit` added; 503 until the first plan). `NDNightCard.fromStrategy(payload)` turns it into the card's shape (paths resampled onto one grid). `show_numbers: false` (the bedroom model's "closed" option is below medium confidence; there is no accuracy gate) shows the recommended option and its steps and no figures, chart or table. 503: "No night plan yet". 404 (an install without the endpoint): the approved mock, labelled "Sample data, not your house".
- **Tests:** `tests/test_status_plan_overlay.py`, `tests/test_new_dashboard.py` (parity table, 0 pending), `tests/test_status_chart_v2_page.py`; Playwright `tests/ui/plan.spec.js`, `tests/ui/new-status.spec.js`; real-browser `tests/browser/check_status_final.py` (both pages, themes, widths, every control), `tests/browser/check_chart_chips.py` (every enabled chip draws) and `tests/browser/check_new_dashboard.py`; Playwright `tests/ui/chart-controls.spec.js` (the live 0.9.5 shape, the owner's "none yet" case), `tests/ui/key-roles-1430.spec.js` (the Predicted blocks follow the engine roles, both pages), `tests/ui/controls-always-switchable-1459.spec.js` (Issue #1459: empty-in-view controls stay switchable and remembered, locks only from `capabilities`, both pages).

## Comfort Edge Selector (Issue #1066; settling guard Issue #1073)

**Authoritative spec (Issue #1073):** [comfort-edge-selection-spec.md](comfort-edge-selection-spec.md) owns the context matrix, the variable table, invariants R1-R9 and the new `thermostat_mode_set` / `comfort_switch_held` events. It adds the settling guard (a shallow breach shortly after the opposite side ran is held), splits the tolerance (`comfort_deadband_*_f`) from the fixed 2 F arming margin, and unifies the mode-change clock and the "ran" source.

A single-setpoint thermostat guards one comfort edge at a time. Every evaluation, the selector picks the edge nearer to indoor: below the floor heats and above the ceiling cools immediately; within an arming margin `m` of an edge it arms that edge; strictly between the margins it keeps the thermostat's actual current mode. Full rule, margin clamp, before/after table and mixed-day walk-through: [08-COMPUTATION-REFERENCE.md §6f](08-COMPUTATION-REFERENCE.md#6f-comfort-family-nearest-edge-arming-issue-1066--supersedes-827843858867-settling-guard-issue-1073).

| Piece | Owner | Notes |
|---|---|---|
| Decision | `comfort_family_decision.decide_comfort_family()` | Stateless, no HA imports; heat-only/cool-only thermostats pick their one mode |
| Anti-chatter | `comfort_family_fsm.transition()` | `comfort_mode_switch_min_interval_s` dwell between non-breach switches; breaches ignore it |
| Shell | `AutomationEngine._resolve_comfort_family_via_fsm()` | Called by `_apply_comfort_band()` and `_set_temperature_for_mode()`; nat-vent/WHF, pause, override, occupancy and TOU keep their existing priority |
| Trigger | `comfort_family_temperature_check()` from `coordinator._async_thermostat_changed()` | Every temperature change; re-enters `apply_classification()` only when the selector disagrees with the armed family |
| Decision memory | The thermostat's real HVAC mode | Restart-safe; only the dwell timestamp is held in memory |
| Mode-change history | `AutomationEngine.comfort_mode_changes` (cap 200) | Appended by `_record_commanded_mode()`, persisted in the state file; feeds `control_quality` |
| Report | `control_quality.py` -> `diagnostic_snapshot` | Download Logs / Full Logs / HA diagnostics; reading guide in [09-DEBUGGING-GUIDE.md](09-DEBUGGING-GUIDE.md#control-quality-report-issue-1066) |

The ODE floor guard (`ode_floor_guard.py`) was removed in this change. The ODE ceiling guard (`ode_ceiling_guard.py`) keeps its decision but skips its `cool` write while the selector is guarding the floor.

## Supported Thermostat Configuration

CA issues separate `heat` and `cool` commands, one single setpoint at a time (dual `heat_cool` mode was dropped
in Issue #301 due to thermostat compatibility bugs; dual-capable thermostats are treated the same way).
A thermostat that supports only one of heating or cooling is armed only in that mode (the comfort edge
selector picks its one mode); a thermostat supporting neither receives no comfort-band commands.
Dual heat-and-cool systems get nearest-edge switching between the two (see Comfort Edge Selector above).

## Observe-Only Mode (Disable Automation)

Climate Advisor exposes a `switch.climate_advisor_automation` entity that controls whether real actions are executed.

**When ON (default)**: Normal operation — all thermostat changes and notifications are executed.

**When OFF (observe-only)**: The full computation pipeline continues (classification, decision-making, state tracking, logging) but all HA service calls are skipped:

- `climate.set_hvac_mode` — skipped
- `climate.set_temperature` — skipped
- `notify.*` — skipped (including daily briefing delivery)

Skipped actions are logged at INFO level with a `[DRY RUN]` prefix:
```
INFO  [DRY RUN] Would set HVAC mode to cool — daily classification — hot day, trend warming 8°F
INFO  [DRY RUN] Would set temperature to 72°F — bedtime — heat setback (comfort 70 - 4 + modifier 2)
INFO  [DRY RUN] Would send notification: Climate Advisor — 🏠 Welcome home! ...
```

**Restart behavior (Issue #1349):** the person who switched Automation off expects it to stay off through any Home Assistant restart, including an overnight outage that comes back after midnight. `automation_enabled` and the engine `dry_run` are restored before the state file's date check; only a missing, corrupt or version-mismatched state file brings automation back on. See [state-persistence.md](state-persistence.md#restore-across-the-date-boundary-issues-1349-1354-1360).

### Implementation

Guards are placed at the 3 thermostat primitives (`_set_hvac_mode`, `_set_temperature`, `_notify`) and the fan primitives (`_activate_fan`, `_deactivate_fan`) in `AutomationEngine`, the fan-entity command (`_async_command_fan_entity`, Issue #589) and the briefing notification in the coordinator. Higher-level logic (classification application, door/window pause tracking, grace periods, occupancy handling) continues unaffected.

**Choke-point backstops (Issue #1108).** Some paths reach a device without passing a primitive, so the lowest command choke points carry their own `self.dry_run is True` gate (`is True`, because MagicMock test stubs are truthy):

| Choke point | Paths it covers | Observe-only result |
|---|---|---|
| `_call_climate` | every `climate.*` write, incl. the setpoint-nudge write and `_ensure_hvac_mode_for_write` | `[DRY RUN] Would call climate.<svc>` and `ClimateCallResult(DRY_RUN)`. `DRY_RUN` is distinct from `REJECTED`: callers stop **without** any note (no `hvac_mode_not_applied`, no verify/retry). `_ensure_hvac_mode_for_write` also returns False before its commanded-mode stamps. |
| `_call_service_with_context` (via `_call_fan_service_with_context` for WHF; Issue #1109 routes `_call_climate` and the thermostat `set_fan_mode` calls through it too) | WHF control-entity commands, incl. the physical-drift off-command | `[DRY RUN] Would call <domain>.<svc>`; returns before `_record_command_context` |
| `_release_orphaned_hvac_fan` | the one-shot post-restore orphan release (Issues #1103/#1104) | **skip-and-clear**: nothing sent, the pending release is dropped (not deferred), `fan_orphan_released` is emitted with `dry_run: True`, `command_sent: None` |
| setpoint `_retry_callback` (15-min retry after a rejected write) | the nudge-then-target retry | returns at the top, before the `setpoint_nudge` event, so no nudge is logged that never happened |

Pending timers are not cancelled when automation is switched off; the gates make them harmless. `tests/test_teardown_static.py` (T7c) fails if a function in `automation.py` or `coordinator.py` that calls `hass.services.async_call` references neither `dry_run` nor `_automation_enabled` (allowlist: the read-only forecast fetches).

## Engine Callback Isolation (Issue #604, Block 5 subtask N2)

`AutomationEngine` itself has no hidden shared/global mutable state — every grace/timer
cancel-token, flag, and its `_decision_lock` (a per-instance `asyncio.Lock`) live on the
instance. It is safe to construct more than one. What is **not** safe by default is the
coordinator's callback wiring: `ClimateAdvisorCoordinator.__init__` wires 9 callback
attributes onto `self.automation_engine` (`_revisit_callback`, `_sensor_check_callback`,
`_sensor_debounce_pending_callback`, `_emit_event_callback`, `_request_refresh_callback`,
`_post_grace_fan_check_callback`, `_get_fan_physical_state_callback`,
`_is_recent_fan_command_callback`, `_reclassify_callback`) — all closures/bound methods over
the single coordinator instance, not parameterized by which engine invoked them. At least 4
of these reach into real production state or trigger real side effects regardless of which
engine fired them:

- `coordinator._on_post_grace_fan_check` (→ `_async_post_grace_fan_reconcile`) hardcodes
  `ae = self.automation_engine` and can issue a real `_activate_fan`/`_deactivate_fan` command.
- `coordinator._emit_event` reads `self.automation_engine._natural_vent_active` — the
  hardcoded production engine's attribute — to decide whether to call a real,
  non-dry-run-gated side effect (`_maybe_reschedule_pre_cool_on_nat_vent_exit`).
- `coordinator.async_request_refresh` and the `_request_refresh_callback` lambda that wraps
  it both trigger a full real `_async_update_data_impl()` cycle.

**The contract**: `AutomationEngine.__init__` takes an optional keyword-only
`callbacks: AutomationEngineCallbacks | None = None` (a 9-field dataclass, one field per
callback above) and `role: str = "production"` (a label for logging/future observability
only — never branched on inside the engine or inside any of the 9 coordinator methods).
Isolation between the production engine and any second ("shadow") engine was
**structural**, not a runtime check someone could forget: a shadow engine was never
given a bundle containing the 4 hazardous callables above, so there was nothing to check.

The coordinator's own wiring is built by `_build_production_automation_callbacks()` — a
behavior-preserving extraction of what used to be 9 individual post-construction
assignments, now one bundle passed at construction time
(`AutomationEngine(..., callbacks=self._build_production_automation_callbacks(),
role="production")`).

**Historical (Issue #613–#757):** During the FSM migration (epic #594), a shadow
`AutomationEngine` instance ran parallel to production as an A/B test, fed the same inputs
and compared for decision divergence. Once every FSM proved zero divergence in production,
the shadow engine and its mirror-replay infrastructure were permanently deleted in Phase 6
graduation (Issue #757). Today only the single production engine remains. See
[Nat-Vent Lifecycle Spec](nat-vent-lifecycle-spec.md)'s "Live Shadow Engine (historical)"
section for the former architecture.

**Rule for any future second `AutomationEngine` instance**: build a dedicated
`AutomationEngineCallbacks` bundle for it, following `_build_shadow_automation_callbacks()`'s
pattern. It must **never** reuse `coordinator._on_post_grace_fan_check`,
`coordinator._emit_event`, `coordinator.async_request_refresh`, or the
`_request_refresh_callback` lambda that wraps it — reusing any of those lets the second
engine's events/decisions mutate real production state and event history regardless of the
second engine's own `dry_run`/`role`. `tests/test_engine_callback_isolation.py::TestHazardCharacterization`
reproduces this exact hazard today (a second engine built with the production bundle leaks
an event into the production event log) as a concrete negative example for review.

## Engine-State Ownership (Issue #1443)

Occupant view: the decisions Climate Advisor makes for the house (may a window-open fan session start, the nat-vent
soft start, the time-of-use Away bank, the remote-sensor swing, the "HVAC cooled within 2 h" fan restriction) act on
the forecast, learned thermal model, today's outdoor peak and TOU window the coordinator holds when it asks, never on
a copy one path forgot to refresh. Before #1443 the coordinator hand-pushed copies into engine privates; #1440 was one
that drifted on the real-HA tier (the adapter seeded only the engine's forecast copy, the coordinator's refresh pushed
its empty list over it).

**The rule (owner's choice: values passed in).** The coordinator is the single owner, and the engine holds no copy.
Single facts are computed by the coordinator and passed in at the decision call:

| Fact | Owner (coordinator) | In `EngineFacts` | Decision input |
|---|---|---|---|
| hourly forecast | `_hourly_forecast_temps` (`_refresh_hourly_forecast`, kept-forecast restore) | `forecast_peak_2h` (never the list) | `decide_nat_vent_start_veto(forecast_peak_2h=…)` (#1138) |
| today's outdoor peak / samples | `_outdoor_temp_history` (each recorded reading; same-day restore; midnight reset) | `outdoor_today_peak`, `outdoor_today_sample_count` | soft start (#540) |
| Away/Vacation TOU window | `_tou_av_window: TouAvWindow` (`_sync_tou_away_vacation_state`) | `tou_av` (expansion, schedule id, bank target) | Away/Vacation band expansion (#899) |

- **Passed in.** `coordinator.engine_facts(now)` builds a frozen `EngineFacts` snapshot. Every coordinator (and
  `api.py`) call into an engine method that reaches one of these facts goes through `decide(coordinator, method, ...)`
  / `decide_sync(...)` (`engine_inputs`), which builds the snapshot when the call starts and runs the call inside
  `engine.decision_facts(snapshot)`. That is a context variable scoped to that call and task, and nothing is kept on
  the engine. The "Next Automation" prediction passes facts for the time it asks about (`facts_at=`).
- **Never reused.** A snapshot older than `FACTS_MAX_AGE` (60 s), or passed for another engine, is ignored. A timer that
  fires later inside a captured context gets a fresh one.
- **Accessors that stay (no argument practical):**
  - `inputs.build_facts(now)`, for an engine-internal timer (grace expiry re-check, override confirmation, fan
    min-runtime cycle, WHF safety re-check) or a read-only reader in `automation.py` (`active_thresholds_for`, the
    status/incident band). These have no coordinator call frame. In production it is the coordinator's own
    `engine_facts`, built at that moment.
  - `inputs.thermal_model()` then `_control_model_provider`: the whole learned model (with #1432, the per-room v4
    control model) read by many decision helpers.
  - A bare engine (headless engine-only harness, unit tests) gets `StaticEngineInputs`, whose fields its builder owns.
- **Engine-owned state** stays on the engine and is changed only by the engine. The coordinator reports observations
  or requests corrections through public methods: `note_equipment_ran` and `note_cycle_activity` for the persisted
  `_last_hvac_*_active` / `_last_fan_active` / `_last_natvent_active` stamps (owner: "keep on engine"),
  `note_observed_fan_origin`, `clear_stale_fan_ownership`, `clear_stale_hvac_fan_flag`, `mark_ha_stopping`,
  `latch_shutdown`. The condition that decides a correction stays in the coordinator.
- **Static guards** (`tests/test_engine_state_ownership_static.py`, AST, alias-aware):
  - Any assignment, augmented assignment, `del`, item/nested assignment, mutating method call or `setattr`/`delattr`
    of an engine private outside `automation.py` fails. Production allowlist: installs only (`_control_model_provider`,
    `_command_observer`, `_occupancy_bank_write`). The harness has its own allowlist. A stale entry fails too.
  - Any direct call from outside `automation.py` into an engine method that reaches a fact (one not routed through
    `decide`) fails.
- **Harnesses seed the owner** (#1440): Tier A coordinator mode and the real-HA adapter write the scenario's
  `thermal_model` / `hourly_forecast` to the coordinator; an engine-only run passes `StaticEngineInputs`.

## Teardown Contract (Issue #1102)

Occupant view: after a settings save (which reloads the config entry) there must be exactly
one Climate Advisor instance per zone that can command the fan, thermostat blower or HVAC,
send a notification, or react to occupancy. Before #1102 a reload inside the 300 s startup
window left the old instance alive through its uncancelled startup timer; it kept commanding
the whole-house fan, turned it back on after the occupant switched it off, and blamed them.

**When teardown runs.** HA unloads a config entry on reload and on removal, never on a
restart. HA's `ConfigEntry.async_unload` calls `async_unload_entry()` (which calls
`coordinator.async_shutdown()`), then runs the entry's on_unload callbacks LIFO. Because
`DataUpdateCoordinator` takes `config_entry` from the setup ContextVar and registers
`async_shutdown` there, **`async_shutdown()` runs twice per unload**. A restart fires
`EVENT_HOMEASSISTANT_STOP` instead, so the STOP listener (`_persist_shutdown_diagnostics`)
remains the only restart-time save; it also stops the instance acting (see "Home Assistant
stopping" below).

**Release rule.** Every resource is released by the mechanism that owns it:

| Resource kind | Where the handle lives | Released by |
|---|---|---|
| Setup-lifetime coordinator timers/listeners (scheduled events, state listeners, bus listeners, startup-coalesce timer, weather retry, api.py cancel-override follow-up) | `coordinator._unsub_listeners` | `async_shutdown()` drains the list |
| Runtime re-armed coordinator timers (occupancy away, TOU away/vacation, door debounce, fan-remote burst, pre-cool trigger) | own handle + cancel helper (`_cancel_*`) | `async_shutdown()` calls each helper |
| Occupancy / door-window listeners | `_unsub_occupancy_listeners`, `_unsub_dw_listeners` | their `_unsubscribe_*` helpers, from `async_shutdown()` |
| Engine timers (grace, fan cycles, backstop, revisit, override confirm, setpoint retry, mode-align recheck, fan verify) | own handle | `AutomationEngine.cleanup()` (#729) |
| STARTED weather-validation hook (`__init__.py`) | `async_at_started` cancel | `entry.async_on_unload` |
| Coordinator state-save / observation-commit tasks (Issue #1113 group A: `set_automation_enabled` save, thermostat-off save in `_async_thermostat_changed`, the two `_commit_observation` spawns) | `config_entry._tasks` via `coordinator._spawn(coro)` | HA awaits them on unload (up to 10 s, then one WARNING), before a reload's new setup starts |

A handle from `async_call_later` / `async_track_*` / `async_at_start(ed)` /
`bus.async_listen(_once)` is never discarded: `tests/test_teardown_static.py` fails on a bare
call. The api.py follow-up is the one append-only exception (one entry per cancel-override
click, never removed, a no-op after firing; growth is bounded by user clicks).

**Entry-tied tasks (Issue #1113).** `coordinator._spawn(coro, *, background=False)` starts a
task with `config_entry.async_create_task` (or `async_create_background_task`, which HA
cancels on unload), falling back to `hass.async_create_task` when the coordinator has no
entry (tests, simulation harness). Occupant view: during a settings save, the old instance's
save or observation commit finishes before the new instance reads the files, so no learned
observation is lost. A restart never unloads entries, so behaviour at HA stop is unchanged.
Two kinds of task must **never** be entry-tied: the `EVENT_HOMEASSISTANT_STOP` listener's
`_persist_shutdown_diagnostics` task, and anything that reloads or unloads its own entry
(it would wait on itself; use `async_schedule_reload`). `tests/test_teardown_static.py`
(T7d) ratchets the remaining bare `hass.async_create_task` calls (Issue #1113 groups B-D:
engine/timer dispatch, refresh requests, notifications) by count per (file, function): a new
bare call fails, and a converted site must lower its allowlist count.
`tests_ha/test_unload_cleanup.py::test_unload_waits_for_a_coordinator_save_task` checks in
real HA that unload stays in `UNLOAD_IN_PROGRESS` until a `_spawn`ed save finishes.

**Real-HA leak check.** `tests_ha/test_unload_cleanup.py` sets up one zone in a real Home
Assistant core (pytest-homeassistant-custom-component), unloads it and ends without advancing
time; HA's autouse `verify_cleanup` fixture then fails the test on any timer, task or thread
still alive. It names no resource, so a new leak anywhere in setup fails it. It runs in its own
CI job (`ha-lifecycle` in `.github/workflows/validate.yml`) and environment
(dependency group `test-ha`), because `tests/conftest.py` stubs `homeassistant` for the whole
process: `python scripts/test.py ha`.

**Shutdown order (`async_shutdown`).**
1. If already shut down, return (the second call from on_unload must not re-save: by then
   `cleanup()` has wiped the engine's grace, and saving again would lose a fan-off grace).
2. Set `coordinator._shut_down`, then `automation_engine._shut_down` (nothing can command
   during the awaited saves).
3. `await super().async_shutdown()` — HA's base: no further refreshes.
4. Saves: shutdown diagnostics, runtime flush, state.
5. Release (table above).
6. `automation_engine.cleanup()` (also sets the engine latch, so `cleanup()` is terminal for
   any caller).

All attribute reads use `getattr` defaults: HA also shuts down a coordinator whose setup
failed part-way.

**Shut-down latch.** A dead instance is reachable only through a leak, so the latch is a
safety net plus a tripwire, not the release mechanism. Each refusal logs one WARNING naming
the instance (`entry_id#objectid` / engine object id) and returns the call site's existing
no-op result, so nothing retries, re-arms or notifies. Unlike `dry_run`, it never reports
success.

| Site | Refusal |
|---|---|
| `coordinator._async_update_data` | returns current `data` (tripwire: HA's own post-shutdown gate is silent) |
| `_zone_scoped` / `_zone_scoped_sync` wrappers (every wrapped timer/listener) | return `None` |
| `coordinator._async_send_briefing` (registered unwrapped; push + email) | return |
| `coordinator._async_command_fan_entity` | `False` |
| `AutomationEngine._call_climate` | `ClimateCallResult(REJECTED)` |
| `_set_hvac_mode` (incl. off-assert blower call), `_set_temperature_impl` | `HvacCommandResult.FAILED`, before `_write_seq` / `_note_*` / verify / retry |
| `_activate_fan`, `_deactivate_fan` (incl. blower calls) | `FanCommandResult.DISABLED`, before the verify timers |
| `_call_service_with_context` (every CA fan/climate command; Issue #1109) | return, before `_record_command_context` |
| `_release_orphaned_hvac_fan`, `_notify` | return |
| `_start_fan_thermo_backstop` (stops an in-flight tick re-arming), `_start_grace_period_action` | no timer; grace returns `False` ("grace disabled") |

**Home Assistant stopping (Issue #1183).** A restart never unloads the entry, so `_shut_down`
never engages then. While HA unloads other integrations their entities go unavailable, and each
change used to start a full update cycle: a false "entities aren't responding" email and a
thermostat command failing with "Connection lost". The STOP listener
(`coordinator._async_homeassistant_stop`) therefore sets a second one-way flag, `_ha_stopping`, on
the coordinator and its engine before it schedules the diagnostics write. Every site in the table
honours it through the same check (`automation._latched` / `_refuse_if_shut_down`), logging at
INFO ("Home Assistant is stopping: ... skipped") instead of the leak WARNING. It is kept apart from
`_shut_down` because `async_shutdown()`'s run-once gate reads `_shut_down` only, so an unload after
a stop still saves and releases. HA's own `hass.is_stopping` is not used: it covers only the
`stopping`/`final_write` states, and `not_running` is also the boot state. The startup mirror
(alerts before entities load) is the startup-coalesce gate in `_run_entity_health_check()`.
Real-HA test: `tests_ha/test_shutdown_1183.py`. A restart never cancels timers armed before it,
so every coordinator timer/listener registers through `_zone_scoped`/`_zone_scoped_sync` (the
startup-coalesce, occupancy-away, weather-retry and door-debounce timers were not; an unwrapped door
debounce could record and save a pause whose HVAC-off command was refused).
`tests/test_teardown_static.py` (T7e) fails on a new unwrapped registration; its allowlist holds
the three that must stay unwrapped (briefing, restart-service listener, STOP listener).

`tests/test_teardown_static.py` also fails if a `hass.services.async_call` in
`automation.py` or `coordinator.py` sits in a function without the latch (allowlist: the two
read-only `weather.get_forecasts` fetches).

**Reload from inside setup.** Code running inside `async_setup_entry()` schedules a reload
with `hass.config_entries.async_schedule_reload(entry_id)`; awaiting `async_reload` there
waits on the entry's own setup lock. The weather Repairs fix (`repairs.py`
`WeatherEntityRepairFlow`) uses the same API (Issue #1116). The reload_needed Repairs flow
awaits `async_reload` directly: it holds no setup lock and a reload error surfaces in the dialog.

**Tests.** `tests/test_reload_teardown.py` (T1: unload at +172 s, nothing runs after),
`tests/test_reload_lifecycle.py` (no live timer/listener after unload; fan-off grace survives
a reload and state is saved once; every refusal; STARTED hook; scheduled reload). The harness
unloads the way HA does via `build_coordinator.async_unload_entry_like_ha()`.

## Automation Engine — Occupancy Methods

| Method | Trigger | Behaviour |
|--------|---------|-----------|
| `handle_occupancy_vacation(active: bool)` | Vacation toggle changes | `active=True`: applies the vacation band (`setpoint_combinations.vacation_edges()`: the optional `vacation_heat`/`vacation_cool`, else setback ± `VACATION_SETBACK_EXTRA`; Issue #1347). `active=False`: restores comfort setpoint. Logs mode change; respects dry-run guard. |
| `handle_occupancy_guest(active: bool)` | Guest toggle changes | `active=True`: sets comfort setpoint immediately, disables all setback paths. `active=False`: re-evaluates current occupancy state and applies appropriate setpoint. |
| `handle_occupancy_home(home: bool)` | Home/Away toggle changes | Delegates to existing away/return logic with the configured setback delay. |

Priority enforcement lives in `_async_occupancy_changed` (coordinator): it reads all three toggle states, resolves the winner using Guest > Vacation > Home/Away > default, and dispatches to the appropriate handler above.

The toggle state is persisted via `StatePersistence` and survives HA restarts. It is also exposed in the dashboard API (`/api/climate_advisor/status`) and debug state.

### repairs.py — HA Repair Issues

Integrates with Home Assistant's Repairs framework to surface actionable fix prompts
when Climate Advisor detects a configuration or data problem (e.g., a missing entity,
expired token, or learning DB schema mismatch). When a repair issue is raised, the
user sees a "Fix" button in the HA Settings → Repairs UI. Resolving the repair
dismisses it from the queue.

### ai_skills_investigator.py — AI Investigator Skill

The skill definition, system prompt and thin orchestrator. The structured context it hands the
Claude investigator - sensor values, HVAC session history, thermal model parameters, fan status,
occupancy mode, diagnostic guidance - is assembled by the context providers in
`ai_skills_context.py` (that is where the deadband logic and the swing lines live, Issue #1233 doc
fix). That context knows the thermostat's deadband behavior (the learned one-sided swing: how far past the setpoint the
thermostat lets the room drift before starting the equipment, Issue #1234) and explains it to
the AI so it does not misinterpret normal deadband oscillation as a CA fault; `_swing_context_lines()` in

Issue #1258: the same learned swing gates comfort incidents, `comfort_violations_minutes` and the #1050 stall alert through `coordinator._is_tolerated_comfort_deviation()` (pure rule in `swing.within_thermostat_hysteresis`); see [Incident Classes](incident-classes.md).
`ai_skills_context.py` adds the one-line meaning of the swing, how many cycles were accepted, the per-reason
reject counts and the coast (Issue #1233). See
[Learning Engine Design](05-LEARNING-ENGINE-DESIGN.md) for swing detection details.
