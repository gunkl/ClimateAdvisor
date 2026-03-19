# Logging Guidelines

Standards for log statements across all Climate Advisor modules.

## Logger Setup

Every module uses the standard Home Assistant pattern:

```python
_LOGGER = logging.getLogger(__name__)
```

The logger name automatically carries module identity (`custom_components.climate_advisor.classifier`, etc.), so **do not add module prefixes** to messages.

## Format Rules

- Always use **%-style formatting**: `"message %s", value` — never f-strings in log calls
- Use `%r` for unexpected or untrusted values (entity states, user-provided strings)
- Use `%d` for integers, `%s` for everything else including floats
- Use an **em dash** (`—`) to separate the event from its detail context
- Use **past tense** for completed actions ("Recorded day", "Loaded state")
- Include **units** wherever quantities appear: `°F`, `seconds`, `minutes`, `chars`

## Level Semantics

| Level | Use For | Examples |
|-------|---------|---------|
| `DEBUG` | High-frequency or transient events: individual service calls, debounce timers, threshold calculations, heuristic detections, classification details | `"Day type — today_high=%.0f°F, classified=%s"` |
| `INFO` | Lifecycle milestones and meaningful state transitions: setup complete, HVAC mode changes, briefings sent, records saved, config created/updated, suggestions accepted/dismissed | `"Config entry created — wake=%s, sleep=%s"` |
| `WARNING` | Recoverable problems with fallback behavior. Always name what failed **and** what happens next. | `"Weather entity %s not found. Check entity ID in options."` |
| `ERROR` | Unrecoverable failures with no fallback (file I/O failures, broken invariants) | `"Failed to save learning state: %s"` |

## Module Coverage

| Module | Log Statements | Levels Used |
|--------|---------------|-------------|
| `__init__.py` | 12 | INFO |
| `coordinator.py` | ~25 | DEBUG, INFO, WARNING |
| `automation.py` | ~17 | DEBUG, INFO |
| `classifier.py` | 4 | DEBUG |
| `briefing.py` | 5 | DEBUG |
| `config_flow.py` | 4 | DEBUG, INFO |
| `sensor.py` | 3 | DEBUG, WARNING |
| `learning.py` | 10 | DEBUG, INFO, WARNING, ERROR |

## Examples from Existing Code

**INFO — lifecycle milestone** (automation.py):
```python
_LOGGER.info("Paused HVAC due to open: %s", entity_id)
```

**DEBUG — transient operational detail** (coordinator.py):
```python
_LOGGER.debug("No persisted state found — starting fresh")
```

**WARNING — recoverable problem with fallback** (coordinator.py):
```python
_LOGGER.warning(
    "Outdoor temp entity %s has non-numeric state %r; "
    "falling back to weather attribute",
    entity_id, state.state,
)
```

**INFO — state transition with detail** (automation.py):
```python
_LOGGER.info("Bedtime setback — heat to %s°F", target)
```
