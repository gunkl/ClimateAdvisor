<!-- Nav: → [Architecture Reference](02-ARCHITECTURE-REFERENCE.md) | → [Strategy and Design](01-STRATEGY-AND-DESIGN.md) -->

# Climate Advisor — Claude Project Instructions

You are helping David build, iterate on, and improve **Climate Advisor**, a custom Home Assistant integration that intelligently manages HVAC (heating and cooling) based on weather forecasts, occupancy, and door/window sensors — and learns from household behavior over time.

## Docs-First Navigation

**Before reading a source file to answer a question about module behavior, check this Anchors table.** Every Tier 2 brief and Tier 3 spec has already been written — use them. Follow the pointer to the right doc, then drill to code only if the doc doesn't fully answer the question.

## Anchors
| Question | Short answer | → Full answer |
|---|---|---|
| What problem does Climate Advisor solve? | The user frequently forgets the heater is on. CA automates HVAC management using weather forecasts, occupancy, and door/window sensors, and sends a daily briefing with any required human actions. | [§Context](00-PROJECT-INSTRUCTIONS.md#context) |
| What are all the modules and how does data flow end-to-end? | 16 source files, each with a single responsibility. Weather entity → coordinator → classifier → automation engine → HVAC service calls. | [Architecture Reference](02-ARCHITECTURE-REFERENCE.md) |
| How does the thermal learning model work — observation types, OLS, EWMA, gate bridge? | Six concurrent observation types (`hvac_heat`, `hvac_cool`, `passive_decay`, `fan_only`, `ventilated`, `solar_gain`) feed separate OLS regression and EWMA update paths. | [Thermal Model v3 Spec](thermal-model-v3-spec.md) |
| How does the automation engine handle occupancy — priority, setback, state transitions? | GUEST > VACATION > HOME/AWAY priority. `_compute_occupancy_mode()` dispatches to per-mode handlers; VACATION fires setback immediately, AWAY after 15-min grace. | [Occupancy Dispatch Spec](occupancy-dispatch-spec.md) |
| How do grace periods work — manual vs automation, timer lifecycle, pre-pause storage? | Two grace types (manual: user override, automation: window recommendation). Cannot be simultaneously active. Timer lifecycle: start → extend → expire → cancel. | [Grace Periods Spec](grace-periods-spec.md) |
| What are all the callsites that clear a manual override, and which ones are safe vs conditional? | Six callsites total. Grace expiry (3 branches) and explicit service call always clear. Bedtime and wakeup handlers skip the clear if `_manual_override_active=True` (fixed in Issue #204). Every clear logged at INFO with `reason=`. | [Grace Periods Spec — What Clears a Manual Override](grace-periods-spec.md#what-clears-a-manual-override) |
| How does the override confirmation delay work — state machine, what it gates, self-resolve vs confirmed paths? | 10-min debounce (configurable) between detecting a thermostat mode change and accepting it as a manual override. While pending, `apply_classification()` is fully blocked. Transients that self-correct within the window are discarded silently. | [Grace Periods Spec — Override Confirmation Delay](grace-periods-spec.md#override-confirmation-delay) |
| How does the Claude AI integration work — circuit breaker, rate limiting, budget, skills? | `ClaudeAPIClient` three-state circuit breaker + per-month budget cap + exponential retry. Two skills: `activity_report` and `investigator`. | [AI Integration Brief](ai-integration.md) |
| What is the full contract for `ClaudeAPIClient.async_request()` and the circuit breaker? | 5-row transition table; `async_request()` checks circuit → rate → budget → retry in order. `ClaudeResponse` mutual-exclusivity invariants documented. | [Claude API Client Spec](claude-api-spec.md) |
| What are the skill registry, execution pipeline, and per-skill contracts? | `AISkillRegistry` 6-step pipeline; `activity_report` 9-section context; `investigator` 7-source context with GitHub issues. Both have deterministic fallbacks. | [AI Skills Framework Spec](ai-skills-spec.md) |
| What are all the REST API endpoints and how does auth and data access work? | 19 views, HA long-lived token required. GETs read `coordinator.data`; POSTs delegate to coordinator or automation engine. Two config fields redacted. | [REST API Brief](rest-api.md) |
| How does the chart state log work — entry schema, ring buffer, retention, persistence? | Raw/hourly/daily entry schemas; 17,520-entry cap (~1 year at 30 min); atomic write to `/config/climate_advisor_chart_log.json`. | [Chart Log Spec](chart-log-spec.md) |
| Why does pred_indoor sometimes track actual indoor (delta ≈ 0)? | First 4h after restart: archive warming up (`ode-warmup`); or thermal model empty (`none`). After warmup: archive provides 4h-advance prediction — delta should be non-zero during transitions. Check log tag: `(archive)` = working; `(ode-warmup)` = auto-resolves; `(none)` = check thermal model confidence. | [Chart Log Spec](chart-log-spec.md#first-write-wins-prediction-archive) |
| How does state persistence work — what files, what fields, what migration chain? | Two JSON files: `climate_advisor_state.json` (runtime) and `climate_advisor_learning.json` (thermal model + history). Atomic write via `.tmp` + `os.replace`. | [State Persistence Brief](state-persistence.md) |
| How does unit conversion work between °F and °C? | `from_fahrenheit()` for absolute temps (subtracts 32, ×5/9); `convert_delta()` for deltas and rates (×5/9 only, no offset). | [Temperature Conversion Brief](temperature-conversion.md) |
| What are all the automation gate conditions and HVAC decision rules? | Full logic table with test coverage map: warm-day guard, grace expiry, manual override, occupancy setback, natural vent exit conditions. | [Computation Reference](08-COMPUTATION-REFERENCE.md) |
| What are the automation decision flowcharts — gate conditions, pause flow, override detection? | Visual Mermaid diagrams for main 30-min loop, door/window pause, manual override, natural vent, and occupancy state machine. | [Automation Flowchart](07-AUTOMATION-FLOWCHART.md) |
| How does `_get_forecast()` work — datetime format, timezone strategy, why UTC midnight causes date-shift? | Date-keyed dict approach (v0.3.44+). UTC midnight datetimes shift to previous local day via `dt_util.as_local()`. Fallback removed; missing dates fall through to `current_outdoor`. | [Forecast Pipeline Spec](forecast-pipeline-spec.md) |
| How does the briefing compute today/tomorrow dates, what data does it consume from the coordinator? | Uses `dt_util.now().date()` as calendar today — not anchored to wake_time or classification cycle. Receives pre-processed `today_high/low`, `tomorrow_high/low` from `_get_forecast()`. | [Briefing Spec](briefing-spec.md) |
| What are all the known ways a user's manual thermostat change can be silently ignored or reversed by CA? | 7 bypasses catalogued: setpoint-only not detected (#197), override confirm lost on restart (#198), grace timer lost on restart (#199), PATH B self-resolve (#200), second override ignored (#201), 30s guard too wide (#202), bedtime/wakeup unconditional clear (#204 — fixed). | [Debugging Guide — Override Bypass Inventory](09-DEBUGGING-GUIDE.md#override-bypass-inventory) |

## Context

David runs Home Assistant and has access to:
- A thermostat/climate entity with built-in presence sensing
- Weather forecast data (today's high/low, tomorrow's high/low)
- Most (not all) door and window contact sensors
- A notification service for daily briefings (email or mobile push)

The user frequently forgets the heater is on, leading to expensive heating bills. The goal is to automate HVAC management invisibly while keeping the home comfortable, and to send a daily briefing that tells the user what (if anything) they should do and why.

## Project State

The integration is at **v0.3.x** — 16 source modules in `custom_components/climate_advisor/`. For the full module list with responsibilities and data-flow diagram, see the [Architecture Reference](02-ARCHITECTURE-REFERENCE.md). For design rationale and guiding principles, see [Strategy and Design](01-STRATEGY-AND-DESIGN.md).

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

When making changes, use targeted edits (not full-file rewrites) and always run `ruff check --fix` + `ruff format` on modified Python files before running tests.
