"""Read the chosen AI provider's limits and compare them with Climate Advisor's requirements (Issue #1336).

What the occupant gets: before an investigation (and when saving the AI settings) a warning that names the
setting to raise in which integration, when the provider is known to be set below what a report needs, so a
report is not silently lost to the provider's output limit. Limits only ever warn; nothing is blocked.

Read-only, inside the approved HA Boundary Rule exception: the chosen AI Task entity's entity-registry entry
and its config subentry options. The option keys are internal to each integration (HA 2026.10.4 source), so a
missing integration, entry or subentry, or an unexpected value, is reported as "unreadable", never guessed.

One reader serves every caller: the ``ai_limits`` REST view, the options flow's save step and the dashboard's
pre-run check.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Any

from homeassistant.core import HomeAssistant

from .ai_settings_sync import provider_of
from .const import (
    AI_PROVIDER_BUILTIN_CLAUDE,
    AI_REQUIRED_COMBINED_TOKENS,
    AI_REQUIRED_OUTPUT_TOKENS,
    CONF_AI_MAX_TOKENS,
    CONF_AI_TASK_ENTITY,
    DEFAULT_AI_MAX_TOKENS,
    DOMAIN,
)

_LOGGER = logging.getLogger(__name__)

# Per integration domain: (kind, option key, default when the option is absent). HA 2026.10.4:
# anthropic/const.py:44 and entity.py:924 (3000), openai_conversation/const.py:43 and entity.py:533 (3000),
# google_generative_ai_conversation/const.py:35 RECOMMENDED_AI_TASK_MAX_TOKENS (19400),
# ollama/const.py:15-16 num_ctx (8192; it holds input and output, so it is compared with the combined need).
_PROVIDER_LIMITS: dict[str, tuple[str, str, int]] = {
    "anthropic": ("output", "max_tokens", 3000),
    "openai_conversation": ("output", "max_tokens", 3000),
    "google_generative_ai_conversation": ("output", "max_tokens", 19400),
    "ollama": ("combined", "num_ctx", 8192),
}
_REQUIRED_BY_KIND = {"output": AI_REQUIRED_OUTPUT_TOKENS, "combined": AI_REQUIRED_COMBINED_TOKENS}

# ai_task stores its preferences under this hass.data key (ai_task/const.py DATA_PREFERENCES, a HassKey: a str).
_AI_TASK_PREFERENCES_KEY = "ai_task_preferences"


def _check(kind: str, setting: str, value: int) -> dict[str, Any]:
    required = _REQUIRED_BY_KIND[kind]
    return {"kind": kind, "setting": setting, "value": value, "required": required, "ok": value >= required}


def _as_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        return int(value)
    except TypeError, ValueError:
        return None


def _result(
    provider: str,
    entity_id: str | None,
    integration: str | None,
    checks: list[dict[str, Any]] | None,
) -> dict[str, Any]:
    readable = checks is not None
    checks = checks or []
    return {
        "provider": provider,
        "entity_id": entity_id,
        "integration": integration,
        "readable": readable,
        "checks": checks,
        "ok": all(c["ok"] for c in checks),
    }


def _preferred_ai_task_entity(hass: HomeAssistant) -> str | None:
    """Home Assistant's preferred AI Task entity (Settings > System > AI), or None. Read defensively."""
    try:
        from homeassistant.components.ai_task.const import DATA_PREFERENCES  # noqa: PLC0415

        key: Any = DATA_PREFERENCES
    except Exception:  # noqa: BLE001 - no ai_task on this install
        key = _AI_TASK_PREFERENCES_KEY
    try:
        prefs = hass.data.get(key)
    except Exception:  # noqa: BLE001
        return None
    entity_id = getattr(prefs, "gen_data_entity_id", None) if prefs is not None else None
    return entity_id if isinstance(entity_id, str) and entity_id else None


def _subentry_options(hass: HomeAssistant, entity_id: str) -> tuple[str | None, Mapping[str, Any] | None]:
    """``(integration domain, subentry data)`` of an AI Task entity; either is None when it cannot be found."""
    from homeassistant.helpers import entity_registry as er  # noqa: PLC0415

    reg_entry = er.async_get(hass).async_get(entity_id)
    if reg_entry is None:
        return None, None
    config_entry_id = getattr(reg_entry, "config_entry_id", None)
    integration = getattr(reg_entry, "platform", None)
    config_entry = hass.config_entries.async_get_entry(config_entry_id) if config_entry_id else None
    if config_entry is not None:
        integration = getattr(config_entry, "domain", None) or integration
    integration = integration if isinstance(integration, str) else None
    subentry_id = getattr(reg_entry, "config_subentry_id", None)
    subentries = getattr(config_entry, "subentries", None) if config_entry is not None else None
    if not subentry_id or not isinstance(subentries, Mapping) or subentry_id not in subentries:
        return integration, None
    data = getattr(subentries[subentry_id], "data", None)
    return integration, data if isinstance(data, Mapping) else None


async def async_read_limits(hass: HomeAssistant, config: Mapping[str, Any]) -> dict[str, Any]:
    """The provider's limits against the requirements, for one zone's (install-wide) AI settings.

    Returns ``{"provider", "entity_id", "integration", "readable", "checks": [{"kind", "setting", "value",
    "required", "ok"}], "ok"}``. ``ok`` is False only when a readable check fails; an unreadable provider has
    ``readable: False`` and no checks. Never raises.
    """
    provider = provider_of(config)
    if provider == AI_PROVIDER_BUILTIN_CLAUDE:
        value = _as_int(config.get(CONF_AI_MAX_TOKENS, DEFAULT_AI_MAX_TOKENS))
        if value is None:
            return _result(provider, None, DOMAIN, None)
        return _result(provider, None, DOMAIN, [_check("output", "max_tokens", value)])

    entity_id = str(config.get(CONF_AI_TASK_ENTITY) or "") or _preferred_ai_task_entity(hass)
    if not entity_id:
        _LOGGER.debug("AI limits unreadable: no AI Task entity chosen and no preferred one set")
        return _result(provider, None, None, None)

    try:
        integration, options = _subentry_options(hass, entity_id)
    except Exception:  # noqa: BLE001 - a limit check must never break the caller
        _LOGGER.debug("AI limits unreadable: registry lookup failed entity=%s", entity_id, exc_info=True)
        return _result(provider, entity_id, None, None)

    spec = _PROVIDER_LIMITS.get(integration or "")
    if spec is None or options is None:
        _LOGGER.debug("AI limits unreadable: entity=%s integration=%s", entity_id, integration)
        return _result(provider, entity_id, integration, None)

    kind, setting, default = spec
    value = _as_int(options.get(setting, default))
    if value is None:
        _LOGGER.debug("AI limits unreadable: entity=%s %s is not a number", entity_id, setting)
        return _result(provider, entity_id, integration, None)

    result = _result(provider, entity_id, integration, [_check(kind, setting, value)])
    if not result["ok"]:
        _LOGGER.info(
            "AI limit below requirement: integration=%s setting=%s value=%d required=%d",
            integration,
            setting,
            value,
            _REQUIRED_BY_KIND[kind],
        )
    return result
