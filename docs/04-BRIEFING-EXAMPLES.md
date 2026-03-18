# Climate Advisor — Daily Briefing Examples

These are example briefings for each day type, showing the tone, structure, and content the system should produce. The briefing is the primary user interface — it should feel like a helpful friend, not a technical system.

## Structure (every briefing)

1. Header with today/tomorrow temps and day type
2. Time-blocked plan with human actions and automated actions clearly labeled
3. "If you leave the house" section
4. "Doors & windows" reminder
5. "Looking ahead" section with trend preview
6. Learning suggestions (when available, after 14+ days)

---

## Example: Mild Day with Warming Trend

**Conditions:** Today high 68°F, low 48°F, tomorrow high 78°F

```
🏠 Your Home Climate Plan for Today
========================================

Today: High 68°F / Low 48°F
Tomorrow: High 78°F / Low 58°F
Day Type: Mild | Trend: Significantly warmer tomorrow (+10°F)

😊 MILD DAY PLAN — The Sweet Spot!
----------------------------------------

🌅 Early Morning (6:30 AM)
  The heater warmed the house to 70°F before sunrise.
  It's now off for the day — outdoor temps will do the rest.
  ✅ Already handled automatically.

🪟 By 10:00 AM — Open windows (south and east side first)
  Outside air will be around 60°F and climbing. Natural cross-breeze
  freshens the air and gently warms the house for free.

🏠 Midday through Afternoon
  This is the sweet spot. No HVAC needed at all. Enjoy it!

🌆 By 5:00 PM — Close the windows
  Temps will start dropping with the sun. Close up to trap the warmth.
  The house should coast comfortably through dinner.

🌙 Evening (10:30 PM)
  If indoor temp drops below 68°F, the heater will gently kick on.
  ✅ Handled automatically.

🚗 If You Leave the House
----------------------------------------
  15 minutes after you leave, the system will drop to 60°F.
  When you return, it will warm back up immediately.
  Expect about 20–30 minutes to feel normal depending on how
  long you were away. No need to touch the thermostat.

🚪 Doors & Windows
----------------------------------------
  If any monitored door or window stays open for more than 3 minutes,
  the HVAC will pause and you'll get a notification. It resumes
  automatically once everything is closed.

🔮 Looking Ahead
----------------------------------------
  Tomorrow is warmer (high of 78°F), so tonight's setback
  will be a bit more aggressive than usual. The warming trend
  means less heating needed overnight — saving energy while you sleep.
```

---

## Example: Hot Day

**Conditions:** Today high 95°F, tomorrow high 92°F

```
🏠 Your Home Climate Plan for Today
========================================

Today: High 95°F / Low 72°F
Tomorrow: High 92°F / Low 70°F
Day Type: Hot | Trend: Stable

☀️  HOT DAY PLAN
----------------------------------------

🌅 Early Morning (before 6:30 AM)
  The AC pre-cooled the house to 73°F while outdoor air
  was still cool. This banking strategy saves significant energy.
  ✅ Already handled automatically.

🏠 All Day — Keep the house sealed up
  • Keep ALL windows and doors closed.
  • Close blinds on sun-facing windows, especially west-facing after noon.
  • The AC will maintain your comfort at 75°F.

🌆 Evening
  If outdoor temps drop below 75°F after sunset, you'll get a
  notification that it's safe to open windows and turn off the AC.
  ✅ Automation will handle the AC shutoff if you open windows.

🚗 If You Leave the House
----------------------------------------
  15 minutes after you leave, the system will let it drift up to 80°F.
  When you return, it will pull it back down immediately.
  Expect about 20–30 minutes to feel normal depending on how
  long you were away. No need to touch the thermostat.
```

---

## Example: Cold Day with Cooling Trend

**Conditions:** Today high 38°F, low 22°F, tomorrow high 30°F

```
🏠 Your Home Climate Plan for Today
========================================

Today: High 38°F / Low 22°F
Tomorrow: High 30°F / Low 18°F
Day Type: Cold | Trend: Significant cold front coming (-8°F)

🥶 COLD DAY PLAN — Conservation Mode
----------------------------------------

The heater is your best friend today. Help it out:

🌅 All Day — Heat runs at 70°F
  • Keep ALL doors and windows CLOSED.
  • Minimize how long exterior doors stay open.
  • Close curtains on north-facing windows for insulation.
  • Open curtains on south-facing windows to capture solar heat.

🌡️  Pre-Heating Tonight
  Tomorrow is even colder. Starting at 7:00 PM, the system will
  bank extra heat by raising the setpoint to 73°F
  for a couple hours, then coast into the night.
  ✅ Automatic — the house will feel extra cozy this evening.

🌙 Bedtime
  Conservative setback tonight — dropping to 67°F instead of the
  usual 60°F, since recovery is harder in extreme cold.
  ✅ Automatic.

🚗 If You Leave the House
----------------------------------------
  15 minutes after you leave, the system will drop to 60°F.
  When you return, it will warm back up immediately.
  Expect about 20–30 minutes to feel normal depending on how
  long you were away. No need to touch the thermostat.

🔮 Looking Ahead
----------------------------------------
  Tomorrow is cooler (high of 30°F), so the system will
  bank extra warmth this evening and use a gentler setback overnight.
  You might notice the house feeling a touch warmer than usual
  before bed — that's intentional.
```

---

## Example: Learning Suggestion Appended to Briefing

After 14+ days, a briefing might end with:

```
💡 Suggestions Based on Recent Patterns
----------------------------------------
  • Over the past 18 days where opening windows was recommended,
    they were opened only 22% of the time. Would you like Climate
    Advisor to stop suggesting window actions and instead rely on
    HVAC with optimized schedules? This uses slightly more energy
    but requires no manual action.

  • You've manually adjusted the thermostat 14 times in the past
    two weeks. This may indicate the comfort setpoints don't match
    your preferences. Would you like Climate Advisor to analyze
    the override patterns and suggest new setpoints?

Reply ACCEPT or DISMISS to any suggestion, or ignore to keep current behavior.
```

## Tone Guidelines

- Friendly, not technical
- Explain *why* for every human action
- Mark automated actions with ✅ so the human knows they can ignore them
- Keep it scannable — someone should be able to glance at it in 30 seconds and know what they need to do
- Use emoji sparingly but consistently for visual anchoring
- Never use jargon like "setback" or "HVAC mode" in the briefing — use plain language like "the heater will lower to 67°F"
