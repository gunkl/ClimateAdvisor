<!-- Nav: ← [Project Instructions](00-PROJECT-INSTRUCTIONS.md) → [SSH Setup](SSH-SETUP.md) -->

# Development Guide

How to set up your local environment, run tests, and lint the Climate Advisor codebase.

## Anchors
| Question | Short answer | → Full answer |
|---|---|---|
| How do I set up the local dev environment without a running Home Assistant? | Install [uv](https://docs.astral.sh/uv/) and run `python scripts/test.py` — uv fetches the pinned Python and dependencies from `uv.lock`. HA is not required for the `tests/` tier — `conftest.py` mocks all HA modules so Climate Advisor modules import standalone. | [§Setup](DEVELOPMENT.md#setup) |
| How do I run the full test suite and check coverage? | `python scripts/test.py` runs both tiers (`unit` / `ha` to pick one; extra args go to pytest). Directly: `uv run --only-group test pytest tests/`; add `--cov=custom_components/climate_advisor` for coverage; `--cov-report=html` for an HTML report in `htmlcov/`. | [§Running Tests](DEVELOPMENT.md#running-tests) |
| What linting commands must pass before submitting a change? | `ruff check custom_components/ tests/` (check), `ruff check --fix` (auto-fix), `ruff format` (format). All three should be clean — the pre-commit hook enforces the same settings. | [§Linting](DEVELOPMENT.md#linting) |
| What is the correct sequence for adding a new module? | Create the module in `custom_components/climate_advisor/`, create a matching `tests/test_<module>.py`, add any new HA submodule imports to `_HA_MODULES` in `conftest.py`, then run the full suite. | [§Adding a New Module](DEVELOPMENT.md#adding-a-new-module) |
| How do I run the real-Home-Assistant tier, build the house fixture, prove a test fails without the fix, or update snapshots? | `python scripts/test.py ha` (WSL on Windows); use the `make_house` fixture in `tests_ha/house/`; `scripts/revert_proof.py` proves an escaped-bug test fails on the pre-fix commit. | [Real-HA tier](DEVELOPMENT.md#real-ha-tier-the-house-markers-revert-proof-snapshots-step-2-issue-1127) |

## Prerequisites

- [uv](https://docs.astral.sh/uv/): `python -m pip install --user uv` (Windows), `curl -LsSf https://astral.sh/uv/install.sh | sh` (Linux/macOS/WSL). uv installs the Python version declared in `.python-version` (always the newest Python Home Assistant supports) by itself.
- Git
- Windows only, for the real-HA tier: WSL with an Ubuntu distro (default name `Ubuntu`, override with `CA_WSL_DISTRO`) that also has uv. Real Home Assistant imports the Unix-only `fcntl` and cannot run natively on Windows.

## Setup

Dependencies live in one place: the `test` and `test-ha` dependency groups in `pyproject.toml`, pinned by `uv.lock`. There are no requirements files. The two groups must never be installed into the same environment (the real-HA pytest plugin auto-loads into any pytest run, while `tests/conftest.py` stubs `homeassistant` for the whole process), so uv is always run with `--only-group`, and `[tool.uv] conflicts` enforces it.

```bash
python scripts/test.py          # both tiers; creates .venv-test (and the ha venv) on first run
python scripts/test.py unit     # stub tier only
python scripts/test.py ha       # real-HA tier only (via WSL on Windows)
python scripts/test.py unit tests/test_classifier.py -k hot   # extra args go to pytest
```

A missing required package fails the run (it never skips): `tests/conftest.py` turns any "not installed" / "is a stub" skip into a failure. After editing dependencies run `uv lock` and commit `uv.lock`.

## Running Tests

Tests live in the `tests/` directory. Home Assistant is **not** required — the test conftest mocks all HA modules so Climate Advisor modules can be imported standalone.

```bash
# All stub-tier tests (what CI runs)
python scripts/test.py unit

# Faster iteration inside the unit environment
uv run --only-group test pytest tests/test_classifier.py -v -k "test_classify_hot_day"
```

A second, small tier in `tests_ha/` runs Climate Advisor inside a **real** Home Assistant core
(HA's own leak checker, Issue #1102). It needs its own environment (group `test-ha`):

```bash
python scripts/test.py ha
```

CI runs both tiers through the same script (`validate-and-test` and `ha-lifecycle` jobs).

### Real-HA tier: the house, markers, revert-proof, snapshots (Step 2, Issue #1127)

`tests_ha/house/` builds one small real home inside real Home Assistant: the simulated thermostat
(`ca_dev_thermostat_sim`, which runs Climate Advisor's own heat-flow math), a real `WeatherEntity` with a
forecast service, contact sensors, a whole-house fan, a notify service, the unit system and a frozen clock.
Use the `make_house` fixture; do not hand-build a coordinator for lifecycle, listener, timer, persistence or
unit-handling behavior.

```python
house = await make_house(units="us", profile="scripted", fan="whf", contacts=["window"], startup="cold")
await house.open("window"); await house.advance(minutes=30)   # 30 s slices, timers fire on time
house.thermostat.mode / .setpoint_f / .indoor_f / .action / .fan_mode
house.calls          # every service call with origin "occupant" | "ca:<n>" (n-th instance) | "other"
await house.occupant_presses_fan(True); await house.occupant_sets_thermostat(mode="heat", setpoint_f=70)
await house.reload_zone(); await house.restart(down_for_seconds=0); await house.close()
```

* `profile`: `closed_loop` (physics runs) or `scripted` (all k values zero; the test sets indoor with `set_indoor`).
* `units`: `us` or `metric` set HA's unit system and Climate Advisor's `temp_unit` together (a mismatch makes HA
  reject every setpoint).
* `startup`: `cold` leaves the 300 s override-suppression window open; `settled` (default) advances past it.
* Lifecycle contract: `close()` unloads Climate Advisor then the sim; HA's autouse `verify_cleanup` is the net.
  `restart()` is unload + set up on the same `hass` and config dir (persistence is exercised, HA boot ordering is
  not).
* Markers (`tests_ha/pytest.ini`, `--strict-markers`, `timeout = 60`): `house`, `goldens`, `snapshot`, `slow`.
* `CA_HA_WORKERS=4 python scripts/test.py ha` runs the tier under xdist (`auto` in CI).

**Revert proof.** A real-HA test for an escaped bug is only worth having if it fails on the code from before the fix:

```bash
python scripts/revert_proof.py 'b686a05^' tests_ha/test_escaped_bugs.py::test_observe_only_does_not_release_blower
python scripts/revert_proof.py '2507a19^' tests_ha/test_escaped_bugs.py::test_manual_fan_survives_restart
python scripts/revert_proof.py 1df0246   tests_ha/test_escaped_bugs.py::test_reloaded_zone_leaves_no_ghost
```

It extracts `custom_components/` at the commit with `git archive` into a scratch directory (`CA_COMPONENT_ROOT`),
requires the tests to fail there and pass on the working tree, and never checks out, resets or stashes anything.
Exit 1 = did not fail pre-fix, 2 = failed on the working tree, 3 = old tree would not import (use
`--reverse-patch <fix-commit>`).

| Bug | Test | Pre-fix commit |
|---|---|---|
| #1108 observe-only releases the person's fan | `test_observe_only_does_not_release_blower` | `b686a05^` |
| #1045 manual fan switched off after restart | `test_manual_fan_survives_restart` | `2507a19^` |
| #1102/#1117 ghost copy after a settings save | `test_reloaded_zone_leaves_no_ghost` | `1df0246` |

**Snapshots (#945).** `tests_ha/test_snapshots.py` compares the real briefing, notifications, Status, Next User
Action, Next Automation and fan cards with `tests_ha/snapshots/test_snapshots.ambr` across reference days (hot,
warm, mild, cool, cold, window open with a fan, away, one Celsius day). CI never passes `--snapshot-update`.
Update deliberately and review the diff as prose:
`python scripts/test.py ha --snapshot-update tests_ha/test_snapshots.py`. Changed a snapshot? Paste the
before/after text in the PR and say why the occupant is better off.

### Test Coverage

```bash
# Run with coverage report
uv run --only-group test pytest tests/ -v --cov=custom_components/climate_advisor

# Generate an HTML coverage report
uv run --only-group test pytest tests/ -v --cov=custom_components/climate_advisor --cov-report=html
# Open htmlcov/index.html in your browser
```

### Test Suite

The project maintains 202 test files under `tests/test_*.py` (verify with `find tests -name 'test_*.py' | wc -l`). Coverage spans: classification logic, thermal learning (v2 and v3 concurrent observations), FSM state machines (nat-vent, door/window, override/grace, fan, economizer, classification, occupancy), automation gate logic, grace/pause/override mechanics, occupancy-aware guards, briefing generation, API views, config flow, and simulation feedback. Shared fixtures and Home Assistant module mocking are centralized in `conftest.py`.

### Writing New Tests

- Add test files as `tests/test_<module>.py`.
- Use fixtures from `conftest.py` (e.g., `basic_forecast`) where applicable.
- For modules that import HA, the conftest mock layer handles it — just import the Climate Advisor module directly.
- If you need a new HA submodule mocked, add it to the `_HA_MODULES` list in `conftest.py`.

## Linting

The project uses [Ruff](https://docs.astral.sh/ruff/) for linting.

```bash
# Check for lint issues
ruff check custom_components/ tests/

# Auto-fix what Ruff can
ruff check custom_components/ tests/ --fix

# Format code
ruff format custom_components/ tests/

# Check formatting without changing files
ruff format custom_components/ tests/ --check
```

## Common Workflows

### Before Submitting Changes

```bash
# 1. Lint
ruff check custom_components/ tests/

# 2. Run tests
python scripts/test.py unit

# 3. Check coverage hasn't dropped
uv run --only-group test pytest tests/ -v --cov=custom_components/climate_advisor

# 4. Review changes
git diff
```

### Adding a New Module

1. Create the module in `custom_components/climate_advisor/`
2. Create a matching test file in `tests/test_<module>.py`
3. If the module imports new HA submodules, add them to `_HA_MODULES` in `conftest.py`
4. Run the full test suite to verify nothing breaks

## Troubleshooting

### `ModuleNotFoundError: homeassistant`

This is expected when running outside HA. The test conftest handles this automatically. If you see this error during tests, make sure you're running from the project root:

```bash
python scripts/test.py unit
```

### Tests pass locally but fail in CI

Check that any new test dependency is in the `test` group of `pyproject.toml` and that `uv.lock` was regenerated (`uv lock`); CI uses `--locked`.
