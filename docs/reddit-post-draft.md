# Draft: Reddit Posts

## Post 1 — r/homeassistant (primary)

**Title:** I built a HA integration that learns your home's thermal behavior and sends a daily briefing explaining every HVAC decision — sharing it now that it's stable

**Body:**

I've been running this in my own house for a while and it's solid enough to open up. Here's what it does:

**Climate Advisor** classifies each day by weather forecast (hot/warm/mild/cool/cold), runs your thermostat accordingly, and sends you a morning notification that explains *why* — not just "set to 72°F" but "warm day today, turning HVAC off at 9am and recommending windows open 7–11am before it gets too hot."

**What makes it different from a simple thermostat schedule:**

- Physics-based thermal model — it observes how fast your specific home heats and cools, learns passive envelope loss, solar gain, and ventilation effects over time. Predictions get more accurate as it collects data.
- Trend-aware setbacks — if tomorrow is 10°F warmer, tonight's bedtime setback goes deeper to pre-bank thermal comfort. If tomorrow is colder, it pre-heats in the evening.
- Door/window pause logic — HVAC pauses when windows open, resumes with hysteresis to prevent cycling
- Learning engine — detects patterns like "you consistently ignore the bedtime setback on Fridays" and suggests config changes
- Optional AI investigator (Claude API) — generates activity reports and can investigate specific anomalies

**GitHub:** https://github.com/gunkl/ClimateAdvisor

Requires HA 2024.6+, a weather entity, and a climate entity. Door/window sensors and occupancy are optional but make it smarter.

**Current limitations:** single thermostat only (multi-zone is planned), no humidity-aware decisions yet.

Screenshots in the GitHub README if you want to see the dashboard and briefing format.

---

## Post 2 — r/homeautomation (slightly different angle)

**Title:** Home Assistant integration that uses a physics model to learn your home's thermal behavior and automates HVAC with daily plain-English briefings

**Body:**

Built this for my own home and it's been running stably — thought I'd share it here.

**Climate Advisor** for Home Assistant watches your weather forecast, classifies the day type (hot/warm/mild/cool/cold), and handles thermostat setpoints accordingly. But the part I find most useful is the daily briefing — every morning it sends a notification explaining today's strategy and why, including window recommendations and any pattern-based suggestions.

The thermal learning model observes how your specific home heats and cools (not just generic "degree days" math), and improves predictions over time. After a few weeks it can tell you things like "your house loses heat at 1.8°F/hr overnight when it's 35°F outside" and use that to decide how aggressive the bedtime setback should be.

Full source at https://github.com/gunkl/ClimateAdvisor — it's a custom HA integration, manual install or HACS (submission pending).

---

## Post 3 — r/smarthome (benefit-focused)

**Title:** HA integration that stopped me from waking up to a cold house — it pre-heats based on tomorrow's forecast and explains its decisions every morning

**Body:**

Built a Home Assistant integration called Climate Advisor that I've been running in my own home. The core thing it does: uses tomorrow's weather forecast to decide how aggressive tonight's setback should be. If tomorrow is going to be cold, it banks extra heat in the evening so the overnight setback doesn't leave you cold by morning.

It sends a daily briefing that explains what it's planning to do and why. Not just thermostat numbers — actual reasoning like "cold day, pre-heating tonight because tomorrow drops below 30°F."

Over time it learns your home's thermal behavior specifically (how fast it heats/cools, how much passive solar gain you get, etc.) so the predictions get better.

GitHub: https://github.com/gunkl/ClimateAdvisor — needs HA 2024.6+, a weather entity, and a supported thermostat.
