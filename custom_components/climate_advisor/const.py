"""Constants for Climate Advisor."""

DOMAIN = "climate_advisor"

# Integration version — MUST match manifest.json "version" field.
# A test in tests/test_version_sync.py enforces this.
VERSION = "0.6.56"

RELEASE_NOTES: dict[str, list[str]] = {
    "0.6.56": [
        "Fix #733: after an HA restart, an already-favorable whole-house-fan"
        " natural-ventilation session could be silently cancelled a moment after"
        " Climate Advisor turned it on, leaving the fan running with no"
        " thermostatic oversight until the next scheduled check the following"
        " morning — the startup fan reconciliation now defers to a just-issued"
        " fan command instead of overriding it, and any orphaned backstop timer"
        " is cleaned up so oversight can never silently lapse.",
    ],
    "0.6.55": [
        "Feat #731: no user-visible change. Continues the internal automation-engine"
        " refactor (fan/whole-house-fan control) with the same extract-and-shadow-"
        " validate pattern already applied to nat-vent, door/window, and override/"
        " grace — adds a shadow-diagnostic comparison axis so the fan/WHF FSM's"
        " agreement with production can be watched the same way the other three"
        " already are; see #594/#727/#729 for background.",
    ],
    "0.6.54": [
        "Feat #729: simplifies the Shadow Engine Primary switch added in 0.6.53 down"
        " to the single control you actually use — the 3 separate nat-vent/"
        " door-window/override-grace FSM toggles are gone, replaced by one choice"
        " (legacy engine or FSM engine). Promoting now reloads the integration"
        " instead of swapping live, which closes a real gap where an in-progress"
        " timer (a grace period, a pending setpoint retry) could keep running"
        " against the wrong engine after a switch. Logs now record which engine"
        " issued each command, so it's provable after the fact.",
    ],
    "0.6.53": [
        "Feat #727: the 3 nat-vent/door-window/override-grace FSM-authoritative"
        " switches now hold whatever state you last set them to across a Home"
        " Assistant restart, instead of always reverting to off. Also adds a new"
        " switch, Shadow Engine Primary, that lets you promote the diagnostic"
        " shadow engine to be the one actually operating your thermostat/fan —"
        " previously it could only compare its decisions against production, never"
        " act on them. Also persisted across restart, and instantly reversible.",
    ],
    "0.6.52": [
        "Fix #724: no user-visible change. Closes a gap in the internal diagnostic"
        " that shadows automation decisions to verify safety-logic correctness — its"
        " copy of the whole-house-fan suppression state was never kept in sync,"
        " which could make the diagnostic falsely report a disagreement during"
        " completely normal overnight whole-house-fan use with a window open.",
    ],
    "0.6.51": [
        "Fix #721/#722: no user-visible change. Closes the last two internal"
        " cross-checks left open by #717 — the door/window pause guard and the"
        " whole-house-fan/HVAC suppression tracker now both get the same audit"
        " trail as the rest of the safety logic. Also found and fixed two"
        " untracked fan-suppression release points that a prior investigation"
        " had missed.",
    ],
    "0.6.50": [
        "Fix #717: no user-visible change. Wires the internal cross-check that lets"
        " the natural-ventilation, door/window, and manual-override safety logic"
        " confirm they're seeing the same events, into production for real — closes"
        " a piece of scaffolding that existed but was never connected. Every"
        " decision still comes from the same logic as before; this only makes the"
        " audit trail behind it real.",
    ],
    "0.6.49": [
        "Fix #716: no user-visible change. The internal shadow-engine diagnostic that"
        " validates upcoming automation changes before they're allowed to affect real"
        " HVAC behavior wasn't tracking whether the whole-house/HVAC fan was on — so a"
        " related check could never meaningfully agree or disagree with production. It"
        " now does, closing a gap in the safety net that gates future automation"
        " changes; nothing about how the fan itself is controlled changed.",
    ],
    "0.6.48": [
        "Fix #714: the whole-house fan and an active thermostat mode (cool/heat) can"
        " no longer run at the same time. If you manually change the thermostat mode"
        " while free cooling is running, the fan now stops immediately instead of"
        " continuing to cycle in the background, and it won't silently turn your"
        " thermostat back off anymore if it happens to reactivate while your manual"
        " change is still in effect.",
    ],
    "0.6.47": [
        "Fix #711: closes a gap where an active whole-house-fan free-cooling"
        " session that was already running when you wake up wasn't re-checked"
        " against the daytime comfort band until whatever the next unrelated"
        " check happened to be — up to 5 minutes later. If indoor drifted"
        " below the graceful cycle-off point in that window, the fan could"
        " end up cycling off and back on again shortly after, instead of"
        " cycling off smoothly right at wake-up.",
    ],
    "0.6.46": [
        "Fix #707: no user-visible change. After certain restarts with an active"
        " whole-house-fan remote timer, a diagnostic comparison (not any real"
        " fan/HVAC decision) could report a false disagreement for several"
        " minutes. Purely a live-verification signal fix.",
        "Fix #708: closes a gap where, if you opt into the nat-vent state-machine"
        " engine, one specific moment — deciding whether to resume free cooling"
        " right after a grace period ends — was still always decided by the old"
        " code regardless of that setting. No change unless you've opted in.",
        "Fix #706: closes a gap where, if you opt into the nat-vent state-machine"
        " engine, it could lose track of an active manual override in two ways:"
        " not recognizing one was already in effect, and — in rare timing"
        " cases — briefly overwriting a fan override that started while a"
        " decision was in flight. Also teaches it the existing rule that free"
        " cooling should keep running during a protected period if the house is"
        " genuinely overheating. No change unless you've opted in.",
        "Fix #709: closes a gap where, if you opt into the door/window"
        " state-machine engine, two of its eight decision points didn't"
        " actually change your grace-period status the way the setting implied,"
        " and a rare zero-length-grace configuration could leave a phantom"
        " grace period reported that never cleared on its own. No change unless"
        " you've opted in.",
    ],
    "0.6.45": [
        "Fix #684: no user-visible change. A diagnostic-only comparison that"
        " checks whether the nat-vent state-machine engine (still not"
        " authoritative over any real decision unless you've opted in) agrees"
        " with production used a fixed 5-minute reactivation cooldown instead"
        " of your actually configured value, when they differ. Only affects"
        " installs that changed the reactivation lockout from its default —"
        " no change to any real fan/HVAC decision either way.",
    ],
    "0.6.44": [
        "Feat #698: the whole-house fan can now briefly pause itself mid-session"
        " once the room hits your comfort target, then resume automatically if"
        " it drifts back — instead of running the whole time regardless. With"
        " the state-machine switch enabled, a running free-cooling session also"
        " now reacts immediately (instead of waiting up to 30 minutes) if"
        " conditions change enough to end it for any reason, not just if the"
        " house gets too cold. Also fixed a small pre-existing mismatch where"
        " the fan could stay on slightly too long after outdoor air warmed past"
        " indoor, by reusing the same shared check used elsewhere.",
    ],
    "0.6.43": [
        "Fix #694: fixed 3 defects introduced by the previous nat-vent"
        " state-machine wiring pass (still not authoritative over any real"
        " decision by default). With the state-machine switch enabled, an"
        " in-flight natural-ventilation session (free cooling already"
        " running) could be killed outright or silently downgraded from a"
        " stronger cooling mode to a weaker one whenever a second door or"
        " window was opened during that session — even though nothing about"
        " outdoor/indoor conditions had changed. Also fixed a case where"
        " reopening a window during an existing door/window pause could"
        " leave the automation's internal pause bookkeeping in an"
        " inconsistent state. No change for installs that haven't opted"
        " into the state-machine switch.",
    ],
    "0.6.42": [
        "Fix #690: two separate places that decide when to end a natural-"
        "ventilation session (a fast check and a slower 30-minute check) used"
        " to disagree by one degree of temperature precision at the exact"
        " moment outdoor and indoor temperatures matched — the fast check"
        " would end the session, the slow one wouldn't, for up to 30 minutes."
        " Both now agree and end the session at the same instant once free"
        " cooling is genuinely gone. Rare edge case; no change for the"
        " common case where temperatures aren't at exact equality.",
    ],
    "0.6.41": [
        "Fix #691: no user-visible change. Adds a new internal method that will"
        " let the nat-vent state-machine engine (still not authoritative over"
        " any real decision today) eventually drive real fan state the same"
        " proven way the door/window engine already does. Not yet connected to"
        " anything — preparation work only.",
    ],
    "0.6.40": [
        "Fix #687: no user-visible change. The nat-vent diagnostic engine (used"
        " to validate a future state-machine switchover, not authoritative over"
        " any real fan/HVAC decision today) couldn't see when a manual fan"
        " override or grace period was active, so it reported 'would activate'"
        " for the full duration of any manual override — the single largest"
        " diagnostic-disagreement bucket found this session. It now correctly"
        " recognizes both.",
    ],
    "0.6.39": [
        "Fix #685: no user-visible change. The shadow-diagnostic 'disagreement'"
        " warning (used to validate the new state-machine engines against the"
        " existing production logic before any future switchover) used to fire the"
        " instant a real multi-step transition briefly looked different between the"
        " two computations, even when both settled on the same answer within"
        " seconds. It now only logs once a disagreement has genuinely persisted for"
        " 60 seconds, so the diagnostic signal reflects real problems instead of"
        " momentary timing noise.",
    ],
    "0.6.38": [
        "Fix #680: no user-visible change. Closes a minor structural gap in the"
        " override/grace FSM dispatcher (Issue #664): the restart clean-slate reset"
        " directly assigned its 3 governed flags instead of routing through the"
        " single dispatch point every other real call site uses. Both paths already"
        " produced the same clean-slate result, so there was no behavioral bug —"
        " this closes the 'exactly one writer' gap before it's relied upon.",
    ],
    "0.6.37": [
        "Fix #679: no user-visible change. Closes another instance of the same"
        " shadow-diagnostic gap class as #676: the Issue #508 stuck-grace backstop"
        " correctly notified the override/grace diagnostic FSM when force-cancelling"
        " an orphaned grace, but never the door/window diagnostic FSM, which could"
        " show a stale 'disagreement' for up to 10 minutes after a real recovery."
        " Real HVAC/fan behavior was always correct throughout; only the diagnostic"
        " mirror could drift.",
    ],
    "0.6.36": [
        "Fix #677: after a restart that lands in the middle of an active QuietCool RF"
        " remote timer, Climate Advisor now reads the remote's own live state to"
        " recognize the timer is still running and re-arms the correct remaining"
        " time, instead of forgetting about it. Previously, when the physical timer"
        " later shut the fan off naturally, CA misread it as a fresh manual power-off"
        " and started a fresh 3-hour lockout — blocking free cooling for hours even"
        " with ideal outdoor air.",
    ],
    "0.6.35": [
        "Fix #676: no user-visible change. Closes a second, separate shadow-diagnostic"
        " gap found immediately after #672/#673 shipped: when a grace period expired"
        " with a door/window sensor still open and free-cooling conditions happened to"
        " be favorable, natural ventilation correctly resumed and the pause was"
        " correctly cleared, but the shadow diagnostic engine was never told about it"
        " and could show a stuck false 'disagreement' for 20+ minutes. Real HVAC/fan"
        " behavior was always correct throughout; only the diagnostic mirror could"
        " drift.",
    ],
    "0.6.34": [
        "Fix #673: no user-visible change. Closes a structural gap in the shadow-"
        " diagnostic safety net related to #672 — four nat-vent/door-window fields"
        " (whether natural ventilation is active, soft-start state, door-pause"
        " state, and the outdoor-rise exit timer) were never included in the"
        " periodic raw-copy step that keeps the shadow diagnostic engine in sync,"
        " so a single missed update anywhere in the code could cause a permanent"
        " false 'disagreement' reading with no way to self-correct. Real HVAC/fan"
        " behavior was always correct throughout; only the diagnostic mirror could"
        " drift.",
    ],
    "0.6.33": [
        "Fix #672: no user-visible change. Three shadow-diagnostic state machines"
        " (door/window, nat-vent, override/grace) each had their own reason for"
        " getting permanently stuck out of sync with real production state after a"
        " restart or a specific state transition — real HVAC/fan behavior was"
        " always correct throughout. Fixed all three: door/window now notices a"
        " grace period that starts for an unrelated reason, nat-vent can recognize"
        " a door-pause condition even after wrongly staying active, and"
        " override/grace now tracks fan-off/window-close/nat-vent-exit/drift-"
        " correction grace periods it previously never modeled at all.",
    ],
    "0.6.32": [
        "Fix #670: right after an HA restart, if a door or window was already open, the"
        " whole-house fan could switch on before the startup-reconciliation logic had a"
        " chance to check the fan's actual state — occasionally causing a fan on/off"
        " flap in the minutes after restart. The regular-cycle nat-vent and window-"
        " cooling checks now wait for startup reconciliation to finish before acting,"
        " same fix already applied to a sibling check in #627.",
    ],
    "0.6.31": [
        "Fix #668: no user-visible change. The shadow-diagnostic door/window FSM was"
        " being wrongly reset every automation cycle whenever a door/window was left"
        " open with no imminent free-cooling opportunity (a diagnostic-only bug — real"
        " HVAC pause behavior was always correct). The periodic nat-vent re-check was"
        " unconditionally signalling 'nat-vent just reactivated while paused' on every"
        " call, regardless of whether that actually happened. Made the signal"
        " event-driven instead, so it only fires when nat-vent genuinely reactivates.",
    ],
    "0.6.30": [
        "Fix #666: no user-visible change. The coordinator test harness silently"
        " dropped the shadow-diagnostic FSM feed for every nat-vent/door-window exit"
        " event (a test-infrastructure bug, not a production one — real HVAC pause"
        " behavior was always correct). Fixed the harness wiring, closed a matching"
        " coverage gap where a specific nat-vent exit reason never emitted its"
        " Activity Report event at all, and added a regression test that reproduces"
        " the exact live disagreement pattern seen in production logs.",
    ],
    "0.6.29": [
        "Feat #664: the override/grace lifecycle FSM (whole-house-fan and thermostat"
        " manual overrides, and the grace period that protects them from being"
        " undone) can now optionally drive real production decisions instead of only"
        " observing them, matching the same opt-in switch nat-vent and door/window"
        " already have. Off by default and not persisted across a restart — nothing"
        " changes for any occupant unless this switch is explicitly turned on. Also"
        " fixes a config edge case found during this work: a manual grace period"
        " disabled via configuration (0 seconds) could have been reported as active"
        " with no way to ever clear it, had the switch been turned on before this fix.",
    ],
    "0.6.28": [
        "Fix #661: the override/grace shadow FSM's diagnostic accuracy for fan"
        " overrides (whole-house-fan remote timers, physical fan-on detection)"
        " is now correct — it previously modeled a confirmation delay that"
        " fan overrides never actually go through in production, causing a"
        " spurious disagreement reading on the most common override path. No"
        " occupant-visible change: override/grace has no authoritative switch"
        " and never drove real decisions — this only fixes what the diagnostic"
        " sensor reports.",
    ],
    "0.6.27": [
        "Fix #660: the door/window pause/grace lifecycle FSM now has full,"
        " off-by-default authority for all 8 real trigger sites — completing"
        " the migration begun in #637. Also fixes a real gap found during that"
        " work: when a grace period was already running and a door/window pause"
        " independently became active too, the FSM's own tracked state could"
        " disagree with what production actually did, and a resume-after-close"
        " could restore the wrong prior HVAC mode in a specific reachable"
        " sequence. Both are fixed at the source for every caller, not"
        " patched per call site. The switch that lets this FSM actually drive"
        " decisions (instead of just tracking them for comparison) stays off"
        " by default — no occupant-visible behavior change from this release"
        " alone.",
    ],
    "0.6.26": [
        "Fix #655: a door/window briefly reopened during an active grace period"
        " could still pause the AC/heat, even though the grace period exists"
        " specifically to avoid reacting to exactly that. The grace check now uses"
        " the same accurate indoor+outdoor reactivation check the automation"
        " already computes a moment later, instead of a coarser outdoor-only"
        " shortcut that could disagree with it — grace now reliably holds for its"
        " full duration.",
        "Fix #657: after a grace period ends with a door/window still open but"
        " conditions now favor natural ventilation, some pause-related dashboard"
        " and Activity Report fields (which door/window, how long it's been"
        " paused) could keep showing stale information from an earlier pause."
        " These now clear correctly alongside the rest of the pause state.",
        "Fix (found during #637 Phase R Step 3 scoping, no user-facing symptom"
        " confirmed): a nat-vent-exit pause path wrote fewer pause-tracking"
        " fields than the equivalent door/window pause path, which could leave"
        " a dashboard field stale and — in one specific edge case — cause a"
        " later door-close to start an unwanted extra grace period. Both pause"
        " paths now share one definition of what a door/window pause writes.",
    ],
    "0.6.25": [
        "Feat #637 (Phase R Step 2, partial): begins letting the door/window"
        " pause/grace lifecycle FSM actually drive production decisions — a new,"
        " off-by-default switch lets it take over 2 of the lifecycle's 7 actions"
        " (a manual thermostat override detected during a pause, and resuming from"
        " a dashboard pause) instead of the older logic. Both were proven"
        " behavior-identical to the existing logic before this shipped, across the"
        " full scenario library plus dedicated tests. The switch defaults off — no"
        " occupant-visible behavior change unless it is explicitly turned on, and"
        " even then only for those 2 actions; everything else about door/window"
        " pause/grace handling is unchanged.",
    ],
    "0.6.24": [
        "Feat #637 (Phase R Step 1b): internal refactor only, no user-visible behavior"
        " change — closes the last coverage gap in the door/window pause/grace"
        " lifecycle's diagnostic-only shadow FSM (Block 5 series, epic #594). 3 of its"
        " 7 tracked event kinds (grace-timer expiry, dashboard resume, and a sensor-"
        " state reconcile check) were never fed to it, deferred as future work when the"
        " FSM was first built. All 7 are now fed. Purely observational — nothing it"
        " computes is ever acted on.",
    ],
    "0.6.23": [
        "Fix #637: after a grace period expires with a door/window still open, if"
        " natural ventilation now takes over cooling, the system was still privately"
        ' marking itself as "paused by door" — which could suppress the away/vacation'
        " energy setback later, and made the dashboard/API misreport the reason HVAC"
        " was off. Now clears correctly the moment nat-vent takes over, matching how"
        " every other nat-vent-activation path already behaves.",
    ],
    "0.6.22": [
        "Feat #633 (Phase R prep): begins the cutover work for the nat-vent lifecycle"
        " FSM — modeled the one remaining gap in its transition table (soft-start"
        " escalating to full free-cooling mid-session), and added an opt-in,"
        " off-by-default switch that lets the FSM's decision drive the real"
        " whole-house-fan/HVAC calls for nat-vent instead of the legacy inline"
        " computation. Proven behavior-identical to the legacy path across the full"
        " scenario library before this shipped. The switch defaults off and does not"
        " persist across a restart — no occupant-visible behavior change unless it is"
        " explicitly turned on.",
    ],
    "0.6.21": [
        "Fix #651: closed two more gaps in the internal diagnostic that shadows"
        " automation decisions to verify an in-progress refactor (#613/#633/#637/#639,"
        " most recently #643/#647). A manual override made directly at the thermostat"
        " now correctly registers with the diagnostic (it was invisible before); and a"
        " fan-only override cleared by the bedtime or morning-wakeup schedule now"
        " reflects immediately instead of a brief delayed self-correction. No"
        " occupant-visible behavior change — this only affects an internal diagnostic"
        " used to validate the automation-engine refactor before any of it goes live.",
    ],
    "0.6.20": [
        "Fix #649: follow-up to #641's whole-house-fan rapid-cycling protection. The"
        " 5-minute floor itself was already working correctly, but the Activity Report"
        " and HA logs made a blocked toggle look like it had actually happened, and"
        " repeated the same misleading row every time the system re-checked while still"
        " blocked. A blocked-then-later-applied fan toggle now shows as a single"
        " accurate 'deferred' entry followed by one real 'applied' entry once the floor"
        " clears, and is no longer mislabeled as an incident — it's the protection"
        " working as intended.",
    ],
    "0.6.19": [
        "Fix #647: the internal diagnostic that shadows automation decisions to verify"
        " an in-progress refactor (added in #613/#633/#637/#639, most recently touched"
        " by #643) was disagreeing with the real automation on nearly every cycle — a"
        " wiring gap left it permanently stuck once a real manual override, grace"
        " period, or certain nat-vent exits occurred, instead of resetting once each"
        " finished. No occupant-visible behavior change — this only affects an internal"
        " diagnostic used to validate the automation-engine refactor before any of it"
        " goes live.",
    ],
    "0.6.18": [
        "Fix #645: after a redeploy or restart, the dashboard could briefly show HVAC mode"
        " 'cool' next to 'windows open (as planned)' — a monitored window/door sensor blipping"
        " unavailable-then-on during startup reset its change timestamp, which made the"
        " automation's debounce check treat the window as still settling and skip the guard"
        " that normally refuses to command an active HVAC mode through an open window. The"
        " compressor never actually ran in the reported case (the target temperature was still"
        " above the indoor reading), but on a warmer morning this could have let real cooling"
        " run with windows open. The guard now always blocks arming an active mode while a"
        " monitored window is open, regardless of that startup timing race.",
    ],
    "0.6.17": [
        "Fix #643: an internal diagnostic that shadows automation decisions to verify"
        " an in-progress refactor wasn't seeing manual fan overrides (the most common"
        " kind of override), so it could not confirm its own consistency after one"
        " occurred. No occupant-visible behavior change — this only affects an"
        " internal diagnostic used to validate the automation-engine refactor before"
        " any of it goes live.",
    ],
    "0.6.16": [
        "Fix #641: the whole-house fan could rapidly cycle on and off (roughly once a"
        " minute) when a predicted-floor or ceiling-threshold exit fired while a window"
        " was still open — the very next check immediately turned it back on, repeating"
        " indefinitely. Two nat-vent exit conditions now correctly hold off reactivation"
        " for 5 minutes after exiting, matching how the outdoor-air-reversal exit already"
        " behaved. As a second layer of protection, CA will never toggle the fan faster"
        " than once every 5 minutes going forward, regardless of cause — any future"
        " situation that would have caused rapid cycling is now blocked outright and"
        " logged as an incident instead of hitting the fan.",
    ],
    "0.6.15": [
        "Feat #639: internal refactor only, no user-visible behavior change — Block 5"
        " Phase 3 (the final phase) builds the unified override/grace transition table"
        " (override_grace_fsm.py), completing the shadow-diagnostic comparison series"
        " started by 0.6.13's nat-vent FSM and 0.6.14's door/window FSM. Unlike those"
        " two, override and grace are modeled as two small composed states, not one flat"
        " enum — grace routinely runs with no override behind it (fan-off, window-close,"
        " dashboard-resume grace), so a single enum would misrepresent reachability. New"
        " golden scenario confirms Issue #282's second-override-during-grace supersession"
        " path, previously untested. Purely observational — nothing it computes is ever"
        " acted on.",
    ],
    "0.6.14": [
        "Feat #637: internal refactor only, no user-visible behavior change — Block 5"
        " Phase 2 builds the unified door/window pause/grace transition table"
        " (door_window_fsm.py), the next diagnostic-only shadow comparison point after"
        " 0.6.13's nat-vent one. Confirmed (via new pending scenarios, not just static"
        " analysis) that production can genuinely be paused-by-door and in-grace at the"
        " same time — the new PAUSED_DURING_GRACE state models that combination rather"
        " than assuming it can't happen. Purely observational — nothing it computes is"
        " ever acted on.",
    ],
    "0.6.13": [
        "Feat #633: internal refactor only, no user-visible behavior change — the"
        " diagnostic-only decision table added in 0.6.12 now actually runs"
        " alongside production on every natural-ventilation check, compared"
        " against what production really did. Still purely observational —"
        " nothing it computes is ever acted on.",
    ],
    "0.6.12": [
        "Feat #633: internal refactor only, no user-visible behavior change —"
        " assembles the natural-ventilation logic into one explicit,"
        " thoroughly-tested decision table and a small generic messaging"
        " mechanism for coordinating between the automation's different"
        " behaviors, laying the groundwork for the same treatment to extend to"
        " the rest of the automation logic over time. Not yet connected to"
        " anything the system does today.",
    ],
    "0.6.11": [
        "Fix #631: the diagnostic-only shadow engine (used to validate an in-progress"
        " automation-logic refactor, never touches real hardware) could disagree with"
        " production for hours at a stretch whenever a manual override or a fan"
        " RF-remote override was active, because it never learned that a grace period"
        " was in effect. It now stays in sync with production's override/grace state on"
        " every check, closing a gap that could make its disagreement warnings"
        " unreliable during exactly the periods they'd matter most.",
    ],
    "0.6.10": [
        "Fix #629: right after turning off the whole-house fan, the air conditioner could"
        " silently switch itself into Cool mode while a monitored window was still open —"
        " with no pause, no notification, and nothing in the logs even saying the mode had"
        " changed. A routine background check that keeps the thermostat's setpoint current"
        " was allowed to also change its mode, and nothing double-checked that a window"
        " wasn't open before it did. The AC now refuses to switch itself on while a"
        " monitored window is open, the same way it already refuses to fight the"
        " whole-house fan — and any time that check changes the mode, it's now spelled out"
        " in the log.",
    ],
    "0.6.9": [
        "Fix #627: after a restart during an active whole-house-fan session (e.g. one"
        " started via RF remote), Climate Advisor could silently turn the fan off within"
        " the first second and then switch the air conditioner into Cool mode roughly 30"
        " seconds later — running the AC and whole-house fan at the same time, which the"
        " automation is specifically designed to prevent. A periodic safety check meant to"
        " catch a truly stray fan was firing before the system had finished settling back"
        " in after the restart. It now waits for that settling window to close before"
        " acting, the same way every other restart-related check already does.",
    ],
    "0.6.8": [
        "Fix #625: the Status card's grace-period text (added in 0.6.6, #620) had grown"
        " into a long, duplicated sentence — for a whole-house-fan override it repeated"
        " what the Fan (WHF) card already said, in different words. It now shows a short"
        " cause (e.g. 'WHF override', 'thermostat override') plus how long the grace"
        " period was set for and when it ends — the same compact style the Fan (WHF)"
        " card already uses for its remote timer. It also now shows a cause at all when"
        " you manually change the thermostat directly (mode or temperature) — previously"
        " that case showed no cause, or occasionally an unrelated leftover from an"
        " earlier event.",
    ],
    "0.6.7": [
        "Fix #623: briefly opening a monitored door (e.g. walking outside) could trigger"
        " an instant 'HVAC paused' notification, bypassing the debounce window you"
        " configured to ignore momentary opens. A timing race in the previous release's"
        " fix (0.6.6, #620) let this happen; the debounce check is now immune to that"
        " race, so a quick in-and-out through a door is correctly ignored.",
    ],
    "0.6.6": [
        "Fix #620: if you turned the whole-house fan off manually while a window was open"
        " and the outdoor air was still favorable, the automation could turn it back on"
        " within seconds, undoing your action. Separately, once a fan session ended (for"
        " any reason) with a window still open, the AC or heat could get set active with"
        " that window open — even if the window had been open for a while and nothing"
        " had ever noticed. All three now correctly pause instead. Also: the Status card"
        " now shows how much longer an active grace period will last and why it started,"
        " information that was previously only visible on the Debug tab.",
    ],
    "0.6.5": [
        "Fix #618: on a hot or cold day, if a whole-house-fan/natural-ventilation session"
        " ended while a window was still open, HVAC could stay silently un-managed for"
        " hours after the window closed — classification wanted the AC or heat on, but"
        " the mode never got applied and nothing indicated a problem. A related bug"
        " could also cancel AC that had just started cooling, moments after it began,"
        " if the thermostat reported a normal post-cycle fan phase. Both are fixed."
        " Also: a specific corrective HVAC-mode restore now shows up in the Activity"
        " Record instead of being invisible.",
    ],
    "0.6.4": [
        "Fix #615: internal fix only, no user-visible behavior change — the diagnostic"
        " shadow engine added in 0.6.3 was missing several real-world inputs (outdoor"
        " temperature, forecast, and 8 of 13 decision triggers), so it could never"
        " correctly agree with the real engine even when both were doing the right"
        " thing. Fixed with full coverage plus an automated check that keeps future"
        " changes from silently reintroducing the gap. The real engine's behavior is"
        " completely unchanged; only the diagnostic sensor's accuracy is affected.",
    ],
    "0.6.3": [
        "Feat #613: internal refactor only, no user-visible behavior change — a second,"
        " permanently inert copy of the automation engine now runs live alongside the"
        " real one, fed the same nat-vent sensor/classification inputs, and can never"
        " issue a real command. A new diagnostic sensor shows whether it agrees with the"
        " real engine's conclusions. This is groundwork for a future safe-rollout"
        " mechanism and does not change today's HVAC behavior.",
    ],
    "0.6.2": [
        "Feat #611: internal refactor only, no user-visible behavior change —"
        " added an offline test harness that proves a second, inert 'shadow'"
        " copy of the automation engine can run alongside the real one without"
        " ever issuing a real command or changing what the real engine does."
        " This is groundwork for a future safe-rollout mechanism (test new"
        " automation logic silently before it's ever allowed to control the"
        " thermostat) and does not change today's behavior.",
    ],
    "0.6.1": [
        "Feat #608: internal refactor only, no user-visible behavior change — the"
        " natural-ventilation exit logic (why a free-cooling session ends: comfort"
        " reached, ceiling reached, prediction, outdoor warming) is now a single,"
        " tested, verified-behavior-preserving decision instead of inline logic,"
        " continuing the groundwork from the previous release. Along the way, this"
        " also surfaced (documented, not yet consolidated) that natural"
        " ventilation currently evaluates some of these same exit conditions in"
        " up to three separate places — a known duplication pattern in this area"
        " that occasionally causes drift between them; consolidating them is"
        " flagged as follow-up work.",
    ],
    "0.6.0": [
        "Feat #606: internal refactor only, no user-visible behavior change — the"
        " natural-ventilation on/off/purge-mode logic now has a single, named,"
        " automatically-verified description of its own state (checked against"
        " every regression-test scenario), laying groundwork for safer future"
        " automation-logic changes in this area.",
    ],
    "0.5.67": [
        "Feat #604: internal refactor only, no user-visible behavior change — makes it"
        " safe to eventually build a second, non-acting engine instance for testing"
        " automation changes without risk to the live system, by giving it its own"
        " isolated set of callbacks instead of ones that could reach into the real"
        " thermostat.",
    ],
    "0.5.66": [
        "Fix #602: the daily learning record (which gates manual-override detection for"
        " setpoint-only changes, HVAC runtime tracking, comfort-violation minutes,"
        " occupancy-away minutes, door/window pause counts, and the thermal-learning"
        " watchdog) was only ever created once a day by the morning briefing — if the"
        " weather integration happened to be unavailable at that one fixed moment, all of"
        " that silently stopped working for the rest of the day, with no warning. It now"
        " also gets created by the regular classification cycle, which already retries"
        " weather forever — the gap shrinks from up to 24 hours to about 30 minutes."
        " Fix #598: a test scenario covering Issue #505's vacation-override-cleared fix"
        " was passing by coincidence rather than exercising the real behavior — this fix"
        " gives it real coverage.",
    ],
    "0.5.65": [
        "Fix #600: after an HA restart or grace-period expiry with the whole-house fan"
        " already running for natural ventilation, the Activity Record no longer shows"
        ' the same "Fan activated" adoption logged 2-3 times in the same minute — the'
        " fan itself only ever turned on once; only the redundant log/event entries are"
        " gone. Also fixes the displayed nat-vent session start time silently jumping"
        " forward on each redundant re-confirmation.",
    ],
    "0.5.64": [
        "Feat #593: closed out the remaining Activity Record payload-completeness gaps"
        " from the #584 investigation — classification decisions now show the trend"
        " magnitude and the exact threshold/margin that produced the day type; setpoint"
        " retry/nudge events show the reject streak count; startup coalescing shows"
        " indoor/outdoor temps and fan archetype; the thermal-learning watchdog shows"
        " today's session count; the fan-stopped and incident-detected cards now use"
        " data they already had (fan device, incident ID, comfort-band comparison)"
        " instead of a generic label; morning wake-up now reports an explicit skip"
        " reason when occupancy is away/vacation, matching its other skip reasons; and"
        " pre-cool deferring to an already-active nat-vent/WHF session now shows what"
        " indoor temp and target it's deferring to, instead of a bare notice. Four"
        " renderer functions with no current emitter are now explicitly marked as"
        " legacy/historical-log-only rather than looking like live, untested code.",
    ],
    "0.5.63": [
        "Feat #592: the Activity Record now explains *why* several nat-vent, door/window"
        " pause, and grace-recovery decisions happened, not just that they happened —"
        ' "Classification suppressed" and "Occupancy setback suppressed" rows now name'
        " which sensor is open and for how long; nat-vent fan-on/floor-skip/soft-start/"
        " ceiling-escalation rows show the actual outdoor/indoor temperatures and"
        " thresholds behind the decision instead of only a derived summary number;"
        ' "Override cleared" (fan-only) and "Override confirmed" rows show the reason/'
        " trigger; and a stuck-grace recovery row now names which mode/time was stale."
        " No automation behavior changed — same decisions, more visible reasoning.",
    ],
    "0.5.62": [
        "Fix #591: fixed the Activity Record showing the same automation decision "
        "(comfort band, classification, occupancy setback skip, nat-vent AC assist, and "
        "several others) two or three times in a row after a restart or overlapping "
        "trigger — each real decision now appears once.",
    ],
    "0.5.61": [
        "Fix #589: disabling automation (the 'Automation Enabled' switch / observe-only"
        " mode) now also stops the whole-house-fan command-only reconciliation path."
        " Previously, on installs where the fan entity only echoes commands"
        " (fan_state_feedback=False), this path kept issuing real fan on/off commands"
        " every ~30 minutes even with automation disabled — the only automated action"
        " that didn't respect the switch. It now honors dry_run like every other"
        " automated action.",
    ],
    "0.5.60": [
        "Feat #580: the dashboard's Activity Record report now defaults to the"
        ' "Last 12 hours" time window instead of 24, and lists events newest-first'
        " (most recent at the top, oldest at the bottom) instead of oldest-first —"
        " so the events you actually care about no longer require scrolling past a"
        " full day of history to find. The AI Investigative Analysis report type is"
        " unaffected and keeps its own separate time-window defaults.",
    ],
    "0.5.59": [
        "Fix #578: several AI Investigative Analysis report-quality fixes from user"
        ' feedback — the "Submit GitHub Issue" button now titles the issue'
        ' "AI Investigative Analysis - <date>" instead of grabbing the first sentence'
        ' of the report as the title; target_temp_low/high reading "unknown" while the'
        " HVAC is legitimately off (e.g. running whole-house-fan/nat-vent only) is now"
        " labeled as expected instead of flagged as a data-quality issue; the weather"
        " bias cap is now included in the report's context so the AI can actually check"
        ' against it; "Manual Overrides Today" now shows a separate fan override count'
        " alongside the setpoint override count so the two no longer look contradictory;"
        ' and "System Errors/Warnings" now reflects real captured WARNING/ERROR log'
        " records instead of a name-matching quirk that almost never caught anything."
        " The AI Activity Report feature (separate from AI Investigative Analysis) has"
        " been retired entirely — it was superseded by the deterministic, non-AI"
        " Activity Record and had not written new data since the #563 skill merge."
        ' The Investigative Analysis report\'s default time window is now "Last 1 day"'
        " instead of 7 days, and new installs now default to Sonnet 5 at low reasoning"
        " effort instead of an outdated model at medium effort.",
    ],
    "0.5.58": [
        'Feat #573 follow-up: replaced the menu-based "Save"/"Save and Reload" options'
        " added in 0.5.57 — Home Assistant's options-flow menu can't render an actual"
        " button (only a plain list row), so those looked identical to the settings"
        " sections instead of a real action. Each settings section now just has its"
        " normal Submit again; saving a section raises a repair notice (Settings ->"
        " System -> Repairs) telling you Climate Advisor has changes waiting, with a"
        " one-click Reload right from there.",
    ],
    "0.5.57": [
        "Feat #573: editing several AI/comfort/schedule settings sections in one visit"
        " to Configure used to reload Climate Advisor after every single section's"
        " Submit, rebuilding the coordinator and AI client each time. Each section now"
        " just saves; applying pending changes is done via a repair notice guiding you"
        " to reload.",
    ],
    "0.5.56": [
        "Fix #572: claude-sonnet-5's first request after being selected could silently"
        " hang for up to 90 seconds with no visible output at all before failing — a"
        " known model quirk that #565/#568/#569 tried to work around by learning it from"
        " a live failure and remembering that lesson, but a genuine Home Assistant"
        " restart could silently erase the lesson, so the failure kept coming back."
        " Climate Advisor now ships pre-verified, correct settings for every supported"
        " Claude model instead of learning them from a failure — so a supported model's"
        " very first request already works correctly, no failed attempt required.",
    ],
    "0.5.55": [
        "Fix #571: a legitimate whole-house-fan nat-vent exit was being misread as an"
        " externally-owned fan and force-corrected by an emergency reconcile — every"
        " single cycle, all morning. The Activity Report showed 'Fan running"
        " (untracked)' and 'fan found running without a CA-owned session' moments"
        " after Climate Advisor's own clean exit, instead of just the clean exit"
        " itself. Also fixed a related gap: the HVAC-fan dashboard status could get"
        " stuck showing 'active' even after the fan genuinely stopped, on"
        " HVAC-integrated-fan configurations.",
    ],
    "0.5.54": [
        "Fix #567: the whole-house fan's own automation-issued commands could get heard"
        " back on the QuietCool remote's RF channel and misread as a person pressing the"
        " physical remote — falsely handing fan control away from Climate Advisor for up"
        " to 3 hours and mislabeling the Activity Report as a manual action that never"
        " happened. Also fixed a related report-only issue: when Climate Advisor quietly"
        " corrects its own stale fan-tracking (no user involved at all), the Activity"
        " Report now says so instead of also claiming 'user turned off'.",
    ],
    "0.5.53": [
        "Fix #568: the AI model-compatibility learning added in #565 (so Climate Advisor"
        " adapts automatically to a newer Claude model's quirks after the first request)"
        " was being silently wiped every time AI settings were saved or Home Assistant"
        " restarted — so it could never actually stick. It's now saved the same way as"
        " other AI usage stats, so it survives both. Also added clearer AI request"
        " logging so any future model-compatibility issue can be diagnosed directly from"
        " the logs.",
    ],
    "0.5.52": [
        "Fix #565: the AI Investigator and AI Activity Report could silently burn their"
        " entire response budget with no visible answer at all on newer Claude models"
        " (confirmed with claude-sonnet-5) — the model was doing its own internal"
        " reasoning with no cap on it, and that reasoning alone could use up the whole"
        " response length before ever getting to write an actual answer. Climate Advisor"
        " now detects this and automatically applies a bounded-reasoning setting so the"
        " model always leaves room for a real answer; it also learns per-model going"
        " forward so this self-heals after the first occurrence instead of repeating on"
        " every request.",
    ],
    "0.5.51": [
        "Fix #563: the AI Investigator was sending nearly the entire history of every"
        " fixed issue to Claude on every single run — a version-scoping check that was"
        " supposed to limit this to only recently-relevant fixes had a bug that let all"
        " 169 fixed-issue records through every time, and a separate rendering bug was"
        " expanding some of that text by roughly 15x on top of that. Investigations should"
        " now run noticeably faster and cheaper, with no loss of the 'was this already"
        " fixed' cross-check the AI uses this data for.",
        "Fix #563: the scheduled 'Generate with AI' activity narration was running the"
        " same full audit-depth analysis as an on-demand investigation (including a live"
        " GitHub fetch) — it now uses a lighter, current-activity-only context, which"
        " should make it noticeably faster.",
        "Fix #563: the Investigate report's progress display now shows real step-by-step"
        " status from the backend and fills in the report as sections complete, instead"
        " of a fake elapsed-seconds counter and raw unformatted text.",
        "Fix #563: fixed a bug where the 'AI Activity Report' scheduled service call"
        " silently failed on every run after a recent internal rename.",
        "Fix #563: the AI model dropdown in settings now shows Anthropic's current"
        " available models automatically instead of a fixed list, and if a configured"
        " model is retired, Climate Advisor automatically switches to a comparable"
        " replacement instead of failing.",
        "Fix #563: fixed AI requests failing outright when a newer model no longer"
        " accepts a setting (e.g. temperature) that older models required — Climate"
        " Advisor now detects this and retries without it automatically.",
        "Fix #563: raised the maximum AI response length setting from 8192 to 16384"
        " tokens, and added a clearer warning when a response uses its full budget but"
        " produces no visible output (rather than the generic 'truncated' message, which"
        " incorrectly implied a bigger budget alone would fix it).",
    ],
    "0.5.50": [
        "Fix #561: the whole-house fan could turn itself on with every door and window"
        " closed, briefly switching the thermostat off for no reason — and the log"
        " misleadingly claimed 'whole-house fan manually turned on' even though nobody"
        " touched it. The fan-cycling logic now re-checks that a monitored sensor is"
        " actually open before ever turning the fan back on, instead of trusting an"
        " internal flag that could go stale for hours. Also fixed the underlying causes:"
        " a self-healing check that could keep a ventilation 'session' alive after"
        " windows closed, and a rare timing gap that could start two duplicate internal"
        " timers, both of which could leave the system briefly confused about whether"
        " it or the user caused a fan change.",
    ],
    "0.5.49": [
        "Fix #557: options dialog sections now save the instant you hit Submit — no more"
        " separate 'Save & Close' step. Previously, submitting a section (e.g. Setpoints or"
        " Notifications) only staged the change in memory; re-opening that same section"
        " before hitting the separate Save button showed the old value, making it look like"
        " the change hadn't taken. Every section now writes and reloads immediately, so"
        " what you see after Submit is always what's actually saved.",
    ],
    "0.5.48": [
        "Fix #558: the AC no longer chases a colder-than-comfort setpoint on hot days after"
        " you return from being away — it now simply restores your normal comfort setting."
        " The overnight pre-cool banking feature (which quietly cools the house before a hot"
        " day, overnight, while it's cheap) now also runs on stretches of consecutive hot"
        " days that aren't getting hotter each day, not just the first day of a heat wave."
        " The morning briefing no longer claims pre-cooling happened if it didn't.",
    ],
    "0.5.47": [
        "Fix #555: Daily Briefing sensor no longer drops to 'unknown' on days with a lot"
        " to say (away/vacation occupancy + dual window opportunities) — the TLDR summary"
        " is now shortened to reliably fit HA's 255-char sensor state limit, with a"
        " truncation safety net and full text still available in the sensor's attributes"
        " as a backstop.",
    ],
    "0.5.46": [
        "Fix #553: `tools/deploy.py` now transfers files by piping a tar stream through the"
        " same SSH connection that extracts/restarts/verifies (no separate `scp`), capping a"
        " full deploy at 3 connections total (was ~8 after #551's partial batching) and"
        " `--rollback` at 1. Also fixes two real bugs found during live validation against"
        " the production HA instance: a crash-safety gap where an interrupted connection"
        " mid-extraction could leave the live integration directory deleted or half-written"
        " (now extracts to a temp dir and swaps it into place as the final, near-instant"
        " step), and a backup/restore tar-format mismatch that produced a nested"
        " climate_advisor/climate_advisor/ directory on rollback. Developer/deployment"
        " tooling only — no change to the integration itself.",
    ],
    "0.5.45": [
        "Fix #551: reverted #549's SSH connection multiplexing in `tools/deploy.py` — it"
        " failed outright on Windows/Git-for-Windows SSH clients against a real HAOS SSH"
        " add-on. Replaced with command batching (fewer, combined SSH round trips) to reduce"
        " connection count and avoid tripping the add-on's rate-limit protection, without"
        " depending on client-side multiplexing support. Developer/deployment tooling only —"
        " no change to the integration itself.",
    ],
    "0.5.44": [
        "Fix #549: `tools/deploy.py` now multiplexes all of its SSH/SCP connections through"
        " one real connection per run (SSH `ControlMaster`), instead of opening 6-8 separate"
        " ones — avoids tripping the HA SSH add-on's rate-limit/brute-force protection."
        " Developer/deployment tooling only — no change to the integration itself.",
    ],
    "0.5.43": [
        "Fix #547: `tools/deploy.py` now prints which SSH identity file it will use before"
        " connecting, and the SSH setup guide documents a Windows-specific default-key-"
        " resolution gotcha. Developer/deployment tooling only — no change to the integration"
        " itself.",
    ],
    "0.5.42": [
        "Fix #545: strengthened project guidance and automated checks to prevent a repeat of"
        " #543-style bugs (blocking file I/O called directly from async code, stalling Home"
        " Assistant). No user-visible behavior change — this is contributor-facing tooling"
        " (docs, lint rule, and a regression test) only.",
    ],
    "0.5.41": [
        "Fix #543: Chart-log save/load no longer runs synchronously on Home Assistant's event"
        " loop — could cause brief startup/update stalls. The integration also now correctly"
        " reports itself as cloud-connected ('cloud_polling') instead of 'local_polling',"
        " matching its use of the Anthropic AI cloud API. Both were required by HACS's"
        " official default-repository review.",
    ],
    "0.5.40": [
        "Feat #540: new 'Nat-Vent Soft-Start (Purge Mode)' setting, on by default. The"
        " whole-house fan can now start moving air and purging attic/thermal-mass heat as soon"
        " as outdoor temperature reaches parity with indoor in the evening, once the day is"
        " confirmed past its peak — instead of waiting for outdoor to be measurably cooler."
        " Disable it in settings if you prefer the old strict-delta-only behavior. See the"
        " Status card for a distinct 'soft-start (purge)' label while it's active.",
    ],
    "0.5.39": [
        "Fix #538: the 'Next User Action' card said 'Free cooling is active.' while nat-vent"
        " or economizer cooling was already running — just repeating what the Status card"
        " already showed instead of telling you what to do. It now shows '-' when there's"
        " nothing for you to do.",
    ],
    "0.5.38": [
        "Fix #534: the Next Automation card's 'outdoor no longer helping' message could read"
        " as a claim about right now even though it was always a forecast for a specific"
        " future time — the action text now says when that's expected to happen (e.g."
        " 'Outdoor will stop helping around 9:00 AM — close windows') instead of only showing"
        " the time in a separate card. Also: mild-day briefings now use the same"
        " weather-forecast-based window close time warm days already got, instead of always"
        " showing a fixed 5:00 PM regardless of actual conditions.",
    ],
    "0.5.37": [
        "Fix #530: turning off the whole-house fan didn't reliably stick — a watchdog meant"
        " to catch a completely different, rare bug was mistaking the normal 'no override in"
        " progress' state of an ordinary fan-off for a stuck automation, and killing its"
        " protection within about a second almost every time. Fixed at the root, so fan-off"
        " now stays off for its full protection window like it's always been supposed to."
        " On top of that, an overnight session started via an 8-hour RF remote timer no"
        " longer produces a burst of contradictory decisions right when the timer runs out —"
        " a fan-off report in the couple of minutes after that timer's own grace period ends"
        " is now recognized as the tail of the same session instead of a brand-new event."
        " Separately, a leftover fan override being cleared at the 6:30 AM wake-up could arm"
        " the AC with windows still open — wake-up no longer releases whole-house-fan HVAC"
        " suppression while a nat-vent session is still active.",
    ],
    "0.5.36": [
        "Fix #528: on warm/mild days, the briefing's window-close and reopen times could"
        " be badly wrong — one real example told the user to close windows at 8 AM"
        " (outdoor was still cooler for hours after that) and reopen at 2 PM, before the"
        " day's actual heat peak, both computed from a data-alignment bug that's now fixed."
        " Feat #528: the Next Automation card can now predict the whole-house fan/nat-vent"
        " starting (using the same real activation logic the automation itself uses), the"
        " warm-day window-close/AC-on/reopen events, and hot-day morning/evening window-"
        " cooling opportunities — previously only the daily briefing knew about any of this.",
    ],
    "0.5.35": [
        "Fix #527: the dashboard's Status, Next User Action, and Next Automation cards could"
        " all say the same thing in different words whenever a door/window was open (or a"
        " grace period or thermostat-change confirmation was active) — the Next User Action"
        " card said 'Automation paused,' restating the Status card instead of telling you"
        " what to actually do (like closing the window), and the Next Automation card said"
        " 'Waiting' instead of showing the real next step (like tonight's bedtime setback)"
        " and when it'll happen. Each card now sticks to its own job, and away/vacation mode"
        " gets a bit of rotating personality in Next User Action instead of one flat line.",
    ],
    "0.5.34": [
        "Fix #523: after an HA restart, if a window was already open, Climate Advisor could"
        " turn the AC on and cool against the open window instead of staying paused like it"
        " does at every other point in the day — most visible after an update. Startup"
        " handling now defers to the same door/window pause logic used the rest of the time,"
        " and that pause logic itself now correctly stays engaged even when the thermostat was"
        " already off when the window opened.",
    ],
    "0.5.33": [
        "Fix #524: the dashboard's whole-house-fan status card never showed the QuietCool"
        " remote's reported speed, even though the underlying detection (#519) was working"
        " correctly — the value was computed but never reached the dashboard. It now shows"
        " promptly after any remote press. Also, the Activity Report's fan-override entries"
        " now note when a remote speed or timer selection armed the override, instead of"
        " looking identical to a generic detected toggle.",
    ],
    "0.5.32": [
        "Fix #518: the warm/windows-day briefing could contradict itself — the header's"
        " window-close time didn't match the body's, an AC-start message ignored whether"
        " windows were actually open (and contradicted a correct warning elsewhere in the"
        " same briefing), a 'reopen windows' message could cancel an AC run that never"
        " started, and a bedtime-setback note could appear even when the header said 'No"
        " setback'. All four are now derived from a single computation so the header and"
        " body always agree, and the AC-safety-net wording is stated once, tied to windows"
        " being closed. Also dropped redundant 'no action needed' phrasing from the"
        " briefing and dashboard status text.",
    ],
    "0.5.31": [
        "Feat #519: Climate Advisor now detects and respects QuietCool remote speed changes"
        " (low/medium/high), not just timer presses. If you adjust speed while the fan was"
        " already running, that's treated as a comfort preference — it's just recorded, not"
        " treated as taking manual control (no grace period or HVAC suppression armed). If"
        " you select a speed while the fan was off, or select a timer (with or without a"
        " speed), that's still treated as an override exactly like before. If your remote's"
        " firmware has been updated to the latest gunkl/quietcool-house-fan, the dashboard"
        " also shows the fan's current remote-reported speed. Fully auto-detected — no new"
        " setting to configure, and installs without the firmware update behave exactly as"
        " they do today.",
    ],
    "0.5.30": [
        "Fix #510: the dashboard WHF status card could show 'nat-vent active, fan idle' for"
        " hours while the whole-house fan was genuinely, physically running — confirmed via"
        " live logs on an install with dedicated fan power detection, where a stale nat-vent"
        " session flag masked ground truth that was available the whole time. The display now"
        " refreshes immediately on every real physical fan transition (previously only when a"
        " manual override was already active) and always trusts confirmed physical state over"
        " CA's own internal session flags when it's available. The related 'active"
        " (unconfirmed)' status — which could also persist indefinitely once stale (observed"
        " 138 times over 24+ hours in the same incident) — now correctly settles to 'inactive'"
        " once enough time has passed for ground truth to be trusted, rather than leading with"
        " 'active' forever. Also fixes two related automation-bookkeeping gaps found during the"
        " same investigation: a whole-house-fan install's post-grace-period check was silently"
        " skipped because it consulted the thermostat's own fan attributes instead of the real"
        " fan entity, and the existing periodic untracked-fan reconciliation now also covers a"
        " stale nat-vent flag, not just a fully-untracked fan, closing the loop within ~30"
        " minutes if it recurs.",
    ],
    "0.5.29": [
        "Fix #511: for installs with no dedicated outdoor sensor (weather-service source"
        " only, e.g. Met.no), the dashboard's 'Actual Outdoor' reading and the automation"
        " decisions based on it could lag or lead true conditions by up to an hour during"
        " a temperature ramp — the weather integration's live reading only refreshes"
        " roughly hourly, so it was really a stale point-sample, not a live value."
        " Outdoor temp is now estimated by interpolating between the two nearest hourly"
        " forecast points, refreshed every 5 minutes, feeding the dashboard, the"
        " windows-recommended flag, nat-vent/economizer gating, and thermal-learning"
        " model accuracy. Installs with a dedicated sensor or input_number are unaffected"
        " — they already had a true live reading.",
    ],
    "0.5.28": [
        "Fix #508: pressing 'Cancel Fan Override' on the dashboard cleared the fan override"
        " but left the grace-period countdown running for its full original duration — up to"
        " 8 hours for a QuietCool RF remote timer — so the dashboard kept saying 'Grace period"
        " active' long after you'd already cancelled it. The fan/HVAC state also had no"
        " guaranteed way to re-sync immediately; it only worked today because an unrelated"
        " door/window event happened to fire a minute later. Both dashboard 'Cancel...'"
        " buttons now share one cancellation path that clears grace, re-checks the fan, and"
        " logs the cancellation to the Activity Report every time. A background safety check"
        " also self-heals any grace period left with no override behind it.",
    ],
    "0.5.27": [
        "Fix #505: vacation mode's deep energy-saving setback was armed once when you"
        " turned vacation mode on, and never enforced again for the rest of the trip —"
        " confirmed against real logs from a 5-day vacation where the home ran at normal"
        " comfort temperature almost the entire time. A temporary override (e.g. for"
        " cleaners) that was later cancelled left the thermostat at the override's"
        " setpoint indefinitely instead of returning to the deep setback, the same way"
        " away mode already correctly does. Away mode itself, home mode, and guest mode"
        " were not affected. Also fixed the same gap for the bedtime and pre-cool"
        " triggers, which had the identical assumption.",
    ],
    "0.5.26": [
        "Fix #504: a monitored door/window sensor bouncing open/closed rapidly (flaky"
        " contact hardware, or a quick open-close-open) could snap the whole-house fan on"
        " and back off within the same minute — an audible burst with no settle time, even"
        " though a sensor debounce period was configured. The debounce now also governs"
        " when free-cooling fan control reacts to a sensor change, not just HVAC"
        " pause/resume, so a bounce no longer instantly re-triggers the fan. The default"
        " debounce is also now 10 minutes (was 5) for new installs. Also fixed: the"
        " Activity Report row for nat-vent ending because a sensor closed now shows the"
        " fan's on->off transition, matching every other fan-transition row.",
    ],
    "0.5.25": [
        'Fix #485: the Activity Report showed the same "Occupancy setback (away)" entry'
        " repeated every ~5 minutes for hours at a time while nobody was home, drowning out"
        " everything else in the log. The setpoint itself was never actually changing —"
        " Climate Advisor just wasn't collapsing the repeats the way it already does for"
        " other frequently-repeated entries. Now it shows one entry with a repeat count and"
        " time range, and still shows a new entry right away whenever something real"
        " changes (you come home, leave for vacation, etc.).",
    ],
    "0.5.24": [
        'Fix #498: the Status dashboard showed "Grace period active" during an override'
        " but never said when it would end — now shows the end time and minutes remaining."
        " Also fixed: the 6:30am wake-up could turn off a manually-overridden whole-house"
        " fan and arm the AC against open windows, self-correcting only by luck a cycle"
        " later. Bedtime, wake-up, and the overnight pre-cool trigger no longer each"
        " decide independently whether to touch the fan or arm HVAC — they now share one"
        " gate with the main 30-minute decision loop, closing two related gaps in the same"
        " pass: none of the three previously respected an open door/window pause, and"
        " bedtime's own free-cooling continuation check could hand off to the compressor"
        " prematurely even while the fan was still doing useful, cheaper work.",
    ],
    "0.5.23": [
        "Fix #495: manually or remotely turning on the whole-house fan (WHF) — by hand, or"
        " via a QuietCool RF remote timer press — left the AC armed for the entire session,"
        " fighting the fan and wasting energy while windows were open. Only Climate"
        " Advisor's own fan activation suppressed HVAC; a user-initiated fan-on did not."
        " Fixed: both paths now share one HVAC-suppression helper, and ending a manual"
        " session reclassifies (rather than blindly restoring a potentially hours-stale"
        " captured mode — an RF-remote-timer session can run up to 12 hours). Also fixes"
        " two QuietCool remote bugs found while investigating: (1) the remote's status"
        " entity can flap unavailable and re-announce a stale timer selection with no user"
        " action, which was previously processed as a fresh press — confirmed live as a"
        " phantom 2-hour override with zero button presses; (2) the dashboard's remote-timer"
        " display could go blank within seconds of a real press. And: the dashboard could"
        " show two contradicting status lines at once when a fan override and a pending"
        " thermostat-override confirmation overlapped — now reconciled.",
    ],
    "0.5.22": [
        "Fix #493: found while verifying #491's restart fix on a real HA restart —"
        " learning.save_state() could occasionally log 'Failed to save learning state:"
        " No such file or directory' when two saves happened to run at the same moment"
        " (common at restart). It wrote to a shared, fixed staging filename, so one save"
        " could find the file already consumed by another. Non-fatal (the error was"
        " already caught and logged; nothing was corrupted), but one save's data could be"
        " silently skipped for that cycle. Each save now stages to its own uniquely-named"
        " temp file, the same pattern already used by CA's other state file — eliminating"
        " the collision entirely.",
    ],
    "0.5.21": [
        "Fix #491: two restart-time bugs found immediately after the 0.5.20 deploy, both"
        " pre-existing and unrelated to #489. (1) The dashboard could show a false"
        " 'Fan manual override' and a bogus multi-hour manual grace period right after"
        " every HA restart — the whole-house fan never turned on and nobody touched the"
        " remote; the QuietCool RF remote's device entity can re-announce its last"
        " retained state while HA is still settling after restart, and neither fan"
        " listener had the same 5-minute startup-suppression guard the thermostat"
        " listener already had (Issue #321). Both fan listeners now share that guard."
        " (2) A 'Climate Advisor unavailable' error banner could appear after routine"
        " restarts/deploys with nothing actually wrong — a plumbing bug in the thermal"
        " observation pipeline (present since April) crashed the coordinator update"
        " whenever a pending thermal observation was abandoned right as HVAC started,"
        " which is common at restart. Fixed; no HVAC or automation timing behavior"
        " changed by either fix.",
    ],
    "0.5.20": [
        "Fix #489: the Doors/Windows status card could show a stale 'N open' reading for"
        " up to 30 minutes after a monitored door or window was actually closed again."
        " Brief real door use (a few seconds) was always detected correctly, but closing"
        " it back up didn't force the dashboard to refresh — only opening did. Now every"
        " sensor transition, open or closed, refreshes the status display immediately."
        " Automation timing is unaffected: the existing debounce still exclusively"
        " governs when HVAC actually pauses or resumes for a door/window event.",
    ],
    "0.5.19": [
        "Feat #486: Climate Advisor can now hear the QuietCool whole-house fan's physical RF"
        " wall remote (via the gunkl/quietcool-house-fan ESPHome firmware's event entity) and"
        " honor a timer selection made at the remote. Previously, pressing '8 hours' on the"
        " remote had no effect on CA's own automatic fan-off timing — CA would still shut the"
        " fan off on its usual ~30-90 minute grace period, contradicting what the person just"
        " told the fan to do. Now, when an optional Fan RF Remote Event Entity is configured,"
        " a 1/2/4/8/12-hour remote timer selection sets the duration of CA's fan manual-override"
        " grace period, so CA backs off for exactly as long as the user asked. Fully optional and"
        " non-breaking: leave the field blank and nothing changes. See docs/fan-remote-spec.md"
        " for the firmware event contract and mapping.",
    ],
    "0.5.18": [
        "Fix #434: optional entity settings can now actually be cleared. Previously, if you'd set"
        " a Home/Away toggle, Vacation toggle, Guest toggle, fan entity, fan-state entity, or a"
        " custom outdoor/indoor temperature-source entity and later wanted to stop using it,"
        " clearing the picker and hitting Save & Close did nothing — Climate Advisor kept reacting"
        " to the old entity even though the UI says 'leave blank if you don't use that feature'."
        " The options flow now removes a field you've emptied, so leaving it blank truly unsets it"
        " (occupancy falls back to Home; vacation/guest default to off).",
    ],
    "0.5.17": [
        "Fix #480: when Climate Advisor's coordinator update fails (the failure that took every"
        " climate_advisor_* entity unavailable simultaneously during the Issue #478 incident),"
        " the dashboard used to keep confidently showing the last-known automation/fan status with"
        " zero indication anything was wrong — you'd have no way to know CA had silently stopped"
        " working until you noticed the numbers looked stale. The Status card now shows"
        " ⚠ Climate Advisor unavailable since HH:MM — <error> the moment an update fails, and the"
        " underlying error/failure-count record is now written to disk, so it survives an HA"
        " restart and is still readable even after HA's own log retention has rotated past the"
        " event — the exact gap that made the original incident's root cause unrecoverable.",
        "Fix #481: fixes a false-positive comfort log entry that could make it look like the"
        " house was too cold overnight when it wasn't. The incident-detection subsystem that"
        " powers compliance/history review was comparing live indoor temperature against the"
        " flat daytime comfort band (e.g. 68°F) even during the overnight sleep window, where a"
        " lower sleep-band floor (e.g. 64°F) is the real, actively-applied target — so indoor"
        " temps that were genuinely comfortable within the sleep band (e.g. 66°F) could still"
        " log a 'comfort_undertemp' incident. Incident detection now resolves the same"
        " currently-active band (sleep/away/vacation-aware) that the dashboard's target-heat/cool"
        " fields and every setpoint-writing automation handler already use, so the incident log"
        " only reflects violations the occupant actually experienced.",
        "Fix #482: no user-visible change (latent-risk hardening). Closes two real gaps found"
        " during Issue #478's investigation in the fan-off manual-vs-automation classification"
        " path. (1) The fan physical-state drift-reconciliation self-correction"
        " (_reconcile_fan_physical_drift()'s off-command) now stamps the same"
        " _fan_command_pending/_fan_command_time bookkeeping every other WHF command site"
        " already sets, matching the existing pattern, so coordinator._async_fan_entity_changed()"
        " can suppress the resulting state-changed event as CA-caused instead of risking a"
        " misclassification as a manual fan-off (which would start a spurious grace period and"
        " temporarily block automated free cooling/HVAC control). (2) Every outgoing WHF"
        " fan/switch service call now carries a real HA Context"
        " (automation.py's new _call_fan_service_with_context()), and"
        " coordinator._async_fan_entity_changed() checks event.context.id/parent_id against it as"
        " an additional, authoritative CA-attribution signal alongside the existing"
        " _fan_command_pending/30-second timing heuristic (kept as-is, not replaced — context"
        " propagation through third-party fan/switch integrations, especially a one-way RF"
        " transmitter with no feedback of its own, is not guaranteed reliable by HA core). Every"
        " provenance decision (matched or not) is now logged at DEBUG so a future investigation"
        " has direct evidence instead of needing cross-source timestamp archaeology, and a"
        " genuinely external fan change's Context id is surfaced as diagnostic data in the"
        " Activity Report payload.",
        "Fix #483: if a manual thermostat override starts a grace period and Climate Advisor's"
        " own automation decision independently converges on the same HVAC mode (and, for"
        " heat/cool modes, the same effective setpoint) the override already produced, the"
        " override is now adopted instead of silently sitting out the rest of the grace window."
        " Checked both pre-expiry (inside apply_classification(), so convergence is recognized"
        " as soon as the next classification cycle agrees — not just at the timer's natural"
        " expiry) and at natural grace expiry (skips the misleading 'your override has expired'"
        " notification when nothing was actually reverted). Deliberately conservative: only"
        " HVAC-mode overrides are eligible; setpoint-only overrides and fan/door-window grace"
        " types are unchanged (see KNOWN_FIXES[483] for the full scope boundary). New Activity"
        " Report event 'override_adopted'.",
    ],
    "0.5.16": [
        "Fix #476: no user-visible change. Migrates all 10 remaining coordinator-dependent test"
        " scenarios (grace-period lifecycle, override detection/confirmation/self-resolve,"
        " bedtime+override interaction, cancel-override, restart behavior) to the coordinator-level"
        " Tier A harness built in #474 — closing out the full scope of #472's original"
        " investigation. Found and fixed 3 more real harness bugs along the way: a scheduler"
        " ordering bug where a coordinator listener's own state dispatch didn't settle before the"
        " next scenario event (silently misattributing timestamps to unrelated timers), an"
        " unpatched dt_util.parse_datetime() returning a MagicMock and crashing thermal-observation"
        " code, and async_track_time_change/interval callbacks (briefing/wakeup/bedtime) being"
        " constructed as coroutines but never awaited. Also fixed engine._sensor_check_callback"
        " being clobbered by an engine-only stub even in coordinator mode, breaking grace-expiry"
        " re-pause detection. Every migrated scenario was verified load-bearing via a real revert"
        " test (temporarily disabling the specific guard it protects, confirming failure, then"
        " restoring) — test-infrastructure only, no changes to coordinator.py/automation.py.",
    ],
    "0.5.15": [
        "Fix #474: no user-visible change. Adds coordinator-level Tier A test"
        " harness coverage — a real ClimateAdvisorCoordinator can now be"
        " constructed headlessly over dispatching FakeHass/FakeScheduler fakes"
        " (real state-change events, real timers), closing a gap where"
        " 12 scenarios covering override detection, away-setback correctness,"
        " and grace-period behavior had no automated regression guard. Also"
        " deletes an 18-line hand-approximation of the coordinator's real"
        " override-detection state machine that had already drifted stale"
        " (test-infrastructure only — tools/sim_harness/, tools/simulate.py;"
        " no changes to the integration itself).",
    ],
    "0.5.14": [
        "Fix #470: the chart's predicted-indoor curve could disagree with its own"
        " displayed target band overnight on nights where an adaptive sleep"
        " setpoint applied and sleep_heat/sleep_cool were left at their defaults"
        " (not explicitly configured) — the prediction curve silently used a flat"
        " default sleep floor while the band shown alongside it used the"
        " thermal-model-adjusted one. Also completes Phase B (coordinator single-"
        " source): the chart's target-band schedule is now computed once per"
        " request instead of twice.",
    ],
    "0.5.13": [
        "Fix #468: the AI Activity Report and Investigator's thermal-model sections"
        " could show an empty learning-health summary and a blank thermal"
        " equilibrium temperature even when the dashboard's Comfort Score sensor"
        " showed real rejection/observation data for the same moment — three"
        " AI-context call sites queried the thermal model without the"
        " per-observation-type health data the dashboard already includes,"
        " producing a structurally incomplete result for no reason. One of the"
        " three had already computed that exact data a few lines above for its"
        " own display and simply never passed it along. Now all three match what"
        " the dashboard sees.",
    ],
    "0.5.12": [
        "Fix #466: no user-visible change. Continues Phase B (coordinator single-"
        " source): added target_temp/target_temp_low/target_temp_high to"
        " coordinator.data so ai_skills_activity.py and ai_skills_context.py stop"
        " independently re-fetching the thermostat entity to derive the same"
        " values. api.py's dashboard status endpoint deliberately keeps its own"
        " live read — it powers the ca_target_heat/cool divergence check (#402/"
        " #462), whose entire purpose is comparing CA's computed target against"
        " the real thermostat right now, not a snapshot that can be up to 30 min"
        " old.",
    ],
    "0.5.11": [
        "Fix #464: no user-visible change. Starts Phase B of the architecture-"
        " consolidation direction (coordinator single-source) by adding"
        " coordinator.get_hvac_runtime_today() as the one place today's live HVAC"
        " runtime is computed, replacing an identical formula that was copy-pasted"
        " byte-for-byte in coordinator.py, ai_skills_context.py, and"
        " ai_skills_activity.py. No drift had occurred yet, but any future change"
        " to the formula (e.g. excluding paused/away time) would have needed to be"
        " applied in 3 places to avoid the AI Activity Report and Investigator"
        " silently diverging from the dashboard.",
    ],
    "0.5.10": [
        "Fix #462: the dashboard's setpoint-divergence indicator (ca_target_heat/cool)"
        " could show the wrong intended target while the home was in away or vacation"
        " mode — it never accounted for occupancy at all, so it displayed the comfort or"
        " sleep band even though the thermostat was actually being held at the (wider)"
        " setback band. Routed through the same select_comfort_band() function every"
        " real setpoint-writing code path already uses, so this indicator can no longer"
        " silently drift from what the thermostat is actually doing. Also corrected the"
        " fallback used when sleep_heat/sleep_cool aren't explicitly configured, from the"
        " flat daytime comfort temps to the documented sleep defaults (64/72°F), matching"
        " what the thermostat is actually set to overnight in that configuration.",
    ],
    "0.5.9": [
        "Fix #460: no user-visible change (confirmed via unit tests and a positive"
        " control). Consolidated the 'should this comfort/setback code path defer"
        " because occupancy is away/vacation' gate — previously phrased 3 different"
        " (but logically equivalent) ways across automation.py's setpoint paths"
        " (_set_temperature_for_mode, handle_bedtime, handle_pre_cool,"
        " handle_morning_wakeup) — into a single should_defer_to_occupancy_setback()"
        " function. No drift had occurred yet, but the risk was live: a future change"
        " to which occupancy modes should defer could easily be applied to 3 of the 4"
        " sites and miss the 4th, the same class of bug already found once in #458.",
    ],
    "0.5.8": [
        "Fix #458: the AI Activity Report could misreport the whole-house fan as a"
        " contradiction ('hvac_mode=off but hvac_action=fan') during the brief window"
        " where CA detects and self-corrects a stale WHF on/off flag (Issue #423's"
        " 'active (unconfirmed)' state) — that specific fan state was missing from this"
        " report's allow-list of expected fan activity, even though the dashboard status"
        " card already handled it correctly. Consolidated the two independently-written"
        " checks (coordinator.py, ai_skills_activity.py) onto one shared predicate so this"
        " class of drift can't recur; also fixed a second latent gap the consolidation"
        " surfaced: a confirmed-running manual fan override wasn't suppressing the"
        " coordinator's own internal contradiction-warning event either.",
    ],
    "0.5.7": [
        "Fix #456: no user-visible change (confirmed via differential testing and a"
        " positive control). Consolidated the nat-vent 'hard exit floor' formula — the"
        " sleep-aware threshold below which an active free-cooling session ends outright"
        " — from 3 independent implementations down to 1. Two automation.py call sites"
        " (check_natural_vent_conditions, nat_vent_temperature_check) previously"
        " recomputed this formula inline instead of using the already-pure, already-tested"
        " fan_thermostat_decision.py version — the same 'sibling function silently drifts'"
        " bug class behind issues #400/#402/#417. No drift had occurred yet here, but the"
        " risk was live: a future fix to one copy could easily miss the other two.",
    ],
    "0.5.6": [
        "Fix #454: no user-visible change. Extracted the shared shape behind the"
        " nat-vent gate's old-vs-new differential comparator (shadow-mode instrumentation,"
        " the Call/ComparisonRun result shape, substitution mode) into a reusable base so"
        " each upcoming pure decide_*() extraction gets a comparator by supplying only"
        " which production method and pure function to wire together, instead of a new"
        " copy-pasted comparator file. A first cut of the refactor introduced an import-order"
        " bug that broke the CLI comparator tool (resolving the production class before the"
        " module that installs test HA stubs) — caught by running the tool directly, not"
        " just the test suite, and fixed before merge.",
    ],
    "0.5.5": [
        "Fix #452: no user-visible change. Continues the nat-vent architecture-reset"
        " direction (v0.5.1) into the test suite — 14 test helpers that hand-copied"
        " production logic (API view dispatch, sensor attributes, coordinator status"
        " strings) because HomeAssistantView couldn't be instantiated in tests now"
        " exercise the real classes directly. Along the way this caught and fixed a"
        " stale test assertion that had silently drifted from production: the bedtime"
        " status line's expected setpoint used an old comfort-temp-plus-delta formula"
        " that stopped matching the real sleep_heat/sleep_cool config keys, so the old"
        " test was passing against logic that no longer runs.",
    ],
    "0.5.4": [
        "Fix #449: found the real reason a whole-house fan could stay off for hours"
        " overnight after being turned off outside of Climate Advisor (e.g. a wall"
        " switch or the device's own remote) — in dual-entity setups (a control switch"
        " plus a separate power-detection sensor), the control entity's Home Assistant"
        " state can silently keep saying 'on' even though the fan is truly off, since"
        " it's a one-way command with no feedback of its own. A plain 'turn on' command"
        " sent to an entity Home Assistant already believes is on can be silently"
        " dropped before it ever reaches the device. Climate Advisor now checks the"
        " power-detection sensor before every command: if the control entity and the"
        " sensor already agree, nothing is touched; if they disagree, it forces a real"
        " transition (off, briefly, then on — or the reverse) so the command actually"
        " reaches the fan. Confirmed against real device history from an actual"
        " overnight incident. Only affects dual-entity whole-house-fan setups —"
        " single-entity setups and HVAC-fan-mode ventilation are unchanged.",
    ],
    "0.5.3": [
        "Fix #446: an automated self-correction (Issue #423's fan physical-drift check"
        " fixing its own stale belief about whether the fan was on) was reported in the"
        " Activity Report as 'Grace period started (manual)' — telling you that you"
        " turned the fan off when nobody did. It's now correctly labeled as an"
        " automation-triggered grace period.",
        "Fix #446: after a restart, if a fan kept appearing as 'running without CA"
        " warrant' (e.g. a thermostat's own circulation schedule CA can't durably"
        " override with a single command), CA re-issued the same correction attempt"
        " every few minutes for up to 45 minutes. It now waits 5 minutes between"
        " correction attempts for the same condition, while still keeping a"
        " persistently-stray fan visible in the logs.",
    ],
    "0.5.2": [
        "Fix #444: the Activity Report could show the same 'Comfort band applied' line"
        " 2-3 times in a row for the exact same setpoint — most visibly right after an"
        " HA restart, when the startup sequence and the regular classification cycle"
        " both independently re-announced the identical band within the same minute."
        " The underlying thermostat command was always correct; only the notification"
        " was duplicated. A short-window dedup now suppresses a redundant announcement"
        " of an unchanged band, without ever skipping the actual setpoint command.",
    ],
    "0.5.1": [
        "Fix #439: the initial setup wizard could write stale sleep-temperature"
        " defaults into a brand-new install — Fahrenheit sleep fields, and all six"
        " Celsius setpoints, were hardcoded and never picked up the household-matched"
        " defaults shipped in 0.5.0. Every unit now derives its default directly from"
        " the same shared constants, so new installs get the intended values.",
        "Fix #440: on a warming-trend night, if natural ventilation ended earlier than"
        " its originally scheduled close time — for any reason, including the window"
        " simply being closed — the overnight pre-cool AC trigger stayed on the old"
        " schedule instead of stepping in right away. It now reacts to nat-vent"
        " actually ending and moves the AC trigger earlier when that saves time,"
        " never later.",
    ],
    "0.5.0": [
        "Feat #438: the default comfort/setback/sleep temperatures shipped for fresh installs"
        " (and any config relying on an unconfigured fallback) now match a real, tuned household"
        " configuration instead of arbitrary round numbers — comfort 68°F/74°F, setback 63°F/79°F,"
        " and a flat sleep target of 64°F/72°F that's cooler than daytime comfort, not warmer."
        " Fixed 3 latent bugs found along the way where a hardcoded fallback had silently drifted"
        " from the value it was supposed to mirror (a setpoint-inconsistency check, the chart's"
        " fan-activity prediction, and the away/vacation display in the daily briefing).",
        "Fix #437: on a warming-trend night, the overnight pre-cool phase (which lowers the AC"
        " ceiling to bank cold thermal mass before the next hot day) could silently become a"
        " no-op — it computed a target but immediately clamped it back up near daytime comfort,"
        " so no extra cooling ever happened even though the system reported pre-cool as active."
        " The clamp now anchors to the sleep temperature range instead of the daytime one, so"
        " pre-cool can use its full intended range. This also closes #436: the chart's target-band"
        " display and the real overnight setpoint can no longer show different pre-cool numbers,"
        " since both now compute the target the same single way.",
    ],
    "0.4.75": [
        "Fix #435: if you run natural ventilation with no whole-house fan or HVAC-fan device"
        " configured (relying on manually-opened windows instead), the activity report could"
        " show a confusing 'Nat-vent fan on/off' entry claiming device \"none\" turned on or"
        " off — even though nothing happened, since there's no fan to control in that setup."
        " The cycling check now only reports a fan transition when one actually occurred.",
    ],
    "0.4.74": [
        "Fix #427: overnight whole-house-fan nat-vent sessions were being torn down and"
        " re-adopted every 5-15 minutes for hours, showing repeated 'fan running (untracked)'"
        " and 'startup reconcile' notifications even though the window never closed. The"
        " proactive floor-exit check (which predicts an imminent floor crossing from the"
        " thermal model) was comparing indoor temperature against the flat daytime comfort"
        " floor instead of the lower overnight sleep floor, so during the sleep window it"
        " believed the floor was already breached hours before it actually was and kept"
        " ending the session for no reason. It now uses the same sleep-aware floor as every"
        " other nat-vent exit/reactivation check, so sessions persist correctly through the"
        " night and the fan only cycles the way it's supposed to.",
    ],
    "0.4.73": [
        "Fix #428: 'Your Next Action' could tell you to open a window or turn on a fan to cool"
        " down even when it was hotter outside than inside — advice that would have made things"
        " worse. It now checks live outdoor temperature (the same free-cooling direction guard"
        " already used by the economizer/nat-vent logic) before ever suggesting a window or fan,"
        " covers the mirrored heating-direction case, and won't repeat advice that's redundant"
        " with what you've already done or what automation is already doing.",
    ],
    "0.4.72": [
        "Fix #424: fan mode 'Both' (whole house fan + HVAC fan simultaneously) is no longer"
        " selectable during setup or in options — a proper per-device redesign for two"
        " independently-tracked physical fans was judged too risky to build on top of the"
        " already-fragile fan-reconcile logic (site of the recent #423 incident), so the"
        " option is removed instead. Existing installs configured with 'Both' are"
        " automatically migrated to 'Whole house fan' the next time the config entry loads.",
    ],
    "0.4.71": [
        "Fix #423: a whole-house fan could get stuck showing 'active (unconfirmed)' for"
        " hours after physically turning off, with nat-vent never resuming even though"
        " conditions clearly favored free cooling. Root cause: the fan-reconcile logic that"
        " runs after a thermostat-internal fan blip always trusted the thermostat's own fan"
        ' attributes as "the fan is running" — correct for a furnace/AC blower, but wrong'
        " for a physically separate whole-house fan switch, which could get silently"
        ' "adopted" as running when it was actually off. It now checks the real configured'
        " fan's own reported state for whole-house-fan setups. Also added a background check"
        " that self-corrects a stuck fan-status flag within about 10 minutes if it ever"
        " disagrees with the real device, instead of only showing 'unconfirmed' in the UI.",
    ],
    "0.4.70": [
        "Fix #418: two remaining nat-vent exit paths (closing the last open window, and the"
        " fast free-cooling-reversal check that runs on every temperature update) now go"
        " through the same unified exit handling the other paths already used. The"
        " fast-loop path had a real bug — it could mark the session as 'paused, waiting for"
        " the window to close' while still turning the HVAC back on into that open window."
        " Closing the last window now restores HVAC and lets it settle into the right mode"
        " within a few minutes (previously instant) — a deliberate tradeoff for consistency.",
    ],
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
# Each entry documents which code paths a fix covered, so the analyzer can say
# "Issue #X fixed this — treat as resolved" instead of "could not verify."
# The AI Investigator renders the matching RELEASE_NOTES bullet for each entry
# (not title/scope_covered — those are for a human/dev-stack session reading this
# file directly). No scope_not_covered field — see Issue #563: it was mandatory on
# every entry, which silently defeated the version-scoping filter below (all 169
# entries always matched). If a fix leaves a genuinely open gap, track it as a
# GitHub issue instead — the Investigator already reads live open issues.
# Add an entry here as part of the definition of done when closing any issue.
KNOWN_FIXES: dict[int, dict] = {
    733: {
        "version_fixed": "0.6.56",
        "title": (
            "An HA restart that landed while a window was open and nat-vent-eligible"
            " conditions held could silently cancel the whole-house-fan session"
            " _do_startup_coalesce() had just correctly activated, leaving the"
            " physical fan running with no thermostatic oversight until the next"
            " scheduled check the following morning."
        ),
        "scope_covered": (
            "automation.py: _reconcile_fan_on_startup_locked()'s not-thermostat_"
            "fan_running branch now checks _is_recent_fan_command_callback"
            "(threshold_seconds=30.0) — the same recent-command guard"
            " _reconcile_fan_physical_drift() already used for the identical class"
            " of stale-read-vs-fresh-command race — before clobbering _fan_active/"
            "_natural_vent_active/_nat_vent_soft_start, and defers to the fresh"
            " in-pass command instead. _deactivate_fan()'s 'already inactive'"
            " early-return path now always calls _cancel_fan_thermo_backstop(),"
            " closing the general class of bug where a self-rescheduling"
            " thermostatic backstop timer could be left orphaned (armed but"
            " pointing at flags that already say nothing is active) by any path"
            " that clears _fan_active outside the full deactivation flow."
        ),
    },
    731: {
        "version_fixed": "0.6.55",
        "title": (
            "Fan/whole-house-fan control (12 real automation.py entry points) was"
            " still ad-hoc boolean flags with no pure, independently-testable"
            " decision layer and no shadow-diagnostic coverage — the same gap the"
            " nat-vent (#633), door/window (#637), and override/grace (#639) FSM"
            " extractions already closed for their own lifecycles."
        ),
        "scope_covered": (
            "fan_lifecycle.py (new): pure 5-axis composed FanLifecycleState"
            " derivation (physical/override/cycling/hvac_ownership/rate_limit)."
            " fan_toggle_rate_limit.py (new): the rapid-cycling backstop decision,"
            " delegated to from automation.py's _fan_toggle_rate_limited()."
            " fan_fsm.py (new): the unified (state, event) -> Transition table —"
            " 16 event kinds, one per real entry point, dispatched on event kind"
            " (handler-triggered, same shape as override_grace_fsm.py); 2 kinds"
            " (THERMO_BACKSTOP_TICK, THERMOSTAT_CHECK_TICK) deliberately never move"
            " to_state, since their outcomes only inform routing this FSM's axes"
            " can't represent without duplicating _exit_nat_vent()/_deactivate_fan()"
            " logic. automation.py: _build_fan_fsm_inputs()/fan_lifecycle_state"
            " property/_apply_fan_fsm_state()/_resolve_fan_fsm_state() — the single"
            " dispatch chokepoint all 16 real entry points now route through, gated"
            " by AutomationEngine._fan_fsm_authoritative (fixed at construction, off"
            " for production/on for shadow — no production behavior change)."
            " coordinator.py: the 4th fixed-per-engine-identity flag"
            " (_engine_a._fan_fsm_authoritative=False, _engine_b's =True), plus a"
            " new fan_mirror shadow-diagnostic axis in"
            " _update_shadow_engine_diagnostic() comparing"
            " automation_engine.fan_lifecycle_state against"
            " shadow_automation_engine.fan_lifecycle_state (no paired fan_fsm axis"
            " — the shadow engine's own fan_lifecycle_state already IS the"
            " FSM-derived state, so a third comparison point would be tautological)."
            " sensor.py: ClimateAdvisorShadowEngineStatusSensor exposes"
            " fan_production_state/fan_shadow_state/fan_mirror_agrees and a"
            " debounce.fan_mirror sub-dict. docs/fan-lifecycle-spec.md (new)."
        ),
    },
    706: {
        "version_fixed": "0.6.46",
        "title": (
            "Nat-vent FSM's production input builder never populated"
            " override_active/grace_active (Bug D); a decision computed before"
            " an await could clobber a real override that started mid-await"
            " (Bug F); the FSM's grace short-circuit didn't model the Issue"
            " #134 overheat-during-grace exception (#688)"
        ),
        "scope_covered": (
            "automation.py: _build_nat_vent_fsm_inputs() now reads live"
            " override/grace state; new _apply_nat_vent_fsm_state_after_"
            " activation() checks _activate_fan()'s FanCommandResult and"
            " applies INACTIVE instead of a stale decision when it returns"
            " OVERRIDDEN, wired into all 5 real call sites. nat_vent_fsm.py:"
            " new shared _grace_blocks_natvent() models the Issue #134"
            " exception in both transition functions."
        ),
    },
    707: {
        "version_fixed": "0.6.46",
        "title": (
            "RF-timer restart-resume's inner handle_fan_manual_override() call"
            " never fed the override/grace shadow FSM tracker, since it's"
            " invisible to _mirror_to_shadow()'s outer-method-name dispatch"
        ),
        "scope_covered": (
            "coordinator.py: _do_startup_coalesce() now explicitly feeds"
            " _evaluate_override_grace_fsm(FAN_OVERRIDE_DETECTED) when the"
            " RF-timer-survives-restart branch actually fires. Diagnostic-only"
            " — production's real override/grace flags were always correct."
        ),
    },
    708: {
        "version_fixed": "0.6.46",
        "title": (
            "Grace-expiry paused-reactivation (_re_pause_for_open_sensor())"
            " never consulted the nat-vent FSM at all, gated only by the"
            " door/window switch"
        ),
        "scope_covered": (
            "automation.py: _re_pause_for_open_sensor() now routes its"
            " reactivation decision through nat_vent_fsm.transition() when"
            " natvent_fsm_authoritative is True, independent of the"
            " door/window switch."
        ),
    },
    709: {
        "version_fixed": "0.6.46",
        "title": (
            "Door/window FSM's _grace_active write conflicted with"
            " override/grace's sole ownership of that flag; door_window_fsm.py"
            " had no equivalent of grace_would_start, so 3 grace-landing"
            " transitions could set a phantom _grace_active with no real timer"
            " when grace was configured off"
        ),
        "scope_covered": (
            "automation.py: _apply_door_window_fsm_state() no longer writes"
            " _grace_active/_grace_protects_override — override/grace's"
            " dispatcher is the sole writer. door_window_fsm.py: new"
            " manual_grace_would_start/automation_grace_would_start input"
            " fields gate the 3 real grace-landing transitions"
            " (_transition_from_paused()'s DASHBOARD_RESUME and"
            " ALL_SENSORS_CLOSED->RESTORE_AND_GRACE,"
            " _transition_from_paused_during_grace()'s DASHBOARD_RESUME)."
            " New tests/test_fsm_flag_ownership.py adds a generalized,"
            " AST-based 'exactly one writer' regression guard for all 3 FSMs'"
            " modeled flags."
        ),
    },
    711: {
        "version_fixed": "0.6.47",
        "title": (
            "handle_morning_wakeup()'s DEFER_NAT_VENT branch left an active"
            " nat-vent/WHF session unchecked against the newly-armed daytime"
            " comfort band, deferring re-evaluation to whatever the next"
            " unrelated periodic/temp-change tick happened to be (up to 5 min"
            " later via the backstop timer) — long enough for indoor to drift"
            " past the graceful daytime cycle-off point into the hard exit"
            " floor before daytime rules were ever applied (#705)"
        ),
        "scope_covered": (
            "automation.py: handle_morning_wakeup()'s DEFER_NAT_VENT branch"
            " now calls nat_vent_temperature_check() immediately with the live"
            " indoor/outdoor reading, so an already-active session is"
            " re-evaluated against the new band at the moment it takes"
            " effect. Already mirrored to the shadow engine via the existing"
            " _mirror_to_shadow('handle_morning_wakeup', ...) call — no"
            " separate wiring needed."
        ),
    },
    714: {
        "version_fixed": "0.6.48",
        "title": (
            "The _whf_owns_hvac() mutex (Issue #392) only choked CA's own"
            " automation-initiated HVAC writes; a manual override the user makes"
            " directly at the thermostat never called _set_hvac_mode(), so nothing"
            " ended an active nat-vent/WHF session — and worse, _activate_fan() would"
            " silently force the thermostat back to 'off' on WHF reactivation even"
            " over a live manual 'cool' override, with no override check at all (#705)"
        ),
        "scope_covered": (
            "automation.py: start_override_confirmation() now ends an active"
            " nat-vent/WHF session immediately the instant a mode change to an"
            " active mode is detected (event-driven, same as"
            " handle_all_doors_windows_closed()), not routed through"
            " _exit_nat_vent() to avoid restoring a stale pre-fan HVAC mode over the"
            " user's live override. _activate_fan() gained the same"
            " override-respecting guard _fan_override_active already has."
            " nat_vent_exit.py/fan_thermostat_decision.py: decide_nat_vent_exit()/"
            " decide_fan_thermostat_check() gained a new highest-priority"
            " manual-override-conflict check (defense-in-depth, tick-based,"
            " already-mirrored to the shadow engine via existing"
            " _sync_shadow_inputs() override-field sync from Issue #631 — no new"
            " wiring needed)."
        ),
    },
    717: {
        "version_fixed": "0.6.50",
        "title": (
            "lifecycle_dispatcher.py's pub/sub router (Issue #633) was built,"
            " tested, and never wired into any real decision path — the three"
            " lifecycle FSMs still cross-read each other's raw booleans by direct"
            " same-object attribute access rather than through the dispatcher's"
            " audited emit/consume contract."
        ),
        "scope_covered": (
            "automation.py: AutomationEngine now owns its own LifecycleDispatcher"
            " instance (never shared with the shadow engine's — structural"
            " isolation, same precedent as AutomationEngineCallbacks/Issue #604)"
            " and registers itself as a real controller for all 8 event types."
            " Real emit() calls added at the 2 existing chokepoints every real"
            " door/window and override/grace transition already funnels through"
            " (_resolve_door_window_pause_flags(), _resolve_override_grace_fsm_state()),"
            " keyed on a before/after diff of _paused_by_door/_grace_active rather"
            " than a static kind-to-direction table (avoids mis-classifying"
            " compound kinds like PAUSED_NAT_VENT_REACTIVATED). A second real"
            " _paused_by_door writer, _apply_nat_vent_fsm_state() (the"
            " nat-vent-FSM-authoritative path), gained its own matching diff/emit."
            " _confirm_override_action()/_clear_manual_override_active() emit"
            " OVERRIDE_CONFIRMED/CLEARED at their single real sites."
            " lifecycle_events.py: added a NAT_VENT_SESSION_STARTED/ENDED pair,"
            " emitted from a before/after diff wrapped around _decision_pass()"
            " (the one point all ~18 scattered _natural_vent_active write sites"
            " already funnel through under _decision_lock) rather than"
            " instrumenting each site individually. An earlier version of this"
            " change also routed _build_nat_vent_fsm_inputs()/"
            " _build_door_window_fsm_inputs() through dispatcher-only mirror"
            " attributes instead of the canonical flags — reverted after it broke"
            " the established direct-attribute-assignment fixture convention"
            " across 40+ existing test files, for no real safety benefit (this"
            " engine both emits and consumes every event today, so same-object"
            " attribute access can never actually go stale the way a genuine"
            " cross-instance mirror could). No decision logic, no authoritative"
            " switch, and no HA service call path changed."
        ),
    },
    716: {
        "version_fixed": "0.6.49",
        "title": (
            "Shadow engine's fan_thermostat_check() mirroring was inert — _fan_active,"
            " the field the mirrored decision actually keys off, was never mirrored to"
            " the shadow instance at all, and its two real writers (_activate_fan()/"
            " _deactivate_fan()) both return early under dry_run before ever assigning"
            " it, so a direct method-replay mirror could never have worked for them."
        ),
        "scope_covered": (
            "coordinator.py: _sync_shadow_inputs() now raw-copies _fan_active from"
            " production to shadow every cycle, same mechanism already used for the"
            " nat-vent/grace/override fields (Issues #615/#631/#673) — sidesteps the"
            " dry_run early-return entirely and transparently covers every writer of"
            " the field, including the coordinator's own stale-flag correction in"
            " _async_thermostat_changed. Also mirrors fan_thermostat_check() at its"
            " two previously-unmirrored call sites (the dedicated indoor/outdoor temp"
            " listeners) — only the thermostat-attribute-change dispatch path was"
            " mirrored before. tests/test_shadow_engine_coverage.py: _fan_active added"
            " to _TRACKED_FIELDS; _activate_fan/_deactivate_fan classified 'internal'"
            " (reached via the raw copy, not a mirror call); new per-caller coverage"
            " test for fan_thermostat_check(), matching the existing"
            " handle_manual_override() precedent."
        ),
    },
    721: {
        "version_fixed": "0.6.51",
        "title": (
            "The paused_by_door guard in handle_manual_override_during_pause()/"
            "resume_from_pause() was the one door/window cross-read Issue #717 left"
            " reading the raw canonical attribute instead of a dispatcher mirror."
        ),
        "scope_covered": (
            "Investigated re-sourcing it to _dispatched_paused_by_door — an audit"
            " confirmed the two values are always equal in production (every real"
            " writer already funnels through a before/after diff), but 14+ existing"
            " tests (test_resume_from_pause.py and 3 sibling files) set"
            " engine._paused_by_door = True directly, bypassing the dispatcher, then"
            " call these methods immediately. Re-sourcing would have silently"
            " no-op'd every one of those tests' scenario, reproducing #717's own"
            " FSM-builder regression for a different field. The guard stays"
            " canonical; no code change to the guard itself. Also extracted"
            " _emit_boolean_transition() (a shared helper for the before/after-diff"
            "-emit shape #717 hand-rolled 3 times) and refactored all 3 existing"
            " sites onto it as a DRY cleanup found while scoping the WHF chokepoint"
            " for #722 below."
        ),
    },
    722: {
        "version_fixed": "0.6.51",
        "title": (
            "door_window_fsm.py's whf_owns_hvac cross-read stayed a direct"
            " _whf_owns_hvac() call under #717 — deriving it from the wrong signal"
            " (the nat-vent session diff) was caught and reverted before #717 shipped."
        ),
        "scope_covered": (
            "automation.py: new _resolve_whf_hvac_suppression() chokepoint wraps"
            " every real write of _pre_fan_hvac_mode and emits WHF_HVAC_SUPPRESSED/"
            "RELEASED into a new _dispatched_whf_owns_hvac mirror (audit-trail only,"
            " same role as every other _dispatched_* mirror — the FSM input itself"
            " still reads _whf_owns_hvac() directly, for the same test-fixture reason"
            " #721 above kept its guard canonical). Investigation found 4 real"
            " writers, not the 2 originally suspected: _suppress_hvac_for_whf(),"
            " _release_whf_and_reclassify(), and both of _deactivate_fan()'s"
            " stranded-suppression-release branches (Issue #618) — the latter two"
            " were missed in the original write-up and are now covered."
        ),
    },
    724: {
        "version_fixed": "0.6.52",
        "title": (
            "Shadow diagnostic never mirrored _pre_fan_hvac_mode, so its"
            " _whf_owns_hvac() was permanently False — a separate, distinct gap from"
            " #721/#722 found during their verification pass (shadow raw-copy sync,"
            " not lifecycle_dispatcher.py)."
        ),
        "scope_covered": (
            "coordinator.py: _sync_shadow_inputs() now raw-copies _pre_fan_hvac_mode,"
            " same one-line pattern as _fan_active (Issue #716). Investigation"
            " corrected the issue's own 'dormant, not urgent' framing: traced"
            " _whf_owns_hvac() to a live-reachable divergence via"
            " _sync_paused_by_door_with_live_sensors() (called from 4 already-mirrored"
            " entry points — apply_classification/handle_bedtime/"
            " handle_morning_wakeup/handle_pre_cool), which reads it as an"
            " early-return guard before calling _pause_for_door_window() (sets"
            " _paused_by_door, a field the shadow diagnostic directly compares)."
            " Without the fix, a genuine WHF session with a window open — WHF's"
            " designed use case — made the shadow incorrectly self-pause while"
            " production correctly did not. tests/test_shadow_engine_coverage.py:"
            " _pre_fan_hvac_mode added to _TRACKED_FIELDS; _suppress_hvac_for_whf()/"
            " _release_whf_and_reclassify() newly registered (internal, raw-copy"
            " covered); _deactivate_fan()/restore_state() already covered."
            " tests/test_shadow_engine_live.py: new TestSyncShadowInputsWhfOwnsHvac"
            " (parity, positive control, and a direct reproduction of the guard"
            " divergence with/without the fix). Zero production/HVAC impact — shadow"
            " engine is permanently dry_run=True."
        ),
    },
    727: {
        "version_fixed": "0.6.53",
        "title": (
            "The 3 FSM-authoritative switches reset to off on every restart by"
            " deliberate Phase R design; the shadow engine could never issue real"
            " commands, only compare against production forever."
        ),
        "scope_covered": (
            "coordinator.py: switch.climate_advisor_nat_vent_fsm_authoritative /"
            " _door_window_fsm_authoritative / _override_grace_fsm_authoritative now"
            " persist across a restart (top-level 'fsm_authoritative' dict in the"
            " state file, restored unconditionally in async_restore_state() before"
            " the same-day gate — a deliberate departure from most of that dict,"
            " which IS date-gated, since these are mode-like settings not daily"
            " ephemeral state). New switch.climate_advisor_shadow_engine_primary"
            " (also persisted) lets the shadow AutomationEngine be promoted to issue"
            " real HVAC/fan commands, demoting the previous production engine to"
            " diagnostic-only — implemented as a routing property"
            " (coordinator.automation_engine / .shadow_automation_engine) over two"
            " fixed engine handles (_engine_a/_engine_b) selected by"
            " _shadow_is_primary, so every existing read call site (api.py,"
            " sensor.py, coordinator.py itself) keeps working unchanged."
            " async_set_shadow_engine_primary() carries over command-tracking/"
            " echo-suppression fields and the 3 FSM flags (never covered by the"
            " existing _sync_shadow_inputs() raw-copy) before flipping dry_run/"
            " callback bundles/role on both engines. KNOWN LIMITATION, logged at"
            " promotion time: the shadow engine's decision coverage is a strict"
            " subset of production's (see coordinator.py's documented un-mirrored"
            " entry-point list) — not gated on closing that gap for this pass."
        ),
    },
    729: {
        "version_fixed": "0.6.54",
        "title": (
            "Issue #727's live in-process engine swap couldn't migrate in-flight"
            " AutomationEngine timers to the newly-primary engine, and 5 of 13"
            " internal timers (including one that issues a real _set_temperature()"
            " call) were never cancellable by cleanup() at all — plus 4 switches"
            " (3 FSM-authoritative + the promotion one) was more control surface"
            " than the single legacy-vs-FSM choice actually used."
        ),
        "scope_covered": (
            "coordinator.py: async_set_shadow_engine_primary() no longer swaps"
            " engines live — it persists the choice and calls"
            " hass.config_entries.async_reload() (fire-and-forget, matching the"
            " same pattern repairs.py's own reload call uses for code belonging to"
            " the entry being reloaded). The reload's existing, already-tested"
            " teardown (async_shutdown() -> both engines' cleanup()) cancels every"
            " internal timer as a side effect, closing the migration gap"
            " structurally instead of hand-carrying fields across a live swap."
            " _ENGINE_COMMAND_TRACKING_FIELDS/_ENGINE_FSM_AUTHORITATIVE_FIELDS and"
            " the fsm_authoritative persisted-state dict are removed — no longer"
            " needed. switch.py: the 3 per-subsystem FSM-authoritative switches"
            " (ClimateAdvisorNatVentFsmAuthoritativeSwitch/DoorWindow.../"
            "OverrideGrace...) are deleted; each engine's 3 FSM flags are now fixed"
            " at construction (_engine_a always legacy, _engine_b always FSM) in"
            " coordinator.py's __init__ — switch.climate_advisor_shadow_engine_primary"
            " is the sole remaining control axis. automation.py: 3 new tracked"
            " cancel-handle attributes (_setpoint_retry_cancel,"
            " _fan_on_verify_cancel, _fan_off_verify_cancel) for the 5 previously-"
            " uncovered timer sites, wired into cleanup(); role=<production|shadow>"
            " added to the 5 real-command log chokepoints"
            " (_set_hvac_mode/_set_temperature/_activate_fan/_deactivate_fan/"
            "_notify), backed by a class-level AutomationEngine.role default so"
            " partially-constructed test fixtures don't crash. sensor.py: the"
            " shadow-engine-status sensor's 2 independent FSM-authoritative"
            " attributes collapse into one fsm_engine_primary field."
        ),
    },
    684: {
        "version_fixed": "0.6.45",
        "title": (
            "Shadow diagnostic's nat-vent lifecycle re-derivation hardcoded"
            " lockout_seconds=300 instead of reading the configured"
            " CONF_NAT_VENT_REACTIVATION_LOCKOUT_S value"
        ),
        "scope_covered": (
            "coordinator.py: _update_shadow_engine_diagnostic()'s nested"
            " _state_for() helper now reads"
            " self.config.get(CONF_NAT_VENT_REACTIVATION_LOCKOUT_S,"
            " NAT_VENT_REACTIVATION_LOCKOUT_S), matching the pattern"
            " _evaluate_nat_vent_fsm() has always used correctly. Since the"
            " default lockout is also 300s, the bug was invisible on any"
            " install using the default value — only installs with a"
            " configured lockout different from 300s could see this"
            " diagnostic-only comparison's production_state/shadow_state re-"
            " derivation systematically disagree with the FSM's own"
            " (correctly config-driven) state for the full duration of any"
            " lockout window. No real fan/HVAC decision was ever affected —"
            " this function's output is never written back to either engine."
        ),
    },
    698: {
        "version_fixed": "0.6.44",
        "title": (
            "Nat-vent mid-session fan cycling (on/off while a session stays"
            " active) and the fast in-session exit check both had no FSM"
            " equivalent — the largest remaining gap in the Epic #594 Phase R"
            " nat-vent build-out"
        ),
        "scope_covered": (
            "New pure module nat_vent_cycling.py (decide_nat_vent_cycling(),"
            " NatVentCyclingInputs, NatVentCyclingDecision) reimplements"
            " nat_vent_temperature_check()'s cycle-off/cycle-on threshold"
            " math. nat_vent_fsm.py: NatVentFsmInputs gained"
            " fan_hardware_active; NatVentTransition gained"
            " fan_should_be_active, populated by _transition_from_active()"
            " when the exit chain returns NONE. automation.py's"
            " nat_vent_temperature_check() (fast per-tick check) now, when"
            " _natvent_fsm_authoritative is enabled: (1) runs the full 5-check"
            " decide_nat_vent_exit() priority chain instead of re-checking"
            " only the comfort floor, so a session can now end via any of the"
            " 5 exit reasons on the fast loop instead of waiting up to 30 min"
            " for the slow loop; (2) drives cycle-off/cycle-on via"
            " decide_nat_vent_cycling(), applying results through the same"
            " _activate_fan()/_deactivate_fan() side-effect code legacy uses."
            " Also fixed a hand-duplicated outdoor-warm-past-indoor comparison"
            " in the cycle-on reactivation guard by delegating to the shared"
            " is_outdoor_rise_exit() (fan_thermostat_decision.py) — applies in"
            " both the authoritative and legacy branches, a plain"
            " bugfix independent of the FSM switch. Dashboard visibility for"
            " a cycled-off/'resting' session required no new code — confirmed"
            " via git archaeology that 'nat-vent (session active, fan idle)'"
            " has existed in _compute_fan_status()/_compute_whf_status()/"
            " _compute_hvac_fan_status() since Issue #321 (v0.4.18) and was"
            " already wired to the dashboard. Two-pass verification caught and"
            " fixed 4 defects before merge: a dropped k_passive diagnostic"
            " field in the fast loop's PROACTIVE_FLOOR exit payload, two"
            " inaccurate descriptions on the differential-harness's allowlist"
            " for known pre-existing scenario divergences, and — the most"
            " consequential — the allowlist's assertion was tightened from a"
            " bare 'some divergence exists' check to pinned exact"
            " event/action divergence counts per scenario, so a future"
            " regression in any of the 6 allowlisted golden scenarios can no"
            " longer pass silently. Surfaced (not fixed, tracked separately)"
            " a same-tick reactivation race after a fast-loop exit — see"
            " issue #699."
        ),
    },
    694: {
        "version_fixed": "0.6.43",
        "title": (
            "Phase 2b FSM wiring (nat-vent decision points behind"
            " _natvent_fsm_authoritative) killed or silently downgraded an"
            " in-flight nat-vent session on a second door/window opening"
        ),
        "scope_covered": (
            "automation.py: 3 defects in the Phase 2b wiring, all gated"
            " behind _natvent_fsm_authoritative (off by default). (1)"
            " handle_door_window_open()'s nat-vent gate check (~line 3163)"
            " read self.nat_vent_lifecycle_state as the FSM's starting"
            " state for what is a pure entry-gate question, routing an"
            " already-active session through the FSM's exit chain instead"
            " of the entry gate on a second window opening; now forces"
            " NatVentLifecycleState.INACTIVE, matching"
            " reconcile_fan_on_startup()'s existing rationale for the same"
            " pattern. (2) The same call site's activation branch"
            " hardcoded NatVentLifecycleState.ACTIVE_FULL_GATE regardless"
            " of the FSM result, silently demoting an in-flight soft-start"
            " session to full-gate via _apply_nat_vent_fsm_state()'s"
            " unconditional projection; now projects ACTIVE_SOFT_START when"
            " _nat_vent_soft_start is already set, matching legacy's"
            " narrower write at this site. (3) The idle-open re-entry site"
            " (both ACTIVE_FULL_GATE and ACTIVE_SOFT_START branches, ~lines"
            " 3560/3589) called _apply_nat_vent_fsm_state(), whose"
            " projection unconditionally clears _paused_by_door, whereas"
            " legacy's write here never touched that flag — now the"
            " pre-call value of _paused_by_door is captured and restored"
            " after the apply. The paused-reactivation site (~line 4006)"
            " was confirmed unaffected — it deliberately clears pause flags"
            " via _resolve_door_window_pause_flags(), matching legacy"
            " already. Also corrected nat_vent_lifecycle_state's stale"
            " 'read-only observability' docstring, which no longer reflects"
            " its Phase 2b role as a production decision input at 3 of the"
            " 4 wired call sites."
        ),
    },
    690: {
        "version_fixed": "0.6.42",
        "title": (
            "Fast-loop and slow-loop nat-vent outdoor-rise exit checks used"
            " different boundary comparisons (>= vs >), disagreeing for up to"
            " 30 minutes at exact outdoor==indoor temperature equality"
        ),
        "scope_covered": (
            "New is_outdoor_rise_exit(indoor, outdoor) in"
            " fan_thermostat_decision.py (same module as the existing shared"
            " resolve_hard_exit_floor()) — non-strict (>=). Both"
            " fan_thermostat_check()'s fast-loop Check 1 and"
            " nat_vent_exit.py's OUTDOOR_RISE check now delegate to it."
            " nat_vent_exit.py's check changes from strict (>) to non-strict"
            " (>=) — a deliberate, real behavior change at exact equality."
            " automation.py's Debug-tab-visible exit reason string corrected"
            " to match (was still displaying '>' after the boundary became"
            " non-strict)."
        ),
    },
    691: {
        "version_fixed": "0.6.41",
        "title": (
            "Added _apply_nat_vent_fsm_state() — nat-vent's FSM-state-to-flags"
            " projection layer, mirroring the door/window engine's existing"
            " _apply_door_window_fsm_state() pattern"
        ),
        "scope_covered": (
            "automation.py: new AutomationEngine._apply_nat_vent_fsm_state()"
            " derives _natural_vent_active/_nat_vent_soft_start/_paused_by_door"
            " from a NatVentLifecycleState value. Deliberately excludes"
            " _nat_vent_outdoor_exit_time — the enum's to_state alone cannot"
            " distinguish 'just entered lockout' from 'already mid-lockout,'"
            " same exclusion-list treatment as door/window's _grace_end_time."
            " Purely additive — zero call sites anywhere in the codebase"
            " (confirmed by grep), not wired into any production path yet;"
            " that wiring is a separate, future phase. Registered in the"
            " shadow-engine flag-mutation coverage registry (test_shadow_"
            "engine_coverage.py) as 'internal', matching the same category"
            " already used for its door/window and override/grace siblings."
        ),
    },
    687: {
        "version_fixed": "0.6.40",
        "title": (
            "Nat-vent diagnostic FSM blind to manual override/grace state,"
            " reporting 'would activate' for the full duration of any manual"
            " override window"
        ),
        "scope_covered": (
            "nat_vent_fsm.py: NatVentFsmInputs gained override_active/"
            "grace_active fields; both _transition_from_inactive() and"
            " _transition_from_active() now short-circuit to INACTIVE while"
            " either is true, before any gate/exit math runs. coordinator.py:"
            " _evaluate_nat_vent_fsm() populates the new fields from"
            " ae._fan_override_active/_manual_override_active/_grace_active."
            " Diagnostic-only — self._nat_vent_fsm_state is never written back"
            " to production; the existing narrow _natvent_fsm_authoritative"
            " production feature (soft-start escalation only) is provably"
            " unaffected, since its own input-construction call site in"
            " automation.py does not pass the new fields, which default to"
            " False. Known remaining gap tracked separately in Issue #688: the"
            " short-circuit doesn't yet model the Issue #134"
            " overheat-during-grace exception."
        ),
    },
    685: {
        "version_fixed": "0.6.39",
        "title": (
            "Shadow-diagnostic disagreement WARNINGs fired instantly on any"
            " transient mismatch during a real multi-step production transition,"
            " producing false-alarm noise that undermined the A/B validation signal"
        ),
        "scope_covered": (
            "coordinator.py: new _shadow_diag_update_axis() tracks wall-clock"
            " elapsed time (not a snapshot count — proven necessary, since duplicate"
            " disagreement snapshots during a real cascade fire 1-2ms apart) since a"
            " continuous disagreement streak began, per comparison axis. All 6 axes"
            " (mirror, fsm, door_window_mirror, door_window_fsm,"
            " override_grace_mirror, override_grace_fsm) now only log a WARNING"
            " once a streak exceeds SHADOW_ENGINE_DIAGNOSTIC_DEBOUNCE_S=60 seconds."
            " A new 'debounce' sub-dict and 'cumulative_reset_date' key were added"
            " to _shadow_engine_diagnostic (additive only — no existing key removed"
            " or renamed), plus a daily-reset cumulative-seconds-of-disagreement"
            " counter per axis, surfaced additively on"
            " ClimateAdvisorShadowEngineStatusSensor. Diagnostic-only — no"
            " production HVAC/fan/grace/override logic touched."
        ),
    },
    680: {
        "version_fixed": "0.6.38",
        "title": (
            "Restart clean-slate reset bypassed the override/grace FSM dispatcher,"
            " leaving a third, ungoverned writer of _override_confirm_pending/"
            "_grace_active/_grace_protects_override alongside the FSM and legacy"
            " branches"
        ),
        "scope_covered": (
            "automation.py: AutomationEngine.restore_state()'s clean-slate block now"
            " routes through _resolve_override_grace_fsm_state(kind="
            "GRACE_TIMER_EXPIRED, origin_state=(OverrideConfirmState.IDLE,"
            " GraceState.NONE)) instead of assigning the 3 flags directly. Both the"
            " FSM branch (transition() falls through to the origin state unchanged"
            " for this event/state combination) and the legacy branch produce the"
            " identical all-clear result — the clean-slate restart policy itself is"
            " unchanged, only how the flags get cleared."
        ),
    },
    679: {
        "version_fixed": "0.6.37",
        "title": (
            "Issue #508's stuck-grace backstop notified the override/grace"
            " diagnostic FSM when force-cancelling an orphaned grace, but never the"
            " door/window diagnostic FSM, which also reads grace_active"
        ),
        "scope_covered": (
            "coordinator.py: _check_orphaned_grace() now also calls"
            " self._evaluate_door_window_fsm('_check_orphaned_grace',"
            " event_kind=DoorWindowFsmEventKind.GRACE_TIMER_EXPIRED), mirroring the"
            " existing override/grace FSM call in the same function. Diagnostic-only"
            " — _evaluate_door_window_fsm() never writes back to production."
        ),
    },
    677: {
        "version_fixed": "0.6.36",
        "title": (
            "Restart mid-RF-timer wiped CA's memory of an active QuietCool remote"
            " timer session, causing a spurious 3-hour manual grace lockout when the"
            " hardware timer naturally elapsed hours later"
        ),
        "scope_covered": (
            "coordinator.py: new _read_live_remote_timer_provenance(), called from"
            " _do_startup_coalesce() before reconcile_fan_on_startup(). Reads the"
            " configured fan_remote_entity's live HA state (which re-announces its"
            " last retained event_type after a restart), parses the timer token +"
            " press timestamp via the existing fan_status.parse_remote_timer_event(),"
            " and computes the token's own natural expiry — no new persisted CA state."
            " automation.py: reconcile_fan_on_startup()/_reconcile_fan_on_startup_locked()"
            " gained a new optional remote_timer_provenance parameter; when present and"
            " unexpired and the fan is still physically running, calls the existing"
            " handle_fan_manual_override(duration_override=remaining_seconds,"
            " is_remote_event=True, remote_timer_hours=token_hours) to re-arm a grace"
            " period sized to the timer's actual remaining time. When provenance is"
            " None (the common case — no active timer, unconfigured, or already"
            " expired), behavior is byte-for-byte unchanged. The existing"
            " _timer_boundary_settle_until mechanism (Issue #530) then arms naturally"
            " when the re-armed grace expires, exactly as it would with no restart."
        ),
    },
    676: {
        "version_fixed": "0.6.35",
        "title": (
            "Door/window shadow FSM stuck at paused_idle when a grace-expiry re-pause"
            " check was preempted by nat-vent reactivation — production correctly"
            " resumed but the shadow FSM never received the transition"
        ),
        "scope_covered": (
            "automation.py: AutomationEngine._re_pause_for_open_sensor()'s nat-vent"
            " reactivation branch (_reactivates=True) now calls"
            " _resolve_door_window_pause_flags(kind=PAUSED_NAT_VENT_REACTIVATED, ...)"
            " and emits the dedicated 'nat_vent_reactivated_while_paused' event type,"
            " matching the structurally identical sibling branch already fixed in"
            " check_natural_vent_conditions() by Issues #647/#660/#668. This call site"
            " was the one remaining place emitting only the unrelated 'sensor_opened'"
            " event (kept unchanged for its own consumers), which is not wired to any"
            " door/window-FSM-clearing transition. Confirmed unrelated to the"
            " natvent_fsm_authoritative switch (Issue #594 Phase R) and to #672/#673's"
            " raw-flag-sync fix — this is a distinct event-wiring gap in a third call"
            " site, found via live overnight logs the night #672/#673 shipped."
        ),
    },
    673: {
        "version_fixed": "0.6.34",
        "title": (
            "Shadow-engine mirror comparison's periodic raw-copy safety net was missing 4"
            " nat-vent/door-window fields, so any missed or exception-interrupted"
            " _mirror_to_shadow() call site touching them caused a permanent,"
            " non-self-healing divergence"
        ),
        "scope_covered": (
            "coordinator.py: _sync_shadow_inputs() now raw-copies _natural_vent_active,"
            " _nat_vent_soft_start, _paused_by_door, and _nat_vent_outdoor_exit_time from"
            " the production engine to the shadow engine every cycle, extending the same"
            " precedent #613/#631 already established for outdoor temp/forecast/thermal"
            " model and grace/override state. _paused_by_door is read by both the"
            " nat-vent and door/window mirror derivations, so this closes both classes of"
            " shadow-engine disagreement in one change. Phase 3 audit (same issue): the"
            " test_shadow_engine_coverage.py registry already tracked all 4 fields and"
            " classified every mutating method mirrored/internal — confirmed still"
            " accurate, no gap found. nat_vent_fsm.py's 6 unused NatVentFsmEventKind"
            " members (DOOR_PAUSE_STARTED, DOOR_PAUSE_ENDED, GRACE_STARTED, GRACE_ENDED,"
            " OVERRIDE_CONFIRMED, OVERRIDE_CLEARED) confirmed safe as documented: only"
            " TICK is ever fed from a real call site, and transition() never branches on"
            " event.kind (grepped — it's only ever recorded into the result), so feeding"
            " TICK for any of those triggers produces an identical transition."
        ),
    },
    672: {
        "version_fixed": "0.6.33",
        "title": (
            "Door/window, nat-vent, and override/grace shadow-FSM diagnostics each had a"
            " different, previously-unexamined reason for staying permanently stuck"
            " out of sync with production"
        ),
        "scope_covered": (
            "door_window_fsm.py: _sync_reconcile_next_state() never checked grace_active"
            " from a NORMAL origin, so a grace period started for a reason unrelated to any"
            " door/window pause (override grace, fan-off grace) was invisible forever"
            " (production=grace fsm=normal, confirmed live for 90+ minutes 2026-08-17)."
            " nat_vent_fsm.py: _transition_from_active() only checked the thermal/comfort"
            " exit chain, never a door-pause/reactivation-lockout condition, so a wrongly-"
            " active FSM had no path back once stuck (confirmed live for 34+ minutes across"
            " 2 full startup-coalesce cycles). override_grace_fsm.py: added"
            " UNPROTECTED_GRACE_STARTED — automation.py's _start_grace_period() (the shared"
            " wrapper for fan-off/window-close/nat-vent-exit/drift-correction grace starts)"
            " now routes through _resolve_override_grace_fsm_state() instead of calling"
            " _legacy_set_grace_flags() directly, closing the one gap those 4 triggers all"
            " shared. All three fixes proven inert on real decisions today: door/window's"
            " SYNC_RECONCILE and nat-vent's new check can never reach the live authoritative"
            " path (confirmed structurally); override/grace's fix was proven equivalent"
            " to the legacy path across the full golden+pending scenario corpus with"
            " _override_grace_fsm_authoritative=True (same rigor #664's original cutover"
            " used) before merging, since it does extend what that switch would drive if"
            " ever flipped."
        ),
    },
    670: {
        "version_fixed": "0.6.32",
        "title": (
            "Regular-cycle nat-vent/economizer checks fired during the startup-coalescing"
            " window, before startup reconciliation ran (same bug class as #627)"
        ),
        "scope_covered": (
            "coordinator.py: the regular _async_update_data() cycle's"
            " check_natural_vent_conditions() and check_window_cooling_opportunity() calls"
            " had no _startup_coalesce_active gate, unlike every sibling override-detection"
            " check in this file. HA-restart-triggered extra coordinator refreshes (fan-"
            " state listener churn) gave the ungated nat-vent check multiple chances to"
            " activate the whole-house fan for real before _do_startup_coalesce()'s"
            " reconcile_fan_on_startup() — the single-shot startup-reconciliation mechanism"
            " (#321/#327) — had run, so reconciliation's own decision arrived minutes late"
            " and against a fan state it never actually chose (traced live via HA log"
            " timestamps, 2026-08-17: fan activated at t+66s post-restart, reconcile ran at"
            " t+5min and read back the self-caused state). This is the same gap #627 fixed"
            " for the backstop_30min untracked-fan reconcile, in two call sites #627 didn't"
            " cover. Extracted _should_run_regular_cycle_nat_vent_check() and"
            " _should_run_regular_cycle_window_cooling_check() gating both behind the"
            " existing shared _suppress_during_startup_coalescing() helper."
        ),
    },
    668: {
        "version_fixed": "0.6.31",
        "title": (
            "Door/window shadow FSM unconditionally reset by check_natural_vent_conditions()"
            " re-check (4th occurrence of #613/#647/#666, opposite failure mode)"
        ),
        "scope_covered": (
            "coordinator.py: _DOOR_WINDOW_FSM_EVENT_KINDS['check_natural_vent_conditions']"
            " = 'paused_nat_vent_reactivated' was a method-name-keyed, UNCONDITIONAL"
            " trigger (added by #660 Step 3) — every call to check_natural_vent_conditions()"
            " fired DoorWindowFsmEventKind.PAUSED_NAT_VENT_REACTIVATED regardless of which"
            " internal branch it took, and door_window_fsm.py's _transition_from_paused()"
            " handles that kind unconditionally by landing on NORMAL. Production only"
            " actually reactivates nat-vent while paused in 2 deeply conditional branches"
            " (automation.py's activate-fan and soft-start branches); every other cycle"
            " with a door left open and no imminent reactivation wrongly reset the shadow"
            " FSM to NORMAL moments after apply_classification()'s SYNC_RECONCILE dispatch"
            " correctly restored PAUSED_IDLE — a permanent live disagreement surviving"
            " deploys and restarts (traced live via HA log timestamps, 2026-08-17;"
            " occupant impact: none — production's own pause state was always correct;"
            " this is shadow-diagnostic-only, unrelated to and independent of #666's"
            " harness fix earlier the same day). Fix: emit an explicit"
            " nat_vent_reactivated_while_paused event at the 2 real branch call sites,"
            " removed the unconditional method-name mapping, added an event-type-keyed"
            " feed instead (matching nat-vent's own 6-exit-event convention) so the shadow"
            " FSM only reacts when a reactivation genuinely happened. New renderer added"
            " to ai_skills_context.py's EVENT_RENDERERS for the new event type (Activity"
            " Report coverage guardrail, Issue #330). Regression tests in"
            " tests/test_shadow_fsm_harness_event_coverage.py cover both directions (no"
            " reactivation leaves the FSM alone; a real reactivation still clears it) plus"
            " a positive control reproducing the old unconditional trigger; the now-stale"
            " assertion in tests/test_door_window_fsm_shadow_wiring.py that pinned the old"
            " unconditional behavior as correct was rewritten to assert the opposite."
        ),
    },
    666: {
        "version_fixed": "0.6.30",
        "title": "Test harness silently broke event-driven shadow-FSM feed coverage (3rd occurrence of #613/#647)",
        "scope_covered": (
            "tools/sim_harness/build_coordinator.py: fixed build_headless_coordinator()"
            " pointing automation_engine._emit_event_callback at a bare local"
            " event_log-appending function instead of the real coordinator._emit_event()"
            " — the only place _feed_lifecycle_fsms_from_event() is called in production."
            " This silently defeated event-driven FSM-feed coverage (nat-vent, door/window,"
            " override/grace) for every coordinator-level Tier A test, including"
            " test_shadow_engine_live.py, which is why #647 (merged the day before this"
            " investigation, explicitly about this bug class) could ship green and still"
            " leave live production logging chronic 'Nat-vent FSM disagreement (#633)'/"
            " 'Door/window FSM disagreement (#637)' WARNINGs (traced live to 2026-08-15"
            " 06:30:43, occupant impact: none — production's own HVAC pause/nat-vent state"
            " was always correct; this is shadow-diagnostic-only). Fix: wrap the real"
            " coordinator._emit_event once and point the engine callback at the wrapped"
            " version, mirroring production's own wiring exactly, so the flat scenario"
            " event_log and the real FSM-feed side effect both fire from one call."
            " automation.py: also fixed a real, independently-confirmed sibling gap found"
            " during the investigation — check_natural_vent_conditions()'s"
            " NatVentExitReason.CEILING_THRESHOLD exit branch called _exit_nat_vent()"
            " without an event_type= kwarg (missed by #649's project-wide rollout,"
            " add1b8f), silently starving both shadow FSMs whenever nat-vent exits that"
            " specific way. New tests/test_shadow_fsm_harness_event_coverage.py replays the"
            " real incident sequence (sensor open before any fresh-open event, nat-vent"
            " hard-floor exit) against the real coordinator and proves both the fix works"
            " and — via a positive control reproducing the harness bug — that the test is"
            " actually load-bearing, confirmed via an explicit revert test (git-stashing"
            " the harness fix reproduces the exact live disagreement; restoring it passes)."
        ),
    },
    660: {
        "version_fixed": "0.6.27",
        "title": "Door/window FSM: full authority for all 8 real trigger sites (completes #637)",
        "scope_covered": (
            "door_window_fsm.py/automation.py/coordinator.py: completes the door/window"
            " pause/grace lifecycle FSM migration begun in #637. Fixed 2 real gaps found"
            " during the investigation: (1) _sync_reconcile_next_state() never checked live"
            " grace_active when the FSM was already in a PAUSED_* state (added a"
            " grace_active-while-paused branch, and made _transition_from_paused() dispatch"
            " SYNC_RECONCILE to it, matching NORMAL/GRACE's existing dispatch); (2)"
            " _transition_from_paused()'s ALL_SENSORS_CLOSED branch inferred pre_pause_mode"
            " truthiness from current_state == PAUSED_ACTIVE (a placeholder) instead of the"
            " real AutomationEngine._pre_pause_mode value, which could disagree in a"
            " reachable edge case involving a still-legacy _exit_nat_vent() branch. Also"
            " found and added the FSM's real 8th trigger site,"
            " check_natural_vent_conditions()'s two reactivation-while-paused branches,"
            " previously fed to the nat-vent FSM but never the door/window FSM at all"
            " (new DoorWindowFsmEventKind.PAUSED_NAT_VENT_REACTIVATED). Structural"
            " consolidation (Step 0, provable no-op): deleted a fully-redundant"
            " coordinator-side FSM-inputs builder in favor of automation.py's single"
            " definition; added AutomationEngine._resolve_door_window_pause_flags(), one"
            " shared authoritative-vs-legacy dispatcher all 8 sites now call instead of a"
            " hand-copied if/else each; collapsed _mirror_to_shadow()'s and"
            " _feed_lifecycle_fsms_from_event()'s repeated try/except FSM-dispatch blocks"
            " into one declarative loop. Split _pause_for_door_window() into an action half"
            " (_pause_for_door_window_action()) and a flags half, letting"
            " handle_door_window_open()/_re_pause_for_open_sensor() run the action"
            " unconditionally while deriving flags under their own event kind (the highest-"
            " risk step: _re_pause_for_open_sensor() now trusts the flags"
            " _on_grace_expired() already applied via an explicitly captured pre-grace-"
            " clear origin state, rather than independently re-deciding and re-writing"
            " them). Exposed door/window's shadow-diagnostic fields on the Shadow Engine"
            " Status sensor (previously computed but never surfaced). The"
            " _doorwindow_fsm_authoritative switch (config/switch.py, not touched by this"
            " PR) still defaults False — zero occupant-visible behavior change from this"
            " release alone; a live switch-flip verification is a separate follow-up step."
            " Two callers of the shared _pause_for_door_window() wrapper"
            " (_apply_comfort_band()'s door/window guard,"
            " _sync_paused_by_door_with_live_sensors()'s Issue #620 direct-pause path) were"
            " not among the 8 sites the originating investigation enumerated, but"
            " automatically gained FSM-authoritative capability through that one shared"
            " wrapper rather than needing individual treatment — noted here since it's a"
            " correction to the investigation's own site count, not a new gap."
        ),
    },
    664: {
        "version_fixed": "0.6.29",
        "title": "Override/grace FSM: full authoritative migration (switch, no partial-scope staging)",
        "scope_covered": (
            "override_grace_fsm.py/override_grace_lifecycle.py/automation.py/coordinator.py/"
            "switch.py/const.py: adds override_grace_fsm_authoritative (default OFF, not"
            " persisted across restart, same convention as natvent_fsm_authoritative/"
            " doorwindow_fsm_authoritative), governing _override_confirm_pending/"
            " _grace_active/_grace_protects_override for all 8 real OverrideGraceFsmEventKind"
            " call sites in one increment — full authority shipped directly rather than"
            " door/window's staged partial-scope rollout, because investigation proved every"
            " flag value these primitives compute is a pure function of their own call"
            " arguments (never of prior engine state), so the FSM and the legacy inline"
            " computation are provably equivalent at every site (confirmed by a"
            " corpus-wide decision-equivalence comparator, switch flipped True, across all"
            " 81 golden + 7 pending scenarios). Split the 4 timer/flag-owning primitives"
            " (_start_grace_period(), _cancel_grace_timers(), start_override_confirmation(),"
            " clear_manual_override()) into an action half (real async_call_later"
            " scheduling + non-derived bookkeeping, always runs unconditionally — timer"
            " ownership never transfers to the FSM, same rule door/window's own switch"
            " already established) and a flags half (dispatcher-owned, genuinely mutually"
            " exclusive between the FSM and legacy computation — an earlier draft called"
            " both unconditionally, which would have made the switch behaviorally inert;"
            " fixed before landing). Found and fixed 2 real correctness gaps the shadow-only"
            " phase's own tests never exercised: (1) override_grace_fsm.py's landing"
            " branches (_land_after_detection, DASHBOARD_RESUME, FAN_OVERRIDE_DETECTED)"
            " never checked whether manual grace is disabled via config"
            " (manual_grace_seconds=0), which would have made an authoritative FSM claim"
            " grace_active=True with no real timer behind it — a stuck-forever phantom"
            " grace, a worse bug class than #661; (2) coordinator.py's 'new_override_during_"
            " grace' Fix D branch was mis-modeled as OVERRIDE_CANCELLED (Issue #647,"
            " shadow-only) when its real production behavior never touches grace at all"
            " (Issue #282's 'Fix D' deliberately leaves the still-running grace protecting"
            " the new override about to be redetected) — re-classified as OVERRIDE_SUPERSEDED,"
            " the previously-unreachable 8th event kind, whose own transition already"
            " correctly preserves grace. Also fixed 2 pre-existing DRY violations found"
            " during investigation: GRACE_TRIGGERS_PROTECTING_OVERRIDE and"
            " OVERRIDE_ADOPT_SETPOINT_TOLERANCE_F were each hand-duplicated (override_grace_"
            "start.py/override_match.py vs automation.py) with no import connecting the"
            " copies — consolidated into single const.py definitions. Default OFF — zero"
            " occupant-visible behavior change from this release alone; a live switch-flip"
            " verification is a separate follow-up step, same as door/window's and"
            " nat-vent's own switches."
        ),
    },
    661: {
        "version_fixed": "0.6.28",
        "title": "Override/grace shadow FSM: fan-override path incorrectly modeled a confirm delay",
        "scope_covered": (
            "override_grace_fsm.py/coordinator.py: handle_fan_manual_override() was mapped"
            " to the same OVERRIDE_DETECTED event kind as the two genuinely"
            " confirm-delay-eligible thermostat-override call sites"
            " (handle_manual_override(), handle_manual_override_during_pause()), causing"
            " _land_after_detection() to model a PENDING confirmation for fan overrides"
            " that production never creates (handle_fan_manual_override() sets"
            " _fan_override_active and starts a protecting grace directly and"
            " unconditionally, never touching start_override_confirmation()/"
            " decide_override_confirm() at all). Added a dedicated FAN_OVERRIDE_DETECTED"
            " event kind, short-circuited at the top of transition() to"
            " (IDLE, ACTIVE_PROTECTING_OVERRIDE) before any state-based dispatch — keeps"
            " _land_after_detection()'s thermostat-only confirm-delay logic completely"
            " untouched. Exposed override/grace's shadow-diagnostic fields on the Shadow"
            " Engine Status sensor (previously computed but never surfaced, same gap #660"
            " fixed for door/window). No authoritative switch exists for override/grace —"
            " zero occupant-visible behavior change; this is a diagnostic-accuracy fix"
            " only, closing the live disagreement observed 2026-08-16"
            " (production=idle/active_protecting_override, fsm=pending/none) on a QuietCool"
            " RF remote timer press."
        ),
    },
    651: {
        "version_fixed": "0.6.21",
        "title": "Override/grace shadow FSM: handle_manual_override entry gap + bedtime/wakeup exit gap",
        "scope_covered": (
            "coordinator.py: two remaining gaps in the override/grace lifecycle FSM diagnostic"
            " (#613/#633/#637/#639, most recently #643/#647), same root-cause class (FSM"
            " event-coverage narrower than the real production state-mutation surface)."
            " Gap 1: handle_manual_override() (the thermostat-level override path, distinct"
            " from handle_fan_manual_override which #643 already wired) was never mirrored to"
            " the shadow FSM at any of its 3 real coordinator.py call sites"
            " (_async_thermostat_changed's new-override-during-grace, mode-changed-outside-"
            "pause, and setpoint-only branches) — added to _OVERRIDE_GRACE_FSM_EVENT_KINDS"
            " and _mirror_to_shadow('handle_manual_override', ...) calls at all 3 sites,"
            " mapping to the same OVERRIDE_DETECTED kind #643 used for the fan path. Gap 2:"
            " handle_bedtime()/handle_morning_wakeup() can silently clear a fan-only override"
            " via clear_manual_override() with no adjacent event emission (the emit is gated"
            " on _manual_override_active, false for a fan-only override) — this was not"
            " permanently stuck (the pre-existing _check_orphaned_grace() self-heal, run every"
            " ~30s cycle, catches it as an orphaned grace within one cycle since"
            " 'fan_manual_override' is in _GRACE_TRIGGERS_PROTECTING_OVERRIDE) but produced a"
            " transient shadow-disagreement blip until then. Fixed via a before/after"
            " _any_override_active() diff around both _async_bedtime()/_async_morning_wakeup()"
            " calls, feeding _feed_override_grace_fsm_cancelled() immediately on a detected"
            " clear rather than waiting on the backstop — coordinator.py only, no automation.py"
            " changes, avoids duplicating either handler's own gate logic."
            " tests/test_shadow_engine_coverage.py gained a new TestPerCallerFsmFeedCoverage"
            " class asserting per-call-site (not just per-event-kind) reachability, the"
            " specific gap in test coverage that let #643 ship asymmetric wiring undetected."
            " Zero production HVAC impact: shadow engine/FSM state reaches nothing but the"
            " Shadow Engine Status diagnostic sensor."
        ),
    },
    647: {
        "version_fixed": "0.6.19",
        "title": "Shadow-engine/FSM diagnostics (#613/#633/#637/#639) disagreed with production on nearly every cycle",
        "scope_covered": (
            "coordinator.py: the three lifecycle FSM diagnostics (nat-vent #633, door/window"
            " #637, override/grace #639) each carry their own tracked state across calls, but"
            " the 'which production call site re-evaluates the FSM' coverage was far narrower"
            " than the actual set of production functions mutating the tracked fields. Root"
            " cause traced via live logs + code, not guessed: (1) override/grace — #643 wired"
            " the entry event (handle_fan_manual_override -> OVERRIDE_DETECTED) but zero exit"
            " events (confirm/self-resolve/cancel/grace-expiry) had any trigger at all, so the"
            " very first real fan override after #643 shipped pushed the FSM into 'pending'"
            " permanently; (2) door/window — _exit_nat_vent()'s sensor-still-open branch"
            " (reached via the already-mirrored check_natural_vent_conditions) could set"
            " _paused_by_door=True with no door/window FSM re-evaluation; (3) nat-vent — audited"
            " all 10 production call paths mutating _natural_vent_active/_nat_vent_soft_start;"
            " 9 were already mirrored to the shadow engine but only 1 (check_natural_vent_"
            " conditions) re-ran the FSM. Fix decouples 'feed the FSM' from 'mirror to shadow':"
            " _evaluate_override_grace_fsm() now takes an explicit OverrideGraceFsmEventKind"
            " instead of deriving one from a mirror method name; automation.py's existing"
            " _emit_event_callback stream (already fired at nearly every real transition) now"
            " also feeds the FSMs via _feed_lifecycle_fsms_from_event(), reusing named events"
            " that were verified emitted AFTER their state mutation completes (override_"
            " cleared was deliberately excluded from this hook — it fires BEFORE the clear, so"
            " cancel_override()/clear_manual_override()'s ~4 real coordinator.py/api.py call"
            " sites instead call _feed_override_grace_fsm_cancelled() directly, post-return)."
            " door_window_fsm.py's pre-existing but never-wired NAT_VENT_EXITED_SENSOR_STILL_"
            "OPEN event kind is now fired, gated on a live any_monitored_sensor_open() read"
            " (not the not-yet-updated _paused_by_door flag) to avoid forcing an incorrect"
            " pause on a clean nat-vent exit. nat-vent's trigger set widened to also include"
            " reconcile_fan_on_startup/on_fan_turned_off (both share the same decide_nat_vent_"
            "gate() gate the FSM already models, unlike apply_classification's periodic/"
            "incidental trigger, which remains deliberately excluded). Zero production HVAC"
            " impact: the shadow engine's dry_run is permanently True and none of this FSM"
            " state is ever written back to a decision path — confirmed by inspection, not"
            " assumption. New regression coverage:"
            " tests/test_shadow_engine_coverage.py::TestOverrideGraceFsmEventCoverage (an"
            " AST/regex registry asserting every OverrideGraceFsmEventKind member is fed from"
            " a real coordinator.py call site — the specific check that would have caught"
            " #643's asymmetric entry-only wiring)."
        ),
    },
    645: {
        "version_fixed": "0.6.18",
        "title": "Sensor reconnect blip during restart made _sensor_debounce_pending() bypass the open-window guard",
        "scope_covered": (
            "coordinator.py: _async_door_window_changed() now inspects old_state when a"
            " monitored sensor reads open — if old_state.state is 'unavailable'/'unknown' (a"
            " reconnect, not a genuine off->on transition), it records that specific"
            " last_changed timestamp in the new _sensor_reconnect_blip_last_changed dict and"
            " skips debounce-timer registration entirely (old_state=None is deliberately NOT"
            " treated the same way — only unavailable/unknown are confirmed blip signatures)."
            " _sensor_debounce_pending() excludes any last_changed value recorded as a known"
            " blip for that sensor; a LATER genuine off->on transition moves last_changed"
            " again so this can never mask a real open. Root cause: a group/helper"
            " contact-sensor entity blips unavailable->on during HA startup (confirmed via"
            " live REST state history around a redeploy), stamping a fresh last_changed on an"
            " already-hours-open window — automation.py's existing arm-blocking guards"
            " (_apply_comfort_band()'s Issue #629 choke-point, _sync_paused_by_door_with_live_"
            "sensors()) were unmodified; they simply now read a correct debounce_pending"
            " signal. An earlier draft of this fix blanket-removed the debounce check from"
            " those guards instead, which broke the golden scenario"
            " issue_623_debounce_race_transient_open_not_paused (debounce's legitimate"
            " nuisance-pause protection for a genuinely brief door-open) — reverted in favor"
            " of this narrower, signal-level fix. Also hardened the sim harness's"
            " simulate_restart (Issue #627) to explicitly reset _paused_by_door/"
            "_pre_pause_mode/_natural_vent_active, matching the real clean-slate guarantee a"
            " fresh engine instance provides in production — the harness reuses the same live"
            " engine instance, so those fields were silently leaking across simulated"
            " restarts and would have masked this fix's own regression scenario. New"
            " regression coverage: tests/test_sensor_reconnect_blip.py and"
            " tools/simulations/pending/issue_645_restart_debounce_bypass_open_window.json."
        ),
    },
    643: {
        "version_fixed": "0.6.17",
        "title": "Override/grace shadow FSM never saw handle_fan_manual_override (Block 5 diagnostic gap)",
        "scope_covered": (
            "coordinator.py: added await self._mirror_to_shadow('handle_fan_manual_override', ...)"
            " at all 3 production call sites (_async_thermostat_changed, _async_fan_entity_changed,"
            " _flush_fan_remote_burst) and added 'handle_fan_manual_override': 'override_detected' to"
            " _OVERRIDE_GRACE_FSM_EVENT_KINDS. The override_grace_fsm.py OVERRIDE_DETECTED transition"
            " was already fully implemented and input-driven generically from live automation_engine"
            " state (Issue #639) — no new pure module, event kind, or transition logic was needed,"
            " only the mirror wiring. tests/test_shadow_engine_coverage.py's registry reclassified"
            " handle_fan_manual_override from 'exempted' to 'mirrored'. Shadow-only, dry_run=True,"
            " zero production behavior change — same isolation posture as the rest of Block 5 (#594)."
            " Root-caused from a live 06:39:08 WARNING-level disagreement on both the door/window"
            " (#637) and override/grace (#639) shadow FSMs that never self-corrected."
        ),
    },
    641: {
        "version_fixed": "0.6.16",
        "title": (
            "WHF rapid on/off cycling — proactive-floor/ceiling-threshold exits didn't arm the reactivation lockout"
        ),
        "scope_covered": (
            "automation.py: PROACTIVE_FLOOR and CEILING_THRESHOLD exits inside"
            " check_natural_vent_conditions(), plus fan_thermostat_check()'s STOP_DEACTIVATE"
            " branch (found during the same audit — its own comment already called it 'the"
            " exact same boundary condition' as its already-armed sibling"
            " STOP_VIA_NAT_VENT_EXIT) — all three now pass set_outdoor_exit_time=True to"
            " _exit_nat_vent(), arming the existing 300s NAT_VENT_REACTIVATION_LOCKOUT_S the"
            " same way the original outdoor-rise exit always has. Root cause: PROACTIVE_FLOOR"
            " is a predictive time-to-floor check independent of the instant reactivation"
            " gate it hands off into when a monitored sensor is still open — indoor/outdoor"
            " barely move tick-to-tick, so the instant gate is almost always still satisfied"
            " immediately after the predictive exit fires, guaranteeing reactivation next"
            " tick and repeating. New tests/test_nat_vent_exit_lockout_coverage.py AST-scans"
            " every _exit_nat_vent() call site (11 total, including on_fan_turned_off, found"
            " during the audit) and requires each to be classified 'arms lockout' /"
            " 'exempted: <reason>', with a positive control checking the classification"
            " against the actual set_outdoor_exit_time argument — a new call site added later"
            " without a classification fails immediately, preventing this bug class from"
            " silently recurring a fourth time. Separately, as defense-in-depth independent"
            " of root cause: new AutomationEngine._fan_toggle_rate_limited() hard-floors any"
            " CA-issued fan reversal to no faster than FAN_MIN_TOGGLE_INTERVAL_S (300s,"
            " const.py) inside _activate_fan()/_deactivate_fan(), logging a WARNING and"
            " raising a new proactive fan_rapid_cycling incident class"
            " (docs/incident-classes.md) on suppression; surfaced on the WHF status-tab card"
            " via a '(rate-limited Xs ago)' suffix. Deliberately compares against a new,"
            " separate _fan_toggle_command_time field rather than the pre-existing"
            " _fan_command_time echo-tracking field (Issue #482) — found during testing that"
            " _reconcile_fan_physical_drift()'s corrective control-entity-sync command"
            " legitimately stamps the latter immediately before an intentional same-tick"
            " recycle-on, which the shared field would have wrongly rate-limited."
            " desired_state.py's decide_fan_cycle_on()/decide_fan_cycle_off() (the"
            " fan_min_runtime_per_hour feature) now floor their computed on/off phase"
            " durations at the same 300s minimum so a configured value outside ~[5, 55]"
            " minutes can't schedule its own off/on command inside the rate-limit window."
        ),
    },
    649: {
        "version_fixed": "0.6.20",
        "title": "WHF rate-limit reporting was misleading and mis-framed as an incident (follow-up to #641)",
        "scope_covered": (
            "automation.py: _activate_fan()/_deactivate_fan() now return a FanCommandResult"
            " (EXECUTED / ALREADY_IN_STATE / RATE_LIMITED_NEW / RATE_LIMITED_DUP / OVERRIDDEN /"
            " DISABLED) instead of None, so callers can tell whether a fan command actually"
            " executed. _exit_nat_vent() gained event_type/event_payload kwargs and now"
            " centralizes emission for its 7 call sites (check_natural_vent_conditions()'s"
            " PROACTIVE_FLOOR/OUTDOOR_RISE/CEILING_THRESHOLD-adjacent branches,"
            " fan_thermostat_check()'s STOP_VIA_NAT_VENT_EXIT/STOP_DEACTIVATE/STOP_COOLED_TO_FLOOR,"
            " and nat_vent_temperature_check()'s hard-floor exit) — the caller's own"
            " nat_vent_predicted_floor_exit/nat_vent_comfort_floor_exit/nat_vent_outdoor_rise_exit/"
            " fan_deactivated event now fires with an accurate fan_mode_change field ('on→auto'"
            " when the toggle executed, a 'deferred (5-min floor, applies HH:MM:SS)' description"
            " when newly rate-limited) instead of unconditionally claiming a state transition"
            " that may not have happened. _fan_toggle_rate_limited() gained a dedup guard"
            " (_fan_rate_limited_direction + comparing against the already-armed"
            " _fan_rate_limited_until) so a repeat block within the same deferral window —"
            " either two decision paths racing in one tick, or fan_thermostat_check() re-"
            " deciding on every retry tick while still blocked — logs at DEBUG and reports"
            " nothing new, instead of an unbounded WARNING/incident per tick. The"
            " incident_detected/fan_rapid_cycling emission (added in #641) was removed entirely"
            " and its docs/incident-classes.md rows deleted — a blocked-and-deferred toggle is"
            " the #641 floor working correctly, not an anomaly; the first block in a window now"
            " logs at INFO (was WARNING). When a deferred toggle finally executes,"
            " _activate_fan()/_deactivate_fan() log a new INFO '5-minute floor expired — applying"
            " deferred ...' line ahead of the pre-existing (unchanged, WARNING) 'Activated/"
            " Deactivated fan' line, so the completion is visible without inflating its severity."
            " ai_skills_context.py: _render_fan_activated()/_render_fan_deactivated() now honor an"
            " optional fan_mode_change payload override (falling back to the historical"
            " 'device: off->on'/'on->off' when absent) — needed because those two renderers,"
            " unlike the nat_vent_* renderers, previously hardcoded the state-transition text"
            " regardless of payload; _render_nat_vent_outdoor_rise_exit()/"
            " _render_nat_vent_away_ceiling_exit() also extended to surface fan_mode_change when"
            " present, matching the pattern the comfort-floor/predicted-floor renderers already"
            " used. coordinator.py: _whf_rate_limit_suffix() reworded from '(rate-limited Xs ago)'"
            " to '(<on/off> pending — 5-min floor, applies at HH:MM:SS)', naming the pending"
            " direction (via the new _fan_rate_limited_direction field) and the exact clock time"
            " the deferred toggle will apply. No change to the #641 floor/lockout mechanism itself."
        ),
    },
    639: {
        "version_fixed": "0.6.15",
        "title": (
            "Block 5 epic #594 Phase 3 (final phase): builds the unified override/grace"
            " transition table (7 new pure functions + override_grace_fsm.py assembly),"
            " following the pattern #633/#637 established. Modeled as two small composed"
            " states (OverrideConfirmState x GraceState), not one flat enum — investigated"
            " and confirmed a flat enum would misrepresent reachability, since grace"
            " outlives or runs independently of override in most transitions. New golden"
            " scenario (override_second_override_during_grace) confirms Issue #282's"
            " second-override-during-grace supersession branch, previously untested by any"
            " scenario. Investigated and ruled out a hypothesized live race between the"
            " supersession branch and the orphaned-grace watchdog (Issue #508) — confirmed"
            " both calls are synchronous with no await between them, so no interleaving is"
            " possible. Wired into the shadow engine's existing diagnostic as a third"
            " comparison point, purely observational — never wired into any decision path"
            " that can act. v1 wiring is narrower than door/window's: only 2 of 7 event"
            " kinds (MANUAL_OVERRIDE_DURING_PAUSE, DASHBOARD_RESUME) have an existing"
            " _mirror_to_shadow() call site to key off; the rest await a future phase if"
            " ever needed."
        ),
        "scope_covered": (
            "New: override_grace_lifecycle.py (2-piece state derivation), "
            "override_confirm_split.py, override_match.py, override_grace_start.py, "
            "override_supersession.py, override_cancel_outcome.py, "
            "override_orphaned_grace.py, override_grace_fsm.py (assembly). Coordinator: "
            "_evaluate_override_grace_fsm(), extended _update_shadow_engine_diagnostic() "
            "with override_grace_production_state/override_grace_shadow_state/"
            "override_grace_fsm_state/override_grace_mirror_agrees/override_grace_fsm_agrees. "
            "9 new test files (124 tests). Golden: override_second_override_during_grace."
        ),
    },
    637: {
        "version_fixed": "0.6.26",
        "title": (
            "Block 5 epic #594 Phase 2: builds the unified door/window pause/grace"
            " transition table (5 new pure functions + door_window_fsm.py assembly),"
            " following the exact pattern #633's nat-vent FSM established. Discovered"
            " and confirmed-by-scenario (not just static trace) that "
            "`_paused_by_door`/`_grace_active` are NOT mutually exclusive in"
            " production — modeled as a new PAUSED_DURING_GRACE state rather than"
            " assumed unreachable. Wired into the shadow engine's existing diagnostic"
            " as a third comparison point, purely observational — never wired into any"
            " decision path that can act."
            " Phase R Step 1 (0.6.23): reading the FSM's own docstring against production"
            " surfaced 3 `_paused_by_door` inconsistencies that must be resolved before the"
            " lifecycle can become FSM-authoritative. Fixed the narrowest, unambiguous one:"
            " `_re_pause_for_open_sensor()`'s nat-vent-reactivation branch never cleared"
            " `_paused_by_door`, unlike the identical branch in"
            " `check_natural_vent_conditions()`. The other two — `_exit_nat_vent()`'s"
            " unconditional sensor-still-open pause (investigated, verified benign, not a"
            " bug: the flag is accurate when set and `_on_grace_expired()` already"
            " re-derives from live sensor state rather than reading it) and"
            " `handle_door_window_open()`'s grace-fallthrough (real gap, five-whys traced"
            " to Issue #87, no live evidence found of it firing, split to its own"
            " independent issue #655 per owner steer, not blocking this migration) — are"
            " resolved. Step 1 for door/window is closed."
            " Phase R Step 1b (0.6.24): before Step 2's read-authority swap, the shadow"
            " FSM had to be proven correct against all 7 `DoorWindowFsmEventKind` members,"
            " not just 4 — `GRACE_TIMER_EXPIRED`/`DASHBOARD_RESUME`/`SYNC_RECONCILE` were"
            " explicitly deferred as future work when Phase 2 shipped. All 3 now wired:"
            " `dashboard_resume` reuses `resume_from_pause()`'s existing mirror call site;"
            " `grace_timer_expired` keys off `_on_grace_expired()`'s own emitted event"
            " types (`grace_expired`/`override_adopted`) via the existing #647 event-driven"
            " hook; `sync_reconcile` (no natural emitted event exists for it) uses"
            " method-name-triggered dispatch instead, the same mechanism"
            " `_NAT_VENT_FSM_TRIGGER_METHODS` already established for nat-vent's own"
            " non-event-shaped triggers — 2 new `_mirror_to_shadow()` call sites"
            " (`handle_pre_cool`, `handle_morning_wakeup`) needed adding first. One"
            ' accepted residual: `_on_grace_expired()`\'s "within planned window" branch'
            " emits no event at all, so that one grace-expiry outcome still doesn't feed"
            " the FSM — documented in door_window_fsm.py's docstring, not silently"
            " missed."
            " Phase R Step 2 (0.6.25, PARTIAL authority): the nat-vent precedent"
            " (`_natvent_fsm_authoritative`) swapped one boolean decision point."
            " Door/window's full scope — deriving all 9 pause/grace flags across 7"
            " methods from FSM state — turned out qualitatively bigger and not equally"
            " safe by default. Per-method mapping initially found 3 mechanical/unblocked"
            " methods and 4 blocked ones (`handle_door_window_open`: #655's gap; "
            "`_re_pause_for_open_sensor`: needs origin-state plumbing it doesn't have;"
            ' `_on_grace_expired`: its unfed "within planned window" branch;'
            " `_exit_nat_vent`'s sensor-still-open branch: write-shape divergence)."
            " `handle_all_doors_windows_closed()` looked mechanical too but was found"
            " during implementation to have its own gap: `door_window_fsm.py`'s"
            " `ALL_SENSORS_CLOSED` transition hardcodes `pre_pause_mode` from state"
            " rather than taking the real value as input — unsafe to trust for real"
            " flag-derivation given `_exit_nat_vent()` can leave `_paused_with_hvac_"
            "already_off` stale. Shipped: `_doorwindow_fsm_authoritative` flag (off by"
            " default) governs ONLY `handle_manual_override_during_pause`/"
            "`resume_from_pause` — both land unconditionally on their target state"
            " regardless of origin state, confirmed by direct code read, no"
            " placeholder-inference risk. Also found (independent of this migration,"
            " filed as #657): `_re_pause_for_open_sensor()`'s 0.6.23 fix only cleared"
            " `_paused_by_door`, not the 3 other stale pause fields the mirrored branch"
            " in `check_natural_vent_conditions()` clears together."
            " Phase R Step 3 (0.6.26): closed all 4 of Step 2's documented blockers except"
            " one. #655 fixed: `handle_door_window_open()`'s grace-active branch now shares"
            " the same real `_nat_vent_may_reactivate()` gate result the nat-vent-vs-pause"
            " decision further down the function already computes, instead of a coarse"
            " outdoor-only proxy that could disagree with it -- closing the exact gap that"
            " let a grace period be silently defeated. `door_window_open_response.py`'s"
            " pure mirror updated to match. #657 fixed: `_re_pause_for_open_sensor()`'s"
            " nat-vent-reactivation branch now clears `_paused_with_hvac_already_off`/"
            "`_paused_entity`/`_paused_since` alongside `_paused_by_door`, matching"
            " `check_natural_vent_conditions()`'s sibling branch. `_exit_nat_vent()`'s"
            " sensor-still-open branch write-shape divergence fixed too (found while"
            " scoping Step 3, no separate issue filed): it used to write only"
            " `_paused_by_door`/`_pre_pause_mode`; both this branch and"
            " `_pause_for_door_window()` now go through a new shared"
            " `_set_door_window_pause_fields()` helper. This also closes the previously"
            "-documented `ALL_SENSORS_CLOSED`/`pre_pause_mode`-placeholder risk in"
            " `door_window_fsm.py`, since `_paused_with_hvac_already_off` can no longer"
            " go stale from this branch -- so `handle_all_doors_windows_closed()` needed"
            " no separate fix of its own, contrary to Step 2's assumption that it would."
            " Also closed Step 1b's one documented shadow-feed residual:"
            ' `_on_grace_expired()`\'s "within planned window" branch now emits'
            ' `"grace_expired"` (reusing the existing event type with a distinguishing'
            " `within_planned_window` payload key), so all 3 of its outcome branches feed"
            " `GRACE_TIMER_EXPIRED`, not just 2. Required updating one LOCKED golden"
            " scenario (`issue_637_paused_during_grace_open_fallthrough.json`) with"
            " explicit user sign-off per the Golden Simulation Test Policy -- it had"
            " deliberately encoded #655's bug as its expected final outcome"
            " (`paused_during_grace`); re-signed to assert the fixed behavior"
            " (`resumed`, i.e. grace correctly holds). Still blocked, deferred to a future"
            " increment: `_re_pause_for_open_sensor()` still needs to know whether it was"
            " reached from `GRACE` or `PAUSED_DURING_GRACE` (a signature/plumbing change,"
            " not a body swap) before it can join FSM authority."
        ),
        "scope_covered": (
            "New: door_window_lifecycle.py (5-state derivation), door_window_pause_entry.py, "
            "door_window_open_response.py, door_window_close_response.py, "
            "door_window_grace_expiry.py, door_window_fsm.py (assembly). Coordinator: "
            "_evaluate_door_window_fsm()/_current_hvac_mode(), extended "
            "_update_shadow_engine_diagnostic() with door_window_production_state/"
            "door_window_shadow_state/door_window_fsm_state/door_window_mirror_agrees/"
            "door_window_fsm_agrees, wired into _mirror_to_shadow() for "
            "handle_door_window_open/handle_all_doors_windows_closed/"
            "handle_manual_override_during_pause (the 3 mirrored methods with an "
            "unambiguous door/window FSM event-kind correspondence). New scenarios "
            "issue_637_paused_during_grace_open_fallthrough.json and "
            "issue_637_paused_during_grace_nat_vent_exit.json confirm PAUSED_DURING_GRACE "
            "is reachable via 2 structurally distinct production paths — both landed "
            "directly in golden/ (this text previously said 'pending user review before "
            "promotion to golden/', which was stale/aspirational and never updated after "
            "that promotion actually happened; corrected during Phase R Step 2 scoping)."
            " 0.6.23: automation.py `_re_pause_for_open_sensor()` now clears `_paused_by_door`"
            " in its nat-vent-reactivation branch; new test"
            " test_resume_from_pause.py::TestGraceExpiryRecheck::"
            "test_repause_clears_paused_by_door_on_nat_vent_reactivation."
            " 0.6.24: coordinator.py — `_DOOR_WINDOW_FSM_EVENT_KINDS` gains"
            " `resume_from_pause`/`dashboard_resume`; new"
            " `_DOOR_WINDOW_GRACE_EXPIRY_EVENT_TYPES` frozenset + branch in"
            " `_feed_lifecycle_fsms_from_event()`; new"
            " `_DOOR_WINDOW_SYNC_RECONCILE_TRIGGER_METHODS` frozenset + branch in"
            " `_mirror_to_shadow()`'s finally block; `_evaluate_door_window_fsm()` now"
            " accepts an optional explicit `event_kind`; 2 new `_mirror_to_shadow()` call"
            " sites in `_async_morning_wakeup()`/`_async_pre_cool_trigger()`. New"
            " `_DOOR_WINDOW_EVENT_KIND_REGISTRY` + `TestDoorWindowFsmEventCoverage` in"
            " test_shadow_engine_coverage.py enforces all 7 event kinds stay reachable."
            " 0.6.26: automation.py `handle_door_window_open()`'s grace-active branch now"
            " calls `_nat_vent_may_reactivate()` directly instead of a coarse outdoor-only"
            " proxy (#655); `door_window_open_response.py`'s `decide_door_open_response()`"
            " updated to match. `_re_pause_for_open_sensor()`'s nat-vent-reactivation branch"
            " clears 4 pause fields, not 1 (#657). New `_set_door_window_pause_fields()`"
            " helper shared by `_pause_for_door_window()` and `_exit_nat_vent()`. New event"
            ' emit in `_on_grace_expired()`\'s "within planned window" branch. Updated'
            " golden `issue_637_paused_during_grace_open_fallthrough.json` (re-signed)."
            " New/extended tests: test_door_window.py"
            "::TestGracePeriodExpiry::test_door_open_during_grace_coarse_outdoor_ok_real_gate_fails_still_blocked,"
            " test_resume_from_pause.py"
            "::TestGraceExpiryRecheck::test_repause_clears_paused_by_door_on_nat_vent_reactivation"
            " (extended), test_nat_vent_activation.py"
            "::TestNatVentOutdoorRiseExit::test_outdoor_rises_above_indoor_exits (extended),"
            " test_grace_refresh_and_band_call.py"
            "::TestGraceExpiryTriggersRefreshCallback::test_planned_window_path_calls_refresh_callback"
            " (extended), test_door_window_pure_modules.py (grace-suppression tests"
            " updated to real-gate semantics), test_door_window_fsm.py::TestFromGrace"
            " (updated to match)."
        ),
    },
    655: {
        "version_fixed": "0.6.26",
        "title": (
            "A door/window briefly reopened during an active grace period could still"
            " pause HVAC, defeating the grace period's purpose. Root cause:"
            " `handle_door_window_open()`'s grace-active branch used a coarse"
            " outdoor-only proxy check to decide whether to suppress the pause, while a"
            " stricter, real 4-variable reactivation gate (`_nat_vent_may_reactivate()`)"
            " was computed independently a few lines later in the same function — the"
            " two could disagree, letting a 'looks cool enough' outdoor reading fall"
            " through the grace suppression only to still land on a pause when the real"
            " gate failed. Fixed by making the grace-active branch share the same real"
            " gate result the fallthrough already computes, instead of a separate proxy."
        ),
        "scope_covered": (
            "automation.py `handle_door_window_open()`: coarse proxy removed, grace-"
            "active branch now calls `_nat_vent_may_reactivate()` directly. "
            "door_window_open_response.py `decide_door_open_response()`: updated to"
            " test `nat_vent_gate_entered` instead of a separate outdoor/threshold"
            " comparison. Golden `issue_637_paused_during_grace_open_fallthrough.json`"
            " re-signed — its final assertion changed from `paused_during_grace` (the"
            " bug, confirmed with explicit user sign-off) to `resumed` (the fix)."
        ),
    },
    657: {
        "version_fixed": "0.6.26",
        "title": (
            "After a grace period ended with a door/window still open but conditions"
            " now favoring natural ventilation, `_re_pause_for_open_sensor()`'s 0.6.23"
            " fix (issue #637 Step 1) cleared only `_paused_by_door`, leaving"
            " `_paused_with_hvac_already_off`/`_paused_entity`/`_paused_since` stale from"
            " the earlier pause — unlike the structurally identical branch in"
            " `check_natural_vent_conditions()`, which clears all 4 fields together."
            " `_paused_entity`/`_paused_since` only feed diagnostic text (a stale entity"
            " name and blank elapsed-minutes in the Activity Report's 'Settings' cell);"
            " `_paused_with_hvac_already_off` feeds real control flow"
            " (`derive_door_window_lifecycle_state()`'s `PAUSED_ACTIVE`/`PAUSED_IDLE`"
            " derivation)."
        ),
        "scope_covered": (
            "automation.py `_re_pause_for_open_sensor()`'s nat-vent-reactivation branch"
            " now clears all 4 fields, matching `check_natural_vent_conditions()`."
            " Extended test_resume_from_pause.py::TestGraceExpiryRecheck::"
            "test_repause_clears_paused_by_door_on_nat_vent_reactivation to seed stale"
            " values and assert all 4 are cleared."
        ),
    },
    633: {
        "version_fixed": "0.6.22",
        "title": (
            "Block 5 epic #594 Phase P completion (0.6.12-0.6.13), then Phase R prep"
            " (0.6.22): builds the unified nat-vent transition table earlier Phase P"
            " sub-issues (#606/#607, #608/#609) deliberately left unbuilt, plus a generic"
            " cross-lifecycle event dispatcher for coordinating between the automation's"
            " independently-migrated behaviors going forward, then wires the transition"
            " table to actually run live (0.6.13) as a third, independent comparison point"
            " alongside the existing production/shadow mirror. Purely observational through"
            " 0.6.13. 0.6.22 begins the actual cutover work: models the transition table's"
            " last documented gap (soft-start mid-session escalation) and adds an opt-in,"
            " off-by-default switch letting the FSM's decision drive real HVAC/fan calls —"
            " the first point in this epic capable of that, proven behavior-identical to"
            " the legacy path before shipping."
        ),
        "scope_covered": (
            "0.6.12: New: lifecycle_events.py (cross-lifecycle event vocabulary), "
            "lifecycle_dispatcher.py (generic event routing + registry-completeness "
            "check, 12 tests), nat_vent_fsm.py (unifies derive_nat_vent_lifecycle_state()/"
            "decide_nat_vent_gate()/decide_nat_vent_soft_start_gate()/decide_nat_vent_exit() "
            "into one (state, event) -> Transition function, 26 direct tests + differential "
            "validation against all golden/pending scenarios). tools/sim_harness/run_production.py: "
            "engine_state snapshot extended with outdoor temp/peak/sample-count and a "
            "live indoor-temp read, needed for the differential validation. "
            "0.6.13: coordinator.py's new _evaluate_nat_vent_fsm() runs nat_vent_fsm.transition() "
            "against production's real live readings, triggered only from check_natural_vent_conditions's "
            "existing shadow mirror (the one mirrored method that unambiguously corresponds to "
            "nat-vent's own gate/exit re-evaluation — deliberately not apply_classification/"
            "reconcile_fan_on_startup's mirrors, neither of which runs the gate/exit chain in "
            "production). The FSM's tracked state is never written onto either engine. Surfaced "
            "via the existing ClimateAdvisorShadowEngineStatusSensor's nat_vent_fsm_state attribute, "
            "folded into its overall agree/disagree value. 10 new coordinator-level tests + 2 new "
            "sensor tests. "
            "0.6.22: nat_vent_fsm.py's _transition_from_active() now re-checks decide_nat_vent_gate() "
            "while ACTIVE_SOFT_START, mirroring automation.py's own soft-start-upgrade block (Issue #540) "
            "exactly. AutomationEngine._natvent_fsm_authoritative (default False, automation.py): when set, "
            "check_natural_vent_conditions()'s soft-start-escalation read routes through "
            "nat_vent_fsm.transition() instead of a hand-duplicated inline copy — the exit-chain decision "
            "already called decide_nat_vent_exit() directly, so nothing changed there. New "
            "tools/sim_harness/nat_vent_fsm_authoritative_compare.py + "
            "tests/test_nat_vent_fsm_authoritative_compare.py (90 tests): full golden+pending corpus "
            "decision-equivalence proof (entire event_log/action_log diff, not a state label) — found and "
            "fixed a latent false-positive bug in tools/sim_harness/differential.py's action-log "
            "canonicalization (a per-call random Context id was being compared as decision content). New "
            "switch.climate_advisor_nat_vent_fsm_authoritative (switch.py) + "
            "ClimateAdvisorCoordinator.set_natvent_fsm_authoritative()/natvent_fsm_authoritative "
            "(coordinator.py), off by default, deliberately not persisted across restart. Surfaced on "
            "ClimateAdvisorShadowEngineStatusSensor's existing attributes (sensor.py), not a new card. "
            "4 new coordinator-plumbing tests."
        ),
    },
    631: {
        "version_fixed": "0.6.11",
        "title": (
            "Live incident 2026-08-12 21:02-23:40 (2h38m): shadow_automation_engine (Issue"
            " #613/#615's diagnostic-only parallel engine, dry_run=True, never touches real"
            " hardware) disagreed with production continuously for the full length of an"
            " active manual-override grace period. Root cause: _grace_active,"
            " _manual_override_active, _fan_override_active, and their companion"
            " mode/source/time/duration fields are set either by AutomationEngine methods"
            " called directly from coordinator.py/api.py (never followed by a"
            " _mirror_to_shadow(...) call) or by purely internal async_call_later timers"
            " with no coordinator call site at all (_on_grace_expired, the"
            " _confirm_override_expired timer's clear path). check_natural_vent_conditions()"
            " — a method that IS mirrored — gates nat-vent reactivation on"
            " 'not self._grace_active', so the shadow kept deciding to reactivate nat-vent"
            " every cycle while production correctly stayed suppressed. This is a second,"
            " independent instance of the #615 input-parity gap class, in a field category"
            " (#615's own coverage-registry audit) that audit's scope excluded by"
            " construction."
        ),
        "scope_covered": (
            "coordinator.py: _sync_shadow_inputs() extended with a raw-copy of the 7 grace/"
            "override lifecycle-gate fields plus their 11 companion content fields (mode,"
            " source, time, duration, remote-timer hours/speed, grace end time/trigger)."
            " Deliberately NOT adding new _mirror_to_shadow(...) call sites for the 4"
            " setter chains (handle_fan_manual_override, handle_manual_override,"
            " clear_manual_override, cancel_override) — that would reintroduce the"
            " 'duplicate each write at its call site' pattern _sync_shadow_inputs() exists"
            " to eliminate, and would start real async_call_later timers against the shared"
            " hass event loop on the shadow engine for no benefit over a raw copy refreshed"
            " every mirrored cycle. tests/test_shadow_engine_coverage.py: _TRACKED_FIELDS"
            " extended to the 7 gate fields; _COVERAGE_REGISTRY gained 10 new entries, all"
            " classified 'exempted' or 'internal' with the specific reason each setter isn't"
            " mirrored (restore_state was also newly added as 'mirrored' — it was already"
            " mirrored in code but missing from the registry). tests/test_shadow_engine_live.py:"
            " new TestSyncShadowInputsGraceOverride class with per-field parity tests and a"
            " positive control reproducing the live incident."
        ),
    },
    629: {
        "version_fixed": "0.6.10",
        "title": (
            "Live incident 2026-08-13: at 06:13:44 the user turned off the whole-house fan."
            " A coordinator refresh fired apply_classification() 9ms later; its routine"
            " comfort-band arm called _set_temperature(mode='cool') 14ms after that,"
            " silently committing the thermostat to Cool mode even though a monitored window"
            " had been open since bedtime. _apply_comfort_band() — the single write point all"
            " 7 comfort-band callers funnel through — never independently re-checked live"
            " window state; it trusted _paused_by_door, which was still False because it was"
            " correct at the moment it was last checked (the thermostat genuinely was still"
            " off then). select_comfort_band()'s edge-selection ternary deliberately treats an"
            " 'off'-day classification the same as 'cool' for the Issue #249 'lazy comfort"
            " band' safety net, so nothing stopped the write. Separately, _set_temperature()'s"
            " log line never named the hvac_mode it silently bundled into the"
            " climate.set_temperature call (Issue #301's single-setpoint architecture), which"
            " is why the mode change was invisible in the logs even after the fact."
        ),
        "scope_covered": (
            "automation.py: new structural choke-point guard directly inside"
            " _apply_comfort_band() — mirrors the pre-existing WHF/AC mutex choke-point in"
            " _set_hvac_mode() (Issue #392 Fix 1b) — refuses to arm an active mode whenever a"
            " monitored sensor is genuinely (debounce-settled) open, reusing the existing"
            " _pause_for_door_window() machinery. Exempted while nat-vent/WHF genuinely owns"
            " HVAC, covering handle_occupancy_away()/handle_occupancy_vacation()'s legitimate"
            " wide setback-band arm during an active nat-vent session (verified against"
            " away_natvent_exits_at_comfort_ceiling/away_with_active_natvent_transition/"
            " bedtime_natvent_continuation golden scenarios). Because the guard lives in the"
            " single _apply_comfort_band() choke point, it covers all 7 call sites"
            " (apply_classification, handle_bedtime, handle_morning_wakeup, handle_pre_cool,"
            " handle_occupancy_away, handle_occupancy_vacation, the post-fan-off reassert"
            " path) — not deferred for any of them. _set_temperature()'s log line now includes"
            " mode=. tools/sim_harness/outcomes.py: hvac_mode_never_commanded now also scans"
            " set_temperature calls (not just set_hvac_mode), with an optional 'since' scope."
            " New golden scenario issue_629_comfort_band_arm_through_open_window"
            " (revert-tested)."
        ),
    },
    627: {
        "version_fixed": "0.6.9",
        "title": (
            "Live incident 2026-08-11: restore_state()'s Issue #263/#327 restart clean-slate"
            " wipes AutomationEngine._fan_override_active but correctly preserves"
            " _pre_fan_hvac_mode (the flag _whf_owns_hvac() depends on). The coordinator's"
            " periodic backstop_30min 'untracked fan' reconcile used only"
            " _fan_override_active as its gate — not _startup_coalesce_active, unlike every"
            " sibling override-detection check in coordinator.py — so it fired on the very"
            " first post-restart update cycle, before the 300s startup-coalescing window had"
            " any chance to settle. It misread a whole-house fan still legitimately running"
            " under a pre-restart RF-remote timer as unwarranted, turned it off, and released"
            " _pre_fan_hvac_mode via _deactivate_fan() — which let the next apply_classification()"
            " cycle commit the thermostat to Cool mode ~34 seconds later with nothing left to"
            " stop it. The premature correction also armed the 5-minute correction cooldown,"
            " silently suppressing the properly-designed _do_startup_coalesce() ->"
            " reconcile_fan_on_startup(trigger='ha_restart') call at the real 300s coalescing"
            " boundary from ever re-evaluating."
        ),
        "scope_covered": (
            "coordinator.py: new _should_run_untracked_fan_backstop() predicate method"
            " (extracted from the inline backstop_30min condition so it can be unit tested"
            " directly) adds `not self._startup_coalesce_active` to the existing"
            " _fan_override_active/_grace_active gate — the same idiom already used by every"
            " other override-detection check in this file. tools/sim_harness/run_production.py:"
            " new `simulate_restart` (drives the real engine.restore_state(engine."
            " get_serializable_state()) continuity boundary) and `fan_backstop_tick` (drives the"
            " real backstop_30min call site) scenario event types — neither existed before,"
            " closing a real harness coverage gap for this call site."
        ),
    },
    625: {
        "version_fixed": "0.6.8",
        "title": (
            "Regression from Issue #620/PR #621: _compute_automation_status()'s grace-active"
            " branch appended the full free-text AutomationEngine._last_action_reason sentence"
            " to the Status card. For whole-house-fan-triggered grace periods that duplicated"
            " what the Fan (WHF) card's _compute_whf_status() already said (e.g. Status:"
            " 'whole-house fan manually turned on — suppressing HVAC to prevent AC/fan"
            " fighting' vs. the Fan card's 'running (manual override)' + 'remote timer: 12h"
            " (ends 10:09 PM)'), producing a long duplicated wall of text. For a manual"
            " thermostat override (_confirm_override(), e.g. the user turns off cooling at"
            " the wall unit) _last_action_reason was never populated at all —"
            " _confirm_override() doesn't call _record_action() — so the Status card showed"
            " a blank or a stale reason left over from some unrelated earlier action."
        ),
        "scope_covered": (
            "automation.py: new AutomationEngine._last_grace_trigger field, set from the"
            " trigger= argument already passed to _start_grace_period() (previously used only"
            " for logging/event-payload correlation), cleared alongside _last_resume_source in"
            " _cancel_grace_timers() and the restart/reset path. coordinator.py: new module-level"
            " _GRACE_TRIGGER_LABELS dict mapping known trigger strings to short (2-3 word) cause"
            " labels; _compute_automation_status()'s grace branch now uses this lookup instead"
            " of _last_action_reason, falling back to no cause segment for unmapped/unknown"
            " triggers rather than leaking a raw internal string onto the UI."
            " _format_grace_remaining() reworked from a 'N min left' countdown to an applied"
            " duration + end time (e.g. '12h (ends 10:09 PM)'), matching the Fan (WHF) card's"
            " own 'remote timer: Xh (ends HH:MM)' structural style."
        ),
    },
    623: {
        "version_fixed": "0.6.7",
        "title": (
            "Regression from Issue #620/PR #621's _sync_paused_by_door_with_live_sensors()."
            " Its debounce guard, _sensor_debounce_pending, only meant 'is a timer currently"
            " registered in _door_open_timers' — that timer is populated exclusively inside"
            " _async_door_window_changed(), the state_changed event listener. But raw"
            " sensor reads (_any_monitored_sensor_open()) reflect an open transition"
            " immediately, independent of when the event loop schedules that listener."
            " A concurrently-running, unrelated coordinator refresh cycle could reach"
            " apply_classification() before the listener's turn, observe 'open, no timer"
            " registered' and misread a just-opened, still-transient sensor as settled —"
            " bypassing the debounce window entirely. Confirmed live 2026-08-11: a user"
            " exiting through a door got an instant pause notification; the log showed the"
            " pause fire 5ms before 'debounce started' logged for the same transition."
        ),
        "scope_covered": (
            "coordinator.py: _sensor_debounce_pending is now a bound method (was an inline"
            " lambda at both the production and shadow-engine callback-bundle construction"
            " sites) that also checks each open monitored sensor's HA-authoritative"
            " state.last_changed timestamp against CONF_SENSOR_DEBOUNCE, in addition to the"
            " existing _door_open_timers registry check — immune to listener scheduling"
            " order, so it only ever widens when the guard returns 'pending', never narrows"
            " it. Fixes both existing consumers (_sync_paused_by_door_with_live_sensors()"
            " and _idle_open) via the single shared callback — no new call sites."
            " tools/sim_harness/fake_hass.py: FakeState gained a last_changed field (None"
            " by default — 'not tracked', matching a seeded/long-settled sensor);"
            " _FakeStates.async_set() now stamps it from the sim clock only on a genuine"
            " value transition; set_simple() accepts an optional explicit last_changed for"
            " scenario seeding. tools/sim_harness/run_production.py: new seed_fresh flag on"
            " sensor_open events models a sensor whose raw state just changed but whose"
            " debounce timer hasn't registered yet (the exact race). 1 new pending scenario"
            " (issue_623_debounce_race_transient_open_not_paused), revert-tested against the"
            " pre-fix lambda to confirm it fails without this change. Explicitly out of"
            " scope, tracked as a follow-up: automation.py's grace-expiry re-pause check"
            " (line ~4349, pre-existing from Issue #561) reads raw sensor state with no"
            " debounce check and could share this race class, but its exposure window"
            " (sensor opening at the exact instant an existing grace timer expires) is much"
            " lower-likelihood than this issue's any-refresh-races-any-fresh-open shape."
        ),
    },
    620: {
        "version_fixed": "0.6.6",
        "title": (
            "Three independent gaps, all confirmed in a live 2026-08-11 incident where"
            " HVAC was set to cool with a monitored window open. (1)"
            " check_natural_vent_conditions()'s _idle_open widening (Issue #244/#402/#504)"
            " never checked _grace_active, unlike the Issue #134 comfort-ceiling exception"
            " beside it — a user manually turning the WHF off could see it reactivated"
            " within 5 seconds if outdoor stayed favorable, directly violating"
            " docs/grace-periods-spec.md's documented fan-off-grace guarantee. (2)"
            " fan_thermostat_check()'s STOP_DEACTIVATE and STOP_COOLED_TO_FLOOR outcomes"
            " were never migrated to _exit_nat_vent() when Issue #418 fixed their sibling"
            " STOP_VIA_NAT_VENT_EXIT for the identical bug (restore_hvac=True default with"
            " no live sensor check) — a comfort-floor exit could silently restore an"
            " active mode into an open window. (3, the deepest gap) decide_scheduled_band_gate()'s"
            " paused_by_door input (Issue #498) reflected only event history"
            " (handle_door_window_open()/_exit_nat_vent()), never live sensor state — a"
            " sensor open since before either of those paths ever ran left the flag False"
            " forever, so the routine apply_classification()/handle_bedtime()/"
            "handle_morning_wakeup()/handle_pre_cool() comfort-restore write had zero live"
            " check at all."
        ),
        "scope_covered": (
            "automation.py: _idle_open gained 'and not self._grace_active'. "
            "fan_thermostat_check()'s two unmigrated stop outcomes now route through"
            " _exit_nat_vent(), with event emission made conditional on whether a genuine"
            " nat-vent session was active (both outcomes can also fire for a non-nat-vent"
            " CA fan, e.g. min-runtime cycling) to avoid mislabeling that case's Activity"
            " Record entry. New _sensor_debounce_pending property (extracted from"
            " _idle_open's own inline pattern — second consumer of the existing"
            " _sensor_debounce_pending_callback, not a new debounce mechanism) and new"
            " _sync_paused_by_door_with_live_sensors() helper, called at the top of all 4"
            " decide_scheduled_band_gate() callers, reusing the existing"
            " _pause_for_door_window() (Issue #523). coordinator.py:"
            " _compute_automation_status()'s grace branch now appends"
            " _format_grace_remaining() (Issue #498, previously wired only into the"
            " Next-Action cards and orphaned there by Issue #527's Status Card Ontology"
            " cleanup) and _last_action_reason — both already correctly shown on the"
            " Debug tab, now also on the Status card. tools/sim_harness/outcomes.py:"
            " two new assertion types (fan_not_active, paused_by_door), same pattern as"
            " the existing nat_vent_still_active/nat_vent_not_active. 2 new pending"
            " scenarios (both revert-tested to confirm they fail without their fix); a"
            " 3rd was attempted for fan_thermostat_check()'s tick-level path specifically"
            " but moved to unsupported/ after the harness proved unable to isolate it from"
            " a parallel, already-correct comfort-floor implementation"
            " (nat_vent_temperature_check()) — that fix's correctness rests on"
            " tests/test_fan_control.py's existing unit coverage instead, which caught a"
            " real event-mislabeling regression during this fix's own implementation."
        ),
    },
    618: {
        "version_fixed": "0.6.5",
        "title": (
            "A whole-house-fan/nat-vent session that ended while a monitored sensor was"
            " still open never cleared its _pre_fan_hvac_mode suppression snapshot"
            " (restore_hvac=False also skipped the release, by the pre-fix design)."
            " _whf_owns_hvac() kept reporting the WHF as still owning the thermostat for"
            " as long as the pause lasted, silently deferring apply_classification()'s"
            " HVAC-mode restore even after the window closed — confirmed in a live"
            " incident to leave HVAC un-managed for ~4.5 hours on a 95F day. Separately,"
            " reconcile_fan_on_startup()'s 'no-fan' branch could not distinguish a"
            " normal post-compressor cooling->fan hvac_action transition from a real"
            " unowned fan appearance, so it blindly replayed the same stale snapshot and"
            " force-cancelled AC that had just started legitimately cooling 5 minutes"
            " earlier. A third, related symptom (a mode change already matching live"
            " classification being logged/notified as 'Manual override detected') was"
            " investigated and found to be correct existing behavior (Issue #269 Bug C:"
            " comparing against _last_commanded_hvac_mode, not classification.hvac_mode,"
            " is required for dual-setpoint heat_cool detection) — not changed."
        ),
        "scope_covered": (
            "_deactivate_fan() gained a release_suppression parameter, decoupled from"
            " restore_hvac: genuine session-end callers (_exit_nat_vent()'s sensor-open"
            " branch, reconcile's no-fan branch) now always release _pre_fan_hvac_mode"
            " even when they can't write a mode into an open window; mid-session"
            " cycling-off (nat_vent_temperature_check()) keeps the prior"
            " restore_hvac-tracking default, since that stranding is intentional."
            " reconcile_fan_on_startup()/_reconcile_fan_on_startup_locked() gained a"
            " recent_hvac_session_ended parameter, set True at the hvac_action=='fan'"
            " coordinator listener when the transition came directly from"
            " cooling/heating — blocks the restore-mode write without blocking the"
            " suppression release. Two pre-existing shadow-engine mirroring gaps found"
            " and closed while auditing all 4 reconcile_fan_on_startup() call sites"
            " (thermostat_state_change and post_grace_expiry sites had no"
            " _mirror_to_shadow call). New stranded_hvac_suppression_restored event"
            " (automation.py) + EVENT_RENDERERS entry (ai_skills_context.py) makes the"
            " previously-invisible stranded-restore action visible in the Activity"
            " Record. tools/sim_harness/outcomes.py mapped the new event type to the"
            " existing 'resumed' outcome (purely additive, same pattern as"
            " whf_hvac_suppressed/whf_hvac_released). 6 new regression tests in"
            " tests/test_fan_control.py covering release-without-restore, the"
            " compressor-stomp guard, and the new event emission."
        ),
    },
    615: {
        "version_fixed": "0.6.4",
        "title": (
            "#613 (v0.6.3) shipped the live shadow engine mirroring only 5 of 13 real"
            " nat-vent entry points and never feeding it 3 input-data attributes"
            " (outdoor temp, forecast, thermal model) — a live incident within hours"
            " of deploy showed the shadow stuck 'inactive' vs production's"
            " 'active_full_gate' after a brief post-restart agreement. A systematic"
            " AST-based audit (not another spot-check) found 9 total gaps, not the"
            " 1-2 initially suspected."
        ),
        "scope_covered": (
            "New coordinator._sync_shadow_inputs(): single audited copy of"
            " _last_outdoor_temp/_hourly_forecast_temps/_thermal_model/"
            "_outdoor_temp_today_peak/_sample_count/_occupancy_mode from production to"
            " shadow, called unconditionally at the top of every _mirror_to_shadow()"
            " invocation. 8 additional decision methods mirrored: fan_thermostat_check()"
            " (Issue #608's own finding: usually the function that actually exits a"
            " session on the dominant dispatch path), on_fan_turned_off() (sync method"
            " — _mirror_to_shadow() extended to await only if the result is awaitable),"
            " reconcile_fan_on_startup() (both call sites; the ha_restart one is the"
            " exact live-incident reproduction), handle_bedtime(),"
            " handle_manual_override_during_pause(), resume_from_pause() (mirrored from"
            " api.py's ClimateAdvisorResumeFromPauseView — the one decision method"
            " triggered from the REST API rather than the coordinator), plus the 2"
            " previously-unmirrored apply_classification() call sites (briefing"
            " generation, post-WHF-release reassertion). shadow_automation_engine"
            ".restore_state() added at coordinator startup, now load-bearing since"
            " reconcile_fan_on_startup() reads the restored fan-activity hints."
            " Investigated and confirmed NOT separate gaps:"
            " _reconcile_fan_physical_drift() and _re_pause_for_open_sensor() are"
            " engine-internal self-scheduled timer callbacks that already run"
            " independently per instance once their trigger paths are mirrored."
            " New tests/test_shadow_engine_coverage.py: AST-scans automation.py for"
            " every method assigning one of the 4 tracked lifecycle fields, requires"
            " each to be in an explicit mirrored/internal/exempted registry — same"
            " enforcement shape as test_executor_offload.py's _BLOCKING_METHODS, so a"
            " future new entry point can't silently ship unmirrored. Blast radius"
            " confirmed zero for production HVAC safety throughout (isolation/dry_run"
            " never implicated) — harm was fully confined to the diagnostic sensor's"
            " accuracy. Full suite (4080 tests) + golden (74/74) + pending (4/4), zero"
            " regressions."
        ),
    },
    613: {
        "version_fixed": "0.6.3",
        "title": (
            "Block 5 (epic #594) sequencing item Q ('live shadow mode — a genuine"
            " second engine instance computing decisions from the same live"
            " inputs, fully inert per N2's redesign, with agreement/disagreement"
            " surfaced via a new diagnostic sensor') had no implementation."
            " Item O (#611/#612) proved N2's isolation held offline against 60"
            " golden/pending scenarios; this issue builds the real, live second"
            " engine instance inside the running coordinator."
        ),
        "scope_covered": (
            "coordinator.shadow_automation_engine (superseding N2's None"
            " placeholder): a real AutomationEngine, role='shadow',"
            " dry_run=True set immediately after construction and never"
            " toggled (no owner switch this phase — that's subtask R)."
            " New coordinator._build_shadow_automation_callbacks(): the 4"
            " callables N2 traced as reaching production (revisit,"
            " request_refresh, post_grace_fan_check, reclassify) are cut off"
            " structurally — revisit left None (a no-op lambda would crash,"
            " since _schedule_revisit() awaits it via async_create_task), the"
            " other 3 are no-op lambdas (safe: automation.py calls them"
            " synchronously, never via async_create_task); read-only callbacks"
            " (sensor_check, sensor_debounce_pending, get_fan_physical_state,"
            " is_recent_fan_command) are shared with production; emit_event is"
            " shadow-local (_on_shadow_emit_event, capped list, never the"
            " production event log). New coordinator._mirror_to_shadow():"
            " replays apply_classification/handle_door_window_open/"
            " handle_all_doors_windows_closed/check_natural_vent_conditions/"
            " nat_vent_temperature_check on the shadow engine immediately after"
            " each production call, with the same args; any shadow-side"
            " exception (including from the diagnostic recompute that follows"
            " it) is caught, logged at WARNING, and swallowed — never"
            " propagated. New coordinator._update_shadow_engine_diagnostic():"
            " compares derive_nat_vent_lifecycle_state() (Issue #606) between"
            " both engines against the real live clock. New"
            " ClimateAdvisorShadowEngineStatusSensor (sensor.py): a"
            " diagnostic-category entity (state agree/disagree/inactive,"
            " attributes carry both derived states + timestamp) — deliberately"
            " not wired into any occupant-facing Status-tab card (Issue #527"
            " ontology), zero HVAC impact. shadow_automation_engine.cleanup()"
            " added to coordinator.async_shutdown() (the shadow schedules real"
            " async_call_later timers directly, independent of dry_run)."
            " New tests/test_shadow_engine_live.py (18 tests) +"
            " tests/test_shadow_engine_sensor.py (6 tests): construction,"
            " callback isolation with a positive control reproducing the N2"
            " hazard, mirror exception/diagnostic isolation with positive"
            " controls, agreement/disagreement diagnostic with a positive"
            " control, shutdown cleanup, sensor state/attributes. Also added"
            " homeassistant.helpers.entity/EntityCategory to the test harness's"
            " HA stub layer (ha_stubs.py) — the first diagnostic-category"
            " entity in this codebase. Full suite (4066 tests) + golden"
            " (74/74) + pending (4/4), zero regressions. No production"
            " automation.py behavior changed for the real engine; two"
            " secondary apply_classification() call sites (once-daily"
            " briefing generation, post-WHF-release reassertion) are"
            " deliberately not mirrored — low-frequency, shadow re-syncs on"
            " the next regular cycle."
        ),
    },
    611: {
        "version_fixed": "0.6.2",
        "title": (
            "Block 5 (epic #594) sequencing item O ('offline validation, zero"
            " live risk — generalize the existing shadow-mode comparator from"
            " single-function to whole-engine comparison') had no implementation."
            " Item N2 (#604/#605) built the callback-isolation prerequisite"
            " (AutomationEngineCallbacks + role kwarg) after the epic flagged a"
            " HIGH-risk finding: a shadow engine's own construction-time callback"
            " wiring, done the old way, could reach back into the production"
            " engine and issue a real service call. This issue proves offline,"
            " before any live coordinator wiring, that N2's isolation actually"
            " holds."
        ),
        "scope_covered": (
            "New tools/sim_harness/shadow_engine_pair.py: run_shadow_pair_scenario()"
            " replays one scenario through three fully independent"
            " (engine, fake_hass, scheduler) stacks — a solo baseline, a paired"
            " production (role='production', dry_run=False), and a shadow"
            " (role='shadow', dry_run=True) — and checks (a) production's"
            " action_log matches baseline's exactly, (b) shadow's action_log is"
            " empty, (c) derive_nat_vent_lifecycle_state() agrees between"
            " production and shadow at scenario end. build_headless_engine()"
            " and run_production_scenario() extended with role/dry_run"
            " passthrough (engine-only mode only — backward-compatible, no"
            " existing caller's behavior changes). New"
            " tests/test_shadow_engine_pair.py: 60 offline-eligible golden +"
            " pending scenarios (18 use_coordinator scenarios are out of this"
            " offline harness's scope — coordinator-level shadow wiring is"
            " subtask Q's job) plus 3 positive controls proving each of the"
            " three checks actually catches what it claims to (forced dry_run"
            " bypass, forced lifecycle disagreement, forced production"
            " contamination). Found and fixed a real canonicalization gap along"
            " the way: differential.py's existing action-log diff falls back to"
            " repr() for a service call's context field (a real HA Context,"
            " carrying a random per-call UUID by design), producing 7/78"
            " false-positive divergences between two identical-code runs;"
            " shadow_engine_pair.py excludes that field from its own comparison."
            " Full suite (4042 tests) + golden (74/74) + pending (4/4), zero"
            " regressions. No coordinator or production automation.py behavior"
            " changed — purely additive test tooling."
        ),
    },
    608: {
        "version_fixed": "0.6.1",
        "title": (
            "check_natural_vent_conditions()'s 5-check priority-ordered exit chain"
            " (comfort-floor, away-ceiling, proactive/ODE floor, outdoor-rise,"
            " ceiling-threshold) was inline conditional logic with no unit-level"
            " coverage of its own, unlike the already-extracted entry gate"
            " (nat_vent_gate.py, Issue #411/#441). Block 5 (epic #594) Phase 2:"
            " extracted following the same proven methodology as #441."
        ),
        "scope_covered": (
            "New custom_components/climate_advisor/nat_vent_exit.py:"
            " NatVentExitReason enum, NatVentExitInputs/NatVentExitDecision"
            " dataclasses, pure decide_nat_vent_exit() — reuses the already-pure"
            " resolve_hard_exit_floor() from fan_thermostat_decision.py rather"
            " than re-deriving it. Swapped into"
            " check_natural_vent_conditions(): side effects (fan/HVAC calls,"
            " event emission, logging) unchanged, only the branching condition"
            " moved. New tests/test_nat_vent_exit.py (22 tests): all 5 exit"
            " reasons, priority order, boundary conditions, plus a revert-test"
            " (temporarily invert a condition, confirm matching tests fail,"
            " restore) proving the extraction is load-bearing. The swap-in"
            " itself surfaced and fixed one real latent bug: an"
            " UnboundLocalError on 'indoor' when the exit-chain block was"
            " reached with _natural_vent_active already False (the paused-"
            "reactivation-lockout code path a few lines later also reads"
            " 'indoor', previously computed unconditionally, now restored to"
            " being computed unconditionally). Full suite (3976 tests) + golden"
            " (74/74) + pending (4/4), zero regressions post-swap."
            " Also documented (docs/nat-vent-lifecycle-spec.md, 'Known"
            " Duplicate-Logic Race' section), NOT fixed in this issue: a"
            " golden-scenario-level positive control for this swap was"
            " attempted and found unreliable, because nat_vent_temperature_check()"
            " and fan_thermostat_check() (both dispatched before"
            " check_natural_vent_conditions() on the same trigger) independently"
            " implement equivalent comfort-floor and outdoor-rise stops and win"
            " the race for every golden scenario tried — the same duplicate-"
            "threshold-logic pattern already tracked for the entry gate"
            " (#400/#402), now confirmed on the exit side across three functions."
            " Consolidating them is flagged as follow-up work, not undertaken here."
        ),
    },
    606: {
        "version_fixed": "0.6.0",
        "title": (
            "Natural ventilation's session state (\"is it active, in soft-start"
            " purge/comfort mode, inactive, or locked out from immediately"
            ' reactivating after an outdoor-warm exit") had no single named'
            " representation anywhere in the codebase — it was always"
            " reconstructed ad hoc from a combination of 4 separate flags"
            " (_natural_vent_active, _nat_vent_soft_start, _paused_by_door,"
            " _nat_vent_outdoor_exit_time). Block 5 (epic #594) Phase 1: the"
            " first step of a multi-phase arc toward an explicit, verifiable"
            " state machine for this lifecycle."
        ),
        "scope_covered": (
            "New custom_components/climate_advisor/nat_vent_lifecycle.py:"
            " NatVentLifecycleState enum (INACTIVE, ACTIVE_FULL_GATE,"
            " ACTIVE_SOFT_START, PAUSED_REACTIVATION_LOCKOUT),"
            " NatVentLifecycleInputs dataclass, pure"
            " derive_nat_vent_lifecycle_state(). Read-only"
            " AutomationEngine.nat_vent_lifecycle_state property — additive"
            " only, not called from any production decision path (verified by"
            " grep). tools/sim_harness/run_production.py's"
            " _snapshot_engine_state() extended with 2 new fields"
            " (_nat_vent_soft_start, _nat_vent_outdoor_exit_time) to support"
            " replay-based verification. New tests/test_nat_vent_lifecycle_state.py"
            " (90 tests): direct unit coverage of the pure function including"
            " the reactivation-lockout boundary; a broad consistency check"
            " across the real final engine flags from every golden (74) +"
            " pending (4) scenario after a full production replay; and 3"
            " independently hand-reasoned ground-truth scenarios"
            " (mild_all_day_nat_vent_only -> ACTIVE_FULL_GATE,"
            " nat-vent-comfort-floor-exit-restores-heat -> INACTIVE,"
            " nat-vent-outdoor-rises-above-indoor-exit ->"
            " PAUSED_REACTIVATION_LOCKOUT). New Tier 3 doc"
            " docs/nat-vent-lifecycle-spec.md, with small additive"
            " cross-reference edits to docs/grace-periods-spec.md and"
            " docs/07-AUTOMATION-FLOWCHART.md at the one real handoff seam"
            " (_exit_nat_vent() forking into the pause or grace lifecycle)."
            " Corrected an inaccurate assumption from this session's earlier"
            " research: the comfort-floor exit inside"
            " check_natural_vent_conditions() does NOT route through"
            " _exit_nat_vent() — confirmed by direct code reading and by a"
            " golden scenario's own verdict text. Zero production decision"
            " logic changed; full suite (3954 tests) + golden (74/74) +"
            " pending (4/4) all pass with zero regressions."
        ),
    },
    604: {
        "version_fixed": "0.5.67",
        "title": (
            "AutomationEngine's 9 coordinator-wired callback attributes (revisit,"
            " sensor_check, sensor_debounce_pending, emit_event, request_refresh,"
            " post_grace_fan_check, get_fan_physical_state, is_recent_fan_command,"
            " reclassify) were always closures/bound methods over the single production"
            " coordinator instance, not parameterized by which engine instance invoked"
            " them. At least 4 (post_grace_fan_check, emit_event, request_refresh, and"
            " the request_refresh lambda) reach into real production state or trigger"
            " real side effects regardless of which engine fired them — unsafe for any"
            " future second (shadow) AutomationEngine instance (Block 5 / epic #594)."
            " AutomationEngine itself has no hidden shared state and was already safe to"
            " instantiate twice; only the coordinator's callback wiring was the hazard."
        ),
        "scope_covered": (
            "automation.py: new AutomationEngineCallbacks dataclass (9 named fields) and"
            " optional keyword-only callbacks/role constructor params on AutomationEngine"
            " — omitted (default) leaves all 9 attributes None exactly as before this"
            " change. coordinator.py: extracted the 9 existing post-construction"
            " assignments into _build_production_automation_callbacks(), passed at"
            " construction time; added a shadow_automation_engine=None placeholder"
            " attribute (stays None until Block 5 subtask Q builds a real second engine)."
            " docs/02-ARCHITECTURE-REFERENCE.md: new 'Engine Callback Isolation'"
            " subsection documenting the contract and naming the unsafe callables so a"
            " future shadow-engine implementer can't miss them. Pure construction-time"
            " refactor — zero behavior change for the existing single (production)"
            " engine, verified via full test suite (3864 tests) + golden (74/74) +"
            " pending (4/4) suites, all passing with zero regressions. Does not build"
            " the shadow engine itself, any state machine, or any comparator — those are"
            " separately scoped Block 5 subtasks P/O/Q/R."
        ),
    },
    602: {
        "version_fixed": "0.5.66",
        "title": (
            "self._today_record (coordinator.py) — which gates setpoint-only manual"
            " override detection, HVAC runtime tracking, comfort-violation minutes,"
            " occupancy-away minutes, door/window pause counts, and the thermal-learning"
            " watchdog — was created in exactly one place: _async_send_briefing(), a"
            " once-daily scheduled trigger with no retry, which bails out early whenever"
            " the weather entity has no forecast at that one fixed moment. A weather"
            " outage overlapping briefing_time silently blacked out all of the above for"
            " the rest of that calendar day, unlike the classification/comfort-band"
            " forecast dependency (Issue #588), which already retries every 30-minute"
            " regular cycle forever. Found while root-causing Issue #598 (a pending test"
            " scenario passing by coincidence because this exact gap silently disabled"
            " the override detection it was meant to exercise)."
        ),
        "scope_covered": (
            "coordinator.py: new _ensure_today_record(classification) method, extracting"
            " the existing DailyRecord creation/counter-preservation logic verbatim out"
            " of _async_send_briefing() (which now calls it instead of inlining"
            " creation). Also called from the regular classification cycle"
            " (_async_update_data_impl(), immediately after a successful classify_day())"
            " — the same self-healing, every-30-min path Issue #588 already proved"
            " retries forecast forever, so the gap shrinks from up to 24 hours to about"
            " 30 minutes. Idempotent: no-ops when a record for today already exists, so"
            " calling it every cycle cannot reset same-day accumulated counters (unlike"
            " the pre-existing once-daily rebuild, which only mattered once a day)."
            " tools/sim_harness/run_production.py: the harness's classification-event"
            " dispatch now mirrors the same real production hook. Issue #598's pending"
            " scenario (tools/simulations/pending/vacation_occupancy_override_cleared.json)"
            " now passes for real — needed one added reconfirmation classification event"
            " after the midnight day-rollover (the harness has no periodic auto-tick the"
            " way production does) and manual_grace_seconds raised from 3600 to 7200 (the"
            " original 1-hour grace was auto-expiring 11 minutes before the scenario's"
            " own explicit cancel_override event, once override detection actually"
            " started working — so the assertion was at risk of passing via grace-expiry"
            " instead of the cancel-override path it exists to test)."
        ),
    },
    600: {
        "version_fixed": "0.5.65",
        "title": (
            "reconcile_fan_on_startup()'s adopt-on branch had no guard against being"
            " re-entered by a second sequential trigger (of its 4 independent callers)"
            " while nat-vent was already CA-owned, producing 2-3 duplicate 'Fan"
            " activated' Activity Record entries for one real fan-on event, and"
            " silently resetting the displayed session start time on each redundant"
            " re-confirmation."
        ),
        "scope_covered": (
            "automation.py: _reconcile_fan_on_startup_locked()'s adopt-on branch"
            " (~line 3822) now checks self._natural_vent_active before mutating"
            " flags/recording/emitting — a redundant re-confirmation still refreshes"
            " _fan_active/_natural_vent_active/the thermo backstop timer, but returns"
            " before _record_action()/_emit_event_callback('fan_activated', ...)."
            " _fan_on_since is now only stamped on true first adoption"
            " (`if self._fan_on_since is None`), not on every re-entry. The sibling"
            " 'turn-off' branch already had its own cooldown (Issue #446) and was"
            " not touched."
        ),
    },
    593: {
        "version_fixed": "0.5.64",
        "title": (
            "Following the #584 investigation's full event-type audit (Block 4 of the"
            " plan), several remaining Activity Record event types either discarded"
            " payload data they already had, or omitted locally-computed inputs their"
            " emit sites could have passed — leaving the reasoning behind those specific"
            " decisions invisible even though the underlying data existed."
        ),
        "scope_covered": (
            "classifier.py: DayClassification gained applied_threshold_f/"
            "threshold_margin_f, computed in classify_day() from the final"
            " (post-hysteresis) day_type against the threshold that produced it."
            " automation.py: classification_applied payload now includes"
            " trend_magnitude/today_high/applied_threshold_f/threshold_margin_f;"
            " setpoint_rejected/setpoint_nudge now include reject_streak;"
            " handle_morning_wakeup()'s DEFER_OCCUPANCY branch now emits"
            " morning_wakeup_skipped (previously silent, unlike its DEFER_OVERRIDE/"
            "DEFER_PAUSED siblings and handle_bedtime()'s equivalent branch);"
            " pre_cool_suppressed_nat_vent's active_session branch (DEFER_NAT_VENT)"
            " now includes indoor/target — pre_cool_target is computed once, up front,"
            " before the gate checks, instead of after the branch that needed it."
            " coordinator.py: startup_coalesced now includes indoor_f/outdoor_f/"
            "fan_archetype; thermal_learning_no_observations now includes"
            " thermal_session_count. ai_skills_context.py: renderers for"
            " classification_applied, setpoint_rejected/setpoint_nudge,"
            " startup_coalesced, thermal_learning_no_observations,"
            " fan_untracked_cleared (now uses fan_device instead of a hardcoded"
            ' "fan: off"), incident_detected (now uses incident_id and the'
            " comfort-band comparison it already carried), morning_wakeup_skipped,"
            " and pre_cool_suppressed_nat_vent were all updated to surface the new/"
            "existing fields; nat_vent_sleep_ceiling_reached and the three legacy"
            " warm_day_* renderers (confirmed zero current emitters) are now"
            " explicitly commented as historical-log-only. Two golden simulations"
            " (away_morning_wakeup_skipped_assertion, morning_wakeup_skipped_away_"
            "occupancy) were updated with the user's explicit sign-off to expect the"
            " new morning_wakeup_skipped event instead of relying on the prior silent"
            " gap; no HVAC/decision-logic behavior changed in either. A pre-existing,"
            " unrelated test-harness gap (a pending scenario passing by coincidence"
            " due to the harness not advancing past a scheduled trigger) was found"
            " during this work and filed separately as #598 rather than fixed here."
        ),
    },
    592: {
        "version_fixed": "0.5.63",
        "title": (
            "Activity Record payload-completeness sweep for the four lifecycle-scoped"
            " decision families (nat-vent, door/window pause, override, grace) — many"
            " emit sites discarded locally-computed inputs (outdoor/indoor temps,"
            " thresholds, k_passive, pause duration/entity, override reason/trigger) that"
            " were never threaded into the event payload, so the renderer could only show"
            " a bare verdict with no visible reasoning behind it."
        ),
        "scope_covered": (
            "automation.py: classification_suppressed_paused and"
            " occupancy_setback_suppressed_paused now carry paused_entity/paused_minutes"
            " (new self._paused_entity/self._paused_since state, set in"
            " _pause_for_door_window() and cleared at every resume site); nat_vent_fan_on"
            " gained outdoor_temp; nat_vent_predicted_floor_exit/"
            " nat_vent_floor_imminent_skip gained indoor_temp/comfort_heat/k_passive;"
            " nat_vent_soft_start_entered gained comfort_heat/decline_margin_f (both call"
            " sites); nat_vent_ceiling_escalation gained hours_to_breach/lead_min/"
            " k_active_cool; nat_vent_bedtime_continue gained outdoor_temp/sleep_cool;"
            " sensor_opened at the _re_pause_for_open_sensor() re-check site gained"
            " outdoor_temp/indoor_temp/threshold; override_cleared's fan-only variant"
            " gained reason; override_confirmed gained cls_mode/source;"
            " nat_vent_comfort_floor_exit's temp_check call site now matches its sibling's"
            " fan_mode_change/hvac_mode_restored shape. coordinator.py:"
            " stuck_grace_recovered at the Issue #321 watchdog site gained"
            " stale_mode/stale_since (captured before clear_manual_override() resets"
            " them). ai_skills_context.py: renderers for all of the above updated to"
            " surface the new fields in the Event/Settings cells; added a shared"
            " _render_paused_entity_settings() helper for the two pause-suppression"
            " renderers. No decision logic changed — golden suite (74/74) and pending"
            " suite (4/4) show zero behavior change."
        ),
    },
    591: {
        "version_fixed": "0.5.62",
        "title": (
            "Activity Record showed the same automation decision (comfort band applied,"
            " classification applied, occupancy setback skipped while paused, nat-vent AC"
            " assist armed, bedtime setback skipped, nat-vent bedtime continue, and the"
            " coordinator's state-contradiction warning) two or three times in a row —"
            " sometimes visibly duplicated, sometimes silently masked by the Activity"
            " Record's own consecutive-same-type row collapsing into a misleading 'x2'"
            " count. Traced to apply_classification()/handle_bedtime() each being"
            " reachable from multiple independent trigger paths (startup coalesce +"
            " its own follow-on refresh, cancel-override + its own delayed reclassify,"
            " and an uncancelled 5-minute revisit timer armed by 6 of 7"
            " _apply_comfort_band() callers) with no consistent dedup boundary — the same"
            " defect shape Issue #96 and #444 each independently patched once, at one"
            " call site apiece."
        ),
        "scope_covered": (
            "automation.py: new AutomationEngine._recent_duplicate(key, signature,"
            " window_seconds=None) shared decision-record dedup helper, generalizing"
            " Issue #444's _last_comfort_band_signature pattern (removed in favor of the"
            " helper; const.py's COMFORT_BAND_EVENT_DEDUP_SECONDS removed — comfort_band_"
            " applied is now permanent/content-keyed instead of a 10-minute window, an"
            " explicit owner-approved decision since a real production 11-minute gap had"
            " already slipped past the old fixed window). Migrated onto the helper:"
            " comfort_band_applied, classification_applied (kept updating the pre-existing"
            " _last_classification_applied marker other code reads directly),"
            " classification_suppressed_paused, occupancy_setback_suppressed_paused (both"
            " away/vacation sites), nat_vent_ac_assist_armed (both sleep-window and"
            " full-band branches), bedtime_setback_skipped (all 3 branches),"
            " nat_vent_bedtime_continue, and coordinator.py's state_contradiction_warning"
            " (kept its original 30-minute window; _last_state_contradiction_time"
            " attribute removed). occupancy_setback and hvac_write_blocked_whf_active were"
            " first tried with PERMANENT (content-keyed) dedup like the others, which broke"
            " 5 golden/pending scenarios (wakeup_preserves_whf_manual_override,"
            " away_morning_wakeup_skipped_assertion, morning_wakeup_skipped_away_occupancy,"
            " cancel_override_then_resume, vacation_occupancy_override_cleared) — their"
            " repeats are often distinct, meaningful re-confirmations hours later (e.g."
            " Issue #505's bedtime-time away-setback reapply), not accidental echoes."
            " Switched to WINDOWED dedup (window_seconds=600) instead: short enough to still"
            " catch a genuine same-cycle duplicate (the literal #584 shape), long enough to"
            " never suppress the hours-apart legitimate repeats every failing scenario"
            " actually needed. Investigating the last failure (wakeup_preserves_"
            " whf_manual_override) further found a third, previously-unaudited site with the"
            " same defect: handle_morning_wakeup()'s own unconditional 'morning_wakeup'"
            " marker event is reachable from the same overlapping-trigger paths (the"
            " scenario invokes handle_morning_wakeup() twice) and was masking the correct"
            " outcome once hvac_write_blocked_whf_active got its own guard — also windowed"
            " (600s) now. coordinator.py: _do_startup_coalesce() now returns whether it already ran"
            " apply_classification() this cycle, and _async_update_data_impl() skips the"
            " redundant regular-cycle call when so. tests/conftest.py:"
            " assert_no_duplicate_events() + LEGITIMATELY_REPEATING_EVENT_TYPES shared"
            " helper, generalizing the len(x_events)==1 idiom from test_override_dedup.py."
            " tools/simulate.py: run_scenario_production() now surfaces the full"
            " timestamped event_log (previously dropped). New"
            " tests/test_no_duplicate_decisions.py runs the golden-level automatic"
            " duplicate check via that event_log across every golden scenario. New"
            " tests/test_multi_site_event_dedup_guard.py: ast-based static guard (mirrors"
            " test_executor_offload.py) over the 7 Delta-2-audited multi-call-site event"
            " types. New tests/test_recent_duplicate_helper.py: unit coverage for the"
            " helper itself (content-keyed and windowed modes, bare-instance safety)."
        ),
    },
    589: {
        "version_fixed": "0.5.61",
        "title": (
            "_async_command_fan_entity() (the whole-house-fan command-only"
            " reconciliation choke point, coordinator.py) had no dry_run/"
            "automation_enabled check — the one automated action path that ignored"
            " the 'Automation Enabled' switch, on installs with"
            " fan_state_feedback=False."
        ),
        "scope_covered": (
            "coordinator.py: _async_command_fan_entity() now checks"
            " self._automation_enabled before issuing the turn_on/turn_off service"
            " call, logging '[DRY RUN] Would command fan entity ...' and returning"
            " False instead when automation is disabled — matching the convention"
            " already used by automation.py's other choke points (_set_hvac_mode,"
            " _set_temperature, _activate_fan/_deactivate_fan, _notify). Changed to"
            " return bool (True if a real service call was issued); both call sites"
            " in the command-only reconciliation block (coordinator.py ~2248-2280)"
            " now only update _last_commanded_fan_state when a command actually"
            " fired, so the desired state re-asserts correctly once automation is"
            " re-enabled instead of the bookkeeping believing a command it never"
            " sent."
        ),
    },
    580: {
        "version_fixed": "0.5.60",
        "title": (
            "Dashboard Activity Record report defaulted to a 24-hour window and"
            " rendered events oldest-first (ascending), putting the most recent"
            " activity at the bottom of a potentially long table."
        ),
        "scope_covered": (
            "api.py: ClimateAdvisorActivityRecordView.get() default `hours` query"
            " param changed from 24 to 12, and now calls build_event_timeline_table()"
            " with newest_first=True. ai_skills_context.py: build_event_timeline_table()"
            " gained an opt-in `newest_first` parameter (default False) that reverses"
            " the already-deduplicated row list immediately before markdown rendering —"
            " the dedup/collapse loop itself still iterates forward-chronologically,"
            " since that's what the run-collapsing logic depends on. The AI"
            " investigation context caller (build_activity_timeline_context()) does"
            " not pass newest_first, so LLM-facing context keeps chronological order."
            " frontend/index.html: the Activity Record time-window dropdown (both the"
            " static markup and the updateReportTypeUI() rebuild) now defaults to"
            ' "Last 12 hours", and the JS fallback in _runActivityRecord() changed from'
            " `|| 24` to `|| 12`. The separate AI Investigative Analysis report type's"
            " dropdown and defaults are untouched."
        ),
    },
    578: {
        "version_fixed": "0.5.59",
        "title": (
            "User inline-annotated feedback on an AI Investigative Analysis report"
            " surfaced several distinct report-quality and methodology gaps: the"
            " GitHub-issue-submission title grabbed the first sentence of the report"
            " summary instead of a stable title; target_temp_low/high reading unknown"
            " while hvac_mode=off (e.g. WHF/nat-vent only) was flagged as a data-quality"
            " issue with no off-mode context; the weather bias cap"
            " (MAX_WEATHER_BIAS_APPLY_F) was referenced by the prompt's own instructions"
            " but never actually supplied in context, making that check unperformable;"
            ' "Manual Overrides Today" only counted setpoint overrides, so a genuine fan'
            " takeover made the counter look wrong without any accompanying explanation;"
            ' "System Errors/Warnings" checked whether a CA-internal event\'s `type`'
            ' string happened to contain the substring "error"/"warning" (coincidental'
            " naming, not severity — CA event-log entries have no severity field at all),"
            " so it almost never caught anything real; and the investigator had no"
            " prompt-level guidance distinguishing routine thermostatic/nat-vent"
            " hysteresis cycling from a known-bug signature, nor any rule against"
            " generating unfalsifiable comparative hypotheses (e.g. claiming a learned"
            " rate is contaminated by mixing two conditions) when no data actually"
            " separates those conditions. Separately, the AI Activity Report — a distinct"
            " skill/service from AI Investigative Analysis — was retired entirely at the"
            " user's request; it had not written new data since the #563 skill merge"
            " (both its callers already stored into the unified investigation report"
            " history instead), and is superseded by the deterministic, non-AI Activity"
            " Record. The Investigative Analysis default time window was changed to Last"
            " 1 day (previously silently defaulted to 7 days once a user actually"
            " selected the Investigative Analysis report type, despite the static HTML"
            " markup appearing to already say 24h), and new-install AI defaults changed"
            " from an outdated model at medium reasoning effort to Sonnet 5 at low effort."
        ),
        "scope_covered": (
            "index.html: openGithubIssueModal() title derivation (report type + date,"
            " never report summary text); dropped the now-dead `data`/`result` locals."
            " ai_skills_context.py: build_hvac_entity_context() appends an"
            ' "(expected — hvac_mode=off, no active setpoint)" note instead of a bare'
            " unknown; weather-bias context gains a cap_f line sourced from"
            " MAX_WEATHER_BIAS_APPLY_F; build_override_details_context() relabels the"
            ' setpoint override "Count:" and adds a "Fan override count:" line computed'
            " from the existing FAN OWNERSHIP HISTORY scan (no duplicate scan added);"
            " build_event_log_context()'s EVENT LOG section no longer does substring"
            ' matching for "error"/"warning" — a new SYSTEM LOG RECORDS section reads'
            " real captured log records instead; build_ai_report_history_context() and"
            " its ai_report_history provider registration removed entirely (dead —"
            " nothing wrote to coordinator._ai_report_history since #563)."
            " ai_skills_investigator.py: _SYSTEM_PROMPT rule 4 and the SYSTEM"
            " ERRORS/WARNINGS output-format section reworded for real log records; rule"
            " 8 (KNOWN-FIXED cross-check) extended to compare a flagged event's timestamp"
            " against ACTIVITY TIMELINE version-change boundaries; new rule 10b forbids"
            " speculative comparative hypotheses without a supplied comparative baseline;"
            " rule 11 gains a third benign-pattern bullet for threshold-adjacent fan"
            " hysteresis cycling with no reconcile/backstop event in the same window."
            " log_capture.py (new): ClimateAdvisorLogRingBuffer, a logging.Handler"
            " (collections.deque(maxlen=LOG_CAPTURE_CAP) ring buffer) attached to the"
            " custom_components.climate_advisor logger namespace once at"
            " async_setup_entry() via log_capture.install()/uninstall(), capturing every"
            " existing _LOGGER.warning()/.error() call site automatically — no per-call-"
            " site changes needed. AI Activity Report removal (coordinator.py: deleted"
            " _ai_report_history, async_store_ai_report, _save_ai_reports,"
            " _load_ai_reports, get_ai_report_history, delete_ai_report, and the"
            " load-on-init call — all dead since #563; api.py: deleted"
            " ClimateAdvisorAIActivityView and ClimateAdvisorAIReportsView (the latter"
            " also called the now-deleted get_ai_report_history() — caught during"
            " implementation, not part of the original plan), their API_VIEWS"
            ' registrations, and the delete_report "activity" branch; __init__.py:'
            " deleted the ai_activity_report service registration/handler;"
            " services.yaml: deleted the ai_activity_report service block; const.py:"
            " deleted API_AI_ACTIVITY, API_AI_REPORTS, AI_REPORT_HISTORY_CAP,"
            " AI_REPORTS_FILE; index.html: deleted the AI Activity Report dropdown"
            " option, dispatch branch, _runAIActivityReport(), the Activity history-"
            " filter button, and the dead ai_reports fetch in loadUnifiedHistory()/"
            " downloadDebugLogs()). Default time window: index.html"
            " updateReportTypeUI()'s investigation branch now defaults report-time-"
            " window to 24h (was 168h — this was the actual live default, not the static"
            " HTML markup a prior read of the code suggested); api.py"
            " ClimateAdvisorInvestigateView and the JS hours fallback in"
            " _runAIInvestigation() changed from 168 to 24. AI defaults: const.py"
            " DEFAULT_AI_MODEL -> claude-sonnet-5, DEFAULT_AI_REASONING_EFFORT -> low;"
            " added AI_MODEL_SONNET_5 to the static AI_MODELS fallback dropdown list"
            " (caught during implementation: the static list used when no API key is"
            " configured yet — i.e. exactly the first-install scenario — did not"
            " previously include claude-sonnet-5 at all, which would have broken the"
            " config-flow model selector's default the moment this file changed)."
        ),
    },
    573: {
        "version_fixed": "0.5.58",
        "title": (
            "The options-flow menu-based navigation added in Issue #50, and the"
            " immediate-persist-on-Submit behavior added in Issue #557, together meant"
            " every single section's Submit both wrote the config entry AND immediately"
            " called hass.config_entries.async_reload() — so a settings session touching"
            " several sections (e.g. Core Settings then AI Settings then Schedule) tore"
            " down and rebuilt the coordinator/AI client once per section instead of"
            " once per session. Changed by explicit user request during the Issue #572"
            " investigation, once it became clear the same reload was involved in that"
            " AI capability-persistence chain of bugs. First shipped (0.5.57) with"
            ' menu-item "Save"/"Save and Reload" entries, but HA\'s options-flow menu'
            " step can only render a plain list row — verified against HA frontend"
            " source (step-flow-menu.ts, dialog-data-entry-flow.ts) — so there was no way"
            " to make either one look or behave like an actual button, distinct from a"
            " settings-section row. Replaced (0.5.58) with a repair-issue notice instead."
        ),
        "scope_covered": (
            "config_flow.py: _commit_section() no longer calls async_reload() — it only"
            " calls hass.config_entries.async_update_entry(), same as before, so"
            " re-opening a section within the same options-flow session still shows the"
            " just-saved value (the original Issue #557 fix this preserves). Every"
            " section's Submit behaves exactly as it did before #573 (writes only,"
            " already true since 0.5.57) and OPTIONS_MENU_OPTIONS has no save/reload"
            " entries at all. Instead, _commit_section() raises a fixable, persistent"
            ' repair issue ("reload_needed", WARNING severity) via'
            " homeassistant.helpers.issue_registry — visible directly on the integration's"
            " Devices & Services page. repairs.py: new ReloadNeededRepairFlow whose confirm"
            " step calls hass.config_entries.async_reload() (the same mechanism Submit"
            " used pre-#573 — not a homeassistant.restart service call, which would be an"
            " out-of-scope HA Boundary Rule violation; that alternative was explicitly"
            " considered and rejected) and deletes the issue. __init__.py:"
            " async_setup_entry() also unconditionally clears the issue on every setup, so"
            ' a reload/restart through any path (Repairs "Fix", HA\'s own generic'
            ' "Reload" action, or a full HA restart) clears the notice. strings.json and'
            " translations/en.json carry the issues.reload_needed translation. Test"
            " coverage: tests/test_config_flow.py (section Submit never reloads, raises the"
            " repair issue), tests/test_repairs.py (TestReloadNeededRepairFlow — confirm"
            " reloads and clears the issue; graceful no-op with no config entries)."
        ),
    },
    572: {
        "version_fixed": "0.5.56",
        "title": (
            "claude-sonnet-5's first request against the reactive-learning system built"
            " by #563/#565/#568/#569 was guaranteed to silently burn the full max_tokens"
            " budget with zero visible output for up to ~90 seconds — the model does"
            " uncapped internal reasoning whenever no thinking control is sent, and"
            " medium/low reasoning tiers never sent any. #565's reactive fix only learned"
            " this from the live failure itself, and #568/#569's persistence fix (which"
            " made the lesson survive an options-flow reload) never covered a genuine HA"
            " restart (EVENT_HOMEASSISTANT_STOP never called _async_save_state()), so on"
            " an install that restarts/redeploys regularly the '90s silent hang' kept"
            " recurring instead of staying fixed after the first occurrence. Traced with"
            " live evidence: ha_logs.py timeline correlating the 2026-08-04 learning event"
            " with restart-signature log bursts before the 2026-08-05 recurrence, plus the"
            " live climate_advisor_state.json pulled via SSH. Root design flaw identified:"
            " no live request should ever be the mechanism that discovers a model's"
            " correct shape."
        ),
        "scope_covered": (
            "claude_api.py: entire reactive-learning subsystem removed"
            " (_unsupported_params, _adaptive_thinking_models, _detect_deprecated_param(),"
            " _detect_adaptive_thinking_required(), the same-call retry loops in both"
            " async_request_streaming() and _async_call_with_retry(), their"
            " get_persistent_stats()/restore_persistent_stats() entries). Replaced with"
            " AI_MODEL_CAPABILITIES, a static per-model table in const.py"
            " (thinking_shape: legacy/adaptive, supports_temperature), verified 2026-08-05"
            " via direct Anthropic Messages API calls for claude-sonnet-4-6/opus-4-6/"
            " haiku-4-5-20251001 (legacy, temperature works — unchanged) and"
            " claude-sonnet-5/opus-5/fable-5 (adaptive, temperature rejected — confirmed"
            " both at a small test prompt and at production scale, max_tokens=8192)."
            " claude-haiku-5 confirmed not to exist (404) and intentionally omitted."
            " _build_request_kwargs() is now a pure table lookup; a model not in the"
            " table falls back to the legacy shape (proven safe for years) and logs a"
            " WARNING naming it. truncated_empty is still detected and logged for a"
            " table-verified model, but only as a signal that Anthropic changed something"
            " — no automatic retry or relearning. Test coverage: tests/test_claude_api.py"
            " (TestBuildRequestKwargsFromCapabilityTable, TestUnexpectedApiErrorObservability,"
            " TestRequestKwargsDecisionLogging)."
        ),
    },
    571: {
        "version_fixed": "0.5.55",
        "title": (
            "A legitimate nat-vent exit (CA turning its own WHF off) was being misread as an"
            " externally-owned/untracked fan every single cycle, then force-corrected by the"
            " 'backstop_30min' periodic reconcile — despite the name, this reconcile is not"
            " actually gated on a 30-minute timer; it runs on every coordinator update cycle."
            " Root cause: _compute_fan_status()'s ground-truth fallbacks (and two sibling"
            " functions, _compute_whf_status()/_compute_hvac_fan_status(), which had the"
            " identical gap) had no OFF-direction confirmation guard — the mirror of the"
            " existing ON-direction 'active (unconfirmed)' guard from Issue #510. When CA"
            " cleared its own ownership flags and commanded the fan off, but the physical"
            " entity/thermostat attribute hadn't caught up yet, the fallback unconditionally"
            " read as 'running (untracked)', and since the periodic backstop derives its"
            " trigger condition directly from that same value, it fired reconcile_fan_on_"
            " startup() moments after every legitimate exit. Separately, _compute_hvac_fan_"
            " status() (HVAC-integrated-fan mode) had NO ground-truth cross-check on the ON"
            " direction at all — unlike its two siblings, which gained one under Issue #510 —"
            " so _fan_active=True rendered as 'active' unconditionally and forever, even if the"
            " HVAC fan later genuinely stopped."
        ),
        "scope_covered": (
            "fan_status.py: new resolve_untracked_fan_status(recent_fan_command: bool) -> str,"
            " the single shared OFF-direction predicate. coordinator.py: the 4 ground-truth"
            " fallback sites (2 in _compute_fan_status(), 1 each in _compute_whf_status()/"
            " _compute_hvac_fan_status()) now route through it instead of independently"
            " hand-rolling the same 'physical/thermostat signal says on -> running (untracked)'"
            " branch — centralizing logic these two specific functions have needed synchronized"
            " parallel fixes for before (Issue #510). _compute_hvac_fan_status() additionally"
            " rewritten to add the ON-direction 'active (unconfirmed)'/stale-flag guard,"
            " mirroring _compute_whf_status()'s shape exactly, via a memoized thermostat-ground-"
            " truth closure shared between the ON and OFF branches. Cross-reference comments"
            " added at _is_recent_fan_command()'s call-site history and the backstop_30min"
            " block. tests: TestDualFanStatus in test_fan_control.py gained 11 new cases across"
            " all three functions and both directions (all revert-tested)."
        ),
    },
    567: {
        "version_fixed": "0.5.54",
        "title": (
            "WHF automation actions were being confused as manual ones. The QuietCool device"
            " transmits AND receives on the same RF channel, so a CA-issued fan command"
            " (confirmed live: nat_vent_cycling_on) could be heard back by the same"
            " receive-side event.quietcool_remote entity ~1.7s later and misread as a fresh"
            " remote press. _async_fan_remote_changed()/_flush_fan_remote_burst() (added in"
            " #486/#519) never called the existing _is_recent_fan_command() echo guard that"
            " the sibling physical-fan-entity handler has used since Fix #239 — an original"
            " design gap, not a regression. The false override then cascaded: a later"
            " legitimate nat-vent exit deferred to the (false) active override and skipped"
            " deactivating the fan, so a genuine physical fan-off ~18 minutes later wasn't"
            " caught until the 2-tick drift-reconciliation backstop fired 10 minutes after"
            " that — and that backstop's own fan_cancel event was unconditionally rendered"
            " 'Fan cancel (user turned off)' regardless of its actual trigger, a second,"
            " independent mislabeling bug surfaced by the same investigation."
        ),
        "scope_covered": (
            "coordinator.py: _async_fan_remote_changed() now calls _is_recent_fan_command"
            "(threshold_seconds=30.0) at ingestion, before any burst is opened — mirrors the"
            " existing guard in _async_fan_entity_changed() (Fix #239); event.context matching"
            " isn't usable here since CA never calls a service on this receive-only entity."
            " _is_recent_fan_command()'s docstring and the Issue #417 sibling-comment in"
            " _async_thermostat_changed() now cross-reference this call site so a future new"
            " fan-state listener doesn't repeat the same missed-guard defect a third time."
            " ai_skills_context.py: _render_fan_cancel() branches on the fan_cancel event's"
            " existing trigger field (fan_off / timer_boundary_settle / physical_drift_correction"
            " — all three already emitted by automation.py, previously ignored by the renderer)"
            " so CA's own drift-reconciliation self-correction no longer renders as a user"
            " action. tests: TestFanRemoteEchoGuard in test_fan_remote.py (4 cases, revert-tested);"
            " TestFanOwnershipAnnotations additions in test_activity_renderers.py (4 cases,"
            " revert-tested); fixed a latent MagicMock-truthiness gap this change exposed in"
            " test_restart_coalescing_fan_guard.py's coordinator stub."
        ),
    },
    568: {
        "version_fixed": "0.5.53",
        "title": (
            "After #565 shipped, the user reported claude-sonnet-5 'still doesn't work' while"
            " claude-sonnet-4-6 kept working. Root-caused from live log evidence (not a live"
            " retest): the two per-model capability caches (#563's _unsupported_params, #565's"
            " _adaptive_thinking_models) were pure in-memory ClaudeAPIClient instance state."
            " Direct log evidence: 'Options section saved — reload triggered (cleared=0)' fired"
            " 4 seconds after the model had just learned it needed adaptive thinking —"
            " hass.config_entries.async_reload() (triggered by any options-flow save, immediate"
            " since #557) tears down and reconstructs the entire coordinator including a"
            " brand-new ClaudeAPIClient, silently discarding everything just learned."
            " ClaudeAPIClient.update_config() — the one method that updates config without"
            " discarding capability state — was confirmed to have zero production call sites;"
            " every real config change goes through async_reload() instead. So the reactive"
            " self-heal from #565 could only ever survive within a single process lifetime,"
            " which routine restarts and any settings change reset — a user actively testing"
            " which model works would never observe it."
        ),
        "scope_covered": (
            "claude_api.py: both self._unsupported_params and self._adaptive_thinking_models are"
            " now included in get_persistent_stats() (serialized as JSON-safe dict[str,"
            " list[str]] / list[str]) and restored by restore_persistent_stats() (type-validated"
            " per the project's JSON-from-disk convention — any malformed shape degrades to"
            " empty rather than raising). This reuses the same bridge already wired through"
            " coordinator._async_save_state()/async_restore_state() for monthly_cost/rate"
            " counters, so both caches now survive config reloads and HA restarts, not just the"
            " lifetime of one ClaudeAPIClient instance. Also added observability requested"
            " during investigation, so a future occurrence is diagnosable from logs alone rather"
            " than needing another live diagnostic: _log_request_kwargs_decision() logs the"
            " resolved model/reasoning_effort/adaptive-thinking-active/unsupported-params/"
            " max_tokens/thinking-and-output_config-presence before every API call (both"
            " request paths); 'response finished' log lines now include input_tokens alongside"
            " output_tokens (claude-sonnet-5's updated tokenizer is documented to parse the same"
            " text into meaningfully more input tokens than claude-sonnet-4-6 — a distinct,"
            " not-yet-observed, input-side context-window risk this makes visible in trend"
            " data); and any APIError matching neither known capability-detection regex (and not"
            " a NotFoundError) now logs a distinct 'Unrecognized API error shape' WARNING with"
            " the full raw message, so a genuinely new failure mode is immediately greppable"
            " instead of blending into generic retry-failure text. Test coverage:"
            " tests/test_claude_api.py (TestPersistentStats capability-cache round-trip +"
            " malformed-data cases, TestUnrecognizedApiErrorObservability,"
            " TestRequestKwargsDecisionLogging)."
        ),
    },
    565: {
        "version_fixed": "0.5.52",
        "title": (
            "Both the AI Investigator (streaming) and AI Activity Report (non-streaming)"
            " could return stop_reason=max_tokens with zero visible answer text on"
            " claude-sonnet-5, at reasoning_effort=medium — the exact zero-output cause"
            " Issue #563 left as an open follow-up. Root-caused via a live diagnostic"
            " bypassing claude_api.py entirely (direct AsyncAnthropic calls dumping the"
            " full raw response, all content block types, and the complete usage object):"
            " claude-sonnet-5 (A) rejects the `temperature` parameter outright — already"
            " self-healed correctly by #563's reactive capability detection, confirmed"
            " working in production logs, NOT the cause of this issue — and (B) rejects"
            " the legacy `thinking: {type: enabled, budget_tokens: N}` shape outright with"
            " a 400 naming the replacement directly ('Use thinking.type.adaptive and"
            " output_config.effort'). Because claude_api.py only ever sent thinking"
            " control at reasoning_effort=='high' (using the now-incompatible legacy"
            " shape), medium/low requests sent no thinking control at all — leaving this"
            " model's own internal reasoning completely uncapped. Reproduced live: on a"
            " production-sized context, claude-sonnet-5 consumed the entire 8192-token"
            " (and separately the entire 16384-token) budget purely on invisible internal"
            " reasoning, stop_reason=max_tokens, zero text, matching live HA logs exactly"
            " (8192 output_tokens, ~93s streaming call, zero visible output). Confirmed"
            " fix live: sending thinking={'type':'adaptive'}, output_config={'effort':"
            " reasoning_effort} on the same context returned stop_reason=end_turn with a"
            " real answer."
        ),
        "scope_covered": (
            "claude_api.py: added _detect_adaptive_thinking_required() (matches the"
            " 'thinking.type.adaptive' replacement-parameter name in Anthropic's 400"
            " message body, mirroring the existing _detect_deprecated_param() pattern) and"
            " a new self._adaptive_thinking_models: set[str] per-model capability cache"
            " (same in-memory, per-client-instance lifecycle as self._unsupported_params)."
            " _build_request_kwargs() now applies thinking={'type':'adaptive'},"
            " output_config={'effort': reasoning_effort} at EVERY reasoning tier (not just"
            " 'high') for any model in _adaptive_thinking_models — output_config.effort"
            " maps directly from the configured low/medium/high reasoning_effort, so no"
            " separate budget_tokens table is needed for adaptive-shape models. The"
            " unsupported-params strip still runs last, after this block, so a model"
            " already known not to accept temperature never gets it re-added (same"
            " ordering guarantee as the existing high-tier legacy path). Reactive"
            " learning: both the streaming retry loop (async_request_streaming, gated on"
            " 'no content yielded yet' — a stream can't un-yield thinking deltas already"
            " shown to the caller) and the non-streaming retry loop"
            " (_async_call_with_retry's APIError branch) now catch a 400 naming"
            " thinking.type.adaptive, learn the model, and retry once with the adaptive"
            " shape, mirroring the existing deprecated-parameter retry exactly."
            " Additionally — since the medium/low failure mode never raises an exception"
            " at all (it's a 'successful' HTTP response with truncated_empty=True) —"
            " _async_call_with_retry now also detects a truncated_empty response after a"
            " successful call and retries once in place (safe: nothing has been shown to"
            " the caller yet, unlike streaming). The streaming path cannot retry"
            " truncated_empty in place for the reason above, but arms"
            " _adaptive_thinking_models on that detection so the *next* call (streaming or"
            " non-streaming, same client instance) applies the adaptive shape from the"
            " start. Test coverage: tests/test_claude_api.py"
            " (TestAdaptiveThinkingKwargsShape, TestAdaptiveThinkingReactiveFallback,"
            " TestAdaptiveThinkingTruncatedEmptyRecovery) — kwargs-shape correctness at"
            " every reasoning tier, parameter-strip ordering, both reactive-retry paths,"
            " both truncated_empty-recovery paths, and confirms an unlearned model (e.g."
            " claude-sonnet-4-6) is completely unaffected — no regression risk for models"
            " never observed to need this."
        ),
    },
    563: {
        "version_fixed": "0.5.51",
        "title": (
            "The AI Investigator's KNOWN-FIXED ISSUES context section was effectively"
            " unbounded: _fix_is_relevant()'s first rule matched any entry with a"
            " non-empty scope_not_covered field, and every entry had one (mandatory per"
            " the release checklist), so all 169 entries passed on every run regardless"
            " of the intended version-scoping. Separately, build_known_fixes_context()"
            " rendered scope_covered/scope_not_covered by iterating them with a bare"
            " `for x in fix.get(...)` — both fields are plain strings, not lists, so this"
            " iterated character-by-character, inflating rendered size by roughly 15x for"
            " affected entries (measured: issue #561 alone went from 3,536 raw characters"
            " to 53,595 rendered characters of single-character garbage lines)."
        ),
        "scope_covered": (
            "Removed scope_not_covered from the KNOWN_FIXES schema entirely (all 169"
            " existing entries) and from the CLAUDE.md release checklist requirement to"
            " author it — audit found it had exactly one functional consumer in the"
            " codebase (this same broken filter/renderer) and no test enforced its"
            " presence. The replacement filter went through two iterations: first"
            " _fix_is_relevant() (version_fixed >= current_tuple), then found during"
            " manual verification to be too narrow — on a real running install that only"
            " ever matches the single most-recent release's fixes, so a fix from even one"
            " release earlier drops out of context immediately, defeating the 'was this"
            " already fixed' cross-check for anyone not on the latest release. Replaced"
            " with _select_relevant_fixes(): always includes not-yet-deployed entries"
            " (version_fixed > current) plus the _KNOWN_FIXES_RECENT_COUNT (15) most"
            " recently fixed entries by count, not version-equality — bounded regardless"
            " of KNOWN_FIXES size or release cadence, and useful across more than one"
            " release. build_known_fixes_context() no longer renders title/scope_covered"
            " at all; it looks up the matching RELEASE_NOTES[version_fixed] bullet"
            " ('Fix #N: ...'/'Feat #N: ...') via the new _release_note_bullet() and uses"
            " that instead — shorter, already occupant-outcome phrased, and already"
            " mandatory for every release, so no new authoring burden. Falls back to"
            " title only if no RELEASE_NOTES bullet matches. Also trimmed"
            " coordinator._github_open_cache/_github_closed_cache (ai_skills_context.py"
            " _fetch_github_issues) to only the fields actually rendered (number, title,"
            " state, labels) via the new _trim_issue_fields(), instead of caching the"
            " full raw GitHub API response. Measured real-world effect: the rendered"
            " KNOWN-FIXED ISSUES context block for the current version went from a raw"
            " source size of 327,000+ characters (169/169 entries, before the ~15x"
            " character-iteration inflation on top of that) to under 1,000 characters,"
            " bounded to at most 15 recent entries going forward — see"
            " tests/test_ai_skills_context_known_fixes.py. Follow-on work in the same"
            " branch/issue: merged the retired activity_report skill into investigator"
            " and scoped the silent narration path to a lighter priority<=1 provider set"
            " (it was wrongly running the full 16-provider audit pipeline, including a"
            " live GitHub fetch, on every scheduled narration); rewrote the streaming"
            " report UI to render real backend step narration and progressive markdown"
            " section cards instead of a fake elapsed-seconds counter and raw-text"
            " painting; fixed a real regression where the merged skill's rename broke"
            " the ai_activity_report service call outright; added dynamic Claude model"
            " discovery (fetch_available_models(), cached, config-flow dropdown) plus"
            " reactive per-model capability detection for both a deprecated/invalid"
            " model ID (retries once with the newest live same-tier model, persisted) and"
            " a deprecated request parameter (e.g. temperature rejected by a specific"
            " model — retries once without it, learned per-model); raised the"
            " ai_max_tokens ceiling 8192->16384; and added explicit detection/logging for"
            " responses that consume the full max_tokens budget while producing zero"
            " visible output, distinct from ordinary truncation. The zero-output cause"
            " itself (observed with claude-sonnet-5 at reasoning_effort=medium) is not"
            " yet root-caused — tracked as a separate follow-up issue rather than guessed"
            " at further in this one."
        ),
    },
    561: {
        "version_fixed": "0.5.50",
        "title": (
            "Whole-house fan (WHF) activated by automation with every monitored door/window"
            " sensor closed, running the exhaust fan against a sealed house for ~45 seconds"
            " with no cooling benefit while HVAC was silently suppressed. The log's own"
            " 'whole-house fan manually turned on' message wrongly implied the user did it."
            " Root-caused to three stacked defects: (A) nat_vent_temperature_check()'s"
            " on-threshold reactivation branch had no contact-sensor check at all and read a"
            " cached self._last_outdoor_temp instead of a caller-sourced live value; (B)"
            " _reconcile_fan_physical_drift()'s CORRECT outcome preserved _natural_vent_active"
            " unconditionally (Issue #423's preserve_nat_vent_session=True), so a session could"
            " survive for hours after windows genuinely closed, with no log line, waiting for"
            " temperature to cross a cycling threshold; (C) reconcile_fan_on_startup() had no"
            " mutual exclusion across its 4 call sites, so two concurrent 'adopt-on' calls each"
            " independently started their own self-rescheduling 5-min backstop timer"
            " (_start_fan_thermo_backstop() only tracks one live handle via"
            " self._fan_thermo_cancel), leaving one permanently uncancellable; (D) the resulting"
            " duplicate _activate_fan() calls raced the single last-write-wins"
            " _fan_command_context_id, so the coordinator's own state-change listener"
            " misattributed CA's own command as a manual override."
        ),
        "scope_covered": (
            "Added AutomationEngine._any_monitored_sensor_open() as the single choke point for"
            " 'is a monitored sensor currently open', replacing 3 duplicated inline checks"
            " (_exit_nat_vent, the idle-open reactivation gate, resume_from_pause) and adding a"
            " 4th call site. nat_vent_temperature_check()'s on-threshold branch now force-closes"
            " the session (via the existing _exit_nat_vent() choke point, with a WARNING log)"
            " instead of cycling the fan on when FAN_MODE_WHOLE_HOUSE/FAN_MODE_BOTH and no"
            " sensor is open — scoped away from FAN_MODE_HVAC, whose fan-only mode has no"
            " physical-exterior-airflow requirement and is unaffected. nat_vent_temperature_check()"
            " now takes outdoor as a required keyword-only parameter (mirroring"
            " fan_thermostat_check()'s existing convention) instead of reading"
            " self._last_outdoor_temp internally; both callers"
            " (coordinator._async_thermostat_changed, automation._thermo_backstop_task) and the"
            " sim harness (tools/sim_harness/run_production.py) updated accordingly."
            " _reconcile_fan_physical_drift()'s CORRECT branch now only passes"
            " preserve_nat_vent_session=True when _any_monitored_sensor_open() confirms the"
            " session is still legitimate; otherwise it force-ends the session (with a WARNING"
            " log) and releases any WHF HVAC suppression via _release_whf_and_reclassify(),"
            " mirroring on_fan_turned_off()'s genuine fan-off sequence. reconcile_fan_on_startup()"
            " gained a self._reconcile_fan_in_progress reentrancy guard (a concurrent call skips"
            " its tick rather than double-processing) via a private"
            " _reconcile_fan_on_startup_locked() body, plus a self._fan_thermo_generation counter"
            " on _start_fan_thermo_backstop()/its tick callback as defense-in-depth (a superseded"
            " chain self-terminates on its next tick instead of ticking forever in parallel)."
            " Replaced the single last-write-wins self._fan_command_context_id with a 30-second"
            " recency list (self._recent_fan_command_context_ids,"
            " _record_fan_command_context(), fan_command_context_matches()), so an overlapping"
            " second command can no longer erase the first's provenance before its resulting"
            " state-changed event is evaluated; coordinator._async_fan_entity_changed() updated"
            " to call the new matcher instead of comparing a single id. Added dedicated unit"
            " tests for all four fixes (sensor-gate force-close, HVAC-mode exemption, drift-"
            " correction session-closure, reentrancy-guard skip/completion/exception-safety,"
            " generation-counter supersession, overlapping-context matching, stale-context"
            " expiry) in tests/test_nat_vent_activation.py and tests/test_fan_control.py."
        ),
    },
    557: {
        "version_fixed": "0.5.49",
        "title": (
            "Options dialog sections appeared to silently discard changes: submitting any"
            " one of the 11 section forms (core, setpoints, temperature sources, sensors,"
            " occupancy, schedule, notifications, advanced, classification thresholds, AI"
            " settings, GitHub integration) only staged the values into an in-memory"
            " self._updates dict and routed back to the menu — none of them called"
            " hass.config_entries.async_update_entry(). Every section's form defaults were"
            " built from self.config_entry.data (the persisted entry), never from the staged"
            " self._updates, so re-opening the just-edited section showed the old value"
            " unless the user separately navigated to the menu's distinct 'Save & Close'"
            " item, which was the only step that actually persisted and reloaded."
        ),
        "scope_covered": (
            "Replaced the two-phase stage-then-save design with immediate per-section commit."
            " Added ClimateAdvisorOptionsFlow._commit_section() — merges the section's input"
            " (reusing _apply_step_input() for the 3 steps with clearable optional-entity"
            " fields), calls hass.config_entries.async_update_entry() +"
            " hass.config_entries.async_reload() immediately, logs an INFO line, then resets"
            " scratch state. All 11 section steps' success branches now call"
            " _commit_section() instead of staging into self._updates directly. Deleted"
            " async_step_save() and removed 'save' from OPTIONS_MENU_OPTIONS and both"
            " strings.json/translations/en.json menu labels — the HA frontend's built-in"
            " dialog close control is the only way to exit the options flow now. Updated"
            " tests/test_config_flow.py's _make_options_flow()/_run_options_flow() harness to"
            " mirror real HA's async_update_entry() (mutating entry.data in place) and to"
            " drive steps without a trailing save call; rewrote TestOptionsFlowMenu's"
            " save-specific tests into per-section-commit tests including an explicit"
            " regression guard that a single section submit persists without any further"
            " step. TestOptionsFlowMultiStep and TestOptionsFlowClearing (Issue #434)"
            " continued passing unchanged against the new harness, confirming multi-section"
            " accumulation and clearable-field semantics are preserved."
        ),
    },
    558: {
        "version_fixed": "0.5.48",
        "title": (
            "Hot-day daytime pre-condition offset (comfort_cool - 2) chased a colder-than-"
            "comfort setpoint during peak afternoon heat after a multi-day away trip ended,"
            " because _set_temperature_for_mode() (called from 5 separate 'resume comfort'"
            " event handlers) reimplemented the offset math without checking the"
            " _pre_condition_achieved gate that the correctly-gated apply_classification()/"
            "select_comfort_band() path respected. Root-cause investigation found the daytime"
            " mechanism was also largely redundant with the sleep band and the separate"
            " overnight pre-cool banking mechanism, and that the daily briefing unconditionally"
            " claimed pre-cooling happened 'this morning' regardless of ground truth. Also found"
            " the overnight pre-cool mechanism (Issue #258) was trend-gated only, missing"
            " plateaued multi-day heat waves with no single night trending sharply warmer."
        ),
        "scope_covered": (
            "Removed the hot-day daytime pre-condition mechanism entirely: classifier.py no"
            " longer sets pre_condition/pre_condition_target for DAY_TYPE_HOT (cooling-trend"
            " pre-heat branch, positive values, untouched). Deleted the now-dead offset branch"
            " from select_comfort_band() and _set_temperature_for_mode() (all 5 callers:"
            " handle_occupancy_home, door/window resume, nat-vent comfort-floor exit, dashboard"
            " resume, economizer deactivation), and the _pre_condition_achieved/"
            "_pre_condition_achieved_date state (definition, save, load). Added"
            " resolve_pre_cool_modifier() in automation.py as the single source of truth for"
            " overnight pre-cool eligibility across all 5 real call sites (handle_pre_cool(),"
            " trigger-time scheduler, reschedule-on-nat-vent-exit, chart target-band dip, ODE"
            " predicted-indoor curve) — broadened to fire on tomorrow's absolute hot"
            " classification (tomorrow_high >= threshold_hot) in addition to the original"
            " warming-trend gate, using a HOT_DAY_PRE_COOL_MODIFIER=-2.0 fallback so a plateaued"
            " heat wave gets real overnight banking (target = sleep_cool - 2) instead of a"
            " no-op. Fixed briefing._hot_day_plan() to only claim overnight pre-cool"
            " prospectively ('tonight') when resolve_pre_cool_modifier() confirms it's actually"
            " eligible, falling back to a truthful, non-fabricated statement otherwise. Retired"
            " golden scenario hot_day_precool_achieved_reverts_to_comfort (Issue #295, asserted"
            " the removed daytime mechanism) to tools/simulations/unsupported/. Added golden"
            " scenario hot_plateau_pre_cool_applied covering the new hot-day-fallback-only case."
        ),
    },
    555: {
        "version_fixed": "0.5.47",
        "title": (
            "sensor.climate_advisor_daily_briefing exceeded HA's 255-char sensor state limit"
            " on days with a lot to report (away/vacation occupancy + dual morning/evening"
            " window-opportunity rows), causing HA to reject the state and fall the sensor"
            " back to 'unknown' — the primary UI surface went blank on exactly the days with"
            " the most to say"
        ),
        "scope_covered": (
            "_generate_tldr_table() in briefing.py shortened: dropped fixed 17-char label"
            " alignment padding (pure whitespace overhead, not reliably rendered as aligned"
            " columns in either UI surface), and removed the redundant '(setback — away/"
            " vacation)' phrase from the HVAC Mode row since the Occupancy row directly below"
            " it already states the same fact. Brings the documented worst case from ~260-265"
            " chars to a comfortable margin under 250. Added a shared"
            " ClimateAdvisorBaseSensor._capped_state() truncation safety net (used by both"
            " ClimateAdvisorBriefingSensor and, refactored behavior-preserving,"
            " ClimateAdvisorLastActionReasonSensor) so a future regression degrades gracefully"
            " instead of dropping to 'unknown'. Added TestTldrTableLength regression test"
            " covering the away/vacation + dual-window worst case, and"
            " tests/test_briefing_sensor.py covering the sensor's truncation/fallback logic"
            " directly."
        ),
    },
    553: {
        "version_fixed": "0.5.46",
        "title": (
            "#551's command batching cut a typical deploy from ~10-11 SSH/SCP connections to"
            " ~8, but a live test still hit the wall: 4 connections succeed, the 5th (the"
            " file-copy scp, still a separate invocation from the batched ssh calls) gets"
            " reset — same threshold as the original unfixed behavior. Explicit direction:"
            " combine everything into no more than 3 connections total, and validate live"
            " before opening any PR (the prior PR, #549, had gone out on lint/unit checks"
            " alone and turned out to be broken in live use)"
        ),
        "scope_covered": (
            "tools/deploy.py: eliminated the scp file-transfer connection entirely by piping"
            " a tar stream of the component directory through an ssh connection's stdin (new"
            " _build_component_tar() using stdlib tarfile+io.BytesIO, new run_ssh_piped()"
            " passing input=tar_bytes to subprocess.run) — the remote script that receives it"
            " also runs everything else that needs to happen after the files land (verify,"
            " restart, wait, log-fetch), all in that one connection. create_backup() combines"
            " connect+backup-tar+legacy-cleanup+mkdir into one ssh call (also serves as the"
            " connectivity test, replacing the standalone test_ssh()) plus one scp download of"
            " the backup — connections 1-2. deploy_files() is connection 3. do_rollback() drops"
            " to 1 connection (the chosen local backup's bytes pipe in the same way, no upload-"
            " then-separate-extract). Result: 3 connections for a full deploy (2 for"
            " --skip-restart), 1 for --rollback — verified via deploy.py's own debug log during"
            " live testing against the real HA host. Two real bugs were caught during that live"
            " validation (not caught by ruff/pytest, which is exactly why live validation was"
            " required this time): (1) crash-safety — the initial rm-rf-then-extract approach"
            " for both deploy and rollback left a multi-second window where a connection drop"
            " mid-extraction (this HA SSH add-on has demonstrated it does reset connections"
            " mid-command) could leave the live integration directory deleted or half-written;"
            " fixed by extracting into a temp dir and swapping it into place as the final,"
            " near-instant step, confirmed safe by directly inspecting the remote filesystem"
            " after a live connection reset actually occurred mid-test. (2) tar-format"
            " mismatch — create_backup()'s backup tar wrapped its contents in a climate_advisor/"
            " directory (tar czf -C parent climate_advisor) while the new temp-extract-and-swap"
            " logic assumed a flat layout matching _build_component_tar(), so a live rollback"
            " test produced a nested climate_advisor/climate_advisor/ and a broken HA"
            " integration ('Integration climate_advisor not found') until create_backup()'s tar"
            " command was changed to -C rpath . (flat, matching the deploy-tar format);"
            " re-verified live afterward with a clean rollback that left the exact expected"
            " file layout and a healthy HA restart. As a side effect of the temp-dir-swap"
            " approach, deploys are now exact mirrors of the source tree (extract-on-top"
            " previously left stale files from earlier versions, e.g. renamed/removed files,"
            " sitting around indefinitely — observed live as a 35-local-vs-38-remote file-count"
            " mismatch, now exactly 35=35). docs/SSH-SETUP.md rewritten to document the 3"
            " connections precisely and record both superseded approaches (#549, #551) and why."
        ),
    },
    551: {
        "version_fixed": "0.5.45",
        "title": (
            "#549's SSH ControlMaster multiplexing, merged to fix deploy.py's rate-limit"
            " problem, made deploys worse in live testing: every connection attempt failed"
            " immediately (~1s, not a timeout) with 'mux_client_request_session: read from"
            " master failed: Connection reset by peer' / 'Failed to connect to new control"
            " master' — reproduced consistently as the very first connection of a fresh run"
            " (ruling out the rate limiter), with both Windows-style and POSIX-style control"
            " socket paths, while a bare non-multiplexed connection to the same host succeeded"
            " instantly immediately before and after each failure — indicating ControlMaster"
            " itself doesn't work reliably with this project's Windows/Git-for-Windows SSH"
            " client build against this HAOS SSH add-on"
        ),
        "scope_covered": (
            "tools/deploy.py: removed control_path(), _multiplex_args(), and"
            " close_control_master() entirely; ssh_args()/scp_args() no longer add any"
            " ControlMaster/ControlPath/ControlPersist options; main()'s try/finally wrapper"
            " (which existed only to call close_control_master()) removed, restoring the"
            " original flow. Replaced with command batching to address the original"
            " connection-count problem without depending on client-side multiplexing support:"
            " create_backup() combines the remote existence check and tar creation into one"
            " SSH call (was two); the new prep_remote_target() combines temp-file cleanup,"
            " legacy climate_advisor.bak.* directory removal (now via a single 'ls | xargs -r"
            " rm -rf' instead of one rm per matched directory), and mkdir -p into one SSH call"
            " (replacing clean_legacy_backups() plus the separate mkdir previously inside"
            " deploy_files()); do_rollback()'s restore-extract and temp-cleanup combined into"
            " one call. Reduces a typical full-deploy run from ~10-11 SSH/SCP connections to"
            " ~8. Verified live against the real HA host: both new combined commands"
            " (create_backup's tar-or-skip logic, prep_remote_target's cleanup+mkdir sequence)"
            " execute correctly and produce the expected results. docs/SSH-SETUP.md updated to"
            " describe the batching approach and explicitly document the ControlMaster"
            " reversion and why, so a future contributor doesn't re-attempt the same fix"
            " without knowing it was already tried and found unreliable here."
        ),
    },
    549: {
        "version_fixed": "0.5.44",
        "title": (
            "Deploying #543/#545/#547 hit a repeatable pattern: the first ~4 SSH connections"
            " tools/deploy.py opens in a single run succeed cleanly, then the 5th gets"
            " 'Connection reset by <host> port 22', and every connection after that times out"
            " for the rest of the run — the signature of the HA SSH add-on's rate-limit/"
            " brute-force protection ('Protection mode') blocking the source IP after too many"
            " connections in a short window, since deploy.py opens 6-8 separate connections"
            " per run (connection test, dir check, backup tar+download, cleanup, mkdir, file"
            " copy, restart, log check)"
        ),
        "scope_covered": (
            "tools/deploy.py: new control_path()/_multiplex_args() helpers add SSH"
            " ControlMaster/ControlPath/ControlPersist options to every ssh_args()/scp_args()"
            " invocation, so all of a run's connections tunnel through one real TCP connection"
            " instead of opening a fresh one each time (ControlMaster=auto — the first call"
            " creates the master, every later call with a matching ControlPath transparently"
            " reuses it; ControlPersist=10m keeps it alive across the run). New"
            " close_control_master() does a best-effort cleanup at the end of main(), wrapped"
            " in try/finally so it runs on every exit path including sys.exit() calls and"
            " --rollback. Verified via a manual reproduction that the exact multiplexed ssh"
            " command builds and executes correctly (produces a real control socket at"
            " ~/.ssh/sockets/deploy-<user>-<host>-<port>.sock). docs/SSH-SETUP.md: new"
            " Troubleshooting entry describing the rate-limit signature and pointing at this"
            " fix plus the add-on's own Protection mode setting as a second line of defense."
        ),
    },
    547: {
        "version_fixed": "0.5.43",
        "title": (
            "Deploying #543/#545 hit intermittent SSH connection resets/timeouts;"
            " confirming which SSH key tools/deploy.py would actually use required a manual"
            " investigation (reading ssh_args()'s HA_SSH_KEY handling, checking ~/.ssh/"
            " contents, running ssh -G by hand) instead of being visible from the tool itself"
        ),
        "scope_covered": (
            "tools/deploy.py: new resolve_ssh_identity() helper — resolves and prints which"
            " SSH identity file will be used (via ssh -G <user>@<host>, a local-only call, no"
            " network I/O) before test_ssh() attempts the actual connection. No change to"
            " connection/deploy logic itself; HA_SSH_KEY was already optional and per-user"
            " (git-ignored .deploy.env), never hardcoded in source. docs/SSH-SETUP.md:"
            " Troubleshooting section gained a note on Windows' two-ssh.exe-on-PATH situation"
            " (Git's MSYS build vs. the native OpenSSH client, which can resolve default"
            " identities differently depending on invocation context) recommending explicit"
            " HA_SSH_KEY as a zero-downside way to remove the ambiguity, plus a"
            " self-diagnostic (ssh -G user@host) users can run themselves."
        ),
    },
    545: {
        "version_fixed": "0.5.42",
        "title": (
            "Issue #543 (blocking file I/O called directly from async coordinator methods)"
            " passed review for months despite this project already having a CRITICAL-tier"
            " rule against blocking the event loop, because that rule (claude.md's"
            " Thread-Safety Requirements, added after Issue #376) was framed entirely around"
            " CPU-bound computation — blocking I/O was only mentioned as a 'don't"
            " double-wrap' assumption — and its enforcement (tests/test_executor_offload.py)"
            " was hardcoded to the 3 call sites from that earlier issue's specific function,"
            " with no way to catch a different function/pattern"
        ),
        "scope_covered": (
            "claude.md: Thread-Safety Requirements section broadened to explicitly cover"
            " blocking I/O (file reads/writes, os.replace/os.chmod, tempfile, sockets,"
            " subprocess) as equally in-scope as CPU-bound work, with a second heuristic"
            " question that doesn't depend on timing, ChartStateLog/#543 added as a second"
            " canonical example explaining why it was missed (no CPU-heavy math, blocking call"
            " hidden behind a helper method name), the 'I/O already wrapped' exemption"
            " reworded so it can't be misread as a blanket I/O pass, and a new Enforcement"
            " line cross-referencing the two mechanisms below. pyproject.toml: ruff's ASYNC"
            " lint category (flake8-async) enabled — verified zero existing violations across"
            " the whole repo except one pre-existing false-positive-for-purpose case in"
            " tools/take_screenshots.py (a standalone Playwright CLI script with its own"
            " private event loop, not Home Assistant's shared one), which got a scoped"
            " per-file-ignore with a comment explaining why. Catches ASYNC230/240/210/212/220"
            "/221/222/251 (blocking open/Path methods/sync HTTP/subprocess/sleep) written"
            " literally inline in any async function going forward — verified this would NOT"
            " have caught #543 itself, since #543's blocking call was hidden inside a helper"
            " object's method, not literally inline. tests/test_executor_offload.py: new"
            " registry-driven TestBlockingIOExecutorOffload class with a _BLOCKING_METHODS set"
            " of (attribute, method) pairs (_chart_log.load/save, _state_persistence.load/save,"
            " learning.load_state/save_state) checked against every async method in"
            " coordinator.py (not just a hardcoded list of call sites) — this is the part that"
            " actually would have caught #543. Verified both that it passes against the"
            " current (fixed) coordinator.py and, via a synthetic reproduction of the original"
            " pre-fix pattern, that it correctly flags a blocking call in an async method"
            " while correctly ignoring the same call in a sync method and a properly-wrapped"
            " async_add_executor_job reference."
        ),
    },
    543: {
        "version_fixed": "0.5.41",
        "title": (
            "HACS default-repository review (hacs/default#8117) flagged two blocking issues:"
            " chart_log.py's ChartStateLog.load()/save() performed synchronous file I/O"
            " (tempfile write + os.replace + os.chmod, or a blocking Path.read_text()) directly"
            " on Home Assistant's event loop from three coordinator.py call sites, and"
            " manifest.json declared iot_class: 'local_polling' for an integration whose AI"
            " features call the Anthropic cloud API"
        ),
        "scope_covered": (
            "coordinator.py: the synchronous self._chart_log.load() call in __init__() was"
            " removed and replaced with the first await"
            " self.hass.async_add_executor_job(self._chart_log.load) call inside"
            " async_restore_state() — safe because nothing between coordinator construction"
            " (__init__.py) and async_restore_state() reads chart_log entries. Both"
            " self._chart_log.save() call sites (the 30-min poll write in"
            " _async_update_data_impl, and the event-driven hvac_action-transition write in"
            " _async_thermostat_changed) now await"
            " self.hass.async_add_executor_job(self._chart_log.save), matching the existing"
            " executor-offload pattern already used for learning/state-persistence I/O"
            " elsewhere in this file. ChartStateLog.append() was left unchanged — it only"
            " mutates an in-memory list, no I/O. manifest.json iot_class corrected from"
            " 'local_polling' to 'cloud_polling'. Updated six test stub builders"
            " (test_event_log_persistence.py, test_grace_restart_behavior.py,"
            " test_solar_phase_periodic.py — missing _chart_log attribute plus positional"
            " executor-mock side_effect lists that assumed a fixed call count/order in"
            " async_restore_state(); test_hvac_session_detection.py — async_add_executor_job"
            " mock that didn't invoke the wrapped function, breaking a"
            " coord._chart_log.save.assert_called() assertion; test_startup_coalesce.py and"
            " test_override_automation_boundary.py — bare MagicMock() hass with no"
            " async_add_executor_job override, which would silently swallow the new awaited"
            " call via the pre-existing contextlib.suppress(Exception) around the chart-log"
            " write). Also fixed a documentation/process gap found while working this issue:"
            " claude.md's mandatory PR checklist never required a CHANGELOG.md entry, so it had"
            " silently gone unmaintained since v0.4.60 — added a checklist step for it and"
            " backfilled CHANGELOG.md with every release from v0.4.61 through v0.5.41 using"
            " real commit dates pulled from git history (three RELEASE_NOTES keys —"
            " 0.4.62, 0.4.75, 0.5.0 — were never actually committed as a live VERSION value and"
            " are folded into the version that actually shipped their content, 0.4.63 and"
            " 0.5.1 respectively; 0.5.31's exact originating commit is ambiguous due to a"
            " same-day multi-version merge sequence and is folded into 0.5.32 with that"
            " ambiguity noted inline)."
        ),
    },
    540: {
        "version_fixed": "0.5.40",
        "title": (
            "Whole-house fan sat idle for up to ~30 minutes after outdoor temperature reached"
            " parity with indoor in the evening — every existing nat-vent activation path"
            " required outdoor to be measurably below indoor, so air-movement/purge benefit at"
            " parity was structurally unreachable, not just delayed"
        ),
        "scope_covered": (
            "New opt-out (default on) 'soft-start' sub-mode: nat_vent_gate.py gains"
            " NatVentSoftStartGateInputs + decide_nat_vent_soft_start_gate(), a sibling to the"
            " existing full gate, not a modification of it. Gated on: WHF fan archetype only"
            " (whole_house_fan/both), door/window open, indoor above comfort_heat and"
            " comfort_cool, today's outdoor temp confirmed past its peak and declining (derived"
            " from coordinator._outdoor_temp_history, with a >=3-sample minimum guard against a"
            " thin post-restart buffer), outdoor <= indoor (parity, not the full gate's"
            " hysteresis-cleared delta), and only when the full bulk-cooling gate hasn't already"
            " cleared (the two gates never compete for the same activation). automation.py: new"
            " _nat_vent_soft_start qualifier flag alongside _natural_vent_active (same pattern as"
            " _grace_protects_override/_grace_active) — cleared at every"
            " _natural_vent_active=False site, wired into both reactivation call sites"
            " (idle-open/comfort-ceiling-during-grace and paused-by-door), with an upgrade check"
            " that clears the qualifier once the full gate independently clears. Reuses the"
            " existing exit hierarchy unchanged. Status card shows 'nat-vent — soft-start"
            " (purge)' distinctly (no new card); nat_vent_soft_start_entered event rendered in"
            " the Activity Report."
        ),
    },
    538: {
        "version_fixed": "0.5.39",
        "title": (
            "Next User Action card narrated automation-mechanism state ('Free cooling is"
            " active.') instead of answering what the occupant should do, duplicating the"
            " Status card and violating the Issue #527 card ontology"
        ),
        "scope_covered": (
            "coordinator.py: _compute_next_action() had two branches (HOT-day fallback and the"
            " WARM/MILD/COOL cooling-needed path) that returned 'Free cooling is active.' when"
            " ae._natural_vent_active or ae._economizer_active was already True. Both now return"
            " '-' — there is nothing for the occupant to do while free cooling is already"
            " handling comfort, and the Status card already surfaces the nat-vent/economizer"
            " mechanism state. Audited the rest of _compute_next_action() for other"
            " mechanism-flag leaks (is_paused_by_door, _grace_active, _manual_override_active,"
            " _override_confirm_pending) — none found outside the diagnostic log line, which is"
            " not user-facing."
        ),
    },
    534: {
        "version_fixed": "0.5.38",
        "title": (
            "Next Automation card's 'outdoor no longer helping' text could be misread as a"
            " present-tense claim even though it was always an accurate forecast for a"
            " separately-displayed future time; mild-day briefings never used the"
            " weather-forecast-based window close time warm days already had"
        ),
        "scope_covered": (
            "Investigated as a possible live nat-vent control defect (reported symptom: nat-vent"
            " session inactive for ~2.5 hours overnight while conditions looked favorable)."
            " Extending the log window through the predicted cutoff time and viewing the"
            " reporter's screenshot directly confirmed the forecast (9:00 AM) was accurate"
            " (outdoor actually caught up to indoor ~9:10-9:23 AM) and nat-vent reactivated on"
            " its own the moment indoor cleared the comfort floor, exactly as designed — no"
            " control-path defect. coordinator.py: _compute_next_automation_action() now folds"
            " the predicted time into the candidate action string itself (e.g. 'Outdoor will"
            " stop helping around 9:00 AM — close windows') instead of relying on the separate"
            " Automation Time card alone, removing the present-tense misread. briefing.py:"
            " generate_briefing() now computes mild_events via _derive_warm_day_events() for"
            " MILD days (mirroring the existing warm-day pattern) and threads it into both"
            " _generate_tldr_table() and _mild_day_plan(), so mild-day close times use the ODE"
            " forecast when available, falling back to the static classifier hour otherwise —"
            " closing a gap where docs/08-COMPUTATION-REFERENCE.md §6d already claimed this"
            " behavior existed but it never had been wired up."
        ),
    },
    530: {
        "version_fixed": "0.5.37",
        "title": (
            "Whole-house-fan-off grace was killed by the Issue #508 orphaned-grace watchdog"
            " within ~1 event-loop tick almost universally (not just the RF-timer case"
            " originally reported); an 8h RF-timer boundary compounded this into a burst of"
            " contradictory grace/override decisions; morning wake-up could separately arm"
            " HVAC with windows still open"
        ),
        "scope_covered": (
            "Three fixes, automation.py + coordinator.py. (1) ROOT CAUSE, general case:"
            " coordinator._check_orphaned_grace() (Issue #508) inferred 'orphaned' purely"
            " from _manual_override_active/_fan_override_active both being False — also the"
            " normal, by-design shape of fan-off/physical-drift-correction/window-close-"
            " resume/dashboard-resume/nat-vent-exit-resume grace, none of which ever touch"
            " those flags. Fixed by making _start_grace_period(trigger=...) set a new"
            " self._grace_protects_override = trigger in _GRACE_TRIGGERS_PROTECTING_OVERRIDE"
            " (frozenset of exactly 'fan_manual_override'/'override_confirmed', the only two"
            " triggers that correspond to a real override) — centralized classification via"
            " the trigger string every callsite already passes, not a new parameter threaded"
            " through all 7 call sites. _check_orphaned_grace() now additionally requires"
            " _grace_protects_override; _cancel_grace_timers() resets it. This restores"
            " Issue #359's fan-off protection for ANY whole-house-fan-off, not only the"
            " RF-timer scenario originally reported, while leaving the original Issue #508"
            " protection fully intact for the three genuine override-driven triggers."
            " (2) RF-timer-specific refinement: _on_grace_expired() snapshots whether the"
            " expiring grace was RF-timer-linked (_fan_remote_timer_hours, read before"
            " clear_manual_override() wipes it) and, if so, arms"
            " self._timer_boundary_settle_until for TIMER_BOUNDARY_SETTLE_SECONDS (const.py,"
            " 120s — the real observed software/hardware gap was ~11s). on_fan_turned_off()"
            " checks this window first: a fan-off inside it routes straight to"
            " _exit_nat_vent() instead of starting a second, independent grace at all —"
            " stronger than (1) alone for this specific, predictable case, since CA already"
            " knows the timer is about to complete. (3) _release_whf_and_reclassify()"
            " (called from clear_fan_override(), itself called by handle_morning_wakeup()"
            " via clear_manual_override()) gained a _natural_vent_active guard: no longer"
            " releases WHF HVAC suppression while a nat-vent session is still considered"
            " active, even if the fan happens to be physically off at that instant — closes"
            " an ordering bug where wake-up's own DEFER_NAT_VENT gate decision (computed"
            " moments earlier) was silently undercut by this side effect before the"
            " following comfort-band write ran. Also threaded a `trigger` parameter through"
            " reconcile_fan_on_startup() (previously hardcoded 'startup reconcile' in its"
            " reason strings for all 4 call sites — ha_restart, backstop_30min,"
            " thermostat_state_change, post_grace_expiry — reading as a phantom HA restart"
            " when none occurred). 13 new regression tests across"
            " tests/test_whole_house_fan_hvac_suppression.py, tests/test_grace_stuck.py, and"
            " tests/test_fan_command_guard.py, each verified to fail without its"
            " corresponding fix via a stash/revert check. docs/grace-periods-spec.md updated:"
            " Orphaned Grace Self-Heal section rewritten for the corrected scope, new"
            " RF-Timer Boundary Settle Window section, Fan-Off Grace and Shared"
            " Scheduled-Band Gate sections cross-referenced."
        ),
    },
    528: {
        "version_fixed": "0.5.36",
        "title": (
            "Warm/mild-day briefing window-close and reopen times could be implausibly"
            " wrong (e.g. reopen shown hours before the day's actual heat peak); Next"
            " Automation card had no predictive events at all — only fixed-schedule ones"
        ),
        "scope_covered": (
            "temperature.py: new find_temperature_crossing(indoor_curve, outdoor_curve,"
            " comparator, after=None) — aligns two {ts,temp} forecast curves by matching"
            " ISO timestamp (not list position) before evaluating comparator(ts, outdoor,"
            " indoor); returns the first matching timestamp or None. briefing.py:"
            " _derive_warm_day_events() nat_vent_cutoff/recovery_time (and transitively"
            " any_nat_vent_window) now go through this function instead of a zip()-by-index"
            " pairing that silently mispaired the two curves whenever they were built with"
            " different 'now' filter boundaries or at different times (indoor cached from"
            " the last 30-min cycle, outdoor rebuilt fresh per briefing) — confirmed live via"
            " production logs and a git-blame trace to the original #518 commit (introduced"
            " whole-cloth, never modified since — an original design gap, not a regression)."
            " Added recovery_time to the existing WarmDayEvents debug log line (previously"
            " the one field in that dict not logged). coordinator.py:"
            " _compute_next_automation_action() gained three new candidate types: (1)"
            " nat-vent/WHF start prediction via decide_nat_vent_gate()/NatVentGateInputs"
            " (nat_vent_gate.py) — the real, already-production-validated activation gate,"
            " not compute_nat_vent_cycling_band() (that function describes the fan's cycling"
            " band once already active, a materially different formula with no ceiling-"
            " margin/fan-mode awareness) — gated on a door/window already open or grace,"
            " matching check_natural_vent_conditions()'s own precondition; (2) the same"
            " WARM/MILD warm-day events above, surfaced as 'Close windows'/'AC turns on'/"
            "'Reopen windows' candidates; (3) HOT-day window_opportunity_morning/evening"
            " (classifier.py, already-computed static fields) surfaced as fixed-schedule"
            " candidates for the first time. docs/08-COMPUTATION-REFERENCE.md: §7 WARM row"
            " now discloses the same ODE-override caveat the MILD row already had; new §9f."
        ),
    },
    527: {
        "version_fixed": "0.5.35",
        "title": (
            "Status, Next User Action, and Next Automation cards independently narrated the"
            " same automation-mechanism fact (paused by door/window, grace period, override"
            " confirming) with three different wordings, instead of each card answering its"
            " own question"
        ),
        "scope_covered": (
            "coordinator.py: _compute_next_action() (Next User Action card) — deleted the"
            " early-return block that read _override_confirm_pending/_manual_override_active/"
            "_grace_active/is_paused_by_door and returned mechanism text; this had been"
            " pre-empting the function's own real comfort guidance (window/fan direction"
            " checks, heating/cooling-needed logic) further down, which is now always"
            " reachable regardless of automation mechanism state. Also trimmed redundant"
            " 'the AC/heater/automation is handling it' tails and an 'Automation active —'"
            " lead-in that restated the Status card's job inside the comfort-guidance card,"
            " and replaced the flat away/vacation sentences with a small date-seeded rotating"
            " pool (_AWAY_ACTION_MESSAGES/_VACATION_ACTION_MESSAGES via _pick_daily_line())."
            " _compute_next_automation_action() (Next Automation + Automation Time cards) —"
            " deleted the 'Windows open as recommended' / 'Waiting — HVAC paused' / 'Grace"
            " period active' / 'Evaluating door/window sensors' early returns so the function"
            " always falls through to the real schedule-candidate list (briefing/wake/bedtime/"
            "pre-cool); added INFO-level entry and outcome logging (this function previously"
            " had none). api.py: wired pause_suppressed_classification and a new"
            " pause_suppressed_classification_text into ClimateAdvisorStatusView — this field"
            " existed in coordinator.get_serializable_state() (Debug tab) and was flagged as a"
            " known gap in KNOWN_FIXES[367] but had never reached the actual Status API"
            " response, so index.html's check of it was unreachable dead code; the text itself"
            " also moved from a frontend-hardcoded literal to this new backend field."
            " CLAUDE.md + docs/08-COMPUTATION-REFERENCE.md §9e: documented the four-card"
            " ontology (Status=state+why, Next User Action=comfort action only, Next"
            " Automation=plan only, Automation Time=when) as a guardrail against a repeat —"
            " this is the second time this duplication class has appeared (first: #495, §9d,"
            " which patched two functions to stay in sync rather than removing the"
            " duplication; that patch didn't hold when a third function grew the same problem"
            " independently)."
        ),
    },
    523: {
        "version_fixed": "0.5.34",
        "title": (
            "HVAC could arm against an already-open window right after an HA restart,"
            " instead of staying paused like it does at every other point in the day"
        ),
        "scope_covered": (
            "coordinator.py: _do_startup_coalesce() hand-rolled its own incomplete copy of"
            " the nat-vent gate purely to decide whether to call handle_door_window_open() at"
            " all — a third parallel copy of threshold logic already consolidated once for"
            " #400/#402. When that pre-check declined nat-vent (e.g. outdoor warmer than"
            " indoor), handle_door_window_open() — the only function that sets"
            " _paused_by_door — was never called, so an open window at restart could fall"
            " through to an unsuppressed apply_classification(). Fixed by deleting the"
            " duplicate gate and delegating unconditionally to handle_door_window_open()"
            " whenever a sensor is open, letting it make the nat-vent-vs-pause decision with"
            " its own complete, single-source-of-truth gate. automation.py:"
            " handle_door_window_open()'s pause branch also never set _paused_by_door when"
            " the current HVAC mode was already 'off' — exactly the state found after a"
            " restart where the window was already open before HA restarted (the sibling"
            " _re_pause_for_open_sensor(), added whole in #47, already got this right;"
            " handle_door_window_open() predates #47 and was never brought in line)."
            " Extracted a shared _pause_for_door_window() helper (mirroring the #491"
            " precedent) used by both call sites, so the off/not-off branch exists in one"
            " place going forward; both branches now also emit an activity event (the"
            " off-mode case previously emitted none in either function). Also guarded"
            " reconcile_fan_on_startup()'s no-fan branch (restore_hvac=not"
            " self._paused_by_door) so a stranded WHF _pre_fan_hvac_mode from before the"
            " restart cannot silently restore HVAC right after the pause fix takes effect —"
            " the same invariant #418 established for the nat-vent-exit path."
            " check_natural_vent_conditions()'s idle-open re-evaluation loop (#244/#402/#504)"
            " needed a companion _paused_with_hvac_already_off flag to keep re-checking nat-"
            " vent opportunity for a sensor open with HVAC genuinely idle, since _paused_by_door"
            " alone is no longer sufficient to mean 'HVAC was actively interrupted.'"
        ),
    },
    524: {
        "version_fixed": "0.5.33",
        "title": (
            "WHF status card never showed remote speed; Activity Report couldn't tell a"
            " remote-armed override from a generic toggle"
        ),
        "scope_covered": (
            "coordinator.py: fan_remote_speed/fan_remote_timer_hours/fan_remote_timer_ends"
            " existed only inside get_debug_state() (a debug-endpoint-only method, never"
            " called by _async_update_data_impl()) — so coordinator.data, and therefore"
            " api.py's main status view and the dashboard's WHF card, never had these keys"
            " in production, regardless of firmware/remote activity. Extracted the"
            " computation into _compute_fan_remote_status_fields(), called from both"
            " get_debug_state() and _async_update_data_impl(), following the exact"
            " precedent compute_nat_vent_cycling_band() established for this same class of"
            " bug (Issue #400/#402). ai_skills_activity.py: _render_fan_manual_override()"
            " now appends the remote speed/timer context when"
            " automation.py::handle_fan_manual_override()'s remote_speed/remote_timer_hours"
            " kwargs are present, instead of silently dropping them; a plain"
            " thermostat-detected override (neither field set) renders unchanged."
            " docs/activity-report-table.md gained catalog rows for fan_manual_override and"
            " fan_speed_observed (both previously undocumented despite being registered"
            " EVENT_RENDERERS entries)."
        ),
    },
    518: {
        "version_fixed": "0.5.32",
        "title": "Warm/windows-day briefing text could contradict itself in several places",
        "scope_covered": (
            "briefing.py: header window-close time (_generate_tldr_table) and the"
            " conversational body's window-close time (_warm_day_plan via"
            " _derive_warm_day_events) were computed from two independent sources — a"
            " classifier constant vs. a live ODE-forecast crossover — and could disagree;"
            " now computed once in generate_briefing() and passed to both as `warm_events`."
            " The AC-safety-net sentence in _warm_day_plan used to independently promise a"
            " fixed clock time with no awareness of door/window state, contradicting the"
            " real automation guard (automation.py apply_classification()'s DEFER_PAUSED"
            " branch, which suppresses AC the whole time a window is open) and duplicating"
            " _fresh_air_section()'s already-correct, debounce-aware version of the same"
            " fact — removed the duplicate, kept one window-state-conditioned statement."
            " The 'reopen windows... I'll turn off the AC' sentence now only claims the"
            " AC-off action when the predicted ceiling breach occurred before the recovery"
            " time (_derive_warm_day_events now also returns `recovery_time`). The"
            " adaptive-thermal-timing footer in _tonight_preview() used to fire whenever"
            " `adaptive_thermal_active` was true regardless of tonight's actual hvac_mode —"
            " now also requires `hvac_mode in ('heat', 'cool')`, so it can't contradict a"
            " header that says 'No setback'. Dropped 'no action needed' phrasing from"
            " briefing.py and reworded coordinator.py's dashboard status fallback string."
            " Added tools/briefing_review.py — a deterministic day_type x hvac_mode x"
            " setback-active scenario matrix with coherence assertions for this bug class."
            " Added an 8th investigator context block (LAST BRIEFING, ai_skills_context.py)"
            " so the AI investigator can review the rendered briefing text itself. Authored"
            " docs/briefing-spec.md's previously-stub sections and reconciled"
            " docs/04-BRIEFING-EXAMPLES.md's warm-day example with 08-COMPUTATION-REFERENCE.md,"
            " and corrected every other example's stale 5-minute debounce reference to the"
            " actual 10-minute default (constant changed in Issue #504)."
        ),
    },
    519: {
        "version_fixed": "0.5.31",
        "title": ("Climate Advisor now detects and respects QuietCool remote speed changes, not just timer presses"),
        "scope_covered": (
            "Designed via a shaping (ontology-first) session followed by three review passes"
            " (consolidation/dedup, blast-radius/scope, behavioral refinement + observability"
            " audit), all at the user's explicit request. Firmware"
            " (gunkl/quietcool-house-fan, component.yaml): new purely-additive"
            " text_sensor.quietcool_speed reporting the firmware's already-internally-tracked"
            " current speed, fed from all three status-beacon byte families (speed/timer/power)"
            " that carry the embedded 0x20 speed-context bit — chosen over a companion-event"
            " design because a press (event.quietcool_remote) and a reading (ambient state) are"
            " different kinds of information, and firing synthetic companion events risked"
            " landing on the same HA state timestamp as a genuine confirmation, which the"
            " existing _last_fan_remote_event_ts dedup guard (Issue #495) could silently"
            " swallow. CA-side (fan_status.py, automation.py, coordinator.py): new"
            " parse_remote_speed_event(); handle_fan_manual_override() gains an optional"
            " remote_speed kwarg (same guarded-overwrite idiom as remote_timer_hours); new"
            " handle_fan_speed_observed() for the comfort-only path (deliberately a separate"
            " function, not a flag inside handle_fan_manual_override(), since that function's"
            " whole contract is 'arm an override'); coordinator burst-combining"
            " (_PendingFanRemoteBurst, REMOTE_BURST_WINDOW_SECONDS=1.5s grounded in the"
            " firmware's own documented protocol timing, not an arbitrary guess) so a single"
            " physical interaction touching both speed and timer fields (transmitted as"
            " separate packets moments apart) produces ONE decision, not two. Classification:"
            " a timer selection (with or without speed) is always an override; a bare speed"
            " press is an override only if the fan was NOT already running BEFORE the"
            " interaction started (was_running_before, snapshotted once at burst-open time —"
            " a self-review catch during implementation found and fixed a real timing bug"
            " where an earlier draft would have re-read physical state at flush time instead,"
            " which would misclassify nearly every genuine off->on override as comfort-only"
            " once the fan had already turned on mid-burst; the fix and the regression it"
            " catches are both covered by a dedicated test). Ambient speed-sensor discovery"
            " (_resolve_fan_remote_speed_sensor) uses HA's entity/device registry, keyed off"
            " the already-configured fan_remote_entity — zero new user-facing config; this is"
            " the first feature in this codebase using entity/device registry, which also"
            " required fixing a real ha_stubs.py gap (the entity_registry/device_registry"
            " submodules weren't pinned onto the homeassistant.helpers parent mock, the same"
            " failure mode already documented for homeassistant.config_entries). A negative"
            " discovery result is never cached (registry can populate asynchronously at"
            " startup). Dashboard (index.html) shows the current speed only when known,"
            " omitted (never 'unknown speed') otherwise, per this project's existing"
            " status-card conventions. New ai_skills_activity.py renderer for the"
            " fan_speed_observed event (required by the #330 event-renderer-coverage"
            " guardrail)."
        ),
    },
    510: {
        "version_fixed": "0.5.30",
        "title": (
            "WHF status card could show 'nat-vent active, fan idle' for hours while the"
            " fan was physically running; related 'active (unconfirmed)' status could also"
            " persist indefinitely"
        ),
        "scope_covered": (
            "Investigated end-to-end (docs -> live logs/entity history -> code) per the"
            " mandatory investigation protocol; confirmed via live data on the reporting"
            " install (fan_state_feedback=True with a dedicated power-detection entity) that"
            " the physical fan was genuinely toggling every ~2-7 minutes for over an hour"
            " while the card showed stale info, and the '_fan_active=True but physical"
            " state=off' WARNING recurred 138 times over 24+ hours (also found firing 6x per"
            " invocation, unrelated log-noise bug, also fixed). Root-caused to two coupled"
            " defects in coordinator.py: (1) _async_fan_entity_changed() only requested an"
            " immediate display refresh when a manual override was already active (mirroring"
            " the Issue #489 door/window pattern too narrowly) -- hoisted to fire"
            " unconditionally on every genuine physical transition; (2) _compute_fan_status()/"
            "_compute_whf_status() checked the _natural_vent_active session flag BEFORE the"
            " existing physical-state ground-truth fallback, so a stale flag blocked the truth"
            " check entirely -- ground truth is now read once (preserving the original lazy-"
            "read property the override_active+fan_active fast path already relied on, kept"
            " via a memoized closure) and consulted inside the nat-vent branch too, reusing the"
            " existing 'running (untracked)' status value rather than adding a new one. The"
            " related 'active (unconfirmed)' status (a DIFFERENT direction of disagreement --"
            " _fan_active=True but physical=off) had the same 'leads with the wrong word'"
            " defect; fixed by reusing the existing _is_recent_fan_command(threshold_seconds="
            "30.0) helper (already used nearby for the same purpose) so the transient ~30s"
            " post-command window still correctly shows 'active (unconfirmed)', but a settled"
            " disagreement now resolves to 'inactive'. Two secondary automation-bookkeeping"
            " fixes bundled in the same investigation: _async_post_grace_fan_reconcile()'s"
            " outer gate was computed from the thermostat's own fan_mode/hvac_action"
            " unconditionally, silently skipping reconciliation entirely for WHF-only installs"
            " (fan physically separate from the thermostat) -- now uses the same archetype-"
            "aware _derive_thermostat_fan_running_for_reconcile() helper already used for the"
            " inner call. And: the pre-existing Issue #359 Fix D periodic untracked-fan"
            " backstop (coordinator.py, 30-min cadence) now ALSO self-corrects the stale-nat-"
            "vent-flag direction with zero additional code, as a direct consequence of the"
            " _compute_fan_status() fix above (_is_untracked is derived from that function's"
            " return value) -- a separate 2-tick-confirm pure-decision-function mechanism was"
            " drafted for this and deliberately discarded once this was discovered, in favor of"
            " reusing the already-shipped, already-tested mechanism."
        ),
    },
    511: {
        "version_fixed": "0.5.29",
        "title": (
            "Weather-service-only installs' outdoor temp reading was a stale point-sample,"
            " not a live value, causing chart/automation errors of up to ~4°F during ramps"
        ),
        "scope_covered": (
            "Confirmed via live chart_log cross-correlation analysis (SSH-pulled, ~107 days"
            " of data) that for weather-service installs with no dedicated sensor (e.g."
            " Met.no's weather.forecast_home), the 'temperature' attribute used as 'Actual"
            " Outdoor' is a step function that plateaus 30-90 min before updating — a"
            " ~50-55 min effective phase lag against true conditions, producing a +3-4°F"
            " morning-warming bias and a -2°F evening-cooling bias. _get_outdoor_temp()"
            " now interpolates linearly between the two nearest hourly-forecast points"
            " (new _interpolate_hourly_outdoor_temp(), built on a new shared"
            " _parse_forecast_entries() parsing helper also used by the two pre-existing"
            " forecast-reading functions) instead of trusting the live attribute directly,"
            " unconditionally for weather_service source — sensor/input_number installs are"
            " untouched. The estimate now refreshes every 5 minutes via the existing thermal-"
            " sample timer (new _refresh_weather_service_outdoor_temp(), gated to skip"
            " entirely for sensor/input_number sources) instead of only once per 30-min"
            " cycle, and a new single _apply_outdoor_temp() propagation method consolidates"
            " what were previously 4 independently-written touch points (30-min cycle x2"
            " lines, daily briefing, and the new 5-min tick) across the coordinator and"
            " automation engine mirror. _async_end_of_day()'s midnight reset now"
            " immediately re-fetches the hourly forecast instead of leaving up to a ~30-min"
            " gap where interpolation degrades nightly to the pre-fix behavior. All"
            " downstream consumers (automation gating, chart 'actual outdoor' values,"
            " classification windows-gate, outdoor sensor entity, api.py dashboard payload)"
            " inherit the fix automatically since they all read the same"
            " _last_outdoor_temp/coordinator.data fields this change updates — no separate"
            " code changes were needed in automation.py, sensor.py, or api.py."
        ),
    },
    508: {
        "version_fixed": "0.5.28",
        "title": (
            "Cancelling a fan override from the dashboard didn't cancel grace, didn't"
            " force reconciliation, and left no activity trail"
        ),
        "scope_covered": (
            "Three root causes traced to a real production incident (RF-remote WHF override,"
            " cancelled 7 minutes in). (1) ClimateAdvisorCancelFanOverrideView.post() (api.py)"
            " called only clear_fan_override(), never _cancel_grace_timers() — confirmed via"
            " git log to be an original design gap from Issue #79 (2026-03-30), not a"
            " regression; the sibling ClimateAdvisorCancelOverrideView (Issue #41) has always"
            " called both. _grace_active stayed True for the rest of the original grace"
            " duration (up to 8 hours for an RF-remote timer), so the dashboard's next-action"
            " text kept saying 'Grace period active' long after the override was gone."
            " (2) Neither cancel view forced the same reconciliation a natural grace expiry"
            " always performs (_post_grace_fan_check_callback -> reconcile_fan_on_startup(),"
            " _request_refresh_callback()); the fan-cancel view additionally never scheduled"
            " apply_classification() the way the thermostat-cancel view already did — resuming"
            " automation depended on an incidental unrelated event (a door/window debounce)"
            " firing shortly after, which is what made the production incident look like it"
            " 'worked' by luck. (3) clear_fan_override() never emitted an activity-log event,"
            " so a fan-only cancellation was invisible in the Activity Report. Fix: a single"
            " new AutomationEngine.cancel_override() method is the canonical 'deliberate"
            " cancel' operation (clear override + cancel grace + fan-reconcile + coordinator"
            " refresh + activity event) used by both dashboard buttons and the internal"
            " adopted_matching_decision path, replacing hand-composed call pairs that had"
            " already drifted out of sync once. _on_grace_expired()'s three inline 5-line"
            " grace-flag resets were deduped to call the existing _cancel_grace_timers()."
            " coordinator._check_orphaned_grace() (extended from the existing Issue #321"
            " stuck-grace watchdog, same ~30s cycle) self-heals the mirror condition"
            " (grace_active=True with no override active) as defense-in-depth beyond the two"
            " known call sites. _render_stuck_grace_recovered() now distinguishes the two"
            " watchdog shapes so the new condition doesn't render a misleading 'expired' label"
            " when the grace timer was actually still validly scheduled in the future."
        ),
    },
    505: {
        "version_fixed": "0.5.27",
        "title": "Vacation deep setback armed once at mode-entry, never re-applied for the rest of the trip",
        "scope_covered": (
            "apply_classification()'s ScheduledBandGate.DEFER_OCCUPANCY branch"
            " (automation.py) special-cased VACATION to log 'deep setback preserved' and"
            " return, never calling handle_occupancy_vacation() — unlike the AWAY branch,"
            " which already called handle_occupancy_away() to actively re-arm the setback"
            " every 30-minute cycle. Confirmed via live production logs from a real"
            " 5-day vacation (2026-07-14 to 2026-07-19): the 82F deep setback"
            " (Set temperature to 82F — vacation mode — deep setback band) fired exactly"
            " once, at mode entry, and never again — indoor temp sampled throughout the"
            " trip held at 73-75F (normal comfort), not the setback ceiling, the entire"
            " time. A manual override (e.g. for cleaners) that later cleared left the"
            " thermostat at the override's setpoint indefinitely rather than restoring"
            " the vacation setback. Original design gap from Issue #85 (commit 0959140),"
            " unaffected by the #498 gate-sharing refactor. Fix calls the existing,"
            " already-correct handle_occupancy_vacation() from the same branch, mirroring"
            " AWAY exactly — net reduction in special-cased logic, no new functions."
            " Same fix applied to two sibling gaps with the identical shape:"
            " handle_bedtime() and handle_pre_cool()'s own DEFER_OCCUPANCY branches"
            " previously skipped for both AWAY and VACATION with no re-push at all,"
            " relying on apply_classification()'s 30-min cycle as an implicit backstop —"
            " a grace-period expiry landing inside the sleep window routes to"
            " handle_bedtime() instead, so that backstop didn't apply there (affecting"
            " AWAY too, not just vacation, in that narrower window). Home and guest mode"
            " were never affected — decide_scheduled_band_gate() only routes AWAY/"
            "VACATION through DEFER_OCCUPANCY; guest has no handle_occupancy_guest() at"
            " all and flows through the normal comfort-band path every cycle like home."
            " Two existing golden simulation scenarios"
            " (away_morning_wakeup_skipped_assertion.json,"
            " morning_wakeup_skipped_away_occupancy.json) had their bedtime-cycle"
            " assertion updated from bedtime_setback_skipped to setback_applied(79F) to"
            " reflect the bedtime sibling fix genuinely re-confirming the away setback"
            " at bedtime now, not just skipping. vacation_mode_full_lifecycle.json's"
            " mid-vacation assertion reason text was corrected: it was worded as if it"
            " proved active reapplication, but the harness's most-recent-decision-at-or-"
            "before lookup semantics meant it could only ever prove no-drift, which is"
            " why this scenario didn't catch the bug despite its name — the new"
            " vacation_occupancy_override_cleared.json pending scenario is what actually"
            " proves active reapplication, by perturbing the setpoint with a manual"
            " override before asserting the setback returns."
        ),
    },
    504: {
        "version_fixed": "0.5.26",
        "title": "Rapid door/window sensor bounce instantly re-triggered nat-vent/WHF reactivation with no settle time",
        "scope_covered": (
            "check_natural_vent_conditions()'s idle_open reactivation branch (automation.py,"
            " Issue #244, widened #402) reacted to a monitored sensor's raw instantaneous"
            " open state with zero debounce — verified via real HA logs that a single"
            " contact sensor group genuinely bounced open/closed 7 times in 28 seconds,"
            " and each open transition independently re-armed the whole-house fan before"
            " the very next close tore it back down via _exit_nat_vent(), producing"
            " duplicate 'Fan activated' Activity Report rows and an audible on/off flap."
            " CONF_SENSOR_DEBOUNCE was configured but only ever gated the pause decision"
            " (handle_door_window_open(), which only runs once its debounce timer actually"
            " expires) — never this reactivation path. Fix reuses the coordinator's"
            " existing per-entity debounce-timer tracking (_door_open_timers) via a new"
            " _sensor_debounce_pending_callback: idle_open now only reactivates once no"
            " currently-open monitored sensor still has a pending debounce timer, i.e."
            " once it has genuinely settled open. No second debounce/lockout concept was"
            " added (would repeat the #402 duplication failure mode). Re-read Issue #244"
            " directly and confirmed its actual scenario (a sensor open all day, outdoor"
            " cooling later) is unaffected: that sensor's debounce timer has long since"
            " resolved by the time #244's case occurs. Default CONF_SENSOR_DEBOUNCE bumped"
            " 300s to 600s for new installs (existing installs with an explicit value are"
            " untouched); its config description rewritten to describe governing pause/resume"
            " and nat-vent/WHF/HVAC-fan engage/exit, not just 'HVAC pauses'. Also fixed:"
            " sensor_all_closed's payload now carries fan_device so"
            " _render_sensor_all_closed() can show 'whf: on->off' on the nat-vent-ending"
            " row (previously blank, even though the fan really did turn off — Issue #411's"
            " emit_event=False suppression on _exit_nat_vent()'s own fan_deactivated event"
            " meant this was the only place that transition could surface, and it wasn't"
            " being passed through)."
        ),
    },
    485: {
        "version_fixed": "0.5.25",
        "title": "occupancy_setback Activity Report entries spammed every ~5 minutes while occupancy was unchanged",
        "scope_covered": (
            "Removed 'occupancy_setback' from ai_skills_activity.py's _NO_DEDUP exclusion"
            " set, so build_event_timeline_table()'s existing consecutive-same-type-row"
            " collapse mechanism (already used for nat_vent_fan_on/off,"
            " occupancy_setback_suppressed_paused, etc.) now applies to it — repeated"
            " identical occupancy_setback rows collapse to a single '×N (first-last)' row"
            " with the last event's Settings cell preserved. Root cause traced: occupancy"
            " transitions are already correctly event-driven"
            " (coordinator._async_occupancy_toggle_changed, fires once per real transition"
            " and no-ops otherwise) — the repeats came from apply_classification()'s"
            " occupancy-defer gate (automation.py) unconditionally re-calling"
            " handle_occupancy_away()/handle_occupancy_vacation() on every automation"
            " cycle, which runs far more often than the documented 30-min interval due to"
            " a self-perpetuating revisit chain (any HVAC action schedules a 5-min"
            " revisit with no idempotency check). handle_occupancy_away/vacation() emit"
            " occupancy_setback on every call with no dedup of their own — unlike"
            " comfort_band_applied, which got a 10-min time-windowed dedup in #444."
            " Confirmed via the cancel_override_then_resume golden scenario (which relies"
            " on a genuine second occupancy_setback firing while occupancy mode is"
            " unchanged, separated by intervening override events) that the fix does not"
            " suppress legitimate re-applies — only truly consecutive identical rows"
            " collapse."
        ),
    },
    498: {
        "version_fixed": "0.5.24",
        "title": "Dashboard grace-expiry display gap; bedtime/wakeup/pre-cool gate logic duplicated and drifted",
        "scope_covered": (
            "(1) Dashboard: _compute_next_automation_action()/_compute_next_action() in"
            " coordinator.py now append a formatted end-time + remaining-minutes suffix"
            " (via new _format_grace_remaining() helper reading ae._grace_end_time) to the"
            " 'Grace period active' text — previously shown with no time information at all."
            " (2) Shared gate: new desired_state.ScheduledBandGate enum +"
            " decide_scheduled_band_gate() pure function is now the single place the"
            " occupancy/manual-override/paused-by-door/nat-vent-or-WHF-ownership checks"
            " live, reused by apply_classification(), handle_bedtime(),"
            " handle_morning_wakeup(), and handle_pre_cool() — each handler keeps its own"
            " distinct band computation and telemetry, only the gate-checking is unified."
            " (3) The reported bug: handle_morning_wakeup()'s independent copy of these"
            " checks was missing the fan-override guard entirely (handle_bedtime()'s copy"
            " had it) — wake-up unconditionally deactivated a manually-overridden"
            " whole-house fan and armed AC, defeating the _whf_owns_hvac() choke-point"
            " guard the write is supposed to respect. Confirmed live: 06:30 wake-up killed"
            " a manual WHF override and armed cool, self-correcting a cycle later only"
            " because an unrelated nat-vent re-evaluation happened to run right after."
            " (4) Related, more subtle bug found while testing the wake-up fix:"
            " clear_manual_override() unconditionally clears _fan_override_active as a"
            " side effect (via clear_fan_override()), and both handle_bedtime() and"
            " handle_morning_wakeup() call it BEFORE their fan-deactivation check — so"
            " reading the live attribute afterward always saw it already cleared, silently"
            " defeating the guard even when written correctly. This means"
            " handle_bedtime()'s equivalent guard was never actually effective either,"
            " confirmed by a test whose own name documented the bug as intended behavior"
            " ('test_bedtime_clears_fan_override_then_deactivates', now corrected). Both"
            " handlers now snapshot _fan_override_active into a local BEFORE calling"
            " clear_manual_override(), matching the capture-before-clear pattern already"
            " used elsewhere in _confirm_override()-adjacent code."
            " (5) Finding #11: none of the three scheduled handlers checked"
            " _paused_by_door at all (unlike apply_classification()/"
            " handle_occupancy_away()/handle_occupancy_vacation(), which all do) — a"
            " door/window pause active at exactly sleep_time/wake_time/the pre-cool"
            " trigger was not protected. All three now defer via the shared gate."
            " (6) handle_bedtime()'s own nat-vent-continuation gate (an inline"
            " outdoor-vs-sleep_cool comparison) is deleted — it could hand off to AC"
            " prematurely even while outdoor was still well below indoor and the fan was"
            " doing useful work. Bedtime now defers entirely to an active nat-vent/WHF"
            " session; the engine's own per-tick check_natural_vent_conditions() (outdoor-"
            " reversal exit) and nat_vent_temperature_check()'s sleep-window cycling"
            " target already manage the session's lifetime correctly without help from"
            " bedtime."
        ),
    },
    495: {
        "version_fixed": "0.5.23",
        "title": "Manual/remote whole-house-fan-on left HVAC armed; QuietCool remote reliability + status display",
        "scope_covered": (
            "(1) HVAC suppression on WHF-on (the core bug): _pre_fan_hvac_mode capture +"
            " _set_hvac_mode('off') previously lived only in the CA-initiated _activate_fan()"
            " path. New shared automation.AutomationEngine._suppress_hvac_for_whf() helper is"
            " now called by BOTH _activate_fan() (refactored to use it, no behavior change)"
            " AND handle_fan_manual_override() (new — scoped to FAN_MODE_WHOLE_HOUSE/BOTH"
            " only; FAN_MODE_HVAC is unaffected by design). Idempotent: a second override call"
            " while a suppression session is already active does not re-capture the mode."
            " (2) Exit is reclassify, not restore: new _release_whf_and_reclassify() releases"
            " _pre_fan_hvac_mode and reuses the existing coordinator fan-off reassert path"
            " (_async_reassert_setpoint_after_fan_off, Issue #359 Fix A) instead of blindly"
            " restoring the mode captured at activation — a manual/RF-remote-timer session can"
            " run up to 12 hours, so the captured mode is often stale by exit. Wired into"
            " on_fan_turned_off() (fan confirmed off by the triggering event) and"
            " clear_fan_override() (grace expiry / user cancel — first checks physical fan"
            " state via the existing _get_fan_physical_state_callback ground truth used by"
            " _reconcile_fan_physical_drift(), and no-ops if the fan is still running, so it"
            " does not race the post-grace fan reconcile). (3) apply_classification()'s"
            " nat-vent early-return now also checks _whf_owns_hvac() (additive OR, not a"
            " replacement for _natural_vent_active) so a manual WHF session — which sets"
            " _pre_fan_hvac_mode but not _natural_vent_active — is covered without weakening"
            " the pre-existing reconcile_fan_on_startup()-adopted-session case."
            " (4) QuietCool remote stale-event dedup: the event.* entity flaps to unavailable"
            " at arbitrary times (not just restart) and re-announces its stale last"
            " event_type with the SAME state (the entity's state field IS the event"
            " timestamp) — confirmed live as a phantom 2-hour override with zero user action."
            " New coordinator._last_fan_remote_event_ts tracks the last acted-on timestamp;"
            " _async_fan_remote_changed() ignores a re-announced identical value. Generalizes"
            " the Issue #491 restart-only guard to every unavailable-then-restore flap."
            " (5) Remote-timer display durability: handle_fan_manual_override() previously"
            " unconditionally overwrote _fan_remote_timer_hours on every call, including a"
            " plain non-remote re-stamp (e.g. the fan entity re-reporting 'on') — nulling an"
            " active remote timer within seconds of a genuine press (confirmed live via the"
            " status API). New is_remote_event parameter disambiguates a genuine remote call"
            " (including a deliberate timer_none 'no timer' press, which correctly clears the"
            " value) from a non-remote re-stamp, which now preserves the existing value."
            " (6) Dashboard status reconciliation: _compute_next_action() gained an"
            " _override_confirm_pending branch (checked before _grace_active, mirroring"
            " _compute_automation_status()'s existing ordering) so a concurrent setpoint-"
            " override-confirm and fan-override-grace no longer produce two contradictory"
            " status lines — display-only, the two override mechanisms remain independent."
        ),
    },
    493: {
        "version_fixed": "0.5.22",
        "title": "learning.save_state() race on shared .tmp filename under concurrent calls",
        "scope_covered": (
            "learning.LearningEngine.save_state() now stages its atomic write via"
            " tempfile.mkstemp(dir=self._db_path.parent, prefix=f'{self._db_path.stem}_',"
            " suffix='.tmp') instead of a fixed self._db_path.with_suffix('.tmp') path,"
            " mirroring the already-proven pattern in state.py's save(). Each call now gets"
            " a guaranteed-unique staging file, eliminating the ENOENT race where two"
            " concurrent calls (9 call sites in coordinator.py, 8 awaited + 1"
            " fire-and-forget from _abandon_observation) could both target the same tmp"
            " path and one's os.replace() would find it already consumed by the other's."
            " Found while verifying #491's fix on a real HA restart — that fix let"
            " _abandon_observation()'s previously-crashing (TypeError) background save"
            " actually execute for the first time, exposing this pre-existing race. On"
            " OSError the orphaned unique tmp file is now cleaned up (os.unlink, errors"
            " suppressed), matching state.py's failure-path behavior. Serialization"
            " failures (TypeError/ValueError from json.dumps) are now caught separately"
            " before any tmp file is created, also matching state.py."
        ),
    },
    491: {
        "version_fixed": "0.5.21",
        "title": "False WHF manual-override/grace-period + coordinator crash at HA restart",
        "scope_covered": (
            "Two independent restart-time bugs, both pre-existing (not introduced by #489)"
            " and both diagnosed from a real restart after the 0.5.20 deploy. (1) New shared"
            " coordinator._suppress_during_startup_coalescing() helper, used by"
            " _async_thermostat_changed() (refactored from its Issue #321 inline check),"
            " _async_fan_entity_changed(), and _async_fan_remote_changed() — all three"
            " override-detection listeners now suppress detection for the same 5-minute"
            " startup-coalescing window (_startup_coalesce_active). Before this fix, only the"
            " thermostat listener had this guard; a QuietCool RF remote event.* entity"
            " re-announcing its last retained event_type during HA's restart/reconnect"
            " sequence was misread by _async_fan_remote_changed() as a fresh timer press,"
            " producing a false 'Fan manual override: whf: ?->on' and a real (but bogus)"
            " manual grace period — confirmed via live HA logs showing the fan status as"
            " 'off (manual override)' (override flag set, _fan_active=False — no real command"
            " was ever issued, matching the fact the WHF never physically ran)."
            " _async_fan_entity_changed() had the identical gap and is fixed for the same"
            " reason (no incident confirmed for that specific path, but the defect class"
            " already shipped one). (2) coordinator._abandon_observation() no longer wraps"
            " hass.async_add_executor_job(self.learning.save_state) — already a scheduled"
            " awaitable — in hass.async_create_task() (which requires a coroutine); the"
            " double-wrap raised 'TypeError: a coroutine was expected, got <Future ...>' on"
            " every restart that hit this HVAC-started thermal-observation-abandonment path,"
            " crashing the coordinator update and surfacing as the '⚠ Climate Advisor"
            " unavailable' status banner (added by Fix #480/0.5.17, which is what made this"
            " 2.5-month-old bug, from Issue #121, visible for the first time)."
        ),
    },
    489: {
        "version_fixed": "0.5.20",
        "title": "Doors/Windows status card refreshes immediately on sensor close, not just open",
        "scope_covered": (
            "coordinator._async_door_window_changed() now requests an immediate coordinator"
            " refresh (async_request_refresh) at the top of the function on every raw sensor"
            " transition — open or closed — so contact_status/contact_sensors reflect live"
            " state right away regardless of debounce. This is purely a display refresh;"
            " it does not change CONF_SENSOR_DEBOUNCE timing or the _paused_by_door /"
            " _natural_vent_active decision logic, which are unaffected. Previously only the"
            " open branch requested an immediate refresh (line ~2986 pre-fix); the close"
            " branch had none, so a stale 'N open' reading could persist until the next"
            " 30-minute coordinator update_interval. Also added a post-decision refresh"
            " after handle_all_doors_windows_closed() in the close branch, mirroring the"
            " existing post-handle_door_window_open() refresh in the open branch — covers"
            " a real, debounce-confirmed pause/resume cycle (HVAC mode/temp restored, grace"
            " started) getting reflected promptly too, not just the raw contact reading."
        ),
    },
    486: {
        "version_fixed": "0.5.19",
        "title": "QuietCool RF remote timer events set the fan manual-override grace duration",
        "scope_covered": (
            "Adds an optional CONF_FAN_REMOTE_ENTITY ('fan_remote_entity') config field (an HA"
            " event entity, e.g. event.quietcool_remote from the gunkl/quietcool-house-fan"
            " firmware). When set, coordinator._async_fan_remote_changed subscribes via"
            " async_track_state_change_event and parses attributes['event_type'] with the new"
            " pure helper fan_status.parse_remote_timer_event() against the single-source mapping"
            " const.REMOTE_TIMER_EVENT_HOURS. Recognized timer tokens (timer_1h/2h/4h/8h/12h,"
            " timer_none) call the EXISTING automation.handle_fan_manual_override(), extended with"
            " an optional duration_override parameter (seconds) that is threaded through to the"
            " existing _start_grace_period(); when set it bypasses decide_grace_start() and uses"
            " the RF-supplied duration instead of the configured manual_grace_seconds. No new"
            " override predicate or new suppression guard was added — the RF timer is a manual"
            " override, so all existing suppression already funnels through the existing"
            " _fan_override_active guard in _deactivate_fan() (nat-vent exit, comfort-floor"
            " breach, standard cycle-off, min-runtime cycle-off all covered for free). A WARNING"
            " is now logged at that guard when the suppressed reason is a comfort/hard-floor exit,"
            " so absolute-timer behavior is observable. timer_none uses the configured grace"
            " duration (no new default constant). Non-recognized event_type tokens (on/off/speed)"
            " are explicitly out of scope for this feature and are ignored. Clearing/expiry rides"
            " entirely on the existing physical fan-off detection and grace-expiry paths — no new"
            " persistence; the RF timer state does not survive an HA restart, consistent with the"
            " existing clean-slate override/grace reset in restore_state() (Issue #327/#282)."
        ),
    },
    434: {
        "version_fixed": "0.5.18",
        "title": "Optional entity config fields can be cleared (leaving a field blank now unsets it)",
        "scope_covered": (
            "Fixes the options flow (ClimateAdvisorOptionsFlow in config_flow.py) so that clearing an"
            " optional entity picker and saving actually removes the stored value. Root cause: the 7"
            " affected fields are declared vol.Optional(KEY, description={'suggested_value': ...}) with"
            " no default=, so when cleared voluptuous omits the key from user_input; async_step_save"
            " merged {**entry.data, **self._updates} with existing data as the base, so an omitted key"
            " fell through to the old value and nothing could ever delete it. Fix adds a _removed set"
            " and a _apply_step_input(user_input, clearable_keys) helper: for each key a step owns, a"
            " present key is a set/update and an absent key is recorded for removal; async_step_save now"
            " pops _removed keys from the merged dict before persisting. Applied to all 7 clearable"
            " optional-entity fields: home_toggle_entity, vacation_toggle_entity, guest_toggle_entity"
            " (occupancy step); fan_entity, fan_state_entity (sensors step); outdoor_temp_entity,"
            " indoor_temp_entity (temperature_sources step). Downstream consumers already read these via"
            " config.get(KEY) with truthiness guards, so a cleared field yields the correct defaults"
            " (occupancy → Home; vacation/guest → off). Also closes the test-coverage gap that let this"
            " ship: tools/sim_harness/ha_stubs.py now realifies config_entries.ConfigFlow/OptionsFlow"
            " (previously bare MagicMocks), so tests invoke the REAL step handlers + async_step_save"
            " instead of mirroring the merge; new TestOptionsFlowClearing parametrizes all 7 fields for"
            " both clear-when-blank and store-when-set, and the prior mirror-style option-flow tests were"
            " converted to real invocation."
        ),
    },
    480: {
        "version_fixed": "0.5.17",
        "title": "Coordinator health observability: surface stale status instead of silently serving frozen data",
        "scope_covered": (
            "Added durable coordinator-health tracking: coordinator.py's _async_update_data() is now"
            " a thin wrapper (_async_update_data) around the real update logic (renamed"
            " _async_update_data_impl); any exception raised by the real logic is caught, recorded as"
            " last_update_error (str)/last_update_error_time (ISO timestamp)/consecutive_failure_count"
            " (int), persisted via _async_save_state() (state.py's existing atomic write-then-replace"
            " pattern — same StatePersistence used for all other operational state), and then"
            " re-raised unchanged so HA's own DataUpdateCoordinator still marks entities unavailable"
            " exactly as before. On the next successful update the failure record is cleared and"
            " re-persisted. async_restore_state() restores the three fields unconditionally (same"
            " precedent as ai_stats — not gated on the same-day check the rest of that function uses),"
            " so a failure recorded just before an overnight restart is still visible afterward."
            " api.py's ClimateAdvisorStatusView.get() now reads coordinator.last_update_success and"
            " adds coordinator_healthy to the response, plus last_error/stale_since (from the two"
            " persisted fields) when unhealthy — purely additive, no existing fields changed or"
            " removed. frontend/index.html's loadStatus() Status card now renders"
            " '⚠ Climate Advisor unavailable since HH:MM — <error>' when coordinator_healthy is false,"
            " using the existing status-item card (no new card added), following the same conditional-line"
            " pattern already used for pause_suppressed_classification/nat_vent_active."
        ),
    },
    481: {
        "version_fixed": "0.5.17",
        "title": (
            "Incident detection now respects the currently-active comfort band"
            " (sleep/away/vacation), not just the flat daytime band"
        ),
        "scope_covered": (
            "coordinator.py: added _resolve_active_comfort_band(), which routes through"
            " select_comfort_band() (automation.py) — the same canonical resolver api.py's"
            " ca_target_heat/ca_target_cool fields and automation.py's setpoint-writing handlers"
            " (apply_classification, handle_bedtime, handle_pre_cool, handle_morning_wakeup,"
            " occupancy handlers) already use, per the Issue #402/#462 precedent. When"
            " coordinator.current_classification is None (e.g. right after HA restart, before the"
            " first classification cycle), falls back to the same sleep/day-only heuristic"
            " api.py uses, for consistency. _detect_and_emit_incidents() now calls this once per"
            " cycle and uses the result for both the comfort_violation/comfort_undertemp"
            " threshold comparison AND the values passed into _is_nat_vent_tolerated_deviation()"
            " (so the nat-vent hysteresis tolerance check also uses the correct active band, not"
            " the static config values). _emit_incident() now accepts optional comfort_heat/"
            " comfort_cool overrides and defaults to the resolved active band via the same"
            " helper if not supplied, so every incident's persisted payload (comfort_heat/"
            " comfort_cool fields) reflects the band that was actually active at emission time —"
            " not always the flat daytime numbers — for anyone reviewing incident history later."
            " Test infrastructure (tools/sim_harness/): added a new 'coordinator_refresh' harness"
            " dispatch event type (run_production.py) so a scenario can explicitly trigger a"
            " second DataUpdateCoordinator cycle (async_request_refresh() -> _async_update_data()"
            " -> _detect_and_emit_incidents()) — previously nothing in the harness re-invoked"
            " _async_update_data() after the coordinator's initial first-refresh at construction,"
            " so this code path was untestable end-to-end via the coordinator harness. Also fixed"
            " build_coordinator.py to additionally capture coordinator-originated events"
            " (self._emit_event(), which _emit_incident()/_detect_and_emit_incidents() call"
            " directly) into the flat scenario event_log the harness's assertions read — these"
            " previously wrote only to the internal self._event_log ring buffer and were"
            " invisible to any scenario assertion. Added a new negative assertion type,"
            " no_comfort_undertemp_incident/no_comfort_violation_incident (outcomes.py), mirroring"
            " the existing override_not_detected precedent. New golden-track scenario"
            " (pending/issue-481-sleep-band-no-false-undertemp-incident.json) verified"
            " load-bearing via a real revert test."
        ),
    },
    482: {
        "version_fixed": "0.5.17",
        "title": "Fan-off bookkeeping gap + event.context provenance for manual-vs-automation classification",
        "scope_covered": (
            "automation.py: (1) _reconcile_fan_physical_drift()'s off-command"
            " (self._command_whf_control_entity(False, ...) via a new internal"
            " _do_drift_reconciliation_off_command() wrapper) now sets"
            " self._fan_command_time/self._fan_command_pending synchronously BEFORE"
            " self.hass.async_create_task(...) schedules the coroutine — matching the exact"
            " pattern _activate_fan()/_deactivate_fan() already use — and clears"
            " _fan_command_pending in the wrapper's own finally block once the command"
            " completes. (2) Added _call_fan_service_with_context(), a shared funnel now used"
            " by _command_whf_control_entity() (and therefore _activate_fan(), _deactivate_fan(),"
            " and the drift-reconciliation off-command) that constructs a fresh"
            " homeassistant.core.Context() per outgoing fan/switch service call, passes it as"
            " services.async_call(..., context=cmd_context), and records cmd_context.id on"
            " self._fan_command_context_id. coordinator.py's _async_fan_entity_changed() reads"
            " event.context.id/parent_id and compares against automation_engine._fan_command_context_id"
            " as an ADDITIONAL suppression signal (self._fan_command_pending OR context_confirms_ca),"
            " logging the comparison at DEBUG on every fan-entity state change (matched or not) and"
            " at INFO when context alone was the deciding signal. handle_fan_manual_override() and"
            " on_fan_turned_off() gained an optional event_context_id parameter, surfaced in the"
            " fan_manual_override/fan_cancel Activity Report event payloads as diagnostic data."
            " tools/sim_harness/ha_stubs.py gained a _MockContext stand-in for"
            " homeassistant.core.Context (id/parent_id/user_id) so the harness/tests construct real,"
            " comparable context objects the same way production does against real HA."
            " tools/sim_harness/fake_hass.py threads a context kwarg through"
            " _FakeServices.async_call -> _apply_state_feedback -> _FakeStates.async_set -> the"
            " dispatched FakeEvent, so a CA-issued command's context reaches the coordinator listener"
            " exactly like real HA propagates the originating service call's context onto the"
            " resulting state write; an externally-injected states.async_set() (no CA service call)"
            " naturally carries context=None, correctly modeling 'no CA attribution available' for a"
            " genuine external actor. New golden-harness assertion types in outcomes.py"
            " (fan_ca_command_not_misclassified, fan_external_change_classified) and a new"
            " external_fan_state_change scenario event type in run_production.py."
        ),
    },
    483: {
        "version_fixed": "0.5.17",
        "title": "Adopt matching automation decision instead of continuing a grace period",
        "scope_covered": (
            "automation.py: new _override_matches_current_decision() helper, called from two"
            " sites. (1) apply_classification() (pre-expiry): when _manual_override_active and"
            " the override's HVAC mode matches the classification about to be applied, the grace"
            " timer is cancelled (_cancel_grace_timers()), the override is cleared"
            " (reason='adopted_matching_decision'), an 'override_adopted' event is emitted, and"
            " execution falls through to apply the (now-agreeing) classification normally this"
            " same cycle — instead of the pre-existing early return that silently skipped for the"
            " rest of the grace window. (2) _on_grace_expired() (natural expiry, the safe minimum"
            " bar): the same match check runs in the normal-expiry branch; on a match, the"
            " misleading 'your manual thermostat override has expired' notification is skipped"
            " (nothing was actually reverted) and 'override_adopted' fires instead of the generic"
            " 'grace_expired' event. Eligibility is deliberately narrow: only overrides that flow"
            " through _confirm_override() and set _manual_override_mode qualify (covers the mode-"
            " change/'normal' and door/window-pause/'pause' override paths — both ultimately reach"
            " _confirm_override()). Setpoint-only overrides (_manual_override_source == 'setpoint')"
            " are excluded outright — matching HVAC mode alone is not evidence the user's chosen"
            " temperature has converged with automation's. For heat/cool modes, a match additionally"
            " requires the thermostat's LIVE setpoint to be within OVERRIDE_ADOPT_SETPOINT_TOLERANCE_F"
            " (1.0°F, const.py) of what select_comfort_band() would arm right now for that mode —"
            " added after cold_day_heat_all_day_with_override.json (a pending regression scenario)"
            " demonstrated that a compound override (mode changed AND a deliberately different"
            " setpoint, e.g. heat at 74°F when comfort_heat=70°F) must not be silently adopted just"
            " because the mode happens to agree. New pending scenario"
            " override_adopted_on_matching_decision.json exercises the full adopt path (pre-expiry,"
            " mode+setpoint match) and was revert-tested (temporarily forcing"
            " _override_matches_current_decision() to always return False; the scenario's 14:30"
            " assertion correctly failed with the un-adopted carried-forward 'override_confirmed'"
            " outcome, then passed again after restoring). ai_skills_activity.py: new"
            " 'override_adopted' event registered in EVENT_RENDERERS, _MANUAL_EVENT_TYPES, and"
            " _NO_DEDUP. tools/sim_harness/run_production.py: thermostat_state_changed events may"
            " now optionally carry a 'temperature' field alongside hvac_mode (models a real"
            " thermostat UI where mode+setpoint change in one interaction); omitted is a no-op,"
            " identical to prior behavior for every pre-existing scenario. tools/sim_harness/"
            " outcomes.py: registered 'override_adopted' as a named production outcome (was"
            " previously falling through to the generic 'unknown:<type>' fallback) — cosmetic only,"
            " does not change production_outcome_at()'s existing same-timestamp last-decision-wins"
            " tie-break semantics."
        ),
    },
    476: {
        "version_fixed": "0.5.16",
        "title": "Migrate all 10 remaining scenarios to the coordinator-level Tier A harness",
        "scope_covered": (
            "Migrated grace_full_lifecycle_clean_expiry, grace_full_lifecycle_sensor_still_open,"
            " grace_window_period_no_repause, override_detection_and_confirmation,"
            " bedtime_skipped_manual_override_active, cold_day_heat_all_day_with_override,"
            " override_self_resolve_transient (all unsupported/ -> pending/), plus goldens"
            " cancel_override_then_resume, grace_prevents_sensor_repause,"
            " grace_timer_expired_on_restart (golden/ -> pending/, pending user re-review since"
            " substantially rewritten). Added coordinator-aware sensor_open/sensor_close dispatch"
            " (real state changes through _async_door_window_changed, not direct engine calls) and"
            " a new cancel_override event type (mirrors api.py's ClimateAdvisorCancelOverrideView.post()"
            " exactly). Fixed 4 real harness bugs found during migration, all in tools/sim_harness/ or"
            " tools/simulate.py: (1) FakeScheduler's per-event dispatch didn't settle before the next"
            " scenario event, so a coordinator listener's own async_create_task chain only drained as an"
            " incidental side effect of whatever unrelated timer fired next, misattributing event"
            " timestamps by minutes; (2) dt_util.parse_datetime() was never patched, returning a"
            " MagicMock from the stubbed homeassistant.util.dt module and crashing"
            " _check_hvac_stabilization and 18 other call sites including stuck-grace detection;"
            " (3) async_track_time_change/interval callback wrappers (_schedule_daily/_schedule_interval)"
            " discarded their inner callback's return value one level below where the coroutine check"
            " happened, so async def callbacks like _async_send_briefing/_async_morning_wakeup were"
            " constructed but never awaited; (4) run_production_scenario() unconditionally overwrote"
            " engine._sensor_check_callback with an engine-only _SensorTracker stub, clobbering the real"
            " coordinator._any_sensor_open wiring even in coordinator mode, breaking grace-expiry"
            " re-pause detection. Also fixed tools/simulate.py's file-reading to use explicit UTF-8"
            " (was crashing on pre-existing mojibake in 2 scenario files under Windows' cp1252 default)."
            " Every migrated scenario's core assertion was verified genuinely load-bearing via a real"
            " revert test — temporarily disabling the specific production guard it protects in a"
            " throwaway edit, confirming the assertion fails, then restoring (git diff confirmed clean"
            " after each). Several scenarios needed the 'no_action' legacy-simulator sentinel corrected"
            " to the actual carried-forward outcome outcomes.py produces (no such sentinel exists in the"
            " real harness), and grace/override-window config extended where the harness default"
            " (300-900s) would auto-clear state before a later scenario event needed it still active."
        ),
    },
    474: {
        "version_fixed": "0.5.15",
        "title": "Coordinator-level Tier A test harness coverage (no production code change)",
        "scope_covered": (
            "tools/sim_harness/: FakeHass gained real state-change dispatch (states.async_set(),"
            " an entity-keyed listener registry, a minimal bus) instead of silent state mutation;"
            " FakeScheduler.installed() now also patches coordinator.py's async_call_later/"
            " async_track_time_change/async_track_time_interval/async_track_point_in_time/"
            " async_track_state_change_event/callback/dt_util.* (previously automation.py only);"
            " ha_stubs.py's _MockDataUpdateCoordinator gained async_config_entry_first_refresh()"
            " and now captures self.hass (a pre-existing gap silently tolerated by prior tests)."
            " New tools/sim_harness/build_coordinator.py constructs a real ClimateAdvisorCoordinator"
            " replicating __init__.py's exact startup sequence. Deleted run_production.py's"
            " thermostat_state_changed override-detection mirror (an 18-line approximation of the"
            " real ~552-line, 3-branch _async_thermostat_changed state machine, already proven stale"
            " post-#249) — real dispatch now reaches the actual listener. Added a"
            " skip_startup_coalesce scenario flag: a freshly built coordinator has its real 5-minute"
            " post-restart override-detection suppression window active, same as production, and"
            " scenarios testing steady-state behavior must opt out or every event vacuously"
            " early-returns before reaching any guard (found via a real revert test: the proving-"
            " slice scenario initially passed with the guard both present AND fully disabled, until"
            " this flag was added — then it correctly passed with the guard intact and failed with"
            " it removed). Migrated tools/simulations/unsupported/away_setpoint_change_not_override.json"
            " to tools/simulations/pending/ as the proving-slice scenario (both assertions load-bearing,"
            " verified via revert test)."
        ),
    },
    470: {
        "version_fixed": "0.5.14",
        "title": "Dedupe double _compute_target_band_schedule() invocation in get_chart_data()",
        "scope_covered": (
            "coordinator.py: get_chart_data() computed the target-band schedule twice per request"
            " (once internally inside _build_predicted_indoor_future(), once directly for the"
            " displayed target_band). Added an optional band_schedule parameter to"
            " _build_predicted_indoor_future(); when provided (now always, from get_chart_data()),"
            " it's reused directly instead of recomputing. Moved the pre-cool trigger/target +"
            " _compute_target_band_schedule() computation in get_chart_data() to run once, before the"
            " historical-view branch, and threaded the result through. This also fixed a pre-existing,"
            " unrelated divergence the consolidation surfaced: _build_predicted_indoor_future()'s"
            " internal recompute pinned sleep_heat/sleep_cool to its own raw-clamped setback values"
            " before calling _compute_target_band_schedule() — which, via compute_bedtime_setback()'s"
            " 'explicit value takes priority' branch, silently skipped the adaptive thermal-model-"
            " derived sleep floor the DISPLAYED band uses whenever sleep_heat/sleep_cool weren't"
            " explicitly configured. The ODE prediction curve now agrees with the displayed band in"
            " that scenario instead of silently disagreeing. Verified with a call-count regression"
            " test proving exactly one invocation post-fix (confirmed 2 on pre-fix code), a test"
            " proving the band_schedule parameter is honored, and a test exercising the"
            " previously-diverging adaptive-sleep-floor scenario."
        ),
    },
    468: {
        "version_fixed": "0.5.13",
        "title": "Thread coordinator._build_learning_health() through 3 AI-context get_thermal_model() calls",
        "scope_covered": (
            "get_thermal_model(learning_health=...) is called two ways: canonically (coordinator.py's"
            " 3 sites, sensor.py's ClimateAdvisorComplianceSensor) with learning_health passed, and"
            " degraded (3 AI-context sites) with no arguments, producing learning_health: {} and"
            " thermal_equilibrium_f: None always, per the function's own docstring. Fixed all 3:"
            " ai_skills_activity.py's swing-acquisition call in async_build_activity_context(); "
            " ai_skills_context.py's build_learning_context() THERMAL MODEL section; and"
            " ai_skills_context.py's build_thermal_pipeline_context(), which already computed the exact"
            " same health dict a few lines above for its own per-type display but never passed it to"
            " the adjacent get_thermal_model() call. All three now receive the same"
            " coordinator._build_learning_health() output the dashboard/sensor sees."
        ),
    },
    466: {
        "version_fixed": "0.5.12",
        "title": "Add target_temp fields to coordinator.data; route AI-context sites through it",
        "scope_covered": (
            "coordinator.py: added target_temp/target_temp_low/target_temp_high to the"
            " _async_update_data() return dict, read from the same climate-entity state (_cs) already"
            " fetched there for hvac_mode/hvac_action. ai_skills_activity.py"
            " (async_build_activity_context()) and ai_skills_context.py (build_hvac_entity_context())"
            " now read hvac_mode/target_temp/target_temp_low/target_temp_high from coordinator.data"
            " instead of independently calling hass.states.get() — each kept a minimal live"
            " hass.states.get() call only for current_temperature, which is out of this issue's scope"
            " and not exposed on coordinator.data. Explicit scope decision (confirmed with the project"
            " owner, not a silent merge): api.py's status endpoint keeps its own live hass.states.get()"
            " call unchanged — it powers the ca_target_heat/cool divergence check (#402/#462), whose"
            " entire purpose is comparing CA's computed target against the REAL thermostat right now;"
            " routing it through coordinator.data's ~30-min-stale cache would compare CA's belief"
            " against CA's own stale snapshot of the thermostat, defeating the check."
        ),
    },
    464: {
        "version_fixed": "0.5.11",
        "title": "Add coordinator.get_hvac_runtime_today() to kill the 3x copy-pasted formula",
        "scope_covered": (
            "coordinator.py: new get_hvac_runtime_today() method — base runtime from today's"
            " record plus elapsed minutes of any in-progress HVAC session, computed live (not"
            " from the up-to-30-min-stale coordinator.data snapshot). Routed coordinator.py's own"
            " _async_update_data() inline computation through it, plus the two AI-context copies"
            " (ai_skills_context.py build_current_state_context(),"
            " ai_skills_activity.py async_build_activity_context()) that previously reached into"
            " coordinator._today_record/_hvac_on_since directly. Verified via unit tests covering"
            " no-record/no-session, base-only, active-session, and rounding cases, plus updated 3"
            " tests in test_ai_investigator.py that previously patched dt_util on the now-unused"
            " ai_skills_context module directly instead of configuring the new method."
        ),
    },
    462: {
        "version_fixed": "0.5.10",
        "title": "Route api.py's ca_target_heat/cool through the canonical select_comfort_band() resolver",
        "scope_covered": (
            "api.py: ClimateAdvisorStatusView.get()'s ca_target_heat/cool computation (Issue #402)"
            " replaced its own third independent implementation of the sleep/day band branch with a"
            " call to automation.py's select_comfort_band() — the same resolver apply_classification(),"
            " handle_bedtime(), handle_pre_cool(), handle_morning_wakeup(), and the occupancy handlers"
            " already use to decide the live floor/ceiling. Deliberately NOT compute_bedtime_setback()"
            " (the chart/briefing's adaptive resolver — a different, documented split per const.py's"
            " #333 changelog): this field exists to detect divergence from the ACTUAL live setpoint,"
            " which is select_comfort_band()'s job. Falls back to the old sleep/day-only heuristic only"
            " when coordinator.current_classification is None (e.g. right after HA restart, before the"
            " first classification cycle). Fixed two real gaps the old inline branch had: (1) it ignored"
            " occupancy mode entirely, so away/vacation setback was never reflected even though the"
            " thermostat was really being held at the setback band; (2) its unconfigured-sleep-temp"
            " fallback was comfort_heat/comfort_cool, not the documented DEFAULT_SLEEP_HEAT/"
            " DEFAULT_SLEEP_COOL (64/72°F) select_comfort_band() actually uses."
        ),
    },
    460: {
        "version_fixed": "0.5.9",
        "title": "Consolidate the occupancy-defer predicate (3 formulations across automation.py)",
        "scope_covered": (
            "automation.py: new pure module-level function should_defer_to_occupancy_setback"
            "(occupancy_mode) — True for OCCUPANCY_AWAY/OCCUPANCY_VACATION. Routed handle_bedtime(),"
            " handle_pre_cool(), and handle_morning_wakeup() (previously the inverted"
            " 'not in (HOME, GUEST)' form) through it directly, and routed"
            " _set_temperature_for_mode() through it as an outer guard while preserving its"
            " distinct per-mode redirect (AWAY -> handle_occupancy_away(),"
            " VACATION -> handle_occupancy_vacation()) — only the boolean gate is unified, not"
            " the dispatch logic. Verified via a unit test proving the predicate agrees with"
            " the original inverted formulation across all 4 occupancy modes, plus a positive"
            " control: corrupting the predicate to always return False was independently caught"
            " by 9 unit test failures across all 4 call sites AND 2 golden scenario divergences"
            " (54/56), proving the extraction is load-bearing in production."
        ),
    },
    458: {
        "version_fixed": "0.5.8",
        "title": "Consolidate CA-fan-running suppression predicate; fix missing 'active (unconfirmed)'",
        "scope_covered": (
            "New fan_status.py module (mirrors the temperature.py precedent): FAN_STATUS_ACTIVE_VALUES"
            " + is_ca_fan_running(fan_status) — the single source of truth for 'does this fan_status"
            " represent activity CA can account for.' ai_skills_activity.py's async_build_activity_context()"
            " now calls it directly, fixing the real bug: its allow-list was missing"
            " 'active (unconfirmed)' (the WHF ground-truth-disagreement state added by #423), so the AI"
            " Activity Report misreported that specific fan state as a contradiction. coordinator.py's"
            " _async_update_data() state-contradiction check also routed through the same predicate"
            " (previously a separate ae._fan_active/_natural_vent_active flag check plus an ad hoc"
            " running-untracked check) — this ALSO fixed a second latent gap: a manual fan override"
            " confirmed running via physical state (ae._fan_override_active=True, ae._fan_active=False)"
            " was not suppressing the coordinator's own contradiction-warning event, even though"
            " ai_skills_activity.py's independent check already treated that case as expected."
            " ae._natural_vent_active is kept as an explicit extra OR condition in coordinator.py,"
            " covering the nat-vent-armed-but-idle moment between cycles"
            " ('nat-vent (session active, fan idle)') — deliberately not one of the four canonical"
            " active values, since the physical fan genuinely isn't running then."
        ),
    },
    456: {
        "version_fixed": "0.5.7",
        "title": "Consolidate the nat-vent hard-exit floor formula duplicated 3 times",
        "scope_covered": (
            "fan_thermostat_decision.py: promoted the private _resolve_vent_floor()'s body into"
            " a new public, standalone pure function resolve_hard_exit_floor(comfort_heat_raw,"
            " sleep_heat, in_sleep_window, hysteresis); _resolve_vent_floor() now delegates to it."
            " automation.py: routed check_natural_vent_conditions()'s inline _vent_floor and"
            " nat_vent_temperature_check()'s inline _hard_floor through the new function instead"
            " of recomputing the sleep/day branch inline. Explicitly out of scope:"
            " _nat_vent_reactivation_floor() (the reactivation GATE's floor) is a deliberately"
            " different formula (no hysteresis subtraction) answering a different question — not"
            " a 4th copy of this same formula, left untouched. Verified via a unit test"
            " reproducing both old inline formulas exactly across 5 sleep/day/hysteresis"
            " combinations, plus a positive control: corrupting resolve_hard_exit_floor() was"
            " independently caught by 3 unit test failures AND a golden scenario divergence"
            " (55/56), proving the extraction is load-bearing in production, not just in tests."
        ),
    },
    454: {
        "version_fixed": "0.5.6",
        "title": "Extract shared differential-comparator base for pure decide_*() modules",
        "scope_covered": (
            "tools/sim_harness/decision_compare_base.py: new module providing the shape shared"
            " by every differential comparator for a production method that RETURNS a value"
            " (shadow-mode instrumentation, DecisionCall/DecisionComparisonRun result shape,"
            " substitution mode). nat_vent_gate_compare.py refactored onto it, keeping its exact"
            " public API (GateCall/GateComparisonRun/compare_scenario/substitute_new_gate)"
            " unchanged for existing callers (tools/nat_vent_gate_diff.py,"
            " tools/nat_vent_gate_substitution_diff.py, tests/test_nat_vent_gate_compare.py,"
            " tests/test_nat_vent_gate_substitution.py). Verified behavior-preserving: 39/39 gate"
            " calls agree (nat_vent_gate_diff.py) and 56/56 scenarios produce identical full"
            " outcomes under substitution (nat_vent_gate_substitution_diff.py), matching pre-refactor"
            " results exactly. fan_thermostat_decision_compare.py deliberately NOT refactored onto"
            " the base — its production method returns nothing (outcome inferred from side-effect"
            " calls) and requires pre-call input capture, a genuinely different instrumentation"
            " shape; verified unaffected (228/228 calls still agree)."
        ),
    },
    452: {
        "version_fixed": "0.5.5",
        "title": "HomeAssistantView test stub gap forced 14 test helpers to hand-replicate production logic",
        "scope_covered": (
            "tools/sim_harness/ha_stubs.py: added _MockHomeAssistantView + _MockJsonResponse, wired"
            " into install_ha_stubs() the same way _MockSensorEntity/_MockCoordinatorEntity already"
            " were — HomeAssistantView subclasses (the 23 api.py view classes) previously silently"
            " became MagicMock instances on instantiation (not a hard error, so it went unnoticed) since"
            " the base was an unconfigured MagicMock attribute. Deleted and rewrote the 14 test helpers"
            " that hand-replicated production logic as a workaround: 3 API-view helpers now drive the"
            " real ClimateAdvisorStatusView/LearningView/RespondSuggestionView/InvestigateView/"
            " InvestigationReportsView; 3 sensor helpers now instantiate the real"
            " ClimateAdvisorFanStatusSensor/ClimateAdvisorComplianceSensor (confirmed already"
            " instantiable — the SensorEntity/CoordinatorEntity metaclass conflict this was blamed on"
            " had already been resolved by an earlier pass and the docs were stale); 3 coordinator-method"
            " helpers (_compute_contact_status/_details, _compute_automation_status,"
            " _compute_next_automation_action) now use the established object.__new__() +"
            " types.MethodType() partial-instantiation pattern instead of replicated bodies. Along the"
            " way, rewriting _compute_next_automation_action's tests against the real method surfaced"
            " and fixed a stale assertion: the old replicated helper's bedtime-setback formula"
            " (comfort_heat - 4 + setback_modifier) predates the sleep_heat/sleep_cool config keys the"
            " real method has read for some time, so two tests were passing against logic production no"
            " longer runs."
        ),
    },
    449: {
        "version_fixed": "0.5.4",
        "title": "WHF control-entity command dedup silently drops reactivation in dual-entity setups",
        "scope_covered": (
            "automation.py: confirmed the real root cause behind #446's symptoms via real HA entity"
            " history (not theory) — a whole-house-fan control/transmitter entity's HA-reported state"
            " showed only 2 transitions across a ~14-hour incident window (on at adoption, off at"
            " morning wake-up), while ~14 repeated turn_on calls during a drift-correction loop"
            " produced zero state changes, because the physical fan had been turned off outside HA's"
            " command path and the one-way transmitter entity had no feedback to reflect that. New"
            " _command_whf_control_entity(desired_on, reason) helper, used by _activate_fan(),"
            " _deactivate_fan(), and the drift-reconciliation correction path: when dual-entity ground"
            " truth (fan_state_entity) is available and the control entity already claims the desired"
            " state but ground truth disagrees, forces a real transition by commanding the OPPOSITE"
            " state first, waiting 5 seconds, then the desired state — symmetric in both directions"
            " (want-on-but-stuck-on and want-off-but-stuck-off). When both signals already agree, no"
            " command is sent at all. Scoped narrowly: single-entity/command-only WHF setups and all"
            " FAN_MODE_HVAC fan-mode control are completely untouched — the helper only activates when"
            " a live dual-entity ground-truth reading is available to justify it."
        ),
    },
    446: {
        "version_fixed": "0.5.3",
        "title": "Automated fan drift-correction mislabeled as manual grace + repeated unwarranted-fan reconcile spam",
        "scope_covered": (
            "automation.py: _clear_fan_flags_and_start_grace() had exactly 2 callers"
            " (on_fan_turned_off(), a genuine user action, and _reconcile_fan_physical_drift(),"
            " Issue #423's automated self-healing correction) but both hit the same hardcoded"
            " self._start_grace_period('manual', ...) — reporting an automated correction to"
            " the user as if they had turned the fan off themselves. Added a source parameter"
            " (default 'manual', preserving on_fan_turned_off()'s call unchanged) and pass"
            " source='automation' from the drift-correction path; the codebase already had 3"
            " precedent call sites using 'automation' for this field. Also added DEBUG-level"
            " instrumentation logging the raw fan_entity/fan_state_entity read on every"
            " backstop tick (not just confirmed drift) — the true root cause of why the"
            " physical-state read repeatedly disagrees on an exact 10-minute cadence overnight"
            " was investigated (ruled out: a tick-counter bug, command-only mode, a toggle-type"
            " RF fan entity — all verified against the real code/config) but not conclusively"
            " identified, since real incident logs were unavailable (rotated on restart)."
            " Separately, reconcile_fan_on_startup()'s 'turn off unwarranted fan' branch had no"
            " rate limit across its 4 call sites, so a recurring condition (e.g. a thermostat's"
            " own fan circulation schedule) triggered a full correction attempt every few"
            " minutes for up to 45 minutes. Added a 5-minute cooldown (reusing the existing"
            " _last_override_detected_time dedup pattern) inside the function itself so it"
            " applies regardless of which caller triggered it; a suppressed correction still"
            " logs at INFO so a persistently-stray fan stays visible."
        ),
    },
    444: {
        "version_fixed": "0.5.2",
        "title": "Duplicate 'Comfort band applied' Activity Report entries for the same setpoint",
        "scope_covered": (
            "automation.py: _apply_comfort_band() had no idempotency guard — it unconditionally"
            " emitted a comfort_band_applied event on every call, even when the band (active edge,"
            " mode, target temperature) was identical to the one just announced. At least 2"
            " overlapping call paths independently invoke apply_classification() -> _apply_comfort_band()"
            " back-to-back around a restart: _do_startup_coalesce() (coordinator.py) calls it directly,"
            " then at the end of that same sequence schedules async_request_refresh(), which triggers a"
            " second, independent _async_update_data() cycle whose regular-cycle path calls it again"
            " within seconds. A grace-expiry re-application colliding with the regular cycle produces the"
            " same duplicate pattern outside of a restart. Real telemetry (Issue #444) confirmed this on"
            " both 0.4.74 and 0.5.1 — a pre-existing defect, not a regression from the architecture-reset"
            " work. Fixed with a short (10 minute) time-windowed dedup on the ANNOUNCEMENT only:"
            " COMFORT_BAND_EVENT_DEDUP_SECONDS in const.py; new _last_comfort_band_signature /"
            " _last_comfort_band_event_at instance state on AutomationEngine. The underlying"
            " _set_temperature() thermostat command is never suppressed — only the redundant event."
        ),
    },
    440: {
        "version_fixed": "0.5.1",
        "title": "Pre-cool AC trigger stuck on stale schedule when nat-vent exits ahead of its scheduled close",
        "scope_covered": (
            "coordinator.py: _compute_pre_cool_trigger_time() computes the pre-cool AC trigger ONCE at"
            " classification time (window_close_time + PRE_COOL_POST_NAT_VENT_DELAY_MINUTES, or a"
            " wake_time-4h fallback) and _maybe_schedule_pre_cool() only ever runs it once per day"
            " (idempotent via _pre_cool_trigger_scheduled). If nat-vent exited for real well before"
            " that scheduled time — the reactivation gate firing, a sensor closing, outdoor rising, an"
            " away/vacation ceiling exit, or a startup reconcile — the trigger stayed on the stale"
            " schedule, wasting the AC-vs-free-cooling decision gap in between. Fixed by detecting a"
            " genuine natural_vent_active True->False transition inside _emit_event() (deliberately not"
            " enumerating the 6 real exit event-type strings, which would silently miss a future exit"
            " path) and, via new pure function _decide_pre_cool_reschedule(), pulling the pending"
            " trigger to now + PRE_COOL_POST_NAT_VENT_DELAY_MINUTES whenever that is earlier than what's"
            " already scheduled — never later, so an exit close to (or after) the scheduled time cannot"
            " accidentally push pre-cool back. New coordinator method"
            " _maybe_reschedule_pre_cool_on_nat_vent_exit() owns cancelling/rescheduling the real timer."
            " 9 new tests (tests/test_pre_cool_reschedule.py): pure-function boundaries plus a"
            " coordinator-level load-bearing positive control proving the _emit_event() transition"
            " detection genuinely drives the reschedule."
        ),
    },
    439: {
        "version_fixed": "0.5.1",
        "title": "Initial setup wizard wrote stale sleep-temperature defaults into brand-new installs",
        "scope_covered": (
            "config_flow.py: async_step_setpoints() (the INITIAL setup wizard, distinct from the"
            " options/edit flow which already read current.get(key, DEFAULT_X) correctly). Fahrenheit"
            " branch: comfort_heat/comfort_cool/setback_heat/setback_cool already referenced the"
            " DEFAULT_* named constants, but sleep_heat/sleep_cool were hardcoded literals 66/78 — the"
            " pre-0.5.0 values, never updated when those constants were reformatted to 64/72. Celsius"
            " branch: ALL SIX fields were hardcoded literal Celsius numbers hand-converted from the OLD"
            " Fahrenheit defaults and never updated. Since these are vol.Required fields with the stale"
            " value pre-filled, submitting the form unchanged wrote the stale numbers explicitly into"
            " the new install's config — not merely a display glitch. Fixed by extracting"
            " setpoint_slider_ranges(is_celsius) — derives every default (both unit branches) from the"
            " DEFAULT_* Fahrenheit constants directly, converting to Celsius via from_fahrenheit()"
            " rounded to the 0.5 step. 4 new regression tests in test_config_flow.py pin both branches'"
            " defaults against the named constants."
        ),
    },
    437: {
        "version_fixed": "0.5.0",
        "title": "Overnight pre-cool thermal-mass banking silently became a no-op on many configs",
        "scope_covered": (
            "automation.py: handle_pre_cool()'s target floor was comfort_heat + PRE_COOL_MIN_HEADROOM_F"
            " (a fixed 2°F above the DAYTIME comfort floor) — correct when DEFAULT_SLEEP_COOL was a"
            " warmer economizer-style 78°F (plenty of headroom below it), but once sleep_cool was"
            " reformatted to a flatter, cooler-than-daytime household default (74->closer to comfort_heat),"
            " the same floor left little to no room: pre-cool clamped its target right back up near the"
            " normal sleep ceiling, banking nothing while still emitting pre_cool_applied as if it worked."
            " Root-caused after the user recalled an existing '+1 above the floor' convention"
            " (nat_vent_temperature_check()'s sleep_heat + hysteresis sleep-window cycling target) and"
            " asked whether pre-cool should reuse it. New compute_pre_cool_target() in automation.py"
            " floors the target at sleep_heat + hysteresis instead, letting pre-cool travel the full"
            " [sleep_heat, sleep_cool] range regardless of the comfort_heat/comfort_cool configuration."
            " The same formula (plus a stale literal 78.0 sleep_cool fallback, same bug class as #435)"
            " was independently duplicated across 5 call sites (handle_pre_cool, trigger-time scheduling,"
            " status text, the chart target-band dip, and the ODE predicted-indoor curve) — all 5 now"
            " route through the one shared function. PRE_COOL_MIN_HEADROOM_F removed (dead)."
        ),
    },
    438: {
        "version_fixed": "0.5.0",
        "title": "Shipped default comfort/setback/sleep temperatures reformatted to a real household config",
        "scope_covered": (
            "const.py: DEFAULT_COMFORT_HEAT/DEFAULT_COMFORT_COOL/DEFAULT_SETBACK_HEAT/"
            "DEFAULT_SETBACK_COOL changed from arbitrary round numbers (70/75/60/80) to a real, tuned"
            " installation's own configured values (68/74/63/79). DEFAULT_SLEEP_HEAT/DEFAULT_SLEEP_COOL"
            " changed from a derived-from-comfort_cool formula (66/78, implicitly warmer-at-night) to"
            " independent flat values (64/72) reflecting a deliberate 'sleep cooler than daytime,"
            " not warmer' household preference. Dozens of scattered literal fallbacks in"
            " automation.py/coordinator.py/briefing.py were swept to reference these named constants"
            " instead of duplicated literals, surfacing and fixing 3 genuine latent drift bugs: the"
            " setpoint-mode-inconsistency incident detector's stray comfort_cool fallback of 76 (vs"
            " 75/74 everywhere else), coordinator.py's chart fan-activity prediction using a stray"
            " natural_vent_delta fallback of 5.0 (vs the real default 3.0), and briefing.py's"
            " away/vacation display using a stray setback_heat fallback of 62 (vs 60/63 elsewhere)."
            " 5 locked golden scenarios depending on the old default values were reviewed and re-signed."
        ),
    },
    435: {
        "version_fixed": "0.4.75",
        "title": (
            "Nat-vent activity report showed 'fan on/off' events for device \"none\" when no fan device was configured"
        ),
        "scope_covered": (
            "automation.py: nat_vent_temperature_check()'s two thermostatic-cycling branches"
            " called _activate_fan()/_deactivate_fan() and then unconditionally emitted the"
            " nat_vent_fan_on/nat_vent_fan_off event regardless of whether the call actually"
            " changed self._fan_active. Both helpers correctly no-op when fan_mode is disabled"
            " (a supported manual-window-only nat-vent configuration), but the event still fired,"
            ' and _fan_device_label() returns "none" in that config — so the rendered activity'
            " report line (ai_skills_activity.py _render_nat_vent_fan_on/_off) read 'Nat-vent fan"
            " on -- indoor X >= Y' / 'none: auto->on', claiming a nonexistent device transitioned."
            " Found via architecture-reset Step 2: fixing a sim-harness fidelity gap in"
            " tools/sim_harness/run_production.py (the harness never dispatched to"
            " nat_vent_temperature_check()/fan_thermostat_check() on an ordinary temp_update tick,"
            " missing the real _async_thermostat_changed state-listener path from"
            " coordinator.py:2837-2862) let 3 golden scenarios reach this code path for the first"
            " time and fail. Fixed by guarding both emissions on whether self._fan_active actually"
            " changed as a result of the call, rather than duplicating the fan_mode check — robust"
            " against any other no-op condition inside _activate_fan()/_deactivate_fan() (override"
            " active, dry_run, idempotency guard). Golden scenarios mild_all_day_nat_vent_only,"
            " nat_vent_active_indoor_in_band_guard_dormant, and nat_vent_ceiling_breach_hvac_escalation"
            " now pass without the spurious event. A 4th golden, whole_house_fan_hvac_suppression"
            " (which DOES configure a real fan_mode), was separately updated (not a bug — its"
            " assertion predated this code path being reachable and didn't anticipate the fan"
            " legitimately cycling off mid-session) to expect nat_vent_fan_off at the point indoor"
            " reaches the cycling off-threshold."
        ),
    },
    427: {
        "version_fixed": "0.4.74",
        "title": (
            "Overnight nat-vent session torn down and re-adopted every 5-15 min"
            " ('running (untracked)' / repeated 'startup reconcile')"
        ),
        "scope_covered": (
            "automation.py: check_natural_vent_conditions()'s Phase 2 proactive floor exit"
            " (~line 2380) was reading the flat daytime comfort_heat directly instead of the"
            " sleep-aware _nat_vent_reactivation_floor() helper already used by Priority-1 hard"
            " exit and reconcile_fan_on_startup() (Issue #417). During the sleep window this made"
            " time_to_floor go negative hours before the real floor was reached, which always"
            " satisfied the exit threshold and tore the session down every ~5 min; the physical"
            " fan kept running independently and got re-adopted as a brand-new session each time"
            " via the Issue #359 Fix D untracked-fan backstop, which Phase 2 then immediately"
            " re-exited on the next tick. Fixed by routing the floor read through"
            " _nat_vent_reactivation_floor() and guarding the block to only fire when"
            " time_to_floor >= 0 (a negative value means the floor is already breached — that"
            " belongs to the Priority-1 hard exit or nat_vent_temperature_check()'s in-session"
            " cycling, not this predictive check). No changes to reconcile_fan_on_startup(), the"
            " untracked-fan backstop, or the in-session cycling mechanism — all three already"
            " implemented the correct behavior; only Phase 2's floor source and guard needed"
            " correcting. tests/test_nat_vent_activation.py: two new regression tests"
            " (test_proactive_exit_uses_sleep_aware_floor_not_daytime_floor,"
            " test_proactive_exit_skips_when_floor_already_breached). New pending simulation"
            " scenario tools/simulations/pending/issue-427-natvent-sleep-floor-churn.json,"
            " verified to fail against the pre-fix code (reproduces the exact -2.09h churn"
            " reading from the reported activity log)."
        ),
    },
    428: {
        "version_fixed": "0.4.73",
        "title": "next_human_action gives backwards window/fan advice when outdoor is hotter than indoor",
        "scope_covered": (
            "temperature.py: added free_cooling_direction_ok(outdoor_temp, indoor_temp), mirroring"
            " automation.py's existing economizer direction_ok gate (~line 4360, Issue #327)."
            " coordinator.py: _compute_next_action() rewritten to accept outdoor_temp,"
            " windows_physically_open, and the AutomationEngine reference; every window/fan"
            " suggestion (both cooling-direction and the previously entirely-missing"
            " heating-direction mirror) is now gated on the live direction check. Added checks for"
            " physically-open windows (avoids redundant 'open windows' when already open),"
            " automation-engine live state (nat_vent/economizer already active, manual override,"
            " grace period, paused-by-door — checked early per the approved guard-ordering"
            " decision), and GUEST occupancy parity with HOME. Added INFO logging at entry and"
            " each decision outcome, WARNING when the direction guard suppresses what would"
            " otherwise have been the wrong suggestion. tests/test_coordinator.py:"
            " TestComputeNextAction now calls the real bound method via a coordinator stub instead"
            " of a hand-copied replica (the replica is how the missing outdoor check went uncaught"
            " through 20+ existing tests) — full matrix of new test cases added, including the"
            " exact reported repro (indoor 75°F / outdoor 80°F)."
        ),
    },
    424: {
        "version_fixed": "0.4.72",
        "title": "Remove selectable 'Both' fan mode; migrate existing configs to whole house fan",
        "scope_covered": (
            "config_flow.py: removed the FAN_MODE_BOTH SelectOptionDict from FAN_MODE_OPTIONS"
            " (no longer offered in setup or options flow) and bumped ClimateAdvisorConfigFlow"
            " VERSION to 17. __init__.py: added a v16->v17 migration block that coerces any"
            " existing fan_mode == 'both' config to FAN_MODE_WHOLE_HOUSE, logging a WARNING when"
            " it does so. const.py: updated the fan_mode CONFIG_METADATA description to drop the"
            " 'Both' mention. translations/en.json and strings.json: dropped the 'Both' mention"
            " from both the setup-step and options-step fan_mode field descriptions."
        ),
    },
    423: {
        "version_fixed": "0.4.71",
        "title": "Whole-house fan stuck 'active (unconfirmed)' for hours, nat-vent never resumed",
        "scope_covered": (
            "coordinator.py: added _derive_thermostat_fan_running_for_reconcile(), an"
            " archetype-aware 'is a fan running' signal for reconcile_fan_on_startup(). All 4"
            " callers (_do_startup_coalesce, the 30-min periodic backstop, the Issue #347"
            " one-shot hvac_action=fan runtime trigger, and _async_post_grace_fan_reconcile)"
            " previously derived this signal purely from the thermostat's own"
            " fan_mode/hvac_action attributes regardless of configured fan_mode — correct for"
            " FAN_MODE_HVAC, wrong for FAN_MODE_WHOLE_HOUSE (a physically separate device)."
            " Confirmed via the CA chart_log: a thermostat-internal fan blip at the moment a"
            " nat-vent proactive-floor-exit ended a real WHF session caused the runtime trigger"
            " to 're-adopt' a WHF that was never actually turned back on, wedging"
            " _fan_active=True for 3.5+ hours despite indoor/outdoor conditions strongly"
            " favoring free cooling the whole time. All 4 sites now resolve the signal via the"
            " new helper: FAN_MODE_HVAC unchanged (thermostat attrs); FAN_MODE_WHOLE_HOUSE uses"
            " _get_fan_physical_state() (the real configured WHF entity) when fan_state_feedback"
            " is enabled; FAN_MODE_BOTH ORs both signals (a strict superset of prior behavior,"
            " not a true per-device model — see scope_not_covered). automation.py: extracted"
            " _clear_fan_flags_and_start_grace() from on_fan_turned_off() (pure refactor, no"
            " behavior change for its existing caller) with a preserve_nat_vent_session"
            " parameter, and added _reconcile_fan_physical_drift(), a new self-healing check"
            " wired into the existing 5-minute _thermo_backstop_task() timer. It compares"
            " _fan_active against the real fan entity's physical state (for WHF/BOTH archetypes"
            " with feedback enabled) and, after 2 consecutive confirming ticks (~10 min, to"
            " avoid acting on command-echo/lag), self-corrects a stale flag while preserving"
            " the nat-vent session — letting the immediately-following"
            " nat_vent_temperature_check() cycling-on branch re-activate the real fan on the"
            " same tick if conditions still warrant it. Previously, nothing ever corrected a"
            " stale _fan_active — _compute_fan_status()/_compute_whf_status() already compared"
            " it against physical reality, but only to render 'active (unconfirmed)' in the UI."
        ),
    },
    418: {
        "version_fixed": "0.4.70",
        "title": "Two nat-vent exit sites bypassed the _exit_nat_vent() choke point (Issue #411 follow-up)",
        "scope_covered": (
            "automation.py: handle_all_doors_windows_closed()'s nat-vent-cleanup branch"
            " (Priority 1 sensor-all-close) and fan_thermostat_check()'s fast-loop Check 1"
            " (fast-loop mirror of the Priority 3 outdoor-rise exit) now both call"
            " _exit_nat_vent() (Issue #411's single choke point) instead of hand-rolling"
            " _natural_vent_active/_paused_by_door/_deactivate_fan() inline. The fast-loop"
            " site had a live correctness bug: it set _paused_by_door=True (implying HVAC"
            " should stay off, waiting for the window to close) but called _deactivate_fan()"
            " with the default restore_hvac=True, restoring HVAC into a window it had just"
            " marked as still open — the exact contradiction _exit_nat_vent() exists to"
            " prevent — and never captured _pre_pause_mode or checked whether a sensor was"
            " genuinely open. The sensor-all-close site had no such bug, but its immediate"
            " classification-aware restore (comfort band re-arm for warm/mild days, direct"
            " mode restore for hot days) is now traded for the generic restore-then-grace"
            " path, converging to the same eventual state via _apply_current_scheduled_state()"
            " at grace expiry (DEFAULT_AUTOMATION_GRACE_SECONDS = 5 min) instead of instantly"
            " — a deliberate tradeoff accepted for full unification, reviewed with the user"
            " before implementation."
        ),
    },
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
    },
    384: {
        "version_fixed": "0.4.53",
        "title": "HACS compliance — integration_type, dynamic README badge, state permissions, knowledge base",
        "scope_covered": (
            "manifest.json integration_type field, README dynamic version badge, "
            "state.py file permissions (chmod 0o600), docs/hacs-compliance.md, CLAUDE.md HACS section"
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
    },
    380: {
        "version_fixed": "0.4.51",
        "title": "AI investigator streaming — no visible progress + stuck 'Generating…' after report renders",
        "scope_covered": (
            "index.html: break added after done event so finally block runs immediately;"
            " loading overlay hidden on first chunk so streaming pre is visible."
            " api.py: write_eof() called before return so TCP connection closes promptly."
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
    },
    369: {
        "version_fixed": "0.4.45",
        "title": "Nat-vent paused-by-door reactivation — diagnostic logging",
        "scope_covered": (
            "Adds DEBUG logging at lockout check and temperature gate failure paths"
            " in check_natural_vent_conditions() paused-by-door block (automation.py ~line 2182)."
            " Each gate condition (delta, floor, ceiling) now logs its value and pass/fail status."
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
    },
    197: {
        "version_fixed": "0.3.55",
        "title": "Setpoint-only thermostat change triggers manual override grace period",
        "scope_covered": [
            "_async_thermostat_changed(): setpoint change without mode change now calls handle_setpoint_override()",
            "handle_setpoint_override() enters grace period immediately (no confirmation window)",
            "Override detection correctly fires for temperature-only user adjustments",
        ],
    },
    203: {
        "version_fixed": "0.3.55",
        "title": "Sensor health comprehension TypeError on int instrumentation keys",
        "scope_covered": [
            "sensor.py _compute_sensor_health(): isinstance(k, str) guard on key iteration",
            "Prevents TypeError when coordinator.data contains numeric keys from HA instrumentation",
        ],
    },
    204: {
        "version_fixed": "0.3.55",
        "title": "Bedtime setback and morning wakeup respect active manual override",
        "scope_covered": [
            "automation.py apply_bedtime_setback(): checks _manual_override_active before setting setpoints",
            "automation.py apply_morning_wakeup(): same guard applied symmetrically",
            "clear_manual_override() callsites audited — override cleared at correct lifecycle points",
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
    },
    141: {
        "version_fixed": "0.3.43",
        "title": "chart_log endpoint estimator replaces passive_decay OLS",
        "scope_covered": [
            "chart_log endpoint — estimator uses chart_log data for R² calculation",
        ],
    },
    139: {
        "version_fixed": "0.3.42",
        "title": "Persist pred_archive across restarts + UTC key rounding",
        "scope_covered": [
            "coordinator._pred_archive — persisted across HA restarts",
            "chart_log timestamp keys — UTC rounding applied consistently",
        ],
    },
    135: {
        "version_fixed": "0.3.37",
        "title": "Chart log pred_indoor/pred_outdoor nearest-entry lookup",
        "scope_covered": [
            "chart_log endpoint — hourly forecast lookup uses nearest-entry not exact-hour match",
            "pred_indoor/pred_outdoor — non-null after this fix",
        ],
    },
    134: {
        "version_fixed": "0.3.37",
        "title": "Nat-vent fan preserved through HVAC-off classification; grace period nat-vent re-entry",
        "scope_covered": [
            "automation._apply_classification() — nat-vent fan preserved when classification sets HVAC off",
            "automation._resume_from_grace() — nat-vent re-entry allowed when indoor exceeds comfort_cool",
        ],
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
    },
    108: {
        "version_fixed": "0.3.22",
        "title": "Sleep temp config no longer enforces ordering vs comfort/setback",
        "scope_covered": [
            "config_flow — sleep_heat/sleep_cool ordering validation removed",
        ],
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
    },
    158: {
        "version_fixed": "0.3.51",
        "title": "Investigation history full report + AI deduplication",
        "scope_covered": [
            "Investigation history panel shows full report text (not just summary)",
            "AI system prompt gains deduplication rule — findings not repeated across sections",
        ],
    },
    160: {
        "version_fixed": "0.3.52",
        "title": "Temperature Forecast chart historical navigation via before_ts anchor",
        "scope_covered": [
            "/api/climate_advisor/chart_data?before_ts=<epoch> endpoint parameter",
            "Chart backward '<' navigation fetches historical window anchored before current view",
            "Chart log lookback bounded by available chart_log retention (~365 days)",
        ],
    },
    162: {
        "version_fixed": "0.3.52",
        "title": "Chart forward navigation after historical re-fetch",
        "scope_covered": [
            "Chart '>' button after backward navigation re-anchors to the retrieved window"
            " rather than jumping directly to current time",
        ],
    },
    164: {
        "version_fixed": "0.3.52",
        "title": "Chart forward navigation into predicted future temperatures",
        "scope_covered": [
            "Chart '>' button beyond latest historical data advances into the physics-simulated"
            " predicted indoor ODE window",
            "Predicted window fetched via before_ts pointing past current time",
        ],
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
    },
    221: {
        "issue": 221,
        "title": "Away setback setpoint change falsely detected as manual override",
        "version_fixed": "0.3.56",
        "scope_covered": [
            "_temp_command_time guard added to setpoint-only override detector",
            "Away setback no longer starts spurious 90-minute grace period",
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
    },
    318: {
        "title": "Sleep setpoint ordering constraint regression",
        "version_fixed": "0.4.15",
        "scope_covered": [
            "config_flow.py async_step_setpoints — removed 4 incorrect cross-field constraints"
            " on sleep_cool/sleep_heat vs comfort/setback bounds",
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

# Default setpoints (°F) — reformatted to match a real, tuned installation's own
# configured values (architecture-reset session, user-requested) rather than
# arbitrary round numbers. Does NOT affect the version 14->15 migration in
# __init__.py, which intentionally preserves the OLD historical defaults
# (70/75/60/80) as its own literal fallbacks for backfilling PRE-EXISTING
# installs that upgrade through that specific version transition — that is
# backward-compatibility logic, not a "new install" default, and must not change.
DEFAULT_COMFORT_HEAT = 68
DEFAULT_COMFORT_COOL = 74
DEFAULT_SETBACK_HEAT = 63
DEFAULT_SETBACK_COOL = 79

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
DEFAULT_SENSOR_DEBOUNCE_SECONDS = 600  # 10 minutes (Issue #504 — was 5 min)
DEFAULT_MANUAL_GRACE_SECONDS = 1800  # 30 minutes
DEFAULT_AUTOMATION_GRACE_SECONDS = 300  # 5 minutes
DEFAULT_WELCOME_HOME_DEBOUNCE_SECONDS = 3600  # 60 minutes
DEFAULT_OVERRIDE_CONFIRM_SECONDS = 600  # 10 minutes
OCCUPANCY_SETBACK_MINUTES = 15
MAX_CONTINUOUS_RUNTIME_HOURS = 3

# Issue #444: _apply_comfort_band() has no source-of-truth "did the band actually
# change" check, so overlapping triggers (startup coalesce + its own follow-on
# Issue #444's original COMFORT_BAND_EVENT_DEDUP_SECONDS (10-minute time-windowed dedup)
# was replaced by Issue #591's shared, permanent (content-keyed) AutomationEngine.
# _recent_duplicate() helper — see automation.py._apply_comfort_band(). A real 11-minute
# production gap slipped past the old fixed window (Issue #591/#590 Finding D/Delta 1).

# Issue #530: an RF-remote-timer-linked manual grace period's software-tracked expiry and
# the timer's own hardware-side completion are the same physical event, but don't land at
# the exact same instant — confirmed live at an 11-second gap, with follow-on RF chatter
# settling within 60 seconds. This window (generous vs. that observed gap) marks how long
# after such a grace expires a fan-off report is still treated as the tail of that SAME
# timer boundary, not a fresh, independent event requiring its own new grace period.
TIMER_BOUNDARY_SETTLE_SECONDS = 120  # 2 minutes

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

# QuietCool RF remote timer events (Issue #486)
# Optional `event.*` entity from the gunkl/quietcool-house-fan ESPHome firmware.
# See docs/fan-remote-spec.md for the full firmware event contract.
CONF_FAN_REMOTE_ENTITY = "fan_remote_entity"
REMOTE_TIMER_EVENT_HOURS = {
    "timer_1h": 1.0,
    "timer_2h": 2.0,
    "timer_4h": 4.0,
    "timer_8h": 8.0,
    "timer_12h": 12.0,
    "timer_none": None,  # remote's default: use configured manual_grace_seconds
}

# QuietCool RF remote speed events (Issue #519). The firmware already emits these on an
# explicit speed-select press (0x1F/0x2F/0x3F); CA previously dropped them entirely. No
# CONFIG_METADATA entry -- this is not user-facing config, it's a fixed token set.
REMOTE_SPEED_TOKENS = frozenset({"low", "medium", "high"})

# Issue #519: window to combine a single physical multi-field remote interaction (a speed
# confirmation and a timer confirmation, transmitted as separate packets moments apart for
# ONE user action) into one decision instead of two. Grounded in the firmware's own documented
# protocol timing (docs/remote-capture-protocol.md in gunkl/quietcool-house-fan):
# SAME_BURST_TOLERANCE_MS=400ms per-value repeat spacing, CONFIRM_WINDOW_MS=1500ms per-field
# confirm cycle, multi-field bursts observed arriving within a similar few-second span.
# Internal-only, not user-configurable. Provisional pending live-hardware confirmation after
# the firmware change ships -- see the Verification step that tunes this against real capture
# data, same status as the firmware's own SELF_ECHO_WINDOW_MS.
REMOTE_BURST_WINDOW_SECONDS: float = 1.5

# Issue #519: object_id substring hint used to find the sibling ambient-speed text_sensor on
# the same ESPHome device as CONF_FAN_REMOTE_ENTITY, via the entity/device registry. Kept
# liberal (not an exact suffix match) since firmware naming could vary across forks/versions.
REMOTE_SPEED_SENSOR_OBJECT_ID_HINTS: tuple[str, ...] = ("speed",)

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

# Issue #685: shadow-engine diagnostic cascade-noise debounce. Real multi-step
# production transitions can fire several distinct top-level automation-engine
# method calls in a fast cascade (confirmed via live log evidence: a 2026-08-19
# 04:55:20-04:55:31 cascade hit 4 comparison axes, resolved in 11.71s). A WARNING
# is only logged once a comparison axis has continuously disagreed for this many
# wall-clock seconds — not a count of consecutive snapshots, since duplicate
# mirrored calls can fire 1-2ms apart during a real cascade.
SHADOW_ENGINE_DIAGNOSTIC_DEBOUNCE_S = 60  # Issue #685: cascade-noise debounce, see investigation evidence

# Issue #641: hard safety floor on CA-issued fan (WHF/HVAC-fan) toggle frequency —
# defense-in-depth against ANY future oscillation bug (not tied to one root cause),
# protecting the physical relay from rapid on/off/on cycling. A plain internal safety
# constant, not a CONF_* option (matching NAT_VENT_HYSTERESIS_F/MIN_VIABLE_NAT_VENT_HOURS
# precedent) — not something a user should be able to weaken below what the hardware needs.
FAN_MIN_TOGGLE_INTERVAL_S = 300

# Nat-vent soft-start sub-mode (Issue #540, scoped from #533): WHF-purge/comfort activation
# at outdoor/indoor parity once today's outdoor temp is confirmed past its peak and
# declining. Opt-out (default on) — users who want the old strict-delta-only behavior can
# disable it. No humidity/dew-point guard exists today; the comfort benefit itself is
# still subjective, but the project has chosen to default this on rather than opt-in.
CONF_NAT_VENT_SOFT_START_ENABLED = "nat_vent_soft_start_enabled"
DEFAULT_NAT_VENT_SOFT_START_ENABLED = True

# Degrees below today's observed outdoor peak required before soft-start considers the
# day "declining" — mirrors NAT_VENT_HYSTERESIS_F's role as a noise-margin buffer.
PEAK_DECLINE_MARGIN_F = 1.0

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

# Real WARNING+/ERROR log-record ring buffer cap (Issue #578) — see log_capture.py
LOG_CAPTURE_CAP = 200

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
            "How long a door/window sensor's state must hold steady before Climate Advisor acts on"
            " it — pausing/resuming HVAC, or engaging/exiting natural-ventilation fan control"
            " (whole-house fan or HVAC fan). Applies to every controlled device, not just HVAC."
            " Short values react faster but are more exposed to quick trips through a door or a"
            " flaky sensor bounce; longer values are steadier but slower to respond to a genuine change."
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
            " 'HVAC fan' uses the thermostat fan mode."
            " Fan activates during economizer maintain phase."
        ),
        "category": "fan",
    },
    "nat_vent_soft_start_enabled": {
        "label": "Nat-Vent Soft-Start (Purge Mode)",
        "description": (
            "When enabled, the whole-house fan may start at outdoor/indoor temperature parity"
            " (not waiting for outdoor to be measurably cooler) once today's outdoor temperature"
            " is confirmed past its peak and declining — for air movement and attic/thermal-mass"
            " purge, not bulk cooling. On by default; disable if you only want the fan to run"
            " once outdoor is measurably cooler than indoor. No humidity/dew-point sensor guards"
            " this today."
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
    "fan_remote_entity": {
        "label": "Fan RF Remote Event Entity",
        "description": (
            "Optional event entity (e.g. from the gunkl/quietcool-house-fan ESPHome firmware) that"
            " fires when the physical RF wall remote is pressed. When set, a remote timer selection"
            " (1/2/4/8/12 hours) sets the duration of the fan manual-override grace period, so CA"
            " honors the user's chosen runtime instead of the configured default. Leave blank to"
            " disable — no subscription is created and behavior is unchanged."
        ),
        "sensitive": False,
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
            " Limits on-demand usage from features like the Investigative Analysis report."
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
    # ai_investigator_model / ai_investigator_reasoning_effort / ai_investigator_max_tokens
    # removed from the options UI (Issue #563) — the investigator now shares the single
    # `ai_model` config used everywhere else, instead of a separate persistent
    # model/reasoning/token-budget block. The CONF_AI_INVESTIGATOR_MODEL/_REASONING/
    # _MAX_TOKENS constants and their config-entry migration defaults are kept (not
    # deleted) purely so the historical v13->v14 config migration in __init__.py
    # doesn't break for very old installs — nothing reads these values anymore.
    "ai_investigator_requests_per_day": {
        "label": "Investigator Requests Per Day",
        "description": (
            "Maximum investigative analysis runs per day."
            " Each investigation uses extended thinking and is more expensive than other AI requests."
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
DEFAULT_SLEEP_HEAT = 64.0  # comfort_heat(68) - DEFAULT_SETBACK_DEPTH_F(4) — still holds
DEFAULT_SLEEP_COOL = 72.0  # a real, tuned installation's own value — NOT derived from
# comfort_cool + DEFAULT_SETBACK_DEPTH_COOL_F (that formula assumes a warmer/looser
# overnight setback for economizing; this household's real preference is the opposite
# direction — cooler for sleep, not warmer — so this is now an independent flat default,
# matching the confirmed-correct P3 bedtime-application behavior (Issue #435/#436
# investigation found production already applies this flat value, not the formula).
MAX_SETBACK_DEPTH_F = 8.0  # never set back more than this
SETBACK_RECOVERY_BUFFER_MINUTES = 30  # pre-heat leads wake_time by this much

# ---------------------------------------------------------------------------
# Overnight Pre-Cool Phase (Issue #258)
# On warming-trend nights, CA applies a cooler ceiling mid-night to bank thermal mass.
# ---------------------------------------------------------------------------
PRE_COOL_POST_NAT_VENT_DELAY_MINUTES: int = 30  # delay after nat-vent window closes before AC pre-cool fires
PRE_COOL_WAKE_OFFSET_HOURS: float = 4.0  # fallback trigger: this many hours before wake_time
# Issue #558: fallback modifier used when overnight pre-cool is triggered by tomorrow's absolute
# hot-day classification rather than by a warming trend (setback_modifier stays 0 on a plateaued
# stretch of hot days, which would otherwise make compute_pre_cool_target() a no-op vs. the plain
# sleep_cool floor). Reuses the magnitude of the retired daytime hot-day catch-up offset, now
# applied only within this patient, nighttime-only mechanism.
HOT_DAY_PRE_COOL_MODIFIER: float = -2.0
# Pre-cool target floor is sleep_heat + nat_vent hysteresis (compute_pre_cool_target() in
# automation.py) — the same "+1 above the floor" convention nat_vent_temperature_check() uses
# for sleep-window fan cycling. Replaces the old comfort_heat + 2F floor (architecture-reset
# session), which left little to no headroom once DEFAULT_SLEEP_COOL was reformatted to a flat,
# cooler-than-daytime default (Issue #436).
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

# ---------------------------------------------------------------------------
# Grace-period adopt-on-match (Issue #483)
# ---------------------------------------------------------------------------
# How close the thermostat's live setpoint must be to the setpoint select_comfort_band()
# would arm right now for a manual mode-override to be considered "matching" automation's
# current decision and adopted early (see _override_matches_current_decision() in
# automation.py). Deliberately tight -- this only exists to catch minor floating-point/
# rounding noise, not to treat a genuinely different user-chosen temperature as a match.
OVERRIDE_ADOPT_SETPOINT_TOLERANCE_F: float = 1.0

# Issue #664: the ONLY two `_start_grace_period(trigger=...)` values that mean "this grace
# exists to protect an active manual/fan override" — read by `_start_grace_period()` to set
# `_grace_protects_override`, which `coordinator._check_orphaned_grace()` uses to scope its
# self-heal to grace types that can actually BE orphaned (an override was cleared without its
# grace being cancelled alongside it). Every other grace trigger (fan-off cooldown, physical-
# drift correction, window-close resume, nat-vent-exit resume, dashboard resume) never sets
# `_manual_override_active`/`_fan_override_active` in the first place by design — treating
# their absence as "orphaned" was the root cause of #530's fan-off grace being killed within
# ~1ms of starting. A future grace-starting call site is automatically excluded here unless
# its trigger string is deliberately added to this set — if it's meant to protect a real
# override, add it; if not, leave it out. Single source of truth (Issue #664) — previously
# also hand-duplicated in override_grace_start.py's own module-level default, which risked
# silent drift since override_grace_fsm.py's call site never passed the real set explicitly.
GRACE_TRIGGERS_PROTECTING_OVERRIDE: frozenset[str] = frozenset({"fan_manual_override", "override_confirmed"})

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
DEFAULT_AI_MODEL = "claude-sonnet-5"
DEFAULT_AI_REASONING_EFFORT = "low"
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

# Model options — Issue #563: these are the OFFLINE FALLBACK defaults, not "the" list.
# claude_api.py's fetch_available_models() fetches the live registry from Anthropic at
# runtime for both the config flow dropdown and capability-tier deprecation fallback;
# this static list is only used when that live fetch fails (no network, no API key yet,
# unsupported SDK version, etc.) — keep it reasonably current, but it is a safety net,
# not the source of truth for what models are actually available.
AI_MODEL_SONNET_5 = "claude-sonnet-5"
AI_MODEL_SONNET = "claude-sonnet-4-6"
AI_MODEL_OPUS = "claude-opus-4-6"
AI_MODEL_HAIKU = "claude-haiku-4-5-20251001"
AI_MODELS = [AI_MODEL_SONNET_5, AI_MODEL_SONNET, AI_MODEL_OPUS, AI_MODEL_HAIKU]

# Per-model request-shape capabilities (Issue #572) — replaces the reactive
# learn-from-a-live-failure approach (#563/#565/#568/#569), which guaranteed a silent,
# ~90s zero-output failure on a model's first-ever request before it could "learn" the
# correct shape, and whose learned state could be lost on a real HA restart (the
# #568/#569 persistence fix only covered the config-reload shutdown path, not
# EVENT_HOMEASSISTANT_STOP). This product supports a small, known set of Claude
# models, not an arbitrary universe of them, so the correct shape is hardcoded here
# instead of discovered live.
#
# Verified 2026-08-05 via direct calls to the Anthropic Messages API (not simulated):
#   - claude-sonnet-4-6 / claude-opus-4-6 / claude-haiku-4-5-20251001: accept
#     `temperature`; no thinking control needed at low/medium; legacy
#     `thinking:{type:enabled,budget_tokens:N}` shape used at "high" only — the
#     behavior this integration has always used for these models, unchanged.
#   - claude-sonnet-5 / claude-opus-5: reject `temperature` outright (400,
#     "`temperature` is deprecated for this model"); reject the legacy thinking shape
#     outright (400, "`thinking.type.enabled` is not supported for this model. Use
#     `thinking.type.adaptive`..."); confirmed producing real visible output with the
#     adaptive shape at both a small test prompt and full production scale
#     (max_tokens=8192) — claude-sonnet-5's silent zero-output failure without any
#     thinking control was also independently confirmed at production scale by
#     Issue #565's own live diagnostic and in live HA logs (2026-08-05).
#   - claude-fable-5: rejects `temperature` (same family pattern); adaptive shape
#     assumed by family consistency with sonnet-5/opus-5, not independently
#     re-confirmed this session.
#   - claude-haiku-5: does not exist (404 not_found_error as of 2026-08-05) — not a
#     real model ID, intentionally omitted.
#
# A model not in this table (e.g. a brand-new Anthropic release picked from the live
# model list before this table is updated) falls back to the "legacy" shape — the
# behavior proven safe for years prior to claude-sonnet-5 — and logs a WARNING naming
# the model, rather than silently guessing or reactively probing it live.
AI_THINKING_SHAPE_LEGACY = "legacy"
AI_THINKING_SHAPE_ADAPTIVE = "adaptive"

AI_MODEL_CAPABILITIES: dict[str, dict] = {
    AI_MODEL_SONNET: {"thinking_shape": AI_THINKING_SHAPE_LEGACY, "supports_temperature": True},
    AI_MODEL_OPUS: {"thinking_shape": AI_THINKING_SHAPE_LEGACY, "supports_temperature": True},
    AI_MODEL_HAIKU: {"thinking_shape": AI_THINKING_SHAPE_LEGACY, "supports_temperature": True},
    AI_MODEL_SONNET_5: {"thinking_shape": AI_THINKING_SHAPE_ADAPTIVE, "supports_temperature": False},
    "claude-opus-5": {"thinking_shape": AI_THINKING_SHAPE_ADAPTIVE, "supports_temperature": False},
    "claude-fable-5": {"thinking_shape": AI_THINKING_SHAPE_ADAPTIVE, "supports_temperature": False},
}

# Default capabilities for a model not present in AI_MODEL_CAPABILITIES.
AI_MODEL_CAPABILITIES_DEFAULT: dict = {
    "thinking_shape": AI_THINKING_SHAPE_LEGACY,
    "supports_temperature": True,
}

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

# Investigation report history (Issue #82)
INVESTIGATION_REPORT_HISTORY_CAP = 60
INVESTIGATION_REPORTS_FILE = "climate_advisor_investigation_reports.json"

# Sensor attributes for AI status
ATTR_AI_STATUS = "ai_status"

# API paths for AI endpoints
API_AI_STATUS = f"{API_BASE}/ai_status"
API_AI_INVESTIGATE = f"{API_BASE}/ai_investigate"
API_INVESTIGATION_REPORTS = f"{API_BASE}/investigation_reports"
API_DELETE_REPORT = f"{API_BASE}/delete_report"
