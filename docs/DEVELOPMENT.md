# Development Guide

How to set up your local environment, run tests, and lint the Climate Advisor codebase.

## Prerequisites

- Python 3.11+ (match your Home Assistant version)
- Git

## Setup

1. Clone the repo:

   ```bash
   git clone https://github.com/gunkl/SmartHVAC.git
   cd SmartHVAC
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

### Current Test Files

| File | What it covers |
|---|---|
| `test_classifier.py` | Day type classification, trend analysis (pure logic, no mocks) |
| `test_const.py` | Internal consistency of constants and thresholds |
| `test_door_window.py` | Door/window sensor group resolution and polarity logic |
| `conftest.py` | Shared fixtures and HA module mocking |

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
