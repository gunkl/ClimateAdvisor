<!-- Nav: ← [Automation Flowchart](07-AUTOMATION-FLOWCHART.md) | → [automation.py#L1082](../custom_components/climate_advisor/automation.py#L1082) [coordinator.py#L2662](../custom_components/climate_advisor/coordinator.py#L2662) | ↔ [Grace Periods](grace-periods-spec.md) [Thermal Model v3](thermal-model-v3-spec.md) [State Persistence](state-persistence.md) -->

# Occupancy Dispatch — Territory Spec (Tier 3)

## Anchors

| Question | Short answer | Full answer |
|---|---|---|
| What is the priority order when multiple toggles are ON simultaneously? | GUEST beats VACATION beats HOME/AWAY. Guest+vacation ON simultaneously resolves to GUEST (full comfort, no deep setback). | [Priority Resolution](#priority-resolution) |
| What triggers the 15-minute grace timer and what cancels it? | Any transition into AWAY mode starts the timer. Any subsequent toggle change to HOME, GUEST, or VACATION cancels it before setback fires. | [State Transitions](#state-transitions) |
| What does arriving home do while a manual override, an unconfirmed change, a door pause or an open window is in effect? | `handle_occupancy_home()` never clears `_manual_override_active`, and (Issue #1050) it no longer forces comfort over it either: the restore is deferred (`occupancy_restore_deferred`) and the push says comfort resumes once the reason clears. Nat-vent/WHF ownership is not a hold. | [`handle_occupancy_home()`](#handlers) |
| Does leaving home clear a manual override? | Yes. `handle_occupancy_away()` / `handle_occupancy_vacation()` clear a confirmed manual override, and since Issue #1050 also cancel an unconfirmed (confirm-pending) change, then apply the setback. A door pause still suppresses the setback and clears nothing. | [Manual Override Interaction](#manual-override-interaction) |
| What setback temperature does VACATION mode apply vs AWAY? | VACATION adds an extra 3°F beyond AWAY: heat target drops an additional `VACATION_SETBACK_EXTRA`, cool target rises an additional `VACATION_SETBACK_EXTRA`. | [Setback Temperature Formulas](#setback-temperature-formulas) |
| What happens to occupancy state if HA restarts mid-away-grace-period (Issue #1354)? | The person who is away or on vacation expects the setback without waiting. `async_setup()` syncs the live Vacation/Away toggles into the engine at once (no 15-minute away grace; live toggles win over a restored mode) and starts counting away minutes if `occupancy_away_since` is unset. The away timer is NOT re-armed and `handle_occupancy_away/vacation()` is not called at setup: the setback is applied by the normal plan application at the end of the startup window. A same-day restore also restores `occupancy_mode` and `occupancy_away_since`; a cross-day restore does not (the toggles decide). | [Persistence](#persistence) |
| What does `_set_temperature_for_mode()` do when called in AWAY or VACATION mode? | It is a safety net: it intercepts the call and redirects to `handle_occupancy_away()` or `handle_occupancy_vacation()` respectively, so no comfort temperature is ever applied while away. | [_set_temperature_for_mode() Safety Net](#_set_temperature_for_mode-safety-net) |
| Does the remote-sensor setpoint loop (Issue #1194 b) run while the home is away or on vacation? | No. `_occupancy_mode` away/vacation closes `_remote_offset_gate()` through the shared scheduled-band gate: the setback is written without an offset, the loop writes nothing while the home is empty, and `handle_occupancy_home()` restores the plain comfort target followed by at most one fresh step (never the old +4 F cap). | [08 Remote-sensor outer loop, Gates](08-COMPUTATION-REFERENCE.md#remote-sensor-outer-loop-on-the-thermostat-setpoint-issue-1194-increment-a) |

## Scope

This spec covers the occupancy toggle listener, mode priority resolution, state-machine transitions, automation handlers, and all occupancy-aware guards across both coordinator and automation engine.

- **Files:**
  - `custom_components/climate_advisor/coordinator.py` — toggle listener, priority resolver, away timer, state sync
  - `custom_components/climate_advisor/automation.py` — occupancy handlers, `_set_temperature_for_mode()` safety net, `apply_classification()` guards, bedtime/wakeup guards, `set_occupancy_mode()`
  - `custom_components/climate_advisor/const.py` — mode constants, `OCCUPANCY_SETBACK_MINUTES`, `VACATION_SETBACK_EXTRA`
  - `custom_components/climate_advisor/state.py` — `STATE_VERSION`, `StatePersistence` (state file I/O)
- **Approximate line ranges** (re-verified 2026-08-22; previous citations were ~5,000 lines stale):
  - `coordinator.py`: L2648–L2780 (toggle listener + priority resolver + away timer)
  - `coordinator.py`: ~L1947 (instance variables), L2333–L2340 (state restore), L2470 (state persist)
  - `automation.py`: L2432 (`apply_classification`, DEFER_OCCUPANCY guard ~L2518), L3305 (`_set_temperature_for_mode` safety net), L6655 (`handle_occupancy_away`), L6728 (`handle_occupancy_home`), L6780 (`handle_occupancy_vacation`), L6840 (`handle_bedtime`, DEFER_OCCUPANCY guard ~L6879), L7019 (`handle_pre_cool`, DEFER_OCCUPANCY guard ~L7068), L7195 (`handle_morning_wakeup`). Line numbers drift with unrelated commits — treat as approximate; verify with `grep -n "async def " automation.py` before citing.
  - `const.py`: L8333 (`OCCUPANCY_SETBACK_MINUTES`), L8377–L8383 (mode constants + `VACATION_SETBACK_EXTRA`), L8485 (`STATE_FILE`)

**Out of scope:**

- Grace period mechanics for door/window pauses — see [Grace Periods](grace-periods-spec.md)
- Thermal model and setback computation details — see [Thermal Model v3](thermal-model-v3-spec.md)
- State file atomic write mechanics — see [State Persistence](state-persistence.md)
- Bedtime setback temperature calculation (`compute_bedtime_setback`) — see [Computation Reference](08-COMPUTATION-REFERENCE.md)

## Occupancy Modes

| Constant | Value | Meaning |
|---|---|---|
| `OCCUPANCY_HOME` | `"home"` | Normal occupied — full comfort temperatures |
| `OCCUPANCY_AWAY` | `"away"` | Temporarily absent — energy setback after 15-minute grace |
| `OCCUPANCY_VACATION` | `"vacation"` | Extended absence — deep setback (3°F beyond AWAY), no grace delay |
| `OCCUPANCY_GUEST` | `"guest"` | Guests present — comfort same as HOME; routed through `handle_occupancy_home()` |

**Supporting constants (`const.py`):**

| Constant | Value | Purpose |
|---|---|---|
| `OCCUPANCY_SETBACK_MINUTES` | `15` | Grace delay before away setback fires |
| `VACATION_SETBACK_EXTRA` | `3` °F | Additional setback depth beyond AWAY for vacation mode |

## Priority Resolution

`_compute_occupancy_mode()` at `coordinator.py:L2906`. Returns one of the four mode strings. Evaluated on every toggle state change (not cached between calls).

**Issue #744 (strangler-fig completion Phase 4):** the priority algorithm below is now a pure leaf, `occupancy_priority.decide_occupancy_priority()`. `_compute_occupancy_mode()` resolves each toggle's effective boolean via `_is_toggle_on()` (still the only `hass.states` touchpoint) and delegates the decision itself. Not flag-gated — this extraction was already effectively pure, the same footing as `select_comfort_band()`/`should_defer_to_occupancy_setback()`, so no A/B shadow proof was needed (see `occupancy_priority.py`'s own module docstring).

**Algorithm (highest priority first):**

1. **GUEST** — Read `CONF_GUEST_TOGGLE` entity. If configured and `_is_toggle_on()` returns `True` → return `OCCUPANCY_GUEST` immediately. No further checks.
2. **VACATION** — Read `CONF_VACATION_TOGGLE` entity. If configured and `_is_toggle_on()` returns `True` → return `OCCUPANCY_VACATION`.
3. **HOME vs AWAY** — Read `CONF_HOME_TOGGLE` entity. If configured:
   - `_is_toggle_on()` returns `True` → return `OCCUPANCY_HOME`
   - `_is_toggle_on()` returns `False` (entity OFF or not configured) → return `OCCUPANCY_AWAY`
4. **Default** — No toggle entities configured at all → return `OCCUPANCY_HOME`

**GUEST beats VACATION:** When both `guest_toggle` and `vacation_toggle` entities are ON simultaneously, the function returns `OCCUPANCY_GUEST`. This is intentional — guests deserve full comfort, not deep setback.

## Toggle Entities

| Config key | Constant | Purpose | Invert key |
|---|---|---|---|
| `"home_toggle_entity"` | `CONF_HOME_TOGGLE` | Home/away binary sensor | `CONF_HOME_TOGGLE_INVERT` |
| `"vacation_toggle_entity"` | `CONF_VACATION_TOGGLE` | Vacation binary sensor | `CONF_VACATION_TOGGLE_INVERT` |
| `"guest_toggle_entity"` | `CONF_GUEST_TOGGLE` | Guest binary sensor | `CONF_GUEST_TOGGLE_INVERT` |

**`_is_toggle_on(entity_id, invert)` behavior** (`coordinator.py:L2892`):

- Reads entity state from `hass.states.get(entity_id)`.
- States `"unavailable"` and `"unknown"` are treated as `False` (OFF). No exception is raised.
- Invert is applied as a boolean XOR: `result = raw_state XOR invert`. A configured invert flag on an unavailable entity still resolves to `False` (unavailable is never treated as ON even after inversion).

Toggle entities may be any binary sensor or input boolean. The integration subscribes to state-change events for each configured toggle entity at setup time (exact listener-registration call site not re-verified in this pass — search for the toggle config keys' entity-id reads during setup rather than trusting a specific line number here).

## State Transitions

### Flow of `_async_occupancy_toggle_changed` (`coordinator.py:L2958`)

Executes whenever any configured toggle entity fires a state-change event:

1. Call `_compute_occupancy_mode()` → `new_mode`
2. If `new_mode == self._occupancy_mode`: **no-op, return immediately**
3. Save `old_mode = self._occupancy_mode`
4. **Away duration tracking:**
   - If departing (transitioning TO any away-type mode from HOME/GUEST): record `_occupancy_away_since = now`
   - If returning (transitioning FROM any away-type mode to HOME/GUEST): compute elapsed minutes as `(now - _occupancy_away_since).total_seconds() / 60.0`, add to `_today_record.occupancy_away_minutes`, clear `_occupancy_away_since = None`
5. Set `self._occupancy_mode = new_mode`
6. Sync to engine: `automation_engine.set_occupancy_mode(new_mode)`
7. **Dispatch by new mode:**
   - **VACATION** → `_cancel_occupancy_away_timer()` → `await automation_engine.handle_occupancy_vacation()` (immediate, no grace)
   - **AWAY** → `_cancel_occupancy_away_timer()` → start 15-minute `async_call_later` timer; callback `_occupancy_away_timer_expired` calls `hass.async_create_task(automation_engine.handle_occupancy_away())`; timer handle stored in `_occupancy_away_timer_cancel`
   - **HOME or GUEST** → `_cancel_occupancy_away_timer()` → `await automation_engine.handle_occupancy_home()`
8. `await _async_save_state()`

### Transition Table

| From | Event | To | Grace | Side effects |
|---|---|---|---|---|
| Any | toggle → GUEST | GUEST | None | cancel away timer; `handle_occupancy_home()` |
| Any | toggle → VACATION | VACATION | None | cancel away timer; `handle_occupancy_vacation()` immediately |
| Any | toggle → AWAY | AWAY (pending) | 15 min | start away timer; setback fires on expiry via `handle_occupancy_away()` |
| AWAY (pending) | 15-min timer expires | AWAY (active) | — | `handle_occupancy_away()` called via `async_create_task` |
| AWAY (pending) | toggle → HOME | HOME | None | cancel away timer; `handle_occupancy_home()` |
| AWAY (pending) | toggle → GUEST | GUEST | None | cancel away timer; `handle_occupancy_home()` |
| AWAY (pending) | toggle → VACATION | VACATION | None | cancel away timer; `handle_occupancy_vacation()` immediately |
| Any | toggle → HOME | HOME | None | cancel away timer; `handle_occupancy_home()` |
| Any | HA restart | (restored) | — | same-day restore reads the mode from `climate_advisor_state.json`; `async_setup()` then syncs the live toggles into the engine (Issue #1354, immediate, no away grace); away timer NOT re-armed |

`_cancel_occupancy_away_timer()` (`coordinator.py:L2951`): calls the stored cancel handle and sets `_occupancy_away_timer_cancel = None`. Safe to call when no timer is running.

## Handlers

**Issue #744 / Phase 6 Step 6 (Issue #757):** each of the 3 handlers below now unconditionally dispatches through a stateless FSM branch — the `AutomationEngine._occupancy_fsm_authoritative` cutover flag and the legacy inline blocks it gated were removed once the FSM path had been permanently authoritative in production for weeks with zero corpus divergence (proven via the now-deleted `tools/sim_harness/occupancy_fsm_authoritative_compare.py` differential comparator). `handle_occupancy_away()`/`handle_occupancy_vacation()` share one pure decision, `occupancy_fsm.decide_away_vacation_dispatch()`; `handle_occupancy_home()` uses `occupancy_fsm.decide_home_dispatch()`. See `occupancy_fsm.py`'s own module docstring for the full five-whys on why this FSM is stateless and why it uses two decision functions instead of one `transition()`. The side-effecting shells are `AutomationEngine._apply_occupancy_away_vacation_decision()`/`_apply_occupancy_home_decision()`.

### `handle_occupancy_home()` (`automation.py:L6794`)

Invoked for both HOME and GUEST modes (coordinator routes GUEST through this same handler).

**Sequence:**

1. Sets `self._occupancy_mode = OCCUPANCY_HOME`
2. If no current classification (`_current_classification` is None): return (no HVAC action)
3. Always (Issue #1208: the family is the resolved band's active edge, `_comfort_restore_family()`, not `c.hvac_mode`, which is "off" on warm/mild days; `HomeInputs.hvac_mode` carries that family, and the leaf's `_RESTORE_MODES` check is unchanged): call `_set_temperature_for_mode()` to restore comfort temperature -- **unless a restore hold applies (Issue #1050, #1048 change 18)**: `_occupancy_restore_hold_reason()` returns, in priority order, `manual_override` (`_manual_override_active`), `override_confirm_pending` (`_override_confirm_pending`), `paused_by_door` (`_paused_by_door`) or `open_sensor` (`desired_state.decide_open_sensor_hold()`: an open, debounced sensor that neither nat-vent nor a WHF session owns). When held, `HomeDecision.restore` is False and `restore_deferred_reason` is set: no restore write, no `occupancy_comfort_restored` event, INFO `Occupancy home -- restore deferred: reason=... mode=...`, and an `occupancy_restore_deferred` event `{mode, reason, indoor_f}` (windowed 600 s). **Reporting what was written (Issue #1245):** when the restore does run, `_set_temperature_for_mode()` records what it did in `self._last_mode_write` (`ComfortModeWrite`: `result`, `family`, `target_f`, `held_reason`). `occupancy_comfort_restored` is emitted only when a write was made and reports THAT family and `target_f` -- the resolved band edge actually sent (sleep band, aggressive savings), never the flat comfort setting. When nothing was written (`held_reason`: `whf_session` = `_set_temperature()` returned `WHF_BLOCKED`; `blower_hold` = `_blower_session_holds_hvac_off()`, Rule 3; `occupancy_redirect`; `no_family`) the engine emits `occupancy_comfort_restore_held` `{mode, reason, indoor_f}` (windowed 600 s on `reason`) instead. `occupancy_comfort_restored` also carries `outcome` (`hvac_outcome_label()`: `sent` / `dry_run` / `failed` / `superseded`); a restore that was attempted but not sent stays `occupancy_comfort_restored` (CA is back in control, P7 resumes) and renders "comfort restore NOT sent -- ...". When the restore was held, the welcome-home push (step 7) reads "Comfort temperature will resume once ventilation ends." instead of "Restoring comfort temperature". Nat-vent/WHF ownership is **not** a hold -- arrival behavior during nat-vent is unchanged. The restore resumes through the normal paths once the reason clears (grace expiry, close/resume, next 30-min cycle); `occupancy_restore_hold_reason()` exposes the still-held reason to the chart (the target band keeps showing the setback) and clears itself
4. **Notification suppression check 1 — proximity:** if indoor temp is already closer to comfort than to setback (`abs(indoor - comfort) < abs(indoor - setback)`), record notification timestamp and return without sending. `HomeDecision` carries `indoor_temp_f` (Issue #1036) — the `HomeInputs.indoor_temp_f` the decision was made from — and the shell's log line and the `occupancy_comfort_restored` event use that value instead of re-reading the sensor after the restore `await` (a re-read could be `None` if the thermostat dropped out and crashed the `%.1f` formatting). Set on every non-NONE outcome; `None` default
5. **Notification suppression check 2 — debounce:** if a welcome-home notification was sent within `CONF_WELCOME_HOME_DEBOUNCE` seconds, return without sending
6. Record `_last_welcome_home_notified = now`
7. Send notification: `"Welcome home! Restoring comfort temperature. Should feel normal in about 20–30 minutes."` via `_notify(..., notification_type="occupancy_home")`; when the restore was deferred (step 3) the text is instead `"Welcome home! Comfort temperature will resume once {your manual thermostat change | your pending thermostat change | the open door/window} clears."`; when the restore ran but wrote nothing (`_last_mode_write.held_reason` set, Issue #1245) it is `"Welcome home! Comfort temperature will resume once ventilation ends."`

**Does NOT clear `_manual_override_active`.** Before Issue #1050 a manual override active when the user returned did not stop the comfort restore (the override flag was only checked in `apply_classification()`); that was the occupant-visible gap this change closes -- the restore now defers to the override (step 3) instead of overwriting the user's own setting. Only bedtime, morning wakeup and the away/vacation handlers clear the override.

### `handle_occupancy_away()` (`automation.py:L6712`)

Invoked after the 15-minute grace timer expires, and also called by `apply_classification()` when occupancy is AWAY on the 30-minute poll cycle.

**Accuracy correction (found while re-verifying citations for Issue #744):** the "Apply setback via `_set_temperature()`" formula description below predates the band-based rewrite (`select_comfort_band()` + `_apply_comfort_band()`, both edges armed at once, thermostat self-arbitrates — see `automation.py`'s own comment "Arm the away setback band — covers both edges; thermostat self-arbitrates") — the single-edge `_set_temperature()` call this section describes no longer matches current code. Full re-derivation of this section against the current band-based implementation is out of scope for Phase 4 (Issue #744) — the FSM extraction reused the current implementation's behavior unchanged (see `occupancy_fsm.py`'s `decide_away_vacation_dispatch()`), it did not touch which setpoint mechanism is used. Flagged here rather than silently left wrong.

**Current sequence (both `handle_occupancy_away()` and `handle_occupancy_vacation()`, Issue #744's `decide_away_vacation_dispatch()`):**

1. Sets `self._occupancy_mode` to `OCCUPANCY_AWAY`/`OCCUPANCY_VACATION`.
2. If `_paused_by_door`: suppress (log info, emit `occupancy_setback_suppressed_paused` with windowed dedup), return — override is NOT cleared in this branch.
3. If `_manual_override_active` **or `_override_confirm_pending`** (Issue #1050: `AwayVacationInputs.override_confirm_pending`; `clear_override = manual_override_active or override_confirm_pending`): clear it via `clear_manual_override(reason="occupancy_away"/"occupancy_vacation")` — leaving home with an unconfirmed manual change cancels it and applies the setback. This happens regardless of whether a classification exists yet (see step 4). Issue #1222: right after the clear, the grace that protected the cleared override is ended too, with `_end_grace_left_protecting_nothing("occupancy_away"/"occupancy_vacation", remote_timer_hours=<`_fan_remote_timer_hours` read before the clear>)` (`grace_cancelled`; same helper as bedtime/wake-up #874). Only an override-protecting grace ends: a fan-off hold, window-close, dashboard-resume or nat-vent-exit grace survives away/vacation. Without it the grace lingered until the coordinator's orphaned-grace watchdog reaped it with an ERROR.
4. If no current classification: (away only) log WARNING; return.
5. Otherwise, compute the setback band via `select_comfort_band(c, config, occupancy_mode=..., in_sleep_window=False, aggressive_savings=...)` and arm it via `_apply_comfort_band()` (both floor/ceiling edges, thermostat self-arbitrates) — emit `occupancy_setback` with windowed (600s) dedup (Issue #591).
6. **No notification sent** (away/vacation transitions are silent; only the HOME transition notifies).

### `handle_occupancy_vacation()` (`automation.py:L6851`)

Invoked immediately (no grace) when VACATION mode is detected. Shares the exact sequence above (steps 2–6) — deep setback vs. standard setback is a `select_comfort_band()` parameter difference (`VACATION_SETBACK_EXTRA = 3°F` wider on both edges), not a different control-flow path.

## Setback Temperature Formulas

| Mode | Heat setpoint | Cool setpoint |
|---|---|---|
| HOME / GUEST | `comfort_heat` | `comfort_cool` |
| AWAY | `setback_heat` | `setback_cool` |
| VACATION | `setback_heat - 3` | `setback_cool + 3` |

The trend `c.setback_modifier` is NOT applied to away/vacation setbacks (the engine's `select_comfort_band()` does not, and since Issue #1076 neither does the chart band); it only moves the pre-cool target. `VACATION_SETBACK_EXTRA = 3` (°F, defined in `const.py:L8383`).

**Comfort-band definition:** `comfort_heat` is the lower bound; `comfort_cool` is the upper bound. In-band condition: `comfort_heat ≤ T ≤ comfort_cool`.

## 30-Minute Poll Guards (`apply_classification`)

`apply_classification()` (`automation.py:L2476`) runs every 30 minutes when the coordinator refreshes, and also on grace-period expiry outside the sleep window (via `_apply_current_scheduled_state()`) and on manual-override cancellation (via the dashboard's Cancel Override button, ~10s delayed). Occupancy guards execute **after** the manual override check and **after** the override-confirm-pending check, then route through the shared `desired_state.decide_scheduled_band_gate()` (Issue #498).

**Evaluation order inside `apply_classification()`:**

1. **Manual override active** → log and return. Override wins; occupancy is not evaluated.
2. **Override confirm pending** → log and return.
3. **Gate resolves `DEFER_OCCUPANCY`** (occupancy is AWAY or VACATION):
   - **AWAY** → call `handle_occupancy_away()` to actively reapply the setback band, then return.
   - **VACATION** → call `handle_occupancy_vacation()` to actively reapply the deep setback band, then return. *(Issue #505: prior to this fix, VACATION just logged "deep setback preserved" and returned without ever re-arming the band — this was an original design gap from Issue #85, not caught by any existing test, that let vacation's setback go unenforced for the rest of a real trip once anything — most commonly a manual override — moved the thermostat off it. `handle_occupancy_vacation()` already existed and already did the correct thing; it was simply never called from this path.)*
   - **Then cancel the pass's own 5-minute revisit** (`_cancel_classification_revisit()`, Issue #1363), exactly as the normal path does at its end (Issue #96 Root Cause E). The reapply writes the setpoint and `_record_action()` arms a revisit; before #1363 this branch returned without cancelling it, so the revisit re-ran the pass, which wrote the same setpoint again and armed the next one. The occupant saw no change, but the thermostat got about 12 identical writes an hour for the whole time the house was away (64 in 5.3 h per zone in production). Now the setback is re-confirmed on the 30-minute cycle like comfort is at home. Leaving (the toggle handler calling `handle_occupancy_away()`/`handle_occupancy_vacation()` directly) still keeps its one 5-minute follow-up check.
4. **HOME or GUEST** → proceed to normal comfort application (never reaches the occupancy gate — `decide_scheduled_band_gate()` only routes AWAY/VACATION through `DEFER_OCCUPANCY`).

The manual-override-first ordering means: if both `_manual_override_active` and `_occupancy_mode == VACATION` are true, the override check fires first and the function returns before the occupancy gate is ever evaluated. Once the override later clears (confirm or cancel), the next `apply_classification()` pass reaches the gate and actively reapplies the correct setback — it does not merely assume it's already there.

## Bedtime and Wakeup Guards

**`handle_bedtime()` (`automation.py:L6916`):**

- If the gate resolves `DEFER_OCCUPANCY` (VACATION or AWAY): bedtime-specific sleep temps are still skipped (applying them would move the thermostat in the wrong direction), but as of Issue #505 the away/vacation setback is now **actively reapplied** here too — `handle_occupancy_vacation()`/`handle_occupancy_away()` is called before returning, exactly mirroring `apply_classification()`'s branch above. This matters because grace-period expiry landing *inside* the sleep window routes here instead of to `apply_classification()` (see `_apply_current_scheduled_state()`), so the same "setback already active" assumption would otherwise be exposed to the same staleness bug on a narrower (≤30 min, until the next backstop cycle) but still real window.
- For HOME/GUEST: clears manual override, deactivates fan if running, applies adaptive bedtime setback via `compute_bedtime_setback()`.

**`handle_morning_wakeup()` (`automation.py:L7271`):**

- If `_occupancy_mode` is NOT `OCCUPANCY_HOME` or `OCCUPANCY_GUEST`: log info and return. No comfort restore while away — this path is unaffected by Issue #505 (a silent no-op here is correct; the away/vacation setback is the intended active state and gets actively reconfirmed elsewhere, not restored to comfort).
- For HOME/GUEST: clears manual override, deactivates fan if still running, restores comfort temperatures.

**`handle_pre_cool()`:** has the same `DEFER_OCCUPANCY` shape as `handle_bedtime()` and received the identical Issue #505 fix — reapplies the away/vacation setback before returning "skipped" rather than assuming it's already active.

## `_set_temperature_for_mode()` Safety Net

`_set_temperature_for_mode()` (`automation.py:L3362`) is the common comfort-application path called by most HVAC-setting code within the engine. It acts as a last-resort occupancy guard, unified via `should_defer_to_occupancy_setback()` (Issue #460):

1. If `_occupancy_mode == OCCUPANCY_AWAY`: log info, `await handle_occupancy_away()`, return.
2. If `_occupancy_mode == OCCUPANCY_VACATION`: log info, `await handle_occupancy_vacation()`, return.
3. Otherwise (HOME/GUEST): apply `comfort_heat` or `comfort_cool` (plus any pre-condition offsets).

**Purpose:** Any code path that calls `_set_temperature_for_mode()` is automatically protected — even if the caller does not check occupancy. Direct calls to `_set_temperature()` (the lower-level setter) bypass this safety net. The away and vacation handlers themselves use `_set_temperature()` directly to avoid infinite recursion.

## Persistence

**State file:** `climate_advisor_state.json` in the HA config root (constant `STATE_FILE` in `const.py:L8485`). Written atomically via `StatePersistence` (`state.py`) using a `.tmp` file and `os.replace()`.

**Keys written (`coordinator.py:L2470`):**

| Key | Type | Value when away timer pending |
|---|---|---|
| `"occupancy_mode"` | `str` | Current mode string (e.g., `"away"`) |
| `"occupancy_away_since"` | `str` \| `null` | ISO datetime string of departure; `null` if home |

**Restore sequence (same-day restore only, below the date gate; `coordinator.py` `async_restore_state`, Issues #1349/#1354):**

1. Read `state.get("occupancy_mode", OCCUPANCY_HOME)` → `self._occupancy_mode`
2. Call `automation_engine.set_occupancy_mode(self._occupancy_mode)` to sync engine
3. Parse `state.get("occupancy_away_since")` → `self._occupancy_away_since` (as `datetime`) or `None`

**At setup (Issue #1354):** `async_setup()` recomputes the mode from the live toggles, calls `automation_engine.set_occupancy_mode()` (live toggles win over the restored mode; INFO `Occupancy at startup: engine <old> -> live toggles <new>` on a difference) and, for a non-present mode with no `occupancy_away_since`, sets it to now. On a cross-day restore the mode and away-since are not restored (same-day only); the toggles alone decide.

**Version mismatch:** `STATE_VERSION = 1` (defined in `state.py:L21`). If the file contains a different version (or is missing/corrupt), `StatePersistence.load()` returns an empty dict and logs a WARNING. The coordinator then defaults `occupancy_mode` to `OCCUPANCY_HOME`.

**Away timer is NOT persisted.** If HA restarts while the 15-minute away timer is running, the timer callback is lost. Since Issue #1354 the engine already holds the live away/vacation mode after setup, so the setback is applied by the normal plan application at the end of the startup window rather than waiting for the next toggle change. The 30-minute `apply_classification()` poll will reapply setback on its next cycle if the mode is still AWAY at that time.

## Manual Override Interaction

**Override flag (`_manual_override_active`)** is checked before all occupancy logic in `apply_classification()`. If both are active:

- Override wins at the `apply_classification()` entry point.
- Occupancy mode is still tracked and synced correctly — the override only suppresses temperature changes, not mode bookkeeping.

**What clears the override flag:**

| Event | Clears `_manual_override_active` |
|---|---|
| `handle_bedtime()` (HOME/GUEST only) | Yes — calls `clear_manual_override()` |
| `handle_morning_wakeup()` (HOME/GUEST only) | Yes — calls `clear_manual_override()` |
| `handle_occupancy_home()` | **No** (and it defers its own restore while the override is active, Issue #1050) |
| `handle_occupancy_away()` | **Yes** -- also cancels an unconfirmed (confirm-pending) change (Issue #1050) and, since Issue #1222, ends the override's grace; not when `_paused_by_door` (suppressed branch) |
| `handle_occupancy_vacation()` | **Yes** -- same as away (including ending the override's grace, Issue #1222) |
| Toggle entity state change | **No** |

**No direct occupancy override mechanism exists.** Occupancy mode is determined solely by the toggle entity states evaluated in `_compute_occupancy_mode()`. There is no API or service call to force-set the occupancy mode independently of the toggle entities.

## Invariants

1. `_occupancy_mode` always contains one of the four valid strings: `"home"`, `"away"`, `"vacation"`, `"guest"`. The `set_occupancy_mode()` validator (`automation.py:L1126`) enforces this — invalid values log a WARNING and default to `OCCUPANCY_HOME`.
2. The coordinator's `self._occupancy_mode` and the engine's `_occupancy_mode` are always in sync at the end of any transition. `set_occupancy_mode()` is called in the same transaction as the coordinator's own assignment (`coordinator.py:L2936`, alongside `_async_occupancy_toggle_changed()`'s own `self._occupancy_mode = new_mode`).
3. At most one away timer is pending at any time. `_cancel_occupancy_away_timer()` is always called before starting a new timer, and before dispatching VACATION, HOME, or GUEST handlers.
4. `handle_occupancy_away()` is never called directly by the coordinator — it is always routed through either the timer callback or `apply_classification()`. This preserves the 15-minute grace guarantee.
5. `handle_occupancy_vacation()` is always called immediately on the vacation toggle (no grace). There is no equivalent grace timer for vacation mode. As of Issue #505, it is also called from `apply_classification()`/`handle_bedtime()`/`handle_pre_cool()`'s `DEFER_OCCUPANCY` branches on every subsequent cycle while vacation mode is active — not just once at toggle time.
6. GUEST mode is functionally identical to HOME at the automation handler level. The distinction is tracked in `_occupancy_mode` for reporting and briefing, but `handle_occupancy_home()` is the only handler invoked for both.
7. Away duration (`_today_record.occupancy_away_minutes`) is only incremented on return from a non-home mode. If HA restarts mid-departure, the duration from `_occupancy_away_since` to restart is not counted (timer is not re-armed, so the next toggle change that returns to HOME accumulates from `_occupancy_away_since` which was persisted).
8. The `_set_temperature_for_mode()` safety net cannot recurse: away and vacation handlers arm the setback band via `select_comfort_band()`/`_apply_comfort_band()` directly (see the "Accuracy correction" note above the handler sections), never routing back through `_set_temperature_for_mode()`.

## Error Conditions

| Failure | Handling | Observable effect |
|---|---|---|
| Toggle entity `unavailable` or `unknown` | `_is_toggle_on()` treats it as `False` (OFF); no exception raised | Entity effectively reads as "not toggled on"; may hold incorrect mode if the sensor is persistently unavailable |
| HA restart while 15-minute away timer is pending | Timer callback lost. Since Issue #1354 `async_setup()` syncs the live Vacation/Away toggles into the engine and starts away-minute counting, so the mode is correct at once; the setback is applied by the normal plan application at the end of the startup window (or the next `apply_classification()` cycle / toggle change) | No 15-minute grace after a restart; setback follows the startup plan application |
| `set_occupancy_mode()` receives an unrecognized mode string | Logs WARNING at `automation.py:L1129` (inside `set_occupancy_mode()`, `L1126`); defaults to `OCCUPANCY_HOME` | Engine remains in HOME mode; coordinator and engine may briefly be out of sync if coordinator accepted the invalid mode |
| State file missing, corrupt, or wrong `STATE_VERSION` | `StatePersistence.load()` returns `{}` and logs WARNING; coordinator defaults to `OCCUPANCY_HOME` | Occupancy state is reset to HOME on restart; `_occupancy_away_since` is cleared |
| No day classification available when handler fires | `handle_occupancy_away()` logs WARNING and returns; `handle_occupancy_home()` and `handle_occupancy_vacation()` return silently | No HVAC temperature change; setback or comfort restore is deferred until `apply_classification()` runs next |
| Both `guest_toggle` and `vacation_toggle` entities ON simultaneously | `_compute_occupancy_mode()` returns `OCCUPANCY_GUEST` (GUEST has higher priority) | Full comfort applied; deep vacation setback is not used; intentional by design |

## Code Reference

- [`set_occupancy_mode`](../custom_components/climate_advisor/automation.py#L1126) — validates and sets engine's occupancy mode
- [`apply_classification` (occupancy guards)](../custom_components/climate_advisor/automation.py#L2476) — override-first, then override-confirm-pending, then `decide_scheduled_band_gate()`'s `DEFER_OCCUPANCY` branch
- [`_set_temperature_for_mode`](../custom_components/climate_advisor/automation.py#L3362) — comfort-application safety net
- [`handle_occupancy_away`](../custom_components/climate_advisor/automation.py#L6712) — away setback handler
- [`handle_occupancy_home`](../custom_components/climate_advisor/automation.py#L6794) — home/guest comfort restore + notification
- [`handle_occupancy_vacation`](../custom_components/climate_advisor/automation.py#L6851) — deep vacation setback handler
- [`handle_bedtime` (occupancy guard)](../custom_components/climate_advisor/automation.py#L6916) — skips sleep setback when away/vacation (DEFER_OCCUPANCY guard)
- [`handle_morning_wakeup` (occupancy guard)](../custom_components/climate_advisor/automation.py#L7271) — skips comfort restore when not home/guest (DEFER_OCCUPANCY guard)
- [`_is_toggle_on`](../custom_components/climate_advisor/coordinator.py#L2892) — reads toggle entity state with unavailable=OFF and XOR invert
- [`_compute_occupancy_mode`](../custom_components/climate_advisor/coordinator.py#L2906) — resolves toggle booleans, delegates priority to `occupancy_priority.decide_occupancy_priority()` (Issue #744)
- [`_cancel_occupancy_away_timer`](../custom_components/climate_advisor/coordinator.py#L2951) — safe timer cancellation
- [`_async_occupancy_toggle_changed`](../custom_components/climate_advisor/coordinator.py#L2958) — toggle event listener and dispatch orchestrator
- [`OCCUPANCY_HOME/AWAY/VACATION/GUEST`](../custom_components/climate_advisor/const.py#L8490) — mode constants
- [`occupancy_priority.decide_occupancy_priority`](../custom_components/climate_advisor/occupancy_priority.py) — pure priority-resolution leaf, extracted Issue #744 (not flag-gated — see module docstring)
- [`occupancy_fsm.decide_away_vacation_dispatch`, `decide_home_dispatch`](../custom_components/climate_advisor/occupancy_fsm.py) — stateless dispatch FSM, Issue #744; unconditionally authoritative since Phase 6 Step 6 (Issue #757) removed the `AutomationEngine._occupancy_fsm_authoritative` cutover flag
- [`AutomationEngine._resolve_occupancy_away_vacation_fsm_state`, `_apply_occupancy_away_vacation_decision`, `_resolve_occupancy_home_fsm_state`, `_apply_occupancy_home_decision`](../custom_components/climate_advisor/automation.py) — the resolver/apply-shell wiring, Issue #744

_Line numbers re-verified 2026-08-22 (Issue #744) against the working tree — the automation.py handler/coordinator citations above were stale again (drifted since #735's own re-verification, from Phase 3/#742 adding ~250 lines). The away/vacation "Setback Temperature Formulas" section's underlying mechanism (`_set_temperature()` with inline formulas) is also stale relative to current code (band-based `select_comfort_band()`/`_apply_comfort_band()`) — flagged in place above rather than silently left wrong; a full rewrite of that section was out of scope for Phase 4. Verify with `grep -n` before citing in a future PR — these will drift again._
