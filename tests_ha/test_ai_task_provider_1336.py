"""Issue #1336 on a real Home Assistant core: Climate Advisor's AI client and limit reader.

Occupant view: the limit reader finds the chosen AI Task entity's provider settings through HA's real entity
registry and config subentries, so a too-small Anthropic output limit is warned about before a report is lost;
and on an install where ``ai_task`` cannot be loaded, an investigation fails with the "choose an AI Task entity"
notice instead of breaking Climate Advisor.

Not covered here: a fake ``AITaskEntity`` end to end. ``homeassistant.components.ai_task`` imports
``conversation``, which needs ``hassil`` / ``home-assistant-intents``; the ``test-ha`` dependency group does not
install them (adding them is a pyproject/uv.lock change). The AI Task call itself is unit-tested in
``tests/test_ai_task_1336.py``.
"""

from __future__ import annotations

from types import MappingProxyType

from homeassistant.config_entries import ConfigSubentry
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.climate_advisor import ai_limits
from custom_components.climate_advisor.claude_api import ClaudeAPIClient
from custom_components.climate_advisor.const import AI_ERROR_NO_ENTITY, AI_PROVIDER_HA_AI_TASK


async def test_ai_task_unavailable_reports_no_entity(hass) -> None:
    client = ClaudeAPIClient({"ai_enabled": True, "ai_provider": AI_PROVIDER_HA_AI_TASK, "ai_task_entity": ""})
    client.hass = hass
    resp = await client.async_request("S", "C")
    assert resp.success is False
    assert resp.error_code == AI_ERROR_NO_ENTITY


async def test_limit_reader_uses_real_registry_and_subentry(hass) -> None:
    subentry = ConfigSubentry(
        data=MappingProxyType({"max_tokens": 3000}),
        subentry_type="ai_task_data",
        title="Claude AI Task",
        unique_id=None,
    )
    entry = MockConfigEntry(domain="anthropic", subentries_data=[subentry.as_dict()])
    entry.add_to_hass(hass)
    sub_id = next(iter(entry.subentries))
    er.async_get(hass).async_get_or_create(
        "ai_task",
        "anthropic",
        "claude_task",
        suggested_object_id="claude_task",
        config_entry=entry,
        config_subentry_id=sub_id,
    )
    out = await ai_limits.async_read_limits(
        hass, {"ai_enabled": True, "ai_provider": AI_PROVIDER_HA_AI_TASK, "ai_task_entity": "ai_task.claude_task"}
    )
    assert out["readable"] is True
    assert out["integration"] == "anthropic"
    assert out["ok"] is False
    assert out["checks"][0]["value"] == 3000
