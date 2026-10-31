# Test Automation Plan — Round 3 (simplified)

*Draft for discussion. Nothing has been built in the repo. Round 3, 2026-10-29: rechecked scope, blast radius, and duplication after round 2 came out too complicated.*

> **Implementation status (2026-10-29).** Being built under epic #1125 (sub-issues #1126, #1127, #1128) on branch `feature/1125-test-automation`.
>
> This plan was first written against an older branch. Current main (v0.8.1) had already delivered part of it in #1102:
> - Python 3.14 is required.
> - A real-Home-Assistant test folder (`tests_ha/`) exists with its own CI job.
> - The "startup timer left running after unload" bug described below is the same bug #1102 fixed and released in v0.8.0.
>
> The steps below build on that work rather than repeating it.

---

## The short version

Round 2 had six phases and six test layers. Rechecking it showed the cause: **it kept adding new testing tools instead of replacing the ones we already have twice over.** The test setup already repeats the same jobs several times. Adding more layers on top would make that worse.

The simplified plan has **three steps**, each removing duplication rather than adding to it:

| Step | In plain English | What it replaces |
|---|---|---|
| **1. One test environment** | Tests run the same way on your PC and on GitHub, with one command and nothing skipped silently | Four Python versions, unpinned packages, 46 skipped tests |
| **2. One way to fake the house** | Tests run Climate Advisor inside real Home Assistant, using one simulated thermostat | Three home-made imitations of Home Assistant and several thermostat fakes |
| **3. One list of household promises** | Each promise is written down once. Production checks it at home, and every test checks it too | Promises scattered across golden assertions, one production watchdog check, and individual unit tests |

The October "briefing, chart and cards disagree" bugs fold into Step 3 as one more promise: *what Climate Advisor tells you matches what the thermostat is doing.* The virtual-house container and mutation testing are dropped from this plan (see "Cut or deferred").

---

## Recheck 1: Scope — what problem are we solving?

Your goals, in your words: behavior changes keep causing regressions and that needs to be solid; the setup should be easy for other developers to adopt, fast, and good value; and it should be in place before the learning and prediction work starts.

Measured against those goals:

| Round 2 item | Serves the goal? | Decision |
|---|---|---|
| Phase 0: fix the environment | Yes. Nothing else is trustworthy without it | **Keep → Step 1** |
| Phase 1: real Home Assistant tests | Yes. About 25 recent bugs, and it already found one more | **Keep → Step 2** |
| Phase 2: "all surfaces agree" grid | Yes, but as a separate tool it duplicates Step 3 | **Fold into Step 3** as one promise |
| Phase 2: saved copies of briefing text | Small and useful. The tool comes free with the Home Assistant test package | **Fold into Step 2** |
| Phase 3: household rules | Yes. This is the main protection against regressions | **Keep → Step 3** |
| Phase 4: virtual-house Docker container | Steps 2 and 3 cover what it was for (restarts, timers, long runs) faster | **Cut for now** |
| Phase 5: mutation testing, coverage targets | Measures the tests rather than protecting the house | **Defer** until after Step 3 |

---

## Recheck 2: Blast radius — what does this touch?

### What the current test setup depends on (measured)

| Piece | Size | How many test files depend on it |
|---|---|---|
| `ha_stubs.py`: fakes Home Assistant with mock objects | 543 lines | **98** |
| Hand-built half-real coordinators (`object.__new__` + binding single methods) | spread through tests | **57** |
| Tests using bare `MagicMock()` | — | 182 |
| `FakeHass` + `FakeScheduler` + `build_engine` / `build_coordinator`: fake HA, fake clock, fake setup | ~1,650 lines | ~14 |
| `run_production.py` + `outcomes.py`: runs scenario files and checks their results | ~2,400 lines | 6 + all 92 goldens |
| Comparison tools for the FSM migration (`differential`, `*_compare`, `enumerator`, `two_phase`) | ~1,200 lines | ~15 |
| Tier B Docker harness | 6 tests, HA pinned a year behind (2025.5.3) | — |
| **Total home-made test machinery** | **~8,600 lines** | |

### What this plan changes

| Area | Step | Size of change | Risk |
|---|---|---|---|
| `pyproject.toml`, requirements, new lock file and `.python-version`, GitHub workflow, `scripts/test` | 1 | Small | Low. About 38 never-run tests start running, and some may fail |
| New `custom_components/__init__.py` (one empty file; not shipped, since deploy and HACS only copy `climate_advisor/`) | 2 | Tiny | Low. Confirm hassfest and HACS checks still pass |
| `coordinator.py`: cancel the startup timer on unload (the bug real Home Assistant found) | 2 | A few lines | Low. Needs its own issue and test |
| New real-HA test folder; the scenario runner switches its engine setup from fake HA to real HA | 2 | Medium | **The main risk.** All 92 goldens run on a different base, and some may behave differently. Each difference is either a real bug or a fake-HA artifact, and must be justified under the test-infrastructure rule |
| `invariant_watchdog.py` grows from 1 promise to about 8, and some need recent history, not just a snapshot | 3 | Medium | Production code: new alerts could fire at home. Roll out as log-only first |
| Docs: `DEVELOPMENT.md`, `sim-harness-*.md`, and CLAUDE.md's testing section (much of its MagicMock advice becomes legacy) | all | Medium | Has to be kept in step |

### What this plan does *not* change

- The ~7,400 existing unit tests keep running as they are. There's no big-bang migration. They retire one at a time, only when a real-HA test covers the same thing better.
- No changes to golden scenario contents. Only the engine underneath changes.
- No Home Assistant boundary changes: nothing outside `climate_advisor/` ships or deploys.

---

## Recheck 3: Duplication (DRY) — the same job done more than once

This is the main finding of this round. **The test setup has the same duplication problem the production code has**, and round 2 would have added one more copy of each.

| Job | Done today by | Round 2 would have added | Step that consolidates it |
|---|---|---|---|
| Pretend to be Home Assistant | `ha_stubs.py` (mocks) + `FakeHass` (service capture) + 57 hand-built coordinators + the Tier B Docker HA | Real-HA tests **and** a virtual-house container | **Step 2:** real Home Assistant in-process becomes the one way. The fakes retire over time |
| Pretend time passes | `FakeScheduler`, hand-patched `dt_util`, libfaketime in Docker | — | **Step 2:** Home Assistant's own controllable clock (`freezer`) |
| Pretend to be a thermostat | `FakeHass` state feedback, `ca_dev_thermostat_sim`, HA demo climate in Docker, per-test MagicMocks | "Simulated house entities" | **Step 2:** `ca_dev_thermostat_sim` (it already uses the real thermal model), in tests and on your multi-zone install |
| State a household promise | 1 production watchdog check, scattered golden assertions, individual unit tests | A separate rules module **and** a separate "surfaces agree" grid | **Step 3:** one promises module, used by production and every test |
| Describe a test situation | Golden JSON scenarios, Tier B Python tests, hand-written unit tests | A random-day generator with its own format | **Step 3:** random days *generate* scenario files in the existing format, so they run through the same runner as goldens |
| Show "what Climate Advisor says = what it does" | Separate tests per surface | A separate agreement grid | **Step 3:** one promise, checked at every step |

**One duplication this plan doesn't fix:** the comfort range computed in several places in *production* (engine, chart, briefing, cards). The Step 3 promise will *detect* disagreement, but the real fix is making production compute it once. That's production refactoring, so it gets its own issue rather than going in this plan.

---

## The three steps

### Step 1 — One test environment *(~1–2 days)*

**For the household:** nothing changes directly. Every check after this means the same thing on your PC and on GitHub.

- **Python 3.14** everywhere, declared once in `.python-version`. It ran Home Assistant 2026.10.4 in the hands-on check.
- **`uv` + lock file:** every package pinned, including the ones currently missing (`voluptuous`, `requests`, `tzdata`).
- **A missing package fails the run instead of skipping.** That turns the 46 skips into 0 skips or a clear failure. Five of them are security-validation tests that have never run on GitHub.
- **`scripts/test`:** one command. Existing tests run natively on Windows, and real-HA tests run through `wsl -- uv run …`. Nothing to click, and I can run it too.

**Done when:** a fresh clone runs every test with one command, on your PC and on GitHub, with no "not installed" skips.

### Step 2 — One way to fake the house: real Home Assistant *(~2–3 weeks)*

**For the household:** bugs that only appear on restart, settings save, reload or a flaky sensor get caught on the PR instead of at home.

1. Real-HA test folder, run in WSL and on GitHub's Linux machines. Proven on this PC: Climate Advisor sets up and unloads in real HA, and 4 tests finish in under 5 s.
2. **Setup/unload test.** It already fails on the startup timer that keeps running after unload (details below). File and fix that first. After that, Home Assistant checks automatically, in every test, that nothing is left running.
3. **Three past bugs as permanent tests:** #1108, #1045 and #1117/#1102. Each must fail with its fix undone and pass with it.
4. **Point the scenario runner at real Home Assistant.** The 92 goldens then run through real HA, with `ca_dev_thermostat_sim` as the thermostat. Each difference that shows up gets investigated, not papered over.
5. **Saved copies of briefing and card text** for a handful of reference days (`syrupy`, included with the test package). Any wording change shows up as a diff (#945).
6. From then on, every lifecycle bug fix comes with a real-HA test. When one covers an old fake-HA test, the old one is retired.

### Step 3 — One list of household promises *(2–3 day trial, then ~2 weeks)*

**For the household:** the promises that matter most can't quietly break again, including on code paths nobody thought to test.

1. **Grow `invariant_watchdog.py` into the single promises list.** Each promise is written once. At home it logs and alerts, as it already does for the AC + whole-house-fan rule. In tests it fails the test. Some promises (like "no heat right after cooling") need a short recent history, not just the current reading, so the watchdog's input grows from a snapshot to "snapshot + last hour".
2. **Check every promise after every step** of every scenario: goldens and real-HA tests alike.
3. **Random days:** generate thousands of days of weather, doors and windows, occupancy, overrides and restarts, written as ordinary scenario files and run through the same runner. Each broken promise saves a replay file.
4. **Trial first (2–3 days):** promises 1–3 plus random days. Commit to the rest only if it re-finds the #1073 bug on the pre-fix code without being told where to look.

**The promises (list confirmed 2026-10-29).** The test strictness and numbers are suggested starting values, to be tuned during the Step 3 trial.

A promise states the outer boundary, not the algorithm. It uses its own looser numbers rather than reusing production's tuning values, so a wrong tuning value can't pass both.

| # | Promise | Past bugs | In tests | At home (starts log-only) | Starting numbers |
|---|---|---|---|---|---|
| 1 | After cooling, a shallow dip below the floor doesn't start the heat | #821–#867, #1073 | Hard | Log | Within 2 h of cooling; "shallow" = less than 3°F below the floor |
| 2 | The AC never runs while the whole-house fan runs | #739, #748, #1057 | Hard | **Alert** (already does) | — |
| 3 | With automation switched off, no thermostat or fan commands | #1108 | Hard | Log → alert | — |
| 4 | A manual override is never undone before its grace ends, except by a listed event (bedtime, wake-up, end-grace button) | #1027, #1045, #1055, #1006 | Hard | Log | Exceptions listed explicitly |
| 5 | No fan start while the indoor temperature is unreadable | #1042, #1037, #1043 | Hard | Log | — |
| 6 | No more than N heat/cool mode flips per hour | #1066, #1073 | Warning at first | Log | N = 2 |
| 7 | While the system has control, the house isn't outside the comfort range for more than N minutes (windows open, nat-vent and manual override excluded) | #1066 | Warning | Log | N = 45 min |
| 8 | What Climate Advisor shows (briefing, chart band, cards) matches the range the thermostat is holding | #1087–#1096, #1054, #1079 | Hard | Not checked at home | — |

---

## Cut or deferred, and why

| Item | Why it's not in this plan |
|---|---|
| Virtual-house Docker container | In-process real HA covers restarts, timers and long simulated runs much faster. Freeze the existing Tier B harness and revisit only if Steps 2–3 leave a specific gap. |
| Separate "surfaces agree" grid | It becomes promise 8, so there's no separate tool. |
| Mutation testing, coverage targets | They measure the tests rather than protect the house. Worth doing after Step 3. |
| Moving all ~7,400 unit tests to real HA | No big-bang move; they retire one at a time (Step 2.6). |
| Production refactor so the comfort range is computed once | It's production work, not test work, so it gets its own issue. Promise 8 will show where to start. |
| FSM-migration comparison tools | They belong to the migration (epic #594) and retire when it finishes. Out of scope here. |

What stays as it is: the **live multi-zone install**, which remains your final check on real hardware.

---

## Evidence behind the plan

**Where recent bugs came from** (about 75 bugs, #929–#1124):

| Kind of bug | Rough count | Step that addresses it |
|---|---|---|
| Real-HA life events: restart, settings save, reload, shutdown, unavailable vs unknown, units, thermostat brands | ~25 | Step 2 |
| Behavior regressions and missed "mirror" paths (#1073 was the 7th fix of the same problem) | ~26 | Step 3 |
| Briefing, chart and cards disagree with the engine | ~20 | Step 3, promise 8 |
| The tests themselves (#1117, #1069, #1039, #961, #1083, #945) | 6 | Steps 1–2 |

**Hands-on check on this PC:**

| | Native Windows | WSL Ubuntu |
|---|---|---|
| Install HA 2026.10.4 + test package | 3 min 23 s (pip) | **11 s (uv)** |
| Loads | ❌ HA imports `fcntl`, a Unix-only module | ✅ |
| Climate Advisor sets up, creates 19 sensors + switch, unloads | — | ✅ |
| 4 tests, with the code read from `C:` | — | < 5 s |

**Probable bug already found (not filed yet, at your request).**
- *Household impact, unverified:* if you save settings within 5 minutes of a Home Assistant start, an old copy of Climate Advisor may run one extra update cycle. That's the #1102 "leftover copy" pattern, but I haven't checked whether it sends a command.
- *Confirmed:* the startup timer at `coordinator.py:1429` is never cancelled on unload. It dates from #321 (June 2026), so it's an original design gap, not a recent regression.

**How others test:** the official HA guidance, the test package, and how HACS, Frigate and Versatile Thermostat use it are covered in [Sources](#sources). In short, real Home Assistant runs in-process, its built-in clock control handles time, the libraries the integration talks to are mocked but Home Assistant itself never is, and every bug fix gets a test that fails without the fix.

---

## Decisions (all made 2026-10-29)

1. **Python:** always stay current, meaning the newest version Home Assistant supports, declared once in `.python-version`. GitHub's test runs, docs and `pyproject.toml` follow it.
2. **The 8 promises:** the list is confirmed. Test strictness and numbers start at the values in Step 3 and get tuned during the trial.
3. **Where promises live:** in the production watchdog, as one list used both at home and in tests. Each new promise starts log-only at home and becomes an alert after about two quiet weeks on the live install.
4. **Tier B Docker harness:** frozen. Not extended; revisit only if Steps 2–3 leave a specific gap, such as testing a Home Assistant upgrade before installing it.

Still to do: file the startup-timer bug at the start of Step 2.

---

## Sources

- [HA — Writing tests](https://contributors.home-assistant.io/core/development/tests/)
- [HA — Running the tests](https://contributors.home-assistant.io/core/development/testing/)
- [HA quality scale — test-coverage rule](https://contributors.home-assistant.io/core/quality-scale/test-coverage/)
- [HA integration quality scale](https://developers.home-assistant.io/docs/core/integration-quality-scale/)
- [HA blog — Snapshot testing](https://developers.home-assistant.io/blog/2023/02/20/snapshot-testing/)
- [pytest-homeassistant-custom-component](https://github.com/MatthewFlamm/pytest-homeassistant-custom-component)
- [HACS integration tests](https://github.com/hacs/integration/tree/main/tests)
- [Frigate HA integration](https://github.com/blakeblackshear/frigate-hass-integration)
- [Versatile Thermostat tests](https://github.com/jmcollin78/versatile_thermostat/tree/main/tests)
- [Integration blueprint](https://github.com/ludeeus/integration_blueprint)
- [ha-testcontainer](https://pypi.org/project/ha-testcontainer/1.0.2/)
