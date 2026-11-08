# Project AGENT PLAYBOOK

This repository expects any automation agent (Codex CLI, LLM assistants, scripted bots) to follow the rules below. Treat this document as the global source of truth for agent behavior on the Climate Advisor project.

## Mission & Mindset

**Project Overview**: Climate Advisor is a Home Assistant custom integration (`climate_advisor`) for intelligent HVAC management. It uses weather forecasts, occupancy detection, and door/window sensors to automate heating and cooling while learning from household behavior over time. See [README.md](README.md) for complete feature descriptions.

**Agent Mission**: Help the developer build, test, and refine this integration while maintaining modular architecture, Home Assistant conventions, and code quality.

**Core Design Principles**:
- The daily briefing is the primary user interface — it must explain "why" for every action
- Automations should be invisible when working correctly
- The learning engine is the long-term differentiator
- All temperatures are in Fahrenheit
- Follow Home Assistant integration conventions at all times

## Project Structure

```
ClimateAdvisor/
├── custom_components/climate_advisor/   # The integration (~80 Python modules)
│   ├── __init__.py          # Entry point and service registration
│   ├── coordinator.py       # Central coordinator (the "brain")
│   ├── automation.py        # HVAC automation engine (AutomationEngine)
│   ├── classifier.py        # Day classification and trend analysis
│   ├── briefing.py          # Daily briefing generator
│   ├── learning.py          # Adaptive learning and thermal model
│   ├── config_flow.py       # Multi-step setup wizard and options flow
│   ├── const.py             # Tunable constants and VERSION
│   ├── api.py               # REST API views for the dashboard
│   ├── sensor.py, switch.py # Entities
│   ├── *_fsm.py, *_decision.py, *_lifecycle.py   # Extracted state machines and pure decisions
│   ├── frontend/index.html  # Dashboard panel
│   ├── fix_history.jsonl    # Per-issue fix records (written by tools/add_fix_entry.py)
│   └── manifest.json, strings.json, translations/en.json
├── docs/                    # Entry point: 00-PROJECT-INSTRUCTIONS.md (its Anchors table routes to every brief/spec)
├── tests/                   # Stub tier (~400 test files)
├── tests_ha/                # Real Home Assistant tier
├── scripts/test.py          # Single test entry point
├── tools/                   # validate.py, deploy.py, simulate.py + sim_harness/, ha_logs.py, learning_db.py, ...
├── brand/icon.png           # HACS store icon
├── README.md, CHANGELOG.md  # User-facing docs and release history
├── agents.md                # This file — agent guidance
├── claude.md                # Claude-specific workflow guidelines
└── hacs.json, pyproject.toml, uv.lock
```

The full module map is in [docs/02-ARCHITECTURE-REFERENCE.md](docs/02-ARCHITECTURE-REFERENCE.md).

## Development Workflow

### Running and Testing

```bash
python scripts/test.py unit      # stub tier (tests/); fastest: CA_UNIT_SHARDS=4 CA_UNIT_WORKERS=2
python scripts/test.py ha        # real Home Assistant tier (tests_ha/, WSL on Windows); CA_HA_WORKERS=N
python scripts/test.py unit tests/test_classifier.py -v   # extra args go to pytest
ruff check . && ruff format --check .
```

Setup, coverage, the real-HA house fixture and troubleshooting: [docs/DEVELOPMENT.md](docs/DEVELOPMENT.md).

### Deploying to Home Assistant

```bash
# Validate before deploying (syntax, manifest, imports, strings)
python tools/validate.py

# Full deploy (validate → backup → copy → restart → verify)
python tools/deploy.py

# Dry run (validate only, show what would deploy)
python tools/deploy.py --dry-run

# Deploy without restarting HA
python tools/deploy.py --skip-restart

# Roll back to previous version
python tools/deploy.py --rollback
```

See [docs/SSH-SETUP.md](docs/SSH-SETUP.md) for initial SSH configuration.

### Development Roadmap

The original v0.1–v0.4 roadmap is historical; the current version is `VERSION` in `const.py` (0.8.x on `main`, 0.9.x on the `0.9.x` branch), history in `CHANGELOG.md`. Planned work is tracked in GitHub issues and epics.

When implementing features, always check which issue or epic they belong to and ensure prerequisites are met.

## Core Development Principles

### Code Quality
- **TESTING**: Create comprehensive automated tests for all features
  - Unit tests for individual modules (classifier, briefing, learning, automation)
  - Integration tests for coordinator workflows and HA platform interactions
  - Mock HA core APIs (`hass`, weather entities, climate entities) for isolated testing
  - After making code changes, always validate with local tests
  - Provide a brief commit summary with markup to ensure GitHub captures the associated issue
- **HOME ASSISTANT CONVENTIONS**: Follow HA patterns strictly
  - Use `async_setup_entry` / `async_unload_entry` for lifecycle
  - Extend `DataUpdateCoordinator` for data management
  - Use `CoordinatorEntity` for sensor entities
  - Register services via `hass.services.async_register`
  - Use `hass.config_entries` for configuration
- **MODULARITY**: Maintain clear separation of concerns
  - Classifier: weather analysis only
  - Coordinator: orchestration only
  - Automation: HVAC control only
  - Briefing: text generation only
  - Learning: pattern analysis only
- **BUG PROCESS**: Follow systematic bug handling:
  1. Investigate and document the issue
  2. Always offer to create a GitHub issue with analysis — summarize to the user what the issue would contain and ask for confirmation
  3. Implement fix with tests
  4. Validate fix passes all tests
  5. Close issue with commit reference

### Testing Strategy

The suite exists in two tiers: `tests/` (stub tier; Home Assistant is stubbed by `tools/sim_harness/ha_stubs.py`, installed from `tests/conftest.py`) and `tests_ha/` (a real Home Assistant core via `pytest-homeassistant-custom-component`). Test files are named by feature or issue, not one per module. Mocking rules and test patterns: claude.md "Testing Requirements". Setup and tiers: [docs/DEVELOPMENT.md](docs/DEVELOPMENT.md).

**Coverage Goals**:
- Classifier: 100% (pure logic, no HA dependencies)
- Learning: 90%+ (mostly pure logic with JSON storage)
- Briefing: 80%+ (text generation, verify key sections present)
- Automation/Coordinator: 70%+ (heavy HA interaction requires mocking)

### Logging and Diagnostics
- Use `logging.getLogger(__name__)` in every module
- Log at appropriate levels: DEBUG for data flow, INFO for actions taken, WARNING for degraded states, ERROR for failures
- Include context in log messages (day type, trend, temperatures)
- Never log sensitive data (API keys, tokens, personal info)

## Security & Safety Guardrails

- **Credential Protection**: NEVER log or commit sensitive data
  - No HA long-lived access tokens in code or logs
  - No user notification service details in version control
  - Use HA's built-in secrets management (`!secret` in YAML)
  - Alert developer if sensitive data detected
- **Safe Defaults**: Automations must fail safely
  - If forecast unavailable, do not change HVAC state
  - If classification fails, default to "mild" (minimal intervention)
  - Never set temperatures outside safe ranges
  - Always respect user's manual overrides

## Documentation Standards

### Documentation Hierarchy
- **[README.md](README.md)**: User-facing documentation (installation, configuration, usage)
- **[CONTRIBUTING.md](CONTRIBUTING.md)**: How to contribute (branches, tests, goldens, issues, PR checklist)
- **[docs/](docs/)**: Design specifications, reference material, and setup guides
- **[agents.md](agents.md)** (this file): Agent development guidance
- **[claude.md](claude.md)**: Claude-specific workflow and conventions

### Keeping Documentation Current
- When adding features, update:
  1. README.md with user-facing details
  2. This agents.md if workflow changes
  3. Relevant docs/ files for design changes
  4. Code comments for complex logic
- Update version numbers in `custom_components/climate_advisor/manifest.json` when shipping features

## Agent Workflow Guidelines

### When in Doubt
1. Read this document to confirm behavior aligns with guardrails
2. Review the design docs in `docs/` for architectural context
3. Review README.md for user requirements
4. Ask clarifying questions if conflicts arise
5. Never guess when safety or data integrity is at stake

### Collaboration with Developers
- Provide normalized, reusable outputs
- Close the feedback loop:
  1. Investigate issues thoroughly
  2. Scaffold solutions with proper structure
  3. Run and validate results
  4. Capture lessons learned for future automation
- Suggest improvements when patterns emerge
