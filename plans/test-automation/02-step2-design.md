# Step 2 design: one way to fake the house, real Home Assistant (Issue #1127)

*Architect A, 2026-10-30. Design only; nothing is built, no source or test file was touched. Branch `feature/1125-test-automation` (origin/main v0.8.1). A throwaway spike (kept in the session scratchpad, not in the repo) was run against real HA 2026.10.4 to check the riskiest assumptions; its results are in section 0.2 and are marked VERIFIED.*

**Crux.** Run Climate Advisor inside real Home Assistant, against the one simulated thermostat we already own, with a clock we control, so that bugs which only exist at the seams (restart, settings save, reload, units, time) are caught on the pull request. Do it without building a second test runner next to the one Step 3 is about to define.

**Sources read.** `01-assessment-and-options.md`; `03-step3-design.md` (sections 9.1, 9.2, 9.4, 12, 14); `CLAUDE.md`; `docs/DEVELOPMENT.md`; `tests_ha/{conftest,test_unload_cleanup}.py`; `scripts/test.py`; `.github/workflows/validate.yml`; `tools/sim_harness/{run_production,fake_hass,fake_scheduler,build_coordinator,ha_stubs}.py`; `tools/simulate.py`; `dev_tools/ha_test_integrations/ca_dev_thermostat_sim/*` and its README; issues #1108, #1045, #1102, #1117, #945; git history of the four fixes; the 91 golden scenarios.

---

## 0. Read this first

### 0.1 The design in plain English

Today a bug like "I saved my settings and the fan came back on by itself" cannot be caught by our fast tests, because those tests run Climate Advisor against *pretend* Home Assistants that we wrote ourselves, and our pretend versions only misbehave in the ways we already thought of. Step 2 moves the important checks onto the real thing.

Five pieces, in the order they get built:

1. **A "house" test fixture.** One call builds a small real home inside real Home Assistant: the simulated thermostat (`ca_dev_thermostat_sim`, which already uses Climate Advisor's own heat-flow math), a weather entity, a door/window sensor, a fan, and a notification service. Time is a dial: tests turn it forward in small steps and everything Home Assistant schedules (including the thermostat's own physics) happens on time.
2. **Three past bugs become permanent tests** (#1108, #1045, #1102/#1117). Each is proven to fail on the code from just before the fix and to pass today.
3. **Saved copies of what the occupant reads** (briefing, Status, Next Action, Next Automation, fan cards), diffed on every change (#945).
4. **Run the 91 golden scenarios through real Home Assistant.** This is done through the *same* `Session` interface Step 3 defines (one seam, two adapters), not a second runner. Where real Home Assistant and the fake disagree, the disagreement is investigated and written in a ledger. A golden is never edited to make it agree.
5. **A written retirement path** for the home-made fakes: what moves first, what has to be true before something is deleted, and what stays forever.

### 0.2 Findings that change the plan's inputs

| # | Finding | Evidence | Consequence |
|---|---|---|---|
| F1 | **The "uncancelled startup timer" is not open on HEAD.** The brief asks for an issue to be filed for it. On this branch the 300 s timer's handle is kept in `_unsub_listeners` (`coordinator.py:1484`, comment "Issue #1102 (F1): keep the handle so unload cancels it") and released in the shutdown `finally` block (`coordinator.py:12417-12419`). Commit `b48fca0` (v0.8.0, #1102) did this. | `git show HEAD:custom_components/climate_advisor/coordinator.py` has the same line; `git log -S"keep the handle so unload cancels it"` -> `b48fca0`. `python scripts/test.py ha` -> 2 passed in 3.6 s, including `test_unload_leaves_nothing_running`, whose docstring says it was red on `1df0246`. A spike that sets up a zone and does **not** unload also passes: HA's stop event reaches CA's own listener ("async_shutdown ... already shut down - no-op"). | **VERIFIED.** The finding the brief describes was the #1102 F1 finding and is already fixed and covered. Section 9 explains what the house fixture does about the 300 s window instead, and recommends **not** filing a duplicate bug. This is flagged to the user (section 14, U3). |
| F2 | **`custom_components` cannot be merged with a second folder unless it is imported first.** The pytest plugin ships its own regular `custom_components` package; whichever import happens first pins `custom_components.__path__`. If HA's loader imports it first (inside a test) it gets the plugin's package and neither `climate_advisor` nor the thermostat sim is found ("Integration not found"). | Spike: first run failed with `Setup failed for 'ca_dev_thermostat_sim': Integration not found`, printed `custom_components.__path__` = plugin folder only. Second run, with the merge done at conftest import time, passed. | **VERIFIED.** `tests_ha/conftest.py` must (a) copy the sim into `<tmp>/custom_components/ca_dev_thermostat_sim` (a namespace portion, no `__init__.py`), (b) put repo root and `<tmp>` on `sys.path`, (c) `import custom_components` before any fixture runs. No file is added under `custom_components/` (hassfest and HACS see nothing new), which also drops the "new `custom_components/__init__.py`" item from the assessment's blast-radius table. |
| F3 | **The thermostat sim works under `freezer` + `async_fire_time_changed`.** In 20 simulated minutes at 30 s steps it left "off, 23.9 C" and Climate Advisor took over (mode cool, `hvac_action: cooling`). A "scripted" profile (`k_passive = k_active_* = 0`, indoor set with the entity's own `apply_manual_temperature_override`) keeps indoor fixed at the scripted value while `hvac_action` still cycles `cooling`/`idle`/`off` by the real compressor state machine. | Spike tests `test_sim_ticks_and_ca_alive`, `test_scripted_profile_and_speed`. | **VERIFIED.** No edit to `dev_tools/` is needed for either closed-loop tests or open-loop golden replay. Open-loop = same entity, physics zeroed. |
| F4 | **Units are a trap and a feature.** With HA left at its metric default and CA configured `fahrenheit`, CA's `climate.set_temperature 75` is read by HA as 75 C and rejected ("Provided temperature 167.0 is not valid. Accepted range is 44.6 to 95.0"); CA then retries forever. | Spike log, repeated `Setpoint validation FAILED ... reject streak=N`. | **VERIFIED.** The house builder takes `units="us"` (HA US customary + CA `temp_unit: fahrenheit`, the default) or `units="metric"` (HA metric + CA `temp_unit: celsius`) and sets both together; a mismatched pair is impossible to build. The Celsius half is exactly the "units" bug class the assessment lists (~25 recent bugs) and costs one parameter. |
| F5 | **A real weather entity matters.** With a plain `weather` state, CA logs `weather.get_forecasts service call failed ... falling back to forecast attribute` on every cycle: the real forecast service path is never exercised. That is the same blind spot as #1117 (the old harness could not run the forecast path). | Spike log. | The house ships a tiny real `WeatherEntity` with `async_forecast_daily/hourly`, so `weather.get_forecasts` works. |
| F6 | **`tools/sim_harness/run_production.py` installs the Home Assistant stubs at import time** (`install_ha_stubs()` at module level), so it cannot even be imported in the real-HA environment, where the plugin owns `homeassistant.*`. | `run_production.py` lines 118-120; `tests/conftest.py` stub layer. | The Session split Step 3 plans (its I2) must also make the stub install a property of the **FakeHass adapter only**, and move the pure parts (scenario model, classification builder, config merge, time parsing, result dataclass) into a stub-free module. Section 5 owns this as shared Increment 5. |
| F7 | **`freezer` freezes `time.perf_counter`.** My first timing printed `28800.00s` for 960 steps. | Spike output. | Do not time tests with `time.perf_counter` inside a frozen test. Budget numbers use pytest's own `--durations` (it captures the real timer at import). Measured: the whole scripted spike (setup + 8 simulated hours at 30 s steps, with a setpoint retry storm) was 4.4 s, about 3.7 ms per step. |
| F8 | **The 91 goldens are 405 virtual hours and 422 events**; 31 already use `use_coordinator`; 29 set `skip_startup_coalesce`; 9 set `initial_thermostat_mode`; 4 inject a `thermal_model`/`hourly_forecast`; none use `thermostat_quirk` or Celsius. | Script over `tools/simulations/golden`. | At ~4 ms per 30 s step, serial replay is about 3 minutes plus about 1 minute of setup; with xdist (already in the lock) about 1-1.5 minutes. Section 11 sets the budget. |
| F9 | **Step 3's Session split is not built.** `git status` shows `run_production.py` unmodified and no `tools/promise_probe/`. | git status at start of session. | Whoever starts first builds it once, in a standalone behavior-preserving PR; the other consumes it. Recommendation in section 5. |

### 0.3 Mirror item (called out explicitly, per the symmetry rule)

The setup/unload leak (#1102) has an exact mirror: the **entry path** (what happens on *start* that survives a *stop*). The house fixture's teardown always unloads in reverse order and the autouse `verify_cleanup` is the net, but the existing tests only check the unload side. Increment 2 adds the start-side counterpart: a test that sets up and tears down **twice in a row** on the same `hass` (the shape of a settings save) and asserts the second instance is the only one that ever commands. That is the T1 scenario from #1102 turned into a general contract, not another one-off.

---

## 1. Constraints this design obeys

- **One seam.** `tools/sim_harness/run_production.py` ends up with the `Session` protocol from Step 3 section 9.1 (`feed`, `advance_to`, `now`, `thermostat`, `finish`) and two adapters. Step 2 adds only the real-HA adapter. No second protocol, no second runner.
- **Goldens are locked.** `tools/simulations/golden/` is never edited. A difference between FakeHass and real HA is investigated; it is never fixed by changing a golden or its assertions.
- **Outcomes layer is untouched.** `outcomes.py` (`production_outcome_at`, `production_temp_at`, `check_assertion`, event-to-outcome mapping) is not edited. The real-HA adapter must produce the same `ProductionRunResult` shape so the existing layer judges both worlds with identical rules. If a divergence tempts anyone to change that layer, rule 8 applies and needs a written justification of why the old semantics were wrong.
- **No mirror tests.** Real-HA tests drive the real entry points (config entry setup, services, state writes, time) and never re-implement production logic. A hand-written persisted-state dict is a mirror; a state file written by a first real instance and read by a second is not.
- **Async-mock rules, `-W error`.** Real-HA tests need no `AsyncMock` at all, which is the point; the `ha` tier keeps `asyncio_mode=auto` and HA's autouse `verify_cleanup`.
- **HA boundary.** Nothing is added to `custom_components/`. The sim and weather entities live in `tests_ha/` and `dev_tools/`; deploy and HACS never see them. Tier B (Docker) stays frozen and untouched; the house reads the Tier B config seed as a read-only data file (see section 4, risk R4).
- **Version bump.** Step 2 adds no production behavior, so no `custom_components/` edit is expected and no version bump. If an increment does touch production (for example a fix a divergence uncovers), that PR carries the normal bump, `fix_history` entry and CHANGELOG line.

---

## 2. Increment overview (dependency order)

Foundations first. Sizes are not the ordering rule.

| # | Increment | Brief item | Depends on | Occupant gets |
|---|---|---|---|---|
| 1 | Loader, markers, revert-proof tool, runner args | (a) groundwork, (f) | none | Nothing directly; makes every later claim provable |
| 2 | House fixture and lifecycle contract | (a), (g) | 1 | A reload or restart can no longer leave a ghost copy of the old zone running |
| 3 | Three escaped-bug tests | (c) | 1, 2 | Fan stays off after a settings save; "automation off" means hands off; a manual fan survives a restart |
| 4 | Snapshots of what the occupant reads | (d) | 2 | A wording change in the briefing or cards can never ship unnoticed |
| 5 | Session split (shared with Step 3 I2) | (b) | none, but see section 5 | Nothing directly; the one seam |
| 6 | Real-HA adapter, triage tool, report format | (b) | 2, 5 | Goldens that pass on the fake but would misbehave at home are exposed |
| 7 | Goldens through real HA: first run, triage, ledger | (b) | 6 | Same as 6, applied to all 91 |
| 8 | Retirement waves | (e) | 3, 7 | Faster, less duplicated test suite; fewer false "green" runs |
| 9 | CI wiring, budget gate, docs convergence | (f) | 1-8 (CI args land in 1, gate lands here) | A PR that breaks the house fails in minutes, with a readable reason |

Increments 3 and 4 do not depend on 5-7, so they can be built and reviewed while the Session split is being agreed with Step 3.

---

## 3. Increment 1: Loader, markers, revert-proof tool, runner args

**Occupant.** None directly. Without this, no later test can honestly claim "this fails on the buggy code".

**What.**

1. `tests_ha/conftest.py` grows the loader from finding F2. Load-bearing shape (proven in the spike):
   - repo root and a session temp dir on `sys.path`; the temp dir holds `custom_components/ca_dev_thermostat_sim/` copied from `dev_tools/ha_test_integrations/ca_dev_thermostat_sim/` (ignore `__pycache__` and `test_*.py`);
   - `importlib.import_module("custom_components")` at conftest import time, with a comment explaining why this line must stay above anything else;
   - cleanup via `atexit` (xdist workers each build their own temp dir, which is fine);
   - an optional env var `CA_COMPONENT_ROOT` (default: repo root) that replaces the repo path on `sys.path`. This is the overlay hook the revert-proof tool uses to point the tests at an old copy of `custom_components/` without a git checkout.
2. `tests_ha/pytest.ini`: register markers `house` (uses the full fixture), `goldens` (real-HA golden replay, deselectable), `snapshot`, and `slow` (anything over 5 s); `--strict-markers`; `timeout = 60` (pytest-timeout is already in the lock).
3. `scripts/revert_proof.py` (new, standard library only, about 100 lines): `python scripts/revert_proof.py <commit> <pytest-node-id>...`. It runs `git archive <commit> custom_components | tar -x -C <scratch>`, sets `CA_COMPONENT_ROOT=<scratch>`, runs the named tests through the same path `scripts/test.py ha` uses, and **requires them to fail**; then runs them on the working tree and **requires them to pass**. Exit 0 only if both hold. It prints the failing assertion so the output can be pasted into a PR as evidence. It never checks out, resets or stashes anything in the repo (this branch has uncommitted work and the shared stash rule applies).
4. `scripts/test.py`: pass-through for `-n`/`--workers` via `CA_HA_WORKERS` (default `0` locally, `auto` in CI). Today `HA_ARGS` is a fixed list; the change is additive and `python scripts/test.py ha` behaves as before when the variable is unset. Also set `PYTHONPYCACHEPREFIX` inside WSL so the ext4 home holds the bytecode instead of the slow `/mnt/c` mount (measured: the whole `ha` tier already costs about 11 s wall for 3.6 s of test time, mostly WSL and uv start-up).
5. `pyproject.toml`: add `syrupy>=6` to the `test-ha` group (it is already installed transitively by the plugin; naming it makes the dependency explicit and survives a plugin upgrade that drops it). `uv lock` should only change group metadata; the done-criteria checks this.

**Files.** `tests_ha/conftest.py`, `tests_ha/pytest.ini`, `scripts/revert_proof.py` (new), `scripts/test.py`, `pyproject.toml`, `uv.lock`, `docs/DEVELOPMENT.md` (two paragraphs).

**Done when.**
- `python scripts/test.py ha` still shows the two existing tests passing, unchanged.
- A throwaway test that needs `ca_dev_thermostat_sim` finds it.
- `python scripts/revert_proof.py 1df0246 tests_ha/test_unload_cleanup.py::test_unload_leaves_nothing_running` exits 0 (fails on pre-#1102 code, passes on HEAD). This is the tool's own positive control and uses a case the docstring already claims.
- `uv lock --check` clean, `ruff check` and `ruff format --check` clean.

**Proof.** The positive control above, plus a negative control: pointing the tool at HEAD for a test that should pass on both makes it exit non-zero with "did not fail on the pre-fix tree".

**Risks.**
- R1: Python 3.14 and HA 2026.10.4 against a v0.7.x tree. `1df0246` ships `VERSION = 20` config flow like HEAD (checked), so the config entry migrates identically; older commits (`2507a19^` is v0.7.91) also report `VERSION = 20`. If a revert-proof target turns out not to import under HA 2026.10.4, the tool reports "pre-fix tree failed to import" separately from "test failed", and the increment that hit it uses the reverse-patch fallback (apply `git diff <fix>^ <fix> -- custom_components | git apply -R` to the scratch copy).
- R2: Copying the sim means a fix in `dev_tools/` is seen only on the next run (that is correct and cheap).

---

## 4. Increment 2: House fixture and lifecycle contract

**Occupant.** After saving settings or restarting Home Assistant, exactly one copy of each zone is alive and it is the new one. The thermostat, fan and notifications are driven by that copy only.

### 4.1 What the fixture builds

`tests_ha/house/` (new package, imported by `tests_ha/conftest.py`):

| Piece | What it is | Why this and not something else |
|---|---|---|
| `house_config(**overrides)` | Starts from the Tier B seed entry (`tools/integration_harness/config-seed/.storage/core.config_entries`, the file `test_unload_cleanup.py` already uses), overlays the few keys a test needs, and fills in the entity ids of the pieces below | One known-good config, read as data. Migration from the seed's version 15 to 20 runs on every setup, which is a free migration test. Risk R4 covers the seed being tied to the frozen Tier B. |
| `ThermostatSim` | A real config entry of `ca_dev_thermostat_sim`, created with `tick_seconds=30` and two **profiles**: `closed_loop` (default k values, physics runs) and `scripted` (all three k values zero; indoor is set by the test) | One thermostat fake, as the plan decided. Both profiles verified (F3). The sim's own compressor state machine supplies `hvac_action`, which the old FakeHass never did. |
| `HouseWeather` | A small real `WeatherEntity` subclass with `native_temperature`, daily/hourly forecast, and `weather.get_forecasts` working | Avoids the attribute-fallback blind spot (F5). The test sets outdoor temperature and forecast with one helper. |
| Contacts | `binary_sensor.house_window` etc. as plain states (`on` = open), created from a list | Plain states are what real contact sensors are to CA; the real `_async_door_window_changed` listener runs. |
| Fans | Physical-state entity (`input_boolean`-style, "detect power") and a command entity (switch or fan) with real `turn_on`/`turn_off` services that update the state after a configurable delay | Covers whole-house-fan (WHF) config. A command-only variant (no feedback) covers `fan_state_feedback` off. Thermostat blower fan is the sim's own `fan_mode`. |
| Notify | `async_mock_service(hass, "notify", ...)` on the configured service | Notifications are an occupant surface; the fixture exposes `house.notifications`. |
| Units | `units="us"` or `"metric"` sets `hass.config.units` and CA `temp_unit` together | Finding F4. |
| Time zone | Defaults to UTC for golden parity; a test can pass a real zone | Goldens carry UTC timestamps. Zone-sensitive tests choose their own. |
| Clock | `pytest-freezer` (`freezer`) starting at a chosen instant before **any** entry is set up | Timers armed during setup are relative to the frozen start. |

### 4.2 What the test sees

```python
house = await build_house(hass, freezer, tmp_path, units="us", profile="closed_loop",
                          fan="whf", contacts=["window"], startup="settled")
await house.advance(minutes=30)               # steps the clock; see below
house.thermostat.mode / .setpoint_f / .indoor_f / .action / .fan_mode
house.set_indoor(78.0)  /  house.set_outdoor(60.0)  /  await house.open("window")
await house.occupant_sets_thermostat(mode="heat", setpoint_f=70)   # service call with a marked Context
house.calls  # every climate/fan/switch/notify service call, in order, with origin ("ca" | "occupant")
await house.reload_zone()      # the settings-save shape: update entry, HA reloads it
await house.restart()          # unload + setup again on the same config dir (see R3 for a true new hass)
await house.close()            # unload CA, then sim, in reverse order; always runs
```

`house.calls` comes from a listener on `EVENT_CALL_SERVICE` (the same bus event CA's own listener uses), so it sees every command regardless of what CA believes it sent. That is the ground truth Step 3 promise P3 wants.

### 4.3 How time moves

`house.advance(...)` steps the frozen clock in small slices and calls `async_fire_time_changed` plus `async_block_till_done` after each. Step size is the smaller of 30 s (the sim tick) and the time to the next scheduled timer, floored at 1 s. Reading the next due timer needs `hass.loop._scheduled`, a private attribute; the spike used fixed 30 s slices and worked. The increment's first task is a 30-minute experiment comparing both: if fixed slices reproduce timer order well enough for the three bug tests, ship fixed slices and keep the next-timer variant for Increment 6, where second-level precision matters for golden event times.

### 4.4 Startup and the 300 s coalescing window (brief item g)

Climate Advisor deliberately suppresses override detection for the first 300 s after setup ("startup coalescing", #321). That is behavior the house must be able to show and also be able to skip:

- `startup="cold"`: setup only. The window is open; tests about startup use this.
- `startup="settled"` (default): after setup, advance 301 s so the window has closed and the one-shot coalesce refresh has run. This mirrors the goldens' `skip_startup_coalesce: true` (29 of 91 use it) and keeps ordinary tests from vacuously early-returning before any override logic.

The timer itself is already cancelled on unload (F1). So the fixture's job is only to make sure the contract stays true:

- `house.close()` always unloads both entries in reverse order, in a `finally`.
- HA's autouse `verify_cleanup` is the safety net. The fixture adds no waivers and no `expected_lingering_timers`.
- `test_house_teardown_leaves_nothing_running` (new): build, advance 5 minutes, close. This is the 'start side' of the existing unload test, and it uses the full house instead of one thermostat state.

**GitHub issue.** The brief says an issue is filed for the uncancelled timer when Step 2 starts. Findings F1 shows the fix already shipped. The recommendation is **not** to file a bug. If the user still wants an issue, it should be a test-coverage issue ("house fixture asserts the start-up timer contract"), which this increment closes. If the house's first real run does show a lingering timer or task, that is a new, different finding and gets its own issue at that moment, with the `verify_cleanup` text in the body.

### 4.5 Lifecycle contract tests (the symmetry fix)

In `tests_ha/test_house_lifecycle.py`:

1. `test_house_teardown_leaves_nothing_running` (above).
2. `test_two_setups_in_a_row_only_the_new_one_commands`: build, close, build again on the same `hass` and config dir at +172 s (inside the startup window, as the #1102 field incident), advance 10 minutes with a cool house and the fan wanted; assert every recorded command is from the second instance's `Context`/run id. This is #1102's T1 as a contract.
3. `test_reload_zone_keeps_persisted_state_and_drops_timers`: `house.reload_zone()`; assert the second instance restored `automation_enabled` and grace end time from disk and that no timer from the first instance fires.

**Files.** `tests_ha/house/{__init__,builder,weather,fans,calls}.py` (new), `tests_ha/conftest.py` (fixture wiring), `tests_ha/test_house_lifecycle.py` (new), `tests_ha/test_house_smoke.py` (new: the spike's two tests, hardened).

**Done when.** `python scripts/test.py ha` passes with the new tests in under 20 s wall on this PC; running the suite twice in one process (`-p no:randomly` not needed) shows no cross-test bleed; `units="metric"` variant of the smoke test passes (a Celsius zone reaches `cooling` without a rejected command; zero `Setpoint validation FAILED` log lines).

**Proof.** The fixture is proven by the bugs it must catch (Increment 3) and by two deliberate sabotages run once and not committed: (a) comment out the `_unsub_listeners` append for the startup timer and confirm `test_house_teardown_leaves_nothing_running` fails with HA's "Lingering timer" text; (b) leave a settings listener running and confirm `test_two_setups_in_a_row...` fails. Output pasted into the PR.

**Risks.**
- R3: `house.restart()` is unload plus setup on the same `hass`, not a new process. It exercises persistence and clean-slate logic but not HA's own boot ordering. A true restart would build a second `hass` over the same config directory with `async_test_home_assistant()`; the spike did not try it. Decision: build the cheap restart now; try the true two-`hass` restart as a stretch inside Increment 3 (#1045), and only keep it if it is stable.
- R4: the seed lives under the frozen Tier B folder. If Tier B is ever deleted, copy the seed to `tests_ha/house/config_seed.json` first. Recorded in `docs/sim-harness-spec.md` so the dependency is visible.
- R5: the sim's tick uses `dt_util.utcnow()` deltas, so a single huge clock jump gives the physics one huge step. `house.advance` never jumps more than one slice, and the fixture raises if a test asks for a step larger than the sim tick while in `closed_loop` without `allow_large_step=True`.

---

## 5. Increment 5: The Session split (shared with Step 3's I2)

Listed here out of numeric order because it is the one piece both steps need; Increment 6 cannot start without it, and nothing in 1-4 depends on it.

**Occupant.** None directly.

**Ownership.** Step 3's I2 plans exactly this refactor (a behavior-preserving split of `run_production_scenario` into a `ProductionSession` plus a thin loop, with `PromiseProbe` attached to it). Step 2 needs the same split plus one more thing Step 3 did not need: finding F6. Building it twice would be the DRY failure this whole epic exists to remove.

**Recommendation.** Do it once, in a standalone PR titled "Session seam, no behavior change", that either step can land first:

| Part | Content |
|---|---|
| New `tools/sim_harness/session.py` (no HA imports) | `Session` and `ThermostatView` protocols exactly as in Step 3 section 9.1, plus `RunResult` (alias of `ProductionRunResult`) and a `make_session(scenario, engine="fakehass" \| "realha", **opts)` factory |
| New `tools/sim_harness/scenario_model.py` (no HA imports) | Pure helpers lifted out of `run_production.py`: config merge with `_DEFAULT_CONFIG`, event sort and start time, `_parse_event_time`, `_build_classification_from_event`, `_snapshot_engine_state`, and the `ProductionRunResult` dataclass |
| `tools/sim_harness/run_production.py` | Keeps its public name and signature (`run_production_scenario`) and becomes the FakeHass adapter (`FakeHassSession`). `install_ha_stubs()` stays here and nowhere else. The dispatch table stays as it is |
| `tools/simulate.py` | Unchanged call site |

**Done when.** Every golden gives identical `event_log`, `action_log` and `engine_state` before and after (compare by running `tools/simulate.py` over all 91 and diffing a JSON dump of the three fields); `tools/simulate.py --check-integrity` clean; `tests/test_production_harness.py` unchanged and green; the real-HA environment can `import tools.sim_harness.session` and `tools.sim_harness.scenario_model` without any stub being installed (a two-line test in `tests_ha/` proves F6 is fixed).

**Proof.** The before/after dump is the revert proof: it is run on the parent commit and on the branch, and the diff must be empty.

**Risk.** R6: the Step 3 doc says the Coordinator decides which lane lands the Session split first. If Step 3's lane lands it first, Increment 5 shrinks to "add the stub-free `scenario_model.py` and `make_session`" (F6 only).

---

## 6. Increment 3: Three escaped-bug tests

All three live in `tests_ha/test_escaped_bugs.py` and use the house. Each test states the occupant story in its docstring, records the pre-fix commit it was proven against, and keeps a **positive control** (the same flow with the bug's trigger absent, so the instrument demonstrably sees the thing the bug hides).

### 6.1 #1108: "automation off" means hands off

**Occupant.** The person turned Climate Advisor's automation switch off to run the fan themselves. Before the fix, after a settings save CA still sent one `climate.set_fan_mode auto` to release a fan it believed it owned.

**Pre-fix commit.** `b686a05^` (fix `b686a05`, v0.8.0 line). Only `automation.py` changed in production.

**Flow (all real).**
1. House with `fan="hvac_fan"` (thermostat blower), `startup="settled"`.
2. Drive CA to run the blower on its own (real min-runtime cycle or a nat-vent-free "fan wanted" condition), so `fan_active` is true and the sim reports `fan_mode: on`. The exact trigger is chosen in the increment from the lowest-effort real path; a fallback is to let CA persist state, then edit the state file through CA's own store API rather than hand-writing JSON.
3. Switch automation off through the real switch entity (`switch.turn_off`), then save settings with fan control disabled (`async_update_entry`) and reload, exactly as a settings save does.
4. Advance past startup.
5. Assert `house.calls` contains **no** `climate.set_fan_mode` from CA after step 3, and the sim's `fan_mode` is still `on`.

**Positive control.** Same flow with automation left on: CA releases the fan (`fan_mode` becomes `auto`) and emits `fan_orphan_released`. This is the "same, unchanged" row in the issue's table.

**Revert proof.** `python scripts/revert_proof.py b686a05^ tests_ha/test_escaped_bugs.py::test_observe_only_does_not_release_blower`. Expected failure text: the unexpected `set_fan_mode` call.

**Why real HA adds something.** The unit test for #1108 builds an engine with a `MagicMock` hass and calls `_release_orphaned_hvac_fan` directly. It cannot show that the real save-and-reload path reaches that function with the pending flag set; the real-HA test can.

**Risks.** The "CA turns the blower on by itself" step may need a particular config. If it proves fragile, step 2 falls back to a first real instance that is made to own the fan by calling the engine's public `start_min_fan_runtime_cycles()` (the same entry point the golden `activate_fan_min_runtime` uses), which is still a real engine in a real HA.

### 6.2 #1045: a manually started fan survives a restart

**Occupant.** The person turns the whole-house fan on at the wall or with the remote. Home Assistant restarts a few minutes later (a deploy, an update). Climate Advisor used to switch the fan back off at start-up, minutes after the person turned it on.

**Pre-fix commit.** `2507a19^` (v0.7.91). Fix `2507a19` touched `automation.py` and `coordinator.py`.

**Flow.**
1. House with `fan="whf"` (command entity plus physical-state entity), a window open so the fan is not otherwise wanted or forbidden, `startup="settled"`.
2. `house.occupant_sets_fan(on=True)` flips the physical-state entity only (no CA command, no RF-timer token), as at the wall.
3. Advance 90 s so CA's real listener records `fan_manual_override` and starts its 3 h manual grace and **persists it**.
4. `await house.restart()`; advance past the 300 s start-up reconcile.
5. Assert: no CA `turn_off` to the fan command entity; physical state still on; an event `fan_manual_override` rearmed (restored) is present; the Fan (WHF) card does not say "inactive".

**Positive control.** Same flow but the grace has already expired when the restart happens (advance 3 h 5 min before restarting): the reconcile **does** turn the fan off. This pins the "drop on expiry" guard from the issue's fix.

**Revert proof.** `scripts/revert_proof.py 2507a19^ ...`. Expected: a `turn_off` to the fan at about +5 min after restart.

**Why real HA adds something.** The unit test `tests/test_fan_manual_override_restart.py` builds the persisted snapshot by hand (`_persisted()` returns a literal dict). If the key CA *saves* ever drifts from the key CA *restores*, that test stays green. The real-HA flow writes with the first instance and reads with the second, so the round trip is the thing under test.

**Risk.** R3 (restart is unload plus setup, not a new process) matters most here. If the stretch goal of a true two-`hass` restart works, this test uses it; if not, it states plainly in its docstring that persistence is covered and HA boot ordering is not.

### 6.3 #1102 / #1117: the ghost copy and the unreachable fan path

**Occupant.** After saving settings the old zone's 300 s timer woke a dead copy of Climate Advisor, which turned the whole-house fan back on after the person had turned it off (#1102). The fast tests could prove the dead copy was still alive but could not watch it send the fan command, because the test harness's forecast step crashed on a time-zone shortcut (#1117).

**Pre-fix commit.** `1df0246` (the merge before the #1102 work). Production fix `b48fca0`.

**Flow.**
1. House with `fan="whf"`, **a real weather entity with a full forecast** (this is the #1117 half: the whole regular update cycle, including fan decisions, actually runs), a window open, outdoor cooler than indoor so CA wants the fan, `startup="cold"`.
2. Advance to +172 s. Record `house.calls` length and the old coordinator object. `await house.reload_zone()`.
3. Advance to +601 s.
4. Assert: the old coordinator ran **no** `_async_update_data` after the reload (spy on the instance as the unit T1 does); `house.calls` after the reload contains commands only from the new instance; no fan command arrives from the old one.

**Positive control (the #1117 half).** With the same house and no reload, advance the same 10 minutes and assert that CA **does** command the fan on (a normal update cycle reaches the fan decision). This is the assertion the old harness could not make. A second positive control shows the occupant's manual `turn_off` is respected by the *new* instance for the rest of the run.

**Revert proof.** `scripts/revert_proof.py 1df0246 ...`. Expected: the dead coordinator's update cycle at about +210/+300/+450 s and at least one fan command attributed to it. Because the pre-fix tree also leaves a lingering timer, HA's own `verify_cleanup` may fail the test first; the tool reports both failures, and the test's own assertion fires before teardown.

**Files for 6.1-6.3.** `tests_ha/test_escaped_bugs.py` (new), small helpers added to `tests_ha/house/` (fan variants), `docs/DEVELOPMENT.md` (table of bug, test, pre-fix commit, command).

**Done when.** For each of the three: revert proof exits 0, positive control passes, whole `ha` tier under budget (section 11). The PR description contains the revert-proof output for all three.

**Symmetry check inside this increment.** For each, the opposite path is exercised: #1108 has the automation-on twin; #1045 has the grace-expired twin; #1102 has the no-reload twin. The #1108 mirror (`_call_climate` and `_call_fan_service_with_context` also got gates) is covered for the thermostat command path by a second test in the same file: with automation off and the house cool, the thermostat receives no `set_temperature`/`set_hvac_mode` at all across 30 minutes.

---

## 7. Increment 4: Snapshots of what the occupant reads (#945)

**Occupant.** The daily briefing and the Status tab are the product. Today a wording regression can ship with CI green because nothing checks the words. After this increment any change to what the person reads shows up as a diff in the pull request, and a human decides whether it is right.

**What.** `syrupy` snapshots of the **real** surfaces, taken from the house, not from calling a formatter directly:

| Surface | Source in the house | Notes |
|---|---|---|
| Daily briefing (TL;DR and full) | `sensor.house_daily_briefing` attributes `tldr` and `full_briefing`, produced by firing the briefing time with the clock | The spike showed this sensor exists on setup (`full_briefing`, `tldr`) |
| Status card | `sensor.house_status` | |
| Next User Action | `sensor.house_your_next_action` | |
| Next Automation action and time | `sensor.house_next_automation_action`, `..._time` | |
| Fan (WHF) and Fan (HVAC) cards | `sensor.house_fan_status` plus the WHF/HVAC status keys | Only in the fan reference days |
| Notifications | `house.notifications` | Push/email text for the briefing |

**Reference days** (about 8, each a parametrized test with a fixed frozen time, weather, config and occupancy): hot, warm, mild, cool, cold; mild with a window open (door/window pause shown on Status); away; and one Celsius zone (`units="metric"`) on the warm day so rounding and unit strings are visible. The status cards are snapshotted at three points in the warm day (morning, mid-day, evening) to cover the "Next User Action / Next Automation" wording that has regressed before (#1087-#1096 class).

**Rules.**
- Snapshot files are `tests_ha/__snapshots__/*.ambr`, committed. CI runs with `--snapshot-warn-unused` off (unused snapshots fail) and **never** with `--snapshot-update`.
- Updating is a deliberate human act: `python scripts/test.py ha --snapshot-update tests_ha/test_snapshots.py`, then review the `.ambr` diff as prose. The PR template line (added to `docs/DEVELOPMENT.md`): "Changed a snapshot? Paste the before/after text and say why the occupant is better off."
- Non-deterministic content (entry ids, absolute timestamps from a real clock) is excluded by construction: frozen clock, fixed titles, and syrupy matchers for object ids. If a field cannot be made stable it is removed from the snapshot and covered by an ordinary assertion.
- Status Card Ontology is enforced next to the snapshot: a small test asserts the Next Automation snapshot text never contains the banned words already enforced by `tests/test_status_card_ontology.py` ("window", "waiting", "paused", "grace"), because the snapshot would otherwise happily lock in a violation.

**Files.** `tests_ha/test_snapshots.py` (new), `tests_ha/__snapshots__/` (new), `tests_ha/house/surfaces.py` (reader helpers), `docs/DEVELOPMENT.md`, `docs/00-PROJECT-INSTRUCTIONS.md` Anchors row.

**Done when.** All reference days snapshot; two consecutive runs on this PC and one on CI give identical output; the Celsius day differs from the Fahrenheit warm day only where units and rounding differ.

**Proof (revert-proof for a text test).** Two deliberate edits run once and not committed: (a) swap the order of two sentences in `_warm_day_plan()` and confirm the warm-day briefing snapshot fails with a readable diff; (b) restore a pre-#940 wording (a reopen-windows time before its close-windows time, the actual #940 bug that motivated #945) and confirm it fails. This turns #945's own motivating example into the revert proof.

**Risks.**
- R7: brittleness. A snapshot test that fails on every harmless edit is ignored within a month. Mitigation: snapshot the occupant-facing strings only, keep the reference-day count at about 8, and keep each `.ambr` entry a single string per surface rather than a dump of every attribute.
- R8: time-of-day text depends on the HA time zone and locale. Fixed in the fixture (UTC unless the test asks).

---

## 8. Increments 6 and 7: Goldens through real Home Assistant

### 8.1 What "through real HA" means

Same scenario file, same assertions, same outcome layer. Only the thing underneath changes:

| | FakeHass adapter (exists) | Real-HA adapter (new) |
|---|---|---|
| Time | `FakeScheduler` heap, virtual | `freezer` + `async_fire_time_changed`, stepped to the scenario timestamp (next-timer stepping, 1 s floor, 60 s cap) |
| Thermostat | A dict-backed state with obedient service feedback, no `hvac_action` | `ca_dev_thermostat_sim` in the `scripted` profile: indoor from the scenario, `hvac_action` from the real compressor model, services validated by HA |
| Engine entry | Calls engine methods directly (engine-only) or builds a coordinator with a stub HA (coordinator mode) | Always the real config entry and real coordinator (the "engine-only" distinction disappears) |
| Events fired by the listener layer | Re-created by the runner ("mirrors `_async_thermostat_changed`'s dispatch") | Produced by the real listeners from real state writes |
| Restart | `restore_state(get_serializable_state())` | `house.restart()` (real unload, real persistence, real setup) |
| Services | Recorded in `action_log` | Recorded from `EVENT_CALL_SERVICE` into the same `action_log` shape (`domain, service, data, ts, context`) |
| Event log | `engine._emit_event_callback` tee | Same tee, installed the same way `build_headless_coordinator` does, so deduplication in `coordinator._emit_event` cannot hide an event from the golden's assertions |

### 8.2 Event mapping (how each scenario event type is driven)

Every one of the 25 event types in the goldens maps to exactly one *dispatch mode*; the adapter reports how many events used each mode, so it is visible how real the run was.

| Mode | Meaning | Event types (golden counts in brackets) |
|---|---|---|
| **state-write** | The adapter writes a state or entity value; real listeners react | `temp_update` (146: indoor via the sim's override method, outdoor via the weather entity), `sensor_open/close/unavailable` (63/13/1), `external_fan_state_change` (16), `external_entity_state_change` (3) |
| **service-as-occupant** | The adapter calls a real service with a marked occupant `Context` | `thermostat_state_changed` (12): sets mode/setpoint through `climate.set_hvac_mode`/`set_temperature` on the sim (so the sim's own state is updated, unlike a bare state write); `occupancy_*` via the real toggle entities when configured |
| **time-travel** | The adapter only moves the clock; the production schedule fires | `bedtime` (12), `wakeup` (4), `pre_cool` (3), `grace_end` (1), `startup_coalesce` (3), `coordinator_refresh` (2), `fan_backstop_tick` (1) when the scenario's configured times match the event time |
| **real-lifecycle** | Real HA lifecycle call | `simulate_restart` (3): `house.restart()` |
| **direct-callback** | The adapter calls the same production method FakeHass calls, inside the HA loop. Declared fallback, reported separately | `classification` (105; hand-built `DayClassification`, see below), `economizer_check` (5), `nat_vent_temperature_check` (7), `reconcile_fan_on_startup` (2), `cancel_override` (2), `occupancy_change_with_override` (1), any time-travel event whose configured time does not match the event time |

**Classification.** The goldens build `DayClassification` by hand (`object.__new__`, skipping `__post_init__`). That is a known risk in the Golden Policy (a hand-set `hvac_mode` can disagree with what `classifier.py` would derive). The adapter keeps `classification` as direct-callback by default for parity, and offers `classification_mode="derive"` (weather forecast through the real `classify_day()`) for new tests and for the Step 3 generator. Running every golden once in `derive` mode and listing the classifications that differ from the hand-set ones is a free audit of the golden premises; it is reported, never acted on by editing goldens.

**Injected engine inputs.** `thermal_model` and `hourly_forecast` (4 goldens) are set on the live engine attributes, as the FakeHass adapter does; they stay direct-callback and are reported.

### 8.3 Divergence triage

A divergence is any scenario whose pass/fail, decisions, service calls or final engine state differ between the two adapters. It is a finding, not a failure to hide.

**Step 1: detect.** `tools/sim_harness/divergence.py` (new, stub-free) runs both adapters on a scenario and writes a report. Because the two adapters run in different environments (stub vs real HA), the comparison is done through serialized results: each env writes `<scenario>.<adapter>.json` (decisions, service calls, final state, dispatch-mode counts, assertion verdicts from the **unchanged** outcome layer), and the tool diffs the pair. `scripts/test.py` gets `compare` as a mode that runs `unit`'s FakeHass dump then `ha`'s real-HA dump then the diff. (Windows: the real-HA half runs in WSL as today.)

**Step 2: report format** (`tools/simulations/results/realha/<scenario>.md` and a roll-up `SUMMARY.md`; machine-readable twin is JSON):

```
Scenario: 733_restart_reconcile_clobbers_fresh_nat_vent   golden   coordinator-mode
Verdict:  FakeHass PASS  |  Real HA FAIL (assertion a3)           Dispatch: 9 state-write, 2 time-travel, 1 real-lifecycle, 4 direct-callback
First difference: event[5] simulate_restart @ 2026-05-01T14:35:00Z
  decisions:     fake   [nat_vent_exit_blocked]
                 real   [nat_vent_session_adopted, nat_vent_exit]
  service calls: fake   (none)
                 real   fan.turn_off  (origin=ca, +00:04:58 after restart)
  state:         fan_active  fake False  |  real True
Class: (unclassified)      Re-ran 3x: stable
Occupant view: the fan the person turned on stops 5 minutes after a restart on real HA but not on the fake.
Evidence: log excerpt (30 lines), state timeline, journal window if Step 3 probe attached
```

**Step 3: classify, with evidence, one class per divergence.**

| Class | Meaning | Action |
|---|---|---|
| E, environment | Our adapter or fixture differs (time zone, unit, tick size, dispatch order) | Fix the adapter or fixture. Allowed. Never touches a golden. |
| F, fake artifact | FakeHass is more obedient or less strict than real HA (no `hvac_action`, instant feedback, no HA service validation) | Record. The golden stays; the fake is the thing that was wrong. Decide later whether to improve the fake or retire the scenario from the fake adapter (Increment 8). |
| T, trigger | The scenario poked the engine directly where production is triggered by a listener or schedule, so the two worlds exercise different code | Record; if the real path is better, mark the scenario's event with a dispatch note in the ledger (not in the golden). |
| P, production bug | Real HA is right; production does something a person would notice | Open an issue (occupant-first sentence first), write a failing real-HA test, run the **symmetry check** on the opposite path, fix under the normal process. The golden then passes on both. |
| G, golden premise | The scenario encodes a state production can never produce (for example a hand-set `hvac_mode` that the real classifier would not derive) | Per the Golden Policy: show the scenario in human-readable form, ask the user, and only on explicit instruction follow the sign procedure. Never auto-edit. Verify the premise before touching production (existing project feedback). |

**Step 4: ledger.** `tools/simulations/realha_divergences.json`: `{scenario, class, first_difference, issue_or_justification, reviewer, date}`. Same ratchet semantics as Step 3's waiver and surface-divergence ledgers: a listed divergence is tolerated, an unlisted one fails the test, a listed one that no longer diverges fails ("remove the stale entry"). **Coordination:** Steps 2 and 3 now have three ledgers with identical rules. Implement the load/validate/stale-check once as `tools/sim_harness/ledger.py` and use it for all three; this is the one cross-step DRY item to settle when the first of them is built.

**Outcome-layer rule.** The adapter feeds the existing `production_outcome_at`/`check_assertion` unchanged. If a divergence appears because the real event stream orders or shapes events differently, the first suspect is the adapter's event tee, not `outcomes.py`. Only if someone concludes the old mapping was *wrong* does rule 8 apply, with its written justification and the question "could this let a real failure pass silently?".

### 8.4 Increment 6: the adapter and the tools (no goldens yet beyond a handful)

**Occupant.** None directly.

**Files.** NEW `tests_ha/realha_session.py` (the `RealHaSession` implementing `Session`, built on the house), NEW `tools/sim_harness/divergence.py`, NEW `tools/sim_harness/ledger.py` (or the Step 3 one), EDIT `scripts/test.py` (`compare` mode), NEW `tests_ha/test_realha_adapter_smoke.py`, docs.

**Done when.**
- `RealHaSession` passes a protocol conformance test shared with `FakeHassSession` (same `Session` calls, same `RunResult` fields).
- Five hand-picked goldens (one per dispatch mode, including a `simulate_restart` one and a `use_coordinator` one) run on both adapters and the report is produced end to end.
- The report tool's own negative control: feed it a deliberately perturbed copy of a result (one service call dropped) and confirm it names that call as the first difference.

**Risks.**
- R9: `hass.loop._scheduled` (private) for next-timer stepping; fallback is fixed 30 s slices with a documented timing granularity and reporting of any scenario whose event times are not multiples of the slice.
- R10: Session split not landed (section 5 / R6).
- R11: direct-callback events running inside the HA loop need `await`ed coroutines; the adapter uses `hass.async_add_job`/direct `await` inside an `async def` runner, never `run_coro` from the sync harness (that helper owns a private loop in the stub world).

### 8.5 Increment 7: all 91 goldens, first run, triage, ledger

**Occupant.** Every golden is a documented real-world situation (the file names are issue numbers). Replaying them in real HA is how we learn which of those protections only exist on paper.

**Work.** Run all 91, produce the roll-up, triage every divergence under 8.3, file issues for class P, open the ledger. Expected order of magnitude: not predicted here; the point of the run is to find out. The triage rule for the session is stop-and-ask: if more than about 15 scenarios diverge for a single environmental reason, fix the adapter (class E) and rerun before triaging the rest.

**Files.** `tests_ha/test_goldens_realha.py` (parametrized over `tools/simulations/golden/*.json`, marker `goldens`, reads the ledger), `tools/simulations/realha_divergences.json`, `tools/simulations/results/realha/` (generated, gitignored except `SUMMARY.md`), docs.

**Done when.**
- 91 of 91 either pass on real HA or are in the ledger with a class and evidence (no unlabeled red).
- Every class P item has an issue and a failing real-HA test.
- `tools/simulate.py --check-integrity` is clean (no golden edited: verified by the manifest hash and by `git diff --stat tools/simulations/golden` being empty).
- The whole goldens run is inside its budget (section 11).

**Proof.** (a) A deliberate sabotage run once: delete a behavior a golden names (for example the settling-hold in the #1073 scenario, or a gate in the observe-only path), and confirm the scenario fails on real HA with the same assertion it fails on in FakeHass. This is exercise (a) of the three-exercise protocol applied to the new adapter ("would this fail if the behavior were deleted"). (b) Run the same sabotage with the real-HA adapter disabled to show the ledger does not mask it.

**Three-exercise protocol (CLAUDE.md global item 10), answered for this increment.** (a) Effectiveness: the sabotage runs above. (b) Documentation alignment: any class P finding gets the docs-vs-production comparison written in its issue (the simulator is production now; only docs can drift). (c) User outcome: each ledger entry carries its "what the occupant would experience" sentence.

**Risks.**
- R12: noise. If the real-HA run is flaky on one machine, the 3x rerun in Step 3 of triage will show it; flaky scenarios are fixed in the adapter before they are ledgered. A flaky entry in the ledger is not allowed.
- R13: scenarios that need a thermostat quirk. None of the 91 use `thermostat_quirk` today (checked), but the FakeHass adapter supports `sensi_like`. Any future scenario using a quirk is tagged `needs-sim-quirk` and stays on FakeHass until the sim grows an equivalent option (a `dev_tools` change, not a production one).

---

## 9. Item (g) in one place: the startup coalesce timer

Short version for the person reading only this section.

- **Is the timer still uncancelled?** No. It was fixed in v0.8.0 by #1102 (commit `b48fca0`, F1) and covered by `tests_ha/test_unload_cleanup.py`. Verified on HEAD (finding F1).
- **What does the house fixture do with the 300 s window?** It offers `startup="cold"` (window open, for tests about start-up) and `startup="settled"` (default: advance 301 s so the window has closed and the coalesce refresh ran). Teardown always unloads both entries in reverse order; HA's `verify_cleanup` is the net; the fixture adds no waiver.
- **Is a GitHub issue filed when Step 2 starts?** Recommended: no bug issue, because there is no open bug. If a tracking issue is wanted, file "House fixture asserts the start-up and teardown timer contract" and close it with Increment 2. This is decision U3 in section 14.
- **What would change the answer?** If the house's first run shows a lingering timer, task or thread, that is a new finding with its own issue (HA's message names the object), and the symmetric start-side check in section 0.3 is where it would show.

---

## 10. Increment 8: Retirement path for the home-made fakes (brief item e)

**Occupant.** A smaller, less duplicated test suite means fewer tests that are green because they only test the fake. Nothing is deleted before the thing replacing it is proven to catch the same bug.

### 10.1 Rules for retiring anything

1. A unit test is retired only when a real-HA test covers the same occupant-visible behavior **and** that real-HA test has a passing revert proof for the bug the unit test was written for **and** the unit test has no branch coverage that exists nowhere else (a coverage diff on the affected module before and after is attached to the PR).
2. The user sees the retirement table for each wave and approves it. The assistant never deletes a test on its own judgment.
3. A pure-logic unit test (classifier math, temperature conversion, OLS, FSM transition tables) is never retired for being "unit": it is fast, Windows-native and not a fake of anything.
4. Nothing in this increment changes `custom_components/`.

### 10.2 What moves first, and when each thing can be deleted

| Artifact | Size and reach | Wave | First move | Delete when | Stays? |
|---|---|---|---|---|---|
| `tests/test_reload_teardown.py` (T1, FakeScheduler-based) | 1 file, 103 lines | 1 (after Increment 3) | The #1102 real-HA test supersedes it | Revert proof for #1102 is green and the T1 unit test adds no unique assertion (the unit test also checks "arms no timer after unload", which the real test covers through HA's `verify_cleanup`) | Static teardown guards (`test_teardown_static.py`, AST-level, fast) stay |
| Hand-written persisted-snapshot restart tests, e.g. `tests/test_fan_manual_override_restart.py` | 1 file, builds restart state by literal dict | 1 | Keep the pure decision cases; retire only the cases that pretend to be a restart | After #1045's real round-trip test is green with revert proof | Decision-table cases stay |
| `tests/test_observe_only_gates.py` end-to-end-ish cases | 1 file | 1 | Keep per-choke-point gate tests (cheap, precise); retire nothing until the thermostat-path twin exists in the house | After both #1108 flows (fan and thermostat) are green with revert proof | Per-gate unit tests stay; they localize failures |
| `build_headless_coordinator` / `build_headless_engine` / `build_headless_multi_zone` users | 23 test files | 2 | Migrate the ones whose subject is lifecycle, listener timing, restart or reload to the house, one file per PR | The last lifecycle-shaped user is migrated; engine-only decision tests may keep `build_headless_engine` (no HA behavior is claimed) | `build_headless_engine` stays as the fast decision harness |
| FakeScheduler | 12 test files, plus the FakeHass runner | 3 | Nothing moves directly; it is the clock of the FakeHass adapter | Only when the FakeHass adapter is deleted | See FakeHass adapter below |
| FakeHass adapter (`run_production.py`), `tools/simulate.py` default engine | Used by 91 goldens, `tools/simulate.py`, `tests/test_production_harness.py`, the AI "run simulate before changing `automation.py`" workflow | 4 | Stays the **default** fast loop (offline, Windows-native, seconds) and the real-HA adapter becomes `--engine realha` | **All** of: (1) 91/91 pass or are ledgered on real HA for 4 consecutive weeks of PRs; (2) real-HA golden replay completes inside the section 11 budget on a developer laptop, not just CI; (3) a Windows developer can run it with one command (WSL as today); (4) no scenario is tagged `needs-sim-quirk`; (5) the user approves | Not before the criteria hold; recommended default is **keep** (decision U1) |
| `ha_stubs.py` (637 lines) and the `tests/conftest.py` stub layer | 98 test files depend on it | n/a | Not a Step 2 deletion target | Never inside Step 2. It is the import shim for the unit tier; it shrinks only as unit tests stop importing HA-dependent production modules | Stays. Track "files that need stubs" as a number in `docs/DEVELOPMENT.md` |
| Hand-built coordinators (`object.__new__` + `types.MethodType`) | 116 test files (the pattern CLAUDE.md prescribes) | n/a | New rule from this step: **no new hand-built coordinator for lifecycle, listener, timer, persistence or unit-handling behavior; use the house.** Existing ones are migrated opportunistically when a bug touches them | Per file, under rule 10.1 | Pure-method tests (a single `_compute_*` method) keep the pattern; it is correct there |
| Per-test thermostat fakes (`MagicMock` climate, FakeHass state feedback) | Scattered | n/a | Same rule: new thermostat behavior uses `ThermostatSim` | Dies with the tests that use it | Stays where the unit is a pure function |
| Differential and `*_compare` tools, enumerator, two-phase | About 1,200 lines, ~15 files | n/a | Not touched | Epic #594 (FSM migration) retires them | Out of scope |
| Tier B Docker harness (`tools/integration_harness/`) | 6 tests, HA 2025.5.3 | n/a | **Frozen** (user decision). Not extended, not deleted | Only if Steps 2-3 leave a specific gap and the user decides | The config seed it carries is read by the house; see R4 |

### 10.3 What stays forever, stated plainly

- The unit tier (`tests/`, stubbed Home Assistant) for pure logic.
- The FakeHass adapter as the fast default loop, until section 10.2's five criteria hold and the user agrees.
- The Tier B Docker harness, frozen.
- The live multi-zone install as the final check on real hardware.

### 10.4 Files and done-criteria for wave 1

**Files.** `docs/DEVELOPMENT.md` (retirement table and the "no new hand-built coordinator for lifecycle behavior" rule), `docs/sim-harness-spec.md` (adapter table, ledger), CLAUDE.md Testing Requirements (a short pointer; the long MagicMock guidance is annotated "legacy, prefer the house for lifecycle behavior"), and the three retired or trimmed test files from wave 1.

**Done when.** Wave 1 retires exactly the cases the table lists, each with its revert-proof output and coverage diff attached; the unit tier count and the `ha` tier count are both reported before and after; no coverage drop on `automation.py`/`coordinator.py` lines the retired tests were the only owner of (proved by the coverage diff, not asserted).

---

## 11. Increment 9: Wall-clock budget and CI (brief item f)

### 11.1 Measured and estimated cost

| Item | Number | Source |
|---|---|---|
| Existing `ha` tier today | 2 tests in 3.6 s of pytest time, about 11 s wall including WSL and uv start-up | Run on this PC, 2026-10-30 |
| One house test (setup, 20 simulated minutes at 30 s, teardown) | About 1.5 s | Spike |
| One step (30 s of simulated time, whole real CA plus sim) | About 3.7 ms (includes a retry storm, so a pessimistic figure) | Spike, F7 |
| Golden replay, serial | 405 virtual hours = 48,600 steps at 30 s = about 3 min, plus 91 setups at about 0.7 s = about 1 min | F8 |
| Golden replay with xdist, 4 workers | About 1 to 1.5 min | xdist 3.8.0 is in the lock |
| Escaped-bug tests + lifecycle + smoke + snapshots | About 30 to 45 s | Estimate from the above |

### 11.2 Budget (enforced, not aspirational)

| Where | Budget |
|---|---|
| Whole `ha` tier on GitHub Actions (`ubuntu-latest`, 4 workers) | **5 minutes** wall, hard cap **8 minutes** (`timeout-minutes: 8` on the job) |
| Whole `ha` tier on this PC (WSL, 4 workers) | 3 minutes |
| Inner loop, excluding goldens (`-m "not goldens"`) | 30 seconds |
| Any single test | 60 s (`timeout = 60`); tests marked `slow` are listed in the PR if they exceed 5 s |
| Real-HA golden replay alone | 90 seconds with 4 workers, 4 minutes serial |

If the tier passes 5 minutes, the first response is to measure with `--durations=25` and fix the slowest step size or setup cost, not to raise the cap. If it still exceeds the cap, split `goldens` into its own job (same script, `-m goldens`), which runs in parallel and is not slower for the PR author.

### 11.3 CI shape

`.github/workflows/validate.yml` already calls `python3 scripts/test.py ha` in the `ha-lifecycle` job; that stays. Changes:

- Rename the job to `ha-real` (its content is no longer only lifecycle), set `timeout-minutes: 8`, set `CA_HA_WORKERS=auto` in `env`, and add `--durations=15` so budget drift is visible in the log.
- Confirm uv's cache is on for the Linux runner (`astral-sh/setup-uv@v10.2.0` defaults to caching on GitHub-hosted runners; verify in the first run rather than assume).
- Snapshots: CI passes no `--snapshot-update`; an unused or mismatched snapshot fails the job.
- Real-HA golden replay and the divergence ledger run inside the same job at first. The `compare` mode (FakeHass vs real HA diff) is a local developer tool and a **weekly scheduled** workflow, not a PR gate, because it needs both environments and its output is a report; the PR gate is the ledger ratchet (unlisted divergence fails).
- The `ha` job should be a **required** status check for merging to `main` once it has been stable for two weeks. That is a repository setting, so it is decision U2 for the user.

### 11.4 Docs convergence (Scribe lane, standing)

Every increment updates the doc it touches and fills that doc's Anchors table (a `_(placeholder)_` row is not done). At the end of Increment 9: `docs/DEVELOPMENT.md` (house, markers, revert-proof, snapshots, budget, retirement table), `docs/sim-harness-spec.md` and `docs/sim-harness-brief.md` (two adapters, dispatch modes, ledger, divergence classes), `docs/00-PROJECT-INSTRUCTIONS.md` Anchors rows (tests_ha house, revert-proof, snapshots, divergence ledger), CLAUDE.md Testing Requirements (house first for lifecycle; the MagicMock section annotated), and the coverage score update on issue #131 (score must not go down). A Scribe verifies each spec against the code before an issue closes.

**Done when (Increment 9).** CI green with the budget enforced; the weekly compare workflow ran once and attached its `SUMMARY.md`; docs Anchors complete; the epic #1125 has a status comment for Step 2 (sub-issue #1127, PR, version if any, one-line outcome) as the epic rule requires.

---

## 12. Cross-cutting risks and open questions (not decisions)

| # | Risk | Likelihood and effect | Mitigation |
|---|---|---|---|
| R1 | Revert-proof on old trees fails to import under HA 2026.10.4 | Low (checked config `VERSION = 20` on all three pre-fix commits); would stall Increment 3 | Reverse-patch fallback built into the tool |
| R3 | `restart()` is not a true new-process restart | Medium; limits what #1045 proves | Try a two-`hass` restart as a stretch; state the limit in the test docstring if it does not work |
| R4 | House reads the frozen Tier B seed | Low; breaks only if Tier B is deleted | Copy the seed when that happens; documented now |
| R5 | Big clock jumps give the sim's physics one huge step | Medium for casual tests | `advance` slices; guard in `closed_loop` |
| R6 | Session split lands in two places | Medium (schedule), solved by one shared PR | Section 5 |
| R9 | Private `loop._scheduled` for next-timer stepping | Medium (HA internals change) | Fixed-slice fallback; granularity reported |
| R12 | Flaky real-HA golden run | Medium | Adapter fixes before ledgering; no flaky ledger entries |
| R14 | xdist and HA fixtures: per-worker temp dirs, shared ports, shared `tmp_path` | Low (HA core uses xdist) | Each worker builds its own sim portion; no fixed ports are used |
| R15 | Real-HA golden replay hides a production bug behind an adapter quirk (a class E mislabel) | Medium | Class E needs the evidence "same input, same output on a second adapter configuration"; the triage reviewer is not the author of the adapter fix |
| R16 | The first real-HA golden run finds many production bugs at once | Unknown, possibly high | Expected and wanted; stop-and-ask threshold in 8.5; class P items are issues, not Step 2 scope creep |
| R17 | `freezer` also freezes `time.monotonic`-derived things inside CA that the unit tier never saw | Low to medium | `verify_cleanup` and the lifecycle tests will show it; fix in the fixture, not in production, unless a real bug |

---

## 13. Increment file summary (write boundaries)

| # | New files | Edited files |
|---|---|---|
| 1 | `scripts/revert_proof.py` | `tests_ha/conftest.py`, `tests_ha/pytest.ini`, `scripts/test.py`, `pyproject.toml`, `uv.lock`, `docs/DEVELOPMENT.md` |
| 2 | `tests_ha/house/*`, `tests_ha/test_house_lifecycle.py`, `tests_ha/test_house_smoke.py` | `tests_ha/conftest.py`, docs |
| 3 | `tests_ha/test_escaped_bugs.py` | `tests_ha/house/*` (fan variants), `docs/DEVELOPMENT.md` |
| 4 | `tests_ha/test_snapshots.py`, `tests_ha/__snapshots__/*`, `tests_ha/house/surfaces.py` | docs |
| 5 | `tools/sim_harness/session.py`, `tools/sim_harness/scenario_model.py` | `tools/sim_harness/run_production.py` (becomes FakeHass adapter), `tools/simulate.py` (imports only) |
| 6 | `tests_ha/realha_session.py`, `tools/sim_harness/divergence.py`, `tools/sim_harness/ledger.py`, `tests_ha/test_realha_adapter_smoke.py` | `scripts/test.py` (`compare`), docs |
| 7 | `tests_ha/test_goldens_realha.py`, `tools/simulations/realha_divergences.json` | none under `tools/simulations/golden/` (locked) |
| 8 | none | trimmed or retired test files per wave, docs, CLAUDE.md pointer |
| 9 | optional `.github/workflows/ha-compare-weekly.yml` | `.github/workflows/validate.yml`, docs |

No file under `custom_components/` is edited by any increment. If one is (a fix found by a divergence), it follows the normal version-bump process in its own PR.

---

## 14. Decisions

### 14.1 Genuinely the user's (defaults applied so work can proceed; none blocks Increments 1-4)

| # | Question | Recommended default | Why it is the user's |
|---|---|---|---|
| U1 | Does the FakeHass adapter stay as the fast default for `tools/simulate.py` indefinitely, with real HA as `--engine realha`, or is real HA meant to replace it? | **Keep FakeHass as default.** Real HA becomes the second adapter; deletion only if all five criteria in section 10.2 hold and the user agrees | It changes the developer and AI workflow ("run simulate before touching `automation.py`"), and real HA needs WSL on Windows |
| U2 | Make the `ha` job a required status check on `main`, and accept a 5 to 8 minute PR job? | **Yes, after two stable weeks** | It is a GitHub repository setting and a CI-time trade the user owns |
| U3 | Item (g): do not file a bug for the startup timer because it is already fixed (finding F1), or file a coverage-only tracking issue? | **No bug; coverage tracking issue optional** | The brief and the assessment both assume an open bug; the code and test history say otherwise. The user should confirm before the plan text is corrected |
| U4 | Approve the retirement waves (section 10) one wave at a time, with the revert-proof and coverage diff shown | **Yes, per wave** | Deleting tests is the user's call under the project's review rules |

### 14.2 Decided here (reversible)

- One seam: the Step 3 `Session` protocol; no second protocol.
- Thermostat fake: `ca_dev_thermostat_sim` only; `scripted` profile for open-loop replay (zeroed k values), no `dev_tools/` edit.
- Loader: namespace portion in a session temp dir plus first-import pin; nothing under `custom_components/`.
- Revert proof by `git archive` overlay (`scripts/revert_proof.py`), never a checkout.
- Snapshots in `tests_ha/`, syrupy named explicitly in the `test-ha` group, no CI snapshot updates.
- The divergence ledger shares one ratchet implementation with Step 3's ledgers.
- Time stepping starts at fixed 30 s slices and moves to next-timer stepping only where Increment 6 needs it.

### 14.3 UNVERIFIED (confirm during the named increment)

- Fixed 30 s slices are precise enough for the three bug tests (Increment 2 experiment).
- A true two-`hass` restart is stable (Increment 3, stretch).
- Which real trigger reliably makes CA own the thermostat blower for the #1108 flow (Increment 3).
- Syrupy's output is stable across two machines for the `sensor` attribute shapes (Increment 4).
- uv cache is on for the Linux runner (Increment 9).
- Real-HA golden replay meets 90 s at 4 workers (Increment 7).
- How many of the 91 goldens diverge, and why (Increment 7); the answer is the point of the run.
