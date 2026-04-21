# Climate Advisor — Automation Flowcharts

This document provides visual decision-path references for every major control flow in the Climate Advisor automation engine. Each diagram reflects the actual source code logic in `coordinator.py`, `automation.py`, and `classifier.py`.

For data structures and coordinator internals see [docs/02-ARCHITECTURE-REFERENCE.md](02-ARCHITECTURE-REFERENCE.md).
For temperature formulas and threshold values see [docs/08-COMPUTATION-REFERENCE.md](08-COMPUTATION-REFERENCE.md).

---

## 1. Main Decision Loop (30-Minute Poll)

`_async_update_data()` runs every 30 minutes via `DataUpdateCoordinator`.

```mermaid
graph TD
    A[30-min poll fires] --> B[Re-resolve door/window sensors]
    B --> C[_get_forecast]
    C --> D{Weather entity ready?}
    D -->|No| E{Retries remaining?}
    E -->|Yes| F[Schedule backoff retry\n30s → 60s → 120s → 240s → 480s]
    E -->|No| G[Wait for next poll]
    D -->|Yes| H[classify_day forecast]
    H --> I{_first_run?}
    I -->|Yes| J{HVAC already running?}
    J -->|Yes| K[Set _manual_override_active\ntreat as manual override]
    J -->|No| L[Continue]
    I -->|No| L
    K --> L
    L --> M[apply_classification]
    M --> N{_manual_override_active?}
    N -->|Yes| O[Skip HVAC mode change]
    N -->|No| P[Set HVAC mode and temp]
    O --> Q[Check economizer]
    P --> Q
    Q --> R[Record temp history]
    R --> S[Save state]
    S --> T[Return data dict to sensors]
```

---

## 2. Classification Pipeline

`classify_day()` in `classifier.py` takes a `ForecastSnapshot` and returns a `DayClassification`.

```mermaid
graph TD
    A[ForecastSnapshot in] --> B{today_high >= 85?}
    B -->|Yes| C[day_type = hot\nhvac_mode = cool\npre_condition = True]
    B -->|No| D{today_high >= 75?}
    D -->|Yes| E[day_type = warm\nhvac_mode = off]
    D -->|No| F{today_high >= 60?}
    F -->|Yes| G[day_type = mild\nhvac_mode = off\nwindows_recommended = True]
    F -->|No| H{today_high >= 45?}
    H -->|Yes| I[day_type = cool\nhvac_mode = heat]
    H -->|No| J[day_type = cold\nhvac_mode = heat]
    C --> K["Compute avg_delta = (high_delta + low_delta) / 2"]
    E --> K
    G --> K
    I --> K
    J --> K
    K --> L{avg_delta > 2?}
    L -->|Yes| M[trend = warming]
    L -->|No| N{avg_delta < -2?}
    N -->|Yes| O[trend = cooling]
    N -->|No| P[trend = stable]
    M --> Q[Apply trend modifier]
    O --> Q
    P --> Q
    Q --> R{cooling AND magnitude >= 10?}
    R -->|Yes| S[pre_condition = True\npre_condition_target = +3.0\nsetback_modifier = +3.0]
    R -->|No| T{warming AND magnitude >= 10?}
    T -->|Yes| U[setback_modifier = -3.0]
    T -->|No| V{cooling AND magnitude >= 5?}
    V -->|Yes| W[pre_condition = True\npre_condition_target = +2.0\nsetback_modifier = +2.0]
    V -->|No| X{warming AND magnitude >= 5?}
    X -->|Yes| Y[setback_modifier = -2.0]
    X -->|No| Z[No modifier]
    S --> AA[Return DayClassification]
    U --> AA
    W --> AA
    Y --> AA
    Z --> AA
```

---

## 3. Daily Schedule

Four scheduled events fire each day via `async_track_time_change`.

```mermaid
graph TD
    A[6:00 AM - Briefing\ndefault] --> B[classify_day + apply_classification]
    B --> C[Init DailyRecord for learning]
    C --> D[generate_briefing]
    D --> E[notify push + email]

    F[6:30 AM - Wakeup\ndefault] --> G[clear_manual_override\n+ clear_fan_override]
    G --> H[Deactivate fan\nif still running]
    H --> HH[Restore comfort temp\nheat: comfort_heat\ncool: comfort_cool]

    I[10:30 PM - Bedtime\ndefault] --> J[clear_manual_override\n+ clear_fan_override]
    J --> JJ[Deactivate fan + economizer\nif active]
    JJ --> K{hvac_mode?}
    K -->|heat| L[Set temp:\ncomfort_heat - 4 + setback_modifier]
    K -->|cool| M[Set temp:\ncomfort_cool + 3]

    N[11:59 PM - End of Day] --> O[Compute avg indoor temp]
    O --> P[Flush HVAC runtime]
    P --> Q[learning.record_day]
    Q --> R[Reset: _today_record = None\n_briefing_sent_today = False\nclear temp history]
```

---

## 4. Door/Window Pause Flow

Sensor state changes are handled by `_async_door_window_changed()` in the coordinator, with pause logic in `handle_door_window_open()` and `handle_all_doors_windows_closed()` in the automation engine.

When the system sets HVAC to `off` as part of a pause, it sets `_hvac_command_pending = True` and records `_hvac_command_time` before issuing the service call. This tells the thermostat state change handler (Section 5) that the mode change is system-initiated, not a user action, preventing false manual override detection.

```mermaid
graph TD
    A[Sensor state change] --> B{Sensor is open?}
    B -->|Yes| C{Debounce timer\nalready running?}
    C -->|Yes| D[Ignore - already pending]
    C -->|No| E[Start debounce timer\ndefault 300s]
    E --> F[Debounce expires]
    F --> G{Sensor still open?}
    G -->|No| H[Discard - closed in time]
    G -->|Yes| I{_grace_active?}
    I -->|Yes| J[Skip pause - grace active]
    I -->|No| K{_is_within_planned_window_period?}
    K -->|Yes| L[Skip pause\nlog 'not pausing - windows recommended']
    K -->|No| M{_paused_by_door\nalready True?}
    M -->|Yes| N[Already paused - skip]
    M -->|No| O[Store _pre_pause_mode]
    O --> P{pre_pause_mode != off?}
    P -->|No| Q[HVAC already off - skip]
    P -->|Yes| R[Set _hvac_command_pending = True\nRecord _hvac_command_time]
    R --> S[_paused_by_door = True\nSet HVAC off\nSend notification]
    B -->|No| T[Cancel pending debounce\nfor this sensor]
    T --> U{ALL sensors closed?}
    U -->|No| V[Wait for more sensors]
    U -->|Yes| W{_paused_by_door?}
    W -->|No| X[Nothing to restore]
    W -->|Yes| Y[_paused_by_door = False\nRestore _pre_pause_mode\nRestore comfort temp]
    Y --> Z[Start automation grace\ndefault 300s]
```

### 4b. Grace Expiry with Planned Window Period Check

When a grace period expires and a sensor is still open, `_grace_expired()` calls `_re_pause_for_open_sensor()`. Before re-pausing, the system first checks whether the open sensor is expected (i.e., within the planned window period for the current classification).

```mermaid
graph TD
    A[Grace period expires] --> B{Any contact sensor\nstill open?}
    B -->|No| C[Clear grace - resume normal automation]
    B -->|Yes| D{_is_within_planned_window_period?}
    D -->|Yes| E[Clear grace\nReturn - sensors open as expected\nno re-pause needed]
    D -->|No| F[Re-pause HVAC\n_paused_by_door = True\nSet HVAC off\nSend notification]
```

**Key behavior:** The planned window period check in the grace expiry path prevents an annoying cycle where the system resumes HVAC after a grace period, immediately re-pauses because a window is still open, and then sends a duplicate notification — all during the time window when open windows are the intended state.

### 4a. Resume from Pause (User Action)

Users can click **"Resume HVAC (override pause)"** in the Door/Window Sensors section of the Debug tab. This action is treated as a manual override.

```mermaid
graph TD
    A[User clicks Resume HVAC\noverride pause button] --> B[Clear _paused_by_door\n_pre_pause_mode = None]
    B --> C[Restore classification HVAC mode\ne.g. cool or heat]
    C --> D[Start manual grace period\ndefault 1800s / 30 min]
    D --> E[_manual_override_active = True]
    E --> F[Status: resumed — door/window override]
```

**Key behaviors:**
- Restores the current day classification's recommended HVAC mode (e.g. `cool` on a hot day), not a previously stored state.
- Starts a full manual override grace period (default 30 min). During this window, new contact sensor open events cannot re-pause HVAC.
- IS recorded as a manual override — `apply_classification` will skip HVAC mode changes until the grace expires or a schedule boundary clears it.
- Status string `"resumed — door/window override"` is surfaced in the Current Status pane and sensor entity.

---

## 5. Manual Override Protection

Thermostat state changes are monitored by `_async_thermostat_changed()` in the coordinator.

The `_hvac_command_pending` flag and `_hvac_command_time` timestamp are set by the system before it issues any HVAC service call (pause, resume, classification apply, etc.). When the thermostat state change event arrives, the handler checks this flag first. If it is set, the change is system-initiated and no override is recorded.

```mermaid
graph TD
    A[Thermostat state change] --> AA{_hvac_command_pending\nset recently?}
    AA -->|Yes| AB[System-initiated change\nSkip override detection]
    AA -->|No| B{is_paused_by_door\nAND new_state != off?}
    B -->|Yes| C[handle_manual_override_during_pause]
    C --> D[_paused_by_door = False\n_pre_pause_mode = None]
    D --> E[_manual_override_active = True]
    E --> F[Start manual grace period]
    F --> G[Cancel all debounce timers]
    B -->|No| H{Mode changed AND\nnot already in override AND\nmode != classification hvac_mode?}
    H -->|No| I[No override - track runtime only]
    H -->|Yes| J[handle_manual_override]
    J --> K[_manual_override_active = True\nRecord mode and time]
    K --> L[Start manual grace period]
    L --> M[apply_classification skips\nHVAC mode change until cleared]
    M --> N[Override cleared at:\nWakeup or Bedtime schedule boundary]
```

---

## 6. Fan Override Detection

Fan state changes are monitored by two listeners. A dedicated fan entity listener watches for direct on/off changes. The existing thermostat listener in `_async_thermostat_changed()` also detects `fan_mode` attribute changes. Both paths call `handle_fan_manual_override()` in the automation engine.

Fan override is tracked separately from HVAC override — `_fan_override_active` is independent of `_manual_override_active`. Both can be active simultaneously.

```mermaid
graph TD
    A[Fan entity state change\nfan listener] --> B{New state differs\nfrom previous?}
    B -->|No| C[Ignore - no real change]
    B -->|Yes| D[handle_fan_manual_override]

    E[Thermostat state change\nthermostat listener] --> F{fan_mode attribute\nchanged?}
    F -->|No| G[Continue HVAC override check]
    F -->|Yes| D

    D --> H[_fan_override_active = True\nRecord override time]
    H --> I[Start fan grace period\ndefault manual_grace_seconds]
    I --> J[Fan automation skips\nfan activation until cleared]
    J --> K[Fan override cleared at:\nBedtime or Wakeup schedule boundary\nvia clear_fan_override]

    L[clear_manual_override called\nat schedule boundary] --> M[clear_fan_override]
    M --> N[_fan_override_active = False]
```

---

## 7. Fan Behavior at Schedule Transitions

Fan and economizer state are explicitly managed at the two main daily schedule boundaries: bedtime and morning wakeup. `clear_manual_override()` calls `clear_fan_override()` internally, so both override flags are cleared together at each boundary.

```mermaid
graph TD
    A[10:30 PM - Bedtime fires] --> B[clear_manual_override]
    B --> C[clear_fan_override\n_fan_override_active = False]
    C --> D{Economizer currently active?}
    D -->|Yes| E[_deactivate_economizer\nRestore normal AC mode]
    D -->|No| F{Fan currently running\nvia automation?}
    E --> F
    F -->|Yes| G[Deactivate fan\nSet fan to auto/off]
    F -->|No| H[No fan action needed]
    G --> I[Apply bedtime setback temp]
    H --> I

    J[6:30 AM - Wakeup fires] --> K[clear_manual_override]
    K --> L[clear_fan_override\n_fan_override_active = False]
    L --> M{Fan currently running\nvia automation?}
    M -->|Yes| N[Deactivate fan\nSet fan to auto/off]
    M -->|No| O[No fan action needed]
    N --> P[Restore comfort temp]
    O --> P
```

---

## 8. Grace Period System

Two grace period types are managed by `_start_grace_period()` in `AutomationEngine`.

When any grace period expires, the system re-checks contact sensor state before resuming normal automation. If any contact sensor is still open, HVAC is re-paused immediately rather than blindly restoring normal operation. This prevents the safety issue of running heating or cooling with a door or window open.

```mermaid
graph TD
    A{Grace trigger source?}
    A -->|Manual override| B[Duration: manual_grace_seconds\ndefault 1800s / 30 min]
    A -->|Automation resume| C[Duration: automation_grace_seconds\ndefault 300s / 5 min]
    B --> D[_grace_active = True\nStart countdown timer]
    C --> D
    D --> E{Timer expires}
    E --> F[_grace_active = False\nclear_manual_override]
    F --> G{should_notify?}
    G -->|Yes| H[Send grace expired notification]
    G -->|No| I[Silent expiry]
    F --> J{Any contact sensor\nstill open?}
    J -->|Yes| K[Re-pause HVAC\n_paused_by_door = True\nSet HVAC off]
    J -->|No| L[Resume normal automation]
    D --> M{While _grace_active = True}
    M --> N[Door open detected]
    N --> O[Skip pause - grace blocks it]
```

---

## 9. Occupancy State Machine

Four occupancy states with priority resolution via `_compute_occupancy_mode()` in the coordinator.

```mermaid
stateDiagram-v2
    [*] --> Home

    Home --> Away: home_toggle OFF
    Home --> Vacation: vacation_toggle ON
    Home --> Guest: guest_toggle ON

    Away --> Home: home_toggle ON
    Away --> Vacation: vacation_toggle ON
    Away --> Guest: guest_toggle ON

    Vacation --> Home: vacation_toggle OFF\n+ home_toggle ON
    Vacation --> Away: vacation_toggle OFF\n+ home_toggle OFF
    Vacation --> Guest: guest_toggle ON

    Guest --> Home: guest_toggle OFF\n+ home_toggle ON
    Guest --> Away: guest_toggle OFF\n+ home_toggle OFF
    Guest --> Vacation: guest_toggle OFF\n+ vacation_toggle ON

    note right of Guest
        Highest priority
        Overrides all other states
        Uses handle_occupancy_home handler
    end note

    note right of Vacation
        Deep setback:
        heat = setback_heat + modifier - 3
        cool = setback_cool - modifier + 3
    end note

    note right of Away
        Standard setback:
        heat = setback_heat + modifier
        cool = setback_cool - modifier
    end note

    note right of Home
        Comfort temps restored:
        handle_occupancy_home handler
    end note
```

---

## 10. Economizer — Window Cooling on Hot Days

`check_window_cooling_opportunity()` in `AutomationEngine` implements a two-phase window cooling strategy.

```mermaid
graph TD
    A[check_window_cooling_opportunity called] --> B{day_type == hot?}
    B -->|No| C{Was economizer active?}
    C -->|Yes| D[_deactivate_economizer\nRestore normal AC]
    C -->|No| E[Return False]
    D --> E
    B -->|Yes| F{windows_physically_open\nAND outdoor <= comfort_cool + 3\nAND in time window?}
    F -->|No| G{Was economizer active?}
    G -->|Yes| H[_deactivate_economizer]
    G -->|No| I[Return False]
    H --> I
    F -->|Yes| J[_economizer_active = True]
    J --> K{aggressive_savings\nenabled?}
    K -->|Yes| L[Phase: maintain\nSet HVAC off\nActivate fan\nVentilation only]
    K -->|No| M{indoor_temp > comfort_cool?}
    M -->|Yes| N[Phase: cool-down\nSet HVAC cool\nAC runs with outdoor assist\nTarget = comfort_cool]
    M -->|No| O[Phase: maintain\nSet HVAC off\nActivate fan\nNatural ventilation holds temp]
    L --> P[Return True - economizer active]
    N --> P
    O --> P
```

Time window check: morning 6:00–9:00 AM or evening 5:00 PM–midnight.

---

## 11. Startup Safety

First-run logic and weather entity backoff handled in `_async_update_data()`.

```mermaid
graph TD
    A[Integration loads] --> B[async_restore_state\nload persisted state from disk]
    B --> C[async_setup\nregister listeners and schedules]
    C --> D[First 30-min poll fires\n_first_run = True]
    D --> E[_get_forecast called]
    E --> F{Weather entity\navailable?}
    F -->|No| G{_startup_retries_remaining > 0?}
    G -->|Yes| H[Schedule retry\n30s → 60s → 120s → 240s → 480s\nDecrement retry counter]
    G -->|No| I[Log warning\nWait for next scheduled poll]
    F -->|Yes| J[classify_day succeeds\n_first_run = False\nReset retry counters]
    J --> K{HVAC currently running?}
    K -->|Yes| L[Set _manual_override_active\nPreserve current HVAC state\nDo not apply classification]
    K -->|No| M[apply_classification normally]
```

---

## 12. Natural Ventilation Decision Flow

`check_natural_vent_conditions()` in `AutomationEngine` implements continuous monitoring. Initial activation fires from `handle_door_window_open()` when a contact sensor opens and the system is not already in a planned window period or grace period.

### 12a. Activation on Sensor Open

When a contact sensor opens (after debounce), the automation engine checks whether natural ventilation conditions are met before deciding how to respond.

```mermaid
flowchart TD
    A[Contact sensor opens\nafter debounce] --> B{Grace period active\nOR outdoor > ceiling threshold?}
    B -->|Yes| C[Skip nat vent check\nGrace or ceiling blocks activation]
    B -->|No| D{Planned window period\nactive for this classification?}
    D -->|Yes| E[Skip — sensor open is expected\nno HVAC action]
    D -->|No| F{outdoor < indoor\nAND indoor > comfort_heat\nAND outdoor < comfort_cool + delta?}
    F -->|Yes| G[Activate natural ventilation\nHVAC off · fan on\n_natural_vent_active = True]
    F -->|No| H[Enter paused state\nHVAC off · fan off\nWait for conditions to improve]
```

### 12b. Continuous Monitoring (`check_natural_vent_conditions`)

This check runs on every coordinator update while nat vent is active or paused. Exit conditions are evaluated in priority order; the first match wins.

```mermaid
flowchart TD
    A{State?} -->|Neither active nor paused| Z[Return — no action]
    A -->|Natural vent active| B{All sensors closed?}
    B -->|Yes| C[Exit nat vent\nResume HVAC from classification]
    B -->|No| D{indoor ≤ comfort_heat?}
    D -->|Yes| E[nat_vent_comfort_floor_exit\nRestore heat at comfort_heat]
    D -->|No| F{outdoor ≥ indoor?}
    F -->|Yes| G[nat_vent_outdoor_rise_exit\nFan off · enter paused state\nStart 300s lockout timer]
    F -->|No| H{outdoor > comfort_cool + delta?}
    H -->|Yes| I[Fan off · enter paused state]
    H -->|No| J[Continue natural ventilation]
    A -->|Paused| K{All sensors closed?}
    K -->|Yes| L[Exit paused state\nResume HVAC from classification]
    K -->|No| M{300s lockout elapsed?\nAND outdoor < indoor - 1°F?\nAND outdoor < comfort_cool + delta?}
    M -->|All yes| N[Re-activate natural ventilation\nFan on · _natural_vent_active = True]
    M -->|Any no| O[Stay paused\nRe-check next coordinator update]
```

**Hysteresis note:** The 1°F gap in the re-activation check (`outdoor < indoor - 1°F`) and the 300-second lockout timer together prevent rapid oscillation when outdoor and indoor temperatures are near equilibrium. Without both guards, a small thermal fluctuation could toggle nat vent on and off multiple times within a single hour.

---

*Last Updated: 2026-04-20*
