"""Tests for the AISkillRegistry framework (ai_skills.py)."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

from custom_components.climate_advisor.ai_skills import AISkillDefinition, AISkillRegistry
from custom_components.climate_advisor.claude_api import ClaudeResponse

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _success_response(content: str = "test response") -> ClaudeResponse:
    """Build a successful ClaudeResponse for use in tests."""
    return ClaudeResponse(
        success=True,
        content=content,
        input_tokens=10,
        output_tokens=20,
        estimated_cost=0.001,
        latency_ms=100.0,
    )


def _failed_response(error: str = "API error") -> ClaudeResponse:
    """Build a failed ClaudeResponse for use in tests."""
    return ClaudeResponse(
        success=False,
        content="",
        input_tokens=0,
        output_tokens=0,
        estimated_cost=0.0,
        latency_ms=0.0,
        error=error,
    )


def _make_skill(
    name: str = "test_skill",
    description: str = "A test skill",
    context_builder=None,
    response_parser=None,
    fallback=None,
    triggered_by: str = "manual",
) -> AISkillDefinition:
    """Build an AISkillDefinition with sensible defaults."""
    if context_builder is None:

        async def context_builder(hass, coordinator, **kwargs):
            return "test context"

    if response_parser is None:

        def response_parser(raw: str) -> dict:
            return {"parsed": raw}

    return AISkillDefinition(
        name=name,
        description=description,
        system_prompt="You are a test assistant.",
        context_builder=context_builder,
        response_parser=response_parser,
        fallback=fallback,
        triggered_by=triggered_by,
    )


def _make_claude_client(response: ClaudeResponse) -> MagicMock:
    """Build a mock ClaudeAPIClient that returns a fixed response."""
    client = MagicMock()
    client.async_request = AsyncMock(return_value=response)
    return client


# ---------------------------------------------------------------------------
# TestAISkillRegistryBasics
# ---------------------------------------------------------------------------


class TestAISkillRegistryBasics:
    """Tests for register, get, and list_skills."""

    def test_register_and_get_skill(self):
        """Registering a skill and getting it by name returns the same object."""
        registry = AISkillRegistry()
        skill = _make_skill(name="my_skill", description="Does a thing")
        registry.register(skill)

        result = registry.get("my_skill")
        assert result is skill
        assert result.name == "my_skill"
        assert result.description == "Does a thing"
        assert result.triggered_by == "manual"

    def test_list_skills(self):
        """list_skills returns name and description for every registered skill."""
        registry = AISkillRegistry()
        registry.register(_make_skill(name="skill_a", description="Alpha"))
        registry.register(_make_skill(name="skill_b", description="Beta"))

        listing = registry.list_skills()
        assert len(listing) == 2

        names = {entry["name"] for entry in listing}
        descs = {entry["description"] for entry in listing}
        assert names == {"skill_a", "skill_b"}
        assert "Alpha" in descs
        assert "Beta" in descs

    def test_get_unknown_skill(self):
        """get() with an unknown skill name returns None."""
        registry = AISkillRegistry()
        assert registry.get("nonexistent") is None

    def test_register_replaces_existing_skill(self):
        """Re-registering a skill under the same name replaces the previous one."""
        registry = AISkillRegistry()
        first = _make_skill(name="dup", description="First")
        second = _make_skill(name="dup", description="Second")
        registry.register(first)
        registry.register(second)

        assert registry.get("dup") is second
        assert len(registry.list_skills()) == 1


# ---------------------------------------------------------------------------
# TestAISkillRegistryExecute
# ---------------------------------------------------------------------------


class TestAISkillRegistryExecute:
    """Tests for async_execute."""

    def test_execute_success(self):
        """Successful AI response: result has success=True, source='ai', and parsed data."""
        registry = AISkillRegistry()

        def parser(raw: str) -> dict:
            return {"summary": raw, "extra": "value"}

        skill = _make_skill(name="success_skill", response_parser=parser)
        registry.register(skill)

        client = _make_claude_client(_success_response(content="Great analysis here"))
        hass = MagicMock()
        coordinator = MagicMock()

        result = asyncio.run(registry.async_execute("success_skill", hass, coordinator, client))

        assert result["success"] is True
        assert result["source"] == "ai"
        assert result["error"] is None
        assert result["data"]["summary"] == "Great analysis here"
        assert result["data"]["extra"] == "value"
        assert result["raw_response"] == "Great analysis here"
        assert result["input_context"] == "test context"

    def test_execute_with_fallback_on_ai_failure(self):
        """When AI fails and a fallback is registered, result comes from fallback."""
        registry = AISkillRegistry()

        def fallback(coordinator, **kwargs) -> dict:
            return {"summary": "fallback summary", "source_note": "local data"}

        skill = _make_skill(name="fallback_skill", fallback=fallback)
        registry.register(skill)

        client = _make_claude_client(_failed_response("Timeout"))
        hass = MagicMock()
        coordinator = MagicMock()

        result = asyncio.run(registry.async_execute("fallback_skill", hass, coordinator, client))

        assert result["success"] is True
        assert result["source"] == "fallback"
        assert result["error"] is None
        assert result["data"]["summary"] == "fallback summary"
        assert result["data"]["source_note"] == "local data"
        # raw_response is empty when using fallback
        assert result["raw_response"] == ""

    def test_execute_unknown_skill(self):
        """Executing an unknown skill returns success=False and source='error'."""
        registry = AISkillRegistry()
        client = _make_claude_client(_success_response())
        hass = MagicMock()
        coordinator = MagicMock()

        result = asyncio.run(registry.async_execute("not_registered", hass, coordinator, client))

        assert result["success"] is False
        assert result["source"] == "error"
        assert "not_registered" in result["error"]
        assert result["data"] == {}

    def test_execute_context_builder_fails(self):
        """When context_builder raises, fallback is used if available."""
        registry = AISkillRegistry()

        async def bad_context(hass, coordinator, **kwargs) -> str:
            raise RuntimeError("Context build error")

        def fallback(coordinator, **kwargs) -> dict:
            return {"summary": "safe fallback"}

        skill = _make_skill(
            name="bad_context_skill",
            context_builder=bad_context,
            fallback=fallback,
        )
        registry.register(skill)

        client = _make_claude_client(_success_response())
        hass = MagicMock()
        coordinator = MagicMock()

        result = asyncio.run(registry.async_execute("bad_context_skill", hass, coordinator, client))

        assert result["success"] is True
        assert result["source"] == "fallback"
        assert result["data"]["summary"] == "safe fallback"

    def test_execute_context_builder_fails_no_fallback(self):
        """When context_builder raises and no fallback exists, returns error result."""
        registry = AISkillRegistry()

        async def bad_context(hass, coordinator, **kwargs) -> str:
            raise RuntimeError("Context build error")

        skill = _make_skill(name="no_fb_skill", context_builder=bad_context, fallback=None)
        registry.register(skill)

        client = _make_claude_client(_success_response())
        hass = MagicMock()
        coordinator = MagicMock()

        result = asyncio.run(registry.async_execute("no_fb_skill", hass, coordinator, client))

        assert result["success"] is False
        assert result["source"] == "error"

    def test_execute_no_fallback_on_failure(self):
        """When AI fails and no fallback is registered, result has success=False."""
        registry = AISkillRegistry()
        skill = _make_skill(name="no_fallback_skill", fallback=None)
        registry.register(skill)

        client = _make_claude_client(_failed_response("Server error"))
        hass = MagicMock()
        coordinator = MagicMock()

        result = asyncio.run(registry.async_execute("no_fallback_skill", hass, coordinator, client))

        assert result["success"] is False
        assert result["source"] == "error"
        assert result["data"] == {}
        assert result["error"] is not None

    def test_execute_passes_kwargs_to_context_builder(self):
        """kwargs passed to async_execute are forwarded to the context_builder."""
        registry = AISkillRegistry()
        received_kwargs: dict = {}

        async def capturing_context(hass, coordinator, **kwargs) -> str:
            received_kwargs.update(kwargs)
            return "context with kwargs"

        skill = _make_skill(name="kwargs_skill", context_builder=capturing_context)
        registry.register(skill)

        client = _make_claude_client(_success_response())
        hass = MagicMock()
        coordinator = MagicMock()

        asyncio.run(registry.async_execute("kwargs_skill", hass, coordinator, client, report_id="abc", user="dave"))

        assert received_kwargs.get("report_id") == "abc"
        assert received_kwargs.get("user") == "dave"

    def test_execute_passes_kwargs_to_fallback(self):
        """kwargs passed to async_execute are forwarded to the fallback."""
        registry = AISkillRegistry()
        received_kwargs: dict = {}

        def capturing_fallback(coordinator, **kwargs) -> dict:
            received_kwargs.update(kwargs)
            return {"summary": "ok"}

        skill = _make_skill(name="fb_kwargs_skill", fallback=capturing_fallback)
        registry.register(skill)

        client = _make_claude_client(_failed_response())
        hass = MagicMock()
        coordinator = MagicMock()

        asyncio.run(registry.async_execute("fb_kwargs_skill", hass, coordinator, client, zone="upstairs"))

        assert received_kwargs.get("zone") == "upstairs"

    def test_execute_result_includes_input_context_on_ai_success(self):
        """The result dict always carries the context string that was sent to Claude."""
        registry = AISkillRegistry()

        async def fixed_context(hass, coordinator, **kwargs) -> str:
            return "my specific context block"

        skill = _make_skill(name="ctx_skill", context_builder=fixed_context)
        registry.register(skill)

        client = _make_claude_client(_success_response(content="AI said something"))
        hass = MagicMock()
        coordinator = MagicMock()

        result = asyncio.run(registry.async_execute("ctx_skill", hass, coordinator, client))

        assert result["input_context"] == "my specific context block"

    def test_execute_result_includes_input_context_on_fallback(self):
        """The context string is preserved in the result even when falling back."""
        registry = AISkillRegistry()

        async def fixed_context(hass, coordinator, **kwargs) -> str:
            return "context before failure"

        def fallback(coordinator, **kwargs) -> dict:
            return {"summary": "fallback"}

        skill = _make_skill(
            name="ctx_fb_skill",
            context_builder=fixed_context,
            fallback=fallback,
        )
        registry.register(skill)

        client = _make_claude_client(_failed_response())
        hass = MagicMock()
        coordinator = MagicMock()

        result = asyncio.run(registry.async_execute("ctx_fb_skill", hass, coordinator, client))

        assert result["input_context"] == "context before failure"


# ---------------------------------------------------------------------------
# TestAISkillRegistryStreaming
# ---------------------------------------------------------------------------


def _make_streaming_client(chunks: list[str], raise_on_stream: Exception | None = None) -> MagicMock:
    """Build a mock client whose async_request_streaming yields given chunks."""

    async def _streaming_gen(*_args, **_kwargs):
        if raise_on_stream is not None:
            raise raise_on_stream
        for chunk in chunks:
            yield chunk

    client = MagicMock()
    client.async_request_streaming = _streaming_gen
    return client


class TestAISkillRegistryStreaming:
    """Tests for async_execute_streaming() — the SSE streaming path."""

    def test_streaming_yields_chunks_then_done(self):
        """Streaming a successful response yields chunk events then a done event."""
        registry = AISkillRegistry()

        def parser(raw: str) -> dict:
            return {"summary": raw}

        skill = _make_skill(name="stream_skill", response_parser=parser)
        registry.register(skill)

        client = _make_streaming_client(["Hello, ", "world!"])
        hass = MagicMock()
        coordinator = MagicMock()

        async def _collect():
            events = []
            async for ev in registry.async_execute_streaming("stream_skill", hass, coordinator, client):
                events.append(ev)
            return events

        events = asyncio.run(_collect())

        chunk_events = [e for e in events if e.get("type") == "chunk"]
        done_events = [e for e in events if e.get("type") == "done"]
        error_events = [e for e in events if e.get("type") == "error"]

        assert len(chunk_events) == 2, f"Expected 2 chunk events, got: {chunk_events}"
        assert chunk_events[0]["text"] == "Hello, "
        assert chunk_events[1]["text"] == "world!"
        assert len(done_events) == 1, f"Expected 1 done event, got: {done_events}"
        assert done_events[0]["success"] is True
        assert done_events[0]["source"] == "ai"
        assert done_events[0]["data"] == {"summary": "Hello, world!"}
        assert done_events[0]["raw_response"] == "Hello, world!"
        assert not error_events

    def test_streaming_unknown_skill_yields_error(self):
        """async_execute_streaming with an unknown skill name yields a single error event."""
        registry = AISkillRegistry()
        client = _make_streaming_client([])
        hass = MagicMock()
        coordinator = MagicMock()

        async def _collect():
            events = []
            async for ev in registry.async_execute_streaming("no_such_skill", hass, coordinator, client):
                events.append(ev)
            return events

        events = asyncio.run(_collect())

        assert len(events) == 1
        assert events[0]["type"] == "error"
        assert "no_such_skill" in events[0]["message"] or "Unknown" in events[0]["message"]

    def test_streaming_api_error_yields_error_event(self):
        """If async_request_streaming raises, the generator yields an error event."""
        registry = AISkillRegistry()
        skill = _make_skill(name="err_skill")
        registry.register(skill)

        client = _make_streaming_client([], raise_on_stream=RuntimeError("Circuit breaker open"))
        hass = MagicMock()
        coordinator = MagicMock()

        async def _collect():
            events = []
            async for ev in registry.async_execute_streaming("err_skill", hass, coordinator, client):
                events.append(ev)
            return events

        events = asyncio.run(_collect())

        error_events = [e for e in events if e.get("type") == "error"]
        done_events = [e for e in events if e.get("type") == "done"]
        assert error_events, f"Expected error event, got: {events}"
        assert not done_events
