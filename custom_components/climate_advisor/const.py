"""Constants for Climate Advisor."""

DOMAIN = "climate_advisor"

# Integration version — MUST match manifest.json "version" field.
# A test in tests/test_version_sync.py enforces this.
VERSION = "0.4.19"

RELEASE_NOTES: dict[str, list[str]] = {
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

# Day type thresholds (°F)
THRESHOLD_HOT = 85
THRESHOLD_WARM = 75
THRESHOLD_MILD = 60
THRESHOLD_COOL = 45
CLASSIFICATION_HYSTERESIS_F = 2  # °F dead zone to prevent threshold bouncing

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
DEFAULT_AI_INVESTIGATOR_REASONING = "high"
DEFAULT_AI_INVESTIGATOR_MAX_TOKENS = 20480  # must exceed HIGH reasoning budget (16384) + output buffer
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
