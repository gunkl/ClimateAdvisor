<!-- Nav: ← [Architecture Reference](02-ARCHITECTURE-REFERENCE.md) -->

# Logging Guidelines

Standards for log statements across all Climate Advisor modules.

## Anchors
| Question | Short answer | → Full answer |
|---|---|---|
| What formatting rules apply to all log messages? | Use %-style formatting (never f-strings), em dash `—` to separate event from detail context, past tense for completed actions, and always include units (°F, seconds, minutes). | [§Format Rules](06-LOGGING-GUIDELINES.md#format-rules) |
| What log level should a thermostat mode change use, and what must the message include? | `WARNING`, via the `_set_hvac_mode` / `_set_temperature` primitives which emit a single consolidated line with a mandatory `reason` keyword argument and `role` field. No call site may omit the reason. | [§Thermostat Adjustment Logging](06-LOGGING-GUIDELINES.md#thermostat-adjustment-logging) |
| What is the reason string convention for thermostat adjustment log messages? | `trigger — context` pattern using em dash: e.g., `"daily classification — hot day, trend warming 8°F"` or `"bedtime — heat setback (comfort 70 - 4 + modifier 2)"`. | [§Reason string convention](06-LOGGING-GUIDELINES.md#reason-string-convention) |
| How are skipped actions logged when automation is disabled (observe-only mode)? | With a `[DRY RUN]` prefix at INFO level, e.g., `"[DRY RUN] Would set HVAC mode to cool — daily classification — hot day"`. Easily grep'd to distinguish from real actions. | [§Dry-run prefix convention](06-LOGGING-GUIDELINES.md#dry-run-prefix-convention) |
| What distinguishes DEBUG from INFO level? | DEBUG: high-frequency or transient events (threshold calculations, debounce timers, per-classification details). INFO: lifecycle milestones and meaningful state transitions (HVAC mode changes, briefings sent, records saved). | [§Level Semantics](06-LOGGING-GUIDELINES.md#level-semantics) |

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

## Thermostat Adjustment Logging

Every thermostat adjustment must include a plain-text **reason** explaining why the change is being made. This is enforced by a required `reason: str` keyword-only parameter on both primitive methods:

```python
async def _set_hvac_mode(self, mode: str, *, reason: str) -> None:
async def _set_temperature(self, temperature: float, *, reason: str) -> None:
async def _set_temperature_for_mode(self, c: DayClassification, *, reason: str) -> None:
```

The primitives emit a single consolidated `WARNING` log per adjustment. The following are illustrative examples of log lines following this pattern:

```
WARNING  Set HVAC mode to cool — daily classification — hot day, trend warming 8°F
WARNING  Set temperature to 72°F (mode=cool) — daily classification — hot day (pre-cool offset -3°F) role=automation
WARNING  Set HVAC mode to off — door/window open — binary_sensor.kitchen_window, was cool mode
WARNING  Set temperature to 68°F (mode=heat) — bedtime — heat setback (comfort 70 - 4 + modifier 2) role=automation
WARNING  Set temperature to 70°F (mode=heat) — morning wake-up — restoring heat comfort role=automation
```

### Reason string convention

Reasons follow a **trigger — context** pattern using the em dash separator:

| Trigger | Context format |
|---------|---------------|
| `daily classification` | `%s day, trend %s %s°F` |
| `door/window open` | `%s, was %s mode` |
| `door/window closed` | `restoring %s mode` / `restoring comfort` |
| `occupancy away` | `%s setback (base %s + modifier %s)` |
| `occupancy home` | `restoring %s comfort` |
| `bedtime` | `%s setback (comfort %s ± offset + modifier %s)` |
| `morning wake-up` | `restoring %s comfort` |

### Dry-run prefix convention

When automation is disabled (observe-only mode), skipped service calls are logged with a `[DRY RUN]` prefix at INFO level:

```
INFO  [DRY RUN] Would set HVAC mode to cool — daily classification — hot day, trend warming 8°F
INFO  [DRY RUN] Would set temperature to 72°F — bedtime — heat setback (comfort 70 - 4 + modifier 2)
INFO  [DRY RUN] Would send notification: Climate Advisor — Welcome home! ...
INFO  [DRY RUN] Briefing generated but notification skipped (automation disabled)
```

The `[DRY RUN]` prefix makes it easy to `grep` logs for skipped actions and distinguish them from real ones.

### Why `reason` is required (no default)

Making it a required keyword-only parameter ensures every current and future call site is forced to provide an explanation. A missing reason causes a loud `TypeError` at runtime rather than a silent log gap.

## Module Coverage

| Module | Log Statements | Levels Used |
|--------|---------------|-------------|
| `__init__.py` | 12 | INFO |
| `coordinator.py` | ~25 | DEBUG, INFO, WARNING |
| `automation.py` | ~15 | INFO |
| `classifier.py` | 4 | DEBUG |
| `briefing.py` | 5 | DEBUG |
| `config_flow.py` | 4 | DEBUG, INFO |
| `sensor.py` | 3 | DEBUG, WARNING |
| `learning.py` | 10 | DEBUG, INFO, WARNING, ERROR |

## Examples from Existing Code

**INFO — lifecycle milestone** (automation.py):

For example, a line following this pattern might read:
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
    "Outdoor temp entity %s has non-numeric state %r; falling back to weather attribute",
    entity_id,
    state.state,
)
```

**WARNING — thermostat adjustment with reason** (automation.py):
```python
_LOGGER.warning(
    "Set temperature to %s (mode=%s) — %s role=%s",
    format_temp(temperature, unit),
    mode,
    reason,
    self.role,
)
```
