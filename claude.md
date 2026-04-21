# Working with Claude on Climate Advisor

This document provides guidelines for using Claude (or other AI assistants) effectively on the Climate Advisor project.

## Philosophy

Claude is a development assistant that helps with:
- Writing code and tests
- Finding bugs and suggesting fixes
- Writing documentation
- Exploring the codebase
- Answering technical questions

**Important**: The human developer remains responsible for all code review, commits, and architectural decisions.

### Goal-Oriented Comfort Model

**Comfort zone temperatures are proxies, not goals.** The goal is that users are comfortable.
When a user deviates from the recommended schedule (e.g., leaves windows open overnight),
treat it as revealed preference — not a failure. Violations should only count when the system
had control and failed to maintain comfort.

Issue #74 (Metrics Rethink) captures the full design analysis for this — do not re-plan
what is already documented there. Key pending decisions:
- `comfort_violations_minutes` should only accumulate when the system had control and failed
- `windows_opened` compliance should track outcome quality (energy + temp), not just schedule adherence
- Repeated overrides in same context = learned preference; single overrides = note but don't learn yet

## Skills

The following custom skills are available to enhance your workflow:

- **[end-of-session](.claude/skills/end-of-session.md)** — Review conversation and suggest additions to CLAUDE.md files that will improve future sessions. Use this when wrapping up work for the day to capture lessons learned and preferences.
- **[simulate](.claude/skills/simulate.md)** — Run behavior simulations against stored scenarios to validate automation logic and catch regressions. Use before and after any change to `automation.py`.

### Simulation Skill

Before any change to `automation.py` automation logic, run `python3 tools/simulate.py`
to confirm all golden scenarios still pass. After implementing a behavior change:
1. Run `python3 tools/simulate.py --pending -v` — new scenarios should pass
2. After the user validates in production: `mv tools/simulations/pending/<name>.json tools/simulations/golden/<name>.json`

Every closed automation-behavior issue should have a corresponding golden scenario.
Scenarios flow: `pending/` → review → `golden/` or `pending-fix/` or `unsupported/`.
Never delete `unsupported/` scenarios — they document deliberate scope decisions.

## Critical Project Decisions

### Modular Architecture (DO NOT CHANGE)

**Decision**: Climate Advisor uses strict separation of concerns across its modules.

**NEVER merge module responsibilities** (e.g., putting classification logic in the coordinator, or automation logic in the briefing generator) unless you have explicit approval from the project owner.

**Why modular?**
- Each module is independently testable
- Clear ownership of concerns (classifier classifies, automation automates, briefing generates text)
- Easier to extend (e.g., adding multi-zone in v0.3 affects automation, not briefing)
- Follows Home Assistant integration best practices

### Daily Briefing as Primary UI

The briefing is the main way users interact with Climate Advisor. When making changes:
- Always consider how they affect the briefing output
- Explain "why" for every automated action, not just "what"
- Keep briefings scannable in 30 seconds
- Use friendly language, not technical jargon

### Home Assistant Boundary Rule (CRITICAL)

**Decision**: Climate Advisor MUST NOT modify, write to, or interact with anything in Home Assistant outside the integration's own scope.

**Allowed scope:**
- Files inside `custom_components/climate_advisor/` (the integration itself)
- HA service calls to `climate` domain (set_hvac_mode, set_temperature) — these are the integration's core purpose
- HA service calls to `notify` domain — user-configured notification service
- One data file: `climate_advisor_learning.json` in the HA config root (learning engine state)

**Everything else is OUT OF SCOPE.** This includes but is not limited to:
- Modifying other integrations or their config
- Writing to `configuration.yaml`, `automations.yaml`, `scripts.yaml`, or any other HA config file
- Calling HA services outside `climate` and `notify` (e.g., `homeassistant.restart`, `input_boolean.turn_on`)
- Creating files outside the integration directory (except the learning DB above)
- Modifying HA add-on configurations
- Deploying files outside `/config/custom_components/climate_advisor/` on the remote server

**Violation protocol:**
1. If a proposed change would touch anything outside the allowed scope, **STOP and flag it as a BOUNDARY VIOLATION** before writing any code
2. Explain exactly what the out-of-scope interaction is and why it's being proposed
3. Ask the user whether to grant an exception
4. If the user approves, log the exception in `docs/HA-BOUNDARY-EXCEPTIONS.md` with: date, what was allowed, why, and a risk note
5. Exceptions are temporary — each one should include a resolution plan and be periodically reviewed

**Why this matters:**
- A broken custom integration should never take down Home Assistant
- Users trust that installing Climate Advisor won't modify their existing setup
- Out-of-scope writes can cause data loss, config corruption, or security issues
- This rule makes the integration safe to install, update, and uninstall cleanly

### Security Requirements (CRITICAL)

**Decision**: All code written for Climate Advisor MUST follow these security rules. They apply to the integration, deployment tools, and tests.

#### Input Validation
- **All HA service registrations** MUST include a `vol.Schema` — no unvalidated service parameters
- **All config flow text fields** MUST validate format before accepting (regex or `cv.*` validator)
- **Cross-field validation** is required when fields have logical relationships (e.g., setback < comfort)
- **Use HA's built-in validators** (`vol.*`, `cv.*` from `homeassistant.helpers.config_validation`) — do NOT write custom validation when an HA utility exists
- **Entity selectors** handle entity ID validation at the UI layer — but always validate existence at runtime if the entity is critical

#### Shell Command Safety
- **NEVER** interpolate variables directly into shell command strings — always use `shlex.quote()` for user-supplied or config-derived values
- **NEVER** use `StrictHostKeyChecking=no` in SSH commands — use `accept-new` at minimum
- **All deployment config values** must be validated before use (hostname format, port range, path safety)

#### Data Exposure
- **Sensor entity attributes** MUST NOT expose raw user behavior data (suggestions, overrides, patterns) — expose counts/summaries only
- **API responses** MUST redact values that could reveal personal information (notify service names, entity IDs that contain names)
- **Config API endpoint** (`ClimateAdvisorConfigView`) MUST check `meta.get("sensitive")` on CONFIG_METADATA entries and replace the value with `"configured"/"not set"` — never send raw sensitive values in REST responses
- **Log statements** MUST NOT include credentials, tokens, full SSH commands with key paths, or infrastructure topology details
- **NEVER log any portion of API keys or tokens** — not even prefixes or partial strings. Log only whether a key is present (e.g., "key configured", "key updated")

#### File & Data Safety
- **JSON files read from disk** MUST be type-validated (`isinstance(data, dict)`) before accessing fields — never assume structure
- **Persisted data files** (learning DB, state) should have restrictive permissions (`0o600`) on non-Windows platforms
- **Lists that grow over time** (history, dismissed items) MUST have a cap to prevent unbounded memory/disk growth
- **Atomic writes** (write to .tmp, then `os.replace`) are required for state files to prevent corruption on crash

#### Secrets & Credentials
- **NEVER** hardcode passwords, API keys, tokens, or secrets in source code
- **`tools/validate.py`** runs a secrets scan before deployment — keep its patterns up to date when new secret types are introduced
- **`.gitignore`** must exclude all secret/credential files — verify before adding new config files

**Violation protocol**: Same as HA Boundary Rule — stop, flag, and ask before proceeding if a proposed change would violate any of these rules.

### Testing Requirements (CRITICAL)

**Decision**: All tests in this project MUST handle Python's async mock infrastructure correctly. Unawaited coroutine warnings are test bugs, not cosmetic noise.

**Why this matters:** Python 3.12+ aggressively warns when `AsyncMock` coroutines are garbage-collected without being awaited. These warnings only appear when running the full test suite (cross-test GC timing), not in isolation — making them easy to miss during development and painful to debug after the fact.

#### Mock HA Async Methods Correctly

- **`hass.async_create_task`** — NEVER use bare `MagicMock()`. Always close coroutines:
  ```python
  def _consume_coroutine(coro):
      coro.close()
  hass.async_create_task = MagicMock(side_effect=_consume_coroutine)
  ```
  Reference implementations: `test_door_window.py:255`, `test_resume_from_pause.py:44`

- **`coordinator._async_save_state`** — Stub as `AsyncMock()` directly on the coordinator instance, not by mocking `hass.async_add_executor_job`. Reference: `test_coordinator.py:262`

- **`coordinator.automation_engine`** — Use `MagicMock()` (NOT `AsyncMock()`) for the engine object. `AsyncMock` causes sync methods like `get_serializable_state()` to return coroutines instead of values. Only set individual async handler methods to `AsyncMock()` in tests that `await` them.

- **`automation_engine` boolean flags must be explicitly set to `False`** in any stub that creates a MagicMock engine:
  ```python
  ae._natural_vent_active = False
  ae._fan_override_active = False
  ```
  Unset `MagicMock` attributes are truthy by default. If `_natural_vent_active` is left unset, the `_is_expected_fan` suppression guard silently passes for all scenarios — causing tests that expect `state_contradiction_warning` to fail only in the full suite, not in isolation. Reference: `test_hvac_session_detection.py` (`_make_update_data_coord`).

- **`@callback` decorator** — Is a `MagicMock` in the test mock layer and swallows decorated functions. If a test needs to invoke a `@callback`-decorated inner function (e.g., timer callbacks), patch it: `patch("...coordinator.callback", side_effect=lambda fn: fn)`

#### Testing Sensor Attributes and API Views

**Sensor entity classes (`ClimateAdvisorBaseSensor` subclasses) CANNOT be directly instantiated in test files that use the lightweight HA module stubs** (e.g., `test_fan_control.py`, `test_contact_status.py`). Importing `sensor.py` in that environment causes a metaclass conflict:
```
TypeError: metaclass conflict: the metaclass of a derived class must be a (non-strict) subclass of the metaclasses of all its bases
```

**Pattern to follow**: Replicate the `extra_state_attributes` logic as a plain helper function in the test file and test that directly. Reference implementations: `test_fan_control.py` (`_fan_sensor_extra_state_attributes` / `TestFanSensorAttributes`), `test_contact_status.py` (`_compute_contact_status`).

**`HomeAssistantView` subclasses (API views) have the same metaclass constraint.** Do NOT try to instantiate `ClimateAdvisorRespondSuggestionView` or similar view classes in tests. Instead, replicate the validation and dispatch logic as a plain `_simulate_post()` helper function in the test file and test that directly. Reference implementation: `test_api_respond_suggestion.py`.

#### Coordinator Class After test_occupancy.py Module Deletion

**`test_occupancy.py` deletes `custom_components.climate_advisor.coordinator` from `sys.modules` and re-imports it.** If another test file holds a module-level reference to `ClimateAdvisorCoordinator` (e.g., `coord = ClimateAdvisorCoordinator`), any methods bound via `types.MethodType` will have stale `__globals__` pointing to the old module. Patches applied to the new module (e.g., `patch("...coordinator.async_call_later")`) won't be visible to those bound methods, causing `call_count == 0` failures that only appear in the full suite.

**Pattern**: Use a `_get_coordinator_class()` helper that calls `importlib.import_module()` each time rather than holding a module-level reference:
```python
def _get_coordinator_class():
    mod = importlib.import_module("custom_components.climate_advisor.coordinator")
    return mod.ClimateAdvisorCoordinator
```
Call this inside every stub factory function before `object.__new__()` and `types.MethodType()`. Reference implementation: `tests/test_daily_record_accuracy.py`.

#### Module-Level sys.modules Mocking

When a test file injects mock packages via `sys.modules` (e.g., `test_claude_api.py` mocks `anthropic`), the mock only takes effect if it runs BEFORE any other test imports the target module. In the full suite, import order is controlled by conftest/other test files, not the mocking test. If the target module has a try/except ImportError guard (like `ANTHROPIC_AVAILABLE`), the guard resolves at first import time.

**Pattern**: In tests that depend on module-level mocking, also patch the module-level variables (e.g., `_mod.ANTHROPIC_AVAILABLE`, `_mod.RateLimitError`) directly on the already-imported module, rather than relying on `sys.modules` ordering.

#### Python 3.14 Async Tests

Python 3.14 removed the implicit event loop. Use `asyncio.run()` instead of
`asyncio.get_event_loop().run_until_complete()` in all tests. The latter raises
`RuntimeError: There is no current event loop in thread 'MainThread'`.

#### Verification

**All new or modified tests MUST pass with warnings-as-errors before completion:**
```bash
pytest tests/test_<file>.py -W error::pytest.PytestUnraisableExceptionWarning -W error::RuntimeWarning
```

### Golden Simulation Test Policy

Golden scenarios in `tools/simulations/golden/` are **LOCKED**. The AI MUST NOT modify these files without explicit user instruction. When the user approves a modification:
1. Show the full scenario in human-readable form (description, each event + intended outcome, each assertion + rationale)
2. Ask the user to confirm the scenario accurately represents real HVAC behavior
3. Run `python tools/simulate.py --sign <scenario-name>` to update `MANIFEST.json`
4. Run `python tools/simulate.py --check-integrity` to confirm clean
5. Run `python tools/simulate.py` to confirm all golden tests still pass

**AI review checklist** before any golden test is written or modified:
- Does the scenario represent a realistic HVAC operating condition?
- Is comfort maintained appropriately for the described conditions?
- If `aggressive_savings=true`, does behavior correctly prioritize savings over comfort?
- Are the assertions grounded in documented automation logic (`docs/08-COMPUTATION-REFERENCE.md`)?
- Could a reasonable HVAC technician confirm this outcome as correct?
- Are all assertion `reason` fields specific enough to explain the HVAC logic, not just restate the outcome?

**Never promote a scenario from `pending/` to `golden/` without running it first and having the user review the scenario card output.**

### Pre-commit Hooks

This project uses `pre-commit` with ruff, ruff-format, check-yaml, check-json, end-of-file-fixer, and trailing-whitespace hooks. The ruff hook runs with `--fix`, which can modify files during commit.

**After running `ruff check --fix` or `ruff format`**, always re-stage modified files before committing. Otherwise pre-commit will fail because the staged content differs from the working copy.

**Before running pytest**, lint all modified files with the same settings as the pre-commit hook:
```bash
python -m ruff check --fix custom_components/climate_advisor/ tests/
python -m ruff format custom_components/climate_advisor/ tests/
python -m ruff check custom_components/climate_advisor/ tests/  # confirm clean
```
Re-stage any files ruff modified, then run tests. Never run pytest on unlinted code — long f-strings and `format_temp` calls frequently exceed the 120-character limit and will fail at commit instead.

A `tools/validate.py` hook also runs on `custom_components/climate_advisor/` files.

### CONFIG_METADATA Category Sync

When adding a new `category` value to CONFIG_METADATA in `const.py`, also update:
- `VALID_CATEGORIES` set in `tests/test_config_metadata.py`
- The `expected` list in `tests/test_config_flow.py::TestOptionsFlowMenu::test_menu_options_list`
- `OPTIONS_MENU_OPTIONS` in `config_flow.py` if the category needs its own menu step

Skipping any of these will produce silent `assert cat in VALID_CATEGORIES` failures.

### Automation Logic Table (CRITICAL)

**Decision**: The automation logic table in `docs/08-COMPUTATION-REFERENCE.md` is the definitive reference for expected behavior across all classification contexts and events.

**Rules:**
- This table MUST be updated whenever automation behavior changes (door/window handling, grace periods, classification application, occupancy, fan mode)
- Every cell in the table must have corresponding test coverage
- Test references are mapped in the table itself
- Before modifying automation logic, check the table for affected cells and ensure tests exist

**Key test files:**
- `tests/test_windows_recommended_integration.py` — windows-recommended + door/window interaction
- `tests/test_door_window.py` — pause/resume/grace mechanics
- `tests/test_resume_from_pause.py` — resume behavior and grace recheck
- `tests/test_occupancy_automation.py` — occupancy-aware automation guards (Issue #85)

#### Occupancy-Aware Guards (Issue #85)

The automation engine tracks `_occupancy_mode` internally and all temperature-setting
code paths must respect it. When adding new code that calls `_set_temperature_for_mode()`
or sets comfort temps, verify it works correctly when occupancy is away/vacation — the
safety net in `_set_temperature_for_mode()` redirects to setback handlers, but direct
calls to `_set_temperature()` bypass it. See §6a in `docs/08-COMPUTATION-REFERENCE.md`.

### Warm-Day Classification Behavior

**Decision**: On a warm day (forecast high ≥ 75°F), the classifier sets `hvac_mode = "off"` in the `DayClassification`. The automation engine applies a comfort-floor guard before executing the shutoff.

**Guard logic** (`automation.py` `apply_classification()`):
- If `current_indoor_temp < comfort_heat`: heat to `comfort_heat` first; emit `warm_day_comfort_gap` event; do NOT set HVAC off yet
- If `current_indoor_temp >= comfort_heat` (or temp unavailable): set HVAC off as normal
- `apply_classification()` is called every 30 min — once indoor reaches the comfort floor, the guard stops firing and HVAC goes off naturally

**Why**: Without this guard, warm-day shutoffs can leave the home 2–3°F below the comfort floor all morning, accumulating comfort violations and driving `comfort_score` toward 0.

**Compliance display in investigator**: `window_rec=` in daily records shows `opened/not-opened/n/a` based on whether windows were recommended and whether they were physically opened. A `?` value is always wrong — it means a field named `window_compliance` was read, which does not exist in `DailyRecord`.

**Comfort band definition**: `comfort_heat` is the **lower** bound; `comfort_cool` is the **upper** bound. A temperature T is in-band iff `comfort_heat ≤ T ≤ comfort_cool`. The AI context now labels these fields explicitly and includes a NUMERIC VERIFICATION RULE to prevent incorrect "within range" claims.

### Fan Status Values

`_compute_fan_status()` returns one of four string values. All four must be accounted for in cross-validation suppression logic in `ai_skills_activity.py`:

| Value | Meaning |
|---|---|
| `"active"` | CA commanded the fan on (natural ventilation or HVAC fan-only mode) |
| `"running (manual override)"` | Fan is running; CA's `_fan_override_active` flag is set |
| `"running (untracked)"` | Thermostat reports fan running (`fan_mode=on` or `hvac_action=fan`) but CA's `_fan_active=False` — typical after HA restart or when user ran fan from thermostat app |
| `"inactive"` | Fan is off and CA has no record of activating it |

`"running (untracked)"` was added in Issue #91. Any code that checks `ca_fan_running` for suppression purposes must include all three non-inactive values.

### Project Memory

Claude Code's built-in memory system stores project context, tooling locations, and hard-won facts so they don't have to be re-discovered every session. Claude reads memory automatically at session start.

**Thermal learning — v2 physics model (Issue #114)**: The thermal model uses two parameters: `k_passive` (hr⁻¹, always negative — envelope decay rate) and `k_active_heat`/`k_active_cool` (°F/hr — HVAC contribution). Parameters are extracted from the full post-heat decay curve via OLS regression, not from a single start/end delta. This fixes the root cause of the old model's zero-rate observations (thermostat sensor lag). The coordinator runs a `PendingThermalEvent` state machine (`active → post_heat → stabilized/abandoned`) tracked by 7 methods (`_start_thermal_event`, `_sample_thermal_event`, `_end_active_phase`, `_check_stabilization`, `_commit_thermal_event`, `_abandon_thermal_event`, `_update_pre_heat_buffer`). The pending event is persisted in `LearningState.pending_thermal_event` so post-heat observation windows survive HA restarts. If an observation is abandoned, a WARNING is logged with the reason. `get_thermal_model()` returns the v2 fields (`k_passive`, `k_active_heat`, `k_active_cool`) plus legacy aliases (`heating_rate_f_per_hour`, `cooling_rate_f_per_hour`) for backward compatibility with `compute_bedtime_setback()`. Physics-based prediction activates when `confidence != "none"` and `k_passive < 0`; falls back to ramp interpolation otherwise.

---

## Git Workflow

### Commits

**NEVER** have Claude make git commits directly. The human developer should:

1. Review all changes made by Claude
2. Test the changes
3. Write commit messages themselves
4. Execute git commands manually

**NEVER** add co-authoring statements like:
```
Co-Authored-By: Claude <noreply@anthropic.com>
```

The git history should reflect human authorship only.

**Override**: If the user explicitly insists after receiving the standard reminder, proceed with the commit. Do not repeat the objection a second time.

### Recommended Workflow

```bash
# 1. Claude writes code/fixes
# (AI makes changes to files)

# 2. Human reviews changes
git diff

# 3. Human tests changes
pytest tests/ -v

# 4. Human commits
git add .
git commit -m "Your commit message here"
git push
```

### Commit Messages

Ask Claude for commit message suggestions, but write them yourself:

**Good Practice:**
```
You: "suggest a commit message for these changes"
Claude: "Suggests: 'Add window compliance pattern to learning engine'"
You: git commit -m "Add window compliance pattern to learning engine"
```

**Bad Practice:**
```
Claude executes: git commit -m "..."
```

## Code Review Responsibilities

### Human Reviews
- Architectural decisions
- Security implications
- Home Assistant API usage correctness
- Test coverage adequacy
- Briefing output quality and tone

### Claude Can Help With
- Identifying potential bugs
- Suggesting test cases
- Finding edge cases in classification logic
- Code formatting and HA convention compliance
- Documentation completeness

## GitHub Issues

### IMPORTANT: Always Create Issues

**Claude MUST create GitHub issues for all new features and bug fixes** before or immediately after implementation:

```bash
# Create issue for new feature
gh issue create --title "Feature: Description" --body "..."

# Create issue for bug fix
gh issue create --title "Fix: Description" --body "..."
```

### Issue Requirements
- Every feature/fix should have a tracking issue
- Include summary, requirements, implementation checklist
- Update issue status when work is complete — provide user with brief commit summary including GitHub issue reference
- Reference related issues when applicable

### Example Workflow
```
1. User requests feature
2. Claude creates GitHub issue with requirements
3. Claude implements feature
4. Claude updates issue with completion status
5. User reviews and commits
```

## Effective Prompting

### Good Prompts

**Specific and focused:**
```
"Add validation to classify_day() to handle missing forecast data
by defaulting to 'mild' day type"
```

**With context:**
```
"The briefing is showing wrong window times for warm days.
Check briefing.py _warm_day_plan() — the open/close
times don't match the classifier recommendations."
```

**Incremental:**
```
"First, add the new suggestion pattern to the learning engine"
"Now update the briefing to display the new suggestion type"
```

### Less Effective Prompts

**Too vague:**
```
"Make it better"
"Fix the bugs"
```

**Too broad:**
```
"Rewrite the entire integration"
"Implement all v0.2 features at once"
```

## Development Workflow

### 1. Planning Phase

**Ask Claude to:**
- Analyze requirements against the design docs in `docs/`
- Suggest implementation approaches
- Identify which modules are affected
- Create task breakdowns

**Before finalising a plan**, trace the full user scenario end-to-end (e.g., a European user with Celsius configured in HA). Check every boundary where data crosses between the integration and HA — both inbound (reading from HA) and outbound (writing back to HA services). Unit-level thinking misses service call boundaries where wrong-unit or wrong-format data causes silent misbehaviour in production.

**Example:**
```
"I want to add energy cost tracking to the learning engine.
What modules need changes and what's the best approach?"
```

### 2. Implementation Phase

#### Execution Pattern for Multi-file Changes

For plans touching 4+ files, structure execution as:
- **Parallel Sonnet executor agents** — one per independent phase or file group, run in a single message with multiple Agent tool calls
- **Coordinator** — the main Claude instance sequences phases, handles dependencies, and passes results between agents
- **Verification agent (Opus 4.6)** — runs `ruff check` then the full test suite after each phase; reports failures back to the coordinator for fixing before the next phase begins

Phases that modify different files with no shared dependencies can run in parallel. Phases with dependencies must be sequential.

**The plan file MUST include an "Execution Structure" section before `ExitPlanMode` is called.** This section defines: which agents run in parallel (grouped by file independence), which files each agent owns, and what verification steps run between groups. Without this section, the parallel execution pattern cannot be validated — the user will push back. Use the table format: `Agent | Phases | Files` with a `→ Verification agent` row between groups.

After the final test phase passes, always run an Opus 4.6 verification agent to review all modified files for correctness, completeness, security, and logging coverage before handing off to the user. Do not skip this step or wait to be asked.

**Ask Claude to:**
- Write implementations following HA conventions
- Create test files
- Add documentation
- Suggest error handling

**Example:**
```
"Add a new suggestion pattern to learning.py that detects
when the user consistently overrides the bedtime setback
temperature. Include the pattern analysis and suggestion text."
```

### 3. Testing Phase

**Ask Claude to:**
- Write test cases for the module
- Suggest edge cases (threshold boundaries, missing data, etc.)
- Create mock HA objects for isolated testing
- Fix failing tests

**Example:**
```
"The test_classifier.py test is failing on trend computation
when tomorrow's high equals today's high. Help me debug it."
```

### 4. Documentation Phase

**Ask Claude to:**
- Update README.md with new features
- Update docs/ files if architecture changed
- Add code comments for complex logic
- Create briefing examples for new scenarios

**Example:**
```
"Update docs/04-BRIEFING-EXAMPLES.md with an example showing
the new energy cost suggestion in the learning section"
```

## Project-Specific Guidelines

### Climate Advisor Architecture

When asking Claude for changes, reference these principles:

1. **Coordinator Pattern**: All orchestration through `ClimateAdvisorCoordinator` extending `DataUpdateCoordinator`
2. **Classification First**: Weather data → classification → all downstream decisions
3. **Event-Driven**: Time-change listeners for scheduled events, state listeners for reactive events
4. **Learning Loop**: Recommend → Track → Analyze → Suggest → User responds → Adapt
5. **Graceful Degradation**: If data is missing, fall back safely — never leave HVAC in a bad state
6. **Config Flow**: All user settings through HA's config flow, never hardcoded

### Code Style

Claude should follow existing conventions:
- Function/variable names: `snake_case`
- Class names: `PascalCase`
- Constants: `UPPER_SNAKE_CASE`
- Async methods: prefix with `async_` (HA convention)
- Private methods: prefix with `_`
- Indentation: 4 spaces
- Type hints on all function signatures
- Docstrings on public methods

#### Occupancy Constants

Use `OCCUPANCY_HOME`, `OCCUPANCY_AWAY`, `OCCUPANCY_VACATION`, `OCCUPANCY_GUEST` from
`const.py` instead of string literals `"home"`, `"away"`, etc. in automation logic.

#### CONFIG_METADATA Keys

`CONFIG_METADATA` in `const.py` is a large dict literal. Use **string literal keys** (e.g., `"ai_enabled"`) rather than `CONF_*` variable references when the variable is defined after the dict in the file. Python dict literals require keys to be defined before the dict is evaluated.

#### Dashboard Frontend (index.html)

The dashboard is a single-file HTML/CSS/JS panel. When adding new status grid cards:
- Use `<div class="label">` and `<div class="value">` (block elements) inside `.status-item` containers — never `<span>` (inline), which causes labels and values to render side-by-side
- Match the existing pattern in the Status tab's `loadStatus()` function
- For clipboard operations, always include a `document.execCommand('copy')` fallback via temporary textarea — `navigator.clipboard` is unavailable in HA iframe panels without secure context
- Use `escapeHtml()` for any user-provided or API-returned content rendered as innerHTML

#### API-Frontend Contract

When adding new REST API endpoints or modifying response shapes, verify that the frontend JavaScript reads the exact same key names. Search `index.html` for the endpoint path and check every property access. Common failure: backend returns `monthly_cost_estimate` but frontend reads `monthly_cost`.

### Home Assistant Patterns

Always use these HA patterns:
```python
# Entity setup
async def async_setup_entry(hass, entry, async_add_entities):

# Coordinator data fetch
async def _async_update_data(self):

# Service registration
hass.services.async_register(DOMAIN, SERVICE_NAME, handler)

# Event scheduling
async_track_time_change(hass, callback, hour=6, minute=0, second=0)

# State access
hass.states.get("weather.forecast_home")

# Service calls
await hass.services.async_call("climate", "set_temperature", {...})

# Notifications
await hass.services.async_call("notify", service, {"message": "..."})
```

#### coordinator.config vs coordinator.config_entry

`ClimateAdvisorCoordinator` never passes `config_entry` to its parent class, so
`coordinator.config_entry` is always `None`. Use `coordinator.config` (the runtime dict
set in `__init__` from `dict(entry.data)`) for all config reads. This affects any code
that builds context from the coordinator — `api.py`, `ai_skills_activity.py`, briefing
helpers, etc. Never access `coordinator.config_entry.options`.

#### Adding new data to the coordinator

New runtime values (thermostat attributes, computed state) belong in the
`_async_update_data()` return dict in `coordinator.py`. Secondary consumers (`api.py`,
`ai_skills_activity.py`) should read from `coordinator.data`, not call
`hass.states.get()` themselves. This keeps the single-update-cycle guarantee and avoids
stale reads.

### Reference Documentation

When working on HA-specific patterns, review the official docs:
- Home Assistant developer docs: https://developers.home-assistant.io/docs/apps/
- Config flow: https://developers.home-assistant.io/docs/config_entries_config_flow_handler
- Data update coordinator: https://developers.home-assistant.io/docs/integration_fetching_data

## What to Share with Claude

### Safe to Share
- Error messages and tracebacks
- HA log output
- Test results
- Code snippets
- Configuration (without secrets)
- Design documents

### Be Careful With
- HA long-lived access tokens (remove before sharing)
- Notification service names that reveal personal info
- Home network details

### Never Share
- Passwords or API keys
- Personal location data
- Private user data

## Limitations of AI Assistance

### Claude Cannot:
- Test against a running Home Assistant instance
- Make architectural decisions alone
- Understand your specific household without context
- Replace human code review
- Debug HA platform-specific issues without logs
- Make judgment calls on comfort preferences

### Claude Can:
- Write boilerplate code quickly
- Suggest alternative approaches
- Find common bugs in Python/HA code
- Create comprehensive tests
- Write documentation and briefing text
- Explain HA conventions and patterns
- Refactor code for clarity

## Troubleshooting Claude Interactions

### Claude Makes Incorrect Assumptions

**Issue**: Claude suggests code that doesn't follow HA conventions

**Solution**: Provide more context
```
"We use DataUpdateCoordinator for all data fetching.
Don't poll entities directly — use the coordinator's data dict."
```

### Claude Generates Too Much Code

**Issue**: Response is overwhelming

**Solution**: Break into smaller tasks
```
"Just add the new data class to classifier.py first,
we'll update the coordinator to use it next."
```

### Claude's Code Doesn't Work with HA

**Issue**: Integration fails to load

**Solution**: Share the full HA log
```
"The integration failed to load with this error: [paste error]
The issue is in __init__.py async_setup_entry()"
```

## Version Control Integration

### Branching Strategy

Create branches for major features:

```bash
# Human creates branch
git checkout -b feature/energy-cost-tracking

# Claude helps implement
"Add energy cost tracking to the learning engine..."

# Human tests and commits
git add .
git commit -m "Add energy cost tracking to learning engine"

# Human merges when ready
git checkout main
git merge feature/energy-cost-tracking
```

### Pull Requests

When creating PRs:

1. Human creates PR with description
2. Human reviews all changes in diff
3. Claude can help write PR description
4. Human approves and merges

**Don't** let Claude create or merge PRs automatically.

## Best Practices

### Do:
- Review all AI-generated code
- Test thoroughly before committing
- Ask for explanations when unclear
- Iterate and refine suggestions
- Use Claude for documentation
- Leverage Claude for test creation
- Keep conversations focused

### Don't:
- Blindly accept all suggestions
- Let Claude make git commits
- Skip testing AI-generated code
- Use overly complex AI suggestions
- Ignore security implications
- Let AI make architectural decisions alone

---

**Last Updated**: 2026-03-18
**For**: Climate Advisor — Home Assistant Integration
**AI**: Claude Opus 4.6
