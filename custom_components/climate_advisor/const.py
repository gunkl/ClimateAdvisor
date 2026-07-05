"""Constants for Climate Advisor."""

DOMAIN = "climate_advisor"

# Integration version — MUST match manifest.json "version" field.
# A test in tests/test_version_sync.py enforces this.
VERSION = "0.4.69"

RELEASE_NOTES: dict[str, list[str]] = {
    "0.4.69": [
        "Fix #420: AI Investigation reports now flag when a report was cut off before"
        " Claude finished writing it (hit the configured max response length), instead of"
        " silently showing an incomplete report as if it were 'Completed'. The dashboard"
        " now shows a clear truncation warning and a log WARNING is emitted so you know to"
        " raise 'Investigator Max Response Length' in AI settings and re-run.",
    ],
    "0.4.68": [
        "Fix #417: overnight nat-vent no longer flickers between 'nat-vent' and"
        " 'paused — door/window open' every few minutes while the window stays open the"
        " whole time. The reactivation gate that decides whether nat-vent can resume was"
        " using the flat daytime comfort floor even during the sleep window, so indoor"
        " temperatures that were perfectly fine relative to the (lower) sleep floor kept"
        " reading as 'too cold' and repeatedly shutting the session down. It now uses the"
        " same sleep-aware floor the fan-cycling logic already used.",
    ],
    "0.4.67": [
        "Fix #415: the Status card no longer shows a stale nat-vent target temperature"
        " (e.g. 'nat-vent (target 71°F)') that could disagree with the correct cycling"
        " band shown right below it (e.g. '64°F–66°F'). The status string is cached for"
        " up to 30 minutes while the cycling band is recomputed live on every dashboard"
        " load, so the two could drift apart across a sleep-window transition. The status"
        " string now just says 'nat-vent' — the live cycling band is the only place the"
        " temperature is shown.",
    ],
    "0.4.66": [
        "Fix #413: restart-cause diagnostics (added in #403) now correctly classify real HA"
        " restarts and deploys as 'version_changed' or 'user_restart' instead of always"
        " showing 'unknown'. The persistence step was wired to async_shutdown(), which only"
        " runs on config-entry unload/reload — not on a normal Home Assistant restart. A new"
        " EVENT_HOMEASSISTANT_STOP listener now persists the same shutdown diagnostics on the"
        " restart path that actually happens in practice.",
    ],
    "0.4.65": [
        "Fix #411: nat-vent floor-exit decisions and false comfort-violation alarms during"
        " correct WHF cycling are now consistent; a stuck thermostat setpoint disagreement"
        " self-corrects instead of retrying forever.",
    ],
    "0.4.64": [
        "Fix #409: streamlined the Status card's nat-vent display — removed the duplicate"
        " target temperature (previously shown twice), removed the redundant 'Natural"
        " ventilation'/'nat-vent' double-naming, and dropped the unverified 'windows open'"
        " prefix (nat-vent can be active without any window physically open; real window"
        " state is already shown by the dedicated Doors/Windows card).",
    ],
    "0.4.63": [
        "Fix #407 follow-up: removed the standalone 'Natural Vent' dashboard card — its"
        " cycling-band and AC-assist info is now shown as a supplemental line on the main"
        " Status card instead of a separate card, per the project's 'no new cards, extend"
        " existing ones' dashboard convention.",
    ],
    "0.4.62": [
        "Fix #407: the dashboard Status card no longer shows a stale daytime nat-vent target"
        " (e.g. 71°F) overnight during the sleep window — it now matches the Natural Vent"
        " card's correct sleep-window target (e.g. 65°F).",
    ],
    "0.4.61": [
        "Fix #405: HVAC writes no longer stay permanently blocked after a whole-house-fan"
        " nat-vent session ends with the fan already off at a restart/coalesce boundary."
        " reconcile_fan_on_startup()'s 'no-fan' decision now releases any stranded HVAC"
        " suppression flag (_pre_fan_hvac_mode) the same way a normal fan deactivation"
        " does, instead of only clearing the fan-tracking flags — previously the home"
        " could be left with no automated cooling response for the rest of the day.",
    ],
    "0.4.60": [
        "Fix #402: whole-house-fan nat-vent could silently stop controlling the home for hours"
        " overnight. Two causes: (1) fan_thermostat_check() — the tick-level safety check that"
        " runs far more often than the 30-minute classification cycle — still used the flat"
        " daytime comfort_heat floor even during the sleep window, so it always ended the"
        " nat-vent session prematurely before the correct sleep-window cycling"
        " (nat_vent_temperature_check(), fixed in #374) ever got a chance to run. (2) Once that"
        " premature exit fired, apply_classification() legitimately arms 'cool' mode as a"
        " compressor backstop — but that permanently blocked the fan's own re-activation check,"
        " which required the thermostat's armed mode to be literally 'off' even though the"
        " compressor was never actually running. Both are fixed: the tick-level floor check is"
        " now sleep-aware, and re-activation now checks whether the compressor is actively"
        " calling (hvac_action) instead of the armed mode string.",
        "Fix #402: nat-vent exit/assist events (comfort-floor exit, predicted-floor exit,"
        " away-ceiling exit, outdoor-rise exit, forecast/floor-imminent skip, AC-assist-armed)"
        " now all carry a fan_device field identifying which physical fan mechanism (WHF/HVAC"
        " fan/both) was involved — previously only the fan-on/off cycling events did.",
        "Fix #402: the single-setpoint dashboard card (cool/heat modes) now shows a '(CA: X)'"
        " annotation when the real thermostat setpoint diverges from CA's intended target by"
        " more than 1°, matching the divergence indicator the heat_cool card already had. The"
        " CA target itself is now also sleep-window aware.",
        "Fix #403: CA now logs its own version at startup and shutdown and classifies why it"
        " restarted — a routine version-change deploy, a user-initiated Home Assistant"
        " restart/stop, or an unexplained (crash-like) restart — and shows that cause on the"
        " restart boundary marker in the AI activity report, instead of leaving restarts"
        " unexplained.",
    ],
    "0.4.59": [
        "Fix #400: nat-vent dashboard/status showed the daytime comfort-band target (e.g. 71°F)"
        " even during the overnight sleep window, after Issue #374 already fixed the fan's actual"
        " cycling target to follow sleep_heat + hysteresis (e.g. 66°F) overnight. The fan was"
        " behaving correctly, but coordinator.py's get_debug_state() independently recomputed the"
        " target with a hardcoded daytime-only formula, so the status page never reflected the"
        " #374 fix. The dashboard now mirrors the same sleep-vs-daytime logic used by the fan"
        " itself.",
    ],
    "0.4.58": [
        "Fix #396: The status card could show 'waiting for coalescing' indefinitely after an HA"
        " restart with no clue why. Diagnostics deployed to confirm the cause ruled out the #392"
        " decision lock (confirmed live: nothing was holding it) — the real blocker is that the"
        " coalesce check only runs once the weather entity is available, and that entity can stay"
        " 'unavailable' for a long time after restart before the weather integration reports back"
        " in. The status card now says 'starting — waiting for weather data' in that specific case"
        " instead of the misleading generic 'waiting for coalescing', so this is diagnosable from"
        " the dashboard alone going forward.",
    ],
    "0.4.57": [
        "Fix #396: Added diagnostics to pinpoint a startup-coalescing regression — after #392's"
        " automation decision lock shipped, the status card could show 'waiting for coalescing'"
        " indefinitely after a restart, with no way to tell what was stuck. The decision lock now"
        " tracks and logs which method holds it and for how long, with checkpoint logging through"
        " the coalesce call chain and a new decision_lock_holder / decision_lock_held_seconds"
        " status field.",
    ],
    "0.4.56": [
        "Fix #392: Whole-house fan (WHF) and AC could fight each other — the ODE ceiling guard"
        " applied the same 'switch to AC once indoor crosses the ceiling' rule to both fan types,"
        " but a WHF is mutually exclusive with AC and physically guaranteed to keep cooling the"
        " house as long as outdoor air is cooler than indoor, so the ceiling number never applied"
        " to it. This caused a repeating off→cool→off→cool flip roughly every 5 minutes. The"
        " ceiling check is now archetype-aware, and HVAC writes are structurally blocked while a"
        " WHF session owns the thermostat (previously only enforced by convention). Fan"
        " activation/deactivation are now idempotent, and automation decisions are serialized so"
        " independently-triggered handlers can no longer race on shared state. Activity Log lines"
        " for fan events now show which fan (hvac_fan/whf/both) actually fired instead of a"
        " generic 'fan' label.",
    ],
    "0.4.55": [
        "Fix #390: Whole-house fan status could show 'off (manual override)' for up to 30 minutes"
        " after the fan was actually confirmed running — the coordinator listener that detects the"
        " fan_state_entity confirming physical on/off silently dropped the event once a manual"
        " override was already active, so the displayed status only caught up at the next scheduled"
        " poll. Now a coordinator refresh is requested immediately so the status reflects reality"
        " within one cycle.",
    ],
    "0.4.54": [
        "Fix #388: Climate Advisor was missing from the Integrations page in Settings → Devices &"
        " Services — v0.4.53 set manifest.json integration_type to 'helper', which Home Assistant's"
        " frontend excludes from the Integrations dashboard and routes to the Helpers tab instead."
        " Corrected to 'service', the accurate HA taxonomy value for a full custom integration.",
    ],
    "0.4.53": [
        "Feat #384: HACS compliance — integration_type field added to manifest, dynamic README version"
        " badge replaces hardcoded string, state file permissions hardened (0o600), HACS knowledge"
        " base added to docs.",
    ],
    "0.4.52": [
        "Fix #382: AI investigator streaming now shows live text as the LLM responds — chunks are"
        " flushed to the browser immediately via aiohttp drain(). Previously all chunks buffered"
        " until EOF, so the user saw no progress until the full report arrived at once.",
    ],
    "0.4.51": [
        "Fix #380: AI investigator streaming — 'Generating…' loading overlay now hides when the"
        " first chunk arrives so live text is visible. Button and spinner restore immediately on"
        " completion instead of waiting for TCP close.",
    ],
    "0.4.50": [
        "Feat #376: Day-type classification thresholds (Hot/Warm/Mild/Cool) are now configurable"
        " in Settings → Day-Type Thresholds. Defaults remain 85/75/60/45°F so existing users see"
        " no change until they opt to adjust.",
        "Feat #376: Thresholds display in the user's chosen temperature unit (°F or °C) with"
        " slider inputs and ascending-order validation.",
        "Feat #376: Config entry migrated from version 15 → 16; existing installations receive"
        " the default threshold values automatically on upgrade.",
    ],
    "0.4.49": [
        "Fix #376: ODE/OLS prediction math (_build_predicted_indoor_future) now runs in a thread-pool"
        " executor instead of directly on the HA event loop — eliminates periodic event-loop blocking"
        " on every coordinator refresh cycle and morning briefing.",
        "Fix #376: Chart data API endpoint (get_chart_data) also offloaded to executor — same ODE"
        " computation ran inline on every chart panel load.",
        "Fix #376: HACS compliance — official Anthropic SDK usage documented in ClaudeAPIClient"
        " docstring; bundled JS libraries (Chart.js, Hammer.js, chartjs-plugin-zoom) attributed"
        " with upstream URLs in index.html.",
    ],
    "0.4.48": [
        "Feat #377: AI investigator context is now built from 11 independently-testable provider"
        " functions in a new ai_skills_context module — replaces the 773-line monolith with a"
        " thin orchestrator.",
        "Feat #377: Focus-aware provider selection — specifying a focus keyword (thermal, fan,"
        " nat-vent, etc.) skips irrelevant providers, reducing token usage ~40% on focused runs.",
        "Feat #377: KNOWN_FIXES injected into AI context are now version-scoped — only entries"
        " that are partially unfixed, just deployed, or not yet deployed are included, eliminating"
        " stale bug history from mature installations.",
        "Feat #377: GitHub issues are now cached (24h open, 30d closed) — no live API fetch on"
        " every investigation; stale cache returned on network error.",
        "Feat #377: AI investigator now streams — first content visible in ~3–5 seconds via SSE;"
        " structured sections rendered on completion. Non-streaming callers unchanged.",
    ],
    "0.4.47": [
        "Feat #374: Nat-vent nighttime cycling now targets sleep_heat (the sleep floor) instead of"
        " stopping at sleep_cool. Fan cycles off at sleep_heat, back on at sleep_heat + 2×hysteresis,"
        " keeping the home just above the sleep floor without over-cooling.",
        "Feat #374: Fan events now carry a fan_device field (whf/hvac_fan/both) so logs and the"
        " activity report distinguish WHF from HVAC fan blower activity.",
        "Feat #374: Status card now shows separate Fan (WHF) and Fan (HVAC) rows. WHF status"
        " cross-checks physical state and warns when CA's internal flag disagrees with the device.",
    ],
    "0.4.46": [
        "Feat #370: Nat-vent (WHF/HVAC fan) now continues past bedtime when outdoor air"
        " is below the sleep target — free cooling closes the gap before handing off to"
        " the compressor. Fan stops automatically when indoor reaches sleep_cool."
        " Fixes stale _natural_vent_active flag after bedtime fan deactivation.",
    ],
    "0.4.45": [
        "Fix #369: add diagnostic logging to nat-vent paused-by-door reactivation gate.",
    ],
    "0.4.44": [
        "Feat #367: Status pane Conditions card combines day type badge, trend direction/magnitude,"
        " and current outdoor temperature into a single card. HVAC Mode card now shows indoor"
        " temperature inline. Standalone Day Type, Trend, and Indoor cards removed.",
    ],
    "0.4.43": [
        "Fix #365: Fan status now correctly shows 'running (manual override)' when the user"
        " manually turns on a WHF and CA records it as an override (not adopted as nat-vent)."
        " Previously showed 'off (manual override)' even though the fan was physically running.",
    ],
    "0.4.42": [
        "Fix #363: WHF fan status sensor now shows 'running (untracked)' when the whole-house fan is"
        " physically on but CA's flags are clear — reads fan_state_entity (Type 2) or fan_entity"
        " (Type 1) via _get_fan_physical_state().",
    ],
    "0.4.41": [
        "Feat #361: Added fan_state_feedback config flag. When OFF (default),"
        " CA operates in command-only mode — asserting desired fan state idempotently"
        " without reading back entity state. Prevents false override detection from"
        " command-echo entities. When ON, enables physical state feedback for WHF"
        " installations with a dedicated state sensor.",
    ],
    "0.4.40": [
        "Fix #359: Fan cancel now correctly re-asserts setpoint after ecobee comfort-program echo.",
        "Fix #359: Fan running untracked after grace expires now reconciled via"
        " post-grace callback and periodic backstop.",
        "Fix #359: User turning fan ON under nat-vent-eligible conditions now triggers"
        " nat-vent adoption (not override).",
        "Fix #359: AI activity investigator now tracks fan ownership across timeline,"
        " annotating nat-vent events when user controls the fan.",
        "Feat #359: Whole-house fan dual-entity support — optional separate state sensor"
        " (fan_state_entity) for Type 2 WHF installations.",
    ],
    "0.4.39": [
        "Fix #354: Activity Record now shows indoor/outdoor temp at thermostat decision events.",
    ],
    "0.4.38": [
        "Feat #352: Analysis tab — single dropdown card replaces three-section layout; "
        "report type selector (Activity Record / AI Activity Report / AI Investigative Analysis) "
        "with adaptive time window and controls. Download .md and Submit GitHub Issue available "
        "for all three types. Debug and Analysis tabs swapped in tab bar order.",
    ],
    "0.4.37": [
        "Feat #352: Activity Record — new deterministic event timeline (no AI required) "
        "with indoor/outdoor temperature columns. Available on the Analysis tab with "
        "Copy, Download .md, and Submit GitHub Issue actions. AI Activity Report and "
        "AI Investigative Analysis now have their own dedicated sections with separate "
        "generate buttons; AI sections show a disabled notice when AI is not configured. "
        "Tab renamed from 'AI' to 'Analysis'.",
    ],
    "0.4.36": [
        "Fix #347: Fan no longer stays running (untracked) indefinitely after thermostat "
        "starts it autonomously between AC cycles. CA now reconciles on every hvac_action "
        "transition to 'fan' — adopts as nat-vent if conditions allow, or turns it off.",
    ],
    "0.4.35": [
        "Fix #345: Prediction Engines debug panel now shows correct confidence for k_solar "
        "(was always 'none' regardless of observation count) and k_active_hvac "
        "(confidence was previously absent from the panel entirely).",
    ],
    "0.4.34": [
        "Fix #343: Prediction Engines debug panel now shows only confidence level per parameter — "
        "stale 'since' dates (which were frozen at first observation and never updated on EWMA changes) "
        "and redundant observation counts have been removed.",
    ],
    "0.4.33": [
        "Fix #341: nat-vent active during sleep window no longer sets two conflicting thermostat "
        "setpoints every 30 minutes all night — one write per cycle (sleep band) instead of two.",
        "Fix #341: 'Grace started' activity report entry now shows what triggered it "
        "(e.g. 'fan override (manual fan change)') in the Settings column instead of a blank.",
        "Fix #341: fan manual override now emits its own timeline event showing the fan state "
        "change (e.g. 'fan: on->auto') so the reason for the 90-min grace period is visible "
        "without reading the Decisions section.",
    ],
    "0.4.32": [
        "Fix #339: Occupancy→away/vacation no longer arms HVAC setback while windows/doors are open. "
        "HVAC stays off; occupancy mode is recorded for correct setback on resume. "
        "Status now shows 'paused — away (setback deferred: windows open)' when both conditions are active.",
    ],
    "0.4.31": [
        "Fix #338: nat-vent + AC assist — band re-armed when nat-vent activates from pause; "
        "aggressive_savings gate prevents compressor through open windows; "
        "comfort band re-armed immediately when windows close on warm/mild days.",
    ],
    "0.4.30": [
        "Fix #337: HVAC no longer runs with windows/doors open — apply_classification now"
        " enforces HVAC off whenever paused, on both hot and cold days.",
    ],
    "0.4.29": [
        "Fix #335: Sleep setback was overridden every 30 minutes after bedtime on installations"
        " configured via the HA UI (time selector). The HA time selector stores times as"
        " 'HH:MM:SS' but _in_sleep_window() only handled 'HH:MM', causing a silent parse"
        " failure and falling back to the daytime comfort band on every 30-min cycle.",
    ],
    "0.4.28": [
        "Fix #333: Bedtime 'Next Automation' label and chart sleep band now show the configured"
        " sleep temp (e.g. 73°F), not the trend-adjusted value. The warming-trend modifier was"
        " never applied to the thermostat at bedtime — only the mid-night pre-cool event uses it."
        " Cool + cooling-trend and heat + warming-trend users no longer see a phantom ±2°F offset.",
    ],
    "0.4.27": [
        "Fan activity now appears in the Activity Report with its trigger source. CA-commanded"
        " fan changes (min-runtime, economizer, whole-house, reconcile, thermostatic, nat-vent)"
        " emit fan_activated/fan_deactivated, and the thermostat's own blower running uncommanded"
        " (e.g. between AC cooling cycles) now logs a deduped 'Fan running (untracked)' event with"
        " the inferred source — so fan activity is no longer invisible in the report.",
    ],
    "0.4.26": [
        "Chart Vent bar: the forecast (right of 'Now') now renders green-only (ventilation"
        " armed/planned) — blue is reserved for live/historical fan that is physically running,"
        " removing the confusing green→blue flip at 'Now'. Removed the two Vent legend keys.",
    ],
    "0.4.25": [
        "Fix #330: The Activity Report's per-event table is now built deterministically in Python"
        " (no longer LLM-generated). The Settings column is always populated on band/setback rows"
        " (e.g. 'setpoint: 72°F Cool (64°F Heat)') and on deduplicated ×N rows — ending the"
        " recurring empty-Settings defect. A renderer registry covers every event type, with a"
        " safe default for any new type and a coverage test that flags unhandled events.",
        "Fix #331: The chart's Fan and Win Rec bars are merged into one Vent bar (blue = fan"
        " physically running, green = nat-vent armed or windows recommended); the HVAC bar now"
        " shows compressor-only states (heating/cooling). Fixes the fan appearing ON while"
        " thermostatically off.",
    ],
    "0.4.24": [
        "Fix #327: The HVAC/whole-house fan can no longer run indefinitely. A thermostatic fast"
        " loop now re-checks on every indoor OR outdoor temperature change and stops the fan the"
        " moment outdoor ≥ indoor (free cooling gone) or the home has cooled to the comfort floor —"
        " no more waiting up to 30 minutes. On restart, startup coalescing reconciles a running fan"
        " (adopt as nat-vent if eligible, otherwise turn it off), and a manual fan change is treated"
        " as a timed override that is reclaimed on expiry or restart. The economizer also no longer"
        " starts the fan when it is warmer outside than inside.",
    ],
    "0.4.23": [
        "Fix #326: Pre-cool now surfaces in the Next Automation card (next to bedtime setback,"
        " morning wake-up, etc.) instead of as a footnote under Status. Removed the hardcoded"
        " 'tonight' label — the trigger time itself conveys when. 'Next Action' renamed to"
        " 'Next User Action' to distinguish occupant advice from scheduled automations.",
    ],
    "0.4.22": [
        "Fix #325: Four async_call_later callbacks in automation.py were missing the @callback"
        " decorator — HA emitted a thread-safety WARNING on every setpoint verify and fan"
        " verify event. The two lambda shortcuts (setpoint retry + setpoint verify) are now"
        " named @callback functions; the two fan-verify undecorated defs also get the"
        " decorator. No behavior change; eliminates the runtime warning.",
    ],
    "0.4.21": [
        "Fix #323: Automation Time card now shows local HH:MM instead of the raw ISO timestamp.",
    ],
    "0.4.20": [
        "Fix #258 CI: test infrastructure patches for pre-cool feature — isinstance guard in"
        " _build_predicted_indoor_future prevents MagicMock comparison errors; pre-cool stub"
        " attributes added to coordinator factory in test_hvac_session_detection and"
        " test_temperature_sensors; test_target_band updated to document correct warming-trend"
        " sign convention (modifier=-2.0 lowers cool ceiling, not raises it). All 50 golden"
        " scenarios pass.",
    ],
    "0.4.19": [
        "Feat #258: Trend-aware overnight pre-cool — on warming-trend nights CA now banks cold"
        " thermal mass by lowering the AC ceiling mid-night (after nat-vent window closes or"
        " 4h before wake, whichever is later). Nat-vent suppresses AC pre-cool when it already"
        " achieved the target. A morning guard prevents the pre-cool target from dropping below"
        " comfort_heat + 2°F. Status card and chart target band both show the pre-cool dip."
        " Sign-convention bug fixed: warm-trend modifier now correctly lowers the sleep ceiling"
        " (pre-cool) instead of raising it (energy setback).",
    ],
    "0.4.18": [
        "Fix #321: HA restart no longer causes spurious manual overrides. A 5-minute startup"
        " coalescing window suppresses override detection; at the 5-minute mark CA evaluates"
        " sensor states and nat-vent conditions, then applies the correct operating mode"
        " with full INFO logging of every command issued.",
        "Fix #321: Grace period stuck-at-0 now self-heals. If the grace expiry callback is"
        " ever lost, the next 30-minute evaluation cycle detects the stale grace_end_time,"
        " logs an ERROR, and force-clears the override so automation resumes.",
        "Feat #321: Natural ventilation now acts as an active thermostat targeting the"
        " midpoint of the comfort band. The fan cycles on when indoor reaches midpoint+1°F"
        " and off at midpoint-1°F, re-evaluated on every thermostat temperature tick."
        " Fan status surfaced as 'nat-vent (session active, fan idle)' when session is"
        " active but fan is idling between cycles.",
    ],
    "0.4.17": [
        "Feat #320: Add step-by-step logging for contact sensor debounce and nat vent gate"
        " evaluation. When a window opens, logs now show: sensor detected, debounce timer"
        " start/expiry time, gate check values (outdoor/indoor temps, thresholds), and which"
        " specific guard (forecast or thermal floor) blocked activation. The next_automation"
        " sensor now shows 'Evaluating door/window sensors' with the expiry time during the"
        " debounce window.",
    ],
    "0.4.16": [
        "Docs #261: Documented that heat-only and cool-only HVAC systems are unsupported."
        " CA requires a system with both heating and cooling capability."
        " Single-mode systems will not receive commands for their unsupported mode — this is"
        " expected behavior. See docs/02-ARCHITECTURE-REFERENCE.md.",
    ],
    "0.4.15": [
        "Fix #318: Sleep setpoint config no longer blocks users from setting sleep"
        " temperatures cooler or warmer than daytime comfort bounds",
    ],
    "0.4.14": [
        "Fix #313: Fan commands no longer trigger false manual-override detection. When Ecobee"
        " reverts its setpoint after a fan mode change, the coordinator now suppresses the"
        " setpoint-change override check for 30s after any fan command (matching the existing"
        " guard on hvac and temp commands).",
        "Fix #313: After every fan activation or deactivation, CA schedules a 30-second"
        " verify-and-repair callback. If the thermostat's setpoint has drifted more than 0.6°F"
        " from what CA commanded, CA re-asserts the correct setpoint — so any delayed Ecobee"
        " state report arrives within the temp-command recency window and is not misread as an"
        " override.",
        "Fix #313: Natural ventilation no longer exits when outdoor and indoor temperatures are"
        " equal. Equal temps mean neutral airflow (no benefit but no harm); only when outdoor is"
        " strictly warmer than indoor does nat-vent exit due to airflow reversal.",
    ],
    "0.4.13": [
        "Fix #185/#310: solar_phase_offset_h now re-fits daily from the chart_log passive-daytime"
        " windows (incremental 2-day lookback). Previously, the one-shot startup backfill flag was"
        " persisted, so the fit ran exactly once and then never again — solar phase estimation was"
        " frozen from the first time the dashboard was opened. Now _maybe_run_periodic_solar_phase_fit()"
        " fires once per calendar day after the backfill completes.",
        "Feat #312: CA now estimates solar phase offset from AC duty cycle patterns when"
        " passive-window observations are unavailable (common in summer when AC runs during"
        " peak solar hours). A secondary EWMA (α=0.07, min 3 qualifying days) accumulates"
        " AC-based estimates without contaminating the primary passive EWMA. A 5-tier resolver"
        " picks the freshest available estimate; a 90-day staleness gate ensures stale"
        " home-specific data is still preferred over the generic prior.",
    ],
    "0.4.12": [
        "Fix #184/#308: k_solar confidence is now graded (none/low/medium/high) based on committed"
        " solar_gain observation count — thresholds: low ≥20, medium ≥50, high ≥100. Previously"
        " hardcoded to 'none' permanently regardless of how many observations had been collected.",
        "Fix #185/#308: _run_solar_phase_chart_log_fit() now emits structured INFO log lines at"
        " entry, window filtering, EWMA update, and no-qualifying-windows exit — making it possible"
        " to diagnose why solar_phase_offset_h is or isn't learning from chart_log passive windows.",
        "Fix #308: tools/learning_db.py --model now includes a Solar Model section showing"
        " solar_phase_offset_h, observation_count_solar, confidence_k_solar, and a rejection summary.",
    ],
    "0.4.11": [
        "Fix #290: Grace expiry UI refresh, bedtime recovery on HA restart, setpoint validation,"
        " and AI report Settings column display.",
        "Fix #263: After an HA restart with a door or window open, automation no longer stays"
        " paused indefinitely. Pause state is no longer persisted across restarts; the"
        " door/window state-change listener re-detects open sensors within ~5 minutes and"
        " re-pauses cleanly — eliminating the race where slow cloud reconnect left the home"
        " with HVAC off and no nat-vent for up to 30 minutes after restart.",
    ],
    "0.4.10": [
        "Fix #295: On hot days, CA no longer holds the pre-cool temperature offset (−2°F) after"
        " the home reaches the comfort ceiling. Once the pre-cool target is met, a"
        " _pre_condition_achieved flag is set and the ceiling reverts to the configured comfort"
        " setpoint for the rest of the day — preventing unnecessary overcooling.",
        "Fix #301: CA no longer uses heat_cool dual-setpoint mode. Every thermostat command is"
        " now a single climate.set_temperature call containing both the mode (cool or heat) and"
        " the single relevant setpoint — CA sets the bound that matters and lets the thermostat"
        " manage its own band internally.",
        "Fix #301: If the thermostat does not accept a commanded setpoint within 10 seconds,"
        " CA automatically retries the same command 15 minutes later. The retry is cancelled if"
        " a newer command has been issued in the meantime.",
        "Fix #301: README now documents that thermostats must have their built-in schedules"
        " and comfort programs disabled, and their hold type set to 'hold until I change',"
        " for CA to operate correctly.",
    ],
    "0.4.9": [
        "Fix #299: CA setpoint writes to the Ecobee thermostat now bypass HA's deduplication"
        " filter. Every setpoint command sends an intentionally-offset pre-write followed by the"
        " exact target, guaranteeing the command reaches the physical thermostat even when HA's"
        " optimistic state already matches the target.",
        "Fix #299: Dual-setpoint (heat_cool) writes no longer include hvac_mode in every call."
        " The mode switch is sent only when the thermostat is not already in heat_cool mode,"
        " preventing the Ecobee from applying its comfort-program setpoints (65/75) instead of"
        " CA's commanded values (e.g. 68/74).",
        "Fix #299: CA now verifies that reported thermostat setpoints match its commanded values"
        " within 1°F before treating a state change as a confirmation. When setpoints differ by"
        " more than 1°F in heat_cool mode the event is treated as an Ecobee comfort-program"
        " reassertion, not a confirmation, preventing false-positive override suppression.",
        "Fix #299: handle_bedtime() now skips the setpoint write if another setpoint command was"
        " issued within the last 30 seconds, eliminating a startup race where the coordinator's"
        " initial classification cycle and the sleep-window bedtime handler both fired and"
        " produced a double-write that triggered the Ecobee comfort-program reversion.",
        "Fix #299: Fallback default temperatures in _set_temperature_for_mode() corrected from"
        " 68°F/76°F to 70°F/75°F, matching the documented comfort defaults.",
    ],
    "0.4.8": [
        "Fix #293: After every HA restart, CA no longer treats a heat_cool thermostat state as"
        " a manual override. The startup check now recognises heat_cool as CA-compatible with"
        " cool/heat classifier outputs, preventing a spurious 30-min grace period that blocked"
        " automation each morning.",
        "Fix #293: When natural ventilation ends (door/window sensors close), CA now uses the"
        " dual-setpoint heat_cool command for capable thermostats instead of reverting to"
        " single-setpoint cool mode. Ecobee users no longer see the band drop from [68/74] to"
        " a single 72°F setpoint after every ventilation cycle.",
        "Fix #293: AI activity investigator now includes active thermostat setpoints"
        " (single-setpoint temperature and dual-setpoint low/high) in its context block so the"
        " AI can explain pre-cool offsets and band boundaries in morning summaries.",
        "Fix #293: GitHub issue titles generated from the dashboard no longer include a"
        " redundant 'Climate Advisor: ' prefix; the full AI-generated summary is used up to"
        " 100 characters.",
    ],
    "0.4.7": [
        "Fix #290: Grace period expiry now immediately triggers a coordinator refresh so sensor"
        " entities reflect cleared override state without waiting up to 30 minutes.",
        "Fix #290: On HA restart, if the system is in the sleep window and no manual override"
        " is active, bedtime setback is re-applied on the first classification cycle (prevents"
        " sleeping at daytime comfort temps after a restart mid-night).",
        "Fix #290: After every climate.set_temperature or _set_temperature_dual() call, a"
        " 10-second validation callback checks whether the thermostat accepted the commanded"
        " setpoints; mismatches are logged as ERROR with commanded vs reported values.",
        "Fix #290: AI activity report Settings column now correctly shows setpoint changes:"
        " override_detected event payload includes old_setpoint_f and new_setpoint_f fields"
        " that the annotation code uses to build the [settings: setpoint: X°F→Y°F] string.",
    ],
    "0.4.6": [
        "Fix #286: climate.set_temperature for dual-setpoint (heat_cool) thermostats now"
        " includes hvac_mode='heat_cool' in the service payload. Without this key the Ecobee"
        " integration silently ignored the setpoints and reverted to its internal hold values"
        " within 1 second. Log now shows actual service values (post-unit-conversion) so"
        " unit-mismatch issues are diagnosable from logs alone.",
    ],
    "0.4.5": [
        "Fix #284: Door/window close and dashboard Resume now correctly restore both heat and"
        " cool setpoints in heat_cool (dual-setpoint) mode. Previously,"
        " _set_temperature_for_mode() silently returned without writing when the classification"
        " used heat_cool — leaving the thermostat at whatever the Ecobee's own schedule had set"
        " until the next 30-min coordinator cycle.",
        "Fix #284: AI investigator context now includes target_temp_low and target_temp_high"
        " from the live thermostat entity — absence of these fields made Issue #281 root cause"
        " analysis inconclusive.",
        "Fix #284: CA dashboard now shows a (CA: X/Y) indicator when live thermostat setpoints"
        " diverge from CA's configured comfort band by more than 1°F.",
    ],
    "0.4.4": [
        "Fix #282: HA restart now clears all override and grace state (clean slate)."
        " CA starts in fresh automation mode after every restart. Override state and grace"
        " timers are no longer carried over. The 5-minute startup settling window remains.",
        "Fix #282: Manual grace expiry now notifies the user by default."
        " Message updated to: 'Your manual thermostat override has expired."
        " Climate Advisor has resumed automated control.'",
        "Fix #282: Brief thermostat adjustments that self-revert within the confirmation"
        " window now send a notification: 'treated as transient, CA continues normal operation.'",
        "Fix #282: Changing thermostat mode while an override grace is active now restarts"
        " the confirmation window for the new mode, rather than being silently ignored.",
    ],
    "0.4.3": [
        "Fix #277: Whole-house fan now suppresses HVAC while active (sets thermostat off;"
        " restores prior mode when fan stops). Running AC while exhausting conditioned air"
        " is no longer possible.",
        "Fix #277: All sensors closing now stops the whole-house fan even when natural"
        " ventilation was not the trigger — the whole-house fan serves no purpose with"
        " windows sealed.",
        "Fix #277: CA's own HVAC-off command (which asserts fan_mode=auto as a side effect)"
        " no longer triggers a spurious fan manual-override grace period. Cloud thermostat"
        " echoes arriving after the 30s guard window are now suppressed.",
        "Fix #277: A single thermostat event that includes both a setpoint change and a"
        " fan_mode change now triggers at most one override response — setpoint wins."
        " Previously, CA's coordinator re-application produced both a setpoint override and"
        " a fan grace period simultaneously.",
        "Fix #277: Activity report event log now places setpoint values in the Settings"
        " column for override_detected entries. AI investigator flags events that occur at"
        " exact automation intervals as timing-coincident (may be automation-caused).",
    ],
    "0.4.2": [
        "Fix #239: CA's own fan activation no longer triggers a spurious manual-override grace period."
        " When CA calls climate.set_fan_mode for natural ventilation, the fan_mode echo from a cloud"
        " thermostat can arrive after _fan_command_pending has already cleared. A new _fan_command_time"
        " timestamp guard (_is_recent_fan_command, 30 s) mirrors the existing _is_recent_temp_command"
        " pattern and suppresses false override detection. Parallel fix to #221/#225.",
    ],
    "0.4.1": [
        "Fix #269: Manual overrides now correctly detected in heat_cool (dual-setpoint) mode."
        " Four bugs fixed: CA's own mode command no longer triggers a false fan override grace period"
        " (cloud-thermostat echo arrives after the 30s guard); heat_cool → cool mode switch is now"
        " detected as a manual override; dual setpoint changes (target_temp_high/target_temp_low)"
        " are now visible and trigger a grace period; hvac_mode now captured in incident records.",
        "Fix #264: Economizer (comfort-band fan assist) no longer re-applies the full classification"
        " setpoint when it exits, overriding a user's manual adjustment during the fan-only period.",
        "Fix #266: Dashboard Status tab now shows the actual band setpoints [heat_floor/cool_ceiling]"
        " for heat_cool thermostats rather than a single target_temperature.",
        "Fix #190: Forecast pipeline — tomorrow's high no longer shows as day-after-tomorrow in"
        " negative-UTC-offset timezones after 5 pm (evening UTC rollover). Reference date is now"
        " local calendar date; forecast entries are matched by raw API date.",
        "Feat #193: Activity report now includes a full event log (last 12 h, chronological) and a"
        " per-override detail section showing each manual setpoint change with time, direction, and"
        " duration. The Timeline section reflects the complete sequence, including automation"
        " re-assertions after an override cleared.",
    ],
    "0.4.0": [
        "Feat #249: Thermostat-is-the-controller — Climate Advisor now programs a comfort band"
        " [comfort_heat, comfort_cool] and lets the thermostat's own deadband hold it, instead of"
        " switching HVAC off and running a 30-minute supervisory loop. The home pre-heats cold"
        " mornings up to comfort and cools warm afternoons by itself; natural ventilation keeps the"
        " band armed (free cooling stays free while the heat floor stays defended); aggressive_savings"
        " widens the band. away/vacation/sleep use setback bands. Single-mode thermostats arm the"
        " threatened edge; dual heat_cool thermostats hold both edges with one command.",
        "Fix #247: The ODE ceiling guard now escalates to AC when outdoor stays below indoor but"
        " ventilation can't hold the comfort ceiling (re-occurrence of #218's incomplete fix). Under"
        " the #249 band model this is the misprogramming backstop; the comfort band is the primary"
        " defense.",
    ],
    "0.3.54": [
        "Fix #172: Predicted indoor temperature no longer drops suddenly at sleep time"
        " — ODE uses classification.hvac_mode for today's mode (prevents evening forecast-high flip);"
        " hvac_mode passed explicitly to both ODE functions (prevents wrong Q branch on sleep setback)",
        "Fix #174: chart_log time sourcing unified — dt_util.now() replaces datetime.now(UTC)"
        " in get_entries() and _maybe_prune() for consistent behavior across production and tests",
        "Fix #176: DailyRecord accumulated counters survive HA restart mid-day"
        " — _async_send_briefing() preserves hvac_runtime_minutes, manual_overrides, and 6 other"
        " fields when replacing _today_record on same calendar day; state saved on HVAC off",
        "Feat #177: AI Investigator noise reduction"
        " — abandonment reasons pre-classified (operational vs quality-failure),"
        " count discrepancy ≤1 suppressed as flush lag, pending observations removed from context;"
        " new investigate-ca-report Claude Code skill with 5-phase triage taxonomy",
        "Feat #180: GitHub issue submission modal restored"
        " — Submit GitHub Issue button in investigation panel, config flow GitHub Integration step,"
        " default title 'Climate Advisor: Investigative Analysis'",
        "Feat #186: window_compliance denominator in AI investigator context"
        " — shows '0.6667 (2 of 3 windows-recommended days)' to prevent AI misinterpretation",
    ],
    "0.3.53": [
        "Fix #170: Setpoint-only overrides now enter manual grace period immediately"
        " — CA no longer resets thermostat after user adjusts target temperature without changing mode"
        " (handle_setpoint_override() bypasses confirmation window; CONFIG_METADATA description corrected)",
    ],
    "0.3.52": [
        "Feat #166: AI Investigation Analysis — feedback loop (helpful/not helpful/wrong),"
        " unified investigation view with history tab, GitHub issue submission from the dashboard",
        "Feat #164: Chart forward navigation into predicted future"
        " — '>' button advances beyond current time using physics-simulated indoor ODE results",
        "Fix #162: Chart forward navigation after historical re-fetch"
        " — advances from the retrieved anchor timestamp instead of jumping to current time",
    ],
    "0.3.51": [
        "Fix #158: Investigation history panel shows full report text"
        " — AI no longer duplicates findings across sections in multi-section reports",
    ],
    "0.3.50": [
        "Fix #156: HVAC thermal observations never committed — 'samples' key shadow bug"
        " in _start_hvac_observation() fixed; startup recovery, rejection log, and AI investigator context updated",
    ],
    "0.3.47": [
        "Fix #149: AI activity report — k_active_hvac heat/cool values now display correctly"
        " (property path fixed: hvac_info['value']['heat/cool'] instead of direct key lookup)",
        "Fix #149: Comfort band [FLAG] now suppressed when indoor/outdoor gap is within thermostat swing deadband",
        "Fix #149: Activity report section deduplication rule added to system prompt",
        "Fix #149: HVAC peak indoor temp now captured at exact HVAC-off moment (not only at poll cycles)",
    ],
    "0.3.56": [
        "Fix #220: Manual override now cleared when occupancy transitions to away or vacation"
        " — automation resumes correctly after user leaves home; override no longer silently persists",
        "Fix #221: Away-mode setback no longer falsely detected as manual override"
        " — automation-issued setpoint change on occupancy transition correctly attributed to automation",
        "Fix #222: Away/vacation setback now uses correct mode-aware setpoint"
        " — cool-mode thermostat correctly receives setback_cool (79°F), not setback_heat (61°F)"
        " (critical bug: wrong setpoint caused AC to run to 61°F all day while away)",
        "Feat #223: Closed-loop simulation feedback system"
        " — production incidents auto-generate pending BSpec scenarios;"
        " simulation_loop.py validates them; Tests dashboard tab surfaces results;"
        " approve_pending_test API promotes to golden",
        "Fix #227/#199: Grace period timer restored after HA restart"
        " — timer re-scheduled on startup if grace was active; override auto-clears if timer already expired"
        " (previously: restart destroyed timer; system stuck with 0 min remaining until user clicked Resume)",
        "Fix #229: Simulator alignment overhaul"
        " — six simulator divergences from production fixed; three-way audit protocol added;"
        " occupant-first framing and simulator mirror rules encoded in process policy",
        "Fix #230: Grace period expiry now converges to scheduled automation state"
        " — bedtime setback suppressed during grace is applied when grace expires"
        " (previously: grace expiry resumed from daytime classification; occupant slept at wrong temperature)",
        "Fix #231: Nat-vent exits at home comfort ceiling when occupancy is away"
        " — nat_vent_away_ceiling_exit fires when indoor >= comfort_cool while away;"
        " free cooling within home band; HVAC setback handles the rest",
    ],
    "0.3.55": [
        "Fix #190: _get_forecast() switches to local date + raw forecast date —"
        " tomorrow's forecast no longer shows day-after-tomorrow in evening hours"
        " (UTC rollover bug in negative UTC offset timezones)",
        "Feat #193: AI activity report gains event log section and override detail section"
        " — recent events and manual override history visible in generated reports",
        "Fix #197: Setpoint-only thermostat change now enters manual grace period"
        " — user adjusting target temperature without changing mode correctly detected as override",
        "Fix #203: Sensor health comprehension guarded against int instrumentation keys"
        " — integration no longer raises TypeError on health data with numeric keys",
        "Fix #204: Bedtime setback and morning wakeup respect active manual override"
        " — automation defers scheduled setpoint changes when user has active override in effect",
        "Fix #205/#206: Three activity report and override detection fixes:"
        " false override_detected events from automation fan actions eliminated (compound command-pending guard);"
        " timeline now renders as markdown table with Time|Event|Source columns;"
        " markdown tables render correctly in the dashboard panel (frontend renderer added)",
        "Fix #208: Activity report time window now respected — event log filters to requested"
        " hours (was hardcoded 24h); reports >36h include HISTORICAL DAILY SUMMARIES"
        " per-day table from learning records",
    ],
    "0.3.44": [
        "Fix #143: _get_forecast() date-keyed dict replaces blind-index fallback"
        " — briefing tomorrow-high now always reads the correct forecast entry"
        " regardless of whether the API includes today or starts from tomorrow",
        "Fix #144: Investigative analyzer gains KNOWN_FIXES behavioral invariant registry"
        " — scope-bounded [COVERED]/[NOT COVERED] markers replace 'could not verify' hedging",
    ],
    "0.3.37": [
        "Fix #135: Chart log pred_indoor/pred_outdoor now non-null —"
        " hourly forecast nearest-entry lookup replaces exact-hour match"
        " (HA returns future-only entries; exact match always failed)",
        "Fix #134: nat-vent fan no longer clobbered by daily classification HVAC-off",
        "Fix #134: Grace period now allows nat-vent re-entry when indoor exceeds comfort_cool",
    ],
    "0.3.31": [
        "Fix #121: Thermal model v3 — parallel multi-type observation collection",
        "PassiveDecay, FanOnlyDecay, VentilatedDecay, SolarGain observation types added",
        "k_passive now collectable without HVAC cycles (passive envelope decay)",
        "Reduced HVAC plateau guard from 1.0°F to 0.3°F (fixes zero-obs on short-cycling thermostats)",
        "ODE extended with k_vent and k_solar terms for improved mild-day prediction",
        "Investigator: fixed 6th fan_status state, warm_day event frequency, window compliance scope",
    ],
    "0.3.29": [
        "Fixed #119: Dynamic Target Band — chart band now tracks actual system targets"
        " (comfort/sleep/setback/vacation) rather than static comfort limits",
        "Fixed #119: Occupancy-aware prediction — away and vacation modes use setback setpoints in physics simulation",
        "Fixed #119: Vacation mode applies deep setback across all forecast days (not just today)",
        "Fixed #119: Night-owl sleep schedules (sleep_time < wake_time) now handled"
        " correctly via midnight wraparound normalization",
        "Fixed #119: setback_modifier (trend offset) now reflected in chart band",
        "Fixed #119: Adaptive sleep temps (compute_bedtime_setback) used in chart and"
        " prediction when thermal model is available",
    ],
    "0.3.22": [
        "Fixed #107: Predicted indoor line now appears on chart after Now"
        " (HA forecast key is 'datetime', not 'time' — all entries were silently dropped)",
        "Fixed #107: Overnight sleep setpoints use sleep_heat/sleep_cool"
        " (was using setback floor — 6°F too cold on heat days)",
        "Fixed #107: Predicted indoor schedule now uses local time, not UTC hour",
        "Fixed #107: UTC/local confusion eliminated in _get_forecast and AI report timestamps",
        "Fixed #108: Sleep temp config no longer enforces ordering vs comfort/setback",
    ],
    "0.3.21": [
        "Fixed #106: Eliminated predicted indoor spike at bucket boundary",
        "Fixed #104: Wildly wrong predicted indoor temps — off-mode days used"
        " setback_cool overnight; daytime drift now accumulates correctly",
        "Fixed #103: HVAC bars align with temperature swings on chart load; bars zoom and reset correctly",
        "Fixed #101: Added sleep_heat/sleep_cool as separate config keys from away setback",
        "Added #105: AI Investigator gains version context, live GitHub issues, and rotating UI status display",
        "Fixed #102: Chart captures short cycles; fan+heat shown as heating; thermostat swing detection added",
        "Fixed #99: Natural ventilation exits when indoor reaches comfort_heat floor",
    ],
}

# Behavioral invariant registry for the investigative analyzer.
# Each entry documents which code paths a fix covered and which it explicitly
# did NOT cover, so the analyzer can say "[COVERED] — resolved" or
# "[NOT COVERED] — potential gap" instead of "could not verify."
# Add an entry here as part of the definition of done when closing any issue.
KNOWN_FIXES: dict[int, dict] = {
    420: {
        "version_fixed": "0.4.69",
        "title": "AI Investigation report streamed text stops mid-way with no error shown",
        "scope_covered": (
            "claude_api.py: ClaudeResponse gained truncated/stop_reason fields;"
            " _async_call_with_retry() (non-streaming) and async_request_streaming()"
            " (streaming) both now read the Anthropic API's stop_reason on every request,"
            " log it unconditionally at DEBUG, and log a WARNING plus set truncated=True"
            " when stop_reason == 'max_tokens'. ai_skills.py: async_execute() and"
            " async_execute_streaming() propagate truncated into their result/'done' dicts."
            " api.py: ClimateAdvisorInvestigateView.post() logs a WARNING and stores"
            " truncated in the persisted investigation report for both the streaming and"
            " non-streaming branches. frontend/index.html: _runAIInvestigation() shows a"
            " truncation warning instead of 'Completed' status; renderReportInPreview()"
            " and the history list both show a truncation banner/badge when reopening a"
            " truncated report; _formatInvestigationReport() notes it in markdown exports."
            " Root cause: stop_reason was never inspected anywhere in the stack, so a"
            " response cut off at the configured max_tokens cap was indistinguishable from"
            " a normal completion — no exception, no log line, UI showed 'Completed'."
        ),
        "scope_not_covered": (
            "Does not change the actual token budget or system-prompt verbosity — a"
            " Investigator Max Response Length that is genuinely too low for the report"
            " content will still truncate the report; it is now visibly flagged instead of"
            " silent. Does not distinguish an early legitimate stop_reason == 'end_turn'"
            " from a complete report — that case was not observed and could not be"
            " confirmed without production log evidence (none was retrievable during this"
            " investigation), but is now always logged at DEBUG so a future occurrence is"
            " diagnosable."
        ),
    },
    417: {
        "version_fixed": "0.4.68",
        "title": "Overnight nat-vent flapped between nat-vent and paused-by-door every ~5min",
        "scope_covered": (
            "automation.py: added _nat_vent_reactivation_floor(), a sleep-aware comfort"
            " floor (mirrors the branch already used correctly by"
            " nat_vent_temperature_check() and fan_thermostat_check()'s comfort-floor"
            " check), and applied it at all 5 places that previously hardcoded the flat"
            " daytime comfort_heat: the two _nat_vent_may_reactivate() call sites inside"
            " check_natural_vent_conditions() (initial gate + Issue #134 comfort-ceiling"
            " re-entry), the paused-by-door reactivation block, _re_pause_for_open_sensor(),"
            " and reconcile_fan_on_startup()'s previously-separate hand-rolled eligibility"
            " check (now folded into _nat_vent_may_reactivate() instead of a 5th copy)."
            " Confirmed root cause via the CA chart_log: comfort_heat=68°F, sleep_heat=64°F,"
            " sleep window 20:30-06:30; indoor held at 67-70°F all night — fine against the"
            " correct sleep floor, but flapping across the wrong daytime floor on every"
            " 1°F-resolution sensor tick. Also: reconcile_fan_on_startup()'s turn-off branch"
            " now routes through the canonical _exit_nat_vent() choke point (Issue #411)"
            " instead of hand-rolling the pause/grace decision, emitting a new"
            " nat_vent_reconcile_exit event for Activity Report visibility. Also: the"
            " coordinator's Issue #347 post-startup-fan-reconcile listener now guards"
            " against CA's own in-flight fan commands (_fan_command_pending /"
            " _is_recent_fan_command), matching every sibling race-sensitive check in"
            " coordinator.py — defense in depth, not the primary fix."
        ),
        "scope_not_covered": (
            "Two other direct manipulators of _natural_vent_active that bypass"
            " _exit_nat_vent() (handle_all_doors_windows_closed() and the fast-loop"
            " fan_thermostat_check() outdoor-reversal check) were found during this"
            " investigation but neither reads comfort_heat and neither was implicated in"
            " this bug — tracked separately in issue #418, not fixed here."
        ),
    },
    415: {
        "version_fixed": "0.4.67",
        "title": "Status card nat-vent target reappears (71°F) desynced from cycling band",
        "scope_covered": (
            "coordinator.py: _compute_automation_status()'s nat-vent branch no longer embeds"
            " a numeric target — it returns the plain string 'nat-vent'. Root cause: that"
            " string is cached for up to update_interval (30 min) while api.py independently"
            " recomputes compute_nat_vent_cycling_band() live on every dashboard poll to"
            " populate the cycling-band line, so the two could diverge whenever a sleep-window"
            " boundary fell between the last coordinator refresh and the current poll. Every"
            " prior fix (#374, #400, #402, #407, #409) corrected which formula each call site"
            " used but left both independently-timed call sites in place, so the divergence was"
            " structurally guaranteed to recur. Removing the number from automation_status"
            " means there is nothing left to desync — the live cycling-band line is now the"
            " sole place this temperature is shown."
        ),
        "scope_not_covered": (
            "compute_nat_vent_cycling_band() itself and the 30-minute coordinator update_interval"
            " are unchanged — this fix removes the redundant, cache-timing-vulnerable display of"
            " the same number, it does not change how or how often the underlying value is"
            " computed."
        ),
    },
    413: {
        "version_fixed": "0.4.66",
        "title": "Restart-cause classification (#403) always showed 'unknown' on real HA restarts/deploys",
        "scope_covered": (
            "coordinator.py: extracted _persist_shutdown_diagnostics() (sets clean_shutdown,"
            " last_shutdown_version, user_initiated_restart, and persists via"
            " learning.save_state()) out of async_shutdown(), and added a new"
            " EVENT_HOMEASSISTANT_STOP listener in async_setup() that calls the same helper."
            " async_shutdown() — reachable only via async_unload_entry(), which fires on"
            " config-entry unload/reload, not on a normal HA restart — is unchanged and still"
            " calls the same helper. Before this fix, the three shutdown-diagnostics fields"
            " added in #403 were only ever written on the entry-unload path, so a real restart"
            " (deploy, or a user clicking 'Restart Home Assistant') never persisted them, and"
            " async_restore_state() always fell through to the 'unknown' cause bucket."
        ),
        "scope_not_covered": (
            "A true crash or container OOM/kill still fires neither EVENT_HOMEASSISTANT_STOP"
            " nor async_unload_entry, so it correctly still classifies as 'unknown' — this is"
            " expected behavior, not a gap. The persist task scheduled from the STOP listener"
            " runs via hass.async_create_task() and is not guaranteed to complete before the"
            " process exits on an unusually fast shutdown; this mirrors the reliability"
            " envelope of every other async_create_task-scheduled cleanup task in this"
            " integration and was not treated as a new risk introduced by this fix."
        ),
    },
    411: {
        "version_fixed": "0.4.65",
        "title": (
            "Nat-vent floor-exit decision loop told a contradictory story and falsely"
            " flagged correct WHF cycling as a comfort violation"
        ),
        "scope_covered": (
            "automation.py: added _exit_nat_vent(reason, set_outdoor_exit_time=False), the"
            " single choke point for ending a nat-vent session, and rewired all 4 exit paths"
            " (the proactive/predictive k_passive-projected floor exit in apply_classification(),"
            " the reactive hard-floor exit, the outdoor-reversal exit, and the outdoor-too-warm"
            " exit) to call it instead of each hand-rolling its own HVAC restore. This removes"
            " the proactive exit's sensor-blind _set_hvac_mode() override (it never checked"
            " whether a monitored door/window sensor was still open) and its redundant"
            " double-restore on top of _deactivate_fan(), and gives the outdoor-too-warm exit a"
            " _pre_pause_mode capture it never had before. Only the outdoor-reversal call site"
            " sets set_outdoor_exit_time=True, preserving the existing reactivation lockout"
            " timer as a side effect of just that one path. Also added"
            " _nat_vent_may_reactivate(outdoor, indoor, comfort_heat, comfort_cool, threshold,"
            " hysteresis=0.0), unifying the identical 4-part reactivation gate that was"
            " hand-copied at 4 sites (handle_door_window_open(), the paused-by-door"
            " reactivation block, _re_pause_for_open_sensor(), and the Issue #134"
            " comfort-ceiling re-entry check inside check_natural_vent_conditions()) — this"
            " duplication had already caused one prior shipped bug (#402) from a copy drifting"
            " out of sync. Also added _setpoint_reject_streak tracking in _set_temperature():"
            " on the second consecutive setpoint_rejected result for the same commanded value,"
            " the retry nudges the setpoint by +/-1 F (by mode sign) first, waits ~30s, then"
            " re-sends the real target — forcing a thermostat that silently ignores repeated"
            " identical commands to recognize a real change. A distinct setpoint_nudge event"
            " (not a generic setpoint event) keeps the transient nudge value from appearing in"
            " status/activity output as if it were a real decision."
            " coordinator.py: added _is_nat_vent_tolerated_deviation(indoor, comfort_heat,"
            " comfort_cool), gating both _detect_and_emit_incidents()'s comfort_violation/"
            "comfort_undertemp emission and the persisted comfort_violations_minutes"
            " accumulation (feeds comfort_score in learning.py) so an in-tolerance deviation"
            " while a nat-vent session is actively cycling is not counted as a comfort failure,"
            " per the project's own 'violations should only count when the system had control"
            " and failed' principle (CLAUDE.md, Issue #74). comfort_undertemp's payload now"
            " also carries nat_vent_active, matching comfort_violation."
            " ai_skills_investigator.py: investigation_fallback() now detects rapid nat-vent"
            " cycling (3+ exit/re-entry pairs within any 60-minute window) and repeated"
            " identical setpoint rejections (2+ setpoint_rejected events for the same commanded"
            " value) as generalized patterns, not hardcoded to the #411 timeline specifically."
        ),
        "scope_not_covered": (
            "Away-mode ceiling exit is intentionally NOT routed through _exit_nat_vent() — it"
            " has no pause/grace state machine and is a genuinely different concept by design."
            " There is no runtime timeout backstop if the setpoint nudge itself also gets"
            " rejected (the retry loop would still cycle on the nudged value); flag as a"
            " follow-up if that recurs in practice. The ODE ceiling-escalation guard"
            " (automation.py ~L1288) intentionally still calls _ceiling_threshold() directly"
            " rather than _nat_vent_may_reactivate() — it is a different decision (escalate to"
            " AC, not start nat-vent) and only the ceiling sub-condition is shared with it, not"
            " the full 4-part reactivation gate."
        ),
    },
    409: {
        "version_fixed": "0.4.64",
        "title": "Status card nat-vent display duplicated target/naming and claimed unverified 'windows open'",
        "scope_covered": (
            "coordinator.py: _compute_automation_status()'s nat-vent branch no longer prefixes"
            " its return string with 'windows open · ' — natural_vent_active does not imply a"
            " contact sensor is open (it can activate purely on temperature/idle-HVAC"
            " conditions per automation.py's idle-reeval path, and door/window sensors are"
            " optional config), and real window state is already shown by the dedicated"
            " Doors/Windows status card, so restating it here was both potentially inaccurate"
            " and duplicative. frontend/index.html: the supplemental nat-vent line under the"
            " Status card no longer repeats the target temperature (already shown once in"
            " automation_status) or the 'Natural ventilation' name (already named 'nat-vent'"
            " in automation_status) — it now shows only the mode qualifier (AC assist / savings"
            " mode) and the cycling band."
        ),
        "scope_not_covered": (
            "Does not touch the other branches of _compute_automation_status() that"
            " legitimately reference window/door state (e.g. 'windows open (as planned)',"
            " 'paused — door/window open') — those describe genuinely door/window-driven"
            " states. Does not touch automation.py or api.py, both already correct."
        ),
    },
    407: {
        "version_fixed": "0.4.63",
        "title": "Dashboard Status card showed stale nat-vent target + redundant Natural Vent card",
        "scope_covered": (
            "coordinator.py: _compute_automation_status()'s nat-vent branch now calls the"
            " existing compute_nat_vent_cycling_band() helper (the Issue #402 follow-up single"
            " source of truth for this value) instead of independently recomputing the flat"
            " daytime comfort-band midpoint ((comfort_heat + comfort_cool) / 2). Previously the"
            " main Status card always showed the daytime midpoint (e.g. 71°F) even overnight"
            " during the sleep window, contradicting the already-correct Natural Vent card,"
            " which fed off compute_nat_vent_cycling_band() and correctly showed the"
            " sleep_heat + hysteresis target (e.g. 65-66°F). This repeats the exact"
            " fix-one-duplicate-implementation-miss-the-sibling pattern documented on that"
            " helper's docstring from #374, #400, and #402. Follow-up (0.4.63): the separate"
            " standalone 'Natural Vent' status-item card in frontend/index.html (added by the"
            " #402 follow-up) duplicated this info and was never requested — its AC-assist"
            " label and cycling-band line are now rendered as a supplemental line inside the"
            " Status card instead, and the standalone card was removed, per the project's"
            " existing 'no new cards, extend existing ones' dashboard convention."
        ),
        "scope_not_covered": (
            "Does not touch automation.py's nat_vent_temperature_check() (the fan's actual"
            " cycling logic, already correct since #374) or api.py's status endpoint (already"
            " correct since #402's extraction of compute_nat_vent_cycling_band()). Does not"
            " touch the unrelated, pre-existing stale test replica of _compute_automation_status()"
            " in tests/test_status_sensors.py — that is a separate, already-known issue."
        ),
    },
    405: {
        "version_fixed": "0.4.61",
        "title": "HVAC writes permanently blocked by stale WHF suppression flag after nat-vent fan goes idle",
        "scope_covered": (
            "automation.py: reconcile_fan_on_startup()'s 'no-fan' branch (fires when a"
            " coalesce/restart boundary observes the thermostat fan confirmed off) now calls"
            " _deactivate_fan(restore_hvac=True) after clearing the fan-tracking flags, instead"
            " of only clearing _fan_active/_fan_on_since/_natural_vent_active. Previously, a WHF"
            " nat-vent session that ended via cycling-off (nat_vent_temperature_check() calling"
            " _deactivate_fan(restore_hvac=False) by design, so the session can resume) and then"
            " never reactivated left _pre_fan_hvac_mode stranded non-None forever once a later"
            " coalesce boundary cleared _natural_vent_active — _whf_owns_hvac() then permanently"
            " blocked every subsequent HVAC write with no recovery path short of a config change"
            " or manual fan cycling. The fix reuses the existing 'already inactive but restore"
            " pending' branch inside _deactivate_fan() (built for the #402 follow-up) — no new"
            " restore-write logic was added, only a new caller of the existing correct path."
        ),
        "scope_not_covered": (
            "If _fan_override_active is True at the moment a no-fan reconcile fires (user"
            " manually turned the fan off while a WHF suppression session was active),"
            " _deactivate_fan()'s override guard returns before reaching the restore logic —"
            " the stranded flag is not released until the override clears and a later reconcile"
            " runs. This mirrors existing, intentional behavior everywhere else _deactivate_fan()"
            " is called (CA never fights a manual override) and is not new to this fix. Also:"
            " this fix does not address the repeated HA-restart-boundary churn itself observed"
            " in the issue #405 activity log (4 restarts within about an hour) — that instability"
            " is tracked separately (see #403's restart-cause classification work, added the"
            " same morning) and was not investigated as part of this fix."
        ),
    },
    402: {
        "version_fixed": "0.4.60",
        "title": (
            "WHF nat-vent permanently stops controlling the home overnight instead of cycling through the sleep window"
        ),
        "scope_covered": (
            "automation.py: fan_thermostat_check()'s Check 2 hard-floor threshold is now"
            " sleep-aware (sleep_heat - hysteresis during the sleep window, comfort_heat"
            " otherwise), mirroring the fix Issue #374 already applied to"
            " check_natural_vent_conditions(). Previously this tick-level check (which fires on"
            " every thermostat temperature change, far more often than the 30-minute"
            " classification cycle) always used the flat daytime floor, so it permanently ended"
            " nat-vent sessions at comfort_heat before the correct sleep-window cycling"
            " (nat_vent_temperature_check()) ever got a chance to run. Separately, the idle"
            " re-activation gate in check_natural_vent_conditions() (Issue #244) now checks"
            " hvac_action (idle/off) instead of requiring the thermostat's armed mode to be"
            " literally 'off' — apply_classification()'s cool-mode ceiling backstop was"
            " permanently blocking re-activation even when the compressor was never actually"
            " running. Also: all nat-vent exit/assist events now carry a fan_device field;"
            " ca_target_heat/cool in the status API are now sleep-window aware; the"
            " single-setpoint dashboard card gained the same (CA: X) divergence annotation the"
            " heat_cool card already had; docs/07 and docs/08 updated to remove the stale"
            " 'Priority 0 sleep-ceiling reached' description (removed from code in #371, docs"
            " never updated until now)."
        ),
        "scope_not_covered": (
            "The floor/cycling threshold formula is now duplicated across three functions"
            " (check_natural_vent_conditions(), fan_thermostat_check(),"
            " nat_vent_temperature_check()) rather than extracted into one shared helper — a"
            " future formula change must be applied to all three or this exact class of bug can"
            " recur. Not extracted here to keep the fix minimal and reviewable. Root cause of"
            " the 7 unexplained system restarts observed during this incident's investigation is"
            " tracked separately in #403 (restart identity / version logging), not fixed here."
        ),
    },
    403: {
        "version_fixed": "0.4.60",
        "title": "CA restarts were unexplained — no way to distinguish routine deploy, user restart, or crash",
        "scope_covered": (
            "coordinator.py: async_shutdown() logs 'Climate Advisor vX shutting down' and persists"
            " clean_shutdown=True, last_shutdown_version=VERSION, and user_initiated_restart"
            " (reflecting whether a homeassistant.restart/stop service call was observed) via"
            " learning.save_state(). async_setup() registers an EVENT_CALL_SERVICE listener that"
            " sets self._user_initiated_shutdown=True only for homeassistant.restart/stop calls."
            " async_restore_state() logs 'Climate Advisor vX starting up' and classifies the"
            " restart cause by comparing the persisted last_shutdown_version against the running"
            " VERSION and checking clean_shutdown: 'version_changed' (with a separate"
            " version_changed event carrying old/new versions), 'user_restart', or 'unknown' when"
            " neither condition is met (crash residual case). The classification is added to the"
            " system_restarted event payload (cause, plus old_version/new_version when"
            " version_changed), and learning.py's LearningState gained the three new persisted"
            " fields with defensive type-checked load. ai_skills_activity.py's"
            " _render_system_restarted() renders the cause on the restart boundary marker."
        ),
        "scope_not_covered": (
            "Cannot retroactively diagnose the 6 other unexplained restarts observed during the"
            " #402 incident night — this only classifies restarts going forward. The 'unknown'"
            " bucket cannot distinguish an OS/container kill from an HA core crash; both look"
            " identical (no clean shutdown, no service-call event observed)."
        ),
    },
    400: {
        "version_fixed": "0.4.59",
        "title": "Nat-vent dashboard target stuck at daytime comfort-band midpoint during sleep window",
        "scope_covered": (
            "coordinator.py: get_debug_state() now computes nat_vent_target,"
            " nat_vent_on_threshold, and nat_vent_off_threshold using the same"
            " sleep-vs-daytime branch as automation.py::nat_vent_temperature_check() (the"
            " fix from Issue #374) — during the sleep window (_in_sleep_window() True), the"
            " target is sleep_heat + hysteresis; otherwise it remains the daytime"
            " comfort-band midpoint (comfort_heat + comfort_cool) / 2. Previously"
            " coordinator.py independently recomputed these three fields with a hardcoded"
            " daytime-only formula, so the dashboard never reflected the #374 fix even"
            " though the fan's actual cycling behavior was already correct."
        ),
        "scope_not_covered": (
            "The formula is still duplicated between automation.py and coordinator.py"
            " (not extracted into one shared helper) — a future change to the sleep-window"
            " target formula in one file could again silently drift from the other. Not"
            " extracted in this fix to keep the change minimal and reviewable."
        ),
    },
    396: {
        "version_fixed": "0.4.58",
        "title": (
            "Startup coalescing hangs indefinitely after restart — status card gave no clue"
            " it was actually waiting on the weather entity, not stuck on #392's decision lock"
        ),
        "scope_covered": (
            "Diagnostics (0.4.57): automation.py added _decision_pass(), an async context manager"
            " wrapping all 6 decision-lock entry points, tracking _decision_lock_holder /"
            " _decision_lock_held_since with DEBUG logging on wait/acquire/release."
            " coordinator.py added '[coalesce-diag]' checkpoint logging through the coalesce call"
            " chain, plus decision_lock_holder / decision_lock_held_seconds status API fields."
            " Root cause confirmed (0.4.58): querying decision_lock_holder live on a stuck instance"
            " showed null — the #392 lock was never the cause. The real mechanism: the coalesce"
            " check in _async_update_data() lives entirely inside `if forecast:`, so it never runs"
            " while weather.home stays 'unavailable' after restart (a pre-existing conditional"
            " structure, not something #392 introduced). _compute_automation_status() now returns"
            " 'starting — waiting for weather data' instead of the generic 'starting —"
            " initializing' when the 5-minute timer has fired but classification is still unset,"
            " so this specific case is diagnosable from the status card alone."
        ),
        "scope_not_covered": (
            "This does not change how long CA waits for weather data or add a hard fallback if the"
            " weather entity never recovers (existing retry-then-30-min-poll behavior is"
            " unchanged) — it only makes the wait accurately labeled instead of silently generic."
            " Whether the user's weather integration itself needs investigation (why it didn't"
            " report back in after restart) is a separate, not-yet-investigated question — this"
            " fix addresses the misleading status message CA showed while that was happening, not"
            " the weather integration's own recovery time."
        ),
    },
    392: {
        "version_fixed": "0.4.56",
        "title": "Whole-house fan (WHF) and AC could fight each other — repeating off→cool→off→cool oscillation",
        "scope_covered": (
            "automation.py: (1) _ceiling_threshold() is now archetype-aware — returns None for"
            " FAN_MODE_WHOLE_HOUSE/BOTH (a WHF is mutually exclusive with AC and physically"
            " guaranteed to converge while outdoor < indoor, so the ceiling number is irrelevant"
            " to it) and the existing comfort_cool-based value for FAN_MODE_HVAC (fan and"
            " compressor coexist, ceiling is a valid handoff signal there). Refactored into the"
            " ODE ceiling guard's dormancy check and mirrored across all 4 nat-vent reactivation"
            " gate sites (handle_door_window_open(), check_natural_vent_conditions() grace"
            " re-entry, nat_vent_temperature_check() paused reactivation, _re_pause_for_open_sensor()"
            " — the last of which was also missing its _apply_nat_vent_hvac_state() call, fixed"
            " alongside). (2) _whf_owns_hvac() choke-point guard added inside _set_hvac_mode() and"
            " _set_temperature() — the two functions every HVAC write ultimately reaches — blocks"
            " non-off writes while a WHF session owns the thermostat, making mutual exclusion"
            " structural rather than a per-caller convention (previously only _activate_fan()/"
            "_deactivate_fan() themselves enforced it; apply_classification()'s normal 30-min"
            " cycle could silently re-arm HVAC to cool while a WHF was running whenever"
            " aggressive_savings was off, the default). apply_classification() now short-circuits"
            " for WHF right after arming the nat-vent state. Emits hvac_write_blocked_whf_active"
            " when a write is blocked, and _re_deactivate_fan() clears _pre_fan_hvac_mode before"
            " (not after) its restore write, fixing a self-blocking ordering bug found during"
            " testing. (3) _activate_fan()/_deactivate_fan() are now idempotent (no-op with a"
            " debug log if already in the target state), so independently-triggered handlers"
            " reaching the same conclusion no longer each re-execute the full activation sequence."
            " (4) self._decision_lock (asyncio.Lock) serializes the six automation entry-point"
            " methods (apply_classification, handle_door_window_open,"
            " handle_all_doors_windows_closed, check_natural_vent_conditions,"
            " _re_pause_for_open_sensor, nat_vent_temperature_check) so triggers firing close"
            " together can no longer interleave on shared engine state; verified no cross-calls"
            " exist between the six, so a direct lock wrap was used (no _impl extraction needed)."
            " (5) _fan_running property replaces scattered _fan_active or _natural_vent_active"
            " OR-checks. ai_skills_activity.py: fan-related Activity Log renderers"
            " (fan_activated/deactivated, fan_manual_override, fan_cancel, nat_vent_fan_on/off)"
            " now show the fan archetype (hvac_fan/whf/both) instead of a generic 'fan' label."
        ),
        "scope_not_covered": (
            "No runtime/safety timeout backstop was added for a WHF session that never converges"
            " (e.g. outdoor stays just barely below indoor for hours) — WHF is governed purely by"
            " outdoor/indoor direction by design decision, not a gap. The underlying"
            " _natural_vent_active/_fan_active/_pre_fan_hvac_mode state is still tracked as loose"
            " engine attributes rather than a single owning object — Issue #393 tracks the deferred"
            " extraction of a FanSession abstraction that would own this state and its invariants;"
            " _whf_owns_hvac() is written as the seed of that future object but the full extraction"
            " was intentionally not done in this fix to keep it reviewable."
        ),
    },
    390: {
        "version_fixed": "0.4.55",
        "title": "WHF status showed 'off (manual override)' for up to 30 min while fan was physically running",
        "scope_covered": (
            "coordinator.py _async_fan_entity_changed(): when a state change arrives on"
            " fan_entity or fan_state_entity while _fan_override_active is already True, the"
            " listener now calls await self.async_request_refresh() before returning, instead of"
            " silently dropping the event. This lets a physical-state confirmation (e.g. the"
            " fan_state_entity flipping on a few seconds after fan_entity did) correct the"
            " displayed fan_status/whf_status within one refresh cycle rather than waiting for the"
            " next scheduled 30-minute poll. handle_fan_manual_override()/on_fan_turned_off() are"
            " still correctly skipped on this path — only the display-refresh trigger was added."
        ),
        "scope_not_covered": (
            "Does not change the coordinator's update_interval (still 30 minutes); only removes"
            " the silent-drop that made this specific confirmation event invisible between polls."
            " Command-only mode (fan_state_feedback=False) is unaffected — that path already"
            " returns before reaching this branch."
        ),
    },
    388: {
        "version_fixed": "0.4.54",
        "title": "Integration missing from Settings → Devices & Services → Integrations page",
        "scope_covered": (
            "manifest.json integration_type corrected from 'helper' to 'service'. HA's frontend"
            " (ha-config-integrations.ts) subscribes to config entries with"
            " type_filter=['device','hub','service','hardware'] for the Integrations dashboard —"
            " 'helper' is excluded from that query and routed to the separate Helpers tab instead."
            " docs/hacs-compliance.md and CLAUDE.md HACS Compliance Requirements updated to match."
        ),
        "scope_not_covered": (
            "Users who already have the v0.4.53 entry showing under the Helpers tab may need to"
            " restart Home Assistant after updating for the entry to reappear under Integrations;"
            " no automatic migration moves it back without a restart."
        ),
    },
    384: {
        "version_fixed": "0.4.53",
        "title": "HACS compliance — integration_type, dynamic README badge, state permissions, knowledge base",
        "scope_covered": (
            "manifest.json integration_type field, README dynamic version badge, "
            "state.py file permissions (chmod 0o600), docs/hacs-compliance.md, CLAUDE.md HACS section"
        ),
        "scope_not_covered": (
            "PR merge conflict monitoring (manual rebase needed if hacs/default advances), "
            "HACS PR #8117 human review (pending Frenck FIFO queue)"
        ),
    },
    382: {
        "version_fixed": "0.4.52",
        "title": "AI investigator streaming — no visible progress, all chunks buffered until EOF",
        "scope_covered": (
            "api.py: await stream_resp.drain() added after each stream_resp.write() call in the"
            " SSE write loop — forces aiohttp to flush each chunk to TCP immediately rather than"
            " accumulating in the protocol write buffer until write_eof()."
            " api.py: chunk_count DEBUG logging added (first chunk, stream complete)."
            " index.html: console.log at stream open / first chunk / done for browser DevTools visibility."
        ),
        "scope_not_covered": (
            "Reverse proxy buffering (nginx/HAOS ingress) is not addressed — drain() flushes"
            " to the HA aiohttp layer; proxies between HA and the browser may still buffer."
            " The X-Accel-Buffering: no response header is already set to mitigate nginx buffering."
        ),
    },
    380: {
        "version_fixed": "0.4.51",
        "title": "AI investigator streaming — no visible progress + stuck 'Generating…' after report renders",
        "scope_covered": (
            "index.html: break added after done event so finally block runs immediately;"
            " loading overlay hidden on first chunk so streaming pre is visible."
            " api.py: write_eof() called before return so TCP connection closes promptly."
        ),
        "scope_not_covered": (
            "Buffering between Claude API and HA server is not addressed — if all chunks arrive"
            " in a single burst, the pre goes from empty to full with no visible intermediate state."
        ),
    },
    376: {
        "version_fixed": "0.4.50",
        "title": (
            "HACS compliance: ODE executor offload + SDK/JS attribution + classification threshold configurability"
        ),
        "scope_covered": (
            "coordinator.py _async_update_data() and _async_send_briefing(): "
            "_build_predicted_indoor_future() wrapped in await hass.async_add_executor_job(functools.partial(...))."
            " api.py ClimateAdvisorChartDataView.get(): coordinator.get_chart_data() offloaded via executor."
            " claude_api.py ClaudeAPIClient docstring: official Anthropic SDK (AsyncAnthropic) use documented."
            " frontend/index.html: Chart.js, Hammer.js, chartjs-plugin-zoom attributed with upstream URLs."
            " CLAUDE.md: Thread-Safety Requirements section added documenting the executor offload rule."
            " tests/test_executor_offload.py: AST regression tests for all three offload callsites."
            " classifier.py classify_day(): threshold keyword args (threshold_hot/warm/mild/cool) with"
            " module-constant defaults — fully backward-compatible."
            " config_flow.py: Day-Type Thresholds step with slider inputs, Celsius/Fahrenheit conversion,"
            " ascending-order validation, config entry migration v15→v16."
            " const.py: CONF_THRESHOLD_* + DEFAULT_THRESHOLD_* + 4 CONFIG_METADATA entries (category=advanced)."
        ),
        "scope_not_covered": (
            "get_chart_data() still calls self.learning.get_thermal_model() + chart_log.get_entries()"
            " synchronously inside the executor — these are I/O and could be further optimized,"
            " but are already off the event loop after this fix."
            " HACS Issue #5 (repo description phrasing) is a manual gh repo edit — not tracked in code."
        ),
    },
    377: {
        "version_fixed": "0.4.48",
        "title": (
            "AI investigator redesign — context provider registry, focus filtering, GitHub TTL cache, SSE streaming"
        ),
        "scope_covered": (
            "ai_skills_context.py: 11 provider functions, ContextProviderRegistry, FOCUS_TAG_MAP,"
            " version-semantic KNOWN_FIXES scoping, two-tier GitHub cache (24h open / 30d closed)."
            " ai_skills_investigator.py: thin orchestrator replaces 600-line monolith."
            " ai_skills_activity.py: format_engine_status_for_ai moved to ai_skills_context."
            " learning.py: get_recent_records() public API."
            " coordinator.py: GitHub TTL cache fields."
            " claude_api.py: async_request_streaming() async generator."
            " ai_skills.py: async_execute_streaming() SSE event generator."
            " api.py: SSE branch in ClimateAdvisorInvestigateView."
            " index.html: apiFetchStream() + streaming _runAIInvestigation()."
        ),
        "scope_not_covered": (
            "No tests for the SSE path in api.py (aiohttp StreamResponse requires integration"
            " environment). Streaming does not support extended thinking (AI_REASONING_HIGH)."
            " Focus keyword matching is keyword-based substring search, not NLP."
        ),
    },
    374: {
        "version_fixed": "0.4.47",
        "title": (
            "Nat-vent sleep target wrong (stopped at sleep_cool instead of sleep_heat);"
            " no fan device distinction in events/status"
        ),
        "scope_covered": (
            "automation.py nat_vent_temperature_check(): sleep window now uses sleep_heat+hysteresis as"
            " cycling target; daytime unchanged (midpoint of comfort band). Priority 0 sleep-ceiling exit"
            " (nat_vent_sleep_ceiling_reached) removed — session persists through sleep window."
            " _fan_device_label() helper added; fan_device field injected into nat_vent_fan_on,"
            " nat_vent_fan_off, fan_activated, fan_deactivated, nat_vent_bedtime_continue events."
            " coordinator.py _compute_whf_status() and _compute_hvac_fan_status() added as separate"
            " per-device status methods; _compute_fan_status() cross-checks physical WHF state when"
            " _fan_active=True and logs WARNING on stale-flag detection."
            " whf_status and hvac_fan_status added to coordinator data dict and API response."
            " frontend: dual Fan (WHF) / Fan (HVAC) rows in status card."
        ),
        "scope_not_covered": (
            "Stale _fan_active flag is only detected via physical state cross-check — auto-clearing"
            " the flag is not implemented (would require a second callback). Multi-zone not covered."
            " nat_vent_sleep_ceiling_reached event no longer emitted — callers relying on it must"
            " migrate to nat_vent_fan_off with fan_device field."
        ),
    },
    370: {
        "version_fixed": "0.4.46",
        "title": "Bedtime setback + WHF/nat-vent: fan blindly deactivated even when outdoor below sleep target",
        "scope_covered": (
            "automation.py handle_bedtime(): compute sleep band before fan block; gate preserves"
            " nat-vent (all archetypes) when _natural_vent_active AND outdoor < sleep_cool."
            " automation.py check_natural_vent_conditions(): Priority 0 sleep-ceiling exit fires"
            " when in_sleep_window AND indoor <= sleep_cool; calls _deactivate_fan(restore_hvac=False)"
            " and clears _natural_vent_active. State inconsistency fix: _natural_vent_active cleared"
            " on bedtime fan deactivation (was left True when _deactivate_fan ran)."
            " New activity-log events: nat_vent_bedtime_continue, nat_vent_sleep_ceiling_reached."
        ),
        "scope_not_covered": (
            "Nat-vent activation at bedtime when not already active (separate activation question)."
            " outdoor == sleep_cool exactly: gate uses strict <, fan deactivates (conservative)."
            " Priority 0 sleep-ceiling exit requires sleep_time/wake_time to be configured —"
            " _in_sleep_window() returns False without them; fan runs to comfort_heat instead of"
            " sleep_cool for users with sleep_cool set but no sleep schedule."
            " Multi-zone scope not covered."
        ),
    },
    369: {
        "version_fixed": "0.4.45",
        "title": "Nat-vent paused-by-door reactivation — diagnostic logging",
        "scope_covered": (
            "Adds DEBUG logging at lockout check and temperature gate failure paths"
            " in check_natural_vent_conditions() paused-by-door block (automation.py ~line 2182)."
            " Each gate condition (delta, floor, ceiling) now logs its value and pass/fail status."
        ),
        "scope_not_covered": (
            "Behavioral root cause of 15-min activation delay not yet confirmed."
            " Monitoring issue filed to review logs at next occurrence."
        ),
    },
    367: {
        "version_fixed": "0.4.44",
        "title": "Status pane: combined Conditions card + HVAC+indoor card",
        "scope_covered": (
            "api.py: outdoor_temp added to status response (from coordinator._last_outdoor_temp,"
            " converted via from_fahrenheit to display unit, same pattern as indoor_temp). "
            "frontend/index.html loadStatus(): Day Type + Trend cards replaced by Conditions card"
            " showing badge, trend arrow/magnitude, and outdoor temp; HVAC Mode card renamed 'HVAC'"
            " and shows indoor temp inline; standalone Indoor card removed. "
            "tests/test_api.py: _simulate_status_get helper gains outdoor_temp field;"
            " _make_coordinator gains outdoor_temp param; 3 new tests for outdoor_temp conversion,"
            " None handling, and Fahrenheit passthrough. "
            "docs/rest-api.md: status endpoint field list updated."
        ),
        "scope_not_covered": (
            "nat_vent_active and pause_suppressed_classification remain absent from the status API"
            " response (pre-existing gap — those fields exist in coordinator.data but were not"
            " wired into api.py before this PR and are not part of this scope)."
        ),
    },
    365: {
        "version_fixed": "0.4.43",
        "title": "_compute_fan_status() showed 'off (manual override)' when fan physically running under override",
        "scope_covered": (
            "coordinator.py _compute_fan_status() override branch: when _fan_override_active=True"
            " and _fan_active=False, calls _get_fan_physical_state() for FAN_MODE_WHOLE_HOUSE"
            " and FAN_MODE_BOTH; returns 'running (manual override)' if physically on,"
            " 'off (manual override)' if physically off. "
            "tests/test_whf_dual_entity.py: TestComputeFanStatusOverride — 3 new tests. "
            "docs/08-COMPUTATION-REFERENCE.md §9d updated."
        ),
        "scope_not_covered": (
            "FAN_MODE_HVAC: no physical-state check added (HVAC fan physical state is read"
            " from thermostat attributes, not a separate entity; existing ground-truth fallback"
            " at priority 6 covers the untracked case for HVAC fans). "
            "Command-only mode (fan_state_feedback=False): _get_fan_physical_state() returns"
            " None; 'off (manual override)' remains the result."
        ),
    },
    363: {
        "version_fixed": "0.4.42",
        "title": "WHF _compute_fan_status() ground-truth fallback for fan_state_entity (Type 2)",
        "scope_covered": (
            "coordinator.py _compute_fan_status(): after _natural_vent_active check, new block"
            " for FAN_MODE_WHOLE_HOUSE and FAN_MODE_BOTH calls _get_fan_physical_state() —"
            " returns 'running (untracked)' when physical_on is True. "
            "Handles Type 1 (fan_entity) and Type 2 (fan_state_entity) via existing helper. "
            "Returns None (command-only mode, fan_state_feedback=False) falls through to 'inactive'. "
            "tests/test_whf_dual_entity.py: TestComputeFanStatusWHF — 4 new tests. "
            "docs/08-COMPUTATION-REFERENCE.md §9d updated."
        ),
        "scope_not_covered": (
            "_compute_fan_status() HVAC-fan untracked path still reads thermostat attributes"
            " directly (no change). "
            "fan_state_entity not yet surfaced in _compute_fan_status() for the 'running (manual override)'"
            " display — that path still relies on CA's internal _fan_active/_fan_override_active flags."
        ),
    },
    361: {
        "version_fixed": "0.4.41",
        "title": "WHF command-only mode: fan_state_feedback config flag",
        "scope_covered": (
            "fan_state_feedback=False suppresses _async_fan_entity_changed() echo detection; "
            "command-only reconcile loop asserts desired fan state idempotently; "
            "post-grace reconcile uses command assertion not state-read; "
            "whf_mode/whf_last_commanded/whf_desired exposed in coordinator data"
        ),
        "scope_not_covered": (
            "Physical wall-switch overrides remain undetectable in command-only mode; "
            "fan_entity relay failures cannot be confirmed without a state sensor"
        ),
    },
    359: {
        "version_fixed": "0.4.40",
        "title": (
            "Fan state machine ON/OFF distinction — nat-vent adoption, setpoint echo"
            " suppression, post-grace reconciliation, WHF dual-entity support"
        ),
        "scope_covered": (
            "automation.py: new on_fan_turned_off() clears fan flags and starts fan-off"
            " grace (no override flag). "
            "_post_grace_fan_check_callback hook added to _on_grace_expired() all three"
            " exit paths. "
            "coordinator.py: _fan_cancel_in_this_event guard suppresses setpoint override"
            " detection when fan turns off. "
            "_async_reassert_setpoint_after_fan_off() re-asserts CA setpoint 5s after"
            " ecobee echo. "
            "Block 3 direction-aware dispatch routes fan-off to on_fan_turned_off() and"
            " fan-on to handle_fan_manual_override(). "
            "_async_fan_entity_changed() elif branch updated same way. "
            "Post-grace callback (_on_post_grace_fan_check/_async_post_grace_fan_reconcile)"
            " triggers reconcile_fan_on_startup() on grace expiry. "
            "Periodic backstop in _async_update_data(): when fan 'running (untracked)' with"
            " no active override/grace, calls reconcile_fan_on_startup(). "
            "HVAC-driven fan guard at both reconcile call sites (heating/cooling skips"
            " reconcile). "
            "WHF Type 2: CONF_FAN_STATE_ENTITY const + CONFIG_METADATA entry + config flow"
            " selector + translations. "
            "_get_fan_physical_state() routes state reads to state entity when configured,"
            " falls back to fan_entity. "
            "ai_skills_activity.py: fan_cancel renderer, fan ownership tracker in"
            " build_event_timeline_table() and async_build_activity_context(). "
            "docs: 08-COMPUTATION-REFERENCE.md fan table rows, 07-AUTOMATION-FLOWCHART.md"
            " fan flowcharts, grace-periods-spec.md fan-off grace section. "
            "tests: test_fan_control.py (TestFanTurnedOff), test_fan_cancel.py (new),"
            " test_nat_vent_activation.py (1 new test), test_whf_dual_entity.py (new),"
            " test_activity_renderers.py (TestFanOwnershipAnnotations). "
            "Golden simulation scenario:"
            " tools/simulations/pending/issue-359-fan-state-machine.json."
        ),
        "scope_not_covered": (
            "HVAC-driven fan coalescing (CA tries set_fan_mode=auto while HVAC blower is"
            " running autonomously, retries if ignored) — deferred to a separate issue. "
            "WHF Type 2 wiring into _compute_fan_status() — reads thermostat entity"
            " attributes directly, not a separate fan_entity; _get_fan_physical_state()"
            " serves the override-detection path only. "
            "Golden scenario does not cover the 13:35 setpoint-echo suppression phase —"
            " coordinator-level logic; covered by test_fan_cancel.py instead."
        ),
    },
    354: {
        "version_fixed": "0.4.39",
        "title": "Activity Record temp columns — alt-key fallback + explicit injection at 5 call sites",
        "scope_covered": (
            "ai_skills_activity.py: added _first_temp() helper that resolves indoor_f/outdoor_f from "
            "alt key names (indoor_temp, indoor, outdoor_temp, outdoor); build_event_timeline_table "
            "now calls _first_temp() instead of entry.get('indoor_f') for both columns. "
            "coordinator.py _emit_event: normalizes indoor_temp/indoor -> indoor_f and "
            "outdoor_temp/outdoor -> outdoor_f before the setdefault block so any event carrying "
            "alt-named temps gets canonical indoor_f/outdoor_f keys. "
            "automation.py: added _indoor_f_for_event() helper reading current_temperature from the "
            "climate entity; injected indoor_f into 6 emit call sites: classification_applied, "
            "occupancy_comfort_restored, comfort_band_applied, occupancy_setback (away), "
            "occupancy_setback (vacation), override_detected. "
            "tests/test_activity_renderers.py: TestAltKeyTempFallback (3 tests)."
        ),
        "scope_not_covered": (
            "Events emitted by coordinator.py directly (e.g. startup_coalesced, fan_running_untracked) "
            "already receive indoor_f/outdoor_f from the setdefault block in _emit_event — no change needed. "
            "Events emitted by automation.py that do not have a meaningful indoor temp "
            "(e.g. grace_started, nat_vent_fan_off) rely on coordinator's setdefault enrichment. "
            "_indoor_f_for_event() reads climate entity attributes only (not the configured "
            "indoor_temp_entity sensor) — temperature may differ slightly from what _get_indoor_temp_f() "
            "would return if a dedicated sensor is configured."
        ),
    },
    352: {
        "version_fixed": "0.4.37",
        "title": "Activity Report: temp columns, Activity Record endpoint, Analysis tab restructure",
        "scope_covered": (
            "coordinator.py _emit_event: enriches every event with indoor_f/outdoor_f at emit time "
            "using setdefault(); ai_skills_activity.py build_event_timeline_table: adds Indoor/Outdoor "
            "columns, _fmt_temp_cell() helper; api.py ClimateAdvisorActivityRecordView: new GET endpoint "
            "/api/climate_advisor/activity_record?hours=N; frontend/index.html: 'AI' tab renamed to "
            "'Analysis', three-section layout (Activity Record / AI Activity Report / AI Investigative "
            "Analysis), Download .md buttons on all sections, Full/Brief stub removed, AI disabled state "
            "wired to loadAIStatus(); tests/test_activity_renderers.py: TestTempColumns (3 tests)."
        ),
        "scope_not_covered": (
            "Activity Record has no server-side pagination — all events in the window are returned. "
            "Download .md uses Blob/URL.createObjectURL which is unavailable in some non-browser contexts. "
            "The AI disabled state only reflects the ai_status endpoint response — it does not prevent "
            "the API call if a user manipulates the DOM directly."
        ),
    },
    347: {
        "version_fixed": "0.4.36",
        "title": "Post-startup thermostat-autonomous fan stays running (untracked) indefinitely",
        "scope_covered": (
            "coordinator.py _async_thermostat_changed: added detection block for "
            "old_action != 'fan' -> new_action == 'fan' transition when CA does not own "
            "the fan (_fan_active=False, _natural_vent_active=False, _fan_override_active=False); "
            "calls reconcile_fan_on_startup with current indoor/outdoor/any_sensor_open; "
            "test_fan_command_guard.py: TestPostStartupUntrackedFanReconcile (3 tests); "
            "docs/08-COMPUTATION-REFERENCE.md: Anchors row 28 and section 9e updated."
        ),
        "scope_not_covered": (
            "Fan running from fan_mode='on' attribute change (not hvac_action='fan') is "
            "handled by the §9b fan_mode override detection block in _async_thermostat_changed "
            "(Issue #37) — no change needed there. The #347 block skips events where fan_mode "
            "also changed, routing them to the existing override path. "
            "Post-startup hvac_action='fan' while _fan_override_active=True is intentionally "
            "skipped (override is timed; it resolves when grace expires)."
        ),
    },
    345: {
        "version_fixed": "0.4.35",
        "title": "Fix k_solar and k_active_hvac confidence display in Prediction Engines debug panel",
        "scope_covered": (
            "learning.py get_engine_status(): k_solar confidence now computed from "
            "observation_count_solar using the same ladder as get_thermal_model() "
            "(none/<20, low/20-49, medium/50-99, high/100+); "
            "k_active_hvac entry now includes a 'confidence' key computed from total "
            "heat+cool observation count (none/<5, low/5-9, medium/10-19, high/20+); "
            "index.html hvacRow(): appends confidence string after heat/cool values."
        ),
        "scope_not_covered": (
            "solar_phase_offset_h and k_vent_window have no confidence grade in "
            "get_thermal_model() either — no change needed for those parameters."
        ),
    },
    343: {
        "version_fixed": "0.4.34",
        "title": "Remove stale 'since' dates and obs_count from Prediction Engines debug panel",
        "scope_covered": (
            "learning.py get_engine_status(): removed _PRE_TRACKING sentinel, _since() helper, "
            "'since' key from all parameter dicts, 'obs_count' key from parameter dicts; "
            "_update_thermal_model_cache() and _update_solar_phase_offset(): removed all "
            "first_active_date_* write blocks and cache default-init keys; "
            "get_thermal_model(): removed first_active_date_* from return dict; "
            "index.html: removed obs and since from engineRow() and hvacRow() rendering; "
            "tools/engine_status.py: removed date_key param from _engine(), removed since column; "
            "tools/learning_db.py: removed first_active_date display from --model output; "
            "tests/test_solar_phase.py: removed test_first_active_date_set_on_first_update, "
            "removed since assertions from test_inactive_before_observations, "
            "test_active_after_first_observation, and test_engine_status_response_shape."
        ),
        "scope_not_covered": (
            "Existing first_active_date_* values in persisted learning DB JSON files are left in "
            "place — they become orphaned fields that are no longer read or written. No migration "
            "removes them; they harmlessly persist until the cache is reset."
        ),
    },
    341: {
        "version_fixed": "0.4.33",
        "title": "Dual setpoint thrash + 'Grace started' missing context in activity report",
        "scope_covered": (
            "_apply_nat_vent_hvac_state(): sleep window guard skips _apply_comfort_band() call "
            "when in_sleep_window=True, emits nat_vent_ac_assist_armed event only; "
            "handle_fan_manual_override(): fan_before/fan_after params added, emits fan_manual_override event; "
            "coordinator call sites updated to pass fan state; "
            "_render_grace_started(): trigger codes mapped to human-readable Settings labels; "
            "_render_fan_manual_override(): dedicated renderer added to EVENT_RENDERERS; "
            "fan_manual_override added to _MANUAL_EVENT_TYPES and _TIMING_MANUAL_EVENT_TYPES."
        ),
        "scope_not_covered": (
            "activity report timeline Event cell still shows 'Grace started' (humanized type) "
            "rather than the full rendered label — dedup-eligible events use _humanize_type for "
            "the Event cell; trigger info now in Settings column as the practical fix. "
            "Grace started triggered by chat_log or direct API call without fan state available "
            "will show empty fan state fields."
        ),
    },
    339: {
        "version_fixed": "0.4.32",
        "title": "Occupancy→away/vacation bypasses HVAC pause guard while windows open",
        "scope_covered": (
            "handle_occupancy_away() and handle_occupancy_vacation() — _paused_by_door guard added "
            "after _occupancy_mode is recorded; skips _apply_comfort_band() call; emits "
            "occupancy_setback_suppressed_paused event. _compute_automation_status() returns combined "
            "paused+occupancy string when both conditions are active."
        ),
        "scope_not_covered": (
            "handle_occupancy_home() on hot/cool days while paused — if day classification is 'cool' "
            "or 'heat', _set_temperature_for_mode() may set comfort temps while windows open. "
            "Separate issue tracked."
        ),
    },
    338: {
        "version_fixed": "0.4.31",
        "title": "Nat-vent + AC assist: band re-arm and aggressive_savings ceiling gate",
        "scope_covered": (
            "apply_classification() enforces nat-vent band on 30-min cycle; "
            "_apply_nat_vent_hvac_state() re-arms full band (aggressive_savings=off) or "
            "floor-only (aggressive_savings=on) at all activation sites; "
            "handle_all_doors_windows_closed() re-arms comfort band immediately for warm/mild days."
        ),
        "scope_not_covered": (
            "FAN_MODE_BOTH archetype not separately tested; "
            "Tier B integration tests for coordinator state-listener timing not covered."
        ),
    },
    337: {
        "version_fixed": "0.4.30",
        "title": "apply_classification enforces HVAC off when _paused_by_door=True",
        "scope_covered": (
            "apply_classification() _paused_by_door guard — enforces HVAC off on every 30-min "
            "classification cycle when windows/doors are open, regardless of whether pause was "
            "entered via direct door-sensor path or nat-vent exit path. Applies to both hot days "
            "(AC suppression) and cold days (heat suppression). Emits classification_suppressed_paused event."
        ),
        "scope_not_covered": (
            "Immediate shutoff at nat-vent exit moment — up to 30-min delay between nat-vent exit "
            "and next classification cycle remains possible. Tracked as a separate improvement."
        ),
    },
    335: {
        "version_fixed": "0.4.29",
        "title": "_in_sleep_window() silent parse failure for HH:MM:SS config format",
        "scope_covered": (
            "_in_sleep_window() in automation.py now uses index-based split (split(':')[0], split(':')[1]) "
            "instead of tuple unpacking, handling both 'HH:MM' and 'HH:MM:SS' formats. "
            "Affects: apply_classification() 30-min cycle — the only caller of _in_sleep_window() "
            "that was re-evaluating the sleep window. handle_bedtime() was unaffected (passes "
            "in_sleep_window=True explicitly). Regression tests added to test_thermostat_program.py "
            "TestInSleepWindow: hhmmss_format_in_window, hhmmss_format_after_sleep_time_in_window, "
            "hhmmss_format_out_of_window."
        ),
        "scope_not_covered": (
            "Config entries with 'HH:MM' format (existing users) were never broken and continue to work. "
            "wake_time receives the same fix but wake_time parse failure was not the reported symptom "
            "(sleep_time is evaluated first in the or-chain). No migration to normalize stored format."
        ),
    },
    330: {
        "version_fixed": "0.4.25",
        "title": "Activity Report — deterministic per-event table with populated Settings column",
        "scope_covered": (
            "build_event_timeline_table() in ai_skills_activity.py replaces the LLM-generated timeline. "
            "EVENT_RENDERERS registry maps all emitted event types; _format_band_setpoint renders the "
            "single-setpoint active/monitored edges (e.g. 'setpoint: 72F Cool (64F Heat)'). Dedup "
            "collapses consecutive same-type rows to xN while PRESERVING the Settings cell. "
            "_default_renderer renders any new/unregistered type safely (never blank/crash). A coverage "
            "guardrail test introspects automation.py/coordinator.py emitters and fails if a new event "
            "type lacks a renderer. parse_activity_response overrides the timeline section; the LLM still "
            "writes summary/decisions/anomalies/diagnostics. Documented in docs/activity-report-table.md."
        ),
        "scope_not_covered": (
            "LLM still authors summary/decisions/anomalies. Historical event-log entries already stored "
            "are rendered by the new renderers (not retroactively rewritten). Non-English locale "
            "formatting not specifically tested."
        ),
    },
    331: {
        "version_fixed": "0.4.25",
        "title": "Chart — merged Vent bar (fan + nat-vent) and compressor-only HVAC bar",
        "scope_covered": (
            "coordinator.get_chart_data/poll and chart_log.append emit fan_running (physically on, via "
            "_compute_fan_status) and nat_vent_active (_natural_vent_active); _bucket_hourly/_bucket_daily "
            "OR-aggregate both. Frontend drawActivityTimeline merges Fan + Win Rec into one Vent bar "
            "(blue=fan_running, green=nat_vent_active||windows_recommended); HVAC bar restricted to "
            "heating/cooling. Back-compat: pre-#331 entries without the new fields fall back to legacy fan."
        ),
        "scope_not_covered": (
            "Historical chart_log entries on disk carry only the legacy fan field; their Vent bar uses the "
            "back-compat fallback (fan->blue, no armed/green distinction). No JS-level test harness for the "
            "frontend; the Vent color decision is covered by the backend field-contract tests."
        ),
    },
    327: {
        "version_fixed": "0.4.24",
        "title": "Fan runs indefinitely — thermostatic fast loop, startup reconciliation, economizer direction guard",
        "scope_covered": [
            "restore_state clears _fan_override_active/_fan_override_time on restart (clean slate,"
            " matching HVAC override) so a restart reclaims fan control instead of perpetuating a"
            " stale override with no grace timer (permanent fan lockout)",
            "_do_startup_coalesce calls reconcile_fan_on_startup: reads live thermostat"
            " fan_mode/hvac_action and decides adopt-on (nat-vent eligible) / turn-off / no-fan;"
            " logs 'Fan reconcile:' INFO",
            "fan_thermostat_check(indoor, outdoor, trigger) re-evaluates a CA-owned running fan on"
            " every indoor temp change (thermostat seam + indoor_temp_entity listener) and every"
            " outdoor temp change (new outdoor_temp_entity listener) + 5-min backstop timer;"
            " stops at outdoor >= indoor (routed through nat_vent_outdoor_rise_exit for a nat-vent"
            " session) or when cooled to the comfort floor; logs 'Fan thermostat check:' DEBUG",
            "check_window_cooling_opportunity gains an outdoor < indoor free-cooling-direction"
            " guard, mirroring nat-vent",
            "coordinator logs 'Fan control: watching indoor=… outdoor=… thermostat=…' at listener"
            " registration (post-deploy validation signal)",
        ],
        "scope_not_covered": [
            "No JS-level dashboard test for fan status rendering",
            "Fast indoor path relies on the thermostat reporting current_temperature as an"
            " attribute; thermostats that do not populate it fall back to the outdoor listener +"
            " backstop timer",
            "End-to-end restart/coalesce reconciliation is exercised via unit tests; the Tier-A"
            " harness does not restart the engine",
        ],
    },
    147: {
        "version_fixed": "0.3.46",
        "title": "Learned solar phase offset + engine visibility",
        "scope_covered": [
            "solar_phase_offset_h EWMA from chart_log daytime passive windows",
            "per-parameter first_active_date_* tracking in learning cache",
            "get_engine_status() method on LearningEngine",
            "REST endpoint /api/climate_advisor/engines",
            "dashboard Debug tab Prediction Engines card",
            "AI investigator ACTIVE_PREDICTION_ENGINES context block",
            "tools/engine_status.py CLI tool",
            "MILD day window scheduling uses MILD_WINDOW_OPEN_HOUR/MILD_WINDOW_CLOSE_HOUR constants",
            "_solar_factor phase_offset_h parameter shifts ODE peak",
        ],
        "scope_not_covered": [],
    },
    146: {
        "version_fixed": "0.3.45",
        "title": "Dual-estimator framework: block-averaged OLS + endpoint estimator with per-night dynamic selection",
        "scope_covered": [
            "k_passive: block-averaged OLS (60-min blocks) alongside endpoint estimator each overnight window",
            "k_vent_window: same dual-estimator framework applied symmetrically",
            "Dynamic per-night selection via decision table — no one-way door",
            "Backfill v2: 30-day chart_log reprocessed, EWMA converges vs stale v1 values",
            "Daytime solar guard: passive windows restricted to 20:00–08:00",
        ],
        "scope_not_covered": [
            "Thermal mass / phase lag — ODE is still first-order, solar peak timing not addressed",
            "In-memory consecutive-pair OLS on 5-min samples — still structurally limited by 1°F quantization",
        ],
    },
    190: {
        "version_fixed": "0.3.55",
        "title": "_get_forecast() evening UTC rollover — tomorrow shows day-after-tomorrow after 5pm PDT",
        "scope_covered": [
            "coordinator._get_forecast() — reference date now uses dt_util.now().date() (local)"
            " instead of dt_util.utcnow().date() (UTC)",
            "forecast entry bucketing now uses fc_obj.date() (raw) instead of astimezone(UTC).date()"
            " — API's intended date is preserved without timezone conversion",
            "briefing tomorrow-high — correct at all hours in all timezones",
        ],
        "scope_not_covered": [
            "_get_hourly_forecast_data() — hourly entries use per-hour timestamps and were not affected by this bug",
        ],
    },
    193: {
        "version_fixed": "0.3.55",
        "title": "AI activity report event log and override detail sections",
        "scope_covered": [
            "async_build_activity_context() includes EVENT LOG section (last N events, filtered by hours)",
            "async_build_activity_context() includes MANUAL OVERRIDES TODAY section"
            " from _today_record.override_details",
            "_event_source_label() annotates each event line with source_label=automation/manual/unknown",
        ],
        "scope_not_covered": [
            "Historical override details from past days (only today's overrides included)",
        ],
    },
    197: {
        "version_fixed": "0.3.55",
        "title": "Setpoint-only thermostat change triggers manual override grace period",
        "scope_covered": [
            "_async_thermostat_changed(): setpoint change without mode change now calls handle_setpoint_override()",
            "handle_setpoint_override() enters grace period immediately (no confirmation window)",
            "Override detection correctly fires for temperature-only user adjustments",
        ],
        "scope_not_covered": [
            "Setpoint changes initiated by CA itself — guarded by _temp_command_pending flag",
        ],
    },
    203: {
        "version_fixed": "0.3.55",
        "title": "Sensor health comprehension TypeError on int instrumentation keys",
        "scope_covered": [
            "sensor.py _compute_sensor_health(): isinstance(k, str) guard on key iteration",
            "Prevents TypeError when coordinator.data contains numeric keys from HA instrumentation",
        ],
        "scope_not_covered": [],
    },
    204: {
        "version_fixed": "0.3.55",
        "title": "Bedtime setback and morning wakeup respect active manual override",
        "scope_covered": [
            "automation.py apply_bedtime_setback(): checks _manual_override_active before setting setpoints",
            "automation.py apply_morning_wakeup(): same guard applied symmetrically",
            "clear_manual_override() callsites audited — override cleared at correct lifecycle points",
        ],
        "scope_not_covered": [
            "Mid-day scheduled classification re-application — already guarded separately",
        ],
    },
    206: {
        "version_fixed": "0.3.55",
        "title": "False override detection + activity report table format",
        "scope_covered": [
            "coordinator.py _async_thermostat_changed() pause-path guard now checks"
            " _hvac_command_pending OR _fan_command_pending OR _temp_command_pending",
            "Normal override path same compound-flag expansion",
            "Activity report timeline system prompt updated to request markdown table (Time|Event|Source)",
            "_event_source_label() maps event types to automation/manual/unknown for source column",
            "frontend index.html renderMarkdown() added — parses | table | syntax to HTML <table>",
            "renderMarkdown() also converts **bold** to <strong> in all AI report sections",
        ],
        "scope_not_covered": [
            "Retroactive correction of prior false override_detected events in learning DB",
            "Residual race if _hvac_command_pending clears before HA state propagates (>3s latency)",
        ],
    },
    208: {
        "version_fixed": "0.3.55",
        "title": "Activity report hours parameter ignored — hardcoded 24h filter",
        "scope_covered": [
            "async_build_activity_context() extracts hours from **kwargs (was silently ignored)",
            "Both event log cutoffs now use the requested window (was hardcoded 12h/24h)",
            "Event log section header shows actual hours value (_fmt_hours helper)",
            "Reports with hours>36 include HISTORICAL DAILY SUMMARIES from learning._state.records",
            "System prompt updated: two-part Timeline when historical summaries present",
        ],
        "scope_not_covered": [
            "Event log ring buffer covers only ~50-60h — 7d event detail unavailable for older days",
            "Chart log temperature trend not included (DailyRecord high/low used instead)",
        ],
    },
    143: {
        "version_fixed": "0.3.44",
        "title": "_get_forecast() blind-index fallback replaced with UTC-date-keyed dict",
        "scope_covered": [
            "coordinator._get_forecast() — date matching uses UTC calendar date (not local date)",
            "UTC midnight datetimes (e.g. 2026-05-16T00:00:00+00:00) now correctly match"
            " their UTC calendar day instead of being shifted to the previous local day",
            "briefing tomorrow-high — reads date-verified tomorrow_fc for correct calendar day",
        ],
        "scope_not_covered": [
            "_get_hourly_forecast_data() datetime handling — hourly entries use per-hour"
            " local timestamps and were not affected by this bug",
        ],
    },
    141: {
        "version_fixed": "0.3.43",
        "title": "chart_log endpoint estimator replaces passive_decay OLS",
        "scope_covered": [
            "chart_log endpoint — estimator uses chart_log data for R² calculation",
        ],
        "scope_not_covered": [],
    },
    139: {
        "version_fixed": "0.3.42",
        "title": "Persist pred_archive across restarts + UTC key rounding",
        "scope_covered": [
            "coordinator._pred_archive — persisted across HA restarts",
            "chart_log timestamp keys — UTC rounding applied consistently",
        ],
        "scope_not_covered": [],
    },
    135: {
        "version_fixed": "0.3.37",
        "title": "Chart log pred_indoor/pred_outdoor nearest-entry lookup",
        "scope_covered": [
            "chart_log endpoint — hourly forecast lookup uses nearest-entry not exact-hour match",
            "pred_indoor/pred_outdoor — non-null after this fix",
        ],
        "scope_not_covered": [
            "_get_forecast() fallback branch — not addressed in this fix; fixed separately in Issue #143",
        ],
    },
    134: {
        "version_fixed": "0.3.37",
        "title": "Nat-vent fan preserved through HVAC-off classification; grace period nat-vent re-entry",
        "scope_covered": [
            "automation._apply_classification() — nat-vent fan preserved when classification sets HVAC off",
            "automation._resume_from_grace() — nat-vent re-entry allowed when indoor exceeds comfort_cool",
        ],
        "scope_not_covered": [],
    },
    121: {
        "version_fixed": "0.3.31",
        "title": "Thermal model v3 — parallel multi-type observation collection",
        "scope_covered": [
            "coordinator._pending_observations — single PendingThermalEvent replaced with parallel dict",
            "PassiveDecay, FanOnlyDecay, VentilatedDecay, SolarGain observation types",
            "k_passive observable without HVAC cycles",
            "HVAC plateau guard reduced from 1.0°F to 0.3°F",
            "ODE extended with k_vent and k_solar terms",
            "investigator — fixed 6th fan_status state, warm_day event frequency, window compliance scope",
        ],
        "scope_not_covered": [],
    },
    119: {
        "version_fixed": "0.3.29",
        "title": "Dynamic Target Band — chart band tracks actual system targets",
        "scope_covered": [
            "coordinator._compute_target_band_schedule() — comfort/sleep/setback/vacation setpoints used",
            "prediction — away/vacation modes use setback setpoints in physics simulation",
            "vacation mode — deep setback applied across all forecast days",
            "night-owl schedules — midnight wraparound normalization",
            "chart band — setback_modifier reflected",
            "adaptive sleep temps — compute_bedtime_setback() used in chart and prediction",
        ],
        "scope_not_covered": [],
    },
    108: {
        "version_fixed": "0.3.22",
        "title": "Sleep temp config no longer enforces ordering vs comfort/setback",
        "scope_covered": [
            "config_flow — sleep_heat/sleep_cool ordering validation removed",
        ],
        "scope_not_covered": [],
    },
    107: {
        "version_fixed": "0.3.22",
        "title": "UTC/local confusion — forecast key, overnight setpoints, predicted schedule, AI report timestamps",
        "scope_covered": [
            "coordinator._get_forecast() — forecast key changed from 'time' to 'datetime'",
            "coordinator._get_forecast() — datetime parsing now timezone-aware via dt_util.as_local()",
            "prediction — predicted indoor schedule uses local time not UTC hour",
            "overnight setpoints — sleep_heat/sleep_cool used instead of setback floor",
            "ai_skills_investigator — activity report timestamps use local time",
        ],
        "scope_not_covered": [
            "_get_forecast() fallback branch — fallback block not addressed in this fix; fixed in Issue #143",
        ],
    },
    156: {
        "version_fixed": "0.3.50",
        "title": "HVAC thermal observations never committed — samples key shadow bug",
        "scope_covered": [
            "samples key removed from HVAC obs dict in _start_hvac_observation",
            "startup recovery now correctly reads active_samples for HVAC obs types",
            "rejection log now reports real sample count (not always n=0)",
            "rejection log entries for all abandonment paths including new_session_started",
            "AI investigator context includes thermal pipeline health section",
            "k_active_cool=None shown as NEVER LEARNED in investigator context",
            "per-obs-type rejection counts in investigator context",
            "get_engine_status() included in investigator context",
            "learning_db --pending flag shows in-flight observations",
        ],
        "scope_not_covered": [
            "Real-time rejection streaming (capped in-memory log)",
            "Chart_log backfill auto-trigger (still manual or restart-triggered)",
            "Automatic sensor resolution upgrade (still manual config)",
        ],
    },
    149: {
        "version_fixed": "0.3.47",
        "title": (
            "Activity report quality: k_active_hvac property path, comfort-band deadband,"
            " section deduplication, swing peak capture"
        ),
        "scope_covered": [
            "k_active_hvac heat/cool values now appear in AI activity context",
            "Comfort band [FLAG] suppressed when gap <= thermostat swing deadband",
            "Activity report section deduplication rule added to system prompt",
            "HVAC peak temperature captured at exact HVAC-off moment for accurate swing measurement",
        ],
        "scope_not_covered": [],
    },
    158: {
        "version_fixed": "0.3.51",
        "title": "Investigation history full report + AI deduplication",
        "scope_covered": [
            "Investigation history panel shows full report text (not just summary)",
            "AI system prompt gains deduplication rule — findings not repeated across sections",
        ],
        "scope_not_covered": [],
    },
    160: {
        "version_fixed": "0.3.52",
        "title": "Temperature Forecast chart historical navigation via before_ts anchor",
        "scope_covered": [
            "/api/climate_advisor/chart_data?before_ts=<epoch> endpoint parameter",
            "Chart backward '<' navigation fetches historical window anchored before current view",
            "Chart log lookback bounded by available chart_log retention (~365 days)",
        ],
        "scope_not_covered": [
            "Forward navigation into future (addressed in Issue #164)",
        ],
    },
    162: {
        "version_fixed": "0.3.52",
        "title": "Chart forward navigation after historical re-fetch",
        "scope_covered": [
            "Chart '>' button after backward navigation re-anchors to the retrieved window"
            " rather than jumping directly to current time",
        ],
        "scope_not_covered": [],
    },
    164: {
        "version_fixed": "0.3.52",
        "title": "Chart forward navigation into predicted future temperatures",
        "scope_covered": [
            "Chart '>' button beyond latest historical data advances into the physics-simulated"
            " predicted indoor ODE window",
            "Predicted window fetched via before_ts pointing past current time",
        ],
        "scope_not_covered": [],
    },
    166: {
        "version_fixed": "0.3.52",
        "title": "AI Investigation Analysis — feedback loop, unified view, GitHub integration",
        "scope_covered": [
            "Feedback buttons (helpful / not helpful / wrong) on each investigation result",
            "Unified investigation view with tabbed history of prior reports",
            "GitHub issue submission modal — pre-filled from investigation findings",
            "Feedback outcome stored in investigation history record",
            "Cancel button in GitHub issue modal closes the dialog without submitting",
        ],
        "scope_not_covered": [],
    },
    170: {
        "version_fixed": "0.3.53",
        "title": "Setpoint-only manual override detection — immediate grace period entry",
        "scope_covered": [
            "automation.handle_setpoint_override() — new method confirms setpoint change"
            " as manual override immediately (no confirmation window)",
            "coordinator._async_thermostat_changed() now calls handle_setpoint_override()"
            " when temperature changes and all CA-command guards pass",
            "apply_classification() returns early while override is active — no temperature reset",
            "handle_setpoint_override() is a no-op if _manual_override_active or"
            " _override_confirm_pending is already True (no double-trigger)",
            "CONFIG_METADATA description for manual_grace_seconds updated to document"
            " both mode-change and setpoint-change trigger paths",
            "docs/08-COMPUTATION-REFERENCE.md Section 11 updated with setpoint override path",
        ],
        "scope_not_covered": [
            "Setpoint changes made by HA automations (treated same as user changes;"
            " will trigger grace — use _temp_command_pending guard to suppress if needed)",
        ],
    },
    180: {
        "version_fixed": "0.3.54",
        "title": "GitHub issue submission modal — restored from uncommitted worktree code",
        "scope_covered": [
            "CONF_GITHUB_TOKEN / CONF_GITHUB_REPO constants added to const.py",
            "ClimateAdvisorSubmitGithubIssueView — POST /api/climate_advisor/submit_github_issue",
            "config_flow async_step_github_settings() — token + repo config fields",
            "frontend modal — openGithubIssueModal, closeGithubIssueModal, submitGithubIssue",
            "_formatCurrentReport() — formats current investigation report as issue body",
            "Default GitHub issue title changed to 'Climate Advisor: <report_type>'",
        ],
        "scope_not_covered": [
            "API_REFINE_REPORT / investigation refinement — excluded from this PR",
            "Annotation toolbar and rating buttons — excluded from this PR",
        ],
    },
    172: {
        "version_fixed": "0.3.54",
        "title": "Predicted indoor temperature drops at sleep time — ODE mode flip + wrong Q branch",
        "scope_covered": [
            "_build_predicted_indoor_future: today's mode overridden with classification.hvac_mode"
            " — prevents evening flip to 'heat' when only cold night forecast entries remain",
            "_simulate_indoor_physics() and _simulate_indoor_physics_v3(): hvac_mode parameter added,"
            " explicit mode dispatch replaces threshold inference; legacy fallback preserved",
            "Both ODE call sites in _build_predicted_indoor_future pass hvac_mode=mode",
        ],
        "scope_not_covered": [],
    },
    174: {
        "version_fixed": "0.3.54",
        "title": "chart_log uses datetime.now(UTC) bypassing dt_util mock in tests",
        "scope_covered": [
            "ChartStateLog._maybe_prune() uses dt_util.now() instead of datetime.now(UTC)",
            "ChartStateLog.get_entries() uses dt_util.now() as default anchor when before= is None",
            "test_chart_historical_nav.py: autouse fixtures freeze chart_log.dt_util.now to _FAKE_NOW",
            "test_chart_log.py: dt_util.now patched on the already-bound module object",
        ],
        "scope_not_covered": [],
    },
    176: {
        "version_fixed": "0.3.54",
        "title": "DailyRecord accumulated counters reset on HA restart mid-day",
        "scope_covered": [
            "_async_send_briefing() preserves same-day accumulated fields when replacing _today_record:"
            " hvac_runtime_minutes, comfort_violations_minutes, manual_overrides, thermal_session_count,"
            " occupancy_away_minutes, windows_opened, window_open_actual_time, override_details",
            "State saved via async_create_task(_async_save_state()) after each HVAC on→off transition",
        ],
        "scope_not_covered": [],
    },
    177: {
        "version_fixed": "0.3.54",
        "title": "AI Investigator noise reduction + investigate-ca-report triage skill",
        "scope_covered": [
            "_build_thermal_pipeline_context(): abandonment reasons split — 'abandoned' coded as"
            " operational interruption [expected], all other codes as quality-failure [signal]",
            "System prompt: count discrepancy of ≤1 between model cache and pipeline counts"
            " suppressed as EWMA flush lag",
            "Pending (in-flight) observations removed from investigator context — moved to activity report",
            "New Claude Code skill: .claude/skills/investigate-ca-report.md —"
            " 5-phase triage with ACTIONABLE/TIME-DEPENDENT/CONTEXTUAL/NOISE/RESOLVED taxonomy,"
            " monitoring issue workflow, HISTORICAL ARTIFACT rule, 6-column triage table",
        ],
        "scope_not_covered": [],
    },
    186: {
        "version_fixed": "0.3.54",
        "title": "window_compliance denominator in AI investigator context",
        "scope_covered": [
            "get_compliance_summary() returns window_compliance_denominator"
            " (count of days where windows were recommended, not total recording days)",
            "_fmt_window_compliance() formats as '0.6667 (2 of 3 windows-recommended days)'"
            " — prevents AI from treating denominator as total recording window",
        ],
        "scope_not_covered": [],
    },
    220: {
        "issue": 220,
        "title": "Manual override not cleared on away/vacation occupancy transition",
        "version_fixed": "0.3.56",
        "scope_covered": [
            "handle_occupancy_away() and handle_occupancy_vacation() now clear"
            " active manual override before applying setback",
            "Override flag cleared prevents setback being silently skipped on classification cycles while away",
        ],
        "scope_not_covered": [
            "Override clearing on guest mode transition (guest mode maintains comfort, no setback)",
        ],
    },
    221: {
        "issue": 221,
        "title": "Away setback setpoint change falsely detected as manual override",
        "version_fixed": "0.3.56",
        "scope_covered": [
            "_temp_command_time guard added to setpoint-only override detector",
            "Away setback no longer starts spurious 90-minute grace period",
        ],
        "scope_not_covered": [
            "Coordinator-level listener timing (integration-track) — simulator cannot fully exercise this path",
        ],
    },
    222: {
        "issue": 222,
        "title": "Away/vacation setback applies heat setpoint in cool mode",
        "version_fixed": "0.3.56",
        "scope_covered": [
            "handle_occupancy_away() and handle_occupancy_vacation() now read"
            " actual thermostat hvac_mode before selecting setback",
            "Cool-mode thermostat receives setback_cool (79°F); heat-mode receives setback_heat (61°F)",
            "June 5 incident (AC targeted 61°F in cool mode while away) cannot recur",
        ],
        "scope_not_covered": [],
    },
    223: {
        "issue": 223,
        "title": "Closed-loop simulation feedback system",
        "version_fixed": "0.3.56",
        "scope_covered": [
            "incident_detected events emitted for 8 incident classes (comfort_violation, occupancy_transition, etc.)",
            "simulation_loop.py polls event_log and runs pending BSpecs through simulate.py",
            "Tools dashboard Tests tab shows pending scenario statistics",
            "approve_pending_test API promotes BSpec from pending/ to golden/",
            "build_historical_scenario.py extracts production incidents into pending scenarios",
            "Incident package auto-appended to existing Submit GitHub Issue button",
            "--from-issue flag on build_historical_scenario.py for developer workflow",
        ],
        "scope_not_covered": [
            "Multi-user scenario submission (Phase 2 — architecture supports it)",
            "Scheduled automatic loop (requires schedule skill setup by user)",
        ],
    },
    227: {
        "issue": 227,
        "title": "Grace timer lost on HA restart; system stuck in manual override with 0 min remaining",
        "version_fixed": "0.3.56",
        "scope_covered": [
            "async_restore_state() re-schedules grace timer with remaining duration on startup",
            "If grace already expired during restart: override cleared immediately on startup",
            "Exception path: clears override as safety fallback",
        ],
        "scope_not_covered": [
            "Restart during active nat-vent (nat-vent flag already handled by separate restore logic)",
        ],
    },
    230: {
        "issue": 230,
        "title": "Grace expiry resumes from daytime classification instead of scheduled state",
        "version_fixed": "0.3.56",
        "scope_covered": [
            "_apply_current_scheduled_state() called after override clears on grace expiry",
            "If in bedtime window (after sleep_time, before wake_time): applies bedtime setback",
            "Otherwise: applies current classification",
            "Occupant wakes to scheduled temperature even when manual adjustment happened within grace window",
        ],
        "scope_not_covered": [
            "Morning wakeup convergence (wakeup time is close to grace expiry — edge case deferred)",
            "Multiple suppressed events during grace window (only most recent scheduled state applied)",
        ],
    },
    231: {
        "issue": 231,
        "title": "Nat-vent continues above home comfort ceiling while user is away",
        "version_fixed": "0.3.56",
        "scope_covered": [
            "check_natural_vent_conditions() adds ceiling exit when occupancy=away and indoor >= comfort_cool",
            "nat_vent_away_ceiling_exit event emitted; fan deactivated; HVAC setback takes over",
            "Free cooling within home comfort band (70-74°F) while away; setback (79°F) handles drift above that",
        ],
        "scope_not_covered": [
            "Vacation mode ceiling exit (vacation setback is higher; same principle applies but not yet implemented)",
        ],
    },
    247: {
        "issue": 247,
        "title": "Ceiling guard never escalated to AC when outdoor stayed below indoor"
        " (re-occurrence of #218's incomplete fix)",
        "version_fixed": "0.4.0",
        "scope_covered": [
            "apply_classification() ceiling-guard dormancy changed from 1 condition (outdoor<=indoor) to 3",
            " (outdoor<=indoor AND _natural_vent_active AND indoor<=ceiling threshold)",
            "Guard now evaluates+fires when indoor exceeds the ceiling even though outdoor<indoor"
            " (solar/internal gains out-pace ventilation) — the #247 reactive case",
            "Guard evaluates+fires when nat-vent is NOT running (windows closed / fan override) — the #215 case",
            "Escalation-on-fire (deactivate fan, clear _natural_vent_active, emit nat_vent_ceiling_escalation)"
            " from #218 part 2 is now reachable because the dormancy correctly lifts",
            "aggressive_savings widens the escalation threshold to"
            " comfort_cool + CEILING_ESCALATION_SAVINGS_MARGIN_F (2.0F)",
            "Warning-only no-op in check_natural_vent_conditions() replaced with an INFO log"
            " noting the guard will escalate",
        ],
        "scope_not_covered": [
            "Predictive pre-emption (firing before indoor crosses the ceiling based on the ODE curve under nat-vent)"
            " — deferred; the fix is reactive once indoor breaches the ceiling threshold",
            "Coordinator cadence (re-evaluation still every 30 min + 5-min revisit) — unchanged",
        ],
    },
    249: {
        "issue": 249,
        "title": "Thermostat-is-the-controller: program a comfort band instead of HVAC off + supervisory guards",
        "version_fixed": "0.4.0",
        "scope_covered": [
            "select_comfort_band() computes [floor, ceiling] from classification/occupancy/sleep/savings;"
            " occupied+awake = full comfort band [comfort_heat, comfort_cool] on ANY day type",
            "_apply_comfort_band() arms the band via the thermostat's command shape:"
            " dual -> heat_cool + target_temp_low/high; single -> cool@ceiling or heat@floor;"
            " emits comfort_band_applied",
            "All scheduled handlers (apply_classification, handle_bedtime, handle_occupancy_away/vacation,"
            " handle_morning_wakeup) route through the band primitive — no more off+setback divergence",
            "Nat-vent and economizer no longer set HVAC off — the band stays armed and only the fan is managed;"
            " the compressor self-arbitrates with the open window (free cooling stays free)",
            "aggressive_savings widens BOTH comfort edges by CEILING_ESCALATION_SAVINGS_MARGIN_F",
            "away/vacation/sleep keep setback/sleep bands; §6b/§6c demoted to passive backstops",
            "Thermostat capability detection (P1: ThermostatCapabilities) + sim harness arms the band",
        ],
        "scope_not_covered": [
            "Adaptive bedtime setback depth (compute_bedtime_setback) — the sleep band uses configured"
            " sleep_heat/sleep_cool; adaptive depth is a follow-up",
            "Heat-only thermostat on a warm day (cannot defend the ceiling) — band no-ops with an INFO log",
            "Single-setpoint mid-day edge re-selection — the band holds both edges via the device's shape",
        ],
    },
    264: {
        "issue": 264,
        "title": "Economizer no longer overrides the #249 comfort band (fan-assist only)",
        "version_fixed": "0.4.1",
        "scope_covered": [
            "check_window_cooling_opportunity() cool-down phase: removed _set_hvac_mode('cool') +"
            " _set_temperature(comfort_cool) — the #249 band already holds comfort_cool, so the"
            " economizer no longer flips the heat_cool band to single cool",
            "Cool-down now only activates the fan to assist the band's cooling (pull cool outdoor air"
            " through the open window); maintain phase unchanged (band stays armed, #249)",
            "Thermostat stays in the stable heat_cool band on hot days — one controller, no mode flip",
        ],
        "scope_not_covered": [
            "Full economizer retirement (its fan role overlaps natural ventilation) — deferred",
            "No economizer on/off toggle added — it remains gated only by hot-day + window-open +"
            " outdoor<=comfort_cool+delta + time-window eligibility",
            "Restart re-evaluation (home sits paused after restart with an open contact) — tracked in #263",
        ],
    },
    266: {
        "issue": 266,
        "title": "Dashboard Status tab shows dual comfort band setpoints for heat_cool thermostats",
        "version_fixed": "0.4.1",
        "scope_covered": [
            "Status card HVAC section: reads target_temp_low/target_temp_high when thermostat is in"
            " heat_cool mode; displays as 'Band: Xf / Yf' instead of a single target_temperature",
            "Status card is now status-only (no inline activity report) — activity report is a separate"
            " on-demand panel",
        ],
        "scope_not_covered": [
            "Historical band setpoint display in chart overlay — chart uses target_band time-series",
        ],
    },
    269: {
        "issue": 269,
        "title": "heat_cool manual override blind spots — 4 bugs",
        "version_fixed": "0.4.1",
        "scope_covered": [
            "Bug A: fan_mode change detection guard now includes _is_expected_confirmation (120s) so"
            " cloud-thermostat fan attribute echoes after CA's mode command are suppressed",
            "Bug B: hvac_mode now stored in coordinator.data and captured in incident_detected records",
            "Bug C: mode override detection uses _last_commanded_hvac_mode or classification.hvac_mode"
            " — heat_cool → cool user switch is now detected as a manual override",
            "Bug D: setpoint detection reads target_temp_high/target_temp_low in heat_cool mode"
            " (temperature attribute is None); grace trigger uses _last_commanded_hvac_mode",
        ],
        "scope_not_covered": [
            "Bug A false-negative: genuine fan change within 120s of a CA mode command (and while mode"
            " still matches last commanded) will be suppressed — bounded and documented trade-off",
            "Dual setpoint override recording uses the cooling setpoint (target_temp_high) as the"
            " representative value; independent heat-floor change has magnitude but no dedicated label",
        ],
    },
    239: {
        "issue": 239,
        "title": "CA fan activation falsely detected as manual override (fan_command_time race guard)",
        "version_fixed": "0.4.2",
        "scope_covered": [
            "AutomationEngine._fan_command_time: datetime | None — timestamp set at the start of"
            " _activate_fan() and _deactivate_fan() before any service call",
            "coordinator._is_recent_fan_command(threshold_seconds=30.0) — reads _fan_command_time;"
            " mirrors _is_recent_temp_command pattern",
            "_async_thermostat_changed fan_mode detection guard: now includes"
            " not _is_recent_fan_command(30.0) — suppresses echoes from CA's own set_fan_mode calls",
            "_async_fan_entity_changed guard: same guard added as belt-and-suspenders",
        ],
        "scope_not_covered": [
            "Restart race: if HA restarts mid-fan-session, _fan_command_time resets to None;"
            " an echo arriving immediately after restart is not suppressed (30-second window is"
            " acceptable given infrequency of restart-coincident echoes)",
        ],
    },
    277: {
        "issue": 277,
        "title": "Fan override false positives, whole-house fan behavioral gaps, timeline clarity",
        "version_fixed": "0.4.3",
        "scope_covered": [
            "Bug A1: _set_hvac_mode('off') fan_command_time guard — set_fan_mode(auto) assertion"
            " now stamps _fan_command_time before the service call; cloud echo suppressed by"
            " _is_recent_fan_command(30s)",
            "Bug B: _setpoint_override_detected mutual exclusion flag — single thermostat event"
            " triggers at most one override type (setpoint wins over fan_mode)",
            "Bug C: FAN_MODE_WHOLE_HOUSE HVAC suppression — _activate_fan captures _pre_fan_hvac_mode"
            " and sets HVAC off; _deactivate_fan restores prior mode; field persisted across restarts",
            "Bug D: handle_all_doors_windows_closed whole-house path — fan stopped when _fan_active"
            " and FAN_MODE_WHOLE_HOUSE/BOTH regardless of _natural_vent_active",
            "Bug F: activity report setpoint values in Settings column for override_detected events",
            "Bug G: AI investigator timing correlation section — [TIMING-COINCIDENT] flags for"
            " events at known automation intervals (30/90/5/10 min) after automation events",
            "Bug H: fan detection diagnostic logging — old/new fan_mode, fan_cmd age, hvac_cmd age,"
            " expected_confirmation value logged at INFO when handle_fan_manual_override() fires",
        ],
        "scope_not_covered": [
            "06:41 grace period root cause — unconfirmed; Bug H logging will make next occurrence"
            " diagnosable from HA logs",
            "FAN_MODE_HVAC (HVAC blower) HVAC behavior — band stays armed per Issue #249 §4; no change in this fix",
        ],
    },
    282: {
        "issue": 282,
        "title": "Override lifecycle — clean slate restart, grace notify, PATH B feedback, second override",
        "version_fixed": "0.4.4",
        "scope_covered": [
            "restore_state(): all override/grace fields (manual_override_active, grace_active,"
            " override_confirm_pending and related timestamps) now explicitly cleared to"
            " False/None regardless of saved state — clean slate on restart",
            "get_serializable_state(): override/grace fields removed (no point saving what isn't restored)",
            "async_restore_state(): grace-timer reschedule block removed — no grace timer"
            " is rescheduled after HA restart",
            "CONF_MANUAL_GRACE_NOTIFY default changed to True — manual grace expiry now"
            " notifies the user with override-specific message by default",
            "_confirm_override_expired PATH B: user notification sent when thermostat"
            " self-reverts within confirmation window",
            "_async_thermostat_changed: new branch detects mode change during active grace"
            " (different mode than current override) — clears override and restarts confirmation",
        ],
        "scope_not_covered": [
            "If user deliberately overrides and HA restarts, the override is lost (accepted"
            " trade-off — clean slate is simpler and more predictable than partial restoration)",
            "Override state is still NOT restored after restart — users must re-override"
            " post-restart if they want CA paused",
        ],
    },
    284: {
        "issue": 284,
        "title": "heat_cool setpoint write failure in door/window close and dashboard resume paths",
        "version_fixed": "0.4.5",
        "scope_covered": [
            "_set_temperature_for_mode(): added heat_cool branch calling _set_temperature_dual("
            "comfort_heat, comfort_cool) — previously returned silently, leaving thermostat at"
            " Ecobee-schedule values until next 30-min coordinator cycle",
            "Call site automation.py door/window close resume (~line 1668): now correctly writes"
            " both setpoints when classification is heat_cool",
            "Call site automation.py dashboard user resume (~line 1988): same fix",
            "ai_skills_investigator.py: target_temp_low and target_temp_high added to HVAC entity"
            " section of investigator context",
            "api.py: ca_target_heat and ca_target_cool added to status response",
            "frontend/index.html: conflict indicator (CA: X/Y) shown when live thermostat"
            " setpoints diverge from CA's comfort band by >1°F",
        ],
        "scope_not_covered": [
            "The 30-min coordinator cycle path (_apply_comfort_band) was already correct — this"
            " fix only affects event-driven restore paths outside the main cycle",
            "Setpoint tolerance / deadband not implemented — CA still overwrites if the Ecobee's"
            " own schedule applies different values; the correct fix is to update CA's comfort"
            " band config to match the desired setpoints",
        ],
    },
    286: {
        "issue": 286,
        "title": "Dual setpoint service call missing hvac_mode — Ecobee reverts to internal hold",
        "version_fixed": "0.4.6",
        "scope_covered": [
            "_set_temperature_dual(): added 'hvac_mode': 'heat_cool' to climate.set_temperature"
            " service payload — without it the Ecobee integration accepted the HA state update"
            " but the physical thermostat snapped back to its internal hold within ~1 second",
            "Log message now shows actual service values (service_low/service_high after"
            " from_fahrenheit conversion) alongside display-formatted values — previously the"
            " log showed internal °F strings regardless of what was actually sent to HA",
            "coordinator.py: DEBUG log at startup includes temp_unit, comfort_heat, comfort_cool"
            " — surfaces unit misconfiguration without requiring a config audit",
        ],
        "scope_not_covered": [
            "Post-command confirmation check not implemented — if the Ecobee still reverts after"
            " the hvac_mode fix (e.g., due to remaining internal hold programs), CA has no retry"
            " mechanism; investigate via startup log and thermostat state history",
            "Ecobee SmartAway / comfort program conflicts not addressed — if the Ecobee's own"
            " occupancy detection fires during a CA write, it may still override CA's setpoints",
        ],
    },
    290: {
        "version_fixed": "0.4.7",
        "title": "Grace expiry UI stale, bedtime lost on restart, setpoint validation, AI report Settings column",
        "scope_covered": [
            "automation.py _on_grace_expired(): calls _request_refresh_callback() on all three"
            " expiry paths so the coordinator immediately updates sensor state after override clears",
            "coordinator.py _check_startup_override(): if system is in sleep window and no override"
            " is active, calls handle_bedtime() so setback is re-applied on HA restart mid-night",
            "automation.py _set_temperature_dual() / _set_temperature(): 10-second"
            " async_call_later validation callback logs ERROR when thermostat reports setpoints"
            " that diverge from commanded values by more than 0.6 (service units); also emits"
            " setpoint_rejected event",
            "automation.py _set_temperature_dual(): sets _last_commanded_hvac_mode='heat_cool'"
            " after the service call so override detection compares against the correct mode",
            "automation.py handle_manual_override() / start_override_confirmation():"
            " accept old_setpoint_f / new_setpoint_f params and include them in override_detected"
            " event payload",
            "coordinator.py setpoint-only override path: passes old_temp / new_temp as"
            " old_setpoint_f / new_setpoint_f to handle_manual_override()",
            "ai_skills_activity.py: annotation code reads old_setpoint_f / new_setpoint_f from"
            " override_detected event (not the non-existent old_temp / new_temp); system prompt"
            " updated to match",
            "fake_hass.py: set_temperature service handler now updates entity state from"
            " hvac_mode in payload, matching real HA behavior",
        ],
        "scope_not_covered": [
            "Retry mechanism if setpoint validation fails — CA logs the mismatch but does not"
            " re-send the command; a subsequent classification cycle (30 min) will re-apply",
            "Grace period stuck-at-0 display issue in the dashboard when _cancel_grace_timers()"
            " is called without clearing _grace_end_time — cosmetic only, not addressed here",
            "Setpoint validation silently no-ops when thermostat drops the temperature attribute"
            " entirely (entity unavailable) — avoids false ERROR but means the failure goes"
            " undetected until the next classification cycle",
            "Startup bedtime recovery skipped when thermostat mode diverges from classification"
            " on restart — the mode-mismatch branch sets an override instead; this may be correct"
            " (real mode divergence is a legitimate override signal) but is untested",
        ],
    },
    293: {
        "version_fixed": "0.4.8",
        "title": "heat_cool startup override false positive + nat-vent restore drops dual-setpoint mode",
        "scope_covered": [
            "coordinator.py _check_startup_override(): heat_cool thermostat state is now treated"
            " as CA-compatible with cool/heat classifier outputs — no spurious override on restart",
            "automation.py _set_temperature_for_mode(): checks _get_thermostat_capabilities();"
            " for dual-setpoint thermostats emits _set_temperature_dual(floor, ceiling) on both"
            " cool and heat paths, preserving heat_cool mode after nat-vent restore",
            "ai_skills_activity.py async_build_activity_context(): reads temperature,"
            " target_temp_low, target_temp_high from climate entity and includes them in context"
            " block so AI can see and explain active setpoints",
            "frontend/index.html openGithubIssueModal(): GitHub issue title no longer prefixed"
            " with 'Climate Advisor:'; substring limit increased from 80 to 100 chars",
            "tests/test_startup_override.py TestStartupHeatCoolCompatibility: three cases covering"
            " heat_cool+cool→no override, heat_cool+heat→no override, cool+heat→override fires",
            "tests/test_nat_vent_restore_dual_setpoint.py TestNatVentRestoreDualSetpoint:"
            " dual-setpoint cool/pre-condition uses dual call, single-setpoint thermostat uses"
            " single call, heat mode dual call",
        ],
        "scope_not_covered": [
            "heat_cool startup state when the thermostat is in heat_cool but classification"
            " is 'off' — treated as incompatible (legitimate override signal) and not changed",
            "Nat-vent restore for thermostats that support heat_cool but currently in 'off' mode"
            " — _set_temperature_for_mode() does not handle the off→heat_cool transition",
            "pre_condition_target design question: 72°F ceiling persists all day on hot days by"
            " design (thermal buffer); making it morning-only (cease offset once indoor ≤ target)"
            " is out of scope for this fix",
        ],
    },
    299: {
        "version_fixed": "0.4.9",
        "title": "Ecobee dual-setpoint desync — double-write dedup bypass, hvac_mode conditional,"
        " setpoint confirmation check, startup cooldown guard",
        "scope_covered": [
            "automation.py _set_temperature(): now issues two service calls — offset pre-write"
            " (temp±1°F, direction chosen to never trigger conditioning) then exact target write;"
            " accepts mode='cool'|'heat' parameter so offset direction is always safe",
            "automation.py _set_temperature_dual(): same double-write pattern"
            " (low-1/high+1 pre-write then exact target); hvac_mode='heat_cool' included in"
            " pre-write only when thermostat is not already in heat_cool mode — omitted in"
            " target write in all cases; _write_seq nonce prevents stale validation callbacks",
            "automation.py _apply_comfort_band(): passes explicit mode='cool' or mode='heat'"
            " to all _set_temperature() callsites so offset direction is correct for each path",
            "automation.py _set_temperature_for_mode(): fallback defaults corrected to"
            " comfort_heat=70°F and comfort_cool=75°F (were 68°F/76°F)",
            "automation.py handle_bedtime(): 30-second cooldown guard skips the bedtime"
            " setpoint write if _temp_command_time is within the last 30s — eliminates startup"
            " race between coordinator's first classification cycle and the sleep-window handler",
            "coordinator.py _async_thermostat_changed(): _is_expected_confirmation() now checks"
            " that reported heat_cool setpoints are within 1°F of CA's pending setpoints;"
            " setpoints outside this window are treated as an Ecobee comfort-program reassertion,"
            " not a CA write confirmation",
            "All caller test files updated: 11 test files revised to expect 2 service calls"
            " per setpoint write (pre-write + target) and verify values at the correct call index",
        ],
        "scope_not_covered": [
            "Two-step mode transition (set_hvac_mode then set_temperature with delay) — not"
            " needed after hold-type change to 'hold until I change again' on the Ecobee device",
            "Celsius homes: pre-write offset is ±1°C (≈1.8°F); functionally identical dedup"
            " bypass behavior, no additional change needed",
            "Ecobee comfort-program reversion triggered by Ecobee app or physical thermostat"
            " control — CA will detect and re-apply on the next 30-min coordinator cycle",
        ],
    },
    263: {
        "version_fixed": "0.4.11",
        "title": "Post-restart pause recovery — clear _paused_by_door on restart (clean-slate)",
        "scope_covered": [
            "automation.py restore_state(): _paused_by_door and _pre_pause_mode are no longer"
            " restored from persisted state; engine starts clean on every HA restart",
            "Door/window state-change listener re-detects open sensors via None→'on' entity"
            " transition; HVAC briefly re-arms then re-pauses after the configured debounce"
            " (default 5 min) — strictly better than sitting paused indefinitely when cloud"
            " weather or thermostat services are slow to reconnect (Issue #263)",
            "tests/test_paused_restart_recovery.py: 7 new TDD tests covering clean-slate behavior",
            "docs/08-COMPUTATION-REFERENCE.md §11: documents the design decision and debounce timing",
        ],
        "scope_not_covered": [
            "Sensor entity that never re-registers after restart (broken sensor) — HVAC stays"
            " armed; user must manually re-pause or re-configure the sensor",
            "Debounce window (5 min) during which HVAC briefly runs — acceptable trade-off vs"
            " indefinite pause; no shorter debounce path is implemented",
        ],
    },
    295: {
        "version_fixed": "0.4.10",
        "title": "Pre-cool ceiling reverts to comfort setpoint after target achieved (#249 gap)",
        "scope_covered": [
            "AutomationEngine: _pre_condition_achieved flag — set when indoor_temp ≤"
            " comfort_cool + pre_condition_target; resets daily (date-keyed); persisted"
            " and restored via state dict so the gate survives HA restarts",
            "select_comfort_band(): receives pre_condition_achieved parameter; ceiling"
            " lowering skipped once flag is True — prevents the −2°F offset from holding"
            " all day after the home is already pre-cooled",
            "coordinator.py: both apply_classification() call sites pass indoor_temp so"
            " the gate evaluates correctly on every 30-min cycle",
            "tests/test_pre_condition_achieved.py: 18 new unit tests covering flag lifecycle,"
            " ceiling guard, daily reset, and state persistence",
            "Pending simulation scenario: hot_day_precool_achieved_reverts_to_comfort",
        ],
        "scope_not_covered": [
            "Hot days where indoor never reaches the pre-cool target — ceiling continues to"
            " apply for the full day (intended; home hasn't been pre-cooled yet)",
            "Consecutive hot days — flag resets at midnight so each day starts fresh",
        ],
    },
    301: {
        "version_fixed": "0.4.10",
        "title": "Revert heat_cool dual-setpoint; single-setpoint operation + 15-minute retry",
        "scope_covered": [
            "automation.py _set_temperature(): single climate.set_temperature call with"
            " {hvac_mode: mode, temperature: service_temp}; sets _last_commanded_hvac_mode/"
            " _hvac_command_time so coordinator suppresses the embedded mode-change echo",
            "automation.py _check_single_setpoint_accepted(): schedules 15-minute retry via"
            " async_call_later(900) on mismatch; retry is nonce-guarded (_write_seq) and"
            " cancels if a newer command has been issued",
            "automation.py _set_temperature_for_mode(): all caps.supports_dual_setpoint branches"
            " removed; always single-setpoint (heat→floor, cool→ceiling)",
            "automation.py _apply_comfort_band(): dual-setpoint path removed; ceiling guard"
            " uses mode='cool', floor guard uses mode='heat'",
            "automation.py _set_temperature_dual(): deleted entirely",
            "coordinator.py _async_thermostat_changed(): _is_expected_confirmation simplified —"
            " _setpoints_match dual-setpoint block removed (mode + 120s window sufficient)",
            "README.md: Thermostat Setup Requirements section added — disable built-in"
            " schedules/comfort programs; set hold type to indefinite",
        ],
        "scope_not_covered": [
            "Persistent rejection loop cap — if a thermostat indefinitely rejects CA's setpoint"
            " the 15-min retry fires indefinitely (bounded to one write per 15 min, emits"
            " setpoint_rejected event each cycle; 30-min classification cycle issues new commands"
            " that cancel stale retries in practice)",
            "Tier B integration test for off→cool echo suppression — coordinator confirmation"
            " logic is correct but no headless test drives the state-listener layer for this path",
        ],
    },
    310: {
        "version_fixed": "0.4.13",
        "title": "Periodic daily solar phase re-fit — fixes frozen solar_phase_offset_h (#185)",
        "scope_covered": [
            "coordinator.py: _maybe_run_periodic_solar_phase_fit() — new method gates a daily"
            " incremental (2-day) chart_log re-fit; fires once per calendar day after the one-shot"
            " backfill completes (_solar_phase_backfill=True)",
            "coordinator.py: _last_solar_phase_fit_date (date|None) persisted and restored via"
            " _build_state_dict() / async_restore_state(); one-shot block stamps this date to prevent"
            " a deploy-day double-fit",
            "coordinator.py: _async_update_data() calls _maybe_run_periodic_solar_phase_fit() when"
            " learning_enabled=True",
            "tests/test_solar_phase_periodic.py: 9 tests — 5 gate tests calling real"
            " _maybe_run_periodic_solar_phase_fit() via MethodType, 4 state persistence tests"
            " calling real _build_state_dict() / async_restore_state()",
            "docs/08-COMPUTATION-REFERENCE.md §5e-v: Two-tier fit scheduling subsection documenting"
            " one-shot backfill gate and periodic daily re-fit",
        ],
        "scope_not_covered": [
            "If no chart_log passive-daytime windows qualify (HVAC almost always on in summer),"
            " the daily re-fit will find nothing to learn — the #308 structured logging makes this"
            " visible via 'Solar phase fit: 0 windows passed quality filter' in ha_logs",
            "solar_gain abandonment rate — still 99/100 'abandoned' (flat indoor temps); addressed"
            " separately if #185 logging confirms HVAC-on is blocking all passive windows",
        ],
    },
    312: {
        "version_fixed": "0.4.13",
        "title": "AC duty-cycle secondary solar phase estimator — seasonal adaptation (#312)",
        "scope_covered": [
            "coordinator.py: _is_ac_duty_solar_day() quality filter (5 gates: setpoint"
            " presence, range [68-80°F], stability <1.5°F, ≥4 cool entries in 11-16h,"
            " indoor breach of setpoint); _estimate_ac_duty_solar_phase() peak-duty estimator;"
            " _run_ac_duty_solar_phase_fit() daily backfill runner",
            "learning.py: update_ac_duty_solar_phase_offset() — secondary EWMA α=0.07,"
            " writes to solar_phase_offset_ac_h only; never touches primary passive EWMA",
            "learning.py: _resolve_solar_phase_offset(cache) — 5-tier resolver:"
            " fresh primary → fresh secondary (obs≥3) → stale primary → stale secondary → default",
            "learning.py: solar_phase_offset_last_obs_date and solar_phase_offset_ac_last_obs_date"
            " fields; THERMAL_PARAM_STALE_DAYS=90 staleness gate — stale home-specific data"
            " is preferred over generic default, masked only when fresh data is available",
            "tests/test_solar_ac_phase.py: 21 new tests covering quality filter (5 reject"
            " paths + pass), AC phase estimator, 4 resolver precedence tests, 8 staleness tests",
            "docs/08-COMPUTATION-REFERENCE.md §5e-viii: two-EWMA architecture and 5-tier resolver documented",
        ],
        "scope_not_covered": [
            "Days with setpoint variance >1.5°F during 11-18h window are rejected — homes"
            " with frequent away/vacation setpoint changes learn the secondary EWMA slowly",
            "k_solar staleness gate — not yet implemented; tracked as future investigation"
            " in #314 (closed as working-as-designed for k_passive; only k_solar is at risk)",
        ],
    },
    318: {
        "title": "Sleep setpoint ordering constraint regression",
        "version_fixed": "0.4.15",
        "scope_covered": [
            "config_flow.py async_step_setpoints — removed 4 incorrect cross-field constraints"
            " on sleep_cool/sleep_heat vs comfort/setback bounds",
        ],
        "scope_not_covered": [
            "No runtime impact — automation.py uses sleep setpoints as-is; this fix is config flow validation only",
        ],
    },
    313: {
        "version_fixed": "0.4.14",
        "title": "False override + premature nat-vent exit after fan command (#313)",
        "scope_covered": [
            "coordinator.py _async_thermostat_changed(): setpoint-override detection block now"
            " checks `not self.automation_engine._fan_command_pending` and"
            " `not self._is_recent_fan_command(threshold_seconds=30.0)` — matches the existing"
            " pattern in the fan-mode change detection block at line ~2585",
            "automation.py _activate_fan(): schedules 30s sync callback"
            " (_verify_setpoint_after_fan_on) via async_call_later; callback re-asserts the"
            " last commanded setpoint via _set_temperature() if thermostat drifted >0.6°F,"
            " using _write_seq guard to skip if a newer command was issued",
            "automation.py _deactivate_fan(): same 30s verify-and-repair callback pattern"
            " (_verify_setpoint_after_fan_off)",
            "automation.py nat-vent exit condition: `outdoor >= indoor` changed to"
            " `outdoor > indoor` — equal temps (neutral airflow) no longer exit nat-vent",
            "tests/test_temp_command_guard.py: TestFanCommandSetpointGuard — 3 tests for"
            " pending flag, 30s recency, and expired (60s) genuine override",
            "tests/test_nat_vent_activation.py: TestNatVentExitEqualTemps — 3 tests"
            " (equal stays active, above exits, below stays active); TestPostFanVerify — 6"
            " tests (schedule on activate, schedule on deactivate, repair on drift, skip on"
            " write_seq advance, skip on manual override, skip within tolerance)",
        ],
        "scope_not_covered": [
            "Ecobee setpoint reversion >60s after the fan command (i.e., after the 30s verify"
            " fires but before the next classify cycle): the existing 15-min retry (#301) covers"
            " persistent drift; a second occurrence in the same session will be caught by the"
            " next classify cycle's setpoint re-assertion",
            "Pre-fan state validation (check thermostat matches expected setpoint BEFORE fan"
            " command): not needed for the #313 incident (setpoint was correct before fan-on);"
            " can be added if pre-drift becomes observed in production",
            "Tier B integration test for the full cascade (fan command → Ecobee revert →"
            " verify fires → re-assert): requires the coordinator state-listener layer;"
            " deferred to Tier B",
        ],
    },
    308: {
        "version_fixed": "0.4.12",
        "title": "k_solar confidence ladder + solar phase fit structured logging (#184/#185)",
        "scope_covered": [
            "learning.py get_thermal_model(): confidence_k_solar graded from observation_count_solar"
            " (none=0–19, low=≥20, medium=≥50, high=≥100); confidence_k_solar alias key added",
            "coordinator.py _run_solar_phase_chart_log_fit(): INFO logs at entry (entry count,"
            " date range), window filtering (N qualified), each EWMA update (old→new), and"
            " no-qualifying-windows exit; DEBUG logs for chart_log=None and empty-buffer guards",
            "tools/learning_db.py --model: Solar Model section with solar_phase_offset_h,"
            " first_active_date_phase_offset, observation_count_solar, confidence_k_solar,"
            " and rejection summary (attempts / committed / dominant reason / last 3 events)",
            "tests/test_solar_learning.py: 11 TDD tests — 9 confidence ladder, 2 logging",
            "docs/08-COMPUTATION-REFERENCE.md §5e: confidence_k_solar table + logging note",
        ],
        "scope_not_covered": [
            "Root cause of solar_phase_offset_h not updating (#185) — logging added in this PR;"
            " check 'Solar phase fit:' lines in ha_logs after deploy to determine if no qualifying"
            " chart_log windows exist (HVAC almost always on in summer) or peak-finding is failing."
            " A follow-up fix issue will be opened based on what the logs reveal.",
            "solar_gain abandonment rate (#184 context) — 99/100 rejections are 'abandoned' due to"
            " flat indoor temps; this is a data quality issue (HVAC prevents free-decay windows),"
            " not addressed by the confidence fix alone",
        ],
    },
    258: {
        "version_fixed": "0.4.19",
        "title": "Trend-aware overnight pre-cool with nat-vent coordination",
        "scope_covered": [
            "automation.py compute_bedtime_setback(): sign-convention fix — warming trend now lowers"
            " sleep ceiling (pre-cool) instead of raising it (energy setback)",
            "automation.py handle_pre_cool(): new method applies cooler ceiling at pre-cool trigger"
            " time; suppressed when nat-vent already achieved target; respects occupancy and override guards",
            "coordinator.py _compute_pre_cool_trigger_time(): trigger = nat-vent close + 30min or"
            " wake_time - 4h fallback; only fires when setback_modifier < 0",
            "coordinator.py: pre-cool scheduled in _async_update_data() when classification becomes"
            " available; cancelled and reset at end-of-day",
            "coordinator.py _compute_target_band_schedule(): chart target band dips to pre_cool_target"
            " from trigger_time to wake_time on warming-trend nights",
            "coordinator.py _compute_automation_status() + _async_update_data(): pre_cool_status"
            " field exposes scheduled/active/suppressed states",
            "api.py + index.html: pre_cool_status wired into existing Automation Status card",
            "briefing.py: warm-day section mentions pre-cool plan with target and time",
            "CLAUDE.md: Observability Requirements (logging + status page + chart) codified as"
            " universal standing standard for all future features",
        ],
        "scope_not_covered": [
            "Adaptive trigger timing from thermal model (wake-4h fallback is fixed, not k_active_cool-derived)",
            "Pre-cool depth does not account for forecast peak hour or solar gains",
            "Cooling-trend nights (setback_modifier > 0): relaxed setback sign fix also corrects"
            " those (higher ceiling = less cooling = energy savings) but no new timed phase added",
        ],
    },
    333: {
        "version_fixed": "0.4.28",
        "title": "Bedtime 'Next Automation' label and chart sleep band show wrong temperature",
        "scope_covered": [
            "automation.py compute_bedtime_setback(): removed setback_modifier from all 6 return"
            " paths — explicit heat, explicit cool, adaptive heat, adaptive cool,"
            " non-adaptive heat, non-adaptive cool",
            "_compute_next_automation_action(): bedtime label now reads raw CONF_SLEEP_HEAT/"
            "CONF_SLEEP_COOL from config instead of calling compute_bedtime_setback()",
            "chart sleep band: _compute_target_band_schedule() calls compute_bedtime_setback()"
            " for the band bounds — now returns configured temp, not trend-shifted temp",
        ],
        "scope_not_covered": [
            "handle_pre_cool() warming-trend path is intentionally unchanged"
            " — pre-cool still adjusts the mid-night ceiling via sleep_cool + setback_modifier",
            "future pre-heat feature (heat + cooling trend) not implemented — documented as"
            " design intent in issue #333 comment",
        ],
    },
    326: {
        "version_fixed": "0.4.23",
        "title": "Status tab: pre-cool in wrong card, 'tonight' hardcoded, 'Next Action' label ambiguous",
        "scope_covered": [
            "_maybe_schedule_pre_cool: stores _pre_cool_trigger_dt + _pre_cool_target; drops 'tonight'",
            "_async_pre_cool_trigger: clears _pre_cool_trigger_dt when trigger fires",
            "_async_end_of_day: resets _pre_cool_trigger_dt and _pre_cool_target",
            "_compute_next_automation_action: refactored events list to full datetimes;"
            " pre-cool injected as candidate — handles cross-midnight correctly",
            "index.html: Status card no longer shows pre_cool_status secondary text",
            "index.html: 'Next Action' label renamed to 'Next User Action'",
        ],
        "scope_not_covered": [
            "pre_cool_status field still returned by API (used by briefing and debug tab)",
            "pre-cool suppressed / active text remains in automation_status when relevant",
        ],
    },
    325: {
        "version_fixed": "0.4.22",
        "title": "async_call_later callbacks missing @callback decorator — HA thread-safety warning",
        "scope_covered": [
            "automation.py line 1409: lambda for _retry_callback → @callback _schedule_retry",
            "automation.py line 1421: lambda for _check_single_setpoint_accepted → @callback _schedule_check",
            "automation.py line 2913: _verify_setpoint_after_fan_on decorated with @callback",
            "automation.py line 3011: _verify_setpoint_after_fan_off decorated with @callback",
        ],
        "scope_not_covered": [
            "coordinator.py:245 _request_refresh_callback lambda — safe; only invoked from @callback context",
            "_on_grace_expired / clear_fan_override async_create_task — safe; always called from @callback chain",
        ],
    },
    321: {
        "version_fixed": "0.4.18",
        "title": "Startup false override, stuck grace, nat-vent thermostat cycling",
        "scope_covered": [
            "coordinator.py: 5-minute startup coalescing window replaces _check_startup_override();"
            " override detection suppressed during window; coalescing evaluates nat-vent and HVAC at t+5min",
            "automation.py _cancel_grace_timers(): _grace_end_time now cleared on every cancel",
            "coordinator.py _async_update_data(): stuck-grace guard detects stale grace_end_time"
            " in past and force-clears override with ERROR log",
            "automation.py: nat_vent_temperature_check() cycles fan on/off at midpoint±1°F;"
            " called from _async_thermostat_changed on every temperature tick",
            "automation.py _deactivate_fan(): restore_hvac=False parameter prevents HVAC mode"
            " restore during fan cycling (only restores on hard session exit)",
            "fan_status: new value 'nat-vent (session active, fan idle)' for cycling-paused state",
            "ai_skills_activity.py: stuck-grace warning flag in investigator context",
        ],
        "scope_not_covered": [
            "Nat-vent cycling not tested against real thermostat hardware (Tier B only)",
            "Startup coalesce timer requires coordinator integration test (Tier B);"
            " unit tests cover _do_startup_coalesce() logic directly",
            "Stuck grace requires coordinator 30-min update cycle for integration test (Tier B)",
        ],
    },
    320: {
        "title": "Nat vent debounce visibility — step logging and next_automation surfacing",
        "version_fixed": "0.4.17",
        "scope_covered": [
            "coordinator.py _async_door_window_changed — INFO log on sensor open with debounce expiry time",
            "coordinator.py _do_debounce — INFO log on expiry with classification context",
            "coordinator.py _compute_next_automation_action — returns 'Evaluating door/window sensors'"
            " with expiry time when debounce is pending",
            "automation.py handle_door_window_open — DEBUG log of gate values; INFO log when primary gates fail",
        ],
        "scope_not_covered": [
            "Nat vent blocked by forecast/thermal guards still produces a 30-min retry window"
            " (30-min coordinator cycle is the retry cadence)",
            "HA restart with sensors open: clean-slate behavior preserved — no automatic re-evaluation on restart",
        ],
    },
}

GITHUB_REPO = "gunkl/ClimateAdvisor"
GITHUB_REPO_URL = "https://github.com/gunkl/ClimateAdvisor"
GITHUB_API_BASE = "https://api.github.com"
GITHUB_CONTEXT_TIMEOUT = 5.0  # seconds — skip if API is slow
GITHUB_ISSUES_LIMIT = 15  # max issues to include in context

CONF_GITHUB_TOKEN = "github_token"
CONF_GITHUB_REPO = "github_repo"
API_SUBMIT_GITHUB_ISSUE = "/api/climate_advisor/submit_github_issue"

# Default setpoints (°F)
DEFAULT_COMFORT_HEAT = 70
DEFAULT_COMFORT_COOL = 75
DEFAULT_SETBACK_HEAT = 60
DEFAULT_SETBACK_COOL = 80

# Day type classifications
DAY_TYPE_HOT = "hot"
DAY_TYPE_WARM = "warm"
DAY_TYPE_MILD = "mild"
DAY_TYPE_COOL = "cool"
DAY_TYPE_COLD = "cold"

# Day type thresholds (°F) — used as defaults when user has not customised them.
THRESHOLD_HOT = 85
THRESHOLD_WARM = 75
THRESHOLD_MILD = 60
THRESHOLD_COOL = 45
CLASSIFICATION_HYSTERESIS_F = 2  # °F dead zone to prevent threshold bouncing

# Configurable day-type threshold keys and defaults.
# These mirror the THRESHOLD_* constants above; existing installs receive the
# same values via the v15→v16 migration default, so behaviour is unchanged.
CONF_THRESHOLD_HOT = "threshold_hot"
CONF_THRESHOLD_WARM = "threshold_warm"
CONF_THRESHOLD_MILD = "threshold_mild"
CONF_THRESHOLD_COOL = "threshold_cool"
DEFAULT_THRESHOLD_HOT = THRESHOLD_HOT
DEFAULT_THRESHOLD_WARM = THRESHOLD_WARM
DEFAULT_THRESHOLD_MILD = THRESHOLD_MILD
DEFAULT_THRESHOLD_COOL = THRESHOLD_COOL

# Trend thresholds (°F difference to trigger predictive behavior)
TREND_THRESHOLD_SIGNIFICANT = 10
TREND_THRESHOLD_MODERATE = 5

# Timing
DOOR_WINDOW_PAUSE_SECONDS = 180  # deprecated — use CONF_SENSOR_DEBOUNCE instead

# Door/window sensor configuration
CONF_SENSOR_POLARITY_INVERTED = "sensor_polarity_inverted"

# Temperature unit preference (stored as canonical fahrenheit internally)
CONF_TEMP_UNIT = "temp_unit"
DEFAULT_TEMP_UNIT = "fahrenheit"

# Thermal learning feature toggles (Issue #61)
CONF_ADAPTIVE_PREHEAT = "adaptive_preheat_enabled"
CONF_ADAPTIVE_SETBACK = "adaptive_setback_enabled"
CONF_WEATHER_BIAS = "weather_bias_enabled"

# Thermal learning threshold config keys (Issue #62)
CONF_MIN_PREHEAT_MINUTES = "min_preheat_minutes"
CONF_MAX_PREHEAT_MINUTES = "max_preheat_minutes"
CONF_DEFAULT_PREHEAT_MINUTES = "default_preheat_minutes"
CONF_PREHEAT_SAFETY_MARGIN = "preheat_safety_margin"
CONF_MAX_SETBACK_DEPTH = "max_setback_depth_f"

# Debounce and grace period config keys
CONF_SENSOR_DEBOUNCE = "sensor_debounce_seconds"
CONF_MANUAL_GRACE_PERIOD = "manual_grace_seconds"
CONF_MANUAL_GRACE_NOTIFY = "manual_grace_notify"
CONF_AUTOMATION_GRACE_PERIOD = "automation_grace_seconds"
CONF_AUTOMATION_GRACE_NOTIFY = "automation_grace_notify"
CONF_WELCOME_HOME_DEBOUNCE = "welcome_home_debounce_seconds"
CONF_OVERRIDE_CONFIRM_PERIOD = "override_confirm_seconds"
CONF_EMAIL_NOTIFY = "email_notify"  # DEPRECATED — replaced by per-event toggles in v8

# Per-event push notification toggles (Issue #50)
CONF_PUSH_BRIEFING = "push_briefing"
CONF_PUSH_DOOR_WINDOW_PAUSE = "push_door_window_pause"
CONF_PUSH_OCCUPANCY_HOME = "push_occupancy_home"

# Per-event email notification toggles (Issue #50)
CONF_EMAIL_BRIEFING = "email_briefing"
CONF_EMAIL_DOOR_WINDOW_PAUSE = "email_door_window_pause"
CONF_EMAIL_GRACE_EXPIRED = "email_grace_expired"
CONF_EMAIL_GRACE_REPAUSE = "email_grace_repause"
CONF_EMAIL_OCCUPANCY_HOME = "email_occupancy_home"

# Startup coalescing window: suppress override detection for this many seconds after restart
STARTUP_COALESCE_SECONDS: int = 300  # 5 minutes (Issue #321)

# Debounce and grace period defaults (seconds)
DEFAULT_SENSOR_DEBOUNCE_SECONDS = 300  # 5 minutes
DEFAULT_MANUAL_GRACE_SECONDS = 1800  # 30 minutes
DEFAULT_AUTOMATION_GRACE_SECONDS = 300  # 5 minutes
DEFAULT_WELCOME_HOME_DEBOUNCE_SECONDS = 3600  # 60 minutes
DEFAULT_OVERRIDE_CONFIRM_SECONDS = 600  # 10 minutes
OCCUPANCY_SETBACK_MINUTES = 15
MAX_CONTINUOUS_RUNTIME_HOURS = 3

# Economizer (window cooling) threshold
ECONOMIZER_TEMP_DELTA = 3  # °F — activate when outdoor temp within this delta of comfort_cool

# Economizer time boundaries for hot-day window cooling
ECONOMIZER_MORNING_START_HOUR = 6  # 6:00 AM
ECONOMIZER_MORNING_END_HOUR = 9  # 9:00 AM
ECONOMIZER_EVENING_START_HOUR = 17  # 5:00 PM
ECONOMIZER_EVENING_END_HOUR = 24  # midnight (end of day)

# Warm-day window timing — open early morning, close before outdoor temps climb
WARM_WINDOW_OPEN_HOUR = 6  # 6:00 AM
WARM_WINDOW_CLOSE_HOUR = 10  # 10:00 AM

# MILD-day window timing — open mid-morning, close late afternoon (Issue #147)
MILD_WINDOW_OPEN_HOUR = 10  # 10:00 AM fallback (was hardcoded in classifier.py)
MILD_WINDOW_CLOSE_HOUR = 17  # 5:00 PM fallback

# Occupancy toggle configuration
CONF_HOME_TOGGLE = "home_toggle_entity"
CONF_HOME_TOGGLE_INVERT = "home_toggle_invert"
CONF_VACATION_TOGGLE = "vacation_toggle_entity"
CONF_VACATION_TOGGLE_INVERT = "vacation_toggle_invert"
CONF_GUEST_TOGGLE = "guest_toggle_entity"
CONF_GUEST_TOGGLE_INVERT = "guest_toggle_invert"

# Occupancy mode values
OCCUPANCY_HOME = "home"
OCCUPANCY_AWAY = "away"
OCCUPANCY_VACATION = "vacation"
OCCUPANCY_GUEST = "guest"

# Vacation deeper setback (degrees beyond normal setback)
VACATION_SETBACK_EXTRA = 3

# Fan control configuration
CONF_FAN_ENTITY = "fan_entity"
CONF_FAN_STATE_ENTITY = "fan_state_entity"  # Issue #359: WHF Type 2 dual-entity support
CONF_FAN_STATE_FEEDBACK = "fan_state_feedback"  # Issue #361: command-only vs feedback mode
CONF_FAN_MODE = "fan_mode"
FAN_MODE_DISABLED = "disabled"
FAN_MODE_WHOLE_HOUSE = "whole_house_fan"
FAN_MODE_HVAC = "hvac_fan"
FAN_MODE_BOTH = "both"
DEFAULT_FAN_MODE = FAN_MODE_DISABLED

# Minimum fan runtime per hour (Issue #77)
CONF_FAN_MIN_RUNTIME_PER_HOUR = "fan_min_runtime_per_hour"
DEFAULT_FAN_MIN_RUNTIME_PER_HOUR = 0  # minutes; 0 = disabled

# Natural ventilation mode (door/window open + outdoor air within comfort range)
CONF_NATURAL_VENT_DELTA = "natural_vent_delta"
# Ceiling tolerance above comfort_cool for nat vent.
# Outdoor must also be below current indoor temperature (see NAT_VENT_HYSTERESIS_F guard).
DEFAULT_NATURAL_VENT_DELTA = 3.0

# Nat vent re-activation guards (Philosopher-approved, Issue #115)
# After an outdoor-warm exit (outdoor ≥ indoor), outdoor must be this many °F
# below indoor before re-activation is allowed. Prevents oscillation at equilibrium.
NAT_VENT_HYSTERESIS_F = 1.0

# Minimum seconds between an outdoor-warm exit and the next re-activation check.
# 5 minutes prevents whiplash cycling when temps are near-equal.
NAT_VENT_REACTIVATION_LOCKOUT_S = 300

CONF_NAT_VENT_HYSTERESIS_F = "nat_vent_hysteresis_f"
CONF_NAT_VENT_REACTIVATION_LOCKOUT_S = "nat_vent_reactivation_lockout_s"

# Minimum viable nat vent window — skip activation (or exit proactively) if thermal
# model predicts indoor will hit comfort_heat floor within this many hours.
MIN_VIABLE_NAT_VENT_HOURS = 1.0

# State persistence
STATE_FILE = "climate_advisor_state.json"

# Chart state log
CHART_LOG_FILE = "climate_advisor_chart_log.json"
CHART_LOG_MAX_DAYS = 365  # 1-year rolling cap (~17,500 entries ≈ 2MB)
CHART_DOWNSAMPLE_HOURLY_DAYS = 3  # raw points for ≤3 days; hourly averages beyond
CHART_DOWNSAMPLE_DAILY_DAYS = 30  # daily summaries for >30 days

# Prediction archive — first-write-wins historical pred_indoor
PRED_ARCHIVE_HORIZON_HOURS = 4  # only archive ODE entries within this lookahead window

# Learning system
LEARNING_DB_FILE = "climate_advisor_learning.json"
SUGGESTION_COOLDOWN_DAYS = 7  # Don't repeat the same suggestion within a week
MIN_DATA_POINTS_FOR_SUGGESTION = 14  # Need 2 weeks of data before suggesting changes
COMPLIANCE_THRESHOLD_LOW = 0.3  # Below 30% compliance triggers a suggestion
COMPLIANCE_THRESHOLD_HIGH = 0.8  # Above 80% means the advice is working

# Temperature source types
TEMP_SOURCE_SENSOR = "sensor"
TEMP_SOURCE_INPUT_NUMBER = "input_number"
TEMP_SOURCE_WEATHER_SERVICE = "weather_service"
TEMP_SOURCE_CLIMATE_FALLBACK = "climate_fallback"

# Sensor attributes
ATTR_DAY_TYPE = "day_type"
ATTR_TREND = "trend_direction"
ATTR_TREND_MAGNITUDE = "trend_magnitude"
ATTR_BRIEFING = "daily_briefing"
ATTR_BRIEFING_SHORT = "daily_briefing_short"
ATTR_NEXT_ACTION = "next_human_action"
ATTR_AUTOMATION_STATUS = "automation_status"
ATTR_LEARNING_SUGGESTIONS = "pending_suggestions"
ATTR_COMPLIANCE_SCORE = "compliance_score"
ATTR_ESTIMATED_SAVINGS = "estimated_savings"
ATTR_AUTOMATION_ENABLED = "automation_enabled"
ATTR_NEXT_AUTOMATION_ACTION = "next_automation_action"
ATTR_NEXT_AUTOMATION_TIME = "next_automation_time"
ATTR_OCCUPANCY_MODE = "occupancy_mode"
ATTR_LAST_ACTION_TIME = "last_action_time"
ATTR_LAST_ACTION_REASON = "last_action_reason"
ATTR_FAN_STATUS = "fan_status"
ATTR_WHF_STATUS = "whf_status"
ATTR_HVAC_FAN_STATUS = "hvac_fan_status"
ATTR_FAN_RUNTIME = "fan_runtime_minutes"
ATTR_FAN_OVERRIDE_SINCE = "fan_override_since"
ATTR_FAN_RUNNING = "fan_running"
ATTR_CURRENT_SETPOINT = "current_setpoint"
ATTR_INDOOR_TEMP = "indoor_temp"
ATTR_OUTDOOR_TEMP = "outdoor_temp"
ATTR_FORECAST_HIGH = "forecast_high"
ATTR_FORECAST_LOW = "forecast_low"
ATTR_FORECAST_HIGH_TOMORROW = "forecast_high_tomorrow"
ATTR_FORECAST_LOW_TOMORROW = "forecast_low_tomorrow"
ATTR_HVAC_ACTION = "hvac_action"
ATTR_HVAC_RUNTIME_TODAY = "hvac_runtime_today"
ATTR_CONTACT_STATUS = "contact_status"

# Revisit delay — follow-up check after any HVAC action (seconds)
REVISIT_DELAY_SECONDS = 300  # 5 minutes

# Event log ring buffer cap (Issue #76)
EVENT_LOG_CAP = 500  # keep last 500 events

# API paths for dashboard panel
API_BASE = "/api/climate_advisor"
API_STATUS = f"{API_BASE}/status"
API_BRIEFING = f"{API_BASE}/briefing"
API_CHART_DATA = f"{API_BASE}/chart_data"
API_AUTOMATION_STATE = f"{API_BASE}/automation_state"
API_LEARNING = f"{API_BASE}/learning"
API_FORCE_RECLASSIFY = f"{API_BASE}/force_reclassify"
API_SEND_BRIEFING = f"{API_BASE}/send_briefing"
API_RESPOND_SUGGESTION = f"{API_BASE}/respond_suggestion"
API_CONFIG = f"{API_BASE}/config"
API_CANCEL_OVERRIDE = f"{API_BASE}/cancel_override"
API_CANCEL_FAN_OVERRIDE = f"{API_BASE}/cancel_fan_override"
API_RESUME_FROM_PAUSE = f"{API_BASE}/resume_from_pause"
API_TOGGLE_AUTOMATION = f"{API_BASE}/toggle_automation"
API_EVENT_LOG = f"{API_BASE}/event_log"
API_ENGINES = f"{API_BASE}/engines"

# Panel
PANEL_URL = "/climate_advisor/frontend"
PANEL_FRONTEND_PATH = "climate-advisor"

# Configuration metadata for the Settings tab.
# When adding new config options, update this dict so the Settings tab
# displays the new option with a proper description.
CONFIG_METADATA = {
    "weather_entity": {
        "label": "Weather Entity",
        "description": (
            "The weather integration used for forecast data."
            " Determines day type classification and all downstream automation decisions."
        ),
        "category": "core",
    },
    "climate_entity": {
        "label": "Thermostat Entity",
        "description": (
            "The climate entity Climate Advisor controls. All HVAC mode and temperature commands go to this entity."
        ),
        "category": "core",
    },
    "comfort_heat": {
        "label": "Comfort Heat",
        "description": (
            "Target temperature when heating is active. Lowering saves energy but may feel cooler."
            " Used for morning wake-up and occupancy-home restores."
        ),
        "category": "setpoints",
    },
    "comfort_cool": {
        "label": "Comfort Cool",
        "description": (
            "Target temperature when cooling is active. Raising saves energy but may feel warmer."
            " The economizer uses this as the threshold for window cooling decisions."
        ),
        "category": "setpoints",
    },
    "setback_heat": {
        "label": "Setback Heat",
        "description": (
            "Temperature when heating and away from home."
            " Lower values save more energy but take longer to recover when you return."
        ),
        "category": "setpoints",
    },
    "setback_cool": {
        "label": "Setback Cool",
        "description": (
            "Temperature when cooling and away from home."
            " Higher values save more energy but take longer to cool down when you return."
        ),
        "category": "setpoints",
    },
    "notify_service": {
        "label": "Notification Service",
        "description": "The HA notify service used for alerts and briefings (e.g., notify.mobile_app).",
        "category": "core",
    },
    CONF_TEMP_UNIT: {
        "label": "Temperature Unit",
        "description": (
            "Whether setpoints and displayed temperatures use Fahrenheit or Celsius. "
            "Setpoints are stored internally in Fahrenheit; changing this unit affects "
            "how they are displayed and entered in the UI."
        ),
        "category": "core",
    },
    "outdoor_temp_source": {
        "label": "Outdoor Temp Source",
        "description": (
            "Where outdoor temperature is read from:"
            " the weather service, a dedicated sensor, or an input_number helper."
        ),
        "category": "sensors",
    },
    "indoor_temp_source": {
        "label": "Indoor Temp Source",
        "description": (
            "Where indoor temperature is read from:"
            " the thermostat's built-in sensor, a dedicated sensor, or an input_number helper."
        ),
        "category": "sensors",
    },
    "door_window_sensors": {
        "label": "Door/Window Sensors",
        "description": (
            "Binary sensors that detect open doors and windows."
            " When open past the debounce period, HVAC pauses to avoid wasting energy."
        ),
        "category": "sensors",
    },
    "sensor_polarity_inverted": {
        "label": "Sensor Polarity Inverted",
        "description": (
            "Enable if your sensors report 'off' when open (some reed switches work this way)."
            " Incorrect polarity means HVAC pauses when doors are closed."
        ),
        "category": "sensors",
    },
    "sensor_debounce_seconds": {
        "label": "Sensor Debounce (minutes)",
        "description": (
            "How long a door/window must stay open before HVAC pauses."
            " Short values react faster but may cause unnecessary pauses for quick trips through a door."
        ),
        "category": "sensors",
        "display_transform": "seconds_to_minutes",
        "default": DEFAULT_SENSOR_DEBOUNCE_SECONDS,
    },
    "manual_grace_seconds": {
        "label": "Manual Grace Period (minutes)",
        "description": (
            "After you manually change the thermostat — either the HVAC mode or the target temperature —"
            " CA waits this many minutes before resuming automated setpoint control."
            " Also prevents re-pausing if a door/window opens during this window. Default: 30 minutes."
        ),
        "category": "sensors",
        "display_transform": "seconds_to_minutes",
        "default": DEFAULT_MANUAL_GRACE_SECONDS,
    },
    "manual_grace_notify": {
        "label": "Push: Manual Grace Expired",
        "description": "Push notification when manual grace expires and normal behavior resumes.",
        "category": "notifications",
    },
    "automation_grace_seconds": {
        "label": "Automation Grace Period (minutes)",
        "description": (
            "After Climate Advisor resumes HVAC (all doors/windows closed),"
            " this grace window prevents immediate re-pausing if a door opens briefly."
        ),
        "category": "sensors",
        "display_transform": "seconds_to_minutes",
        "default": DEFAULT_AUTOMATION_GRACE_SECONDS,
    },
    "automation_grace_notify": {
        "label": "Push: Automation Grace Expired",
        "description": "Send a push notification when the automation grace period expires.",
        "category": "notifications",
    },
    "override_confirm_seconds": {
        "label": "Override Confirmation Delay (minutes)",
        "description": (
            "Time between system changes and confirmation of manual override."
            " When a change looks like a manual override, Climate Advisor waits this long before formally accepting it."
            " Transient events (thermostat restart, fan cycle) that resolve within the window are ignored."
            " Set to 0 to confirm overrides immediately."
        ),
        "category": "sensors",
        "display_transform": "seconds_to_minutes",
        "default": DEFAULT_OVERRIDE_CONFIRM_SECONDS,
    },
    "fan_mode": {
        "label": "Fan Control Mode",
        "description": (
            "Controls how fans assist ventilation. 'Whole house fan' controls a dedicated entity."
            " 'HVAC fan' uses the thermostat fan mode. 'Both' uses both."
            " Fan activates during economizer maintain phase."
        ),
        "category": "fan",
    },
    "fan_entity": {
        "label": "Fan Entity",
        "description": (
            "The fan or switch entity to control for whole-house ventilation."
            " Only used when fan mode is 'whole_house_fan' or 'both'."
        ),
        "category": "fan",
    },
    "fan_state_entity": {
        "label": "Fan State Entity",
        "description": (
            "Optional separate sensor entity to read the actual physical state of the whole-house fan."
            " Use when the fan has a dedicated control entity and a separate state sensor (WHF dual-entity)."
            " If left blank, the Fan Entity is used for both control and state."
        ),
        "sensitive": False,
        "category": "fan",
    },
    "fan_state_feedback": {
        "label": "Fan state feedback reliable",
        "description": (
            "Turn ON if your fan entity or state sensor reports actual motor state "
            "(not just the last command sent). Leave OFF if you're not sure — CA will "
            "command the fan to the desired state on every cycle without reading back "
            "the entity state. Physical wall-switch overrides are undetectable when OFF."
        ),
        "category": "fan",
        "sensitive": False,
        "default": False,
    },
    "fan_min_runtime_per_hour": {
        "label": "Fan Minimum Runtime Per Hour",
        "description": (
            "Minutes of fan runtime per hour (0 = disabled, 60 = always on)."
            " Activates the fan for the specified duration each hour for air"
            " circulation. The cycle start time is offset from the clock hour"
            " based on when HA started."
        ),
        "category": "fan",
    },
    "home_toggle_entity": {
        "label": "Home/Away Toggle",
        "description": (
            "An entity that indicates whether someone is home. ON = home, OFF = away."
            " Climate Advisor applies setback temperatures when away."
        ),
        "category": "occupancy",
    },
    "home_toggle_invert": {
        "label": "Invert Home Toggle",
        "description": "Enable if your toggle reports ON when you're away and OFF when you're home.",
        "category": "occupancy",
    },
    "vacation_toggle_entity": {
        "label": "Vacation Mode Toggle",
        "description": (
            "An entity that indicates vacation mode."
            " When active, Climate Advisor applies a deeper temperature setback for extended energy savings."
        ),
        "category": "occupancy",
    },
    "vacation_toggle_invert": {
        "label": "Invert Vacation Toggle",
        "description": "Enable if your toggle reports ON when you're NOT on vacation.",
        "category": "occupancy",
    },
    "guest_toggle_entity": {
        "label": "Guest Mode Toggle",
        "description": (
            "An entity that indicates guests are present."
            " Overrides vacation and away modes — the house stays at comfort temperature while guests are visiting."
        ),
        "category": "occupancy",
    },
    "guest_toggle_invert": {
        "label": "Invert Guest Toggle",
        "description": "Enable if your toggle reports ON when guests are NOT present.",
        "category": "occupancy",
    },
    "welcome_home_debounce_seconds": {
        "label": "Welcome Home Quiet Period (minutes)",
        "description": (
            "Minimum time between welcome home notifications. If someone leaves and returns"
            " within this window, the notification is suppressed. Set to 0 to always notify."
        ),
        "category": "occupancy",
        "display_transform": "seconds_to_minutes",
        "default": DEFAULT_WELCOME_HOME_DEBOUNCE_SECONDS,
    },
    "wake_time": {
        "label": "Wake Time",
        "description": (
            "When morning comfort temperatures are restored."
            " Earlier times mean the house is comfortable when you get up but use more energy overnight."
        ),
        "category": "schedule",
    },
    "sleep_time": {
        "label": "Sleep Time",
        "description": (
            "When bedtime temperatures take effect. The system transitions to your sleep temperatures at this time."
        ),
        "category": "schedule",
    },
    "sleep_heat": {
        "label": "Sleep Temperature (Heat)",
        "description": (
            "Target temperature during sleep hours when you are home."
            " Independent from your away setback — use this to stay warmer at night"
            " than when you leave the house."
        ),
        "category": "setpoints",
    },
    "sleep_cool": {
        "label": "Sleep Temperature (Cool)",
        "description": (
            "Target temperature during sleep hours when you are home."
            " Independent from your away setback — use this to stay cooler at night"
            " than when you leave the house."
        ),
        "category": "setpoints",
    },
    "briefing_time": {
        "label": "Briefing Time",
        "description": (
            "When the daily climate briefing is generated and sent."
            " Should be before wake_time so you see it when you get up."
        ),
        "category": "schedule",
    },
    "learning_enabled": {
        "label": "Learning Engine",
        "description": (
            "When enabled, Climate Advisor tracks patterns"
            " (manual overrides, window compliance, runtime) and generates adaptive suggestions over time."
        ),
        "category": "advanced",
    },
    "adaptive_preheat_enabled": {
        "category": "advanced",
        "label": "Adaptive Pre-heat Timing",
        "description": "Use learned heating rate to compute pre-heat start time.",
    },
    "adaptive_setback_enabled": {
        "category": "advanced",
        "label": "Adaptive Bedtime Setback",
        "description": "Use learned heating/cooling rate to compute maximum safe setback depth.",
    },
    "weather_bias_enabled": {
        "category": "advanced",
        "label": "Weather Forecast Bias Correction",
        "description": (
            "Apply a location-specific correction to tomorrow's forecast based on observed forecast accuracy."
        ),
    },
    "min_preheat_minutes": {
        "label": "Minimum Pre-heat Time (min)",
        "description": "Shortest pre-heat window the system will ever schedule.",
        "category": "advanced",
    },
    "max_preheat_minutes": {
        "label": "Maximum Pre-heat Time (min)",
        "description": "Longest pre-heat window the system will ever schedule.",
        "category": "advanced",
    },
    "default_preheat_minutes": {
        "label": "Default Pre-heat Time (min)",
        "description": "Pre-heat duration used before enough observations are collected.",
        "category": "advanced",
    },
    "preheat_safety_margin": {
        "label": "Pre-heat Safety Margin",
        "description": ("Multiplier applied to model-computed pre-heat time as a buffer (e.g. 1.2 = 20% extra)."),
        "category": "advanced",
    },
    "max_setback_depth_f": {
        "label": "Maximum Setback Depth (°F)",
        "description": "Largest overnight setback the adaptive engine may compute.",
        "category": "advanced",
    },
    "aggressive_savings": {
        "label": "Prefer Savings Over Comfort",
        "description": (
            "When enabled, favors energy savings: the economizer skips AC-assisted cooling"
            " (ventilation only when windows open), and setbacks may be more aggressive."
            " When disabled, AC actively cools to comfort when outdoor temps drop."
        ),
        "category": "advanced",
    },
    "threshold_hot": {
        "label": "Hot Day Threshold",
        "description": (
            "Days whose forecast high is at or above this temperature are classified as Hot. Default: 85°F / 29°C."
        ),
        "category": "advanced",
    },
    "threshold_warm": {
        "label": "Warm Day Threshold",
        "description": (
            "Days whose forecast high is at or above this temperature (but below Hot) are"
            " classified as Warm. Default: 75°F / 24°C."
        ),
        "category": "advanced",
    },
    "threshold_mild": {
        "label": "Mild Day Threshold",
        "description": (
            "Days whose forecast high is at or above this temperature (but below Warm) are"
            " classified as Mild. Default: 60°F / 16°C."
        ),
        "category": "advanced",
    },
    "threshold_cool": {
        "label": "Cool Day Threshold",
        "description": (
            "Days whose forecast high is at or above this temperature (but below Mild) are"
            " classified as Cool; below is Cold. Default: 45°F / 7°C."
        ),
        "category": "advanced",
    },
    "push_briefing": {
        "label": "Push: Daily Briefing",
        "description": "Send a short TLDR briefing summary to your phone each morning.",
        "category": "notifications",
    },
    "push_door_window_pause": {
        "label": "Push: HVAC Paused",
        "description": "Send a push notification when HVAC is paused due to an open door or window.",
        "category": "notifications",
    },
    "push_occupancy_home": {
        "label": "Push: Welcome Home",
        "description": "Send a push notification when someone arrives home and comfort temperature is restored.",
        "category": "notifications",
    },
    "email_briefing": {
        "label": "Email: Full Daily Briefing",
        "description": "Send the full daily briefing via email with complete forecast and plan details.",
        "category": "notifications",
    },
    "email_door_window_pause": {
        "label": "Email: HVAC Paused",
        "description": "Send an email when HVAC is paused due to an open door or window.",
        "category": "notifications",
    },
    "email_grace_expired": {
        "label": "Email: Grace Period Expired",
        "description": "Send an email when a grace period expires and normal sensor behavior resumes.",
        "category": "notifications",
    },
    "email_grace_repause": {
        "label": "Email: Re-paused",
        "description": "Email when HVAC is re-paused because a door/window is still open after grace.",
        "category": "notifications",
    },
    "email_occupancy_home": {
        "label": "Email: Welcome Home",
        "description": "Send an email when someone arrives home and comfort temperature is restored.",
        "category": "notifications",
    },
    "ai_enabled": {
        "label": "Enable AI Features",
        "description": (
            "Master switch for all AI-powered features."
            " When disabled, Climate Advisor uses only its built-in coded logic."
        ),
        "category": "ai_settings",
    },
    "ai_api_key": {
        "label": "Claude API Key",
        "description": (
            "Your Anthropic API key. Stored securely in Home Assistant's config entry."
            " Never logged or exposed in sensor attributes."
        ),
        "category": "ai_settings",
        "sensitive": True,
    },
    "ai_model": {
        "label": "AI Model",
        "description": (
            "Which Claude model to use."
            " Sonnet is recommended for cost/quality balance."
            " Haiku is cheapest. Opus is most capable but expensive."
        ),
        "category": "ai_settings",
    },
    "ai_reasoning_effort": {
        "label": "Reasoning Effort",
        "description": (
            "How much reasoning effort Claude uses."
            " Higher effort produces better analysis but uses more tokens and costs more."
        ),
        "category": "ai_settings",
    },
    "ai_max_tokens": {
        "label": "Max Response Length (tokens)",
        "description": (
            "Maximum length of AI responses in tokens. Higher values allow more detailed analysis but cost more."
        ),
        "category": "ai_settings",
    },
    "ai_temperature": {
        "label": "Creativity (temperature)",
        "description": (
            "Controls randomness in AI responses. 0 = deterministic, 1.0 = most creative. 0.3 recommended for analysis."
        ),
        "category": "ai_settings",
    },
    "ai_monthly_budget": {
        "label": "Monthly Budget Cap ($)",
        "description": (
            "Maximum estimated monthly spend in USD. Set to 0 for no limit. AI features pause when budget is reached."
        ),
        "category": "ai_settings",
    },
    "ai_auto_requests_per_day": {
        "label": "Auto Requests Per Day",
        "description": (
            "Maximum automated/scheduled AI requests per day."
            " Limits unattended usage from features like daily plan generation."
            " Resets at midnight."
        ),
        "category": "ai_settings",
    },
    "ai_manual_requests_per_day": {
        "label": "Manual Requests Per Day",
        "description": (
            "Maximum user-triggered AI requests per day."
            " Limits on-demand usage from features like the Activity Report."
            " Resets at midnight."
        ),
        "category": "ai_settings",
    },
    "ai_investigator_enabled": {
        "label": "Enable Investigative Agent",
        "description": (
            "Enable the investigative agent, which performs deep cross-source analysis"
            " to find incongruities, data quality issues, and system errors."
            " Requires AI to be enabled and configured. Default is off."
        ),
        "category": "ai_settings",
    },
    "ai_investigator_model": {
        "label": "Investigator AI Model",
        "description": (
            "Which Claude model the investigative agent uses."
            " Opus is recommended for deep analysis. Sonnet is a cost-effective alternative."
        ),
        "category": "ai_settings",
    },
    "ai_investigator_reasoning_effort": {
        "label": "Investigator Reasoning Effort",
        "description": (
            "How much extended thinking the investigator uses."
            " High is recommended — the agent needs to reason through multiple hypotheses."
        ),
        "category": "ai_settings",
    },
    "ai_investigator_max_tokens": {
        "label": "Investigator Max Response Length (tokens)",
        "description": (
            "Maximum token length for investigator reports."
            " Larger values allow more detailed findings. 8192 recommended."
        ),
        "category": "ai_settings",
    },
    "ai_investigator_requests_per_day": {
        "label": "Investigator Requests Per Day",
        "description": (
            "Maximum investigative analysis runs per day."
            " Each investigation uses extended thinking and is more expensive than activity reports."
            " Resets at midnight."
        ),
        "category": "ai_settings",
    },
}

# ---------------------------------------------------------------------------
# Thermal Model Learning (Issue #61)
# ---------------------------------------------------------------------------
MIN_THERMAL_SESSION_MINUTES = 5  # ignore sessions shorter than this (was 10; Ecobee cycles 7-9 min)
MIN_THERMAL_OBSERVATIONS = 5  # min obs before model is trusted
THERMAL_MODEL_MAX_OBS = 30  # use only most recent N observations
THERMAL_POST_HEAT_TIMEOUT_MINUTES = 45  # abandon post_heat phase after this long
THERMAL_STABILIZATION_THRESHOLD_F = 0.3  # |dT| < this over window → stabilized
THERMAL_STABILIZATION_WINDOW_MINUTES = 5  # window length for stabilization check
THERMAL_K_PASSIVE_MIN = -0.5  # reject k_passive outside this range (hr⁻¹)
THERMAL_K_PASSIVE_MAX = -0.001  # upper bound: near-zero decay (extremely well-insulated house)
THERMAL_K_ACTIVE_HEAT_MIN = 0.5  # reject k_active_heat outside this range (°F/hr)
THERMAL_K_ACTIVE_HEAT_MAX = 15.0  # upper bound: physically implausible heating rate
THERMAL_K_ACTIVE_COOL_MIN = -15.0  # reject k_active_cool outside this range (°F/hr)
THERMAL_K_ACTIVE_COOL_MAX = -0.5  # upper bound (least negative): minimal cooling effect
THERMAL_MIN_R_SQUARED = 0.2  # reject observation if R² below this
THERMAL_MIN_POST_HEAT_SAMPLES = 4  # min post-heat samples to commit (Issue #130: lowered from 10, enables short cycles)
THERMAL_PRE_HEAT_BUFFER_MINUTES = 15  # rolling pre-heat buffer length
THERMAL_SAMPLE_INTERVAL_SECONDS = 60  # sampling cadence during active/post_heat
THERMAL_MAX_ACTIVE_SAMPLES = 120  # cap on active_samples list per event
THERMAL_MAX_POST_HEAT_SAMPLES = 45  # cap on post_heat_samples list per event
DEFAULT_PREHEAT_MINUTES = 120  # fallback when no model data
MIN_PREHEAT_MINUTES = 30  # clamp floor
MAX_PREHEAT_MINUTES = 240  # clamp ceiling (4 hrs)
PREHEAT_SAFETY_MARGIN = 1.3  # multiply computed time by this
DEFAULT_SETBACK_DEPTH_F = 4.0  # preserved fallback (current heat setback)
DEFAULT_SETBACK_DEPTH_COOL_F = 3.0  # preserved fallback (current cool setback)

# Conservative heat setback on cold days (shallower than normal to aid morning recovery)
COLD_DAY_SETBACK_DEPTH_F: float = 3.0

# Window opportunity: today/tomorrow low must be at or below this to open windows on a hot day
WINDOW_OPPORTUNITY_MAX_LOW_F: float = 80.0

# Thermal factor bucket boundaries (outdoor temp in °F, internal representation)
THERMAL_COLD_BUCKET_LIMIT_F: float = 60.0  # below this → "cold" regime
THERMAL_MILD_BUCKET_LIMIT_F: float = 70.0  # below this (≥ cold limit) → "mild" regime

# Thermal factor interpolation zone half-width (°F either side of each bucket boundary)
# Eliminates hard jumps when outdoor temp crosses a threshold.
THERMAL_BUCKET_INTERP_HALF_F: float = 2.0

THERMAL_MIN_DECAY_F = 1.0  # min total post-heat decay required to commit (°F)

# --- v3 Observation Type string constants ---
OBS_TYPE_PASSIVE_DECAY = "passive_decay"
OBS_TYPE_FAN_ONLY_DECAY = "fan_only_decay"
OBS_TYPE_VENTILATED_DECAY = "ventilated_decay"
OBS_TYPE_SOLAR_GAIN = "solar_gain"
OBS_TYPE_HVAC_HEAT = "hvac_heat"
OBS_TYPE_HVAC_COOL = "hvac_cool"

# Thermal rejection reason codes (emitted in ThermalRejectionEvent)
REJECT_TOO_FEW_SAMPLES = "too_few_samples"
REJECT_SMALL_DELTA = "small_delta"
REJECT_OLS_BAD_FIT = "ols_bad_fit"
REJECT_OLS_WRONG_SIGN = "ols_wrong_sign"
REJECT_OLS_BOUNDS = "ols_bounds"
REJECT_ABANDONED = "abandoned"
REJECT_TOO_FEW_BLOCKS = "too_few_blocks"
REJECT_WINDOW_TOO_SHORT = "window_too_short"
REJECT_NO_INTERIOR_PEAK = "no_interior_peak"

# Reduced plateau guard (was THERMAL_MIN_DECAY_F = 1.0)
THERMAL_HVAC_MIN_DECAY_F = 0.3
# Minimum ΔT for single-point k_active estimate (filters sensor noise / no-effect cycles)
THERMAL_HVAC_MIN_SIGNAL_F: float = 0.5

# Thermostat swing (deadband half-amplitude) detection constants
THERMAL_SWING_DEFAULT_F: float = 1.5
THERMAL_SWING_MIN_F: float = 0.1
THERMAL_SWING_MAX_F: float = 5.0
THERMAL_SWING_CONF_LOW: int = 1
THERMAL_SWING_CONF_MEDIUM: int = 3
THERMAL_SWING_CONF_HIGH: int = 10

# Passive decay observation thresholds
THERMAL_PASSIVE_MIN_SAMPLES = 30
THERMAL_PASSIVE_MIN_DELTA_F = 3.0
THERMAL_PASSIVE_MIN_SIGNAL_F = 0.5

# Block-averaged OLS estimator for k_passive (dual-estimator framework, Issue #146)
THERMAL_BLOCK_OLS_BLOCK_MINUTES = 60  # width of each averaging block (minutes)
THERMAL_BLOCK_OLS_MIN_BLOCKS = 6  # minimum blocks required for OLS (≥6 → ≥6h window)
THERMAL_DUAL_AGREE_REL = 0.30  # max relative disagreement for endpoint+block to "agree"
THERMAL_DUAL_OLS_GOOD = 0.50  # block-OLS R² threshold for "good" quality
THERMAL_DUAL_OLS_OK = 0.20  # block-OLS R² threshold for "ok" quality

# Chart_log endpoint estimator thresholds (replaces passive_decay consecutive-pair OLS)
# Min window duration and temperature drop for passive-only and overnight ventilated windows.
THERMAL_CHART_LOG_PASSIVE_MIN_MINUTES: int = 120  # 2h minimum window
THERMAL_CHART_LOG_PASSIVE_MIN_DT_F: float = 1.0  # at least 1°F sensor change
THERMAL_CHART_LOG_VENT_MIN_MINUTES: int = 120  # 2h minimum for overnight ventilated windows

# Fan-only decay observation thresholds
THERMAL_FAN_MIN_SAMPLES = 15
THERMAL_FAN_MIN_SIGNAL_F = 0.2

# Ventilated decay observation thresholds
THERMAL_VENT_MIN_SAMPLES = 20
THERMAL_VENT_MIN_SIGNAL_F = 0.3
# Lower trigger delta for ventilated_decay: k_vent_window is measurable at 1°F differential.
# passive_decay needs 3°F for sufficient envelope-decay SNR; vent obs measures a different
# phenomenon (air exchange rate) where smaller differentials still carry useful signal.
THERMAL_VENTILATED_MIN_DELTA_F: float = 1.0

# Solar gain observation thresholds
THERMAL_SOLAR_MIN_SAMPLES = 20
THERMAL_SOLAR_MIN_RATE_F_PER_HR = 0.5
THERMAL_SOLAR_DAYTIME_START_H = 8
THERMAL_SOLAR_DAYTIME_END_H = 18

# Solar phase offset (learning — Issue #147)
THERMAL_SOLAR_PHASE_OFFSET_H_DEFAULT = 2  # Prior before learning (peak at 3pm)
THERMAL_SOLAR_PHASE_OFFSET_MIN = 0  # Clamp lower bound
THERMAL_SOLAR_PHASE_OFFSET_MAX = 4  # Clamp upper bound (5pm max: offset=4 → peak at local hour 17)
THERMAL_SOLAR_PHASE_MIN_ENTRIES = 3  # Min chart_log entries in window
THERMAL_SOLAR_PHASE_MIN_WINDOW_H = 4  # Min window span (hours)
THERMAL_SOLAR_PHASE_MIN_DT_F = 1.5  # Min indoor ΔT for visible peak
THERMAL_SOLAR_PHASE_ALPHA = 0.10  # EWMA alpha (slow — stable building physics)
THERMAL_PARAM_STALE_DAYS = 90  # days — parameter older than this treated as None at resolver

# AC duty-cycle secondary solar phase estimator (Issue #312)
THERMAL_SOLAR_PHASE_AC_ALPHA = 0.07  # EWMA alpha (slower — less reliable signal)
THERMAL_SOLAR_PHASE_AC_MIN_OBS = 3  # Min observations before secondary is trusted
THERMAL_SOLAR_PHASE_AC_SETPOINT_MIN_F = 68.0  # Setpoint range lower bound
THERMAL_SOLAR_PHASE_AC_SETPOINT_MAX_F = 80.0  # Setpoint range upper bound
THERMAL_SOLAR_PHASE_AC_SETPOINT_STABILITY_F = 1.5  # Max allowed setpoint spread (°F)
THERMAL_SOLAR_PHASE_AC_MIN_COOL_ENTRIES = 4  # Min cool entries in 11:00-16:00 window
THERMAL_SOLAR_PHASE_AC_PEAK_WINDOW_START_H = 11  # Peak window start (inclusive)
THERMAL_SOLAR_PHASE_AC_PEAK_WINDOW_END_H = 16  # Peak window end (exclusive)
THERMAL_SOLAR_PHASE_AC_STABILITY_WINDOW_END_H = 18  # Setpoint stability check end (exclusive)
REJECT_AC_NO_COOL_SETPOINTS = "ac_no_cool_setpoints"
REJECT_AC_SETPOINT_UNSTABLE = "ac_setpoint_unstable"
REJECT_AC_SETPOINT_OUT_OF_RANGE = "ac_setpoint_out_of_range"
REJECT_AC_INSUFFICIENT_MIDDAY_ACTIVITY = "ac_insufficient_midday_activity"
REJECT_AC_NO_SETPOINT_BREACH = "ac_no_setpoint_breach"

# Shared cap across all observation types
THERMAL_MAX_OBS_SAMPLES = 200

# v3 sampling redesign (Issue #122)
# THERMAL_DECAY_MAX_WINDOW_MINUTES is deprecated — subsumed by THERMAL_ROLLING_MAX_WINDOW_MINUTES (Issue #126).
# Kept here for backward compatibility; do not use in new code.
THERMAL_DECAY_MAX_WINDOW_MINUTES: int = 60  # wall-clock limit before vent/fan obs abandon
# Renamed from THERMAL_ROLLING_WINDOW_MINUTES — minimum window before first commit attempt.
THERMAL_ROLLING_MIN_WINDOW_MINUTES: int = 30
THERMAL_ROLLING_MAX_WINDOW_MINUTES: int = 240  # 4h hard cap; subsumes THERMAL_DECAY_MAX_WINDOW_MINUTES
THERMAL_ROLLING_WINDOW_MINUTES: int = THERMAL_ROLLING_MIN_WINDOW_MINUTES  # backward-compat alias
THERMAL_ROLLING_MIN_DELTA_T_F: float = 0.2  # min total indoor ΔT to commit a short window
# THERMAL_MIN_DECAY_SAMPLES is the single source of truth for OLS sample-pair floors.
# coordinator.py pre-gates on (THERMAL_MIN_DECAY_SAMPLES + 1) to guarantee at least
# THERMAL_MIN_DECAY_SAMPLES pairs are available for OLS.  Do not change either constant
# independently — the +1 offset is intentional and must be preserved.
THERMAL_MIN_DECAY_SAMPLES: int = 4  # min OLS pairs for rolling-window decay types (vs HVAC's 10)
THERMAL_SOLAR_FACTOR_MIN_RANGE: float = 0.30  # min solar_factor variance across samples for 2-param OLS
THERMAL_K_SOLAR_MAX_F_PER_HR: float = 8.0  # upper bound for k_solar (°F/hr); physical max ~6°F/hr on clear day
THERMAL_PASSIVE_SAMPLE_INTERVAL_S: int = 300  # 5 min — passive/vent slow decay
THERMAL_FAN_SAMPLE_INTERVAL_S: int = 120  # 2 min — fan-only (faster signal)
THERMAL_SOLAR_SAMPLE_INTERVAL_S: int = 300  # 5 min — solar gain slow trend
THERMAL_HVAC_POST_HEAT_SAMPLE_INTERVAL_S: int = 300  # 5 min — post-heat is passive dynamics

# Per-type passive confidence count thresholds
THERMAL_PASSIVE_CONF_LOW = 5
THERMAL_PASSIVE_CONF_MEDIUM = 15
THERMAL_PASSIVE_CONF_HIGH = 30

# Sleep temperature config keys (Issue #101)
CONF_SLEEP_HEAT = "sleep_heat"
CONF_SLEEP_COOL = "sleep_cool"
DEFAULT_SLEEP_HEAT = 66.0  # comfort_heat(70) - DEFAULT_SETBACK_DEPTH_F(4)
DEFAULT_SLEEP_COOL = 78.0  # comfort_cool(75) + DEFAULT_SETBACK_DEPTH_COOL_F(3)
MAX_SETBACK_DEPTH_F = 8.0  # never set back more than this
SETBACK_RECOVERY_BUFFER_MINUTES = 30  # pre-heat leads wake_time by this much

# ---------------------------------------------------------------------------
# Overnight Pre-Cool Phase (Issue #258)
# On warming-trend nights, CA applies a cooler ceiling mid-night to bank thermal mass.
# ---------------------------------------------------------------------------
PRE_COOL_POST_NAT_VENT_DELAY_MINUTES: int = 30  # delay after nat-vent window closes before AC pre-cool fires
PRE_COOL_WAKE_OFFSET_HOURS: float = 4.0  # fallback trigger: this many hours before wake_time
PRE_COOL_MIN_HEADROOM_F: float = 2.0  # pre-cool target floor = comfort_heat + this (prevents morning heat firing)
THERMAL_OBS_CAP = 200  # max observations in LearningState

# ---------------------------------------------------------------------------
# ODE Ceiling Guard (Issue #136)
# ---------------------------------------------------------------------------
CEILING_PRECOOL_FALLBACK_MIN: int = 120  # fallback lead time when k_active_cool not learned
CEILING_BRIDGE_TOLERANCE_F: float = 1.0  # bridge homes: require breach > comfort_cool + this
# Issue #247: in aggressive_savings mode, tolerate this much overshoot above comfort_cool before
# the ceiling guard escalates nat-vent -> AC (savings homes accept a small overshoot before paying
# for cooling; normal mode escalates at comfort_cool).
CEILING_ESCALATION_SAVINGS_MARGIN_F: float = 2.0

# Issue #249 — thermostat capability detection. Home Assistant's
# ClimateEntityFeature.TARGET_TEMPERATURE_RANGE bit: when set in a climate entity's
# `supported_features`, the thermostat accepts target_temp_low/target_temp_high (dual-setpoint /
# heat_cool band). Defined locally as a stable HA flag value so automation.py need not import
# homeassistant.components.climate (which breaks the lightweight stub test environment).
CLIMATE_FEATURE_TARGET_TEMP_RANGE: int = 2

ATTR_THERMAL_HEATING_RATE = "thermal_heating_rate"
ATTR_THERMAL_COOLING_RATE = "thermal_cooling_rate"
ATTR_THERMAL_CONFIDENCE = "thermal_confidence"

# ---------------------------------------------------------------------------
# Weather Forecast Offset Learning (Issue #61)
# ---------------------------------------------------------------------------
MIN_WEATHER_BIAS_OBSERVATIONS = 7  # need a full week before applying bias
WEATHER_BIAS_MAX_OBS = 30  # use last 30 days of forecast comparisons
MIN_WEATHER_BIAS_APPLY_F = 0.5  # don't apply bias smaller than 0.5°F
MAX_WEATHER_BIAS_APPLY_F = 8.0  # cap correction at 8°F (sanity limit)
ATTR_FORECAST_HIGH_BIAS = "forecast_high_bias"
ATTR_FORECAST_LOW_BIAS = "forecast_low_bias"
ATTR_FORECAST_BIAS_CONFIDENCE = "forecast_bias_confidence"

# ---------------------------------------------------------------------------
# AI / Claude API Integration (Issue #68)
# ---------------------------------------------------------------------------

# Config keys
CONF_AI_ENABLED = "ai_enabled"
CONF_AI_API_KEY = "ai_api_key"
CONF_AI_MODEL = "ai_model"
CONF_AI_REASONING_EFFORT = "ai_reasoning_effort"
CONF_AI_MAX_TOKENS = "ai_max_tokens"
CONF_AI_TEMPERATURE = "ai_temperature"
CONF_AI_MONTHLY_BUDGET = "ai_monthly_budget"
CONF_AI_AUTO_REQUESTS_PER_DAY = "ai_auto_requests_per_day"
CONF_AI_MANUAL_REQUESTS_PER_DAY = "ai_manual_requests_per_day"
CONF_AI_INVESTIGATOR_ENABLED = "ai_investigator_enabled"
CONF_AI_INVESTIGATOR_MODEL = "ai_investigator_model"
CONF_AI_INVESTIGATOR_REASONING = "ai_investigator_reasoning_effort"
CONF_AI_INVESTIGATOR_MAX_TOKENS = "ai_investigator_max_tokens"
CONF_AI_INVESTIGATOR_RPD = "ai_investigator_requests_per_day"

# Defaults
DEFAULT_AI_ENABLED = False
DEFAULT_AI_MODEL = "claude-sonnet-4-6"
DEFAULT_AI_REASONING_EFFORT = "medium"
DEFAULT_AI_MAX_TOKENS = 4096
DEFAULT_AI_TEMPERATURE = 0.3
DEFAULT_AI_MONTHLY_BUDGET = 0  # 0 = no cap
DEFAULT_AI_AUTO_REQUESTS_PER_DAY = 5
DEFAULT_AI_MANUAL_REQUESTS_PER_DAY = 20
DEFAULT_AI_INVESTIGATOR_ENABLED = False
DEFAULT_AI_INVESTIGATOR_MODEL = "claude-sonnet-4-6"
DEFAULT_AI_INVESTIGATOR_REASONING = "medium"
DEFAULT_AI_INVESTIGATOR_MAX_TOKENS = 8192  # must exceed MEDIUM reasoning budget (4096) + output buffer
DEFAULT_AI_INVESTIGATOR_RPD = 3

# Model options
AI_MODEL_SONNET = "claude-sonnet-4-6"
AI_MODEL_OPUS = "claude-opus-4-6"
AI_MODEL_HAIKU = "claude-haiku-4-5-20251001"
AI_MODELS = [AI_MODEL_SONNET, AI_MODEL_OPUS, AI_MODEL_HAIKU]

# Reasoning effort options and budget_tokens mapping
AI_REASONING_LOW = "low"
AI_REASONING_MEDIUM = "medium"
AI_REASONING_HIGH = "high"
AI_REASONING_OPTIONS = [AI_REASONING_LOW, AI_REASONING_MEDIUM, AI_REASONING_HIGH]
AI_REASONING_BUDGET_TOKENS = {
    AI_REASONING_LOW: 1024,
    AI_REASONING_MEDIUM: 4096,
    AI_REASONING_HIGH: 16384,
}

# Circuit breaker
AI_CIRCUIT_BREAKER_THRESHOLD = 5  # consecutive failures before tripping
AI_CIRCUIT_BREAKER_COOLDOWN_SECONDS = 300  # 5 min cooldown

# Retry
AI_MAX_RETRIES = 3
AI_RETRY_BASE_DELAY_SECONDS = 1.0  # exponential backoff: 1s, 2s, 4s

# Request history cap (metadata-only deque)
AI_REQUEST_HISTORY_CAP = 50

# Persisted report history
AI_REPORT_HISTORY_CAP = 60
AI_REPORTS_FILE = "climate_advisor_ai_reports.json"

# Investigation report history (Issue #82)
INVESTIGATION_REPORT_HISTORY_CAP = 60
INVESTIGATION_REPORTS_FILE = "climate_advisor_investigation_reports.json"

# Sensor attributes for AI status
ATTR_AI_STATUS = "ai_status"

# API paths for AI endpoints
API_AI_STATUS = f"{API_BASE}/ai_status"
API_AI_ACTIVITY = f"{API_BASE}/ai_activity"
API_AI_REPORTS = f"{API_BASE}/ai_reports"
API_AI_INVESTIGATE = f"{API_BASE}/ai_investigate"
API_INVESTIGATION_REPORTS = f"{API_BASE}/investigation_reports"
API_DELETE_REPORT = f"{API_BASE}/delete_report"
