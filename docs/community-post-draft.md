# Draft: community.home-assistant.io Post

**Category:** Custom Integrations
**URL:** https://community.home-assistant.io/c/projects/custom-integrations/47

---

**Title:** Climate Advisor — HVAC automation that learns your home's thermal behavior and explains every decision in a daily briefing

---

I've been running this integration in my own home for a while and it's stable enough to share. It does a few things I haven't seen combined in one place before, so I figured it was worth posting.

**What it does in one sentence:** It classifies each day by weather forecast (hot/warm/mild/cool/cold), sets your thermostat accordingly throughout the day, and sends you a morning notification explaining exactly what it's going to do and why.

---

### Screenshots

*(paste forecast_3d.png, status.png, and a sample briefing notification here)*

---

### Key features

- **5 day types** (Hot / Warm / Mild / Cool / Cold) with trend modifiers — e.g. if tomorrow is 10°F warmer, tonight's setback is more aggressive to pre-bank thermal comfort
- **Occupancy-aware setpoints** — home, away, vacation, and guest modes with configurable grace periods and door/window pause logic
- **Physics-based thermal learning** — observes your home's heating/cooling rate, envelope loss (k_passive), solar gain, and ventilation effects over time; predictions improve as it collects data
- **Daily briefing** — morning notification that explains today's strategy, recommended window actions, and any learning suggestions (not just "set to 72°F" — it tells you *why*)
- **AI investigator** (optional, needs Claude API key) — generates activity reports and can investigate anomalies like "why was it warm all night on Tuesday?"
- **Built-in dashboard** — temperature forecast chart, target band overlay, compliance scores, learning engine status
- **22 REST API endpoints** if you want to build your own automations on top of it

---

### What you need

- Home Assistant 2024.6+
- A weather entity (e.g. `weather.forecast_home` from Met.no, OpenWeatherMap, etc.)
- A climate entity (any HA-supported thermostat)
- Optional but recommended: door/window binary sensors, occupancy sensor, whole-house fan entity

---

### What it doesn't do yet

- Multi-zone (single thermostat only — multi-zone is planned)
- Humidity-aware decisions
- Utility rate / energy cost integration

The thermal model needs a few hours of observation before predictions are meaningful. Learning suggestions start appearing after ~14 days of data.

---

### Install

Manual install via HACS (once listed) or copy `custom_components/climate_advisor/` to your HA config directory.

**GitHub:** https://github.com/gunkl/ClimateAdvisor

Happy to answer questions about setup or the thermal model internals.

---
