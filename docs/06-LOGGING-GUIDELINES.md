<!-- Nav: ← [Architecture Reference](02-ARCHITECTURE-REFERENCE.md) -->

# Logging Guidelines

How Climate Advisor writes log lines, so the HA log can answer a homeowner's "why did the house do that?". How to *read* the logs is in the [Debugging Guide](09-DEBUGGING-GUIDE.md#3-container-logs-real-time).

## Anchors
| Question | Short answer | → Full answer |
|---|---|---|
| What format do log messages use? | %-style args (never f-strings), outcome first, then `key=value` pairs; em dash `—` between the event and its detail; units on every quantity. Logger lines carry internal °F; occupant-facing *reason strings* use the user's `temp_unit` (Issue #1050). | [§Format Rules](#format-rules) |
| Which level do I pick? | DEBUG = repeated or rejected detail; INFO = a decision or state change, including a routine guard deferring a write (Issue #874); WARNING = a guard clamped/overrode a value, a safety guard acted, or a recoverable fault; ERROR = a failed command or broken invariant. | [§Level Semantics](#level-semantics) |
| What must a thermostat write log? | One INFO line from `_set_hvac_mode` / `_set_temperature`, with a required `reason` and `role=`. | [§Thermostat Adjustment Logging](#thermostat-adjustment-logging) |
| How is observe-only (automation off) logged? | `[DRY RUN] Would ...` at INFO for a skipped command; repeats drop to DEBUG (Issue #1304). A safety notice still sent to the occupant logs one WARNING per episode (Issues #1144, #1288). | [§Observe-only](#observe-only-automation-off) |
| How do I tell which zone a line came from? | Learning lines carry `zone=<entry id>`; thermostat-write lines carry `zone=<climate entity>`; captured WARNING+ records get a zone tag from `log_capture.zone_scope()` (Issue #812). | [§Zone Attribution](#zone-attribution) |

## Logger Setup

Every module uses `_LOGGER = logging.getLogger(__name__)`. The logger name (`custom_components.climate_advisor.<module>`) already says which module wrote the line, so do not repeat it in the message. A subsystem prefix that helps grep is fine (`WHF safety:`, `apply_classification:`, `investigator:`).

Pure decision modules (`*_fsm.py`, `*_decision.py`, `swing.py`, ...) do not log. They return a verdict and a reason, and the caller in `automation.py` or `coordinator.py` logs the outcome. Most log lines live in those two files.

## Format Rules

- **%-style args**: `_LOGGER.info("msg %s", value)`, never an f-string in a log call. No call site uses one today; ruff does not check this, so review does.
- **Outcome first, then `key=value`**: `"Swing sample accepted: zone=%s mode=%s value=%.2f ..."`. This is the CLAUDE.md Observability rule and keeps grep predictable.
- `%r` for untrusted values (entity states, user strings); `%d` for counts; `%.1f` (or `%.2f`) for temperatures and other floats.
- **Em dash** `—` separates the event from its detail. Newer WHF and nat-vent lines use `--`. Both appear, so grep for the words, not the dash.
- **Units** on every quantity: `°F`, `s`, `min`, or a `_s` / `_f` key suffix.
- **Temperature units (Issue #1050).** A `reason` string (what the Activity Log and push notifications show) formats temperatures with `AutomationEngine._t()` / `_td()`, never a hard-coded `°F`. Other logger lines keep the raw internal °F value and the `°F` label so logs compare across installs. The exception is the thermostat-write lines, which render the setpoint with `format_temp()` in the user's unit. Do not change substrings other code matches on (`airflow reversed`, `cooled to floor`, `manual override to`, `sleep band [`, `comfort band [`, ...). See [temperature-conversion.md](temperature-conversion.md#display-sites-and-reason-strings-issue-1050-part-of-1048).
- **Never log** credentials, tokens or any part of an API key (CLAUDE.md Security Requirements).

## Level Semantics

| Level | Use for | Real example |
|---|---|---|
| `DEBUG` | High-frequency or transient detail: per-cycle calculations, debounce, a rejected learning sample, an observe-only re-ask | `"Day type — today_high=%.0f°F, classified=%s"` (classifier.py:308) |
| `INFO` | A decision or state change: setup, mode/setpoint writes, briefings, saved records, an accepted learning sample, and a guard that *defers or redirects* a write for a routine reason (WHF owns the thermostat, door/window pause) | `"Door/window pause (%s): HVAC already off — pause flag set, no mode change needed"` (automation.py:7625) |
| `WARNING` | A guard *clamped or overrode* a value the caller asked for; a safety guard acted because the HVAC would otherwise be in an unexpected state; a recoverable fault with a fallback (say what failed **and** what happens next) | `"Pre-cool target %.1f°F below floor %.1f°F (...); clamped to %.1f°F"` (automation.py:11986); `"Forecast entry for %s has a null %s field — falling back to %.1f°F"` (coordinator.py:5028) |
| `ERROR` | A failed command, a broken invariant being force-corrected, or I/O that lost data | `"Stuck grace detected: ... force-cancelling grace (Issue #508)."` (coordinator.py:3425); `"Failed to save learning state: %s"` (learning.py:1113) |

Two cases worth naming:

- **Accepted vs rejected samples (Issue #1233).** A per-run learning line is INFO when the sample is counted and DEBUG when it is rejected or skipped: `Swing sample accepted` / `Swing sample rejected` (coordinator.py:9313 / 9300), `Coast sample recorded` / `Coast sample skipped` (coordinator.py:9361 / 9351). Rejected lines appear only with debug logging on. How to read them: [Debugging Guide — Learned Swing](09-DEBUGGING-GUIDE.md#debugging-the-learned-swing-issue-1233).
- **Rule-2 state backstop (Issue #1288).** The whole-house fan and the heating/cooling are both on because Climate Advisor missed the occupant's press, so the backstop stops one of them. This is always a WARNING, because it only acts after a missed press: `"WHF safety: rule-2 state backstop: ... -- stopping the %s: trigger=%s ..."` (automation.py:12810). It also writes an Activity row and a `whf_rule2_backstop` event. Behavior: [Fan Lifecycle Spec](fan-lifecycle-spec.md#rule-2-state-backstop-the-fan-and-the-heatingcooling-never-run-together-issue-1288).

Why routine writes are INFO and not WARNING (Issue #585): [08 §13](08-COMPUTATION-REFERENCE.md#13-logging-level). Issue #874 later moved two guards from WARNING to INFO: WHF-owns-thermostat and the `apply_classification` door/window `DEFER_PAUSED` branch (automation.py:5172, 4795). §13's bullets that still call them WARNING are older than that change.

WARNING and above is also copied into the AI Investigator's `SYSTEM LOG RECORDS` (`log_capture.py`, 200-record ring buffer). An INFO line never reaches the investigator, so a decision the occupant would care about also needs an event (see [below](#logs-vs-events-vs-incidents)).

## Thermostat Adjustment Logging

Every thermostat write goes through the primitives. Each takes a required keyword-only `reason` with no default, so a missing reason raises a `TypeError` instead of leaving a silent gap:

```python
async def _set_hvac_mode(self, mode: str, *, reason: str) -> HvacCommandResult
async def _set_temperature(self, temperature: float, *, reason: str, mode: str = "cool", ...) -> HvacCommandResult
async def _set_temperature_for_mode(self, c: DayClassification, *, reason: str, ...) -> HvacCommandResult | None
```

Each successful write logs one INFO line and records the action (`_record_action` logs `Action recorded: <action> — <reason>`, automation.py:2459):

```
INFO  Set HVAC mode to heat — door/window closed — restoring heat mode (occupant's setpoint kept) role=production
INFO  Set temperature to 68°F (mode=heat) — zone=climate.thermostat bedtime — sleep band [66/74] role=production
INFO  HVAC write blocked — whole-house fan owns thermostat (zone=climate.thermostat, door/window open — binary_sensor.kitchen_window)
```

Sources: automation.py:5883, 6417, 5172. `role` is the engine's label: `production` in HA, or whatever the sim harness passes.

### Reason string convention

Reasons follow `trigger — context`, with temperatures in the user's unit:

| Trigger | Context (as written today) |
|---|---|
| `daily classification` | `<day_type> day, trend <direction> <delta>` (automation.py:4925) |
| `door/window open` | `<entity_id>` (automation.py:7985) |
| `door/window closed` | `restoring <mode> mode (occupant's setpoint kept)` / `ending natural ventilation mode` (automation.py:8138, 8050) |
| `occupancy away` / `occupancy vacation` | `setback band` (automation.py:16849) |
| `occupancy home` | `restoring <heat\|cool> comfort` (automation.py:16970) |
| `bedtime` | `sleep band [<floor>/<ceiling>]` (automation.py:11870) |
| `morning wake-up` | `comfort band [<floor>/<ceiling>]` (automation.py:12244) |

## Observe-only (automation off)

With the automation switch off (`dry_run`), Climate Advisor sends no commands and records nothing it did not send (Issues #1293, #1304):

- A skipped command logs at INFO with a `[DRY RUN]` prefix: `[DRY RUN] Would set HVAC mode to %s — %s role=%s`, `[DRY RUN] Would set temperature to %s (%s mode) — %s role=%s`, `[DRY RUN] Would send notification: %s — %s role=%s` (automation.py:5838, 6124, 2213).
- A check that asks again on every tick logs the first ask at INFO and the repeats at DEBUG. `_log_fan_dry_run()` allows one INFO per caller per 30 minutes (automation.py:13303); other re-asks pick the level from `self.dry_run` (automation.py:13439).
- "Session kept / not recorded" lines carry `(observe-only)` and are usually DEBUG (automation.py:13347, 13635).
- **Observe-only notices.** In two safety cases Climate Advisor still notifies the occupant with automation off, using `_notify(..., observe_only_ok=True)`. One is the whole-house fan running on a sealed house (Issue #1144). The other is the fan running beside heating/cooling (Issue #1288). Each logs one WARNING per episode, `"WHF safety: ... automation is off (observe-only): notifying, not stopping"` (automation.py:12962, 12996), and emits `whf_sealed_observe_notified` or `whf_rule2_observe_notified`.

Grep `[DRY RUN]` to list everything Climate Advisor would have done.

## Zone Attribution

With two or more zones, each config entry runs its own coordinator, and all of them write to the same logger.

- **Log text.** Learning lines carry `zone=<entry id>` from `_zone_log_id()` (coordinator.py:4691, learning.py:997; Issue #1233). Thermostat-write lines carry `zone=<climate entity>`. Give any new per-zone line a `zone=` field.
- **Captured records (Issue #812).** `log_capture.py` tags each WARNING+ record with the zone label held in a `ContextVar`. Coordinator timers and listeners run inside `_zone_scoped` / `_zone_scoped_sync`, and REST handlers run inside `zone_scope()` (api.py). The ContextVar does not cross into executor threads. Offload work with `coordinator._executor_job()`, which wraps `bind_zone_for_executor()`, and never with a bare `hass.async_add_executor_job()`. A record with no zone shows as "unknown zone" in the investigator. Details: [Multi-Zone Spec — Gap 10c](multi-zone-spec.md#10c--zero-zone-attribution-anywhere-in-logging).

## Logs vs Events vs Incidents

A log line is for whoever reads the HA log and never reaches the dashboard. Anything the occupant should see also needs one of these:

- **An event**: `coordinator._emit_event(type, data)`, which the engine reaches through `_emit_event_callback`. Events feed the Activity Report and the investigator's event log. Vocabulary: [Activity Report Table](activity-report-table.md).
- **An incident**, for a detected problem: an `incident_detected` event built by `build_incident_payload()` (coordinator.py:11577, automation.py:5564). See [Incident Payload Brief](incident-payload-brief.md) and [Incident Classes](incident-classes.md).

## Reading the Logs

`tools/ha_logs.py` pulls `ha core logs` over SSH and keeps lines containing `climate_advisor` (the logger name), so every module is included. `--thermal` narrows to thermal-learning lines, and `--history` reads the REST recorder instead. Usage and retention notes: [Debugging Guide §3](09-DEBUGGING-GUIDE.md#3-container-logs-real-time). DEBUG lines appear only with a `logger:` override for `custom_components.climate_advisor` in `configuration.yaml`.
