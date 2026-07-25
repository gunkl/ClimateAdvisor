<!-- Nav: ← [Architecture Reference](02-ARCHITECTURE-REFERENCE.md) -->

# Climate Advisor — Daily Briefing Examples

These are example briefings for each day type, showing the tone, structure, and content the system produces. The briefing is the primary user interface — it should feel like a message from a helpful friend who manages your home climate, not a technical report.

## Anchors
| Question | Short answer | → Full answer |
|---|---|---|
| What sections appear in every briefing and in what order? | Header (temps + day type + trend) → TLDR table → conversational day plan → "If you head out" → "Fresh air" → grace period status (if active) → "Looking ahead" → learning suggestions (if available). | [§Structure (every briefing)](04-BRIEFING-EXAMPLES.md#structure-every-briefing) |
| When does the grace period paragraph appear in a briefing, and what language does it use? | Only when a grace period is active at send time. Manual grace uses "hands-off window"; automation grace uses "settling period". The word "grace period" is never used in the body text. | [§Example: Active Automation Grace Period](04-BRIEFING-EXAMPLES.md#example-active-automation-grace-period) |
| What triggers the adaptive thermal timing sentence in the briefing footer? | `adaptive_thermal_active=True` in coordinator data — set when thermal model confidence is "low" or better for both heat and cool rates. The sentence is absent while the model is still collecting data. | [§Example: Adaptive Thermal Timing Active](04-BRIEFING-EXAMPLES.md#example-adaptive-thermal-timing-active) |
| What are the key voice rules for briefing text? | First person from the system ("I'll…"), affirm user choices first then explain system response, cause-and-effect framing, numerals for all temps/times, no emoji in the conversational body, scannable in 30 seconds. | [§Voice Rules](04-BRIEFING-EXAMPLES.md#voice-rules) |
| How does the fresh air paragraph differ by HVAC mode? | When HVAC is off: "no energy impact at all". When HVAC is heating or cooling: explains the debounce duration, says the system will pause after that time, and suggests minimizing impact (shorter bursts, close other windows). | [§Example: Mild Day with Warming Trend](04-BRIEFING-EXAMPLES.md#example-mild-day-with-warming-trend) |

## Structure (every briefing)

1. Structured header with today/tomorrow temps, day type, and trend (scannable at a glance)
2. TLDR summary table — plain-text aligned key/value rows (Day Type, HVAC Mode, Windows, Bedtime Setback, Tomorrow) for quick scanning in push notifications and email
3. Conversational body — flowing prose paragraphs covering the day plan, what the system will do, and what the user can do to help
3. "If you head out" paragraph — occupancy setback behavior
4. "Fresh air" paragraph — affirms the user's right to open a window anytime, explains what the system will do in response using the actual configured debounce duration, describes the impact on the day's climate strategy, and suggests how to minimize that impact. Varies by HVAC mode.
5. Grace period status paragraph — only included when a grace period is currently active at briefing time. Explains the hands-off window (manual) or settling period (automation) in plain language so the user understands why door/window sensing is temporarily modified.
6. "Looking ahead" paragraph — trend-based preview of tonight/tomorrow
7. Learning suggestions (when available after 14+ days, kept structured for accept/dismiss clarity)

---

## Example: Mild Day with Warming Trend

**Conditions:** Today high 68°F, low 48°F, tomorrow high 78°F

```
🏠 Your Home Climate Plan for Today
========================================

Today: High 68°F / Low 48°F
Tomorrow: High 78°F / Low 58°F
Day Type: Mild | Trend: Significantly warmer tomorrow (+10°F)

  Day Type:        Mild (68°F)
  HVAC Mode:       Heat at 70°F
  Windows:         Open 10 AM – 5 PM
  Bedtime Setback: 66°F at 10:30 PM
  Tomorrow:        Significantly warmer tomorrow (+10°F) (78°F)

This is the good stuff — a day where the house practically takes care of itself. I ran the heater to 70°F before sunrise, and now it's off for the day. The weather does the rest.

By 10:00 AM, the outside air will be around 60°F and climbing. If you open windows on the south and east side first, you'll get a natural cross-breeze that freshens the air and warms the house for free.

Through the afternoon, no HVAC needed at all. Enjoy the break!

Close up the windows by 5:00 PM to trap the warmth before the sun drops. The house should coast comfortably through dinner. If it dips below 68°F later tonight, I'll gently kick the heater back on — but that's all automatic.

If you head out, nothing really changes today — the HVAC is off. If it was running as a safety net, it'll set back on its own.

If you want to open a window for some fresh air, go for it — the HVAC is off today so there's no energy impact at all. Enjoy the breeze. If the system does need to kick on as a safety net later and a window is still open, I'll give it 10 minutes and then pause until you close up.

Looking ahead — tomorrow's warmer at 78°F, so I'm going to set back a bit more aggressively tonight. Less heating needed means energy saved while you sleep.
```

---

## Example: Warm Day

**Conditions:** Today high 80°F, low 60°F, tomorrow high 82°F. Window times below are the
static `WARM_WINDOW_OPEN_HOUR`/`WARM_WINDOW_CLOSE_HOUR` constants (6 AM–10 AM, see
[§7. Window Recommendations](08-COMPUTATION-REFERENCE.md#7-window-recommendations)); when
ODE forecast data is available, the close time instead comes from `nat_vent_cutoff`
(Issue #518) — the header and body always show the same value, whichever source is used.

```
🏠 Your Home Climate Plan for Today
========================================

Today: High 80°F / Low 60°F
Tomorrow: High 82°F / Low 62°F
Day Type: Warm | Trend: Stable

  Day Type:        Warm (80°F)
  HVAC Mode:       Off — windows day
  Windows:         Open 6 AM – 10 AM
  Bedtime Setback: No setback
  Tomorrow:        Stable (82°F)

Open windows around 6:00 AM to catch the cool morning air. Close up at 10:00 AM — after that the outdoor air will be warmer than inside.

Indoor temps are forecast to reach 75°F around 2:30 PM. Once windows are closed, the AC will step in automatically if it's needed to hold that ceiling.

If you head out, nothing really changes today — the HVAC is off. If it was running as a safety net, it'll set back on its own.

If you want to open a window for some fresh air, go for it — the HVAC is off today so there's no energy impact at all. Enjoy the breeze. If the system does need to kick on as a safety net later and a window is still open, I'll give it 10 minutes and then pause until you close up.

Tomorrow looks pretty similar to today — 82°F for a high. Nothing special planned overnight.
```

Note: the AC-as-backup fact is stated exactly once — the forecast/ceiling sentence ties it
to windows being closed, and the fresh-air paragraph owns the window-open debounce/pause
guard (matching the real `automation.py` `apply_classification()` `DEFER_PAUSED` behavior).
Earlier versions of this example independently promised a specific AC start clock-time with
no window-state awareness — that was the root cause of Issue #518 and has been corrected.

---

## Example: Hot Day

**Conditions:** Today high 95°F, low 72°F, tomorrow high 92°F

```
🏠 Your Home Climate Plan for Today
========================================

Today: High 95°F / Low 72°F
Tomorrow: High 92°F / Low 70°F
Day Type: Hot | Trend: Stable

  Day Type:        Hot (95°F)
  HVAC Mode:       Cool at 75°F
  Windows:         Closed all day
  Bedtime Setback: 78°F at 10:30 PM
  Tomorrow:        Stable (92°F)

I got a head start on the heat this morning. The AC pre-cooled the house to 73°F while the outdoor air was still cool — that banking strategy saves a lot of energy over the course of the day.

Today's a keep-it-sealed kind of day. Keep all windows and doors closed, and if you can, close the blinds on sun-facing windows (especially west-facing ones after noon). I'll hold things at 75°F all day — you shouldn't need to touch anything.

If outdoor temps drop below 75°F after sunset, I'll send you a heads-up that it's safe to open windows and give the AC a rest. If you do open up, I'll handle shutting the AC off automatically.

If you head out, no worries. After about 15 minutes I'll let the house drift up to 80°F to save energy. When you're back, I'll pull it right back down — give it 20 to 30 minutes to feel normal again.

If you want to crack a window for some fresh air, no problem — it's your house. I'll keep the AC running for a bit in case it's just a quick thing, but if it stays open past 10 minutes I'll shut the AC off so you're not cooling the outdoors. Once you close up, I'll fire the AC back up right away. Just know that on a day like today it may take a bit longer to pull back down to 75°F, so if you want to minimize the impact, shorter is better — and try to keep other windows and doors shut while you've got one open.

Tomorrow looks pretty similar to today — 92°F for a high. Nothing special planned overnight.
```

---

## Example: Cool Day with Cooling Trend

**Conditions:** Today high 55°F, low 35°F, tomorrow high 50°F

```
🏠 Your Home Climate Plan for Today
========================================

Today: High 55°F / Low 35°F
Tomorrow: High 50°F / Low 30°F
Day Type: Cool | Trend: Cooling trend (-5°F)

  Day Type:        Cool (55°F)
  HVAC Mode:       Heat at 70°F
  Windows:         Closed all day
  Bedtime Setback: 66°F at 10:30 PM
  Tomorrow:        Cooling trend (-5°F) (50°F)

It's a heater day. I'll keep the house at 70°F through the morning — it's too cool outside for windows today, so we're staying sealed up.

Between about 11am and 3pm, I'll ease the setpoint back a couple degrees to ride whatever solar gain the house picks up through the windows. You won't notice the difference, but it saves a bit of energy.

After 3pm I'll bring it back to 70°F as the sun drops. At bedtime, I'll set things to 66°F for sleeping — most people sleep better a little cooler.

If you head out, I'll drop to 60°F after about 15 minutes. When you get back, I'll warm things right up — should take 20 to 30 minutes depending on how long you were gone.

If you want to open a window for some fresh air, no problem — go for it. I'll keep the heat running for a bit in case you're just airing things out, but if it stays open past 10 minutes I'll turn the heat off so we're not heating the neighborhood. Once you close up, the heat kicks right back on. It'll take a little extra energy to warm back up, so if you want to minimize the impact, a quick burst of fresh air works great — and closing doors to the room with the open window helps keep the rest of the house comfortable while you do it.

Looking ahead — tomorrow's cooler at 50°F, so I'll bank some extra warmth this evening and go easy on the overnight setback. If the house feels a touch warmer than usual before bed, that's intentional.
```

---

## Example: Cold Day with Cooling Trend

**Conditions:** Today high 38°F, low 22°F, tomorrow high 28°F

```
🏠 Your Home Climate Plan for Today
========================================

Today: High 38°F / Low 22°F
Tomorrow: High 28°F / Low 12°F
Day Type: Cold | Trend: Significant cold front coming (-10°F)

  Day Type:        Cold (38°F)
  HVAC Mode:       Heat at 70°F
  Windows:         Closed all day
  Bedtime Setback: 66°F at 10:30 PM
  Tomorrow:        Significant cold front coming (-10°F) (28°F)

It's going to be cold out there. The heater runs all day at 70°F, and you can help it out — keep doors and windows closed, minimize how long you hold exterior doors open, and close curtains on the north side. If you have south-facing windows, open those curtains to grab some free solar heat.

Tomorrow's even colder, so I'm going to bank some extra heat this evening. Starting around 7pm, I'll bump the setpoint up to 73°F for a couple hours, then coast into the night. The house will feel extra warm before bed — that's on purpose.

Tonight I'm using a conservative setback — 67°F instead of the usual 60°F. When it's this cold, a deeper setback takes too long to recover from in the morning.

If you head out, I'll drop to 60°F after about 15 minutes. When you get back, I'll warm things right up — should take 20 to 30 minutes depending on how long you were gone.

If you want to open a window for some fresh air, no problem — go for it. I'll keep the heat running for a bit in case you're just airing things out, but if it stays open past 10 minutes I'll turn the heat off so we're not heating the neighborhood. Once you close up, the heat kicks right back on. It'll take a little extra energy to warm back up, so if you want to minimize the impact, a quick burst of fresh air works great — and closing doors to the room with the open window helps keep the rest of the house comfortable while you do it.

Looking ahead — tomorrow's cooler at 28°F, so I'll bank some extra warmth this evening and go easy on the overnight setback. If the house feels a touch warmer than usual before bed, that's intentional.
```

---

## Example: Active Automation Grace Period

**When shown:** The briefing fires (e.g., 6:00 AM) and an automation grace period is still running because Climate Advisor resumed HVAC after all doors/windows closed sometime before the briefing.

This paragraph appears between the "fresh air" section and the "looking ahead" section:

```
One heads-up: I just resumed the HVAC after all the doors and windows closed, so I'm in a 5 minute settling period. During that time, opening a door or window briefly won't immediately pause things again — this prevents the system from cycling on and off if you're moving in and out. After the settling period, normal door/window sensing resumes.
```

---

## Example: Active Manual Grace Period

**When shown:** The briefing fires and a manual grace period is still running because the user manually turned HVAC back on during a pause sometime before the briefing.

```
One heads-up for this morning: you manually turned the HVAC back on earlier, so I'm in a 30 minute hands-off window right now. During that window, opening a door or window won't trigger a pause — I'm giving you space to settle in without the system jumping in. Once the window closes, door/window sensing goes back to normal.
```

**Note:** Grace period paragraphs only appear in the briefing when a grace period is actually active at send time. Most mornings they are absent.

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

---

## Example: Adaptive Thermal Timing Active

**When shown:** The thermal model has sufficient confidence (`"low"` or better) for both heating and cooling rates. The `adaptive_thermal_active=True` flag is set in the coordinator data. The adaptive timing sentence appears at the end of the briefing footer, after the "Looking ahead" paragraph.

```
Bedtime setback and pre-heat timing are tuned to your home's actual heating performance.
```

This sentence is only included when `adaptive_thermal_active` is `True`. When the model is still collecting data (confidence `"none"`), the sentence is absent — no explanation of the absence is shown to avoid confusion.

---

## Example: Forecast Bias Suggestion

**When shown:** The learning engine's `get_weather_bias()` method finds a bias magnitude exceeding 3°F at `"medium"` or higher confidence. This suggestion appears in the learning suggestions block appended to the daily briefing.

Example (weather service running warm):

```
💡 Suggestions Based on Recent Patterns
----------------------------------------
  • Your weather service appears to run warm by about 4°F on average
    — I've seen this across 22 recent days. I've adjusted my timing
    calculations to compensate. No action needed on your end, but you
    may notice my pre-heat and setback timing has shifted slightly.
```

Example (weather service running cool):

```
💡 Suggestions Based on Recent Patterns
----------------------------------------
  • Your weather service appears to run cool by about 3°F on average
    across the past 15 days. I've adjusted my timing calculations to
    compensate. If you feel the house is too warm or too cool at key
    times of day, this correction may resolve it.
```

The suggestion key is `forecast_bias_significant`. It is subject to the standard `SUGGESTION_COOLDOWN_DAYS = 7` cooldown and will not reappear until the bias remains significant after the cooldown expires.

---

## Voice Rules

- First person from the system: "I'll turn on the AC", not "the system will"
- Always "you" for the user, never "the homeowner"
- User-centric framing: affirm the user's choices first ("no problem — it's your house"), then explain what the system does in response
- Cause-and-effect: explain *why* before asking the user to do something ("close windows by noon so the AC can work efficiently")
- Short paragraphs (2-4 sentences max)
- Numerals for all temps and times (not spelled out)
- No emoji in the conversational body (emoji only in the structured header and learning suggestions)
- Key times and temperatures should appear in prominent sentence positions where they naturally draw the eye
- Scannable in 30 seconds — someone should be able to glance and know what they need to do today
- Configuration-accurate durations: the fresh air section always shows the actual configured debounce duration, not a hardcoded value. Grace period paragraphs show the actual configured grace duration.
- Grace periods use plain language: "hands-off window" for manual grace (framed as giving the user space), "settling period" for automation grace (framed as preventing cycling). Avoid the word "grace period" in the body text — it sounds technical.
