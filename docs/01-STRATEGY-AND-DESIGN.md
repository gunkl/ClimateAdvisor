<!-- Nav: ← [Project Instructions](00-PROJECT-INSTRUCTIONS.md) → [Architecture Reference](02-ARCHITECTURE-REFERENCE.md) -->

# Climate Advisor — Strategy & Design Rationale

This doc explains *why* Climate Advisor behaves the way it does. Exact formulas, constants and edge cases live in the [Computation Reference](08-COMPUTATION-REFERENCE.md) and the Tier 3 specs linked below; this doc links to them rather than repeating them.

## Anchors
| Question | Short answer | → Full answer |
|---|---|---|
| What does Climate Advisor do on a warm or mild day? | It holds the comfort band. It does not turn the HVAC off. CA writes one heat or cool setpoint at the nearest band edge, and the thermostat holds it (#249, #301). | [§Holding the Comfort Band](#holding-the-comfort-band) |
| What are the household rules? | 1: the whole-house fan (WHF) never runs in a sealed house (stopped within 30 s). 2: the WHF and the HVAC never run together, and the newest action wins. 3: no heating or cooling into a window open past its delay, unless there is a manual override. | [§Household Rules](#household-rules) |
| How does trend direction change behavior? | A trend of 5°F or more into tomorrow is moderate, and 10°F or more is significant. A cooling trend schedules an evening pre-heat. A warming trend deepens the overnight pre-cool. | [§Forecast as a Decision Engine](#forecast-as-a-decision-engine) |
| What are the occupancy priority rules? | Guest → Vacation → Home/Away → default Home. Guest overrides away and vacation. All toggles are optional, and each one has an invert option. | [§Occupancy](#occupancy) |
| How long does CA respect a manual change? | 30 min by default (manual grace). After CA restores HVAC when a window closes, a 5 min automation grace applies. | [§Respecting the Occupant](#respecting-the-occupant) |
| When does the learning engine make suggestions? | After 14 days of data. A dismissed suggestion is not repeated for 7 days. | [§Learning](#learning) |

## The Problem

People forget the heater is on, and the heating bills are expensive. The household has Home Assistant, a thermostat, weather forecasts and door/window sensors on most openings.

## Core Philosophy

The goal isn't just "turn off the heater". It is to **stop heating (or cooling) an empty or already-comfortable house**, and to keep it comfortable when someone is home. The automation should be invisible when it works well. Comfort temperatures stand in for the real goal, which is that the people in the home are comfortable. When an occupant departs from the plan, treat it as a preference they have revealed, not as a failure.

## Holding the Comfort Band

On every day type, Climate Advisor programs a comfort band: the floor is `comfort_heat` and the ceiling is `comfort_cool` while someone is home and awake. Away, vacation and sleep use setback or sleep edges instead. A single-setpoint thermostat can guard only one edge at a time, so CA writes **one** setpoint: heat at the floor or cool at the ceiling, whichever edge the house is nearest (#301, #1066). The thermostat then holds that edge on its own between CA's 30 minute cycles.

Why: warm and mild days used to send `hvac_mode=off` and rely on guards to rescue the house when it drifted. Afternoon ceiling breaches kept recurring (#136, #218, #247). With the band always armed, those breaches are structurally impossible (#249). Natural ventilation runs with the band still armed. The outside air does the work for free, and the AC runs only if the breeze cannot hold the ceiling.

Detail: [Computation Reference §6e](08-COMPUTATION-REFERENCE.md#6e-comfort-band-programming-issue-249), [§6f](08-COMPUTATION-REFERENCE.md#6f-comfort-family-nearest-edge-arming-issue-1066--supersedes-827843858867-settling-guard-issue-1073), [Comfort-Edge Selection Spec](comfort-edge-selection-spec.md).

## Day Type Classification

Each day is classified by today's high. The thresholds can be adjusted.

| Type | Default threshold | Strategy |
|------|-----------|-----------------|
| Hot  | ≥ 85°F   | Cool. Keep the house sealed. Pre-cool overnight when it helps. Use window cooling (economizer) in cool mornings and evenings. |
| Warm | 75–84°F  | Hold the comfort band. Ventilate when outside air helps. |
| Mild | 60–74°F  | Hold the comfort band. Recommend windows when outside air helps. |
| Cool | 45–59°F  | Heat. |
| Cold | < 45°F   | Heat, and pre-heat ahead of colder days. |

Detail: [Computation Reference §1](08-COMPUTATION-REFERENCE.md#1-day-classification), [§7 Window Recommendations](08-COMPUTATION-REFERENCE.md#7-window-recommendations).

## Forecast as a Decision Engine

Today's and tomorrow's highs and lows give a **trend direction** (warming, cooling or stable) and a **magnitude**. A change of 5°F or more is moderate, and 10°F or more is significant.

- **Cooling trend:** pre-heat in the evening to bank heat before the cold arrives. The target is 2°F above `comfort_heat` for a moderate trend and 3°F above for a significant one. The start time is worked back from bedtime using the learned heating rate.
- **Warming trend:** cool deeper overnight, so the house banks cold mass before a hot day (overnight pre-cool).

The trend's setback modifier is used **only** by the overnight pre-cool target. It does not change the away, vacation or bedtime setbacks. Detail: [Computation Reference §2–§5](08-COMPUTATION-REFERENCE.md#3-setback-modifier).

## Household Rules

These rules are authoritative. Rules 1 and 2 apply only with a whole-house fan. Rule 3 applies to every home with door/window sensors.

1. **The WHF never runs in a sealed house.** With every door and window closed, a running WHF is stopped within 30 s, even under a manual or RF-remote override. CA also never *starts* the WHF on a sealed house.
2. **The WHF and the HVAC never run together, and the newest action wins.** A device-state backstop also checks this. If the fan and the heating/cooling both read on for about 10 s, one of them is stopped, whatever the button timing (#1288).
3. **No heating or cooling into a window open past its delay**, unless the occupant turned it on (a manual override).

A WHF therefore **needs at least one door/window sensor** (#1200). Without one, CA cannot tell an open house from a sealed one, so it cannot enforce rules 1 and 3. Setup warns about this, and the configuration is not supported.

Detail: [Fan Lifecycle Spec](fan-lifecycle-spec.md#rule-2-state-backstop-the-fan-and-the-heatingcooling-never-run-together-issue-1288), [Sealed-House Guard](08-COMPUTATION-REFERENCE.md#h-sealed-house-guard--override-sessions-are-not-exempt-issue-882), [Supported Configuration](fan-lifecycle-spec.md#supported-configuration-a-whole-house-fan-needs-a-doorwindow-sensor-issue-1200).

## Doors and Windows

An opening never pauses the HVAC immediately. Each sensor has a delay (default **10 min**, configurable). HVAC pauses only if the sensor is still open when its own delay runs out. Pausing instantly would thrash the HVAC on every brief door opening, so HVAC running during the delay is correct. The delay is judged per sensor: one sensor open past its own delay is enough, and a second door opening later does not hide it. HVAC resumes when every sensor has closed.

A sensor that goes **dark** (unavailable, unknown or missing) holds an existing pause if its last real reading was open. It never *starts* a pause (#1181). A silent sensor must not decide for the house in either direction.

When outdoor air helps, an open house runs natural ventilation through the configured fan instead of pausing. Detail: [Computation Reference §10](08-COMPUTATION-REFERENCE.md#10-doorwindow-hvac-pause), [Nat-Vent Lifecycle Spec](nat-vent-lifecycle-spec.md).

## Fans

The fan mode is `disabled`, `whole_house_fan` or `hvac_fan` (the thermostat's blower). The `both` mode is still in the code, but it is hidden from setup and **not supported** (#1175). Under `whole_house_fan`, a press of the thermostat's blower is the occupant's own business and is ignored. Detail: [Fan Lifecycle Spec](fan-lifecycle-spec.md), [Fan Remote Spec](fan-remote-spec.md).

## Respecting the Occupant

- **Manual change grace:** a change the occupant makes at the thermostat is kept for 30 min by default before CA resumes its plan. The latest manual change replaces any earlier one. A thermostat mode change is confirmed only after a short delay, so a transient that corrects itself is not treated as an override.
- **Automation grace:** for 5 min after CA restores HVAC when the windows close, a quick reopen does not cause rapid cycling.
- **Startup coalescing:** for 300 s after a restart, override detection is suppressed and the day's plan is applied when the window ends. Occupant changes made inside that window are **not supported** (owner decision, #1235). Do not "fix" this by replaying them. See [Computation Reference](08-COMPUTATION-REFERENCE.md#startup-coalescing-window--occupant-changes-are-not-supported-owner-decision-issue-1235).

Detail: [Grace Periods Spec](grace-periods-spec.md).

## Occupancy

Occupancy comes from optional toggle entities (`home_toggle_entity`, `vacation_toggle_entity`, `guest_toggle_entity`). Each one has an invert option (`*_toggle_invert`). The priority order is:

```
Guest     →  treated as home (comfort); overrides away and vacation
Vacation  →  the optional vacation setpoints, else setback plus VACATION_SETBACK_EXTRA (3°F), applied immediately (Issue #1347)
Away      →  standard setback, after a fixed 15 min delay
Default   →  home (when no toggle is configured)
```

Leaving home clears a manual override. Returning home restores comfort unless an override, a pending change or an open window owns the thermostat. Detail: [Occupancy Dispatch Spec](occupancy-dispatch-spec.md).

## Scheduling and Optional Features

- **Daily schedule:** warm up at wake time, set back at bedtime. With no sleep temperatures configured, the bedtime setback depth adapts to the learned thermal model. See [Computation Reference §5](08-COMPUTATION-REFERENCE.md#5-temperature-setpoints-by-context).
- **Bedroom (sleep) sensor:** while the sleep schedule is active, a separate sensor can drive comfort. CA then raises or lowers the hallway thermostat's setpoint in 2°F (1°C) steps, up to 4°F (2°C), so the equipment actually runs for the bedroom (#1194). See [Remote-sensor outer loop](08-COMPUTATION-REFERENCE.md#remote-sensor-outer-loop-on-the-thermostat-setpoint-issue-1194-increment-a).
- **TOU pre-conditioning:** before a scheduled high-cost period, CA banks heat or cold toward the home's existing comfort-band edge using the learned thermal rate. There is no separate temperature setting for it (#786). See [Scheduler Spec](scheduler-spec.md).
- **Multi-zone:** each thermostat is its own config entry, with its own learning, settings and dashboard. Nothing is shared between zones (#796). See [Multi-Zone Spec](multi-zone-spec.md).
- **Observe-only mode:** with the automation switch off, CA keeps planning and reporting but sends no commands, and it records nothing it did not send. See [Architecture Reference](02-ARCHITECTURE-REFERENCE.md#observe-only-mode-disable-automation).

## The Daily Briefing

The daily briefing is one morning notification (push or email) that:
1. states the weather and day type in plain language;
2. lists the specific human actions, with times and reasons (for example, when to open or close windows);
3. explains what the automation handles silently;
4. describes what happens if the occupants leave;
5. includes learning suggestions when there are any.

The briefing **is** the user interface. The occupant reads one message and does 2–3 things. See [Briefing Spec](briefing-spec.md) and [Briefing Examples](04-BRIEFING-EXAMPLES.md).

## Graceful Degradation and Self-Checks

If the occupants ignore the briefing, nothing uncomfortable happens. The armed comfort band keeps the house in range anyway, and following the briefing just saves energy. CA also watches itself:
- **Entity health:** if the thermostat, the weather source, a sensor or another configured entity disappears or stops responding, CA notifies once (with a daily reminder while it stays missing) and shows it on the Status card (#805). See [Entity Health Brief](entity-health-brief.md).
- **Household promises:** plain-English checks over real thermostat and fan readings, such as "never undo a manual change early". Most promises are log-only until they have been quiet for about two weeks. The WHF/AC overlap and sealed-house promises alert (#1128). See [Household Promises Spec](household-promises-spec.md).
- **Stall alert:** CA alerts when the house stays off target while CA is in control. See [Computation Reference](08-COMPUTATION-REFERENCE.md#house-off-target-stall-alert-issue-1050-part-of-1048).

## Learning

**Core principle:** if the system recommends something the occupants rarely do, it should work out a better alternative and ask.

- **Thermal model v3:** parallel observation types (heating, cooling, passive decay, fan-only, ventilated and solar gain) learn the home's physics. The model feeds pre-heat timing, bedtime depth, TOU lead times and predictions. See [Thermal Model v3 Spec](thermal-model-v3-spec.md).
- **Learned thermostat swing:** CA learns the thermostat's own hysteresis, as the median of recent heat-on or cool-on samples (#1234). See [Computation Reference §5e-vii](08-COMPUTATION-REFERENCE.md#5e-vii-thermostat-swing---the-thermostats-own-hysteresis-issue-102-redefined-by-issue-1234).
- **Suggestions:** these start after 14 days of data, and a dismissed suggestion waits 7 days before it can return. The patterns are low window compliance, frequent overrides, high runtime on mild or warm days, short departures, comfort violations and frequent door pauses. See [Learning Engine Design](05-LEARNING-ENGINE-DESIGN.md).

---

Contributor note: when you add a configuration option, also add it to `CONFIG_METADATA` in `const.py` so the dashboard Settings tab shows it (see [claude.md](../claude.md)).
