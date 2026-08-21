# HA Community Forum draft — "Share your Projects" category

**Where to post:** https://community.home-assistant.io/c/projects/9 → New Topic

**Suggested title:**
Climate Advisor — weather-aware, learning HVAC automation (now in the HACS default store)

**Suggested body:**

---

Hi all — I've been building **Climate Advisor**, a custom integration that manages your HVAC based on weather forecasts, occupancy, and door/window sensors, and learns from how you actually use your home over time. It's now in the **HACS default store**, so you can install it by searching HACS directly — no custom repository needed.

**What it does:**

- Pulls tomorrow's forecast every morning, classifies the day (hot/warm/mild/cool/cold), and picks an HVAC strategy — pre-cooling on hot days, natural ventilation windows on mild days, pre-heating ahead of a cold snap, etc.
- Tracks occupancy (home/away/vacation/guest) and applies setback temperatures automatically.
- Supports whole-house fan and HVAC fan-only ventilation, coordinated with an economizer two-phase cooling strategy (cool with AC, maintain with free ventilation).
- Builds a physics-based thermal model of your house from real HVAC/ventilation/solar behavior — not fixed rate-of-change numbers — so predictions get more accurate the longer it runs.
- Sends a daily briefing (email/notification) explaining *why* it's doing what it's doing, plus a dashboard with prediction-vs-actual charts.
- Optional AI Investigator (Claude-powered) cross-references the thermal model, event log, and compliance stats to flag anomalies and suggest fixes.

**Screenshots:**

*(attach docs/screenshots/forecast_3d.png, docs/screenshots/status.png, docs/screenshots/forecast_24h.png, docs/screenshots/ai.png when posting — the forum's image uploader is used at post time, these aren't hosted anywhere yet)*

**Install:**

[![Open your Home Assistant instance and open a repository inside the Home Assistant Community Store.](https://my.home-assistant.io/badges/hacs_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?owner=gunkl&repository=ClimateAdvisor&category=integration)

Or: HACS → search "Climate Advisor" → install.

**Links:**

- Repo: https://github.com/gunkl/ClimateAdvisor
- Issues/feature requests: https://github.com/gunkl/ClimateAdvisor/issues

Would love feedback, especially from anyone with a different climate/HVAC setup than mine (I've been developing against my own home, so multi-zone and heat-pump-only setups are areas I'm actively looking to validate).

---

**Notes for posting:**
- Forum requires a HA Community account (David likely already has one from prior HA use).
- Attach real screenshots at post time — the placeholders above are file references only.
- Consider cross-linking this thread from the GitHub README once posted (a "Discuss on the HA Community Forum" link).
