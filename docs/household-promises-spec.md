<!-- Nav: ← [Invariant Watchdog Brief](invariant-watchdog-brief.md) | → [invariant_watchdog.py](../custom_components/climate_advisor/invariant_watchdog.py) | ↔ [Development Guide](DEVELOPMENT.md) -->

# Household Promises — Territory Spec (Tier 3)

Issue #1128 (epic #1125, Step 3). Design record: `plans/test-automation/03-step3-design.md` (§4, §5, §14).

## Anchors

| Question | Short answer | → Full answer |
|---|---|---|
| What is a household promise? | A pure check over recent ground truth (thermostat/fan readings and the commands CA actually sent) that says "this must never happen in a home". One registry (`PROMISES`) is run by production and by every test world. | [Scope](#scope) |
| Which promises exist and what do they mean? | P1 settling_flip, P2 ac_whf_mutex, P3 observe_only_silence, P4 override_respected, P5 no_blind_fan_start, P5b blind_fan_continue, P6 mode_flip_rate, P7 comfort_kept. P8 surfaces_agree is test-only (readers in `tools/sim_harness/surfaces.py`, ratchet in `tools/simulations/surface_divergences.json`). | [The promises](#the-promises) |
| What happens at home when a promise is missed? | Log-only: one WARNING, an `invariant_violation` event with `severity="log"`, a persisted counter. No notification, no Status line. Only P2 alerts. | [Home behaviour](#home-behaviour-and-severity) |
| Which override endings does P4 allow? | A fixed list per device (thermostat vs fan vs fan-off grace). Everything else is a miss. | [P4 exceptions](#p4-listed-exceptions) |
| When does a log-only promise become an alert? | After about two quiet weeks on the live install, every hit having been fixed or tuned, via a one-line `home_level` change in a normal patch release. | [Promotion policy](#promotion-to-alert-policy) |
| How do tests and generated days use them? | `PromiseProbe` feeds the same journal and calls `run_promise_checks(scope="test")`; `simulate.py --generate N` judges random days with it. | [Test-side use](#test-side-use) |

## Scope

- **Files:** `custom_components/climate_advisor/invariant_watchdog.py` (checks, `PromiseNumbers`, `PromiseConfig`, `PROMISES`, `run_promise_checks`), `custom_components/climate_advisor/promise_journal.py` (evidence records and the bounded `PromiseJournal`), `coordinator.py` (`_tick_household_promises`, `_evaluate_household_promises`, `_promise_observe_command`), `tools/sim_harness/promise_probe.py`, `generator.py`, `replay.py`, `trial_gate.py`, `world.py`.
- **Entry points:** `run_promise_checks(inp, cfg, scope=...)`; `PromiseJournal.window(now)`.
- Not covered: the original hard invariant `check_ac_whf_mutex` contract ([invariant-watchdog-brief.md](invariant-watchdog-brief.md)); the real-HA tier ([DEVELOPMENT.md](DEVELOPMENT.md#real-ha-tier-the-house-markers-revert-proof-snapshots-step-2-issue-1127)).

Ground-truth rule (brief Invariant 1) with one amendment dated 2026-10-29: only P4 may read CA's own override/grace events (`Episode`/`Mark`); every other promise reads readings and issued commands only. A promise never encodes the algorithm (no gates, flags or FSM states), so a new code path that breaks the household outcome is still caught.

## The promises

Tests: severity from `PromiseSpec.test_severity`. Home: `PromiseSpec.home_level`.

| # | key | Plain-English promise | Tests | Home | Kind |
|---|---|---|---|---|---|
| 1 | `settling_flip` | After the AC ran, a shallow dip below the floor does not start the heat (and the mirror: after heat, a shallow rise does not start the AC), unless the occupant, an override, or a deliberate setpoint explains it. "Shallow" follows the user's configured settling tolerance. | hard | log | event |
| 2 | `ac_whf_mutex` | The AC never runs while the whole-house fan runs. Snapshot check, existing behaviour. | hard | **alert** | state |
| 3 | `observe_only_silence` | With automation switched off, CA sends no thermostat or fan command. No tolerance. | hard | log | event |
| 4 | `override_respected` | A manual override is not undone before its grace ends, except by a listed event; also flags an unlisted early end that leaves the override unprotected. | hard | log | event |
| 5 | `no_blind_fan_start` | CA does not start a fan while its indoor reading was unreadable at send time. | hard | log | event |
| 5b | `blind_fan_continue` | A fan already running does not keep running more than 35 min with no indoor reading. | warning | log | event |
| 6 | `mode_flip_rate` | At most 2 heat/cool flips per hour as reported by the thermostat (occupant-caused flips within 5 min are excused). | warning | log | event |
| 7 | `comfort_kept` | While CA is in control, the house is not outside the comfort range for more than 45 min. | warning | log | event |
| 8 | `surfaces_agree` | What CA shows (Status API, chart band, briefing, Next User Action, ODE ceiling, nat-vent target, fan and status cards) matches the band the engine arms. Test-only; **not in the registry** (it needs live coordinator objects, see [P8](#p8-surfaces_agree-test-only)). | hard (ratcheted) | none | n/a |

### P1 and P4 judging details (Issues #1150, #1151)

- **P1 lookback stops at same-side equipment (#1150).** A flip is opposite, then idle, then same-side, with no same-side run in between. Scanning back from a heat start, the first `heating` reading ends the search with no hit (the mirror for a cool start). A furnace restart after an earlier in-family heat run is a normal cycle, not a flip.
- **P1 excuse anchor (#1150, owner sign-off pending).** The 30 min occupant excuse is measured to the equipment start. `P1_EXCUSE_ANCHOR_ON_ARMING_COMMAND` (default `False`) additionally measures it to the first CA `set_hvac_mode` into the same family within 30 min before the start (the occupant spoke, CA armed in response, the equipment lagged). It widens the excuse (design Invariant 2), so it stays off until the owner approves; flipping it is a one-line change.
- **P4 reports one hit per reversal instant (#1151).** A `set_hvac_mode` and a `set_temperature` in the same tick are one reversal: dedupe by (episode, ts); the hit key is `override_respected:<episode>:<ts>` with no service. Wording: "... {N} min before your manual change was due to end." (N rounded half up) when grace remained, otherwise "... while your manual change was still in force."

Tunable numbers live in `PromiseNumbers` (e.g. `P5_BLIND_CONTINUE_MIN=35`, `P6_MAX_FLIPS=2`, `P7_MAX_MIN=45`); each is deliberately looser than production's own allowance and `tests/test_promise_numbers.py` walks the legal configuration grid to prove it.

## Home behaviour and severity

`ClimateAdvisorCoordinator._tick_household_promises()` runs on a 5-minute heartbeat: record a reading, then `_evaluate_household_promises()` runs `run_promise_checks(..., scope="home")`. For each new hit (P2 is skipped; it keeps its existing alert path in `_run_invariant_watchdog`): WARNING `Household promise missed (log only)`, `bump_promise_stats`, an `invariant_violation` event (`severity="log"`), and a state save. A check that raises is logged and skipped at home but re-raised in tests.

- **Occupant/operator view:** the AI Investigator shows these as "Noted, not alerted" lines (`ai_skills_context.py`); diagnostics show per-promise hit counts and last timestamps (`diagnostic_snapshot.py`, `promise_stats`, counts and timestamps only).
- **Dedup:** event promises use the journal's `seen_keys` LRU (256); long-running hits (P5b, P7) use `RunKey` so a sliding window does not re-report the same run.

## The journal

`PromiseJournal` is a pure leaf (no HA imports, no I/O), in-memory only, fed by production hooks and by test adapters. Retention `JOURNAL_MAX_AGE` 3 h; caps: 400 readings, 128 commands, 128 marks, 64 episode marks, 8 open episodes, 256 seen keys. A reading is stored when a discrete field changed, indoor moved 0.5 F, or 5 min passed. After a restart it is empty, so history-needing checks see no data and report nothing (a blind window, never a false alarm). Only `promise_stats` is persisted (capped to the promise keys, type-validated by `sanitize_promise_stats`). Commands are journaled through `AutomationEngine._command_observer` and, for the coordinator's own fan reconcile command, `_promise_observe_command` directly (Issue #1129: that command carries no Context by design; investigated, not a defect, locked by tests).

## P4 listed exceptions

Source: `invariant_watchdog.py` (`_COMMON_LISTED`, `_THERMOSTAT_LISTED`, `_FAN_OVERRIDE_LISTED`, `_p4_end_is_listed`).

| Ending | Thermostat override | Fan override | Fan-off grace |
|---|---|---|---|
| `grace_expired`, `superseded`, `restart` | yes | yes | yes |
| `user_cancel*` (dashboard End Grace) | yes | yes | yes |
| `adopted`, `adopted_matching_decision`, `self_resolved` | yes | yes (shared set) | no |
| `ac_whf_mutex`, `new_override_during_grace` | yes | yes | no |
| `occupancy_*` (away/vacation) | yes | yes | no |
| `bedtime`, `morning_wakeup` | **no** (the #1119 shape) | yes | no |
| reason containing `ceiling` (#134) | no | no | yes |
| `stuck_grace_recovered` | not excused | not excused | not excused |

**What the episode records for a setpoint change during a door pause (Issues #1131/#1133).** A setpoint-only change made while a door/window pause has the thermostat Off is recorded with `override_detected.detected_mode` = the pre-pause mode (`AutomationEngine._occupant_mode_when_ca_holds_off()`), not Climate Advisor's own pause `off`: the thermostat reads Off only because CA put it there, and what the occupant asked for is HVAC at their setpoint. Recorded as `off`, the door-close mode restore read as CA reversing the occupant's value. A temperature-only change while the whole-house fan owns HVAC is not an override at all (no episode). P4 itself is unchanged; the 10 seeds x 100 coordinator-day calibration is `override_respected` 30 hits before the #1131/#1133/#1146/#1147/#1148 fixes and 0 after, so `tests/test_sim_generator.py::_UNTRIAGED_ON_HEAD` no longer lists it.

Reversal: thermostat mode differing from the occupant's, or a setpoint differing by at least 2.0 F; fan: the opposite on/off. A command equal to the occupant's value is a re-affirm. A newer occupant action after the episode start excuses a later CA command. A restart empties the journal, so restart survival (#1006, #1045) belongs to the real-HA lifecycle tests.

## Test-side use

- `PromiseProbe` (`tools/sim_harness/promise_probe.py`) owns no promise logic: it polls a `PromiseSource`, records into `PromiseJournal`, runs `run_promise_checks(scope="test")`, and takes severity from the registry. Hard hits fail; warning hits are held by a ratchet baseline (`promise_baseline.json`, may only go down). Known hits on LOCKED goldens are recorded in `promise_waivers.json` (scenario, promise, reason, issue), never by editing a golden.
- Goldens are checked through the probe (`tests/test_goldens_promises.py`); the real-HA tier feeds it as well (`tests_ha/test_goldens_realha.py`).
- Generated days: `python tools/simulate.py --generate N [--seeds S ...] [--profile coordinator|engine|mixed]` runs N random households per master seed closed-loop against the real engine (toy house in `world.py`); `--generate-day DAY_SEED` replays a seed; `--replay-generated FILE [--shrink]` replays and shrinks a saved failure. `day_seed = master_seed * 1_000_000 + index`. `tools/sim_harness/trial_gate.py` measures whether generated days find a known regression unaided. The generator knows no promise.
- Mechanism evidence and the nightly waiver ledger (Issue #1153): every `ProbeHit` carries `mechanism`, a flat record with every field present and `null` where unknowable (`tools/sim_harness/mechanism.py`: cause, command service/target/mode/reason/origin, episode kind/source/grace seconds, configured grace, override/grace FSM flags, sensor-open, commit). Nightly seeds are fresh, so `--generate N --waiver-ledger tools/sim_harness/nightly_waivers.json --issue-state github` tolerates a known hard hit only through a ledger entry tied to an OPEN issue, with `added`/`expires` (max 45 days apart), matched on promise + mechanism fields (never a seed). A closed issue, a passed expiry, a hit that no longer occurs, a duplicate, or a lookup that cannot prove the issue open fails the run. Offline (`--issue-state offline [--issue-state-file F]`, the default) skips the closed-issue check with a loud warning.

## P8 surfaces_agree (test-only)

Occupant first: the person reads the Status card, chart, briefing and Next User Action line to learn what the house is held to. If the thermostat is armed 64-72 F at night while Next User Action still reasons from 68-74 F, the page cannot be trusted. P8 asks, per surface, "does the range you quote equal the range the engine is armed with right now?".

- **Readers call the real production code** on the FakeHass coordinator session (`tools/sim_harness/surfaces.py`); nothing is re-derived. Armed reference: `engine._resolved_comfort_band()`. Surfaces: chart (`coordinator._build_target_band_for()` + `_target_band_lower_upper_now()`, fed a synthetic hourly forecast through its own `hourly_forecast_override` argument), the real `ClimateAdvisorStatusView.get()` (`ca_target_heat/cool`), the real `generate_briefing()` HVAC Mode row, `_compute_next_action()` boundary-probed (it exposes no band, so indoor is swept and the guidance flip points are compared), `engine._ceiling_threshold()`, `_nat_vent_target_now()` (must sit inside the band), the live thermostat setpoint (must not be looser than the armed edge), and the status and fan cards (`_compute_automation_status`, `_compute_next_automation_action`, `_compute_fan_status`, `_compute_whf_status`, `_compute_hvac_fan_status`, which must answer and agree with the physical fan).
- **Applicability gates (documented, not tuned to pass):** chart only on a steady stretch (the hourly bucket equals both neighbours) with no held arrival restore, and only for a household config that carries the four required sleep keys (the config flow guarantees them; the engine and chart use different built-in defaults when they are absent); briefing only awake on a heat/cool day; thermostat hold only in a quiet state (no override, grace, pause, session or recent CA command), only when the live value is what CA last wrote, and only after it persists longer than the welcome-home debounce plus 5 min; a looser-than-armed hold is a miss, a tighter one (pre-cool, TOU banking) is deliberate.
- **Harness shim:** scenario `occupancy_*` events drive the engine only, so the coordinator's `_occupancy_mode` is aligned to the engine's for the duration of one sample (restored after), reproducing production, where the coordinator changes mode first.
- **Not in `PROMISES`:** that tuple holds pure `check(PromiseInput, PromiseConfig)` functions that also run at home; P8 needs live objects. `P8_KEY`, `P8_SEVERITY="hard"`, `P8_HOME_LEVEL=None` live in `surfaces.py`.
- **Ratchet:** `tools/simulations/surface_divergences.json` lists divergences known on HEAD (`surface`, `scenario` or `context: generated`, `issue`, `evidence`). Unlisted divergence fails; a listed row that no longer diverges also fails. Goldens are LOCKED, so nothing is waived by editing one.
- **Run it:** `python tools/simulate.py --surfaces [--generate N --seeds S ...]` (all 91 goldens plus optional generated days); unit tier: `tests/test_promise_surfaces.py` (goldens plus the fixed generated sample seeds 101/102 x 10 days); nightly: `.github/workflows/nightly.yml` (P8 over fresh seeds).

## Promotion to alert policy

1. `promise_stats[key]` tracks `hits`, `first_ts`, `last_ts`; "two quiet weeks" means about 14 days without a hit on the live install.
2. Every hit seen meanwhile is fixed (issue plus regression test) or its `PromiseNumbers` value is tuned with a note. Never silently waived.
3. Promotion is a one-line `home_level` change in `PROMISES`, shipped as a normal patch release (version bump, `add_fix_entry.py`, CHANGELOG). The design names P1, P3, P4 and P5 as candidates; P5b, P6 and P7 stay log. Changing `PROMISES` needs human sign-off.

## Error Conditions

| Failure | Handling |
|---|---|
| A check raises at home | WARNING `Household promise check failed`, check skipped |
| Reading/command/event hook raises | DEBUG log, skipped; never reaches the update cycle |
| Corrupt persisted `promise_stats` | Sanitized to the known keys on load |

## Code Reference

- `PROMISES` and `run_promise_checks` in `custom_components/climate_advisor/invariant_watchdog.py`
- `PromiseJournal` in `custom_components/climate_advisor/promise_journal.py`
- `ClimateAdvisorCoordinator._evaluate_household_promises` in `coordinator.py`
- `tools/sim_harness/promise_probe.py`, `generator.py`, `replay.py`, `trial_gate.py`
