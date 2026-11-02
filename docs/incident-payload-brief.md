<!-- Nav: ← [Architecture Reference](02-ARCHITECTURE-REFERENCE.md) | → incident_payload.py (source) | ↔ [Incident Classes](incident-classes.md) -->

# Incident Payload — Architecture Brief (Tier 2)

## Anchors

| Question | Short answer | → Full answer |
|---|---|---|
| What does `incident_payload.py` own and not own? | It owns the common `incident_detected` payload envelope (`build_incident_payload()`) and the HA-validation-error classifier (`is_climate_validation_error()`). It does NOT emit events; emission stays with the coordinator (`_emit_incident` -> `_emit_event`) and the engine (`_emit_engine_incident` -> `_emit_event_callback`). | [Scope](#scope) |
| Why does it exist? | Before Issue #1049 the coordinator and four hand-built copies in `AutomationEngine` each assembled the same envelope and drifted (one carried a display-unit setpoint under a `_f` key). | [Responsibilities](#responsibilities) |
| What units do payload temperatures use? | Internal °F always, regardless of the display unit (`setpoint_f`, `indoor_f`, `last_commanded_setpoint_f`, `comfort_*`); renderers convert for display. | [Invariants](#invariants) |
| What counts as a "validation error" rather than a device failure? | An exception whose MRO contains `ServiceValidationError`, `Invalid`, `MultipleInvalid` or `ServiceNotSupported` (matched by class name so it works with test stubs). Those are logged and skipped, never an `hvac_command_failed` episode. | [Interfaces](#interfaces) |
| Which classes carry the thermostat context fields? | `comfort_violation` / `comfort_undertemp` add `thermostat_mode`, `thermostat_action`, `last_commanded_mode`, `last_commanded_setpoint_f`, `last_command_result` via `coordinator._thermostat_incident_context()`. | [Interfaces](#interfaces) |
| What tolerance did a comfort incident use? | `comfort_violation` / `comfort_undertemp` add `swing_tolerance_f` (internal degF delta, floor 0.5, cap 2.5 = `COMFORT_SWING_TOLERANCE_MAX_F`), `swing_tolerance_source` (`learned` / `default`, with ` (capped)` appended when the cap bit, e.g. `learned (capped)`) and `swing_tolerance_applied` (thermostat mode matched the direction) via `coordinator._hysteresis_incident_context()` (Issue #1258). | [incident-classes.md](incident-classes.md) |

## Scope

**Owns:**
- `build_incident_payload(incident_class, incident_id, *, comfort_heat, comfort_cool, occupancy_mode=None, extra=None)` — the envelope `{incident_class, incident_id, comfort_cool, comfort_heat, occupancy_mode}` merged with class-specific `extra`.
- `is_climate_validation_error(err)` — HA parameter-validation vs device-failure classification used by `AutomationEngine._call_climate()`.

**Explicitly does NOT own:**
- Event emission and the event log (coordinator `_emit_event`, engine `_emit_event_callback`).
- Incident detection rules and class definitions ([incident-classes.md](incident-classes.md)).
- Episode gating of `hvac_command_failed` (`automation.py` `_note_hvac_command_failed` via `next_fallback_episode`).

## Responsibilities

- Give every emitter one payload shape; the coordinator's `_emit_incident()` and the engine's `_emit_engine_incident()` both call `build_incident_payload()`.
- Keep temperature fields in internal °F so a Celsius install never writes `setpoint_f` in °C.
- Classify climate-call exceptions so out-of-range setpoints and modes the thermostat does not offer are skipped quietly.

## Interfaces

```python
def build_incident_payload(
    incident_class: str,
    incident_id: str,
    *,
    comfort_heat: float | None,
    comfort_cool: float | None,
    occupancy_mode: str | None = None,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]: ...
def is_climate_validation_error(err: BaseException) -> bool: ...
```

| Symbol | Caller(s) | Purpose |
|---|---|---|
| `build_incident_payload()` | `coordinator.py` `_emit_incident()`; `automation.py` `_emit_engine_incident()` (`setpoint_mode_inconsistency`, `hvac_command_failed`) | Common envelope |
| `is_climate_validation_error()` | `automation.py` `_call_climate()` | REJECTED vs FAILED |

**Events:** `incident_detected` (emitted by callers, not by this module).

**Band source (Issue #1054):** both emitters fill `comfort_heat`/`comfort_cool` from the band in force (sleep / away / vacation / aggressive savings, plus the TOU away/vacation widening since Issue #1080) via `AutomationEngine._resolved_comfort_band()`; the engine falls back to the flat config values only before the first classification exists.

## Data Structures

No persistent state. The payload is a plain dict appended to `coordinator._event_log`.

## Invariants

1. Temperature values in a payload are internal °F, never the display unit.
2. `build_incident_payload()` never raises and never mutates `extra`.
3. A validation error never opens a `hvac_command_failed` episode.

## Disclosure Path

← Tier 1 parent: [Architecture Reference](02-ARCHITECTURE-REFERENCE.md)
→ Tier 3: [Incident Classes](incident-classes.md), [Computation Reference — Command shapes since #301](08-COMPUTATION-REFERENCE.md#command-shapes-since-301-explicit-mode-switch-issue-1049)
↔ Siblings: [Entity Health Brief](entity-health-brief.md)
