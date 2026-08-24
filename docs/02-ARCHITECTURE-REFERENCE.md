<!-- Nav: ← [Strategy and Design](01-STRATEGY-AND-DESIGN.md) → [Learning Engine Design](05-LEARNING-ENGINE-DESIGN.md) -->

# Climate Advisor — Architecture Reference

## Anchors
| Question | Short answer | → Full answer |
|---|---|---|
| What are the source files and what does each own? | 46 files total (2026-08-21). The original 16 own one responsibility each: `coordinator.py` orchestrates, `classifier.py` classifies, `automation.py` executes HVAC calls, `learning.py` persists and analyses behavior, etc. Block 5's FSM migration (epic #594) added 30 more — mostly small pure `decide_*()` decision-core modules plus the three lifecycle FSMs and the dispatcher pair. | [§File Structure](02-ARCHITECTURE-REFERENCE.md#file-structure) |
| What is the FSM Decision Layer — the three lifecycle FSMs, and is anything actually driving HVAC through them? | `nat_vent_fsm.py`, `door_window_fsm.py`, `override_grace_fsm.py` each model one lifecycle's state as a named enum. As of Issue #729, each engine's per-subsystem `_*_fsm_authoritative` flags were fixed at construction (all `True` on the FSM engine identity, all `False` on the legacy one) — a single switch, `switch.climate_advisor_shadow_engine_primary`, chooses which whole engine identity is primary, replacing the independent per-subsystem switches from Issue #594/#664. Phase 6 graduation (Issue #757) has since removed the override/grace (Step 3), door/window (Step 4), nat-vent (Step 5), and occupancy (Step 6) flags entirely — all four dispatchers are now unconditionally FSM-authoritative; only `classification_fsm.py`'s `_classification_fsm_authoritative` remains gated (Step 7, not yet executed). `lifecycle_dispatcher.py` is wired into production as of Issue #717, but as a same-instance emit/consume audit trail, not a cross-instance mirror. | [§FSM Decision Layer](02-ARCHITECTURE-REFERENCE.md#fsm-decision-layer) |
| How does data flow from the weather entity to an HVAC service call? | Weather entity → coordinator (every 30 min) → `classify_day()` → `DayClassification` → `apply_classification()` in the automation engine → `climate.set_temperature` / `climate.set_hvac_mode`. | [§Data Flow](02-ARCHITECTURE-REFERENCE.md#data-flow) |
| What are the five coordinator-scheduled daily events and when do they fire? | Briefing (default 6:00 AM), morning wakeup (6:30 AM), bedtime setback (10:30 PM), end-of-day save (11:59 PM), and the 30-minute forecast refresh loop. | [§Coordinator Scheduled Events](02-ARCHITECTURE-REFERENCE.md#coordinator-scheduled-events) |
| What is the debounce / grace period system and how do the three timers interact? | Debounce (default 10 min as of Issue #504) delays *any* reaction to a sensor state change until it holds steady for the configured time — pause/resume HVAC, and, since #504, nat-vent/WHF/HVAC-fan engage/exit. Manual grace (default 30 min) blocks new pauses after a user override. Automation grace (default 5 min) blocks re-pause after system resumes. Manual override always wins. | [§Debounce and Grace Period System](02-ARCHITECTURE-REFERENCE.md#debounce-and-grace-period-system) |
| What sensors does Climate Advisor expose and what do their attributes carry? | Eight sensor entities: day type, trend, next action, daily briefing, comfort score, status, occupancy mode, and AI status — each with diagnostic attributes. | [§Sensors Exposed](02-ARCHITECTURE-REFERENCE.md#sensors-exposed) |
| How does the thermal state machine (v3) work and which methods own each phase? | Ten coordinator methods drive six concurrent observation types in `_pending_observations` (Issue #121); HVAC observations survive HA restarts via `LearningState.pending_thermal_event`. | [§Coordinator Thermal State Machine Methods](02-ARCHITECTURE-REFERENCE.md#coordinator-thermal-state-machine-methods) |
| How does state persistence work — what is stored, where, and in what format? | Two JSON files: `climate_advisor_state.json` (runtime coordinator state, atomic write) and `climate_advisor_learning.json` (thermal model + observation history). State version mismatch discards and resets; no migration chain. | [§State Persistence Brief](state-persistence.md) |
| How does unit conversion work between °F and °C? | `from_fahrenheit()` for absolute temperatures (subtracts 32, ×5/9); `convert_delta()` for differences and rates (×5/9 only, no offset). Unit is user-selected in config flow, not auto-detected from HA. | [§Temperature Conversion Brief](temperature-conversion.md) |
| How does the REST API work — endpoints, auth, and data access pattern? | 19 views, all requiring a HA long-lived token. GETs read from `coordinator.data`; POSTs delegate to the coordinator or automation engine. Two config fields are redacted; out-of-range numerics are silently clamped. | [§REST API Brief](rest-api.md) |
| How does the Claude AI integration work — circuit breaker, rate limiting, skills, budget? | `ClaudeAPIClient` wraps all API calls with a three-state circuit breaker, per-month budget cap, and exponential-backoff retry. `AISkillRegistry` provides a pluggable skill framework; two skills are registered: `activity_report` and `investigator`. | [§AI Integration Brief](ai-integration.md) |
| Where is the full Tier 3 occupancy dispatch spec — priority resolution, state transitions, setback formulas? | The Territory spec covers all four occupancy modes, GUEST > VACATION > HOME/AWAY priority, toggle entity wiring, 7-row state transition table, setback formulas, and persistence across HA restarts. | [§Occupancy Dispatch Spec](occupancy-dispatch-spec.md) |
| Where is the Tier 3 spec for the chart state log — entry schema, ring buffer contracts, retention, persistence? | The Territory spec covers the raw/hourly/daily entry schemas, append ordering invariant, 8-path load error table, atomic write contract, downsampling tiers, and the 17,520-entry (≈1 year) cap. New `fan_running`/`nat_vent_active` fields and merged Vent bar documented in §Vent Bar Fields (Issue #331). | [Chart State Log — Territory Spec](chart-log-spec.md) |
| Where is the Tier 3 spec for the Activity Report per-event table — event vocabulary, setpoint convention, renderer runbook? | The Territory spec covers 34 event types in 6 groups, `_format_band_setpoint` single-setpoint convention, `EVENT_RENDERERS` registry, `_default_renderer` contract, dedup mechanics, and the four-step runbook for adding a new renderer. | [Activity Report Table Spec](activity-report-table.md) |
| What replaced the ClimateSimulator and how does the production harness work? | Issue #236 eliminated the standalone simulator engine. `tools/sim_harness/` drives the real `AutomationEngine` headless via FakeHass + FakeScheduler. `tools/simulate.py` is now a CLI/MANIFEST shell only. | [Sim Harness Brief](sim-harness-brief.md) |
| What are the full contracts for FakeHass, FakeScheduler, run_production, and outcomes? | FakeHass service-bus interception + state-feedback loop; FakeScheduler virtual clock patching contract; run_production event dispatch table including predicted_indoor ODE re-classification; outcomes custom assertion types. | [Sim Harness Spec](sim-harness-spec.md) |
| Is there a live second `AutomationEngine` instance, and what does it never share with production? | Yes — `coordinator.shadow_automation_engine` (Issue #613), permanently `dry_run=True`, `role="shadow"`. The coordinator's 9 callback attributes are not automatically safe to reuse: 4 reach into real production state/side effects regardless of which engine fired them, so the shadow gets its own bundle (`_build_shadow_automation_callbacks()`). `AutomationEngineCallbacks` (Issue #604) makes isolation structural, not a runtime check. | [§Engine Callback Isolation](02-ARCHITECTURE-REFERENCE.md#engine-callback-isolation-issue-604-block-5-subtask-n2) |
| Is there anything that catches a hard invariant violation (e.g. AC and WHF both physically on) even if the automation engine's own bookkeeping looks fine? | Yes — `invariant_watchdog.py` (Issue #749), a deliberately minimal detect-and-alert module that reads ground-truth sensor/thermostat state only (never internal override/grace flags) once per coordinator update cycle. Never issues a corrective command itself. | [Invariant Watchdog Brief](invariant-watchdog-brief.md) |

## File Structure

```
custom_components/climate_advisor/
├── __init__.py          # Integration setup, service registration
├── manifest.json        # HA integration metadata (domain, dependencies, version)
├── const.py             # Constants: thresholds, defaults, attribute names
├── config_flow.py       # 3-step setup wizard (entities → sensors → schedule)
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
├── repairs.py           # HA Repairs integration: surfaces actionable fix prompts when CA detects config or data problems.
├── ai_skills_investigator.py  # The sole registered AI skill ("investigator"): system prompt, response parser, deterministic fallback, thin context-assembly orchestrator. Serves both silent/scheduled narration and on-demand investigation — the former separate "Activity Report" skill (ai_skills_activity.py) was retired and merged into this one, Issue #563.
├── ai_skills_context.py  # ContextProviderRegistry + all 16 context providers the investigator's context is assembled from, including the event-timeline/renderer functions ported from the retired ai_skills_activity.py.
├── api.py               # REST API views for the dashboard panel (19 views): GETs read from coordinator.data, POSTs delegate to the coordinator or automation engine.
├── state.py              # Operational state persistence: climate_advisor_state.json, atomic write (.tmp + os.replace).
├── temperature.py        # Temperature unit utilities: from_fahrenheit() (absolute, subtracts 32 then ×5/9) vs convert_delta() (differences/rates, ×5/9 only).
│
│                         # --- Block 5 FSM migration (epic #594) — 30 files below, all added after the original 16 above ---
├── desired_state.py      # Shared pure decide_scheduled_band_gate() gate used by every scheduled/cyclical comfort-band call site (Issue #498) — replaced four independently hand-copied gate checks.
├── lifecycle_dispatcher.py  # Generic cross-lifecycle pub/sub event dispatcher for the three FSMs below. Built Issue #633, wired into production Issue #717 — see §FSM Decision Layer.
├── lifecycle_events.py   # Cross-lifecycle event vocabulary: LifecycleEventType (DOOR_PAUSE_*, GRACE_*, OVERRIDE_*, NAT_VENT_SESSION_*) + LifecycleEvent dataclass (Issue #633, extended #717).
│
├── nat_vent_fsm.py       # Nat-vent lifecycle FSM — unified (state, event) -> Transition table (Issue #633). Defines no state itself; drives NatVentLifecycleState from nat_vent_lifecycle.py.
├── nat_vent_lifecycle.py # Pure session-state derivation for nat-vent: NatVentLifecycleState (4 states — INACTIVE, ACTIVE_FULL_GATE, ACTIVE_SOFT_START, PAUSED_REACTIVATION_LOCKOUT) (Issue #606).
├── nat_vent_gate.py      # Pure decision core: the nat-vent reactivation gate (architecture-reset Step 2).
├── nat_vent_exit.py      # Pure decision core: the nat-vent active-session exit chain, all 5 exit reasons in priority order (Issue #608).
├── nat_vent_cycling.py   # Pure decision core: mid-session nat-vent fan cycling (Issue #698).
├── nat_vent_reactivation_lockout.py  # Pure decision core: the 300s nat-vent reactivation lockout predicate.
│
├── door_window_fsm.py    # Door/window pause/grace lifecycle FSM — unified transition table (Issue #637). Drives DoorWindowLifecycleState from door_window_lifecycle.py.
├── door_window_lifecycle.py  # Pure session-state derivation for door/window pause/grace: DoorWindowLifecycleState (5 states — NORMAL, PAUSED_ACTIVE, PAUSED_IDLE, GRACE, PAUSED_DURING_GRACE) (Issue #637).
├── door_window_open_response.py   # Pure decision core: a fresh door/window open event (Issue #637).
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
├── override_orphaned_grace.py  # Pure predicates for the two mirror-image grace/override watchdogs — Issue #508 (grace without override) and Issue #321 (override stuck past due grace-end) (Issue #639).
├── override_supersession.py  # Pure decision core: override detection and Issue #201/#282's "second override during active grace" replace-not-stack behavior.
│
├── fan_thermostat_decision.py  # Pure decision core: the tick-level fan thermostatic stop check (architecture-reset Step 2).
├── fan_drift_reconciliation.py # Pure decision core: fan physical-state drift reconciliation (Issue #423).
├── fan_status.py          # Fan-status suppression predicate shared by the WHF/HVAC fan status cards (§ Fan Status Values, CLAUDE.md).
├── setpoint_verify_decision.py  # Pure decision core: the post-fan setpoint verify check (architecture-reset Step 2).
│
├── classification_fsm.py  # Classification decision FSM — stateless, composes decide_scheduled_band_gate() + decide_ode_ceiling_guard() (Issue #742). See §FSM Decision Layer.
├── ode_ceiling_guard.py    # Pure decision core: the ODE ceiling guard (proactive-cooling escalation) — Issues #136/#247/#392 Fix 1, extracted Issue #742.
│
├── economizer_fsm.py      # Economizer lifecycle FSM — unified (state, event) -> Transition table (Issue #746). Drives EconomizerLifecycleState from economizer_lifecycle.py. See §FSM Decision Layer.
├── economizer_lifecycle.py  # Pure session-state derivation for the economizer: EconomizerLifecycleState (3 states — INACTIVE, COOL_DOWN, MAINTAIN) (Issue #746).
├── economizer_gate.py     # Pure decision core: the economizer eligibility/phase-selection gate (Issue #746).
├── log_capture.py         # Real WARNING+/ERROR log-record capture for the AI Investigator (Issue #578).
└── frontend/            # Dashboard panel (iframe): index.html + locally bundled Chart.js v4 + zoom plugin + HammerJS
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
One day's tracked data: what was recommended, what actually happened, outcomes (runtime, overrides, comfort violations). Stored as JSON, rolling 90-day window. v0.3.48 adds five setback-visibility fields: `setback_heat_applied_f`, `setback_cool_applied_f`, `setback_depth_f`, `setback_was_adaptive`, `setback_skipped_reason`. See [§6a DailyRecord setback fields](08-COMPUTATION-REFERENCE.md#6a-occupancy-aware-automation-guards-issue-85).

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

## Coordinator Thermal State Machine Methods

These methods on `ClimateAdvisorCoordinator` implement the Issue #121 v3 concurrent-observation pipeline. They are driven by thermostat state changes detected in `_async_thermostat_changed` and by the 30-minute coordinator tick.

| Method | Role |
|--------|------|
| `_start_hvac_observation(session_mode)` | Begins an HVAC (heat or cool) observation; pre-loads the pre-heat buffer |
| `_start_decay_observation(obs_type)` | Begins one of the four rolling-decay observation types |
| `_sample_all_observations()` | Per-tick: samples all active observations in `_pending_observations`; starts new decay types when trigger conditions first become true |
| `_end_hvac_active_phase(obs_type)` | Transitions HVAC obs from `active` to `post_heat` when HVAC stops |
| `_check_hvac_stabilization(obs_type)` | Tests for post-heat stabilization; triggers commit or continues polling |
| `_evaluate_rolling_window(obs_type)` | Commit/keep-alive decision for rolling-window decay types (30-min min, 240-min cap) |
| `_commit_rolling_window_obs(obs_type)` | Calls `_commit_observation()` for a rolling obs that has passed the signal check |
| `_commit_observation_if_sufficient(obs_type)` | Commits if `len(samples) >= min_samples`, else abandons — used on HVAC-start contamination flush |
| `_abandon_observation(obs_type, reason)` | Removes obs from `_pending_observations`; appends to rejection log; logs WARNING |
| `_commit_observation(obs_type)` | Calls `learning.record_thermal_observation()` and pops obs from `_pending_observations` |

All six concurrent types are tracked in `_pending_observations: dict[str, PendingObservation]`. The pending HVAC event is serialised in `LearningState.pending_thermal_event` so a mid-event HA restart can recover the post-heat phase. See [Thermal Model v3 Spec](thermal-model-v3-spec.md) for the full observation-type matrix and lifecycle.

> **v2 pipeline coexistence note:** The v2 HVAC-specific methods (`_start_thermal_event`, `_sample_thermal_event`, `_end_active_phase`, `_check_stabilization`, `_commit_thermal_event`, `_abandon_thermal_event`, `_update_pre_heat_buffer`) remain in the codebase as a **parallel, independent pipeline** — they are not internal helpers of the v3 methods. On every HVAC start, `_async_thermostat_changed` calls both `_start_thermal_event` and `_start_hvac_observation`, and the 30-minute tick calls both `_sample_thermal_event`/`_check_stabilization` (v2) and `_sample_all_observations`/`_check_hvac_stabilization` (v3). The v2 pipeline has not been retired. `_update_pre_heat_buffer` is shared infrastructure used by both paths.

## FSM Decision Layer

Block 5's FSM migration (epic #594) built three named-state finite state machines — one per lifecycle already governed by ad-hoc boolean flags — driven by Issue #633 (nat-vent), #637 (door/window pause), and #639 (override/grace). Each FSM's `transition()` calls the *same* pure `decide_*()` functions the legacy flag-mutating code already called directly, so no decision logic was duplicated when the FSMs were built — only the read/write of *state* was pulled into a named enum. Through Issue #727, each of the 3 `_*_fsm_authoritative` flags was an independent, runtime-toggleable, non-persisted switch. **Issue #729 replaced that**: each engine's 3 flags are now fixed at construction — one engine identity is always fully legacy (`False`/`False`/`False`), the other always fully FSM (`True`/`True`/`True`) — and `switch.climate_advisor_shadow_engine_primary` is the single remaining control axis, choosing which whole engine identity is primary. It's persisted across a restart (a deliberate departure from the original always-reset-to-legacy safety framing — see the switch's own docstring in `switch.py`), and promotion now happens via a config-entry reload rather than a live in-process swap, since the reload path cleanly cancels every engine-owned timer that a live swap could not migrate.

| FSM module | State type | Cutover switch | Authoritative today? |
|---|---|---|---|
| `nat_vent_fsm.py` (Issue #633, legacy removed Issue #757) | `NatVentLifecycleState` (`nat_vent_lifecycle.py`) — `INACTIVE`, `ACTIVE_FULL_GATE`, `ACTIVE_SOFT_START`, `PAUSED_REACTIVATION_LOCKOUT` | *(none — see below)* | Sole implementation; the 10 inline legacy call sites (no shared dispatcher — each independently called `nat_vent_fsm.transition()` or its legacy equivalent) and the `_natvent_fsm_authoritative` cutover flag were removed in Phase 6 graduation (Issue #757) once the FSM path had been permanently authoritative in production for weeks with zero corpus divergence. Removing the flag-gate surfaced two genuine pre-existing bugs in the FSM path itself (a broken Fix 2/#249 grace-bypass in `handle_door_window_open()`, and a missing reactivation-lockout arm on the `AWAY_CEILING` exit reason allowing an immediate flip-flop re-entry) — see Issue #757's own `KNOWN_FIXES` entry for full detail; both fixed in this same step, not deferred. |
| `door_window_fsm.py` (Issue #637, legacy removed Issue #757) | `DoorWindowLifecycleState` (`door_window_lifecycle.py`) — `NORMAL`, `PAUSED_ACTIVE`, `PAUSED_IDLE`, `GRACE`, `PAUSED_DURING_GRACE` | *(none — see below)* | Sole implementation; the legacy inline closures at each of `_resolve_door_window_pause_flags()`'s 14 real call sites and the `_doorwindow_fsm_authoritative` cutover flag were removed in Phase 6 graduation (Issue #757) once the FSM path had been permanently authoritative in production for weeks with zero corpus divergence. `_resolve_door_window_pause_flags()` now unconditionally dispatches through `door_window_fsm.transition()`, applying the decision for real via `_apply_door_window_fsm_state()`. Removing the flag-gate surfaced two genuine pre-existing bugs in the FSM path itself (a wrong `fan_mode` default, and SENSOR_OPENED independently re-deriving conditions its two `_pause_for_door_window()` callers had already ruled out) — see Issue #757's own `KNOWN_FIXES` entry for full detail; both fixed in this same step, not deferred. |
| `override_grace_fsm.py` (Issue #639, legacy removed Issue #757) | Composite `(OverrideConfirmState, GraceState)` (`override_grace_lifecycle.py`) — `OverrideConfirmState`: `IDLE`/`PENDING`; `GraceState`: `NONE`/`ACTIVE_PROTECTING_OVERRIDE`/`ACTIVE_UNPROTECTED` (not one flat enum, since grace routinely runs with no override behind it) | *(none — see below)* | Sole implementation; the legacy inline closures at each of `_resolve_override_grace_fsm_state()`'s 12 real call sites (10 in `automation.py`, 2 in `coordinator.py`) and the `_override_grace_fsm_authoritative` cutover flag were removed in Phase 6 graduation (Issue #757) once the FSM path had been permanently authoritative in production for weeks with zero corpus divergence. `_resolve_override_grace_fsm_state()` now unconditionally dispatches through `override_grace_fsm.transition()`, applying the decision for real via `_apply_override_grace_fsm_state()`. |
| `fan_fsm.py` (Issue #731, legacy removed Issue #757) | Composed `FanLifecycleState` (`fan_lifecycle.py`) — 5 independent axes, not one flat enum: `physical` (`OFF`/`ON`/`ON_DRIFT_SUSPECTED`), `override` (`NONE`/`ACTIVE`/`ACTIVE_REMOTE_TIMER`), `cycling` (`IDLE`/`ACTIVE`/`SUSPENDED`), `hvac_ownership` (`NONE`/`SUPPRESSING`), `rate_limit` (`NOT_DEFERRED`/`DEFERRED_ACTIVATE`/`DEFERRED_DEACTIVATE`) — because a WHF session can genuinely occupy several axes simultaneously (physically on, under manual override, owning HVAC suppression, and rate-limited, all at once) | *(none — see below)* | Sole implementation; the 17 legacy closures at each of `_resolve_fan_fsm_state()`'s real call sites and the `_fan_fsm_authoritative` cutover flag were removed in Phase 6 graduation (Issue #757) once the FSM path had been permanently authoritative in production for weeks with zero corpus divergence. `_resolve_fan_fsm_state()` now unconditionally dispatches through `fan_fsm.transition()`, applying the decision for real via `_apply_fan_fsm_state()`. |
| `classification_fsm.py` (Issue #742) | **None** — deliberately stateless (see the spec's own five-whys); `transition()` takes no `current_state` parameter, only composes `desired_state.decide_scheduled_band_gate()` (existing, unchanged) with the new `ode_ceiling_guard.decide_ode_ceiling_guard()` (this phase's extraction of `apply_classification()`'s ~190-line ODE ceiling guard block) | `AutomationEngine._classification_fsm_authoritative` | Fixed at construction, same #729 pattern — `False` for `_engine_a`/production (legacy inline block runs byte-identical), `True` for `_engine_b`/shadow (FSM's decision is applied for real via `_apply_ode_ceiling_guard_decision()`) |
| `occupancy_fsm.py` (Issue #744, legacy removed Issue #757) | **None** — deliberately stateless, same reasoning as `classification_fsm.py` (see its own module docstring's five-whys); two pure decision functions instead of one `transition()` — `decide_away_vacation_dispatch()` (shared by `handle_occupancy_away()`/`handle_occupancy_vacation()`) and `decide_home_dispatch()` (`handle_occupancy_home()`) — since the 3 real call sites are genuinely different method shapes, not one call site with internal branching. The guest/vacation/home priority resolution itself (`_compute_occupancy_mode()`) was separately extracted to `occupancy_priority.py`'s `decide_occupancy_priority()`, but that extraction is **not** flag-gated — it was already effectively pure, same footing as `select_comfort_band()`/`should_defer_to_occupancy_setback()` | *(none — see below)* | Sole implementation; the legacy inline branches in `handle_occupancy_away()`/`handle_occupancy_home()`/`handle_occupancy_vacation()` and the `_occupancy_fsm_authoritative` cutover flag were removed in Phase 6 graduation (Issue #757) once the FSM path had been permanently authoritative in production for weeks with zero corpus divergence. The three handlers now unconditionally dispatch through `_resolve_occupancy_away_vacation_fsm_state()`/`_resolve_occupancy_home_fsm_state()`, applying the decision for real via `_apply_occupancy_away_vacation_decision()`/`_apply_occupancy_home_decision()`. Unlike nat-vent/door-window/override-grace/fan/economizer, the `occupancy_mirror` shadow-diagnostic axis was KEPT rather than removed at this step — see the shadow-diagnostic paragraph below for why. Removing the flag-gate surfaced no new production bugs in this step (full suite: 5176 collected/5170 passed before → 5080 collected/5074 passed after, a delta of exactly the 96 tests in the deleted differential-comparator file that had proven the migration, nothing else). |
| `economizer_fsm.py` (Issue #746, legacy removed Issue #757) | `EconomizerLifecycleState` (`economizer_lifecycle.py`) — `INACTIVE`, `COOL_DOWN`, `MAINTAIN` (a genuine multi-tick session, same shape as nat-vent/door-window/fan above, not the stateless classification/occupancy shape — `_economizer_active` is fully redundant with `_economizer_phase != "inactive"` in every real write site, so this is a single 3-value enum rather than a composed multi-axis state like fan's) | *(none — see below)* | Sole implementation; the legacy two-phase branch in `check_window_cooling_opportunity()` and the `_economizer_fsm_authoritative` cutover flag were removed in Phase 6 graduation (Issue #757) once the FSM path had been permanently authoritative in production for weeks with zero corpus divergence. `check_window_cooling_opportunity()` now unconditionally calls `_check_window_cooling_opportunity_fsm()`, which applies the decision for real via `_apply_economizer_fsm_state()`. |

Each FSM is also shadow-mirrored the same way the whole-engine shadow (Issue #613) mirrors the rest of `AutomationEngine`: `coordinator.shadow_automation_engine` runs the identical FSM code against the same live inputs, and a diagnostic comparison (`ClimateAdvisorShadowEngineStatusSensor`) surfaces production/shadow/FSM agreement — pure observation, zero actuation surface, never wired into any occupant-facing Status-tab card. `classification_fsm.py` follows a single-axis shape (a `classification_mirror` axis only, comparing `decide_scheduled_band_gate()`'s live result on each engine's own flags), since classification is genuinely stateless rather than dispatched-in-engine. `occupancy_fsm.py` follows the same single-axis shape again (an `occupancy_mirror` axis, comparing `should_defer_to_occupancy_setback()`'s live result on each engine's own `_occupancy_mode`) — with the caveat that the occupancy dispatch *handlers themselves* (`handle_occupancy_away()`/`handle_occupancy_home()`/`handle_occupancy_vacation()`) are never invoked on the shadow engine at all (only whichever engine is currently `coordinator.automation_engine`, i.e. primary, receives those calls); `_occupancy_mode` is kept in sync production→shadow every cycle by `_sync_shadow_inputs()`, so the axis is a meaningful live comparison, but it does not close the broader shadow-engine coverage gap the project already tracks for other subsystems. `fan_fsm.py` no longer has a shadow-diagnostic axis: Phase 6 graduation (Issue #757) removed the `fan_mirror` axis, its `_SHADOW_DIAG_AXES` entry, and the differential comparator that proved the migration — there is no longer a second implementation to compare against, so a mirror axis would carry zero signal. `shadow_automation_engine.fan_lifecycle_state` is still kept live via `_sync_shadow_inputs()`'s raw copy of `_fan_active`/`_pre_fan_hvac_mode` (needed independently for `_whf_owns_hvac()`'s door/window-mirror correctness, Issue #716/#724) and the 12 fan-specific `_mirror_to_shadow()` call sites (`fan_thermostat_check`/`reconcile_fan_on_startup`/`on_fan_turned_off`/`handle_fan_manual_override`), which remain load-bearing for the nat-vent and override/grace FSM evaluators they also trigger. `economizer_fsm.py` no longer has a shadow-diagnostic axis either: Phase 6 graduation (Issue #757) removed the `economizer_mirror` axis, its `_SHADOW_DIAG_AXES` entry, and the `_economizer_active`/`_economizer_phase` raw-copy in `_sync_shadow_inputs()` along with the legacy code path and its differential comparator — there is no longer a second implementation to compare against, so a mirror axis would carry zero signal. `override_grace_fsm.py` no longer has shadow-diagnostic axes either: Phase 6 Step 3 (Issue #757) removed BOTH the `override_grace_mirror` and `override_grace_fsm` axes (this subsystem tracked two, unlike fan/economizer's one) and their `_SHADOW_DIAG_AXES` entries, along with the legacy code path and its differential comparator. Unlike fan/economizer, `_sync_shadow_inputs()`'s override/grace raw-copy block (14 fields) was deliberately left in place at Step 3 time — `_grace_active` was then a genuine live dependency of both the still-active `nat_vent` and `door_window` mirror axes (`check_natural_vent_conditions()` and `_door_window_state_for()` both read it directly), and the other 13 fields transitively feed the shadow engine's own FSM recomputation of `_grace_active` whenever `handle_fan_manual_override()`/`handle_manual_override()` are mirrored onto it — see `_sync_shadow_inputs()`'s own docstring for the full dependency chain. The underlying `_evaluate_override_grace_fsm()`/`_override_grace_fsm_state` independently-tracked shadow-FSM-replay machinery (the former `override_grace_fsm` axis's data source) was also deliberately left in place, out of Step 3's scope, for a future cleanup pass. `door_window_fsm.py` no longer has shadow-diagnostic axes either: Phase 6 Step 4 (Issue #757) removed BOTH the `door_window_mirror` and `door_window_fsm` axes (this subsystem also tracked two, like override/grace) and their `_SHADOW_DIAG_AXES` entries, along with the legacy code path and its differential comparator. Unlike Step 3's override/grace precedent, Step 4 DID remove the underlying `_evaluate_door_window_fsm()`/`_evaluate_door_window_fsm_nat_vent_exit()`/`_door_window_fsm_state` shadow-FSM-replay machinery — it had zero other consumers (confirmed by grep before deletion), so nothing was left orphaned by removing it, and doing so retired a known, confirmed-inert diagnostic bug (a missing `PAUSED_NAT_VENT_REACTIVATED` case in `_transition_from_grace()` that left the now-deleted replica tracker stuck at `GRACE` for hours — never a real-production bug, since production's own `_paused_by_door`/`_grace_active` flags were always correct; only the diagnostic replica was affected) as a side effect rather than fixing it directly. `_sync_shadow_inputs()`'s 14 raw-copy fields were re-audited at Step 4 and kept in full, unchanged — `_grace_active`'s door/window mirror dependency is gone now that `door_window_mirror`/`door_window_fsm` no longer exist, but nat-vent's own still-active dependency on the same fields (documented above) was never the reason to consider deleting them, so nothing changed. `nat_vent_fsm.py` no longer has shadow-diagnostic axes either: Phase 6 Step 5 (Issue #757) removed BOTH the `mirror` and `fsm` axes (nat-vent's own two, same two-axis shape as override/grace's and door/window's) and their `_SHADOW_DIAG_AXES` entries, along with the legacy code path and its differential comparator. Like Step 4's door/window precedent (and unlike Step 3's override/grace), Step 5 DID remove the underlying `_evaluate_nat_vent_fsm()`/`_nat_vent_fsm_state` shadow-FSM-replay machinery — it had zero other consumers (confirmed by grep before deletion), so nothing was left orphaned by removing it. `_sync_shadow_inputs()`'s 14 raw-copy fields (grace/override) plus nat-vent's own 4 (`_natural_vent_active`/`_nat_vent_soft_start`/`_paused_by_door`/`_nat_vent_outdoor_exit_time`) were re-audited at Step 5 and kept in full, unchanged — unlike the prior two steps, this is NOT a case of "the dependency's reason is gone": nat-vent's own mirrored methods (`check_natural_vent_conditions()` and siblings) are still replayed on the shadow engine by `_mirror_to_shadow()` for the dual-engine shell's own sake (the shadow engine instance itself persists until Step 8), so a stale copy of any of these fields would still corrupt that live replay, not just a deleted comparison axis. `occupancy_fsm.py`'s own flag removal (Phase 6 Step 6, Issue #757) is the first graduation step where the mirror axis was deliberately KEPT rather than removed: `occupancy_mirror` compares `should_defer_to_occupancy_setback()`'s live result on each engine's own `_occupancy_mode` — a pre-existing pure function BOTH engines already called unconditionally regardless of the (now-removed) `_occupancy_fsm_authoritative` flag, same footing as `classification_mirror`'s own still-standing axis. Removing the flag therefore did not retire this axis's usefulness the way it did for nat-vent's/door-window's/override-grace's/fan's/economizer's own mirror axes, which each compared "did the legacy branch's write agree with the FSM branch's write" — a comparison that became meaningless once there was no legacy branch left. `_sync_shadow_inputs()`'s existing `_occupancy_mode` raw-copy (`se.set_occupancy_mode(ae._occupancy_mode)`) was re-audited at Step 6 and left unchanged — it was never flag-gated. See [Nat-Vent Lifecycle Spec](nat-vent-lifecycle-spec.md), [Grace Periods Spec](grace-periods-spec.md), [Fan Lifecycle Spec](fan-lifecycle-spec.md), [Classification Lifecycle Spec](classification-lifecycle-spec.md), [Occupancy Dispatch Spec](occupancy-dispatch-spec.md), and each module's own docstring for the full transition tables and per-lifecycle scope boundaries.

### Dispatcher Wiring (Issue #717)

`lifecycle_dispatcher.py`'s generic pub/sub router (built Issue #633) was wired into a real production decision path by Issue #717/PR #720. `AutomationEngine` now owns its own `LifecycleDispatcher` instance — structurally isolated from the shadow engine's, same precedent as `AutomationEngineCallbacks` (Issue #604) — and registers as controller for all 8 event types (`DOOR_PAUSE_STARTED/ENDED`, `GRACE_STARTED/ENDED`, `OVERRIDE_CONFIRMED/CLEARED`, `NAT_VENT_SESSION_STARTED/ENDED`). Real `emit()` calls sit at the chokepoints every genuine transition already funnels through: `_resolve_door_window_pause_flags()` and `_resolve_override_grace_fsm_state()` (before/after diffs of `_paused_by_door`/`_grace_active`), `_apply_nat_vent_fsm_state()` (a second real `_paused_by_door` writer, active only when `_natvent_fsm_authoritative` is on), `_confirm_override_action()`/`_clear_manual_override_active()` (single unconditional sites for `OVERRIDE_CONFIRMED`/`CLEARED`), and a before/after diff of `_natural_vent_active` wrapped around `_decision_pass()` — the one serialization point all ~18 scattered nat-vent write sites already pass through, chosen instead of instrumenting each site individually.

**This is a same-instance emit/consume round-trip today, not a cross-instance mirror — and that distinction is deliberate, not a shortcut.** An earlier draft of this change routed `_build_nat_vent_fsm_inputs()`/`_build_door_window_fsm_inputs()` through dispatcher-synced mirror attributes (`_dispatched_paused_by_door`, `_dispatched_grace_active`, etc.) instead of the canonical `_paused_by_door`/`_grace_active`/`_manual_override_active`/`_natural_vent_active` attributes. It was reverted: `AutomationEngine` both emits and consumes every one of these events on itself, so the canonical attributes can never actually go stale relative to a same-object mirror the way `coordinator.py`'s `_sync_shadow_inputs()` mirror genuinely can (that one exists precisely because production and shadow *are* separate instances). Routing the FSM builders through the dispatcher-only mirror also broke the established direct-attribute-assignment fixture convention used across 40+ existing test files, for no real staleness benefit. The dispatcher's mirror attributes (`_dispatched_*`) still exist and are still populated by `_on_lifecycle_event()` — they're an observability/diagnostic round-trip proof (exercised by `check_registry_completeness()` and the dispatcher's own `event_log`), not the FSM builders' source of truth. The FSM input builders read the canonical attributes directly, unchanged by this wiring. Zero decision logic, zero authoritative-switch behavior, and zero HA service call path changed — see PR #720's commit message for the full before/after reasoning.

## Coordinator Chart Helper Functions

Pure module-level functions called by `get_chart_data()` to build the dashboard temperature forecast payload.

| Function | Role |
|----------|------|
| `_compute_target_band_schedule(hourly_timestamps, config, occupancy_mode, now)` | Returns `[{ts, lower, upper}]` — the occupancy-aware dynamic target band for each forecast hour. Away/vacation applies flat setback for today only; home/guest uses wake/sleep ramp schedule. Future days always use the home schedule. (Issue #119) |
| `_build_predicted_indoor_future(hourly_forecast, config, now, ...)` | Returns `[{ts, temp}]` — predicted future indoor temperatures. Uses physics ODE when model has confidence ≥ `"low"` and `k_passive < 0`; falls back to ramp interpolation otherwise. Accepts `occupancy_mode` parameter and calls `_compute_target_band_schedule()` internally so prediction and target band share a single source of truth. (Issues #114, #119) |
| `_build_future_forecast_outdoor(hourly_forecast)` | Returns `[{ts, temp}]` — raw hourly outdoor forecast temperatures. |
| `_simulate_indoor_physics(t_current, t_outdoor, q, k_passive, dt_hours)` | Single ODE time step for the physics path. |
| `_compute_ramp_hours(temp_delta, rate)` | Computes ramp duration for the legacy fallback path. |

`get_chart_data(range_str: str = "24h", before_ts: float | None = None)` — the `before_ts` parameter (Unix milliseconds, Issue #160) enables historical navigation. When supplied, the data window is anchored at that timestamp instead of the current clock: `get_entries(range_str, before=datetime.fromtimestamp(before_ts/1000, UTC))`. The frontend passes `before_ts` to scroll the chart to any past window without losing the current live view.

**Data window vs viewport:** `before_ts` shifts the *data window* (what the API fetches from the chart log). The frontend *viewport* is a separate concern — range preset buttons and drag-to-zoom control the display without necessarily changing `before_ts`. When `before_ts` is absent, the window ends at now (live mode).

`get_chart_data()` also returns two setpoint arrays added in v0.3.48 (Issue #151):

| Response key | Content |
|---|---|
| `historical_setpoint` | `[{ts, temp}]` — actual thermostat `target_temperature` captured at each 30-min poll; `null` entries when HVAC mode is off |
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

```
weather_entity: weather.home
climate_entity: climate.living_room
outdoor_temp_entity: sensor.outdoor_temp (optional)
indoor_temp_entity: sensor.indoor_temp (optional)
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
- Post-heat timeout: 45 min (`THERMAL_POST_HEAT_TIMEOUT_MINUTES`)
- Stabilization threshold: 0.3°F over 5 consecutive minutes (`THERMAL_STABILIZATION_THRESHOLD_F`, `THERMAL_STABILIZATION_WINDOW_MINUTES`)
- Sample interval: 60 seconds (`THERMAL_SAMPLE_INTERVAL_SECONDS`)
- Pre-heat buffer window: 15 min / max 15 entries (`THERMAL_PRE_HEAT_BUFFER_MINUTES`)
- Minimum R² for k_passive acceptance: 0.2 (`THERMAL_MIN_R_SQUARED`)
- Minimum post-heat samples for regression: 4 (`THERMAL_MIN_POST_HEAT_SAMPLES`)
- k_passive sanity bounds: −0.5 to −0.001 hr⁻¹
- k_active_heat sanity bounds: 0.5 to 15.0 °F/hr
- k_active_cool sanity bounds: −15.0 to −0.5 °F/hr

### Chart Log Parameters
- Entry cadence: every coordinator tick (~30 min)
- Retention cap: 365 days rolling (~17,500 entries ≈ 2MB)
- Downsampling: raw points ≤3 days; hourly averages 4–30 days; daily summaries >30 days
- Storage: `climate_advisor_chart_log.json` in HA config dir

## Supported Thermostat Configuration

CA issues separate `heat` and `cool` commands (dual `heat_cool` mode was dropped due to
thermostat compatibility bugs). The underlying HVAC system must support **both** heating
and cooling — heat-only or cool-only systems will not receive commands for their unsupported
mode and `_apply_comfort_band` will no-op silently for that day type. This is expected
behavior, not a defect.

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

### Implementation

Guards are placed at the 3 thermostat primitives (`_set_hvac_mode`, `_set_temperature`, `_notify`) in `AutomationEngine` plus the briefing notification in the coordinator. Higher-level logic (classification application, door/window pause tracking, grace periods, occupancy handling) continues unaffected.

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
Isolation between the production engine and any future second ("shadow") engine is
**structural**, not a runtime check someone can forget: a shadow engine simply is never
given a bundle containing the 4 hazardous callables above, so there is nothing to check.

The coordinator's own wiring is built by `_build_production_automation_callbacks()` — a
behavior-preserving extraction of what used to be 9 individual post-construction
assignments, now one bundle passed at construction time
(`AutomationEngine(..., callbacks=self._build_production_automation_callbacks(),
role="production")`).

**Block 5 subtask Q (Issue #613) built the second instance** N2 scaffolded a placeholder
for: `coordinator.shadow_automation_engine` is now a real, live `AutomationEngine`,
`role="shadow"`, `dry_run=True` set immediately after construction and never toggled
(no owner-facing switch this phase — that's Phase 5/subtask R). It is built with its own
`_build_shadow_automation_callbacks()` bundle:

- The 4 hazardous callables above are cut off structurally, not dry_run-gated: `revisit`
  is left `None` (not a no-op lambda — `_schedule_revisit()` calls it via
  `hass.async_create_task(revisit_cb())`, which would crash on a lambda returning `None`);
  `request_refresh`, `post_grace_fan_check`, and `reclassify` are no-op lambdas (safe here
  because `automation.py` invokes all three as plain synchronous calls, never wrapped in
  `async_create_task`).
- `sensor_check`, `sensor_debounce_pending`, `get_fan_physical_state`,
  `is_recent_fan_command` are pure reads — safely shared with production's own bundle.
- `emit_event` is shadow-local (`coordinator._on_shadow_emit_event`, a capped list),
  never `coordinator._emit_event`.

Production's nat-vent decision calls (`apply_classification`, `handle_door_window_open`,
`handle_all_doors_windows_closed`, `check_natural_vent_conditions`,
`nat_vent_temperature_check`) are each replayed on the shadow engine via
`coordinator._mirror_to_shadow()` immediately after the production call, with the same
arguments — isolated by a try/except that swallows (and logs at WARNING) any shadow-side
exception, including one from the agreement-diagnostic recompute that follows it, so a
shadow-engine bug can never affect production's own control flow. Full detail —
mirrored call sites, the diagnostic sensor, shutdown/cleanup — is in
`docs/nat-vent-lifecycle-spec.md`'s "Live Shadow Engine" section, not duplicated here.

**Rule for any future second `AutomationEngine` instance**: build a dedicated
`AutomationEngineCallbacks` bundle for it, following `_build_shadow_automation_callbacks()`'s
pattern. It must **never** reuse `coordinator._on_post_grace_fan_check`,
`coordinator._emit_event`, `coordinator.async_request_refresh`, or the
`_request_refresh_callback` lambda that wraps it — reusing any of those lets the second
engine's events/decisions mutate real production state and event history regardless of the
second engine's own `dry_run`/`role`. `tests/test_engine_callback_isolation.py::TestHazardCharacterization`
reproduces this exact hazard today (a second engine built with the production bundle leaks
an event into the production event log) as a concrete negative example for review.

## Automation Engine — Occupancy Methods

| Method | Trigger | Behaviour |
|--------|---------|-----------|
| `handle_occupancy_vacation(active: bool)` | Vacation toggle changes | `active=True`: applies setback + `VACATION_SETBACK_EXTRA` offset. `active=False`: restores comfort setpoint. Logs mode change; respects dry-run guard. |
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

Provides the Claude investigator with structured context about Climate Advisor's
internal state: sensor values, HVAC session history, thermal model parameters,
fan status, occupancy mode, and diagnostic guidance. This module knows the
thermostat's deadband behavior (swing) and explains it to the AI so it does not
misinterpret normal deadband oscillation as a CA fault. See
[Learning Engine Design](05-LEARNING-ENGINE-DESIGN.md) for swing detection details.
