<!-- Nav: <- Context: [Architecture Reference](02-ARCHITECTURE-REFERENCE.md) | -> Detail: [Incident Classes](incident-classes.md) | Related: [AI Skills Spec](ai-skills-spec.md) -->

# Simulation Feedback Loop -- Tier 2 Brief

## Anchors

| Question | Short answer (<= 2 sentences) | -> Full answer |
|---|---|---|
| What is the closed loop and what problem does it solve? | Production incidents are automatically converted to pending BSpec scenarios, validated against simulate.py (logic) and production event logs (integration), and surfaced to a human for approval into the golden suite. It was built because bugs #220-222 all involved occupancy-mode transitions with near-zero simulation coverage. | [What the loop is](#what-the-loop-is) |
| What is the difference between a Logic Trace and an Integration Trace? | A Logic Trace is what simulate.py predicts from automation engine behavior alone, run offline. An Integration Trace is what the coordinator + HA together actually did in production, observed from the event log. | [Two Validation Tracks](#two-validation-tracks) |
| What is a BSpec and how does it differ from a golden scenario? | A BSpec is a scenario JSON expressing what the system SHOULD do (assertions). It lives in pending/ until a human approves it. A golden scenario is a BSpec that has been reviewed, signed, and promoted to golden/. | [Key Concepts](#key-concepts) |
| What are the 8 incident classes and which are proactive vs reactive? | comfort_violation, nat_vent_escalation, override_detected, system_restart are reactive. setpoint_mode_inconsistency, rapid_override_after_automation, occupancy_transition, override_active_on_occupancy are new classes added for bugs #220-222. setpoint_mode_inconsistency is the only proactive class (fires at command time). | [Incident Classes](#incident-classes) |
| What is the full lifecycle from production event to golden test? | production event -> incident_detected emitted -> build_historical_scenario.py -> pending/ BSpec -> simulation_loop.py validates -> Validation Record written -> Dashboard Tests tab -> human approves -> golden/ | [Lifecycle](#lifecycle) |
| When does incident detection run -- proactive or reactive? | Proactive: setpoint_mode_inconsistency fires at command time inside automation.py _set_temperature, before the HA service call. All other classes are reactive: they fire post-cycle in coordinator._detect_and_emit_incidents after each 30-min update. | [Detection Timing](#detection-timing) |
| What is the Promotion Gate? | A human reviews per-BSpec pass/fail statistics in the Dashboard Tests tab, then clicks Approve. The approve endpoint runs --sign, moves the file from pending/ to golden/, and updates MANIFEST.json. | [Promotion Gate](#promotion-gate) |
| Where do Validation Records live and are they tracked in git? | tools/simulations/results/ in the simulation-feedback-loop worktree. This directory is gitignored -- Validation Records are ephemeral state. Only scenario JSON files in pending/ and golden/ are tracked. | [Results Storage](#results-storage) |

---

## What the Loop Is

The simulation feedback loop converts production incidents into regression tests automatically. Without it, a bug must be noticed by the user, diagnosed, and manually converted into a test. With the loop, any coordinator cycle that trips an incident class generates a BSpec within minutes.

**Why it was built:** Bugs #220, #221, and #222 all involved occupancy-mode transitions that the existing golden suite did not exercise:
- #220: comfort violation during away-mode setback on a cool day
- #221: automation-driven setpoint change falsely detected as manual override (`_temp_command_pending` timing gap)
- #222: 61 F setpoint applied in cool mode -- a silent failure that produced no comfort violation for hours

None of these were catchable by the existing golden suite. The loop closes this gap by detecting the exact signatures of these failures at production time.

---

## Key Concepts

| Concept | What it is |
|---|---|
| Incident | A real production event window that the system flagged as needing review. Has an incident_class and incident_id. |
| BSpec | What the automation/coordinator SHOULD do given a set of inputs. A scenario JSON file in pending/. |
| Logic Trace | What simulate.py predicts (automation engine behavior only, offline). |
| Integration Trace | What actually happened in production (coordinator + automation + HA together, observed from event_log). |
| Validation Record | One comparison: BSpec assertions vs. Logic Trace or Integration Trace. Written to results/. |
| Track | Logic (simulator can validate) vs. Integration (requires production). Every BSpec assertion is tagged with one. |
| Pending Test | A BSpec with accumulated Validation Records, not yet approved. Visible in the Dashboard Tests tab. |
| Promotion Gate | Human reviews statistics in the Tests tab -> approves -> BSpec moves from pending/ to golden/. |

---

## Two Validation Tracks

Every BSpec assertion is tagged `"track": "logic"` or `"track": "integration"`.

**Logic track:** Verifiable by simulate.py in isolation. Covers automation engine decisions: setpoint selection, day classification application, natural vent escalation, ceiling guard ODE projection. Runs offline; no HA connection required.

**Integration track:** Requires coordinator behavior, HA state events, and async flag timing. Covers: `_temp_command_pending` flag timing, `_last_commanded_hvac_mode` state, whether an automation-driven setpoint change is falsely detected as manual, occupancy handler setpoint selection from coordinator.py (not automation.py).

A BSpec can contain both track types. The Dashboard shows logic pass rate and integration pass rate separately. A BSpec with 3 logic passes and 0 integration passes surfaces as partially validated -- human judgment determines whether to promote.

**Why two tracks matter:** simulate.py mirrors `automation.py` only. Bugs #221 and #222 live in `coordinator.py`. The simulator can never catch them. Tagging assertions by track makes this boundary explicit and prevents false confidence from logic-track passes.

---

## Incident Classes

Eight classes are defined. See [incident-classes.md](incident-classes.md) for the full reference table including production signals, detection timing, and bug motivations.

Brief summary:

| Class | Track | Detection |
|---|---|---|
| comfort_violation | Both | Reactive |
| comfort_undertemp | Both | Reactive |
| nat_vent_escalation | Logic | Reactive |
| override_detected | Integration | Reactive |
| system_restart | Both | Reactive |
| setpoint_mode_inconsistency | Integration | Proactive |
| rapid_override_after_automation | Integration | Reactive |
| occupancy_transition | Both | Reactive |
| override_active_on_occupancy | Integration | Reactive |

Bold classes (setpoint_mode_inconsistency, rapid_override_after_automation, occupancy_transition, override_active_on_occupancy) are new, motivated by bugs #220-222.

---

## Detection Timing

**Proactive (at command time):**

`setpoint_mode_inconsistency` fires inside `automation.py _set_temperature()`, BEFORE the HA service call. If the applied setpoint is below `comfort_heat` while the HVAC mode is `cool`, or above `comfort_cool` while in `heat` mode, the incident is emitted immediately. This is the only class that fires at command time.

Rationale: Bug #222 produced a 61 F setpoint in cool mode. This failure is silent -- no comfort violation fires for hours because the room must first cool down to 61 F. Proactive detection catches it at the moment the bad command is issued.

**Reactive (post-cycle):**

All other classes are detected in `coordinator._detect_and_emit_incidents()`, called after each `_async_update_data()` completes. The detector scans the event log and current state:
- `comfort_violation`: indoor out of comfort band for more than 15 min
- `rapid_override_after_automation`: `override_detected` event within 60s of any automation decision event
- `occupancy_transition`: any `occupancy_change` event in the last cycle
- `override_active_on_occupancy`: `manual_override_active=True` at time of `occupancy_change`

Each incident class is emitted at most once per 30-min cycle (deduplicated by class within the cycle).

---

## Lifecycle

```
Production coordinator cycle (every 30 min)
    |
    +-- Proactive check (at _set_temperature command time):
    |       setpoint_mode_inconsistency -> emit incident_detected immediately
    |
    +-- Reactive checks (post-cycle, _detect_and_emit_incidents):
            comfort_violation, rapid_override_after_automation,
            occupancy_transition, override_active_on_occupancy
                -> emit incident_detected into coordinator._event_log

Simulation Loop (tools/simulation_loop.py, runs every 30 min via schedule)
    |
    +-- GET /api/climate_advisor/event_log?hours=1 -- find incident_detected events
    +-- Cross-reference results/processed_incidents.json -- dedup
    +-- For each new incident:
            build_historical_scenario.py --type <class> --incident-id <id>
                -> BSpec JSON written to tools/simulations/pending/
            simulate.py --pending --filter <name>
                -> Logic Validation Record written to results/<name>/<ts>-logic.json
            Compare production event_log to BSpec expected events
                -> Integration Validation Record written to results/<name>/<ts>-integration.json
    +-- Rebuild results/pending_stats.json
    +-- Send HA notification if 3+ consecutive failures (same class, same track)

Dashboard Tests tab (frontend/index.html)
    |
    +-- GET /api/climate_advisor/pending_tests
    +-- Human reviews per-BSpec statistics (logic passes, integration passes, failures)
    +-- POST /api/climate_advisor/approve_pending_test { scenario_name }
            -> simulate.py --sign <name>
            -> mv pending/<name>.json -> golden/<name>.json
            -> simulate.py --check-integrity
            -> result written to results/approved.json
```

---

## Promotion Gate

Promotion is a human action, not automatic. The loop accumulates evidence; the human decides.

The Dashboard Tests tab shows:

| Name | Class | Track | Runs | Logic | Int. | Fail | Last | Action |
|---|---|---|---|---|---|---|---|---|
| away_cool_setback | setpoint_mismatch | both | 3 | 2/3 | -/3 | 1 | 5m | [Approve] |

The human reviews:
1. Pass rate on both tracks -- a BSpec with consistent logic passes and 0 integration passes may need more production cycles
2. The scenario card output (`simulate.py --pending -v`) to confirm the assertions represent real HVAC behavior
3. Whether the scenario can be reproduced (not a one-time anomaly)

Clicking Approve runs:
1. `simulate.py --sign <name>` -- updates MANIFEST.json
2. `mv pending/<name>.json golden/<name>.json`
3. `simulate.py --check-integrity` -- confirms clean
4. Result written to results/approved.json

After approval, the user merges the worktree's updated golden/ into main via PR. This keeps the promotion in git history.

---

## Results Storage

`tools/simulations/results/` is gitignored. It is ephemeral state, not part of the repo.

Files in results/:
- `results/<name>/<ts>-logic.json` -- one Logic Validation Record per loop run
- `results/<name>/<ts>-integration.json` -- one Integration Validation Record per loop run
- `results/pending_stats.json` -- rebuilt each loop run; read by GET /pending_tests
- `results/processed_incidents.json` -- dedup tracker; prevents re-processing the same incident
- `results/approved.json` -- history of human approvals

Only scenario JSON files (pending/, golden/) are tracked in git.

---

## Worktree Context

All simulation loop work operates from a dedicated worktree:

```
c:/Users/David/Documents/VSCode Projects/ClimateAdvisor-simulation-loop/
    tools/simulation_loop.py          -- the loop agent
    tools/build_historical_scenario.py  -- incident -> BSpec builder
    tools/simulate.py                 -- enhanced with new assertion types + ODE
    tools/simulations/
        pending/                      -- auto-generated BSpecs land here
        golden/                       -- approved BSpecs
        results/                      -- Validation Records (gitignored)
        synthetic/                    -- hand-authored reference scenarios
```

The approve API endpoint writes to this worktree. The schedule skill registers `python tools/simulation_loop.py --once --hours 1` to run every 30 minutes.
