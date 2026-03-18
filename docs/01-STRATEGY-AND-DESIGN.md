# Climate Advisor — Strategy & Design Rationale

## The Problem

David's wife frequently forgets the heater is on, resulting in expensive heating bills. The household has Home Assistant with a thermostat (with presence sensing), weather forecasts, and most door/window sensors.

## Core Philosophy

The goal isn't just "turn off the heater" — it's **stop heating (or cooling) an empty or already-warm-enough house** while keeping things comfortable when someone is home. The automation should be invisible when it's working well.

## Layered Approach

The system uses five layers, each adding intelligence:

### Layer 1: Temperature-Aware Shutoff
Use outdoor temperature and forecast to decide when HVAC is unnecessary. If the outdoor temp is approaching the setpoint and the forecast shows it will exceed it, stop heating and let passive solar/ambient warming handle it.

### Layer 2: Occupancy-Based Logic
When nobody's home (detected by thermostat presence sensing), drop to a protection temperature. On return, restore comfort. The key tunable is the delay before setback (default 15 min) and the setback depth.

### Layer 3: Door/Window Awareness
Pause HVAC when monitored doors/windows are open for more than a threshold (default 3 min). Resume when all close. Even partial sensor coverage catches most waste scenarios.

### Layer 4: Runaway Protection
Safety net for the "forgot it was on" scenario. Maximum continuous runtime alerts, daily runtime budgets, and notifications when setpoint is bumped unusually high.

### Layer 5: Smart Scheduling
Morning warm-up before wake time, bedtime setback for sleeping, and forecast-aware adjustment of these schedules.

## Forecast as a Decision Engine

The four data points — today's high, today's low, tomorrow's high, tomorrow's low — provide a **trend direction** and **magnitude of change**. This trend modifies all automation behavior:

### Warming Trends (tomorrow warmer than today)
- More aggressive overnight setback (tomorrow handles recovery)
- Earlier heater shutoff in the morning
- On transition days, pre-position for cooling mode

### Cooling Trends (tomorrow colder than today)
- Pre-heat in the evening to bank thermal energy
- Less aggressive overnight setback (harder recovery in cold)
- On extreme drops, temporarily relax shutoff thresholds

## Day Type Classification

Every day is classified into one of five types based on today's high:

| Type | Threshold | Primary Strategy |
|------|-----------|-----------------|
| Hot  | ≥ 85°F   | AC pre-cool, sealed house |
| Warm | 75–84°F  | HVAC off, windows + AC standby |
| Mild | 60–74°F  | HVAC off, windows, heat in AM/PM |
| Cool | 45–59°F  | Heat with midday break |
| Cold | < 45°F   | Heat all day, conservation mode |

## The Daily Briefing

A single morning email/notification that:
1. States the weather and day type in plain language
2. Lists specific human actions with times and reasons
3. Explains what the automation handles silently
4. Describes what happens if they leave the house
5. Includes learning suggestions when available

The briefing **is** the user interface. The wife doesn't need to know about 15 automations — she reads one email and does 2–3 things.

## The Learning Engine

After 14+ days of data, the learning engine analyzes patterns:

| Pattern | Detection | Suggestion |
|---------|-----------|------------|
| Windows recommended but rarely opened | Compliance < 30% | Switch to HVAC-only strategies |
| Frequent manual thermostat overrides | > 10 in 14 days | Analyze and suggest new setpoints |
| High runtime on mild/warm days | > 2 hours avg | Add sensors or adjust setpoints |
| Short departures (30-45 min) | > 5 in 14 days | Shorten setback delay or skip for short trips |
| Comfort violations | > 30 min on 5+ days | Reduce setback aggressiveness |
| Frequent door pauses | > 20 in 14 days | Identify problem doors, adjust timing |

## Graceful Degradation

If humans don't follow the briefing, the automations are the safety net. If "open windows at 10am" is ignored, the system notices indoor temp climbing and can send a reminder or kick on AC as a fallback. Following the briefing is optimal; ignoring it doesn't cause discomfort.

## Including AC (Cooling)

The system handles both heating and cooling:
- Forecast-based pre-cooling on hot days
- Shoulder season mode switching (heat AM → cool PM)
- Door/window pause applies to both modes
- "Just open windows" notifications when HVAC isn't needed
- Separate comfort and setback setpoints for heat vs. cool
