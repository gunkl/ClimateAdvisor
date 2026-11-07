"""ChatGPTBackend against a real aiohttp server on localhost (no mocks, never OpenAI).

The tests/ tier stubs aiohttp, so real exception classes (ClientPayloadError,
ClientConnectorError, ...) and real chunked-stream behaviour are only proven here.
The production base URL is a fixed constant; only this test repoints it.
"""

from __future__ import annotations

import json
import logging
import socket

import aiohttp
import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

from custom_components.climate_advisor import chatgpt_backend
from custom_components.climate_advisor.ai_backend import AIBackendError
from custom_components.climate_advisor.chatgpt_backend import ChatGPTBackend, ChatGPTBackendError

TOKEN = "at_SECRETTOKEN_do_not_log_0123456789"
MODEL = "gpt-test"


def _sse(*events: dict) -> bytes:
    return "".join(f"event: {e['type']}\ndata: {json.dumps(e)}\n\n" for e in events).encode()


COMPLETED = {
    "type": "response.completed",
    "response": {"usage": {"input_tokens": 11, "output_tokens": 7}, "output": []},
}


class Account:
    """The injected account manager: a fixed token and recorded calls."""

    def __init__(self) -> None:
        self.hass = None
        self.cooldowns: list = []
        self.successes = 0
        self.account_identity = ("owner", "subject")
        self.quota_epoch = 0
        self.refresh_generation = 0

    async def async_get_access_token(self, force_refresh: bool = False) -> str:
        return TOKEN

    def quota_cooldown_active(self) -> bool:
        return False

    def set_quota_cooldown(self, code=None) -> None:
        self.cooldowns.append(code)

    def note_request_succeeded(self, started_epoch=None) -> None:
        self.successes += 1


class Server:
    def __init__(self) -> None:
        self.requests: list[dict] = []
        self.handler = None


@pytest.fixture
async def served(monkeypatch, socket_enabled):
    state = Server()

    async def responses(request: web.Request) -> web.StreamResponse:
        state.requests.append({"headers": dict(request.headers), "body": await request.json()})
        return await state.handler(request)

    app = web.Application()
    app.router.add_post("/v1/responses", responses)
    server = TestServer(app)
    await server.start_server()
    session = aiohttp.ClientSession()
    monkeypatch.setattr(chatgpt_backend, "RESPONSES_URL", str(server.make_url("/v1/responses")))
    monkeypatch.setattr(chatgpt_backend, "_get_session", lambda _hass: session)
    yield state
    await session.close()
    await server.close()


def _backend() -> tuple[ChatGPTBackend, Account]:
    account = Account()
    return ChatGPTBackend({}, account), account


async def _collect(backend: ChatGPTBackend, out: list) -> None:
    async for event in backend.async_stream(system_prompt="sys", user_message="hi", model=MODEL):
        out.append(event)


def _stream_handler(*chunks: bytes, status: int = 200):
    async def handler(request: web.Request) -> web.StreamResponse:
        resp = web.StreamResponse(status=status, headers={"Content-Type": "text/event-stream"})
        await resp.prepare(request)
        for chunk in chunks:
            await resp.write(chunk)
        return resp

    return handler


class TestRealStream:
    async def test_full_stream_ends_in_stop(self, served, caplog):
        caplog.set_level(logging.DEBUG)
        served.handler = _stream_handler(
            _sse({"type": "response.reasoning_summary_text.delta", "delta": "think"}),
            _sse({"type": "response.output_text.delta", "delta": "Hel"}),
            _sse({"type": "response.output_text.delta", "delta": "lo"}),
            _sse(COMPLETED),
        )
        backend, account = _backend()
        events: list = []
        await _collect(backend, events)
        assert [e["type"] for e in events] == ["thinking", "text", "text", "stop"]
        assert "".join(e["text"] for e in events if e["type"] == "text") == "Hello"
        assert events[-1]["input_tokens"] == 11
        assert events[-1]["output_tokens"] == 7
        assert account.successes == 1
        assert TOKEN not in caplog.text

    async def test_request_shape_and_authorization(self, served):
        served.handler = _stream_handler(_sse({"type": "response.output_text.delta", "delta": "x"}, COMPLETED))
        backend, _ = _backend()
        await _collect(backend, [])
        (request,) = served.requests
        body = request["body"]
        assert body["store"] is False
        assert body["stream"] is True
        assert body["model"] == MODEL
        for key in ("temperature", "max_output_tokens", "max_tokens", "reasoning", "max_completion_tokens"):
            assert key not in body
        assert request["headers"]["Authorization"] == f"Bearer {TOKEN}"

    async def test_request_aggregates_into_a_response(self, served):
        served.handler = _stream_handler(_sse({"type": "response.output_text.delta", "delta": "ok"}), _sse(COMPLETED))
        backend, _ = _backend()
        result = await backend.async_request(system_prompt="s", user_message="u", model=MODEL)
        assert result.success is True
        assert result.content == "ok"

    async def test_mid_stream_failed_raises_before_stop(self, served, caplog):
        caplog.set_level(logging.DEBUG)
        served.handler = _stream_handler(
            _sse({"type": "response.output_text.delta", "delta": "partial"}),
            _sse({"type": "response.failed", "response": {"error": {"code": "server_error"}}}),
        )
        backend, _ = _backend()
        events: list = []
        with pytest.raises(ChatGPTBackendError) as exc:
            await _collect(backend, events)
        assert "stop" not in [e["type"] for e in events]
        assert exc.value.category == "permanent"
        assert TOKEN not in str(exc.value)
        assert TOKEN not in caplog.text

    async def test_dropped_connection_mid_stream_is_transient_before_stop(self, served, caplog):
        caplog.set_level(logging.DEBUG)

        async def handler(request: web.Request) -> web.StreamResponse:
            resp = web.StreamResponse(headers={"Content-Type": "text/event-stream"})
            await resp.prepare(request)
            await resp.write(_sse({"type": "response.output_text.delta", "delta": "part"}))
            request.transport.abort()  # no terminating chunk: a real truncated body
            return resp

        served.handler = handler
        backend, _ = _backend()
        events: list = []
        with pytest.raises(ChatGPTBackendError) as exc:
            await _collect(backend, events)
        assert exc.value.category == "transient"
        assert "stop" not in [e["type"] for e in events]
        assert TOKEN not in str(exc.value)
        assert TOKEN not in caplog.text

    async def test_clean_end_without_terminal_event_is_transient(self, served):
        served.handler = _stream_handler(_sse({"type": "response.output_text.delta", "delta": "x"}))
        backend, _ = _backend()
        events: list = []
        with pytest.raises(ChatGPTBackendError) as exc:
            await _collect(backend, events)
        assert exc.value.category == "transient"
        assert "stop" not in [e["type"] for e in events]

    async def test_usage_limit_429_is_quota_and_not_retried(self, served, caplog):
        caplog.set_level(logging.DEBUG)

        async def handler(request: web.Request) -> web.Response:
            return web.json_response({"error": {"code": chatgpt_backend.USAGE_LIMIT_CODE}}, status=429)

        served.handler = handler
        backend, account = _backend()
        with pytest.raises(ChatGPTBackendError) as exc:
            await _collect(backend, [])
        assert exc.value.category == "quota"
        assert exc.value.no_retry is True
        assert exc.value.status == 429
        assert exc.value.error_code == chatgpt_backend.USAGE_LIMIT_CODE
        assert len(served.requests) == 1
        assert account.cooldowns == [chatgpt_backend.USAGE_LIMIT_CODE]
        assert TOKEN not in caplog.text

    async def test_other_429_is_rate_limit_and_reads_a_real_retry_after_header(self, served):
        async def handler(request: web.Request) -> web.Response:
            body = {"error": {"type": "rate_limit_error", "code": "slow_down"}}
            return web.json_response(body, status=429, headers={"retry-after": "56"})

        served.handler = handler
        backend, account = _backend()
        with pytest.raises(ChatGPTBackendError) as exc:
            await _collect(backend, [])
        assert exc.value.category == "rate_limit"
        assert exc.value.no_retry is True
        assert exc.value.error_code == "slow_down"
        assert "56 seconds" in str(exc.value)
        assert len(served.requests) == 1
        assert account.cooldowns == []


class TestRealConnectionRefused:
    async def test_refused_is_transient(self, monkeypatch, caplog, socket_enabled):
        caplog.set_level(logging.DEBUG)
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        # The socket is closed, so nothing listens on the port.
        monkeypatch.setattr(chatgpt_backend, "RESPONSES_URL", f"http://127.0.0.1:{port}/v1/responses")
        async with aiohttp.ClientSession() as session:
            monkeypatch.setattr(chatgpt_backend, "_get_session", lambda _hass: session)
            backend, _ = _backend()
            with pytest.raises(AIBackendError) as exc:
                await _collect(backend, [])
        assert exc.value.category == "transient"
        assert TOKEN not in str(exc.value)
        assert TOKEN not in caplog.text


class TestRedirectsAreRefused:
    @pytest.fixture
    async def redirecting(self, monkeypatch, socket_enabled):
        """Both API routes answer with a redirect to a route that records whatever reaches it."""
        reached: list[bytes] = []

        async def target(request: web.Request) -> web.Response:
            reached.append(await request.read())
            return web.json_response({"models": []})

        async def redirect(request: web.Request) -> web.Response:
            return web.Response(status=request.app["redirect_status"], headers={"Location": "/elsewhere"})

        app = web.Application()
        app["redirect_status"] = 307
        app.router.add_post("/v1/responses", redirect)
        app.router.add_get("/v1/models", redirect)
        app.router.add_route("*", "/elsewhere", target)
        server = TestServer(app)
        await server.start_server()
        session = aiohttp.ClientSession()
        monkeypatch.setattr(chatgpt_backend, "RESPONSES_URL", str(server.make_url("/v1/responses")))
        monkeypatch.setattr(chatgpt_backend, "MODELS_URL", str(server.make_url("/v1/models")))
        monkeypatch.setattr(chatgpt_backend, "_get_session", lambda _hass: session)
        yield reached
        await session.close()
        await server.close()

    async def test_inference_redirect_sends_nothing_to_the_target(self, redirecting):
        backend, _ = _backend()
        with pytest.raises(ChatGPTBackendError) as err:
            await _collect(backend, [])
        assert err.value.status == 307
        assert redirecting == []

    async def test_model_list_redirect_sends_nothing_to_the_target(self, redirecting):
        backend, _ = _backend()
        with pytest.raises(ChatGPTBackendError) as err:
            await backend.async_fetch_models()
        assert err.value.status == 307
        assert redirecting == []
