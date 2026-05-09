<!-- Nav: ← [Architecture Reference](02-ARCHITECTURE-REFERENCE.md) → Detail: repairs.py (source) | Tier 3 spec: not yet written -->

# Repairs — Architecture Brief (Tier 2)

## Anchors

| Question | Short answer | → Full answer |
|---|---|---|
| What does `repairs.py` own and what does it explicitly not own? | It owns the HA Repairs flow classes and the `async_create_fix_flow()` dispatcher. It does NOT own issue creation or dismissal — those live in `__init__.py`. | [Scope](#scope) |
| What repair issues are currently defined and what triggers each? | One issue: `weather_entity_not_found`, raised during integration setup when the configured weather entity is absent from HA and auto-resolution fails. | [Issue Types](#issue-types) |
| How does HA call into `repairs.py` when a user clicks Fix? | HA calls `async_create_fix_flow(hass, issue_id, data)`. For `weather_entity_not_found` this returns `WeatherEntityRepairFlow`; all other issue IDs fall back to `ConfirmRepairFlow`. | [HA Repairs Integration Pattern](#ha-repairs-integration-pattern) |
| What does `WeatherEntityRepairFlow` do when the user submits a valid entity? | It validates the entity exists in `hass.states`, updates the config entry with the new `weather_entity` value, calls `ir.async_delete_issue()` to dismiss the repair, then schedules an async config-entry reload. | [WeatherEntityRepairFlow](#weatherentityrepairflow) |
| What invariant governs issue lifecycle — when is the issue re-raised? | The issue is raised only when the weather entity is missing AND auto-resolution fails. It is deleted as soon as a valid entity is found (either by the flow or by `__init__.py` on next load). It will be re-raised on the next integration load if the entity is still missing. | [Invariants](#invariants) |
| Who raises the `weather_entity_not_found` issue and on what schedule? | `_validate_weather_entity()` in `__init__.py:async_setup_entry()` — deferred to `EVENT_HOMEASSISTANT_STARTED` on first boot, or called immediately on reload. | [Coordinator Interface](#coordinator-interface) |

## Scope

**Owns:**
- HA Repairs flow classes (`WeatherEntityRepairFlow`)
- The `async_create_fix_flow()` dispatcher — the entry point HA calls when a user initiates a repair
- Form rendering for the weather-entity picker step (entity selector UI, validation, error messages)
- Config-entry update and issue dismissal on successful fix submission

**Explicitly does NOT own:**
- Issue creation — `ir.async_create_issue()` is called in `__init__.py`, not here
- Issue detection logic — the check for whether the weather entity exists is in `__init__.py:_validate_weather_entity()`
- Translation strings for issue titles/descriptions — stored in `strings.json` / `translations/en.json` under the `issues` key

## Responsibilities

- Expose `async_create_fix_flow()` so HA's Repairs framework can instantiate the correct flow class for each issue ID
- Present a form that lets the user pick a replacement weather entity from all available `weather.*` entities
- Validate that the chosen entity actually exists in `hass.states` before accepting the form
- On valid submission: update the config entry with the new entity, dismiss the issue via `ir.async_delete_issue()`, and schedule an integration reload (deferred via `hass.async_create_task` to avoid teardown mid-flow)
- Fall back to `ConfirmRepairFlow` for any unknown issue ID (HA's built-in acknowledge-only flow)

## Interfaces

```python
async def async_create_fix_flow(
    hass: HomeAssistant,
    issue_id: str,
    data: dict | None,
) -> RepairsFlow:
    """Return the RepairsFlow subclass for the given issue_id."""
```

| Symbol | Caller(s) | Purpose |
|---|---|---|
| `async_create_fix_flow()` | HA Repairs framework (platform dispatch) | Returns the correct flow instance when a user clicks "Fix" on a repair issue |
| `WeatherEntityRepairFlow.async_step_init()` | HA flow engine | Renders and handles the entity-picker form step |

**Events emitted / consumed:** none — the module communicates solely through HA's config-entry API and issue registry.

## Issue Types

### `weather_entity_not_found`

| Field | Value |
|---|---|
| Issue ID | `weather_entity_not_found` |
| `is_fixable` | `True` (shows a "Fix" button in HA UI) |
| `is_persistent` | `True` (survives HA restarts until explicitly deleted) |
| `severity` | `ir.IssueSeverity.ERROR` |
| `translation_key` | `weather_entity_not_found` |
| `translation_placeholders` | `{"entity_id": <configured entity ID>}` |

**Trigger:** `__init__.py:_validate_weather_entity()` finds that `hass.states.get(weather_entity)` returns `None` AND the auto-resolution heuristic (`_resolve_weather_entity()`) cannot suggest a replacement.

**User-visible effect:** An error card appears in **Settings → System → Repairs** with a "Fix" button. Clicking Fix opens `WeatherEntityRepairFlow`.

**Resolution:** User selects a valid `weather.*` entity. `WeatherEntityRepairFlow` updates the config entry, calls `ir.async_delete_issue(hass, DOMAIN, "weather_entity_not_found")`, and triggers a reload. The issue disappears from the Repairs queue.

**Re-raise condition:** If the integration is reloaded and the weather entity is still absent (and auto-resolution again fails), `_validate_weather_entity()` will call `ir.async_create_issue()` again with the same issue ID.

### `WeatherEntityRepairFlow`

Multi-step flow — currently one step (`async_step_init`):

1. **GET (no user_input):** Render form with an `EntitySelector` filtered to `domain="weather"`.
2. **POST (user_input present, entity not found):** Re-render with `errors={"weather_entity": "entity_not_found"}`.
3. **POST (user_input present, entity found):**
   - Update config entry: `{**entry.data, "weather_entity": <new_entity>}`
   - Call `ir.async_delete_issue(hass, DOMAIN, "weather_entity_not_found")`
   - Schedule reload via `hass.async_create_task(hass.config_entries.async_reload(entry.entry_id))`
   - Return `self.async_create_entry(title="", data={})` to close the flow

## HA Repairs Integration Pattern

Climate Advisor registers itself as a Repairs platform in `manifest.json` (via `"iot_class"` and platform discovery). HA's Repairs component discovers `async_create_fix_flow` automatically by module convention at `{domain}/repairs.py`.

| Concept | Value |
|---|---|
| Integration domain | `climate_advisor` (from `const.DOMAIN`) |
| HA base classes used | `RepairsFlow` (interactive), `ConfirmRepairFlow` (acknowledge-only) |
| Issue registry helpers | `homeassistant.helpers.issue_registry` (`ir.async_create_issue`, `ir.async_delete_issue`) |
| UI selector | `homeassistant.helpers.selector.EntitySelector` with `EntitySelectorConfig(domain="weather")` |
| Translation key namespace | `issues.weather_entity_not_found` in `strings.json` / `translations/en.json` |

## Data Structures

No module-level data structures. `WeatherEntityRepairFlow` carries no persistent fields beyond what HA's `RepairsFlow` base class manages (hass reference, flow ID).

The entity selector schema:

```python
vol.Schema({
    vol.Required("weather_entity"): selector.EntitySelector(
        selector.EntitySelectorConfig(domain="weather")
    ),
})
```

**Persistence:** none — the module is stateless. Issue persistence is managed by HA's issue registry, not by this module.

## Invariants

1. **Issue ID uniqueness:** `weather_entity_not_found` is a fixed string constant — it is the same key on every raise. HA's issue registry deduplicates on `(domain, issue_id)`, so multiple calls to `ir.async_create_issue()` with the same pair update the existing issue rather than creating duplicates.
2. **Dismissal before reload:** `ir.async_delete_issue()` is always called before the config-entry reload is scheduled. The reload is deferred via `hass.async_create_task()` to avoid tearing down the flow context mid-execution.
3. **Config-entry target:** The flow always applies updates to `entries[0]` — the first (and only expected) config entry for the `climate_advisor` domain. Multi-entry setups are not supported.
4. **Fallback safety:** `async_create_fix_flow()` never returns `None` and never raises. Unknown issue IDs return `ConfirmRepairFlow()`, which is always safe.
5. **Re-raise on recurrence:** The issue is not suppressed after resolution. If the triggering condition returns (entity removed again, integration reloaded), `_validate_weather_entity()` will re-raise the issue on the next startup event.

## Coordinator Interface

`repairs.py` does NOT export any function called by `coordinator.py`. All calls originate from:

| Caller | Function | When |
|---|---|---|
| `__init__.py:async_setup_entry()` | `ir.async_create_issue()` | Deferred to `EVENT_HOMEASSISTANT_STARTED` on first boot; immediate on reload if HA is already running |
| `__init__.py:_validate_weather_entity()` | `ir.async_delete_issue()` | When auto-resolution succeeds (entity found or alias resolved) |
| HA Repairs framework | `async_create_fix_flow()` | When user clicks "Fix" in Settings → System → Repairs |

The coordinator (`coordinator.py`) does not interact with `repairs.py` directly. Issue detection is a setup-time concern owned by `__init__.py`.

## Disclosure Path

← Tier 1 parent: [Architecture Reference](02-ARCHITECTURE-REFERENCE.md) — see §repairs.py — HA Repair Issues
→ Tier 3 spec: not yet written (known gap — see CLAUDE.md Known Gaps)
↔ Siblings: [REST API Brief](rest-api.md) | [AI Integration Brief](ai-integration.md) | [State Persistence Brief](state-persistence.md)
