<!-- Nav: ← [Project Instructions](00-PROJECT-INSTRUCTIONS.md) → [SSH Setup](SSH-SETUP.md) -->

# Development Guide

How to set up your local environment, run tests, and lint the Climate Advisor codebase.

## Anchors
| Question | Short answer | → Full answer |
|---|---|---|
| How do I set up the local dev environment without a running Home Assistant? | Clone the repo, create a venv, `pip install -r requirements_test.txt`. HA is not required — `conftest.py` mocks all HA modules so Climate Advisor modules import standalone. | [§Setup](DEVELOPMENT.md#setup) |
| How do I run the full test suite and check coverage? | `pytest tests/ -v` for all tests; add `--cov=custom_components/climate_advisor` for coverage; `--cov-report=html` for an HTML report in `htmlcov/`. | [§Running Tests](DEVELOPMENT.md#running-tests) |
| What linting commands must pass before submitting a change? | `ruff check custom_components/ tests/` (check), `ruff check --fix` (auto-fix), `ruff format` (format). All three should be clean — the pre-commit hook enforces the same settings. | [§Linting](DEVELOPMENT.md#linting) |
| What is the correct sequence for adding a new module? | Create the module in `custom_components/climate_advisor/`, create a matching `tests/test_<module>.py`, add any new HA submodule imports to `_HA_MODULES` in `conftest.py`, then run the full suite. | [§Adding a New Module](DEVELOPMENT.md#adding-a-new-module) |

## Prerequisites

- Python 3.11+ (match your Home Assistant version)
- Git

## Setup

1. Clone the repo:

   ```bash
   git clone https://github.com/gunkl/ClimateAdvisor.git
   cd ClimateAdvisor
   ```

2. Create and activate a virtual environment (recommended):

   ```bash
   python -m venv .venv

   # Linux / macOS
   source .venv/bin/activate

   # Windows (PowerShell)
   .venv\Scripts\Activate.ps1

   # Windows (Git Bash)
   source .venv/Scripts/activate
   ```

3. Install test dependencies:

   ```bash
   pip install -r requirements_test.txt
   ```

   This installs `pytest`, `pytest-cov`, and `ruff`.

## Running Tests

Tests live in the `tests/` directory. Home Assistant is **not** required — the test conftest mocks all HA modules so Climate Advisor modules can be imported standalone.

```bash
# Run all tests with verbose output
pytest tests/ -v

# Run a specific test file
pytest tests/test_classifier.py -v

# Run a specific test by name
pytest tests/ -v -k "test_classify_hot_day"
```

### Test Coverage

```bash
# Run with coverage report
pytest tests/ -v --cov=custom_components/climate_advisor

# Generate an HTML coverage report
pytest tests/ -v --cov=custom_components/climate_advisor --cov-report=html
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
pytest tests/ -v

# 3. Check coverage hasn't dropped
pytest tests/ -v --cov=custom_components/climate_advisor

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
pytest tests/ -v
```

### Tests pass locally but fail in CI

Check that `requirements_test.txt` is up to date with any new test dependencies you've added.
