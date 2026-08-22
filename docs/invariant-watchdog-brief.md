<!-- Nav: ← [02-ARCHITECTURE-REFERENCE.md] | → [invariant_watchdog.py] | ↔ [grace-periods-spec.md] -->

# Invariant Watchdog — Architecture Brief (Tier 2)

## Anchors

| Question | Short answer | → Full answer |
|---|---|---|
| What does this module own and what does it explicitly not own? | It owns detecting and alerting on hard system invariant violations from ground-truth state. It does NOT own enforcing or correcting them — that stays in `automation.py`'s command layer. | [Scope](#scope) |
| Why does it read ground truth instead of the automation engine's own flags? | Because the 2026-08-22 incident (#739/#748) that motivated this module happened while those flags stayed entirely self-consistent — only a physical-state read catches that class of bug. | [Responsibilities](#responsibilities) |
| What happens when a violation is detected? | A CRITICAL log line every cycle, plus (deduped to once per 5 minutes) an event-log entry, a push notification, and a Status-tab surface. Never a corrective command. | [Interfaces](#interfaces) |
| How do I add a new invariant? | Same process as this project's LOCKED golden-scenario policy — explicit human review and sign-off before adding an entry to `INVARIANTS`. | [Invariants](#invariants) |

## Scope

**Owns:**
- The `INVARIANTS` list and each invariant's pure check function (currently one: `check_ac_whf_mutex`).
- `run_invariant_checks()` — runs every registered check against a snapshot of ground-truth inputs.

**Explicitly does NOT own:**
- Enforcing/correcting a violation — that is `automation.py`'s command layer (`_deactivate_fan()`, `_suppress_hvac_for_whf()`).
- Reading live HA state — the coordinator reads ground truth (`hvac_action`, `_get_fan_physical_state()`) and passes it in; this module never touches `hass.states` itself, keeping it a pure, trivially-testable leaf.
- General input/config validation — this is not a validation framework. It holds only genuine hard invariants ("must never happen"), not routine sanity checks.

## Responsibilities

- Detect when the whole-house fan and the AC are both physically active at the same time, independent of what CA's internal override/grace/session bookkeeping believes.
- Return a list of `InvariantViolation` objects (empty when nothing is wrong) so the caller can log/alert/surface without this module needing to know about HA logging, notifications, or the dashboard.

## Interfaces

```python
def check_ac_whf_mutex(
    *, hvac_action: str | None, whf_physically_on: bool | None, fan_mode: str
) -> InvariantViolation | None: ...
def run_invariant_checks(
    *, hvac_action: str | None, whf_physically_on: bool | None, fan_mode: str
) -> list[InvariantViolation]: ...
```

| Symbol | Caller(s) | Purpose |
|---|---|---|
| `run_invariant_checks()` | `coordinator.py` `_run_invariant_watchdog()` | Runs all registered checks once per update cycle |
| `check_ac_whf_mutex()` | `run_invariant_checks()` (via `INVARIANTS`) | The one currently-registered hard invariant |

**Events emitted / consumed:**

| Event | Direction | Handler |
|---|---|---|
| `invariant_violation` | emitted (by `coordinator._run_invariant_watchdog()`, not this module directly) | `ai_skills_context.py` `_render_invariant_violation()` |

## Data Structures

```python
@dataclass(frozen=True)
class InvariantViolation:
    name: str  # e.g. "ac_whf_mutex"
    detail: str  # human-readable, occupant-first description
```

No persistence — violations are transient per-cycle findings, surfaced via the existing event log (`coordinator._emit_event`, capped at `EVENT_LOG_CAP`) and `coordinator.data["invariant_violations"]` (replaced each cycle, not accumulated).

## Invariants

1. **Every check function reads ground truth only, never automation-engine bookkeeping.** No `check_*` function may accept or read `_fan_override_active`, `_fan_remote_timer_hours`, `_manual_override_active`, `_grace_active`, or any other internal flag — only live sensor/thermostat reads, passed in by the caller.
2. **`INVARIANTS` is a flat list, not a plugin/registry system.** A new entry requires the same explicit human sign-off as this project's LOCKED golden-scenario policy (show the proposed invariant in human-readable form, confirm it represents a genuine hard constraint, only then land it).
3. **This module never issues an HA service call.** It is a pure leaf — `run_invariant_checks()` has no side effects. All logging/notification/event-emission side effects live in `coordinator._run_invariant_watchdog()`, one level up.

## Disclosure Path

← Tier 1 parent: [02-ARCHITECTURE-REFERENCE.md](02-ARCHITECTURE-REFERENCE.md)
→ Tier 3 specs: none yet — a single-invariant module doesn't yet warrant one; revisit if `INVARIANTS` grows.
↔ Siblings: [grace-periods-spec.md](grace-periods-spec.md) (documents the AC/WHF mutex's own command-layer enforcement, which this module observes but does not implement)
