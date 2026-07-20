<!-- Nav: ← [Automation Flowchart](07-AUTOMATION-FLOWCHART.md) | → [automation.py#L308](../custom_components/climate_advisor/automation.py#L308) [coordinator.py#L777](../custom_components/climate_advisor/coordinator.py#L777) | ↔ [Grace Periods](grace-periods-spec.md) [Thermal Model v3](thermal-model-v3-spec.md) [State Persistence](state-persistence.md) -->

# Occupancy Dispatch — Territory Spec (Tier 3)

## Anchors

| Question | Short answer | Full answer |
|---|---|---|
| What is the priority order when multiple toggles are ON simultaneously? | GUEST beats VACATION beats HOME/AWAY. Guest+vacation ON simultaneously resolves to GUEST (full comfort, no deep setback). | [Priority Resolution](#priority-resolution) |
| What triggers the 15-minute grace timer and what cancels it? | Any transition into AWAY mode starts the timer. Any subsequent toggle change to HOME, GUEST, or VACATION cancels it before setback fires. | [State Transitions](#state-transitions) |
| Does `handle_occupancy_home()` clear the manual override flag? | No. `handle_occupancy_home()` restores comfort temperatures but does not clear `_manual_override_active`. Override is cleared only at bedtime and morning wakeup. | [Manual Override Interaction](#manual-override-interaction) |
| What setback temperature does VACATION mode apply vs AWAY? | VACATION adds an extra 3°F beyond AWAY: heat target drops an additional `VACATION_SETBACK_EXTRA`, cool target rises an additional `VACATION_SETBACK_EXTRA`. | [Setback Temperature Formulas](#setback-temperature-formulas) |
| What happens to occupancy state if HA restarts mid-away-grace-period? | `occupancy_mode` is restored from `climate_advisor_state.json`; `occupancy_away_since` is also restored. However, the 15-minute grace timer is NOT re-armed on restart — the mode is synced to the engine via `set_occupancy_mode()` but the away timer callback is lost. | [Persistence](#persistence) |
| What does `_set_temperature_for_mode()` do when called in AWAY or VACATION mode? | It is a safety net: it intercepts the call and redirects to `handle_occupancy_away()` or `handle_occupancy_vacation()` respectively, so no comfort temperature is ever applied while away. | [_set_temperature_for_mode() Safety Net](#_set_temperature_for_mode-safety-net) |

## Scope

This spec covers the occupancy toggle listener, mode priority resolution, state-machine transitions, automation handlers, and all occupancy-aware guards across both coordinator and automation engine.

- **Files:**
  - `custom_components/climate_advisor/coordinator.py` — toggle listener, priority resolver, away timer, state sync
  - `custom_components/climate_advisor/automation.py` — occupancy handlers, `_set_temperature_for_mode()` safety net, `apply_classification()` guards, bedtime/wakeup guards, `set_occupancy_mode()`
  - `custom_components/climate_advisor/const.py` — mode constants, `OCCUPANCY_SETBACK_MINUTES`, `VACATION_SETBACK_EXTRA`
  - `custom_components/climate_advisor/state.py` — `STATE_VERSION`, `StatePersistence` (state file I/O)
- **Approximate line ranges:**
  - `coordinator.py`: L763–L896 (toggle listener + priority resolver + away timer)
  - `coordinator.py`: L287–L290 (instance variables), L525–L532 (state restore), L600–L601 (state persist)
  - `automation.py`: L1432 (`apply_classification`, DEFER_OCCUPANCY guard ~L1513), L2249 (`_set_temperature_for_mode` safety net), L3852 (`handle_occupancy_away`), L3899 (`handle_occupancy_home`), L3951 (`handle_occupancy_vacation`), L4001 (`handle_bedtime`, DEFER_OCCUPANCY guard ~L4033), L4149 (`handle_pre_cool`, DEFER_OCCUPANCY guard ~L4199), L4324 (`handle_morning_wakeup`). Line numbers drift with unrelated commits — treat as approximate; verify with `grep -n "async def " automation.py` before citing.
  - `const.py`: L130, L155–L161 (constants)

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

`_compute_occupancy_mode()` at `coordinator.py:L777`. Returns one of the four mode strings. Evaluated on every toggle state change (not cached between calls).

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

**`_is_toggle_on(entity_id, invert)` behavior** (`coordinator.py:L763`):

- Reads entity state from `hass.states.get(entity_id)`.
- States `"unavailable"` and `"unknown"` are treated as `False` (OFF). No exception is raised.
- Invert is applied as a boolean XOR: `result = raw_state XOR invert`. A configured invert flag on an unavailable entity still resolves to `False` (unavailable is never treated as ON even after inversion).

Toggle entities may be any binary sensor or input boolean. The integration subscribes to state-change events for each configured toggle entity at setup time (`coordinator.py:L810`).

## State Transitions

### Flow of `_async_occupancy_toggle_changed` (`coordinator.py:L827`)

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
| Any | HA restart | (restored) | — | mode read from `climate_advisor_state.json`; engine synced; away timer NOT re-armed |

`_cancel_occupancy_away_timer()` (`coordinator.py:L820`): calls the stored cancel handle and sets `_occupancy_away_timer_cancel = None`. Safe to call when no timer is running.

## Handlers

### `handle_occupancy_home()` (`automation.py:L1650`)

Invoked for both HOME and GUEST modes (coordinator routes GUEST through this same handler).

**Sequence:**

1. Sets `self._occupancy_mode = OCCUPANCY_HOME`
2. If no current classification (`_current_classification` is None): return (no HVAC action)
3. If `c.hvac_mode` is `"heat"` or `"cool"`: call `_set_temperature_for_mode()` to restore comfort temperature
4. **Notification suppression check 1 — proximity:** if indoor temp is already closer to comfort than to setback (`abs(indoor - comfort) < abs(indoor - setback)`), record notification timestamp and return without sending
5. **Notification suppression check 2 — debounce:** if a welcome-home notification was sent within `CONF_WELCOME_HOME_DEBOUNCE` seconds, return without sending
6. Record `_last_welcome_home_notified = now`
7. Send notification: `"Welcome home! Restoring comfort temperature. Should feel normal in about 20–30 minutes."` via `_notify(..., notification_type="occupancy_home")`

**Does NOT clear `_manual_override_active`.** If a manual override is active when the user returns, comfort restore via `_set_temperature_for_mode()` still runs (override flag is only checked in `apply_classification()`).

### `handle_occupancy_away()` (`automation.py:L1615`)

Invoked after the 15-minute grace timer expires, and also called by `apply_classification()` when occupancy is AWAY on the 30-minute poll cycle.

**Sequence:**

1. Sets `self._occupancy_mode = OCCUPANCY_AWAY`
2. If no current classification: log WARNING and return
3. Apply setback via `_set_temperature()` (bypasses the `_set_temperature_for_mode()` safety net — direct call):
   - Heat mode: `setback_heat + c.setback_modifier`
   - Cool mode: `setback_cool - c.setback_modifier`
   - Any other HVAC mode (off, fan only): log info, no temperature change
4. **No notification sent.**

### `handle_occupancy_vacation()` (`automation.py:3951`)

Invoked immediately (no grace) when VACATION mode is detected.

**Sequence:**

1. Sets `self._occupancy_mode = OCCUPANCY_VACATION`
2. If no current classification: return
3. Apply deep setback via `_set_temperature()`:
   - Heat mode: `setback_heat + c.setback_modifier - VACATION_SETBACK_EXTRA` (3°F below AWAY setback)
   - Cool mode: `setback_cool - c.setback_modifier + VACATION_SETBACK_EXTRA` (3°F above AWAY setback)
4. **No notification sent.**

## Setback Temperature Formulas

| Mode | Heat setpoint | Cool setpoint |
|---|---|---|
| HOME / GUEST | `comfort_heat` | `comfort_cool` |
| AWAY | `setback_heat + c.setback_modifier` | `setback_cool - c.setback_modifier` |
| VACATION | `setback_heat + c.setback_modifier - 3` | `setback_cool - c.setback_modifier + 3` |

`c.setback_modifier` comes from the `DayClassification` (e.g., trend days adjust the setback depth). `VACATION_SETBACK_EXTRA = 3` (°F, defined in `const.py:L161`).

**Comfort-band definition:** `comfort_heat` is the lower bound; `comfort_cool` is the upper bound. In-band condition: `comfort_heat ≤ T ≤ comfort_cool`.

## 30-Minute Poll Guards (`apply_classification`)

`apply_classification()` (`automation.py:1432`) runs every 30 minutes when the coordinator refreshes, and also on grace-period expiry outside the sleep window (via `_apply_current_scheduled_state()`) and on manual-override cancellation (via the dashboard's Cancel Override button, ~10s delayed). Occupancy guards execute **after** the manual override check and **after** the override-confirm-pending check, then route through the shared `desired_state.decide_scheduled_band_gate()` (Issue #498).

**Evaluation order inside `apply_classification()`:**

1. **Manual override active** → log and return. Override wins; occupancy is not evaluated.
2. **Override confirm pending** → log and return.
3. **Gate resolves `DEFER_OCCUPANCY`** (occupancy is AWAY or VACATION):
   - **AWAY** → call `handle_occupancy_away()` to actively reapply the setback band, then return.
   - **VACATION** → call `handle_occupancy_vacation()` to actively reapply the deep setback band, then return. *(Issue #505: prior to this fix, VACATION just logged "deep setback preserved" and returned without ever re-arming the band — this was an original design gap from Issue #85, not caught by any existing test, that let vacation's setback go unenforced for the rest of a real trip once anything — most commonly a manual override — moved the thermostat off it. `handle_occupancy_vacation()` already existed and already did the correct thing; it was simply never called from this path.)*
4. **HOME or GUEST** → proceed to normal comfort application (never reaches the occupancy gate — `decide_scheduled_band_gate()` only routes AWAY/VACATION through `DEFER_OCCUPANCY`).

The manual-override-first ordering means: if both `_manual_override_active` and `_occupancy_mode == VACATION` are true, the override check fires first and the function returns before the occupancy gate is ever evaluated. Once the override later clears (confirm or cancel), the next `apply_classification()` pass reaches the gate and actively reapplies the correct setback — it does not merely assume it's already there.

## Bedtime and Wakeup Guards

**`handle_bedtime()` (`automation.py:4001`):**

- If the gate resolves `DEFER_OCCUPANCY` (VACATION or AWAY): bedtime-specific sleep temps are still skipped (applying them would move the thermostat in the wrong direction), but as of Issue #505 the away/vacation setback is now **actively reapplied** here too — `handle_occupancy_vacation()`/`handle_occupancy_away()` is called before returning, exactly mirroring `apply_classification()`'s branch above. This matters because grace-period expiry landing *inside* the sleep window routes here instead of to `apply_classification()` (see `_apply_current_scheduled_state()`), so the same "setback already active" assumption would otherwise be exposed to the same staleness bug on a narrower (≤30 min, until the next backstop cycle) but still real window.
- For HOME/GUEST: clears manual override, deactivates fan if running, applies adaptive bedtime setback via `compute_bedtime_setback()`.

**`handle_morning_wakeup()` (`automation.py:4324`):**

- If `_occupancy_mode` is NOT `OCCUPANCY_HOME` or `OCCUPANCY_GUEST`: log info and return. No comfort restore while away — this path is unaffected by Issue #505 (a silent no-op here is correct; the away/vacation setback is the intended active state and gets actively reconfirmed elsewhere, not restored to comfort).
- For HOME/GUEST: clears manual override, deactivates fan if still running, restores comfort temperatures.

**`handle_pre_cool()`:** has the same `DEFER_OCCUPANCY` shape as `handle_bedtime()` and received the identical Issue #505 fix — reapplies the away/vacation setback before returning "skipped" rather than assuming it's already active.

## `_set_temperature_for_mode()` Safety Net

`_set_temperature_for_mode()` (`automation.py:2249`) is the common comfort-application path called by most HVAC-setting code within the engine. It acts as a last-resort occupancy guard:

1. If `_occupancy_mode == OCCUPANCY_AWAY`: log info, `await handle_occupancy_away()`, return.
2. If `_occupancy_mode == OCCUPANCY_VACATION`: log info, `await handle_occupancy_vacation()`, return.
3. Otherwise (HOME/GUEST): apply `comfort_heat` or `comfort_cool` (plus any pre-condition offsets).

**Purpose:** Any code path that calls `_set_temperature_for_mode()` is automatically protected — even if the caller does not check occupancy. Direct calls to `_set_temperature()` (the lower-level setter) bypass this safety net. The away and vacation handlers themselves use `_set_temperature()` directly to avoid infinite recursion.

## Persistence

**State file:** `climate_advisor_state.json` in the HA config root (constant `STATE_FILE` in `const.py:L199`). Written atomically via `StatePersistence` (`state.py`) using a `.tmp` file and `os.replace()`.

**Keys written (`coordinator.py:L600–L601`):**

| Key | Type | Value when away timer pending |
|---|---|---|
| `"occupancy_mode"` | `str` | Current mode string (e.g., `"away"`) |
| `"occupancy_away_since"` | `str` \| `null` | ISO datetime string of departure; `null` if home |

**Restore sequence (`coordinator.py:L525–L532`):**

1. Read `state.get("occupancy_mode", OCCUPANCY_HOME)` → `self._occupancy_mode`
2. Call `automation_engine.set_occupancy_mode(self._occupancy_mode)` to sync engine
3. Parse `state.get("occupancy_away_since")` → `self._occupancy_away_since` (as `datetime`) or `None`

**Version mismatch:** `STATE_VERSION = 1` (defined in `state.py:L21`). If the file contains a different version (or is missing/corrupt), `StatePersistence.load()` returns an empty dict and logs a WARNING. The coordinator then defaults `occupancy_mode` to `OCCUPANCY_HOME`.

**Away timer is NOT persisted.** If HA restarts while the 15-minute away timer is running, the timer callback is lost. The persisted `occupancy_mode` will be `"away"` and `occupancy_away_since` will reflect the departure time, but setback will not fire until the next toggle state change re-triggers `_async_occupancy_toggle_changed`. The 30-minute `apply_classification()` poll will reapply setback on its next cycle if the mode is still AWAY at that time.

## Manual Override Interaction

**Override flag (`_manual_override_active`)** is checked before all occupancy logic in `apply_classification()`. If both are active:

- Override wins at the `apply_classification()` entry point.
- Occupancy mode is still tracked and synced correctly — the override only suppresses temperature changes, not mode bookkeeping.

**What clears the override flag:**

| Event | Clears `_manual_override_active` |
|---|---|
| `handle_bedtime()` (HOME/GUEST only) | Yes — calls `clear_manual_override()` |
| `handle_morning_wakeup()` (HOME/GUEST only) | Yes — calls `clear_manual_override()` |
| `handle_occupancy_home()` | **No** |
| `handle_occupancy_away()` | **No** |
| `handle_occupancy_vacation()` | **No** |
| Toggle entity state change | **No** |

**No direct occupancy override mechanism exists.** Occupancy mode is determined solely by the toggle entity states evaluated in `_compute_occupancy_mode()`. There is no API or service call to force-set the occupancy mode independently of the toggle entities.

## Invariants

1. `_occupancy_mode` always contains one of the four valid strings: `"home"`, `"away"`, `"vacation"`, `"guest"`. The `set_occupancy_mode()` validator (`automation.py:L308–L315`) enforces this — invalid values log a WARNING and default to `OCCUPANCY_HOME`.
2. The coordinator's `self._occupancy_mode` and the engine's `_occupancy_mode` are always in sync at the end of any transition. `set_occupancy_mode()` is called in the same transaction as the coordinator's own assignment (`coordinator.py:L864–L867`).
3. At most one away timer is pending at any time. `_cancel_occupancy_away_timer()` is always called before starting a new timer, and before dispatching VACATION, HOME, or GUEST handlers.
4. `handle_occupancy_away()` is never called directly by the coordinator — it is always routed through either the timer callback or `apply_classification()`. This preserves the 15-minute grace guarantee.
5. `handle_occupancy_vacation()` is always called immediately on the vacation toggle (no grace). There is no equivalent grace timer for vacation mode. As of Issue #505, it is also called from `apply_classification()`/`handle_bedtime()`/`handle_pre_cool()`'s `DEFER_OCCUPANCY` branches on every subsequent cycle while vacation mode is active — not just once at toggle time.
6. GUEST mode is functionally identical to HOME at the automation handler level. The distinction is tracked in `_occupancy_mode` for reporting and briefing, but `handle_occupancy_home()` is the only handler invoked for both.
7. Away duration (`_today_record.occupancy_away_minutes`) is only incremented on return from a non-home mode. If HA restarts mid-departure, the duration from `_occupancy_away_since` to restart is not counted (timer is not re-armed, so the next toggle change that returns to HOME accumulates from `_occupancy_away_since` which was persisted).
8. The `_set_temperature_for_mode()` safety net cannot recurse: away and vacation handlers call `_set_temperature()` directly, not `_set_temperature_for_mode()`.

## Error Conditions

| Failure | Handling | Observable effect |
|---|---|---|
| Toggle entity `unavailable` or `unknown` | `_is_toggle_on()` treats it as `False` (OFF); no exception raised | Entity effectively reads as "not toggled on"; may hold incorrect mode if the sensor is persistently unavailable |
| HA restart while 15-minute away timer is pending | Timer callback lost; `occupancy_mode = "away"` and `occupancy_away_since` are restored from state file; timer is NOT re-armed | Setback does not fire automatically; fires on next `apply_classification()` 30-minute cycle or next toggle change |
| `set_occupancy_mode()` receives an unrecognized mode string | Logs WARNING at `automation.py:L311`; defaults to `OCCUPANCY_HOME` | Engine remains in HOME mode; coordinator and engine may briefly be out of sync if coordinator accepted the invalid mode |
| State file missing, corrupt, or wrong `STATE_VERSION` | `StatePersistence.load()` returns `{}` and logs WARNING; coordinator defaults to `OCCUPANCY_HOME` | Occupancy state is reset to HOME on restart; `_occupancy_away_since` is cleared |
| No day classification available when handler fires | `handle_occupancy_away()` logs WARNING and returns; `handle_occupancy_home()` and `handle_occupancy_vacation()` return silently | No HVAC temperature change; setback or comfort restore is deferred until `apply_classification()` runs next |
| Both `guest_toggle` and `vacation_toggle` entities ON simultaneously | `_compute_occupancy_mode()` returns `OCCUPANCY_GUEST` (GUEST has higher priority) | Full comfort applied; deep vacation setback is not used; intentional by design |

## Code Reference

- [`set_occupancy_mode`](../custom_components/climate_advisor/automation.py#L308) — validates and sets engine's occupancy mode
- [`apply_classification` (occupancy guards)](../custom_components/climate_advisor/automation.py#L653) — override-first, then vacation early return, then away redirect
- [`_set_temperature_for_mode`](../custom_components/climate_advisor/automation.py#L925) — comfort-application safety net
- [`handle_occupancy_away`](../custom_components/climate_advisor/automation.py#L1615) — away setback handler
- [`handle_occupancy_home`](../custom_components/climate_advisor/automation.py#L1650) — home/guest comfort restore + notification
- [`handle_occupancy_vacation`](../custom_components/climate_advisor/automation.py#L3951) — deep vacation setback handler
- [`handle_bedtime` (occupancy guard)](../custom_components/climate_advisor/automation.py#L1727) — skips sleep setback when away/vacation
- [`handle_morning_wakeup` (occupancy guard)](../custom_components/climate_advisor/automation.py#L1768) — skips comfort restore when not home/guest
- [`_is_toggle_on`](../custom_components/climate_advisor/coordinator.py#L763) — reads toggle entity state with unavailable=OFF and XOR invert
- [`_compute_occupancy_mode`](../custom_components/climate_advisor/coordinator.py#L777) — priority resolver (GUEST > VACATION > HOME/AWAY > default)
- [`_cancel_occupancy_away_timer`](../custom_components/climate_advisor/coordinator.py#L820) — safe timer cancellation
- [`_async_occupancy_toggle_changed`](../custom_components/climate_advisor/coordinator.py#L827) — toggle event listener and dispatch orchestrator
- [`OCCUPANCY_HOME/AWAY/VACATION/GUEST`](../custom_components/climate_advisor/const.py#L155) — mode constants
- [`OCCUPANCY_SETBACK_MINUTES`, `VACATION_SETBACK_EXTRA`](../custom_components/climate_advisor/const.py#L130) — timing and depth constants
