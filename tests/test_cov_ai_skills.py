"""Coverage for AISkillRegistry's failure paths (ai_skills.py).

Occupant view: when the AI provider answers with something the parser cannot read, the context cannot be built, or
the fallback itself breaks, the occupant still gets a clear result (the deterministic fallback, or an error they
can see) and the dashboard's progress stream ends with an error or a done event, never a hang or a crash.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

from custom_components.climate_advisor.ai_skills import (
    STATUS_KEY_GATHERING,
    AISkillDefinition,
    AISkillRegistry,
    _maybe_persist_model_fallback,
    status_event,
)
from custom_components.climate_advisor.claude_api import ClaudeResponse


def _ok(content: str = "raw") -> ClaudeResponse:
    return ClaudeResponse(
        success=True, content=content, input_tokens=1, output_tokens=1, estimated_cost=0.0, latency_ms=1.0
    )


def _skill(*, context_builder=None, response_parser=None, fallback=None) -> AISkillDefinition:
    async def _ctx(hass, coordinator, **kwargs):
        return "ctx"

    return AISkillDefinition(
        name="s",
        description="d",
        system_prompt="sys",
        context_builder=context_builder or _ctx,
        response_parser=response_parser or (lambda raw: {"parsed": raw}),
        fallback=fallback,
    )


def _bad_parser(raw: str) -> dict:
    raise ValueError("unparseable")


def _registry(skill: AISkillDefinition) -> AISkillRegistry:
    reg = AISkillRegistry()
    reg.register(skill)
    return reg


def _stream(reg: AISkillRegistry, client, coordinator=None) -> list[dict]:
    async def _collect():
        return [e async for e in reg.async_execute_streaming("s", MagicMock(), coordinator or MagicMock(), client)]

    return asyncio.run(_collect())


def _streaming_client(events: list[dict] | None = None, raises: BaseException | None = None):
    async def _gen(**kwargs):
        for e in events or []:
            yield e
        if raises is not None:
            raise raises

    client = MagicMock()
    client.async_request_streaming = _gen
    return client


def test_status_event_carries_params_copy():
    params = {"n": 1}
    event = status_event("t", "k", params)
    assert event == {"type": "status", "text": "t", "key": "k", "params": {"n": 1}}
    assert event["params"] is not params


class TestNonStreaming:
    def test_unparseable_reply_falls_back_with_no_error_code(self):
        fallback = MagicMock(return_value={"fb": True})
        client = MagicMock()
        client.async_request = AsyncMock(return_value=_ok())
        coord = MagicMock(config={})
        result = asyncio.run(
            _registry(_skill(response_parser=_bad_parser, fallback=fallback)).async_execute(
                "s", MagicMock(), coord, client
            )
        )
        assert result["source"] == "fallback" and result["data"] == {"fb": True}
        assert result["error_code"] is None

    def test_fallback_failure_reports_both_failed(self):
        def _boom(coordinator, **kwargs):
            raise RuntimeError("fallback broke")

        client = MagicMock()
        client.async_request = AsyncMock(
            return_value=ClaudeResponse(
                success=False, content="", input_tokens=0, output_tokens=0, estimated_cost=0.0, latency_ms=0.0
            )
        )
        result = asyncio.run(
            _registry(_skill(fallback=_boom)).async_execute("s", MagicMock(), MagicMock(config={}), client)
        )
        assert result["success"] is False and result["source"] == "error"
        assert result["error"] == "Both AI and fallback failed for s"
        assert result["input_context"] == "ctx"


class TestModelFallbackPersistence:
    def test_coordinator_without_persist_hook_is_left_alone(self):
        coord = object()  # no async_persist_model_fallback attribute
        response = MagicMock(resolved_model="claude-sonnet-other")
        # Must not raise even though the coordinator cannot persist the substitution.
        assert asyncio.run(_maybe_persist_model_fallback(coord, "claude-sonnet-4-6", {}, response)) is None

    def test_substitution_is_persisted(self):
        coord = MagicMock()
        coord.async_persist_model_fallback = AsyncMock()
        response = MagicMock(resolved_model="claude-sonnet-other")
        asyncio.run(_maybe_persist_model_fallback(coord, "claude-sonnet-4-6", {}, response))
        coord.async_persist_model_fallback.assert_awaited_once_with("claude-sonnet-other")


class TestStreaming:
    def test_queued_section_status_is_drained_before_sending(self):
        async def _ctx(hass, coordinator, status_callback=None, **kwargs):
            await status_callback("Loading a…")
            await status_callback("Loading b…")
            return "ctx"

        events = _stream(
            _registry(_skill(context_builder=_ctx)),
            _streaming_client([{"type": "text", "text": "hi"}, {"type": "stop", "stop_reason": "end_turn"}]),
        )
        texts = [e["text"] for e in events if e["type"] == "status" and e["key"] == STATUS_KEY_GATHERING]
        assert "Loading a…" in texts and "Loading b…" in texts
        assert events[-1]["type"] == "done" and events[-1]["raw_response"] == "hi"

    def test_context_builder_failure_ends_with_error(self):
        async def _ctx(hass, coordinator, **kwargs):
            raise RuntimeError("no data")

        events = _stream(_registry(_skill(context_builder=_ctx)), _streaming_client())
        assert events[-1] == {"type": "error", "message": "Context builder failed", "error_code": None}

    def test_unknown_stream_events_are_ignored(self):
        events = _stream(
            _registry(_skill()),
            _streaming_client(
                [
                    {"type": "ping"},
                    {"type": "text", "text": "ok"},
                    {"type": "stop", "stop_reason": "max_tokens", "truncated_empty": False},
                ]
            ),
        )
        assert not any(e["type"] == "ping" for e in events)
        done = events[-1]
        assert done["type"] == "done" and done["raw_response"] == "ok" and done["truncated"] is True

    def test_unparseable_streamed_reply_is_done_with_empty_data(self):
        events = _stream(
            _registry(_skill(response_parser=_bad_parser)),
            _streaming_client([{"type": "text", "text": "garbage"}, {"type": "stop", "stop_reason": "end_turn"}]),
        )
        done = events[-1]
        assert done["type"] == "done" and done["data"] == {} and done["raw_response"] == "garbage"

    def test_provider_failure_carries_its_error_code(self):
        exc = RuntimeError("limit")
        exc.error_code = "output_limit"
        events = _stream(_registry(_skill()), _streaming_client(raises=exc))
        assert events[-1] == {"type": "error", "message": "limit", "error_code": "output_limit"}
