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

    F[6:30 AM - Wakeup\ndefault] --> G[clear_manual_override]
    G --> H[Restore comfort temp\nheat: comfort_heat\ncool: comfort_cool]

    I[10:30 PM - Bedtime\ndefault] --> J[clear_manual_override]
    J --> K{hvac_mode?}
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
    I -->|No| K{_paused_by_door\nalready True?}
    K -->|Yes| L[Already paused - skip]
    K -->|No| M[Store _pre_pause_mode]
    M --> N{pre_pause_mode != off?}
    N -->|No| O[HVAC already off - skip]
    N -->|Yes| P[_paused_by_door = True\nSet HVAC off\nSend notification]
    B -->|No| Q[Cancel pending debounce\nfor this sensor]
    Q --> R{ALL sensors closed?}
    R -->|No| S[Wait for more sensors]
    R -->|Yes| T{_paused_by_door?}
    T -->|No| U[Nothing to restore]
    T -->|Yes| V[_paused_by_door = False\nRestore _pre_pause_mode\nRestore comfort temp]
    V --> W[Start automation grace\ndefault 3600s]
```

---

## 5. Manual Override Protection

Thermostat state changes are monitored by `_async_thermostat_changed()` in the coordinator.

```mermaid
graph TD
    A[Thermostat state change] --> B{is_paused_by_door\nAND new_state != off?}
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

## 6. Grace Period System

Two grace period types are managed by `_start_grace_period()` in `AutomationEngine`.

```mermaid
graph TD
    A{Grace trigger source?}
    A -->|Manual override| B[Duration: manual_grace_seconds\ndefault 1800s / 30 min]
    A -->|Automation resume| C[Duration: automation_grace_seconds\ndefault 3600s / 60 min]
    B --> D[_grace_active = True\nStart countdown timer]
    C --> D
    D --> E{Timer expires}
    E --> F[_grace_active = False\nclear_manual_override]
    F --> G{should_notify?}
    G -->|Yes| H[Send grace expired notification]
    G -->|No| I[Silent expiry]
    D --> J{While _grace_active = True}
    J --> K[Door open detected]
    K --> L[Skip pause - grace blocks it]
```

---

## 7. Occupancy State Machine

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

## 8. Economizer — Window Cooling on Hot Days

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

## 9. Startup Safety

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

*Last Updated: 2026-03-19*
