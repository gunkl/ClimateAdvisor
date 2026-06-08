<!-- Nav: ← [07-AUTOMATION-FLOWCHART.md](07-AUTOMATION-FLOWCHART.md) | → [automation.py](../custom_components/climate_advisor/automation.py) + [coordinator.py](../custom_components/climate_advisor/coordinator.py) | ↔ [state-persistence.md](state-persistence.md) -->

# Grace Period State Machine — Territory Spec (Tier 3)

## Anchors

| Question | Short answer (≤2 sentences) | → Full answer |
|---|---|---|
| What triggers a manual grace period vs an automation grace period? | Manual grace starts after a user-initiated HVAC change (thermostat override during pause, fan manual change, or dashboard resume). Automation grace starts whenever Climate Advisor itself resumes HVAC after a door/window closes or after natural ventilation ends. | [§ Grace Period Types](#grace-period-types) |
| How long does each grace type last by default, and can it be configured? | Manual grace defaults to 1800 s (30 min); automation grace defaults to 300 s (5 min). Both durations are configurable via `manual_grace_seconds` and `automation_grace_seconds` in config; a value of 0 disables grace entirely. | [§ Grace Period Types](#grace-period-types) |
| What does an active grace period suppress? | A door or window opening during an active grace period does NOT pause HVAC (unless outdoor temperature is already cool enough to qualify for natural ventilation). | [§ Manual Grace — What It Suppresses](#manual-grace) |
| When a grace timer fires, what are the three possible outcomes? | (1) Within planned window period → clear grace silently. (2) A sensor is still open → clear grace flags, then schedule `_re_pause_for_open_sensor()`. (3) All sensors closed → clear grace flags, optionally send notification. | [§ Timer Lifecycle — Expiry Callback](#timer-lifecycle) |
| Is grace state persisted across HA restarts? | Yes (fixed v0.3.56 / #227). `async_restore_state()` re-schedules the grace timer with remaining duration; if already expired during restart, override is cleared immediately. Pause state (`_paused_by_door`, `_pre_pause_mode`) was already persisted. | [§ Pre-Pause Mode Storage — HA Restart](#pre-pause-mode-storage) |
| What happens to an active grace period when occupancy changes? | The engine has no explicit occupancy-triggered grace cancellation. Grace timers run to expiry regardless of occupancy transitions; occupancy handlers (`handle_occupancy_away`, `handle_occupancy_home`) do not call `_cancel_grace_timers()`. | [§ Occupancy Interaction](#occupancy-interaction) |
| What is the override confirmation delay — how does it work and what does it gate? | A debounce window (default 600 s) between detecting a thermostat mode change and formally accepting it as a manual override. While pending, `apply_classification()` returns early, blocking all HVAC commands. If the mode self-corrects within the window (transient glitch), the event is discarded — no grace period starts. | [§ Override Confirmation Delay](#override-confirmation-delay) |
| What are all the callsites that clear a manual override, and under what conditions? | Seven callsites: three in `_grace_expired()` branches (always clear — intended), two scheduled handlers (bedtime/wakeup — skip if override active after Issue #204 fix), one explicit service call (always clears), and one occupancy handler (away/vacation — clears before setback, fix #220). Every clear is logged at INFO with a `reason=` parameter. | [§ What Clears a Manual Override](#what-clears-a-manual-override) |

---

## Scope

**Files:**
- `custom_components/climate_advisor/automation.py` — all grace and pause logic
- `custom_components/climate_advisor/coordinator.py` — door/window state listeners, debounce timer scheduling, manual override detection during pause

**Line ranges (automation.py):**
- `_is_within_planned_window_period()`: L321–L340
- `handle_door_window_open()`: L1045–L1186
- `handle_all_doors_windows_closed()`: L1188–L1232
- `handle_manual_override_during_pause()`: L1404–L1427
- `resume_from_pause()`: L1429–L1460
- `_start_grace_period()`: L1462–L1548
- `_cancel_grace_timers()`: L1550–L1559
- `_re_pause_for_open_sensor()`: L1561–L1613
- `restore_state()`: L2038–L2079
- `get_serializable_state()`: L2081–L2116

**Line ranges (coordinator.py):**
- `_subscribe_door_window_listeners()`: L744
- `_cancel_all_debounce_timers()`: L900–L914
- `_any_sensor_open()`: L933–L935
- `_async_door_window_changed()`: L1798–L1888
- `_async_thermostat_changed()` (pause-override detection): L1890–L1926

**Out of scope for this spec:**
- Natural ventilation internal logic (see `check_natural_vent_conditions()`, `_re_evaluate_nat_vent()`)
- Fan min-runtime cycles (separate lifecycle from grace)
- Occupancy setback calculations

---

## Grace Period Types

### Manual Grace

**Trigger:** Any of three user-initiated events:
1. User manually changes the thermostat HVAC mode while `_paused_by_door = True` — detected by `_async_thermostat_changed()` in the coordinator; dispatches to `handle_manual_override_during_pause()` which calls `_start_grace_period("manual")`.
2. User manually changes fan state — `handle_fan_manual_override()` calls `_start_grace_period("manual")` directly.
3. User presses "Resume" on the dashboard — `resume_from_pause()` clears the pause and calls `_start_grace_period("manual")`.
4. A manual thermostat override is confirmed after the `CONF_OVERRIDE_CONFIRM_PERIOD` window — `_confirm_override()` calls `_start_grace_period("manual")`.

**Duration:** Configurable via `CONF_MANUAL_GRACE_PERIOD` (`manual_grace_seconds`). Default: `DEFAULT_MANUAL_GRACE_SECONDS = 1800` seconds (30 minutes). A configured value of 0 disables manual grace entirely (timer is not started, `_grace_active` remains `False`).

**What it suppresses:** During active manual grace, `handle_door_window_open()` checks `self._grace_active` early (L1053–L1066). If grace is active AND outdoor temperature is at or above `nat_vent_threshold` (i.e., outdoor is NOT cool enough for natural ventilation), the method returns immediately without pausing HVAC. If outdoor is cool enough for natural ventilation, execution falls through to the nat-vent path — grace does not suppress nat-vent activation.

**Notification:** Off by default (`CONF_MANUAL_GRACE_NOTIFY` defaults to `False`). Configurable to on.

**End condition:** Timer fires → `_grace_expired()` callback executes one of three branches (see [Timer Lifecycle](#timer-lifecycle)).

---

### Automation Grace (Window-Close Grace)

**Trigger:** Any of two Climate Advisor-initiated resumptions:
1. All monitored doors/windows transition to closed — `handle_all_doors_windows_closed()` restores `_pre_pause_mode`, calls `_start_grace_period("automation")` (L1231).
2. Natural ventilation ends because all sensors closed — after fan deactivation and HVAC mode restore, `_start_grace_period("automation")` is called (L1214).

**Duration:** Configurable via `CONF_AUTOMATION_GRACE_PERIOD` (`automation_grace_seconds`). Default: `DEFAULT_AUTOMATION_GRACE_SECONDS = 300` seconds (5 minutes). A configured value of 0 disables automation grace.

**What it suppresses:** Same early-return guard in `handle_door_window_open()` as manual grace. A door or window opening within 5 minutes of HVAC resumption does not re-pause the system (unless nat-vent conditions are met).

**Notification:** On by default (`CONF_AUTOMATION_GRACE_NOTIFY` defaults to `True`). Configurable to off.

**End condition:** Same three-branch expiry callback as manual grace.

---

## State Machine

The grace period state machine is embedded within the broader pause/resume lifecycle. States are represented by the combination of `_paused_by_door` and `_grace_active` flags. There is no named state enum in the code.

| From State | Event | To State | Side Effect |
|---|---|---|---|
| NORMAL (`paused=F, grace=F`) | Door/window opens; stays open past debounce; HVAC was not off | PAUSED (`paused=T, grace=F`) | Store `_pre_pause_mode`; set HVAC off; send door-pause notification |
| NORMAL | Door/window opens; HVAC was already off | NORMAL (unchanged) | `_paused_by_door` set to True but no HVAC call needed; no grace started |
| NORMAL | Door/window opens during planned window period | NORMAL (unchanged) | No pause, no grace; sensor open is expected |
| PAUSED | All sensors close | GRACE (`paused=F, grace=T`) | Restore `_pre_pause_mode` via HVAC service call; restore comfort temp; start automation grace timer |
| PAUSED | User manually changes HVAC mode on thermostat (not automation-initiated) | GRACE (`paused=F, grace=T`) | Clear `_paused_by_door` and `_pre_pause_mode`; start manual grace; cancel all debounce timers |
| PAUSED | User presses "Resume" on dashboard | GRACE (`paused=F, grace=T`) | Clear pause; restore `_current_classification.hvac_mode` (not `_pre_pause_mode`); set `_resumed_from_pause=True`; start manual grace |
| GRACE | New door/window open (outdoor too warm for nat-vent) | GRACE (unchanged) | Suppressed — no re-pause, no new grace timer |
| GRACE | New door/window open (outdoor cool enough for nat-vent) | NAT_VENT (special: `paused=F, grace=T`, `_natural_vent_active=T`) | Falls through grace guard to nat-vent path; HVAC off, fan on |
| GRACE | Grace timer fires; within planned window period | NORMAL (`paused=F, grace=F`) | Clear grace flags silently; call `clear_manual_override()` |
| GRACE | Grace timer fires; sensor still open (`_sensor_check_callback()` returns True) | PAUSED (`paused=T, grace=F`) | Clear grace flags; schedule `_re_pause_for_open_sensor()`; emit `grace_expired` event with `re_paused=True` |
| GRACE | Grace timer fires; all sensors closed | NORMAL (`paused=F, grace=F`) | Clear grace flags; call `clear_manual_override()`; emit `grace_expired` event with `re_paused=False`; send notification if enabled |
| GRACE | `_cancel_grace_timers()` called (e.g., new grace replaces old) | NORMAL (`paused=F, grace=F`) | Cancel active timer; clear `_grace_active`, `_last_resume_source` |
| NORMAL | Fan manual override detected | GRACE (`paused=F, grace=T`) | Start manual grace (fan override path — HVAC pause not involved) |

**Note on concurrent manual and automation timers:** `_start_grace_period()` always calls `_cancel_grace_timers()` first (L1469). This means starting a new grace (of either type) unconditionally cancels any running timer. The engine cannot have both `_manual_grace_cancel` and `_automation_grace_cancel` active simultaneously — the second call to `_start_grace_period()` replaces the first. `_grace_active` therefore reflects the most recently started grace only.

---

## Pre-Pause Mode Storage

**What is stored:** `self._pre_pause_mode` stores the HVAC mode string (e.g., `"heat"`, `"cool"`) as read from `hass.states.get(self.climate_entity).state` at the moment the pause begins (L1164–L1166 in `handle_door_window_open()`).

**Pause guard:** If `_pre_pause_mode` is `None` or `"off"`, `_paused_by_door` is NOT set and HVAC is not touched (L1168). This prevents double-pausing on an already-off thermostat.

**Where stored:** In-memory on the `AutomationEngine` instance as `self._pre_pause_mode`. It is also included in `get_serializable_state()` (L2085) and therefore written to the persisted learning JSON on each state save.

**Restoration on door-close:** `handle_all_doors_windows_closed()` calls `_set_hvac_mode(self._pre_pause_mode, ...)` (L1222–L1224), then sets `_pre_pause_mode = None` (L1232). Comfort temperature is also restored via `_set_temperature_for_mode()`.

**Restoration on dashboard resume:** `resume_from_pause()` does NOT use `_pre_pause_mode`. It uses `_current_classification.hvac_mode` instead (L1449–L1456), because the classification may have changed since the pause was set. `_pre_pause_mode` is set to `None` at L1443.

**HA restart during PAUSED state:** `restore_state()` reads `paused_by_door` and `pre_pause_mode` from the persisted dict (L2044–L2045). Pause state survives restart. Grace timers do NOT survive restart — `_grace_active` is explicitly reset to `False` (L2068). The docstring at L2040–L2042 confirms: "Grace timers are cleared on restart (natural reset point)."

**HA restart during GRACE state (fixed v0.3.56 / #227):** Grace timer is restored. `async_restore_state()` reads `_grace_end_time` and re-schedules `_grace_expired` with remaining duration. If the grace window expired during the restart, override is cleared immediately. Prior to this fix, grace was always discarded on restart — `_manual_override_active` stayed `True` indefinitely.

---

## Invariants

Confirmed from code:

1. **Pre-pause mode is captured before any HVAC service call.** `handle_door_window_open()` reads `state.state` into `_pre_pause_mode` at L1165–L1166 before calling `_set_hvac_mode("off")` at L1172. The stored value reflects the mode that was active, not the mode after HVAC is turned off.

2. **Pause only occurs when HVAC was active.** The guard at L1168 (`if self._pre_pause_mode and self._pre_pause_mode != "off"`) prevents setting `_paused_by_door = True` when HVAC was already off. In that case no service call is made and `_paused_by_door` remains `False`.

3. **`_start_grace_period()` always replaces any running grace.** `_cancel_grace_timers()` is the first call inside `_start_grace_period()` (L1469). Starting grace twice in succession cancels the first timer and begins a fresh one. The two cancel handles (`_manual_grace_cancel`, `_automation_grace_cancel`) are mutually exclusive: each call to `_start_grace_period()` sets only one of them based on `source`.

4. **Grace suppresses new pauses but not natural ventilation.** The early-return in `handle_door_window_open()` at L1053–L1066 exits only when `outdoor >= nat_vent_threshold`. When outdoor is cool enough, execution falls through to the nat-vent evaluation path. Grace does not unconditionally suppress all sensor-open behavior.

5. **Grace timer is restored across HA restarts (fixed v0.3.56 / #227).** `async_restore_state()` reads `_grace_end_time` from the persisted state dict and computes remaining duration. If remaining > 0: `async_call_later(remaining, _grace_expired)` re-schedules the expiry callback. If already expired: `clear_manual_override()` is called immediately. Exception path: `clear_manual_override()` as safety fallback. Prior to this fix, `restore_state()` unconditionally set `_grace_active = False`, leaving `_manual_override_active = True` indefinitely after restart.

6. **Automation grace is always the source after a door-close resume; manual grace is always the source after a user action.** The `source` parameter passed to `_start_grace_period()` is hardcoded at each call site — `"automation"` in `handle_all_doors_windows_closed()` (L1214, L1231) and `"manual"` in `handle_manual_override_during_pause()` (L1426), `resume_from_pause()` (L1459), `handle_fan_manual_override()` (L1426), and `_confirm_override()` (L1651).

7. **Debounce timers are cancelled when a manual override is detected during pause.** `_cancel_all_debounce_timers()` is called in `_async_thermostat_changed()` at L1919, immediately after dispatching to `handle_manual_override_during_pause()`. This prevents orphaned debounce timers from re-triggering a pause after the user has manually resumed HVAC.

8. **`_resumed_from_pause` flag is set only by `resume_from_pause()` and cleared only by `clear_manual_override()`.** It is not set by `handle_all_doors_windows_closed()` (automatic resume) or by `handle_manual_override_during_pause()`. The flag drives the dashboard status string: `"resumed — door/window override"` vs `"grace period (automation|manual)"`.

9. **Invariant NOT confirmed (no code evidence found):** The spec prompt suggested verifying "Manual grace and automation grace cannot both be active simultaneously." The code does not maintain this as a named invariant — it falls out of the `_cancel_grace_timers()` call at the top of `_start_grace_period()`. If something calls `_start_grace_period("manual")` while automation grace is running, `_automation_grace_cancel()` is invoked and `_manual_grace_cancel` is set. The result is exclusive-at-a-time, but only because of the unconditional cancel, not an explicit guard.

---

## Timer Lifecycle

**Start:** `async_call_later(self.hass, duration, _grace_expired)` at L1540. Returns a cancel callable stored in either `_manual_grace_cancel` (source=`"manual"`) or `_automation_grace_cancel` (source=`"automation"`). `_grace_active` is set to `True` before the timer starts (L1481). `_grace_end_time` is set to an ISO timestamp of `now + duration` (L1483).

**Cancel:** `_cancel_grace_timers()` (L1550–L1559) invokes both cancel callables if present, then sets `_grace_active = False` and `_last_resume_source = None`. Called in:
- `_start_grace_period()` — beginning of every new grace (replaces previous timer)
- `cleanup()` — coordinator/engine teardown (L2120)
- Implicitly: any new call to `_start_grace_period()` cancels the previous via `_cancel_grace_timers()` at L1469

**Extend:** There is no explicit "extend" operation. A new `_start_grace_period()` call cancels the previous and starts a fresh full-duration timer. This is the effective extension mechanism, but it is not named or documented as such in code.

**Expiry callback name:** `_grace_expired` (inner closure defined at L1486). The `@callback` decorator marks it as a synchronous HA callback. It executes one of three branches:

| Branch | Condition | Action |
|---|---|---|
| Planned window | `_is_within_planned_window_period()` returns True | Clear grace flags silently; call `clear_manual_override()`; return |
| Re-pause | `_sensor_check_callback()` returns True (sensor still open) | Clear grace flags; call `clear_manual_override()`; schedule `_re_pause_for_open_sensor()` via `async_create_task`; emit `grace_expired` event with `re_paused=True` |
| Normal expiry | All sensors closed or no callback set | Clear grace flags; call `clear_manual_override()`; emit `grace_expired` event with `re_paused=False`; send notification if `should_notify` is True |

**`_re_pause_for_open_sensor()` (L1561–L1613):** Async method scheduled via `async_create_task`. Re-checks planned window period first (if within window, skips re-pause). Otherwise evaluates nat-vent conditions — if outdoor is cool enough for natural ventilation, activates nat-vent mode instead of re-pausing. Falls through to re-pause: captures `state.state` into `_pre_pause_mode`, sets `_paused_by_door = True`, calls HVAC off (unless already off), sends `grace_repause` notification.

**`_sensor_check_callback`:** Set by the coordinator after engine construction. Points to `coordinator._any_sensor_open()` which reads live HA sensor states for all `_resolved_sensors`. If `None` (e.g., in unit tests), the re-pause branch is skipped and grace expires normally.

---

## Pre-Pause Mode Storage

*(See also the invariants section above for the full storage contract.)*

**Serialized fields in `get_serializable_state()`:**
```
"paused_by_door": bool
"pre_pause_mode": str | None
"grace_active": bool
"last_resume_source": str | None
"grace_end_time": str | None  (ISO timestamp — informational, not used on restore)
```

**Fields restored in `restore_state()`:**
```
_paused_by_door   ← "paused_by_door"   (default: False)
_pre_pause_mode   ← "pre_pause_mode"   (default: None)
_grace_active     = False              (always reset)
_last_resume_source = None             (always reset)
```

`_grace_end_time` is NOT restored (stays as `None` after restart). This is consistent — without an active timer, the end time is meaningless.

---

## Occupancy Interaction

**Occupancy mode changes do not directly cancel or alter grace periods.** There are no calls to `_cancel_grace_timers()` or `_start_grace_period()` inside `handle_occupancy_away()`, `handle_occupancy_home()`, or `handle_occupancy_vacation()`. Grace timers run to natural expiry regardless of occupancy state changes during an active grace window.

**Away/vacation mode and pause:** If occupancy transitions to away or vacation while `_paused_by_door = True`, the pause flag persists. The occupancy handlers apply temperature setbacks to the thermostat (via `_set_temperature()`) but do not call `_set_hvac_mode()`, so HVAC remains off as it was during the pause. When sensors close and `handle_all_doors_windows_closed()` fires, `_pre_pause_mode` is restored to whatever mode was active when the pause began — which may now be inconsistent with the setback temp applied by the occupancy handler. There is no reconciliation step between pause restoration and current occupancy mode at the `handle_all_doors_windows_closed()` level. However, `_set_temperature_for_mode()` (called during resume) internally routes through occupancy-aware logic via `_set_temperature_for_mode()` — which checks `_occupancy_mode` and applies setback if away/vacation.

**Away mode and door-open suppression:** Away/vacation mode does not skip the debounce or pause flow. If HVAC is running in heat/cool mode while occupancy is away (e.g., pre-conditioning), a door opening will still trigger the debounce and potentially pause HVAC.

**Grace expiry during occupancy-away:** The `_grace_expired` callback does not read `_occupancy_mode`. All three branches (planned window, re-pause, normal expiry) execute the same logic regardless of current occupancy state.

---

## Error Conditions

### Door/Window Sensor Goes Unavailable During Grace

`_is_sensor_open()` in the coordinator (L916–L924) returns `False` when the sensor state is missing (`hass.states.get()` returns `None`) or the state string is not `"on"` (or `"off"` when polarity is inverted). An unavailable sensor appears closed. If all other sensors are closed and the unavailable sensor was the only one open, `handle_all_doors_windows_closed()` is not triggered by the sensor event (because the sensor never fires a `state_changed` event transitioning to `"off"` — it goes to `"unavailable"`). However, the coordinator's `_async_door_window_changed()` only fires on state change events. If the sensor goes unavailable without a prior close event, the system stays paused indefinitely until either: a manual resume, another sensor closes (triggering all-closed check), or an HA restart (which preserves pause state).

There is no polling path that periodically re-evaluates sensor states during a pause — the check only happens on state-change events and at the grace-expiry recheck.

### HA Restart During Grace

**Fixed in v0.3.56 / #227.** Grace timer is now restored on restart. `async_restore_state()` reads `_grace_end_time` from the persisted state dict and recomputes remaining duration:
- **Remaining > 0:** `async_call_later(remaining, _grace_expired)` re-schedules the callback. `_grace_active` is set to `True`. System resumes grace normally.
- **Already expired:** `clear_manual_override(reason="grace_expired_during_restart")` is called immediately. `_manual_override_active` is cleared; system returns to normal scheduling.
- **Exception:** `clear_manual_override(reason="grace_restore_error")` as safety fallback.

Prior to this fix, grace timer was unconditionally discarded on restart. `_manual_override_active` stayed `True` indefinitely and the dashboard showed "0 min remaining" with no self-resolution path.

### HA Restart During PAUSED State

Pause state (`_paused_by_door = True`, `_pre_pause_mode = "<mode>"`) is persisted and restored. HVAC remains off. The engine re-enters PAUSED state immediately after restart with the correct pre-pause mode ready for restoration.

### Concurrent Door/Window Events (Multiple Sensors)

Each sensor gets its own debounce timer in `_door_open_timers` (keyed by `entity_id`). A second sensor opening while the first is in debounce starts a second independent timer. Both timers can fire and call `handle_door_window_open()` in sequence. The second call is a no-op because `_paused_by_door` is already `True` (early return at L1050–L1051).

On close: `handle_all_doors_windows_closed()` is only called when ALL monitored sensors are closed (L1871: `all_closed = all(not self._is_sensor_open(s) for s in self._resolved_sensors)`). Partial closes do not trigger resume.

### Pre-Pause Mode Is None When Sensor Opens

If `hass.states.get(self.climate_entity)` returns `None` (thermostat entity unavailable), `_pre_pause_mode` stays `None`. The guard at L1168 (`if self._pre_pause_mode and self._pre_pause_mode != "off"`) evaluates to `False`. `_paused_by_door` is NOT set and HVAC is not touched. No notification is sent. The sensor opening is effectively silently ignored in this case.

---

## Override Confirmation Delay

### Purpose

The override confirmation delay is a debounce window between detecting a thermostat HVAC mode change and formally accepting it as a manual override. Its purpose is to discard transient glitches — thermostat restarts, HA restart HVAC echoes, fan cycling — that look identical to intentional user overrides but resolve on their own within a few minutes. Without this window, each transient would trigger a 30-minute manual grace period and prevent the system from responding to weather changes.

### Configuration

| Constant | Config key | Default | Label |
|---|---|---|---|
| `CONF_OVERRIDE_CONFIRM_PERIOD` | `"override_confirm_seconds"` | `600` s (10 min) | "Override Confirmation Delay (minutes)" |

Set `override_confirm_seconds = 0` to confirm overrides immediately — the `_confirm_override()` path is taken synchronously with no pending window.

### Trigger

Detected in `coordinator.py` `_async_thermostat_changed()`. Conditions for triggering:
1. The HVAC mode changed to something other than `"off"`
2. The new mode differs from `classification.hvac_mode` (what the system expects)
3. The change is NOT flagged as automation-initiated (i.e., not a CA service call)

When all three hold, the coordinator dispatches to `automation_engine.handle_manual_override(mode)`, which calls `start_override_confirmation()`.

### State

Three instance variables track the pending confirmation window:

| Variable | Type | Meaning |
|---|---|---|
| `_override_confirm_pending` | `bool` | `True` while a detection is awaiting confirmation |
| `_override_confirm_mode` | `str \| None` | The detected HVAC mode string (e.g., `"cool"`) |
| `_override_confirm_time` | `datetime \| None` | UTC timestamp of when detection occurred |

A cancel callable (`_override_confirm_cancel`) holds the timer handle, like the grace cancel handles.

### Gate on `apply_classification()`

While `_override_confirm_pending` is `True`, `apply_classification()` returns early:

```python
if _override_confirm_pending:
    # "Override confirmation pending (detected=X at T) — skipping HVAC mode change"
    return
```

This blocks ALL downstream classification effects: HVAC mode changes, temperature setpoint commands, occupancy guards, fan logic, window open/close recommendations. The system is effectively paused at the classification boundary while the window runs.

### State Machine

```
[DETECTION] coordinator._async_thermostat_changed()
  Mode ≠ classification.hvac_mode AND not automation-initiated
  └─ handle_manual_override(mode)
       ↓
[PENDING] start_override_confirmation()
  _override_confirm_pending = True
  Event: "override_detected" (dupe-gated 5-min window)
  Timer: CONF_OVERRIDE_CONFIRM_PERIOD (default 600 s)
       ↓ timer fires → _confirm_override_expired()
       ├─ [PATH A: state still divergent from classification]
       │   └─ _confirm_override(current_mode)
       │       _manual_override_active = True
       │       Event: "override_confirmed"
       │       _start_grace_period("manual")      ← see Grace Period Types § Manual Grace
       │       apply_classification() remains blocked for CONF_MANUAL_GRACE_PERIOD (30 min)
       │
       └─ [PATH B: state returned to classification]
           _override_confirm_pending = False
           Event: "override_self_resolved"
           apply_classification() unblocked immediately
           No grace period started
```

**Total blocking time if confirmed:** up to `CONF_OVERRIDE_CONFIRM_PERIOD + CONF_MANUAL_GRACE_PERIOD` = 10 + 30 = **40 minutes** (both configurable).

### Events Emitted

| Event | When | Key Payload Fields |
|---|---|---|
| `override_detected` | Detection occurs (dupe-gated 5-min) | `detected_mode`, `source` |
| `override_confirmed` | PATH A: timer expires, state still divergent | `mode`, `confirm_delay_seconds` |
| `override_self_resolved` | PATH B: timer expires, state resolved | `detected_mode`, `current_mode` |

### Interaction with `clear_manual_override()`

`clear_manual_override()` handles both the pending and active override states:

```python
if _override_confirm_pending:
    _override_confirm_cancel()   # cancels the pending timer
    _override_confirm_pending = False
    _override_confirm_time = None
    _override_confirm_mode = None
```

This means an occupancy transition, a fan override, or a dashboard cancel that calls `clear_manual_override()` will also cancel an in-progress confirmation window. The override is discarded without ever reaching PATH A or PATH B.

### Persistence

`_override_confirm_pending` and its companion variables are **not persisted** across HA restarts. `restore_state()` does not restore them. On restart, any in-flight confirmation window is silently abandoned and the flags reset to `False`. This is intentional — an HA restart is itself a transient event, and the mode state that triggered the confirmation may no longer reflect user intent after restart.

---

## What Clears a Manual Override

`clear_manual_override()` is the single function that deactivates `_manual_override_active` and cancels any pending override confirmation window. Every callsite must be traceable in logs.

### `reason` parameter (post-Issue #204 fix)

`clear_manual_override()` now accepts a `reason: str` keyword argument. Every call logs at INFO level:

```
[climate_advisor] clear_manual_override called — reason=<reason>
```

This makes every clear event attributable in `python tools/ha_logs.py --full` without a lengthy investigation. Before this fix, all four callsites were indistinguishable in logs.

### Callsite inventory

| # | Callsite | Location | Condition | Behaviour after Issue #204 fix |
|---|---|---|---|---|
| 1 | `_grace_expired()` — planned window branch | `automation.py:~1500` | Grace timer fires while within a planned window period | Always clears — intended; override was established before a window period |
| 2 | `_grace_expired()` — re-pause branch | `automation.py:~1510` | Grace timer fires; sensor still open; re-pause scheduled | Always clears — the re-pause re-establishes HVAC-off state; prior override no longer meaningful |
| 3 | `_grace_expired()` — normal expiry branch | `automation.py:~1520` | Grace timer fires; all sensors closed | Always clears — intended; grace window has elapsed normally |
| 4 | `handle_bedtime_setback()` | `automation.py:1926` | Configured `sleep_time` fires | **Skips entirely if `_manual_override_active=True`** — emits `bedtime_setback_skipped` event; logs skip at INFO. Clears only if override is not active. |
| 5 | `handle_morning_wakeup()` | `automation.py:2025` | Configured `wake_time` fires | **Skips entirely if `_manual_override_active=True`** — emits `morning_wakeup_skipped` event; logs skip at INFO. Clears only if override is not active. |
| 6 | `clear_manual_override` HA service call | `automation.py` service handler | User explicitly calls the service from UI or automation | Always clears — explicit user intent |
| 7 | `handle_occupancy_away()` / `handle_occupancy_vacation()` | `automation.py` occupancy handlers | Occupancy transitions to away/vacation while `_manual_override_active=True` | Calls `clear_manual_override(reason="occupancy_away")` / `clear_manual_override(reason="occupancy_vacation")` before applying setback. Fixed in staging (issue #220). Note: `handle_occupancy_home()` does **NOT** call `clear_manual_override()` — returning home restores comfort via `_set_temperature_for_mode()` without touching the override flag. |

### Note — Away/vacation setback and override detection (Issue #221)

Away/vacation setback setpoint changes are guarded by `_is_recent_temp_command(30s)` in the coordinator's setpoint-change detection block (fix #221). When CA issues a setback setpoint command via `handle_occupancy_away()` or `handle_occupancy_vacation()`, `_set_temperature()` records a `_temp_command_time` timestamp. Any thermostat echo arriving within 30 s is suppressed and does not trigger a manual override detection. This prevents the automation's own setback from opening a spurious grace period.

### Invariant

After Issue #204: no scheduled timer (bedtime, wakeup) may call `clear_manual_override()` while `_manual_override_active=True`. Only grace expiry (callsites 1–3) and the explicit service call (callsite 6) may unconditionally clear an active override. All other callsites must check the flag first.
