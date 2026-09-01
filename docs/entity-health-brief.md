<!-- Nav: <- Context: [Architecture Reference](02-ARCHITECTURE-REFERENCE.md) -> Detail: entity_health.py (source) | Tier 3 spec: not yet written -->

# Entity Health — Architecture Brief (Tier 2)

## Anchors

| Question | Short answer | -> Full answer |
|---|---|---|
| What does `entity_health.py` own and what does it explicitly not own? | It owns the declarative registry of monitored config keys and the pure `run_entity_health_sweep()` function that checks each one's current HA state. It does NOT own debounce/transition tracking, notification, or status-card surfacing — those live in `coordinator.py`. | [Scope](#scope) |
| Which entities does it monitor, and why isn't each one its own hand-written check? | 11 config keys (thermostat, weather source, notify service, indoor/outdoor temp sensors, fan entities, occupancy toggles) plus `door_window_sensors` (a list, swept per member). One generic sweep, not 11 bespoke functions, because every check is the same operation ("does this configured entity currently resolve?") — see the module's own docstring for why this deliberately diverges from `invariant_watchdog.py`'s flat-list shape. | [Registry](#registry) |
| How does the coordinator turn a raw sweep result into a user notification? | `_run_entity_health_check()` calls the sweep every update cycle, then `_process_entity_health_transitions()` diffs the result against `self._entity_health_state` (transient, in-memory) to detect ok->missing transitions, batches everything that needs notifying that cycle, and `_notify_entity_health_issues()` sends one notification via the existing `AutomationEngine._notify()`. | [Coordinator Interface](#coordinator-interface) |
| How often does a still-missing entity re-notify? | Once on the ok->missing transition, then at most once per `_ENTITY_HEALTH_REMINDER_SECONDS` (24h) while it stays missing — never every 30-min cycle, which is the exact failure mode Issue #805 was filed over (a different bug's log showed hours of unrelated per-cycle incidents with no entity-health signal at all, motivating this debounce shape here). | [Debounce](#debounce) |
| Why is nothing notified during the startup-coalesce window? | `_run_entity_health_check()` returns `[]` immediately while `self._startup_coalesce_active` is `True`, so entities that simply haven't loaded yet at HA boot never false-positive — the same window `_compute_automation_status()` already uses to suppress alarm-shaped states for the same race condition. | [Startup Suppression](#startup-suppression) |
| Where does this show up to the user? | One batched push/email notification (`notification_type="entity_health"`, gated by `push_entity_health`/`email_entity_health` config toggles) plus a line on the existing Status card (`coordinator.data["entity_health_issues"]` -> `api.py` -> `index.html`'s `loadStatus()`) — no new card, no HA Repairs issue. | [User-Visible Surface](#user-visible-surface) |

## Scope

**Owns (`entity_health.py`):**
- `ENTITY_HEALTH_REGISTRY` — the declarative source of truth for what gets swept, its friendly name, criticality tier, and relevance gate (e.g. `outdoor_temp_entity` only matters when `outdoor_temp_source` is a dedicated sensor; `fan_entity`/`fan_state_entity` only matter when `fan_mode` uses a whole-house fan)
- `run_entity_health_sweep(hass, config)` — one pass over the registry plus `door_window_sensors`, returning a list of `EntityHealthIssue`
- The blank-value-never-flagged rule that structurally enforces "optional by design" (an unset `fan_remote_entity` or empty `door_window_sensors` list is never an issue)

**Owns (`coordinator.py`):**
- `_run_entity_health_check()` — the per-cycle entry point, isolated in its own try/except so a bug in the sweep can never abort the update cycle it runs inside
- `_process_entity_health_transitions()` — the ok/missing/recovered state machine and 24h reminder timer
- `_notify_entity_health_issues()` — unconditional per-issue logging (ERROR for critical, WARNING otherwise) plus one batched `_notify()` call

**Explicitly does NOT own:**
- Any automation/control-logic behavior. `_is_sensor_open()` still treats a missing door/window sensor as closed; `_is_toggle_on()`'s existing per-cycle WARNING is untouched. This module detects and notifies — it never changes what the automation engine does with a degraded reading. Changing that behavior (e.g. failing safe on a missing door sensor) is a distinct, further-reaching change with its own golden-scenario testing burden, deliberately out of scope here.
- HA Repairs issues or fix-flows. Unlike `weather_entity_not_found` (see [Repairs Brief](repairs-brief.md)), entity health uses notification only — the project owner explicitly rejected a repair-flow-per-entity pattern as unneeded complexity for Issue #805.

## Registry

```python
ENTITY_HEALTH_REGISTRY: dict[str, dict[str, Any]] = {
    "climate_entity": {"friendly_name": "Thermostat", "criticality": "critical"},
    "weather_entity": {"friendly_name": "Weather source", "criticality": "critical"},
    "notify_service": {"friendly_name": "Notification service", "criticality": "critical"},
    "outdoor_temp_entity": {
        "friendly_name": "Outdoor temperature sensor",
        "criticality": "degraded",
    },  # gated: source == sensor/input_number
    "indoor_temp_entity": {
        "friendly_name": "Indoor temperature sensor",
        "criticality": "degraded",
    },  # gated: source == sensor/input_number
    "fan_entity": {"friendly_name": "Whole-house fan", "criticality": "degraded"},  # gated: fan_mode uses a WHF
    "fan_state_entity": {"friendly_name": "Fan state sensor", "criticality": "degraded"},  # gated: fan_mode uses a WHF
    "fan_remote_entity": {"friendly_name": "Fan RF remote", "criticality": "optional"},
    "home_toggle_entity": {"friendly_name": "Home/away toggle", "criticality": "degraded"},
    "vacation_toggle_entity": {"friendly_name": "Vacation toggle", "criticality": "optional"},
    "guest_toggle_entity": {"friendly_name": "Guest toggle", "criticality": "optional"},
}
# door_window_sensors is a list, not a scalar -- swept per member, each flagged "degraded".
```

`notify_service` is checked differently from the other 10: it's a service name (e.g. `"notify.mobile_app_x"`), not a state entity, so it's resolved via `hass.services.has_service("notify", service_name)` rather than `hass.states.get()`.

A blank/unset config value for any key is never flagged, regardless of tier — this is how "optional by design" is enforced structurally rather than special-cased per key.

## Coordinator Interface

| Symbol | Caller | Purpose |
|---|---|---|
| `_run_entity_health_check()` | `_async_update_data_impl()`, called once per ~30-min cycle right alongside `_run_invariant_watchdog()` | Runs the sweep, processes transitions, returns the current issue list for `coordinator.data["entity_health_issues"]` |
| `_process_entity_health_transitions(issues)` | `_run_entity_health_check()` | Diffs against `self._entity_health_state`, decides what needs notifying this cycle |
| `_notify_entity_health_issues(issues)` | `_process_entity_health_transitions()` (only when there's something to notify) | Logs every issue unconditionally, then sends one batched `AutomationEngine._notify()` call |

`self._entity_health_state: dict[str, dict]` is transient (not persisted) — a restart re-evaluates from scratch, which is desirable since a restart is itself the most common way an entity reappears.

## Debounce

| Transition | Result |
|---|---|
| ok -> missing/unavailable | Notify immediately; record `first_seen`/`last_notified` |
| missing -> missing, < 24h since last notify | No notification |
| missing -> missing, >= 24h since last notify | One reminder notification; `last_notified` updated |
| missing -> ok | State cleared, logged at INFO; **no** recovery notification (keeps this quiet by default) |

Multiple entities transitioning `ok -> missing` in the same cycle (e.g. a network drop taking out several devices at once) batch into **one** notification, not N separate pushes.

## Startup Suppression

`_run_entity_health_check()` returns `[]` immediately whenever `self._startup_coalesce_active` is `True`, without touching `self._entity_health_state` at all. This means an entity still missing once coalescing ends is treated as a fresh `ok -> missing` transition (correctly notifying), not a stale continuation of something that was silently tracked during boot.

## User-Visible Surface

- **Notification:** one push/email via `AutomationEngine._notify(message, title, notification_type="entity_health")`, gated by `push_entity_health`/`email_entity_health` config toggles (default `True`, same opt-out pattern as every other event type).
- **Status card:** `coordinator.data["entity_health_issues"]` (list of `{config_key, entity_id, friendly_name, criticality, status}`) -> `api.py`'s status endpoint -> rendered as additional `.value` lines on the existing Status card in `index.html`'s `loadStatus()`, right below `invariant_violations` — no new card, per the Observability Requirements' "extend existing cards" rule.
- **Logs:** every issue is logged unconditionally (ERROR for `critical`, WARNING otherwise) regardless of whether the notification itself succeeds — `notify_service` is one of the monitored entities, so a broken notify target must not mean the failure goes unrecorded anywhere.

## Related Fix: HVAC Command Failure Containment

Issue #805 also closed a second gap in the same failure area: `_set_hvac_mode()`/`_set_temperature()` in `automation.py` (the two single write points, per Fix 1b) previously had no `except` clause around their `hass.services.async_call()` calls — a vanished `climate_entity` would raise uncaught, risking an aborted update cycle. Both now catch the exception, log an ERROR, and emit an `incident_detected`/`hvac_command_failed` event (see [Incident Classes](incident-classes.md)) instead of propagating. This is deliberately a two-line change at each of the two existing choke points, not a broader `automation.py` change.

## Disclosure Path

<- Tier 1 parent: [Architecture Reference](02-ARCHITECTURE-REFERENCE.md)
-> Tier 3 spec: not yet written (known gap, same status as `repairs-brief.md`)
<-> Siblings: [Repairs Brief](repairs-brief.md) | [Incident Classes](incident-classes.md) | [Invariant Watchdog Brief](invariant-watchdog-brief.md)
