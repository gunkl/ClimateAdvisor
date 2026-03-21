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
- **Log statements** MUST NOT include credentials, tokens, full SSH commands with key paths, or infrastructure topology details

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

- **`@callback` decorator** — Is a `MagicMock` in the test mock layer and swallows decorated functions. If a test needs to invoke a `@callback`-decorated inner function (e.g., timer callbacks), patch it: `patch("...coordinator.callback", side_effect=lambda fn: fn)`

#### Verification

**All new or modified tests MUST pass with warnings-as-errors before completion:**
```bash
pytest tests/test_<file>.py -W error::pytest.PytestUnraisableExceptionWarning -W error::RuntimeWarning
```

### Project Memory

Claude Code's built-in memory system stores project context, tooling locations, and hard-won facts so they don't have to be re-discovered every session. Claude reads memory automatically at session start.

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

**Example:**
```
"I want to add energy cost tracking to the learning engine.
What modules need changes and what's the best approach?"
```

### 2. Implementation Phase

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
