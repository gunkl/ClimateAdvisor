"""Install-wide AI settings (Issue #1336).

Climate Advisor has one config entry per zone and no install-level entry. AI settings are
install-wide by keeping the same values on every zone's entry:

- ``pick_install_ai_values()`` chooses the values for the whole install from every zone's
  original values (the stably-first zone with AI configured wins), used once by the
  config entry v20 -> v21 migration.
- ``unified_entry_data()`` builds one entry's new data from those values, backing up the
  entry's own original AI values under ``AI_SETTINGS_BACKUP_KEY`` when they differ, so no
  key or setting is lost.
- ``async_copy_to_other_entries()`` copies the AI step's saved values to every other zone
  when the occupant saves the AI step in any zone.

Stable order is ``hass.config_entries.async_entries(DOMAIN)``, the same order
``zone_registry.get_default_coordinator()`` / ``list_zones()`` /
``default_briefing_notifications_enabled()`` use.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from typing import Any

from homeassistant.core import HomeAssistant, callback

from .const import (
    AI_PROVIDER_BUILTIN_CLAUDE,
    AI_PROVIDER_HA_AI_TASK,
    AI_SETTINGS_BACKUP_KEY,
    CONF_AI_API_KEY,
    CONF_AI_AUTO_REQUESTS_PER_DAY,
    CONF_AI_ENABLED,
    CONF_AI_INVESTIGATOR_ENABLED,
    CONF_AI_INVESTIGATOR_RPD,
    CONF_AI_MANUAL_REQUESTS_PER_DAY,
    CONF_AI_MAX_TOKENS,
    CONF_AI_MODEL,
    CONF_AI_MONTHLY_BUDGET,
    CONF_AI_PROVIDER,
    CONF_AI_REASONING_EFFORT,
    CONF_AI_TASK_ENTITY,
    CONF_AI_TEMPERATURE,
    DOMAIN,
)

# Every key the AI settings step owns. These are kept identical on every zone's entry.
AI_SHARED_KEYS: tuple[str, ...] = (
    CONF_AI_ENABLED,
    CONF_AI_PROVIDER,
    CONF_AI_TASK_ENTITY,
    CONF_AI_API_KEY,
    CONF_AI_MODEL,
    CONF_AI_REASONING_EFFORT,
    CONF_AI_MAX_TOKENS,
    CONF_AI_TEMPERATURE,
    CONF_AI_MONTHLY_BUDGET,
    CONF_AI_AUTO_REQUESTS_PER_DAY,
    CONF_AI_MANUAL_REQUESTS_PER_DAY,
    CONF_AI_INVESTIGATOR_ENABLED,
    CONF_AI_INVESTIGATOR_RPD,
)


def extract_ai_values(data: Mapping[str, Any]) -> dict[str, Any]:
    """The AI keys present in ``data`` (absent keys are left out, not defaulted)."""
    return {key: data[key] for key in AI_SHARED_KEYS if key in data}


def original_ai_values(data: Mapping[str, Any]) -> dict[str, Any]:
    """An entry's AI values from before unification: its backup if it has one, else its current values."""
    backup = data.get(AI_SETTINGS_BACKUP_KEY)
    if isinstance(backup, Mapping):
        return dict(backup)
    return extract_ai_values(data)


def provider_of(values: Mapping[str, Any]) -> str:
    """The provider these values select. Pre-#1336 values have no provider: a key means built-in Claude."""
    provider = values.get(CONF_AI_PROVIDER)
    if provider in (AI_PROVIDER_HA_AI_TASK, AI_PROVIDER_BUILTIN_CLAUDE):
        return provider
    return AI_PROVIDER_BUILTIN_CLAUDE if values.get(CONF_AI_API_KEY) else AI_PROVIDER_HA_AI_TASK


def is_ai_configured(values: Mapping[str, Any]) -> bool:
    """AI is on and usable: built-in Claude needs a key; AI Task can rely on HA's preferred entity."""
    if not values.get(CONF_AI_ENABLED):
        return False
    if provider_of(values) == AI_PROVIDER_BUILTIN_CLAUDE:
        return bool(values.get(CONF_AI_API_KEY))
    return True


def pick_install_ai_values(
    entries_data: Iterable[Mapping[str, Any]],
    normalize: Callable[[dict[str, Any]], dict[str, Any]] | None = None,
) -> tuple[dict[str, Any] | None, bool]:
    """Choose the install's AI values from every zone's ORIGINAL values, in stable order.

    Returns ``(values, conflict)``: ``values`` is the stably-first zone with AI configured
    (``None`` when no zone has AI configured); ``conflict`` is True when another configured
    zone had different values (the occupant is told which zone's settings were kept).
    """
    configured = [original_ai_values(d) for d in entries_data]
    if normalize:
        # Compare every zone in one form: a zone migrated earlier holds its values already normalized.
        configured = [normalize(dict(v)) if v else v for v in configured]
    configured = [v for v in configured if is_ai_configured(v)]
    if not configured:
        return None, False
    winner = configured[0]
    conflict = any(v != winner for v in configured[1:])
    return dict(winner), conflict


def unified_entry_data(
    data: Mapping[str, Any],
    install_values: Mapping[str, Any],
    normalize: Callable[[dict[str, Any]], dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """This entry's data with the install's AI values; its own originals backed up when they differ.

    ``normalize`` puts the originals in the same form as ``install_values`` before comparing (the v21
    migration names the provider and maps "high" to "medium"), so a zone whose settings only changed form
    gets no backup, and no second copy of its key.
    """
    new_data = dict(data)
    own_original = original_ai_values(data)
    compared = normalize(dict(own_original)) if normalize else own_original
    if compared != dict(install_values) and AI_SETTINGS_BACKUP_KEY not in new_data:
        new_data[AI_SETTINGS_BACKUP_KEY] = own_original
    for key in AI_SHARED_KEYS:
        new_data.pop(key, None)
    new_data.update(install_values)
    return new_data


@callback
def async_copy_to_other_entries(hass: HomeAssistant, source_entry_id: str, values: Mapping[str, Any]) -> int:
    """Write the AI step's values to every other Climate Advisor entry. Returns how many changed.

    Values take effect on the next reload, like every other options section (Issue #573).
    """
    changed = 0
    shared = {key: values[key] for key in AI_SHARED_KEYS if key in values}
    for entry in hass.config_entries.async_entries(DOMAIN):
        if entry.entry_id == source_entry_id:
            continue
        new_data = dict(entry.data)
        for key in AI_SHARED_KEYS:
            new_data.pop(key, None)
        new_data.update(shared)
        if new_data != dict(entry.data):
            hass.config_entries.async_update_entry(entry, data=new_data)
            changed += 1
    return changed
