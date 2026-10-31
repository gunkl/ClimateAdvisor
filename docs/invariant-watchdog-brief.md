<!-- Nav: ← [02-ARCHITECTURE-REFERENCE.md] | → [invariant_watchdog.py] | → [promise_journal.py] | ↔ [grace-periods-spec.md] -->

# Invariant Watchdog — Architecture Brief (Tier 2)

## Anchors

| Question | Short answer | → Full answer |
|---|---|---|
| What does this module own and what does it explicitly not own? | It owns detecting and alerting on hard system invariant violations from ground-truth state. It does NOT own enforcing or correcting them — that stays in `automation.py`'s command layer. | [Scope](#scope) |
| Why does it read ground truth instead of the automation engine's own flags? | Because the 2026-08-22 incident (#739/#748) that motivated this module happened while those flags stayed entirely self-consistent — only a physical-state read catches that class of bug. | [Responsibilities](#responsibilities) |
| What happens when a violation is detected? | A CRITICAL log line every cycle, plus (deduped to once per 5 minutes) an event-log entry, a push notification, and a Status-tab surface. Never a corrective command. | [Interfaces](#interfaces) |
| How do I add a new invariant? | Same process as this project's LOCKED golden-scenario policy — explicit human review and sign-off before adding an entry to `INVARIANTS` or `PROMISES`. | [Invariants](#invariants) |
| What are the household promises and where do they live? | Nine pure checks (`PROMISES` in `invariant_watchdog.py`: promises 1-7 plus 5b are registered; promise 8 is test-only) over an immutable `PromiseInput` built by `promise_journal.PromiseJournal`. Run by `run_promise_checks()`; wired into the coordinator log-only (Step 3 increment I8): `coordinator._evaluate_household_promises()` on the 5-minute tick, fed from the thermostat listener, `_emit_event`, `AutomationEngine._call_service_with_context` (via `_command_observer`) and `_async_command_fan_entity`. | [Household promises](#household-promises-issue-1128) |
| May a check read CA's own flags? | Only promise 4 (`override_respected`), and only CA's override/grace *events*. Every other check reads ground truth. Amended 2026-10-29, user-approved. | [Invariants](#invariants) |
| How do the promises stay no stricter than production? | `PromiseNumbers` holds every tunable; `tests/test_promise_numbers.py` walks the legal config grid and fails if a promise could flag what production allows. | [Household promises](#household-promises-issue-1128) |

## Scope

**Owns:**
- The `INVARIANTS` list and each invariant's pure check function (currently one: `check_ac_whf_mutex`).
- `run_invariant_checks()` — runs every registered check against a snapshot of ground-truth inputs.
- The household-promise registry `PROMISES` (frozen `PromiseSpec` tuple), `PromiseNumbers`/`NUMBERS`, `PromiseConfig`, `Hit`, and `run_promise_checks()` (Issue #1128).
- `promise_journal.py` — the bounded in-memory evidence journal and record types the promises decide on (`Reading`, `Command`, `Mark`, `Episode`, `PromiseInput`, `PromiseJournal`), plus the `EVENT_MARK_MAP` of CA events it reads. No Home Assistant imports.

**Explicitly does NOT own:**
- Enforcing/correcting a violation — that is `automation.py`'s command layer (`_deactivate_fan()`, `_suppress_hvac_for_whf()`).
- Reading live HA state — the coordinator reads ground truth (`hvac_action`, `_get_fan_physical_state()`) and passes it in; this module never touches `hass.states` itself, keeping it a pure, trivially-testable leaf.
- Reporting a hit (WARNING line, `invariant_violation` event with `severity=log`, persisted `promise_stats`) lives in `coordinator._evaluate_household_promises()`; only promise 2 alerts (existing `_run_invariant_watchdog` path, dedup key `invariant_violation:ac_whf_mutex`). No promise appears on the Status card until promoted.
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

## Household promises (Issue #1128)

Written once, run both inside production (log-only first, alert later) and after every step of every test. Design: `plans/test-automation/03-step3-design.md`.

| # | key | Past bugs | Test severity | Home level now |
|---|---|---|---|---|
| 1 | `settling_flip` | #821-#867, #1066, #1071, #1073 | hard | log |
| 2 | `ac_whf_mutex` (adapter over the snapshot `check_ac_whf_mutex`) | #739, #748, #1057 | hard | alert (existing behavior) |
| 3 | `observe_only_silence` | #589, #1102, #1108, #1109 | hard | log |
| 4 | `override_respected` | #1027, #1045, #1055, #1006, #1119 | hard | log |
| 5 | `no_blind_fan_start` | #1037, #1042 | hard | log |
| 5b | `blind_fan_continue` | #1037, #1043 | warning | log |
| 6 | `mode_flip_rate` | #1066/#1073 flip-flop | warning | log |
| 7 | `comfort_kept` | #1066 | warning | log |
| 8 | `surfaces_agree` | band shown vs band held | hard (ratcheted) | none (test-only; not in `PROMISES`) |

Decisions (user, 2026-10-29): promise 1 follows the user's configured settling tolerance minus 0.5 F of slack (flag zone `min(2.0, smallest tolerance) - 0.5`; lookback `min(120, configured window) - 5` min), both directions, each clause gated by the day's forecast high; promise 4's exception list is split by device (below); promise 5b is included.

Promise 4 exceptions. Fan override: natural grace expiry, dashboard cancel, bedtime, wake-up, occupancy away/vacation, a newer occupant action, supersede, adoption, restart. Thermostat override: the same list **minus bedtime and wake-up** (a command that undoes a thermostat override at bedtime or wake-up is the #1119 defect shape). `stuck_grace_recovered` is never an excuse. Fan-off grace: natural expiry, dashboard cancel, supersede, restart, indoor above the comfort ceiling.

Journal: readings, commands and marks are kept 3 h (caps 400/128/128); episode marks are kept as long as the episode is open; dedup LRU 256; the per-promise `promise_stats` helpers are validated and capped. The journal is not persisted: after a restart every history-needing check sees no data and reports nothing.

Every temperature in `Reading`/`Command`/`Hit.evidence` is internal degrees F; only `Hit.detail` is rendered in the user's unit.

## Data Structures

```python
@dataclass(frozen=True)
class InvariantViolation:
    name: str  # e.g. "ac_whf_mutex"
    detail: str  # human-readable, occupant-first description
```

No persistence — violations are transient per-cycle findings, surfaced via the existing event log (`coordinator._emit_event`, capped at `EVENT_LOG_CAP`) and `coordinator.data["invariant_violations"]` (replaced each cycle, not accumulated).

## Invariants

1. **Every check function reads ground truth only, never automation-engine bookkeeping.** No `check_*` function may accept or read `_fan_override_active`, `_fan_remote_timer_hours`, `_manual_override_active`, `_grace_active`, or any other internal flag — only live sensor/thermostat reads and the service commands CA actually issued, passed in by the caller. **Amended 2026-10-29 (user-approved, Issue #1128): promise 4 (`override_respected`) is the one exception.** It is a promise about CA keeping its own word, so it may read CA's own override/grace *events* (as derived `Episode`s), but it still decides a violation on a ground-truth *effect* (a command that reverses the occupant's value). Promises 1-3, 5, 5b, 6 and 7 read only readings and commands; marks are used by promise 7 solely for exclusions (occupancy, nat-vent session, restart). No check may read an internal flag directly.
2. **`INVARIANTS` and `PROMISES` are flat collections, not plugin/registry systems.** A new entry (or a loosened promise) requires the same explicit human sign-off as this project's LOCKED golden-scenario policy (show the proposed invariant in human-readable form, confirm it represents a genuine hard constraint, only then land it).
3. **Promise numbers are never stricter than production.** Every tunable lives in `PromiseNumbers`; `tests/test_promise_numbers.py` proves it over the legal config grid (degrees F and C). A change to a number, or to a default or clamp in `const.py`, that breaks the proof fails there first.
4. **Every promise has a negative control and a compliant twin** (`tests/test_promise_checks.py`). A promise without both is not done.
5. **Every climate/fan/switch service call goes through a known choke point** (`tests/test_service_call_choke_points.py`). Promise 3 and the command journal only see what flows through them. `coordinator._async_command_fan_entity` is the one named, justified bypass of `_call_service_with_context` (it sends no `Context`; it needs its own journal hook in I8).
6. **This module never issues an HA service call.** It is a pure leaf — `run_invariant_checks()` has no side effects. All logging/notification/event-emission side effects live in `coordinator._run_invariant_watchdog()`, one level up.

## Anchors added for the promise registry

| Symbol | Where | Purpose |
|---|---|---|
| `run_promise_checks(inp, cfg, *, scope)` | `invariant_watchdog.py` | Runs every promise for `"home"` (those with a home level) or `"test"` (all); a crashing check is swallowed at home, re-raised in tests |
| `PROMISES`, `PromiseSpec` | `invariant_watchdog.py` | The flat registry: number, key, label, check, test severity, home level |
| `PromiseNumbers` / `NUMBERS` | `invariant_watchdog.py` | Every tunable promise number with its looseness rationale |
| `PromiseConfig.from_config()` | `invariant_watchdog.py` | Raw-config view; owns its own sleep window and four floor/ceiling lookups, never calls the engine's band helpers |
| `Hit` | `invariant_watchdog.py` | One broken promise: stable key, plain-English detail, evidence in F |
| `equipment()`, `occupant_actions()` | `invariant_watchdog.py` | Ground-truth helpers shared by several promises (an occupant action is derived from readings + commands, not from CA's override detection) |
| `PromiseJournal`, `derive_episodes()`, `EVENT_MARK_MAP` | `promise_journal.py` | Bounded evidence store, episode derivation, and the CA events it reads |
| `sanitize_promise_stats()`, `bump_promise_stats()` | `promise_journal.py` | Validated, capped per-promise counters for the future persisted `promise_stats` |

## Disclosure Path

← Tier 1 parent: [02-ARCHITECTURE-REFERENCE.md](02-ARCHITECTURE-REFERENCE.md)
→ Tier 3 specs: `docs/household-promises-spec.md` is planned (Step 3, increment I10); until then the design is `plans/test-automation/03-step3-design.md`.
↔ Siblings: [grace-periods-spec.md](grace-periods-spec.md) (documents the AC/WHF mutex's own command-layer enforcement, which this module observes but does not implement)
