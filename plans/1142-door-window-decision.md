# Issue #1142 — one door/window "ventilate or pause" decision

Status: phase 1-2 done, phase 3 partly shipped (#1143). Written against origin/main after PR #1173 (0.8.12).
Line numbers are `custom_components/climate_advisor/automation.py` unless another file is named; they will drift.

## 0. Owner constraints that shape this plan

- Build on the FSM layer (docs/02 §FSM Decision Layer: the FSMs are the sole authority). No new layer above it. Precedent for one shared pure function: `desired_state.decide_scheduled_band_gate` (#498).
- The planned window gates **pause**, not nat-vent start (#1139, docs/08 §16, matrix rows C2/C5, golden `mild_all_day_nat_vent_only`). Not a start-gate option.
- Household rules: (1) WHF never on a sealed house; (2) WHF and HVAC never together, newest occupant action wins; (3) HVAC stays paused while a window is open unless a manual override turned it on.
- Golden scenarios are locked. Anything that flips one stops at an owner decision.

## 1. Path map: where ventilate-vs-pause is decided on a door/window change

The "decision" has two halves: **(A) may nat-vent start** and **(B) record a pause (and which kind)**. Today each site answers a different subset.

| # | Site (file:line) | Half | Rules it applies | What it does NOT apply |
|---|---|---|---|---|
| 1 | `handle_door_window_open()` 5899 | A+B | protected manual change (#1133), fan_off grace (#1006), grace real-gate bypass (#655), planned window (pause only), FSM start gate (`nat_vent_fsm` TICK, hysteresis 0, grace forced off), reactivation lockout (#699), **forecast-peak guard 6088 and thermal-floor guard 6119 (only here)** | fan command result (#1140) |
| 2 | door/window FSM `SENSOR_OPENED`, `door_window_fsm.py` 449 (NORMAL) and 627 (GRACE); gate re-derivation `_nat_vent_gate_inputs` 395, used at 457, 635, 680 | B | re-derives planned window + `decide_nat_vent_gate` unless `nat_vent_gate_ruled_out`; sub-state from `decide_pause_entry` | forecast/floor guards, override, lockout (hence the `ruled_out` patch, #1132) |
| 3 | paused-by-door reactivation, `check_natural_vent_conditions()` 6396; branches ~7074 (full gate) and ~7109 (soft start) | A | nat-vent FSM TICK incl. lockout (`PAUSED_REACTIVATION_LOCKOUT`), soft-start enable flag | forecast/floor guards (#1138) |
| 4 | idle-open re-entry, same method, precondition 6482, FSM at ~6560-6620 | A | `_idle_open` = sensor open, HVAC not calling, debounce settled, no grace, no lockout; nat-vent FSM TICK | forecast/floor guards (#1138) |
| 5 | startup adoption `_reconcile_fan_on_startup_locked()` 8178 | A | adopts a fan that is already running; FSM entry gate | by design: it does not start a fan (veto would switch off an occupant-started fan). Out of scope |
| 6 | `_sync_paused_by_door_with_live_sensors()` 5728 | B | manual protection, already paused, planned window, `decide_open_sensor_hold` | nat-vent gate (calls the wrapper with `ruled_out=True`) |
| 7 | `_apply_comfort_band` guard 4004 | B | `decide_open_sensor_hold`; deliberately NOT the planned window (#629) | nat-vent gate (`ruled_out=True`) |
| 8 | `_re_pause_for_open_sensor()` 9282 | A+B | planned window, nat-vent FSM TICK (hysteresis 0, no lockout), then `_pause_for_door_window()` | forecast/floor guards, lockout |
| 9 | `_exit_nat_vent()` sensor-open branch 10607 (pause at ~10710) | B (exit side) | manual protection; records the pause on a raw `_any_monitored_sensor_open()` | fan-off result (#1141) |
| 10 | `handle_all_doors_windows_closed()` 6279 ff. (close side) | B | exit via #9 when nat-vent active; since #1173 also calls `_enforce_whf_safety_locked("sensor_closed", sealed_confirmed=True)` right after `_exit_nat_vent` (rule 1) | n/a |
| 11 | `_pause_for_door_window_action()`/`_pause_for_door_window()` 5845 (shared action + flags) | B | thermostat capture, turn off, notify, event; returns the authoritative `hvac_already_off` | (was) passing it to the FSM — #1143 |

Start sites that actually turn the fan on for nat-vent: 6159 (#1), 6605/6649 (#4), 7074/7109 (#3), 9352 (#8). Other `_activate_fan` callers (cycling 7464, min-runtime 2959/2964, economizer 15848/15861) are session-internal and must NOT get a start veto. `_activate_fan` already has one choke-point start gate since #1173 (sealed house, ~14289) — a precedent, but it is the wrong place for the forecast/floor guards precisely because of those internal callers.

#1173 impact on this map: the close side (#10) now enforces the WHF safety stop after `_exit_nat_vent`; `_whf_start_blocked_by_sealed_house()` (10167) is a rule-1 start gate living in `_activate_fan`/the min-runtime path. Neither changes the open-side decision. It does narrow the #1141 mirror below (a deferred fan-off with the window CLOSED is now caught by the enforcer).

## 2. Where they diverge (root causes, with origins)

- **#1143.** `_build_door_window_fsm_inputs()` (11263 area) read the live thermostat mode after `_pause_for_door_window_action()` had already commanded "off" (the climate entity updates inside the blocking `set_hvac_mode`). `decide_pause_entry("off")` = PAUSE_IDLE, so every active pause from NORMAL was recorded as idle. Origin: `git log -p` d8e235a (#757 Phase 6 Step 4) deleted the legacy path that wrote `hvac_already_off` from the action's return value. Effect: `_actively_paused` (6404) False, so the paused-reactivation branch (#3) is unreachable after an ordinary active pause and the idle-open branch (#4) starts the fan instead; pause flags stay set while the fan runs.
- **#1138.** The two guards were written inline in #1 (Phase 2, #116) and the Phase 2c comment (automation.py ~6450 and nat_vent_fsm) records that they were deliberately left caller-side. Sites 3, 4, 8 call the same FSM but not the guards.
- **#1140.** #1 returns after `_activate_fan()` whatever its `FanCommandResult` is; `_apply_nat_vent_fsm_state_after_activation` treats DISABLED and SUPPRESSED as active, and the three `sensor_opened` emitters report `fan: off->on` regardless.
- **#1141.** `_exit_nat_vent()` records the pause without looking at whether the fan-off ran; `_end_nat_vent_session` (#931) keeps the session on RATE_LIMITED_NEW/DUP, so session and pause are both set (the FSMs document that combination as unreachable).
- **#1132.** Same family: the FSM's re-derivation (#2) disagreed with the caller's decision. Patched by `nat_vent_gate_ruled_out`.

## 3. The authoritative decision (design)

Following the owner's verification comment: **no `decide_nat_vent_start()`, no new layer.** Two small, pure additions to the existing FSMs, and callers pass what they already decided.

1. **Half A (may nat-vent start).** The nat-vent FSM `_transition_from_inactive` (nat_vent_fsm.py 381) is already the combined start decision (override, grace, lockout, gate, soft start). Add the forecast-peak and thermal-floor guards as pure functions in `nat_vent_gate.py` (`decide_forecast_peak_veto`, `decide_floor_imminent_veto`, both returning a reason or None) and as optional `NatVentFsmInputs` fields (`forecast_peak_2h: float | None`, `thermal_k_passive`/`thermal_confidence`, defaulting to "no veto" so existing builders are unchanged). `_transition_from_inactive` returns INACTIVE with a `veto_reason` on the transition. Sites 1, 3, 4, 8 get the guards by calling the FSM they already call; `_build_nat_vent_fsm_inputs` fills the fields in ONE place. Startup adoption (5) does not fill them (explicit opt-out).
2. **Half B (record a pause).** `handle_door_window_open()` already knows the answer; it passes it to the door/window FSM event instead of the FSM re-deriving it. `nat_vent_gate_ruled_out` stays as the named "force pause" input (it forces three things: gate entered, planned window, grace — it cannot be deleted; `_nat_vent_gate_inputs` has three uses and goes only if the grace-expiry site 8 moves too). The sub-state comes from the action's `hvac_already_off` (shipped, #1143), never the live mode.
3. Planned window stays a pause-only rule (not an input to half A).

Behavior-preserving ordering: step 1 for sites that already apply the guards (site 1: replace the inline code by the FSM veto — identical outputs, provable by the existing tests plus goldens) can ship without an owner decision; wiring the veto into sites 3, 4, 8 is the #1138 behavior change (decision D2).

## 4. How each child issue is resolved

| Issue | Resolution | Status |
|---|---|---|
| #1143 | `hvac_already_off` through `PauseEntryInputs` -> `DoorWindowFsmInputs` -> `_build_door_window_fsm_inputs` -> `_resolve_door_window_pause_flags`, from `_pause_for_door_window()` and the `handle_door_window_open()` fall-through. One helper `_pause_entry_outcome()` in `door_window_fsm.py` is the only place that builds the pause-entry input. | **Shipped** (this branch) |
| #1141 | Record the exit pause only when `_end_nat_vent_session()` actually ended the session (it now returns a bool); on a deferred fan-off record nothing and let the next tick (still-true exit condition) or the window closing complete the exit. | **Written and tested, NOT shipped: it flips one locked golden** (D3). Patch + tests: `plans/1142-attachments/` |
| #1140 | Needs D1 first. | Blocked |
| #1138 | Section 3 step 1 + wiring to sites 3, 4, 8. | Blocked on D2 (site 1 extraction alone is behavior-preserving; not done here to keep the diff reviewable until D2 is answered) |
| #1132 | Already fixed; the design keeps `nat_vent_gate_ruled_out`. | Done |

## 5. Test strategy

- Real `AutomationEngine` + the state-following fake thermostat (`tests/test_restore_resolved_band._make_engine`: `set_hvac_mode` changes the state it reports). A static `MagicMock` state hides #1143 (that is why `tests/test_door_window.py` missed it, and why `tests/test_door_pause_recorded_1132.py::_paused_engine` hand-sets the flag).
- No mirror tests: assertions are on observable results (recorded sub-state, which branch ran via emitted events, thermostat state after close), not on the code path.
- Symmetry set for every new test: open vs close, heat vs cool (parametrized), fan vs no-fan (`FAN_MODE_WHOLE_HOUSE` engines), active vs idle pause, NORMAL vs GRACE origin.
- Revert-prove: each new test file is run against the pre-fix source (patch reversed) and must fail on behavior.
- Gates: `ruff check . && ruff format --check .`, `python scripts/test.py unit`, `tools/simulate.py` (golden, `--pending`, `--check-integrity`, `--surfaces`), calibration `--generate 100 --seeds 1..10` origin/main vs branch, compare hit lines (not timing); real-HA tier `tests_ha/` is not runnable in this sandbox (no WSL/HA core) — unverified.
- For D2: add a golden-independent unit test per site (3, 4, 8): forecast peak ahead -> no fan, pause kept; guard clears -> fan starts. For D1: a test per `FanCommandResult` (DISABLED, SUPPRESSED, RATE_LIMITED_*, OVERRIDDEN).

## 6. Owner decisions needed

### D1 — #1140: is "windows-only" nat-vent (no fan) intended?
Question: when `fan_mode` is disabled (or the fan command is SUPPRESSED), should an open window with favourable conditions (a) start a nat-vent session with no fan, as today, or (b) pause HVAC like any other open window?

Facts:
- Verified on origin/main: with `fan_mode` disabled the gate passes and `_natural_vent_active=True` with no service call. HVAC is not touched (`_apply_nat_vent_hvac_state`), so the house "sits" with the thermostat as it was. SUPPRESSED arises from #1050 read-back give-up, #835 HVAC-fan restriction, #893 no "on" fan mode.
- The simulator default is `fan_mode=disabled`, and the signed golden `away_natvent_activates_free_cooling` behaves as windows-only nat-vent; that golden's prose and `coordinator.py` (~11620) say a fan is the opt-in.
- **Measured** (throwaway patch in a copy of the branch: skip nat-vent when `fan_control_enabled(fan_mode)` is False, then `tools/simulate.py`): **15 of 91 goldens flip** — away_natvent_activates_free_cooling, away_natvent_exits_at_comfort_ceiling, away_with_active_natvent_transition, ceiling_guard_dormancy_nat_vent_active_in_band, ceiling_guard_fires_when_outdoor_stays_below_indoor, cold_morning_warm_day_no_breach, mild_all_day_nat_vent_only, nat-vent-evening-activation, nat-vent-indoor-hot-outdoor-near-comfort, nat-vent-outdoor-rises-above-indoor-exit, nat_vent_active_indoor_in_band_guard_dormant, nat_vent_ceiling_breach_hvac_escalation, p3_warm_day_natvent_band_stays_armed, warm_day_ceiling_breach_ac_defense, warm_day_forecast_improves_guard_dormant. (Many of those goldens set no fan at all, so the sim default is doing the work.)
- Household rules: windows-only does not break rule 1 (no fan) or rule 2 (no fan); rule 3 (HVAC paused while a window is open) is what option (b) enforces and option (a) arguably leaves to `_apply_nat_vent_hvac_state`.

Options:
1. Keep windows-only nat-vent (documented): no golden change; fix only the SUPPRESSED gap (treat it like RATE_LIMITED -> INACTIVE, never "active", and make the three `sensor_opened` emitters report `fan: off->on` only when the fan command EXECUTED). Recommended default if the owner confirms windows-only is intended; zero golden edits.
2. Pause when the fan is disabled/suppressed: rule-3-pure, but needs a re-sign of up to 15 goldens (owner-approved edits, `--sign`, one at a time with the scenario card review).
3. Windows-only only for "away" / no-fan installs, pause for installs that have a fan: a config-dependent split; smaller blast radius, more surface.

Methodology to settle it: `git log -S"fan_mode" -- tools/simulations/golden/` for each of the 15 for intent; check live installs with `fan_mode=disabled` in `ha_logs` for `natural_ventilation` periods and what the thermostat did.

### D2 — #1138: must the forecast-peak and thermal-floor guards hold on every path that can start nat-vent?
Facts:
- Verified (issue comment): a forecast-peak skip pauses HVAC, then the next coordinator refresh starts the fan via #3/#4 with no guard. "Delay" = one refresh cycle. Live: 0 guard skips 2026-10-24..30, so no occupant-visible case yet.
- #116 scoped the guards to site 1 only; docs never say they must hold everywhere.
- A persistent veto while the window stays open needs a re-check rule or the house sits with HVAC off and no fan (rule 3 holds, comfort does not).
- **Measured** (throwaway patch: guards vetoing the FSM full/soft start at sites 3 and 4): golden 91/91, pending 43/43 unchanged. Calibration with the guards on sites 3/4 was NOT measured (a run was started and lost to the sandbox OOM killer; a single `--generate` process grows past 5 GB over several seeds, so run one seed per process). Measure it before deciding.
- `reconcile_fan_on_startup` stays out (adopting a running fan, not starting one).

Options:
1. Guards are a property of the nat-vent start decision (section 3): hold on sites 1, 3, 4, 8. Veto is re-evaluated every tick (forecast peak ages out of the 2 h look-ahead; the floor estimate moves with indoor/outdoor), so no timeout is needed; the house is paused (HVAC off) meanwhile, per rule 3. Recommended.
2. Same plus an explicit cap (e.g. veto holds at most N minutes per open window, then fall back to HVAC off + no fan notice): more state; only if the owner does not accept an unbounded veto while a window is open.
3. Keep site-1-only and document it (close #1138 as won't-fix): fan can start one cycle after a "skip", which contradicts the Activity Report's "Nat vent skipped" claim.

### D3 — #1141: deferred fan-off at nat-vent exit with the window open (changes a locked golden)
Facts:
- Owner rescope 2026-10-31: pausing at exit is correct (also inside the planned window); the defect is only that the pause is recorded when the fan-off was deferred (session kept + pause set).
- Fix is written and passes 7 new tests incl. the real slow-loop tick retry and both orders (deferred off first / window closes first) — `plans/1142-attachments/1141-deferred-exit.patch` and `test_nat_vent_exit_pause_1141.py.txt` (rename to `tests/test_nat_vent_exit_pause_1141.py`). Revert-proved (4 of 7 fail without it).
- **It flips `issue_637_paused_during_grace_nat_vent_exit`** (locked, signed): nat-vent starts 13:07:00, exits 13:08:45 (inside the 5-minute floor), and the golden asserts `paused_during_grace` at 13:08:45; with the fix the assertion sees `nat_vent_outdoor_rise_exit` (pause recorded on a later tick). The golden's assertion encodes exactly the session-plus-pause combination the FSM docs call unreachable. `tests/test_production_harness.py::test_golden_passes_production_engine[...]` is the one unit test that fails.
- Mirror found while probing (not fixed): in the SENSORS-CLOSED branch of the same function, a deferred fan-off keeps the session but still starts an automation grace ("nat_vent_exit_resume") with the fan running. Probe: session True, grace True, thermostat off, fan on. #1173's enforcer now stops a fan running into a sealed house, but the grace-with-session combination is still produced. Needs its own look.

Options:
1. Owner approves editing the golden's pause assertion to the retry tick (or moving the exit past the floor), re-sign via `--sign`, ship the patch. Recommended.
2. Leave the defect and document the combination as reachable under the rate limit (then the FSM comments must change).
3. Keep recording the pause immediately but ALSO end the session logically (clear `_natural_vent_active` while the fan-off is pending) — this re-opens the stranded-fan failure #931 fixed. Not recommended.

## 7. Overlaps with concurrent work

- Edited in this branch: `_pause_for_door_window()` (5845, one argument added), the `handle_door_window_open()` fall-through tail (6237-6242, one argument added), `_build_door_window_fsm_inputs()` / `_resolve_door_window_pause_flags()` (new optional parameter), `door_window_fsm.py`, `door_window_pause_entry.py`.
- NOT edited: `_restore_comfort_after_door_window_close` (#1165/#1157), `_dispatch_override_grace_expired` / `_re_pause_for_open_sensor` / `cancel_override` (#1156/#1154) — `_re_pause_for_open_sensor` is covered through `_pause_for_door_window()` without touching it; coordinator `_async_fan_entity_changed` (#1130); `_suppress_hvac_for_whf` / `on_fan_turned_off` (#1163/#1164); `_enforce_sealed_house_whf_guard` and the #1144 enforcer.
- Future work in D1/D2 will touch `_re_pause_for_open_sensor` (site 8) and `handle_door_window_open` (shared with #1133/#1165 authors): coordinate before starting.

## 8. Calibration of what shipped (#1143)

`python tools/simulate.py --generate 100 --seeds N` for N = 1..10 (one process per seed, merged tree vs origin/main at b0b5567): all ten runs print identical output apart from timing (`hard=none` both sides; warnings identical per seed, e.g. seed 3 `blind_fan_continue 1, comfort_kept 10`, seed 5 `blind_fan_continue 2, comfort_kept 4`). No new promise hits. Golden 91/91, pending 43/43, integrity OK, P8 surfaces ok (2 known divergences, same as main). Unit tier 9293 passed, 8 skipped, 1 xfailed. `tests_ha/` (real HA) not run: not available in this sandbox, so `tests_ha/test_door_pause_recorded_1132.py` and `test_goldens_realha.py` are unverified on this change.
