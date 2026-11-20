"""Issue #1336: bring-your-own AI provider through Home Assistant AI Task.

Occupant view: the Investigator report is written by whatever AI integration the occupant set up in Home
Assistant (no Claude key pasted into Climate Advisor). When the provider stops at its output limit, cannot fit
the data, or has no AI Task entity, the occupant is told which one (a stable ``error_code``) instead of a bare
failure; a reply missing sections is flagged "cut short"; a known-too-small provider limit is warned about
before the run. Upgrading keeps every stored setting (backed up, never deleted) and makes the AI settings
apply to all zones.
"""

from __future__ import annotations

import asyncio
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from homeassistant.exceptions import HomeAssistantError

from custom_components.climate_advisor import claude_api
from custom_components.climate_advisor.ai_skills import AISkillDefinition, AISkillRegistry
from custom_components.climate_advisor.ai_skills_investigator import (
    REQUIRED_REPORT_SECTIONS,
    parse_investigation_response,
    report_cut_short,
)
from custom_components.climate_advisor.claude_api import AIProviderError, ClaudeAPIClient, classify_ai_task_error
from custom_components.climate_advisor.const import (
    AI_CUT_SHORT,
    AI_ERROR_CONTEXT_LIMIT,
    AI_ERROR_NO_ENTITY,
    AI_ERROR_OUTPUT_LIMIT,
    AI_ERROR_PROVIDER,
    AI_PROVIDER_BUILTIN_CLAUDE,
    AI_PROVIDER_HA_AI_TASK,
    AI_SETTINGS_BACKUP_KEY,
    AI_SHARED_DATA_KEY,
    AI_TASK_NAME,
    DOMAIN,
    ISSUE_AI_SETTINGS_UNIFIED,
)

_GEN = "custom_components.climate_advisor.claude_api._async_ai_task_generate"

_FULL_REPORT = "\n".join(
    f"## {h}\nSome text."
    for h in (
        "ACTIVITY SUMMARY",
        "INCONGRUITIES FOUND",
        "DATA QUALITY ISSUES",
        "SYSTEM ERRORS / WARNINGS",
        "HYPOTHESES",
        "RECOMMENDED ACTIONS",
        "ASSUMPTIONS & CONFIDENCE",
    )
)


def _task_client(**config) -> ClaudeAPIClient:
    cfg = {"ai_enabled": True, "ai_provider": AI_PROVIDER_HA_AI_TASK, "ai_task_entity": "", **config}
    client = ClaudeAPIClient(cfg)
    client.hass = MagicMock()
    return client


# ---------------------------------------------------------------------------
# Transport
# ---------------------------------------------------------------------------


class TestAITaskRequest:
    def test_success_needs_no_key_and_costs_nothing(self):
        client = _task_client(ai_task_entity="ai_task.openai")
        gen = AsyncMock(return_value=SimpleNamespace(data="## HYPOTHESES\nx", conversation_id="c"))
        with patch(_GEN, gen):
            resp = asyncio.run(client.async_request("skill: investigator\nSYS", "CONTEXT"))
        assert resp.success is True
        assert resp.content == "## HYPOTHESES\nx"
        assert (resp.estimated_cost, resp.input_tokens, resp.output_tokens) == (0.0, 0, 0)
        assert resp.error_code is None
        assert client._client is None  # no anthropic client in AI Task mode
        kwargs = gen.await_args.kwargs
        assert kwargs["entity_id"] == "ai_task.openai"
        assert kwargs["instructions"] == "skill: investigator\nSYS\n\nCONTEXT"
        assert client.get_request_history()[-1]["success"] is True
        assert client._rate_counters.manual_requests_today == 1
        # Same guard as the built-in path: the model-fallback persistence must never fire on AI Task
        assert resp.resolved_model == ""

    def test_real_helper_passes_task_name_and_preferred_entity(self):
        fake_ai_task = MagicMock()
        fake_ai_task.async_generate_data = AsyncMock(return_value=SimpleNamespace(data="ok"))
        hass = MagicMock()
        with (
            patch.dict(sys.modules, {"homeassistant.components.ai_task": fake_ai_task}),
            patch.object(sys.modules["homeassistant.components"], "ai_task", fake_ai_task, create=True),
        ):
            asyncio.run(claude_api._async_ai_task_generate(hass, entity_id=None, instructions="I"))
        fake_ai_task.async_generate_data.assert_awaited_once_with(
            hass, task_name=AI_TASK_NAME, entity_id=None, instructions="I"
        )

    def test_empty_reply_is_a_provider_failure(self):
        client = _task_client()
        with patch(_GEN, AsyncMock(return_value=SimpleNamespace(data="   "))):
            resp = asyncio.run(client.async_request("SYS", "CTX"))
        assert resp.success is False
        assert resp.error_code == AI_ERROR_PROVIDER
        assert client._circuit_breaker.consecutive_failures == 1

    def test_failure_trips_breaker_like_builtin(self):
        client = _task_client()
        with patch(_GEN, AsyncMock(side_effect=HomeAssistantError("boom"))):
            for _ in range(5):
                asyncio.run(client.async_request("SYS", "CTX"))
            resp = asyncio.run(client.async_request("SYS", "CTX"))
        assert resp.circuit_open is True
        assert client.get_status()["status"] == "circuit_open"

    def test_budget_is_not_checked_on_ai_task(self):
        client = _task_client(ai_monthly_budget=1)
        client._budget.monthly_cost = 999.0
        with patch(_GEN, AsyncMock(return_value=SimpleNamespace(data="ok"))):
            resp = asyncio.run(client.async_request("SYS", "CTX"))
        assert resp.success is True
        assert client.get_status()["status"] == "active"

    def test_status_shows_provider_and_entity(self):
        st = _task_client(ai_task_entity="ai_task.ollama").get_status()
        assert st["provider"] == AI_PROVIDER_HA_AI_TASK
        assert st["ai_task_entity"] == "ai_task.ollama"
        assert st["monthly_cost_estimate"] == 0.0

    def test_unattached_client_is_inactive_and_fails_cleanly(self):
        client = ClaudeAPIClient({"ai_enabled": True, "ai_provider": AI_PROVIDER_HA_AI_TASK})
        assert client.get_status()["status"] == "inactive"
        resp = asyncio.run(client.async_request("SYS", "CTX"))
        assert resp.success is False and resp.error_code == AI_ERROR_PROVIDER


class TestErrorClassification:
    @pytest.mark.parametrize(
        ("exc", "code"),
        [
            (
                HomeAssistantError(translation_domain="anthropic", translation_key="response_max_tokens"),
                AI_ERROR_OUTPUT_LIMIT,
            ),
            (HomeAssistantError("OpenAI response incomplete: max output tokens reached"), AI_ERROR_OUTPUT_LIMIT),
            (
                HomeAssistantError(translation_domain="anthropic", translation_key="response_context_window_exceeded"),
                AI_ERROR_CONTEXT_LIMIT,
            ),
            (HomeAssistantError("No entity_id provided and no preferred entity set"), AI_ERROR_NO_ENTITY),
            (HomeAssistantError(translation_key="no_entity"), AI_ERROR_NO_ENTITY),
            (KeyError("ai_task_preferences"), AI_ERROR_NO_ENTITY),
            (HomeAssistantError("AI Task entity ai_task.x not found"), AI_ERROR_PROVIDER),
            (HomeAssistantError(translation_domain="anthropic", translation_key="api_refusal"), AI_ERROR_PROVIDER),
            (TimeoutError(), AI_ERROR_PROVIDER),
        ],
    )
    def test_classify(self, exc, code):
        assert classify_ai_task_error(exc) == code

    def test_code_is_carried_on_response(self):
        client = _task_client()
        err = HomeAssistantError(translation_domain="anthropic", translation_key="response_max_tokens")
        with patch(_GEN, AsyncMock(side_effect=err)):
            resp = asyncio.run(client.async_request("SYS", "CTX"))
        assert resp.success is False
        assert resp.error_code == AI_ERROR_OUTPUT_LIMIT

    def test_timeout_is_provider_error(self):
        client = _task_client()
        with patch(_GEN, AsyncMock(side_effect=TimeoutError())):
            resp = asyncio.run(client.async_request("SYS", "CTX"))
        assert resp.error_code == AI_ERROR_PROVIDER
        assert "timed out" in resp.error


class TestAITaskStreaming:
    async def _collect(self, client):
        return [e async for e in client.async_request_streaming("SYS", "CTX")]

    def test_one_text_event_then_stop(self):
        client = _task_client()
        with patch(_GEN, AsyncMock(return_value=SimpleNamespace(data="REPORT"))):
            events = asyncio.run(self._collect(client))
        assert events == [
            {"type": "text", "text": "REPORT"},
            {"type": "stop", "stop_reason": "end_turn", "truncated_empty": False},
        ]
        assert client._total_requests == 1
        assert client.get_request_history()[-1]["success"] is True

    def test_failure_raises_with_code(self):
        client = _task_client()
        err = HomeAssistantError("OpenAI response incomplete: max output tokens reached")
        with patch(_GEN, AsyncMock(side_effect=err)), pytest.raises(AIProviderError) as info:
            asyncio.run(self._collect(client))
        assert info.value.error_code == AI_ERROR_OUTPUT_LIMIT
        assert client._circuit_breaker.consecutive_failures == 1

    def test_skill_stream_puts_code_and_keys_on_events(self):
        client = _task_client()
        registry = AISkillRegistry()

        async def _ctx(hass, coordinator, status_callback=None, **kw):
            await status_callback("Loading thing…")
            return "CTX"

        registry.register(
            AISkillDefinition(
                name="s", description="d", system_prompt="SYS", context_builder=_ctx, response_parser=lambda t: {}
            )
        )
        err = HomeAssistantError(translation_key="response_context_window_exceeded")

        async def _run():
            return [e async for e in registry.async_execute_streaming("s", MagicMock(), MagicMock(config={}), client)]

        with patch(_GEN, AsyncMock(side_effect=err)):
            events = asyncio.run(_run())
        statuses = [e for e in events if e["type"] == "status"]
        assert [e["key"] for e in statuses] == [
            "ai.status_gathering",
            "ai.status_gathering",
            "ai.status_sending",
            "ai.status_provider",
        ]
        assert all(e["text"] for e in statuses)
        assert "Claude" not in " ".join(e["text"] for e in statuses)
        assert events[-1] == {
            "type": "error",
            "message": "AI Task error: response_context_window_exceeded",
            "error_code": AI_ERROR_CONTEXT_LIMIT,
        }

    def test_non_streaming_fallback_carries_code(self):
        client = _task_client()
        registry = AISkillRegistry()

        async def _ctx(hass, coordinator, **kw):
            return "CTX"

        registry.register(
            AISkillDefinition(
                name="s",
                description="d",
                system_prompt="SYS",
                context_builder=_ctx,
                response_parser=lambda t: {},
                fallback=lambda coordinator, **kw: {"full_text": ""},
            )
        )
        with patch(
            _GEN, AsyncMock(side_effect=HomeAssistantError("No entity_id provided and no preferred entity set"))
        ):
            result = asyncio.run(registry.async_execute("s", MagicMock(), MagicMock(config={}), client))
        assert result["source"] == "fallback"
        assert result["error_code"] == AI_ERROR_NO_ENTITY


class TestBuiltinUnchanged:
    def test_builtin_still_requires_key(self):
        client = ClaudeAPIClient({"ai_enabled": True, "ai_provider": AI_PROVIDER_BUILTIN_CLAUDE})
        assert client.uses_ai_task is False
        assert client.get_status()["status"] == "inactive"


# ---------------------------------------------------------------------------
# Cut short
# ---------------------------------------------------------------------------


class TestCutShort:
    def test_complete_report_is_not_cut_short(self):
        assert report_cut_short(parse_investigation_response(_FULL_REPORT)) is False

    def test_missing_section_is_cut_short(self):
        partial = _FULL_REPORT.split("## ASSUMPTIONS")[0]
        assert report_cut_short(parse_investigation_response(partial)) is True

    def test_truncated_is_cut_short(self):
        assert report_cut_short(parse_investigation_response(_FULL_REPORT), truncated=True) is True

    def test_fallback_and_empty_are_not_cut_short(self):
        assert report_cut_short({"full_text": ""}) is False
        assert report_cut_short(parse_investigation_response("")) is False

    def test_required_sections_match_parser(self):
        keys = set(parse_investigation_response("x")) - {"full_text"}
        assert set(REQUIRED_REPORT_SECTIONS) == keys

    def test_investigate_view_adds_cut_short_to_json_result(self):
        from custom_components.climate_advisor.api import ClimateAdvisorInvestigateView

        partial = _FULL_REPORT.split("## HYPOTHESES")[0]
        coord = MagicMock()
        coord.config = {"ai_enabled": True, "ai_investigator_enabled": True}
        coord.claude_client.check_investigator_rate_limit.return_value = (True, "")
        coord.ai_skills.async_execute = AsyncMock(
            return_value={
                "success": True,
                "source": "ai",
                "data": parse_investigation_response(partial),
                "error": None,
                "input_context": "c",
                "raw_response": partial,
                "truncated": False,
            }
        )
        coord.async_store_investigation_report = AsyncMock()
        hass = MagicMock()
        hass.data = {DOMAIN: {"entry1": coord}}
        req = MagicMock(app={"hass": hass}, headers={}, query={})
        req.json = AsyncMock(return_value={})
        resp = asyncio.run(ClimateAdvisorInvestigateView().post(req))
        assert resp.json_data["cut_short"] is True
        assert resp.json_data["cut_short_code"] == AI_CUT_SHORT
        assert resp.json_data["error_code"] is None


# ---------------------------------------------------------------------------
# Limit reader
# ---------------------------------------------------------------------------


def _limits_hass(domain: str | None, subentry_data: dict | None, *, preferred: str | None = None):
    hass = MagicMock()
    prefs = SimpleNamespace(gen_data_entity_id=preferred)
    hass.data = {"ai_task_preferences": prefs}
    reg_entry = SimpleNamespace(config_entry_id="ce1", config_subentry_id="sub1", platform=domain)
    registry = MagicMock()
    registry.async_get.side_effect = lambda eid: reg_entry if domain else None
    entry = SimpleNamespace(
        domain=domain, subentries={"sub1": SimpleNamespace(data=subentry_data)} if subentry_data is not None else {}
    )
    hass.config_entries.async_get_entry.return_value = entry
    return hass, registry


def _read(hass, registry, config):
    from custom_components.climate_advisor import ai_limits

    er_mod = sys.modules["homeassistant.helpers.entity_registry"]
    with patch.object(er_mod, "async_get", return_value=registry):
        return asyncio.run(ai_limits.async_read_limits(hass, config))


_TASK_CFG = {"ai_enabled": True, "ai_provider": AI_PROVIDER_HA_AI_TASK, "ai_task_entity": "ai_task.x"}


class TestLimitReader:
    def test_anthropic_default_is_known_small(self):
        out = _read(*_limits_hass("anthropic", {}), _TASK_CFG)
        assert out["readable"] is True and out["ok"] is False
        assert out["integration"] == "anthropic" and out["entity_id"] == "ai_task.x"
        assert out["checks"] == [
            {"kind": "output", "setting": "max_tokens", "value": 3000, "required": 8192, "ok": False}
        ]

    def test_openai_raised_limit_is_ok(self):
        out = _read(*_limits_hass("openai_conversation", {"max_tokens": 8192}), _TASK_CFG)
        assert out["ok"] is True and out["checks"][0]["value"] == 8192

    def test_google_default_is_ok(self):
        out = _read(*_limits_hass("google_generative_ai_conversation", {}), _TASK_CFG)
        assert out["ok"] is True and out["checks"][0]["value"] == 19400

    def test_ollama_compares_num_ctx_with_combined(self):
        out = _read(*_limits_hass("ollama", {}), _TASK_CFG)
        assert out["checks"] == [
            {"kind": "combined", "setting": "num_ctx", "value": 8192, "required": 24576, "ok": False}
        ]
        assert _read(*_limits_hass("ollama", {"num_ctx": 32768}), _TASK_CFG)["ok"] is True

    def test_other_integration_is_unreadable(self):
        out = _read(*_limits_hass("open_router", {}), _TASK_CFG)
        assert out == {
            "provider": AI_PROVIDER_HA_AI_TASK,
            "entity_id": "ai_task.x",
            "integration": "open_router",
            "readable": False,
            "checks": [],
            "ok": True,
        }

    def test_garbage_value_is_unreadable(self):
        out = _read(*_limits_hass("anthropic", {"max_tokens": "lots"}), _TASK_CFG)
        assert out["readable"] is False and out["ok"] is True

    def test_absent_preferred_entity_is_unreadable(self):
        hass, registry = _limits_hass("anthropic", {}, preferred=None)
        out = _read(hass, registry, {**_TASK_CFG, "ai_task_entity": ""})
        assert out["readable"] is False and out["entity_id"] is None

    def test_preferred_entity_is_used_when_none_chosen(self):
        hass, registry = _limits_hass("anthropic", {"max_tokens": 9000}, preferred="ai_task.pref")
        out = _read(hass, registry, {**_TASK_CFG, "ai_task_entity": ""})
        assert out["entity_id"] == "ai_task.pref" and out["ok"] is True

    def test_entity_not_in_registry_is_unreadable(self):
        out = _read(*_limits_hass(None, None), _TASK_CFG)
        assert out["readable"] is False and out["integration"] is None

    def test_builtin_reports_its_own_max_tokens(self):
        out = _read(MagicMock(), MagicMock(), {"ai_api_key": "k", "ai_max_tokens": 4096})
        assert out["provider"] == AI_PROVIDER_BUILTIN_CLAUDE
        assert out["checks"] == [
            {"kind": "output", "setting": "max_tokens", "value": 4096, "required": 8192, "ok": False}
        ]


# ---------------------------------------------------------------------------
# Migration v20 -> v21
# ---------------------------------------------------------------------------


def _entry(entry_id: str, title: str, data: dict, version: int = 20):
    return SimpleNamespace(entry_id=entry_id, title=title, data=dict(data), version=version, unique_id=entry_id)


def _migrate_all(entries):
    """Run the real async_migrate_entry over every entry in stable order; returns created issues."""
    from custom_components.climate_advisor import async_migrate_entry

    hass = MagicMock()
    hass.config_entries.async_entries.return_value = entries

    def _update(entry, *, data, version, unique_id=None):
        entry.data = dict(data)
        entry.version = version

    hass.config_entries.async_update_entry.side_effect = _update
    with patch("custom_components.climate_advisor.ir.async_create_issue") as create:
        for e in entries:
            assert asyncio.run(async_migrate_entry(hass, e)) is True
    return create


_ON_KEY = {"ai_enabled": True, "ai_api_key": "sk-1", "ai_model": "claude-sonnet-4-6", "ai_reasoning_effort": "high"}
_OFF = {"ai_enabled": False, "ai_api_key": "", "ai_model": "claude-sonnet-4-6", "ai_reasoning_effort": "medium"}


class TestMigrationV21:
    def test_no_zone_configured(self):
        a, b = _entry("a", "A", _OFF), _entry("b", "B", {**_OFF, "ai_reasoning_effort": "high"})
        create = _migrate_all([a, b])
        assert a.version == b.version == 21
        assert a.data["ai_provider"] == AI_PROVIDER_HA_AI_TASK
        assert b.data["ai_reasoning_effort"] == "medium"
        assert b.data[AI_SETTINGS_BACKUP_KEY]["ai_reasoning_effort"] == "high"  # original kept
        create.assert_not_called()

    def test_one_zone_configured_copies_to_all(self):
        a, b = _entry("a", "A", _OFF), _entry("b", "B", _ON_KEY)
        create = _migrate_all([a, b])
        for e in (a, b):
            assert e.data["ai_enabled"] is True
            assert e.data["ai_api_key"] == "sk-1"
            assert e.data["ai_provider"] == AI_PROVIDER_BUILTIN_CLAUDE  # a key stays on built-in Claude
            assert e.data["ai_reasoning_effort"] == "medium"
        assert a.data[AI_SETTINGS_BACKUP_KEY]["ai_enabled"] is False  # zone A's own originals kept
        assert b.data[AI_SETTINGS_BACKUP_KEY]["ai_reasoning_effort"] == "high"
        create.assert_not_called()

    def test_same_values_no_conflict(self):
        a, b = _entry("a", "A", _ON_KEY), _entry("b", "B", _ON_KEY)
        create = _migrate_all([a, b])
        assert a.data["ai_api_key"] == b.data["ai_api_key"] == "sk-1"
        create.assert_not_called()

    def test_different_values_keep_first_and_raise_issue(self):
        a = _entry("a", "Upstairs", _ON_KEY)
        b = _entry("b", "Downstairs", {**_ON_KEY, "ai_api_key": "sk-2", "ai_model": "claude-haiku-4-5"})
        create = _migrate_all([a, b])
        assert b.data["ai_api_key"] == "sk-1"
        assert b.data["ai_model"] == "claude-sonnet-4-6"
        assert b.data[AI_SETTINGS_BACKUP_KEY]["ai_api_key"] == "sk-2"  # nothing lost
        create.assert_called()
        args, kwargs = create.call_args
        assert args[2] == ISSUE_AI_SETTINGS_UNIFIED
        assert kwargs["translation_key"] == ISSUE_AI_SETTINGS_UNIFIED
        assert kwargs["translation_placeholders"] == {"zone": "Upstairs"}
        assert kwargs["is_fixable"] is False

    def test_order_independent(self):
        """The second zone migrating first gives the same result (judged by original values)."""
        a = _entry("a", "Upstairs", _ON_KEY)
        b = _entry("b", "Downstairs", {**_ON_KEY, "ai_api_key": "sk-2"})
        from custom_components.climate_advisor import async_migrate_entry

        hass = MagicMock()
        hass.config_entries.async_entries.return_value = [a, b]

        def _update(entry, *, data, version, unique_id=None):
            entry.data = dict(data)
            entry.version = version

        hass.config_entries.async_update_entry.side_effect = _update
        with patch("custom_components.climate_advisor.ir.async_create_issue"):
            asyncio.run(async_migrate_entry(hass, b))
            asyncio.run(async_migrate_entry(hass, a))
        assert a.data["ai_api_key"] == b.data["ai_api_key"] == "sk-1"

    def test_enabled_without_key_becomes_ai_task(self):
        a = _entry("a", "A", {"ai_enabled": True, "ai_api_key": ""})
        _migrate_all([a])
        assert a.data["ai_provider"] == AI_PROVIDER_HA_AI_TASK


# ---------------------------------------------------------------------------
# Shared client lifecycle (real async_setup_entry / async_unload_entry)
# ---------------------------------------------------------------------------


class TestSharedClientLifecycle:
    def test_one_client_for_all_zones_removed_with_last(self):
        from tools.sim_harness._loop import run_coro
        from tools.sim_harness.build_coordinator import async_unload_entry_like_ha, build_headless_multi_zone

        ai = {"ai_enabled": True, "ai_provider": AI_PROVIDER_HA_AI_TASK, "ai_task_entity": "ai_task.x"}
        zones, fake_hass, _ = build_headless_multi_zone(
            configs=[
                {"climate_entity": "climate.a", "zone_title": "A", **ai},
                {"climate_entity": "climate.b", "zone_title": "B", **ai},
            ]
        )
        coords = [z["coordinator"] for z in zones.values()]
        shared = fake_hass.data[AI_SHARED_DATA_KEY]
        assert coords[0].claude_client is coords[1].claude_client is shared["client"]
        assert coords[0].ai_skills is coords[1].ai_skills is shared["skills"]
        assert shared["client"].hass is fake_hass
        assert shared["client"].uses_ai_task is True

        entries = [z["entry"] for z in zones.values()]
        run_coro(async_unload_entry_like_ha(fake_hass, entries[0]))
        assert AI_SHARED_DATA_KEY in fake_hass.data  # a zone still uses it
        run_coro(async_unload_entry_like_ha(fake_hass, entries[1]))
        assert AI_SHARED_DATA_KEY not in fake_hass.data


class TestDeprecationIssue:
    def _run(self, datas):
        from custom_components.climate_advisor import _update_builtin_claude_deprecation_issue

        hass = MagicMock()
        hass.config_entries.async_entries.return_value = [SimpleNamespace(data=d) for d in datas]
        with (
            patch("custom_components.climate_advisor.ir.async_create_issue") as create,
            patch("custom_components.climate_advisor.ir.async_delete_issue") as delete,
        ):
            _update_builtin_claude_deprecation_issue(hass)
        return create, delete

    def test_raised_while_builtin_claude_in_use(self):
        create, delete = self._run([{"ai_enabled": True, "ai_api_key": "k", "ai_provider": "builtin_claude"}])
        args, kwargs = create.call_args
        assert args[2] == "builtin_claude_deprecated" == kwargs["translation_key"]
        assert kwargs["is_fixable"] is False
        delete.assert_not_called()

    def test_deleted_on_ai_task_or_ai_off(self):
        create, delete = self._run(
            [{"ai_enabled": True, "ai_provider": "ha_ai_task"}, {"ai_enabled": False, "ai_api_key": "k"}]
        )
        create.assert_not_called()
        delete.assert_called_once()
