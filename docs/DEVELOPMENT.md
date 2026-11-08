<!-- Nav: ← [Project Instructions](00-PROJECT-INSTRUCTIONS.md) → [SSH Setup](SSH-SETUP.md) -->

# Development Guide

How to set up a local environment, run the test tiers and the simulator, lint, and get a change through CI.
Test-writing rules (async mocks, stubs, partial instantiation) are in
[claude.md, Testing Requirements](../claude.md#testing-requirements-critical). The release checklist is in
[claude.md, Release Process](../claude.md#release-process-critical--must-follow-this-order). Deploying to a real
Home Assistant is covered in [SSH Setup](SSH-SETUP.md).

## Anchors
| Question | Short answer | → Full answer |
|---|---|---|
| How do I set up the local dev environment without a running Home Assistant? | Install [uv](https://docs.astral.sh/uv/) and run `python scripts/test.py`. uv fetches Python 3.14 (`.python-version`) and the locked dependencies. The `tests/` tier needs no HA: `tools/sim_harness/ha_stubs.py` stubs every `homeassistant.*` module. | [§Setup](DEVELOPMENT.md#setup) |
| How do I run the tests, in parallel, and check coverage? | `python scripts/test.py [unit\|ha\|all] [pytest args]`. `CA_UNIT_SHARDS=4 CA_UNIT_WORKERS=2` (the fastest local unit run) and `CA_HA_WORKERS=auto` turn on parallel runs (unset means serial). Coverage: `python scripts/test.py unit --cov=custom_components/climate_advisor`. | [§Running Tests](DEVELOPMENT.md#running-tests) |
| What linting must pass? | The commands CI runs: `tools/validate.py`, `ruff check .` and `ruff format --check .`, each through `uv run --locked --only-group test`. | [§Linting](DEVELOPMENT.md#linting) |
| Which CI checks must pass to merge to `main`? | Branch protection requires `validate-and-test` and `ha-real` (both in `validate.yml`) and `validate` (Hassfest). | [§CI](DEVELOPMENT.md#ci) |
| How do I run the real-Home-Assistant tier, build the house fixture, prove a test fails without the fix, or update snapshots? | `python scripts/test.py ha` (WSL on Windows). Use the `make_house` fixture in `tests_ha/house/`. `scripts/revert_proof.py` proves an escaped-bug test fails on the pre-fix commit. | [Real-HA tier](DEVELOPMENT.md#real-ha-tier-the-house-markers-revert-proof-snapshots-step-2-issue-1127) |
| How do I run the simulator and its goldens? | `python tools/simulate.py` drives the real `AutomationEngine` (single-engine harness, #236). Goldens are locked by `MANIFEST.json`, and only `--sign` after human review changes them. | [§Simulator](DEVELOPMENT.md#simulator) |
| What does the nightly run, and how do I hear about a failure? | It runs fresh-seed generated days (P1-P7, then P8) and the full real-HA tier. Each seed prints its timing and `peak_rss`. When any job fails, `report-failure` opens one tracking issue, or comments on it if it is already open. | [§Nightly](DEVELOPMENT.md#nightly) |
| Where do the README's test badges come from? | `badges.yml` runs both tiers with coverage and JUnit XML on every push to `main`, daily and by hand; `tools/badges.py` turns the reports and the real-HA ledger into shields.io endpoint JSON on the `badges` branch. The nightly badge is GitHub's workflow status. Issue #1343. | [§Test badges](DEVELOPMENT.md#test-badges) |
| What must a PR carry besides code? | A version bump in `const.py` and `manifest.json`, a `CHANGELOG.md` entry and a `fix_history.jsonl` entry added with `tools/add_fix_entry.py`. `tests/test_version_sync.py` checks that the two versions match. | [§Before Submitting Changes](DEVELOPMENT.md#before-submitting-changes) |
| What is the correct sequence for adding a new module? | Create the module in `custom_components/climate_advisor/` and a matching `tests/test_<module>.py`. Add any new HA submodule to `_HA_MODULES` in `tools/sim_harness/ha_stubs.py`, then run the full unit tier. | [§Adding a New Module](DEVELOPMENT.md#adding-a-new-module) |

## Prerequisites

- [uv](https://docs.astral.sh/uv/). On Windows: `python -m pip install --user uv` (or `winget install --id=astral-sh.uv -e`).
  On Linux, macOS or WSL: `curl -LsSf https://astral.sh/uv/install.sh | sh`. uv installs the Python in `.python-version`
  (`3.14`). `pyproject.toml` requires `>=3.14.2`, which the pinned HA 2026.10.4 needs.
- Git.
- On Windows only, for the real-HA tier: WSL with an Ubuntu distro that also has uv (`CA_WSL_DISTRO`, default
  `Ubuntu`). Real Home Assistant imports the Unix-only `fcntl`.

## Setup

Dependencies live only in the `test` and `test-ha` dependency groups of `pyproject.toml`, pinned by `uv.lock`. There
are no requirements files. The two groups must never share an environment: the real-HA pytest plugin auto-loads into
any pytest run, while `tests/conftest.py` stubs `homeassistant` for the whole process. `[tool.uv] conflicts` enforces
this, and every command uses `--only-group`. `scripts/test.py` gives each tier its own venv and creates it on the first
run: `.venv-test` and `.venv-test-ha` (on Windows, `~/.cache/climate-advisor/venv-test-ha` inside WSL).

After editing dependencies, run `uv lock` and commit `uv.lock`. CI and `scripts/test.py` both use `--locked`.

## Running Tests

```bash
python scripts/test.py                     # both tiers (default "all")
python scripts/test.py unit                # tests/ against stubbed HA
python scripts/test.py ha                  # tests_ha/ against real HA (via WSL on Windows)
python scripts/test.py unit tests/test_classifier.py -k hot   # extra args go to pytest
python scripts/test.py unit --cov=custom_components/climate_advisor --cov-report=html   # report in htmlcov/
```

- The unit tier always runs with `-W error::pytest.PytestUnraisableExceptionWarning -W error::RuntimeWarning`.
- A skip caused by a missing or stubbed package is turned into a failure (`tests/conftest.py`).
- **Parallel runs:** `CA_UNIT_WORKERS` and `CA_HA_WORKERS` set the pytest-xdist worker count, as a number or `auto`.
  Unset or `0` runs serially. CI uses `4` for both. `CA_HA_WORKERS=auto` is fine locally.
- **Fastest local unit run:** `CA_UNIT_SHARDS=4 CA_UNIT_WORKERS=2 python scripts/test.py unit`. It runs four
  round-robin slices of the unit tier at once, each a pytest process with 2 workers, prefixes each slice's output with
  `[K/4]` and ends with each slice's time. That is 8 processes, the same memory as `CA_UNIT_WORKERS=8`, but faster:
  4 min 26 s against 7 min 50 s on a 24-core dev container (measured back to back, 2026-11-07), probably because
  one process ends on a long tail of slow `test_sim_generator` tests that four slices share out. Keep slices x workers
  near 8 on a RAM-limited machine (each worker holds about 1.8 GB); `auto` workers can run out of RAM.
- **One CI slice:** `CA_UNIT_SHARD=K/8` runs only the Kth of the eight slices, exactly as CI's `unit (K/8)` job does,
  to reproduce a failure in that job. `CA_HA_SHARD=K/4` does the same for an `ha-real (K/4)` job.
- **Order-dependence guard (#1186):** `python scripts/test_changed_isolated.py [--base origin/main] [--all]` runs each
  changed `tests/test_*.py` in its own pytest process. CI runs it with `--strict`.

### Real-HA tier: the house, markers, revert-proof, snapshots (Step 2, Issue #1127)

`tests_ha/` runs Climate Advisor inside a real Home Assistant core, under HA's own leak checker (#1102). It is Tier B
of the [Sim Harness](sim-harness-brief.md#tier-a-and-tier-b). `tests_ha/house/` builds one small real home:

- the simulated thermostat (`ca_dev_thermostat_sim`, which runs Climate Advisor's heat-flow math);
- a real `WeatherEntity` with a forecast service;
- contact sensors, an optional fan and a notify service;
- the unit system and a frozen clock.

Use the `make_house` fixture. Do not hand-build a coordinator to test lifecycle, listener, timer, persistence or
unit-handling behavior.

```python
house = await make_house(units="us", profile="scripted", fan="whf", contacts=["window"], startup="cold")
await house.open("window"); await house.advance(minutes=30)   # 30 s slices, timers fire on time
house.thermostat.mode / .setpoint_f / .indoor_f / .action / .fan_mode
house.calls          # every service call with origin "occupant" | "ca:<n>" (n-th instance) | "other"
await house.occupant_presses_fan(True); await house.occupant_sets_thermostat(mode="heat", setpoint_f=70)
await house.reload_zone(); await house.restart(down_for_seconds=0); await house.close()
```

* `profile`: `closed_loop` (the default; physics runs) or `scripted` (all k values are zero, and the test sets indoor
  with `set_indoor`).
* `units`: `us` or `metric`. It sets HA's unit system and Climate Advisor's `temp_unit` together, because a mismatch
  makes HA reject every setpoint.
* `fan`: `None`, `whf`, `whf_dual`, `whf_command_only` or `hvac_fan`. The `make_house` docstring in
  `tests_ha/house/builder.py` describes these and the other keyword arguments.
* `startup`: `cold` leaves the 300 s override-suppression window open. `settled` (the default) advances past it.
* Lifecycle: `close()` unloads Climate Advisor, then the sim, and HA's autouse `verify_cleanup` is the safety net.
  `restart()` unloads and sets up again on the same `hass` and config dir, so it exercises persistence but not HA boot
  ordering.
* **The 30 s slices pitfall:** `advance()` fires timers only at the end of each slice (`tests_ha.house.builder.SIM_STEP`).
  A timer due mid-slice runs at the slice's end, at the same virtual instant as the next `occupant_*` call. The
  occupant's change then lands inside CA's echo guard (3 s for mode, 30 s for setpoint) and goes undetected, which a
  real household would not see (#1235 observation A). To test occupant timing near a CA write, monkeypatch `SIM_STEP`
  to 1 s, or place the change at least 3 s (mode) or 30 s (setpoint) after the write.
* Markers: `house`, `goldens`, `snapshot` and `slow`. `tests_ha/pytest.ini` sets `--strict-markers` and `timeout = 60`.

**Revert proof.** A real-HA test for an escaped bug is worth having only if it fails on the code from before the fix:

```bash
python scripts/revert_proof.py 'b686a05^' tests_ha/test_escaped_bugs.py::test_observe_only_does_not_release_blower
python scripts/revert_proof.py '2507a19^' tests_ha/test_escaped_bugs.py::test_manual_fan_survives_restart
python scripts/revert_proof.py 1df0246   tests_ha/test_escaped_bugs.py::test_reloaded_zone_leaves_no_ghost
```

The script extracts `custom_components/` at the commit with `git archive` into a scratch directory
(`CA_COMPONENT_ROOT`). It requires the tests to fail there and to pass on the working tree, and it never checks out,
resets or stashes anything. Exit codes:

- `1`: the tests did not fail pre-fix.
- `2`: the tests failed on the working tree.
- `3`: the old tree would not import. Use `--reverse-patch <fix-commit>` instead.

| Bug | Test | Pre-fix commit |
|---|---|---|
| #1108 observe-only releases the person's fan | `test_observe_only_does_not_release_blower` | `b686a05^` |
| #1045 manual fan switched off after restart | `test_manual_fan_survives_restart` | `2507a19^` |
| #1102/#1117 ghost copy after a settings save | `test_reloaded_zone_leaves_no_ghost` | `1df0246` |

**Snapshots (#945).** `tests_ha/test_snapshots.py` compares the real briefing, notifications, Status, Next User
Action, Next Automation and fan cards with `tests_ha/snapshots/test_snapshots.ambr`, across a set of reference days.
CI never passes `--snapshot-update`. To update the snapshots on purpose, run
`python scripts/test.py ha --snapshot-update tests_ha/test_snapshots.py`. Then paste the before and after text in the
PR and say why the occupant is better off.

## Simulator

`tools/simulate.py` is a report and CLI shell over `tools/sim_harness/`, which drives the **real** production
`AutomationEngine` headless, with FakeHass and a virtual clock. There is no second simulator to keep in sync (#236).
See the [Sim Harness Brief](sim-harness-brief.md).

```bash
python tools/simulate.py                  # all golden scenarios (tests/test_production_harness.py runs them too)
python tools/simulate.py -s NAME -v       # one scenario, with its decision timeline
python tools/simulate.py --check-integrity
python tools/simulate.py --generate 20 --seeds 1 2 --waiver-ledger tools/sim_harness/nightly_waivers.json --issue-state offline
python tools/simulate.py --generate-day <day_seed>   # replay one generated day from a failing run's log
```

Goldens in `tools/simulations/golden/` are locked. Each file's hash is signed into `MANIFEST.json`, and the pre-commit
hook runs `--check-integrity` on any golden change. Only `python tools/simulate.py --sign NAME` updates the manifest,
after a human has reviewed the scenario card. The full policy is in
[claude.md, Golden Simulation Test Policy](../claude.md#golden-simulation-test-policy). On Windows, run `--fix-crlf`
before `--sign`.

## Linting

[Ruff](https://docs.astral.sh/ruff/) settings are in `pyproject.toml` (line length 120). CI runs exactly these
commands:

```bash
uv run --locked --only-group test python tools/validate.py
uv run --locked --only-group test ruff check .            # add --fix to auto-fix
uv run --locked --only-group test ruff format --check .   # drop --check to format
```

The pre-commit hooks (ruff `--fix`, ruff-format, validate and the golden integrity check) and the rule about
re-staging files are in [claude.md, Pre-commit Hooks](../claude.md#pre-commit-hooks).

## CI

| Workflow | Job | What it runs | Required on `main` |
|---|---|---|---|
| `validate.yml` | `validate-and-test` | The gate: passes only when `lint`, all eight `unit` slices and `isolated` pass | yes |
| `validate.yml` | `lint` | `tools/validate.py`, `ruff check .`, `ruff format --check .` | via the gate |
| `validate.yml` | `unit (K/8)` | One round-robin eighth of the unit tier each (`CA_UNIT_SHARD=K/8`, `CA_UNIT_WORKERS=4`), in parallel | via the gate |
| `validate.yml` | `isolated` | Changed unit test files, each alone (`CA_UNIT_WORKERS=4`) | via the gate |
| `validate.yml` | `ha-real` | The gate: passes only when all four `ha-real (K/4)` slices pass | yes |
| `validate.yml` | `ha-real (K/4)` | One round-robin quarter of the real-HA tier each (`CA_HA_SHARD=K/4`, `CA_HA_WORKERS=4`, `--durations=15`, 8-minute cap), in parallel | via the gate |
| `hassfest.yml` | `validate` | Home Assistant's Hassfest | yes |
| `hacs.yml` | `hacs` | HACS validation | no |
| `nightly.yml` | see [§Nightly](DEVELOPMENT.md#nightly) | Scheduled runs | no |
| `ha-compare-weekly.yml` | `compare` | Every golden on FakeHass and on real HA, with a diff report | no |
| `badges.yml` | `unit`, `ha`, `publish` | Both tiers with coverage, then the README badges (see [§Test badges](DEVELOPMENT.md#test-badges)) | no |

If tests pass locally but fail in CI, check that any new test dependency is in the `test` group and that `uv.lock` was
regenerated, because CI uses `--locked`.

## Nightly

`nightly.yml` runs every day at 06:17 UTC. You can also start it by hand (`workflow_dispatch`, with `seeds`, default
12, and `days`, default 250).

| Job | What it does |
|---|---|
| `seeds` | Picks fresh master seeds once and prints them, so every job judges the same seeds and a failure can be replayed. |
| `generated-days` | Runs `simulate.py --generate <days> --seeds ...`, judged by the household promises P1-P7 and the waiver ledger (`--issue-state github`). It prints each seed's timing and `peak_rss`: read these before raising the 120-minute cap. Failing scenarios are uploaded as the `generated-failures` artifact. |
| `surfaces` | Runs `simulate.py --surfaces --generate 50` on the same seeds (P8, ratcheted by `tools/simulations/surface_divergences.json`). |
| `ha-lifecycle` | Runs the full real-HA tier. |
| `report-failure` | Runs when any job failed or was cancelled. It opens the issue "Nightly generated-days / real-HA run failing", or comments on it if it is already open. Close that issue once the nightly is green again. |

To replay a failure, run `python tools/simulate.py --replay-generated <file>` on the downloaded artifact, or
`--generate-day <day_seed> --profile <profile>` with the values from the log.

## Test badges

The README's second badge row (Issue #1343) shows every test layer, in the same `flat-square` style as the version
badges. `badges.yml` builds them on every push to `main`, daily at 07:37 UTC (after the nightly) and by hand. It is
not a required check and never runs on PRs, so coverage does not slow the PR jobs or their time caps.

| Badge | Source | Example |
|---|---|---|
| coverage | Line and branch coverage of `custom_components/climate_advisor`, both tiers combined (`coverage combine`) | `84%` |
| unit tests | `tests/` JUnit XML and that tier's own coverage | `12,107 passed · 81% cov` |
| real HA tests | `tests_ha/` JUnit XML and that tier's own coverage | `468 passed · 62% cov` |
| goldens | `test_golden_passes_production_engine` cases (Tier A: every golden through the real engine) | `93/93 pass` |
| goldens on real HA | `test_golden_on_real_ha` cases, minus the entries in `tools/sim_harness/realha_ledger.json` | `88 match · 5 triaged` |
| nightly | GitHub's own status of `nightly.yml` on `main` (shields.io `github/actions/workflow/status`) | `passing` |

- **Flow:** the `unit` and `ha` jobs run `scripts/test.py` with `--cov=custom_components/climate_advisor
  --cov-branch --junitxml=...` (`COVERAGE_FILE` per tier) and upload `reports/`. `publish` writes a coverage JSON per
  tier and a combined one, runs `python tools/badges.py --out badges` and force-pushes the files as a single commit
  to the `badges` branch. The README reads them through `img.shields.io/endpoint`.
- **Honest by design:** a failing test still publishes, and the badge turns red with the failure count. A missing
  report says `unknown` in grey; it never keeps an old green number.
- **Colours:** coverage ≥90 brightgreen, ≥80 green, ≥70 yellowgreen, ≥60 yellow, ≥50 orange, below that red.
  Tier badges are brightgreen with no failures, otherwise red. The real-HA goldens badge is green while ledgered
  divergences exist.
- **Locally:** `COVERAGE_FILE=reports/coverage.unit python scripts/test.py unit --cov=custom_components/climate_advisor
  --cov-branch --cov-report= --junitxml=reports/unit.xml` (and the same for `ha`), then `coverage json` per data file
  and `python tools/badges.py --out badges`. `reports/` and `badges/` are git-ignored.
- shields.io and `raw.githubusercontent.com` cache for a few minutes, so a new run shows up shortly after it finishes.

## Common Workflows

### Before Submitting Changes

1. Lint (see [§Linting](DEVELOPMENT.md#linting)).
2. Run `python scripts/test.py unit`. Also run `ha` when you touched lifecycle, listeners, timers or persistence.
3. Do the release bookkeeping, which every PR needs:
   - Bump `VERSION` in `custom_components/climate_advisor/const.py` and `"version"` in `manifest.json`.
   - Add a `CHANGELOG.md` entry.
   - Append one `fix_history.jsonl` entry per issue with
     `python tools/add_fix_entry.py --issue N --version X.Y.Z --title "..." --scope "..." [--user-summary "..."]`.
   - Run `python scripts/test.py unit tests/test_version_sync.py` to check that `const.py` and `manifest.json` match.

   Ordering, version conflicts and GitHub Releases are covered in
   [claude.md, Release Process](../claude.md#release-process-critical--must-follow-this-order).

### Adding a New Module

1. Create the module in `custom_components/climate_advisor/`.
2. Create a matching `tests/test_<module>.py`, using the shared fixtures in `tests/conftest.py` (for example
   `basic_forecast`).
3. If the module imports a new HA submodule, add it to `_HA_MODULES` in `tools/sim_harness/ha_stubs.py`, the single
   stub source for both the unit tier and the harness.
4. Run the full unit tier.

## Troubleshooting

**`ModuleNotFoundError: homeassistant`:** run the tests through `python scripts/test.py unit` from the project root,
so that `tests/conftest.py` installs the stubs. Running a test file in a venv that has neither group installed fails
the same way.
