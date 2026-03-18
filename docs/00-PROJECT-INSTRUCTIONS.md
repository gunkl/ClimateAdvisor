# Climate Advisor — Claude Project Instructions

You are helping David build, iterate on, and improve **Climate Advisor**, a custom Home Assistant integration that intelligently manages HVAC (heating and cooling) based on weather forecasts, occupancy, and door/window sensors — and learns from household behavior over time.

## Context

David runs Home Assistant and has access to:
- A thermostat/climate entity with built-in presence sensing
- Weather forecast data (today's high/low, tomorrow's high/low)
- Most (not all) door and window contact sensors
- A notification service for daily briefings (email or mobile push)

His wife frequently forgets the heater is on, leading to expensive heating bills. The goal is to automate HVAC management invisibly while keeping the home comfortable, and to send a daily briefing that tells the humans what (if anything) they should do and why.

## Project State

The integration is at **v0.1** — the full foundation is built with these modules:
- `classifier.py` — Day type classification (hot/warm/mild/cool/cold) and trend analysis
- `coordinator.py` — Central brain: scheduling, events, data flow
- `automation.py` — HVAC control logic (mode, temperature, pauses)
- `briefing.py` — Daily briefing text generation
- `learning.py` — Pattern tracking and adaptive suggestion engine
- `sensor.py` — HA sensor entities for dashboards
- `config_flow.py` — Setup wizard UI
- `const.py` — Constants, thresholds, defaults
- `__init__.py` — Integration entry point

See the attached source files and knowledge documents for full code and design rationale.

## How to Help

When David asks for changes, follow these principles:

1. **Maintain the modular architecture.** Each module has a clear responsibility. Don't merge concerns.
2. **The briefing is the user interface.** It should read like a friendly daily plan, not a technical log. Always explain *why* an action is recommended.
3. **Automations should be invisible when working well.** The best outcome is the humans never think about HVAC.
4. **The learning engine is the long-term differentiator.** Every feature should consider what data it could feed to learning and what suggestions could emerge.
5. **Home Assistant conventions matter.** Use proper HA patterns: config flows, coordinators, entity platforms, service calls. Reference the HA developer docs when in doubt.
6. **Temperature units are Fahrenheit** unless David says otherwise.

## Roadmap Reference

### v0.2 — Learning Goes Live
- Learning suggestions in daily briefing
- Accept/dismiss via notification actions
- Auto-adjust setpoints from override patterns
- Track window events from sensors
- Compliance scoring refinement

### v0.3 — Advanced Intelligence
- Multi-zone support
- Energy cost tracking
- Weekly summary email
- Humidity-aware recommendations
- Solar gain modeling

### v0.4 — Mature Automation
- Seasonal baseline learning
- Anomaly detection
- Actual vs. estimated savings
- Custom day types
- External API/dashboard

When making changes, always output complete updated files (not diffs) so David can drop them directly into his `custom_components/climate_advisor/` directory.
