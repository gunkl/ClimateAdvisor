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
SmartHVAC/
├── custom_components/
│   └── climate_advisor/
│       ├── __init__.py          # Integration entry point and service registration
│       ├── automation.py        # HVAC automation engine
│       ├── briefing.py          # Daily briefing generator
│       ├── classifier.py        # Day classification and trend analysis
│       ├── config_flow.py       # 3-step setup wizard and options flow
│       ├── const.py             # All tunable constants
│       ├── coordinator.py       # Central coordinator (the "brain")
│       ├── learning.py          # Adaptive learning engine
│       ├── sensor.py            # 6 sensor entity definitions
│       ├── manifest.json        # Integration metadata
│       └── strings.json         # UI translation strings
├── docs/
│   ├── 00-PROJECT-INSTRUCTIONS.md   # Foundational project briefing
│   ├── 01-STRATEGY-AND-DESIGN.md    # System strategy and 5-layer approach
│   ├── 02-ARCHITECTURE-REFERENCE.md # Technical blueprint and data flow
│   ├── 03-HA-AUTOMATION-UI-GUIDE.md # Manual HA automation fallback guide
│   ├── 04-BRIEFING-EXAMPLES.md      # Example daily briefings by scenario
│   ├── 05-LEARNING-ENGINE-DESIGN.md # Learning engine specification
│   └── SSH-SETUP.md                 # SSH deployment setup guide
├── tools/
│   ├── validate.py              # Pre-deploy validation script
│   └── deploy.py                # Deployment script (Python)
├── README.md                    # User-facing documentation
├── agents.md                    # This file — agent guidance
├── claude.md                    # Claude-specific workflow guidelines
├── hacs.json                    # HACS repository metadata
└── .gitignore                   # Git ignore rules
```

## Development Workflow

### Running and Testing

**Home Assistant Development:**
```bash
# Run tests
pytest tests/ -v

# Run a specific test module
pytest tests/test_classifier.py -v

# Run with coverage
pytest tests/ --cov=custom_components/climate_advisor

# Lint and type check
ruff check .
mypy custom_components/climate_advisor/
```

**Note:** The integration depends on `weather` and `climate` HA platforms. Testing requires either a running HA instance or proper mocking of HA core APIs.

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

The project follows a phased development plan:

1. **v0.1**: Foundation — Core modules complete ✅ (Current)
2. **v0.2**: Learning suggestions live in briefing
3. **v0.3**: Multi-zone support, energy cost tracking
4. **v0.4**: Seasonal learning, anomaly detection

When implementing features, always check which version they belong to and ensure prerequisites are met.

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

Tests do not exist yet but should be built following this strategy:

**Unit Tests** (per module):
- `test_classifier.py` — Day classification for all 5 types, trend computation, edge cases at thresholds
- `test_briefing.py` — Briefing generation for each day type, trend modifiers, learning suggestion inclusion
- `test_learning.py` — Pattern detection (all 6 suggestion types), suggestion lifecycle, compliance scoring, rolling window
- `test_automation.py` — HVAC mode changes, door/window pause/resume, occupancy setback/restore, bedtime/morning, pre-conditioning
- `test_config_flow.py` — All 3 setup steps, options flow, validation

**Integration Tests**:
- `test_coordinator.py` — Forecast fetch → classify → automate → brief → learn pipeline
- `test_init.py` — Setup/teardown, service registration, suggestion response handling

**Mocking Approach**:
- Mock `hass` object and its services (`climate.set_temperature`, `notify.*`)
- Mock weather entity state and attributes for forecast data
- Mock `async_track_time_change` and other HA event helpers
- Use `pytest-homeassistant-custom-component` if available, otherwise build lightweight mocks

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
