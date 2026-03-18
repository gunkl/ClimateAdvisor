# Climate Advisor — Home Assistant Automation UI Guide

This document captures the manual automation approach discussed before the integration was built. It's useful as a reference for understanding the logic, for testing individual behaviors, and as a fallback if someone wants to implement pieces without the full integration.

## Building Automations in the HA UI

Every automation in the UI has three sections:
- **Triggers**: What starts the automation
- **Conditions**: What must be true for it to proceed
- **Actions**: What it does

Found under: **Settings → Automations & Scenes → Create Automation → Create new automation**

## Automation 1: Outdoor Temp Rising — Shut Off Heater

**Trigger:** Numeric state → outdoor temp sensor → "Above" → 65°F (5° below comfort setpoint)
**Condition:** State → thermostat is in "heat" mode
**Action:** Call service → `climate.set_hvac_mode` → set to "off". Optional: send notification.

**Companion (evening restart):** Trigger when outdoor temp drops below threshold after 5pm → set thermostat back to "heat" mode.

## Automation 2: Nobody Home — Setback

**Trigger:** State → presence entity → changes to "not_home" → **"For" duration: 15-30 minutes**
**Condition:** (Optional) Numeric state → setpoint is above setback temp
**Action:** Call service → `climate.set_temperature` → setback temp (e.g., 60°F)

**Companion (return):** Trigger on state → "home" → Action: restore comfort setpoint. No delay.

## Automation 3: Door/Window Open — Pause Heating

**Trigger:** State → contact sensor → "open" → **"For" duration: 2-3 minutes**
Multiple sensors: Click "Add trigger" for each, trigger mode "any".

**Condition:** State → thermostat is in "heat" mode
**Action:** Call service → `climate.set_hvac_mode` → "off". Send notification.

**Companion (resume):** Trigger when sensors return to "closed" (For: 30 seconds).
**Important:** Add "And" condition checking ALL sensors are "closed" before resuming.

## Automation 4: Runaway Runtime Protection

**Trigger:** State → thermostat `hvac_action` attribute → equals "heating" → **"For": 2-3 hours**
**Condition:** Outdoor temp above 40°F (long runtime may be legitimate in extreme cold)
**Action:** Call service → `climate.set_temperature` → drop 3-4°F. Send notification.

## Automation 5: Bedtime Setback

**Trigger:** Time → 10:30pm
**Condition:** State → thermostat in "heat" mode
**Action:** Call service → `climate.set_temperature` → drop 3-5°F

**Companion (morning warm-up):** Trigger on Time → 30-60 min before wake. Add condition checking outdoor temp (skip if forecast high exceeds setpoint).

## Forecast-Based Automations (Template Conditions)

These use **Template conditions** in the UI, which require a small expression.

### Warming Trend — Aggressive Setback Tonight

**Trigger:** Time → 9pm
**Condition:** Template → `{{ state_attr('weather.your_entity', 'forecast')[0].temperature - state_attr('weather.your_entity', 'temperature') > 10 }}`
**Action:** Lower setpoint 5-8°F instead of the usual 3-5°F.

### Cooling Trend — Pre-Heat

**Trigger:** Time → early evening
**Condition:** Template → tomorrow's low significantly colder than today's
**Action:** Bump setpoint 2-3°F above comfort to bank thermal energy.

### Master Mode Selector

**Trigger:** Time → 6am and 3pm
**Action:** Use "Choose" (If-then branches):
- If tomorrow's high > 80°F and trending warmer → cool mode + pre-cool
- Else if tomorrow's low < 40°F and trending colder → heat mode + pre-heat
- Else if tomorrow's high 65-78°F → off + "open windows" notification
- Else → maintain current mode

## UI Tips

**Naming:** Use clear names like "Heat — Pause on open door"
**Traces:** Check the Traces tab after each run to debug
**Helpers:** Create Input Number helpers for setpoints and thresholds (Settings → Helpers). Reference in automations instead of hard-coding.
**Testing:** Manually trigger any automation from its three-dot menu.
**Template testing:** Use Developer Tools → Template (flask icon) to test forecast expressions before using them.
