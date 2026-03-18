# Climate Advisor — Learning Engine Design

## Purpose

The learning engine is what makes Climate Advisor more than a fancy scheduler. It observes the gap between what was recommended and what actually happened, and uses that gap to suggest improvements. The long-term goal is a system that adapts to the household's real behavior rather than insisting on idealized behavior.

## Core Principle

**If you give people a behavior to perform and they rarely do it, calculate a better alternative that can be performed and ask the user if they'd like to switch.**

## Data Collection

Every day, a `DailyRecord` is saved with:

### What Was Recommended
- Day type and trend
- Whether windows were recommended (and suggested open/close times)
- Recommended HVAC mode

### What Actually Happened
- Whether windows were opened (and actual times)
- Total HVAC runtime in minutes
- Time spent away from home
- Number of door/window pause events
- Number of manual thermostat overrides

### Outcomes
- Average indoor temperature
- Minutes spent outside comfort range (comfort violations)
- Estimated energy cost (future: from utility rate integration)

### Suggestion Tracking
- Was a suggestion sent today?
- User response: accepted, dismissed, or ignored

## Storage

- JSON file in HA config directory (`climate_advisor_learning.json`)
- Rolling 90-day window (older records pruned automatically)
- Persists across HA restarts

## Suggestion Generation

Suggestions are generated when `generate_suggestions()` is called (typically during the daily briefing). Requires minimum 14 days of data.

### Pattern: Low Window Compliance

**Detection:** Of days where windows were recommended, compliance < 30%.
**Suggestion:** "Would you like Climate Advisor to stop suggesting window actions and instead rely on HVAC with optimized schedules?"
**If accepted:** `disable_window_recommendations = True` — the briefing stops suggesting window actions, and the automation engine uses HVAC for those day types instead.
**Trade-off:** Slightly higher energy use, zero human effort required.

### Pattern: Frequent Manual Overrides

**Detection:** More than 10 manual thermostat adjustments in 14 days.
**Suggestion:** "Would you like Climate Advisor to analyze the override patterns and suggest new setpoints?"
**If accepted:** System flags for setpoint analysis (future: actually analyze the override direction and magnitude to suggest specific new setpoints).
**Trade-off:** May result in slightly different comfort baseline.

### Pattern: High Runtime on Mild/Warm Days

**Detection:** Average HVAC runtime > 120 minutes on mild/warm day types.
**Suggestion:** "Would you like to add more door/window sensors, or adjust the setpoints for mild days?"
**Root cause:** Usually indicates doors/windows being left open without sensors, or setpoints that are too aggressive for the house's thermal characteristics.

### Pattern: Short Departures

**Detection:** More than 5 departures of 30–45 minutes in 14 days.
**Suggestion:** "Would you like to shorten the setback delay from 15 minutes to 5 minutes, or skip setback for departures under 1 hour?"
**Insight:** The current 15-minute delay means the setback barely takes effect before the person returns, wasting the recovery energy. Options: faster setback (saves more) or skip setback (saves the recovery penalty).

### Pattern: Comfort Violations

**Detection:** Indoor temp outside comfort range for > 30 minutes on 5+ of the last 14 days.
**Suggestion:** "Would you like to reduce the setback aggressiveness, or start the morning warm-up earlier?"
**If accepted:** Setback modifier increases by 2°F (less aggressive), and/or morning pre-heat starts 15 minutes earlier.

### Pattern: Frequent Door Pauses

**Detection:** More than 20 HVAC pause events from door/window sensors in 14 days.
**Suggestion:** "If a specific door is the main culprit, would you like to extend the pause delay for that door, or exclude it from monitoring?"
**Future enhancement:** Track which specific sensor triggers most pauses and name it in the suggestion.

## Suggestion Lifecycle

1. **Generated:** During daily briefing generation, based on pattern analysis
2. **Delivered:** Appended to the daily briefing email/notification
3. **Response:** User can accept, dismiss, or ignore
   - **Accept:** Config changes are applied, recorded in settings_history
   - **Dismiss:** Suggestion key added to dismissed list, won't reappear for SUGGESTION_COOLDOWN_DAYS (7)
   - **Ignore:** Suggestion reappears next day until acted on or pattern resolves

## Future Learning Capabilities (v0.3+)

### Thermal Model Learning
Track how quickly the house heats/cools under different conditions to build a simple thermal model. This enables:
- More accurate recovery time estimates
- Better pre-heat/pre-cool timing
- Optimized setback depth based on actual house performance

### Seasonal Baselines
After a full year of data, establish seasonal baselines for runtime, comfort scores, and energy use. Detect anomalies (e.g., "Your heating runtime this November is 30% higher than last November — possible insulation issue or thermostat drift").

### Override Pattern Analysis
Instead of just counting overrides, analyze their direction and timing:
- "You consistently raise the temperature 2°F around 3pm" → suggest a scheduled bump
- "You lower the temperature every night before your usual bedtime" → suggest moving bedtime setback earlier

### Energy Cost Integration
With utility rate data, convert runtime minutes to estimated cost and show daily/weekly/monthly savings compared to "no automation" baseline.

## Configuration Changes Applied by Suggestions

| Suggestion Key | Config Changes |
|---------------|---------------|
| `low_window_compliance` | `disable_window_recommendations: true` |
| `frequent_overrides` | `request_setpoint_analysis: true` |
| `short_departures` | `occupancy_setback_minutes: 5` |
| `comfort_violations` | `setback_modifier: +2`, `morning_preheat_offset_minutes: 15` |
| `frequent_door_pauses` | `door_pause_seconds: 300` |

## Compliance Summary API

The `get_compliance_summary()` method returns a dict suitable for sensor attributes:

```python
{
    "status": "active",            # or "collecting_data"
    "days_recorded": 28,
    "window_compliance": 0.35,     # 35% of recommended days
    "avg_daily_hvac_runtime_minutes": 145.5,
    "comfort_score": 0.92,         # 92% of time in comfort range
    "total_manual_overrides": 8,
    "pending_suggestions": 2,
}
```
