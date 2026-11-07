"""Tests for chatgpt_backend.py with mocked HTTP and SSE fixtures (no real OpenAI calls)."""

from __future__ import annotations

import asyncio
import json
import logging
import sys
from typing import Any
from unittest.mock import MagicMock, patch

if "homeassistant" not in sys.modules:
    from conftest import _install_ha_stubs

    _install_ha_stubs()

import pytest  # noqa: E402

if "custom_components.climate_advisor.claude_api" not in sys.modules:
    _m = MagicMock()
    _m.__name__ = "anthropic"
    _m.__path__ = []
    _m.__file__ = None
    _m.__spec__ = None
    _m.__loader__ = None
    _m.__package__ = "anthropic"
    _m.APIError = type("APIError", (Exception,), {})
    _m.APITimeoutError = type("APITimeoutError", (Exception,), {})
    _m.RateLimitError = type("RateLimitError", (Exception,), {})
    _m.NotFoundError = type("NotFoundError", (_m.APIError,), {})
    _m.AsyncAnthropic = MagicMock()
    sys.modules["anthropic"] = _m

from custom_components.climate_advisor import chatgpt_backend as cb  # noqa: E402
from custom_components.climate_advisor.ai_backend import PROVIDERS, AIBackendError  # noqa: E402

TOKEN = "tok-SECRET-abc123"
TOKEN2 = "tok-REFRESHED-xyz789"


def sse(*events: dict[str, Any]) -> list[bytes]:
    lines: list[bytes] = []
    for ev in events:
        lines.append(f"event: {ev['type']}\n".encode())
        lines.append(f"data: {json.dumps(ev)}\n".encode())
        lines.append(b"\n")
    return lines


def delta(text: str) -> dict:
    return {"type": "response.output_text.delta", "delta": text}


def completed(usage: dict | None = None) -> dict:
    resp: dict[str, Any] = {"status": "completed"}
    if usage is not None:
        resp["usage"] = usage
    return {"type": "response.completed", "response": resp}


class FakeContent:
    def __init__(self, lines: list[bytes], raise_after: Exception | None = None) -> None:
        self._lines, self._raise = lines, raise_after

    def __aiter__(self):
        return self._gen()

    async def _gen(self):
        for line in self._lines:
            yield line
        if self._raise:
            raise self._raise


class FakeResp:
    def __init__(self, status=200, lines=None, body=None, headers=None, raise_after=None) -> None:
        self.status = status
        self.headers = {"x-request-id": "req_123", **(headers or {})}
        self.content = FakeContent(lines or [], raise_after)
        self._body = json.dumps(body).encode() if body is not None else b""

    async def read(self) -> bytes:
        return self._body

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False


class FakeSession:
    """Serves queued FakeResp objects; records each call."""

    def __init__(self, *responses: FakeResp) -> None:
        self.queue = list(responses)
        self.calls: list[dict[str, Any]] = []

    def _do(self, method, url, **kw):
        self.calls.append({"method": method, "url": url, **kw})
        return self.queue.pop(0)

    def post(self, url, **kw):
        return self._do("post", url, **kw)

    def get(self, url, **kw):
        return self._do("get", url, **kw)


class FakeManager:
    def __init__(self, cooldown: bool = False) -> None:
        self.refreshes = 0
        self.cooldown = cooldown
        self.cooldown_set: list[str | None] = []
        self.successes = 0
        self.cooldown_checks = 0
        self.fail_refresh: Exception | None = None
        self.account_identity: Any = ("owner", "subject")
        self.quota_epoch = 0
        self.refresh_generation = 0

    async def async_get_access_token(self, force_refresh: bool = False) -> str:
        if getattr(self, "fail_token", None):
            raise self.fail_token
        if force_refresh:
            self.refreshes += 1
            if self.fail_refresh:
                raise self.fail_refresh
            return TOKEN2
        return TOKEN

    def quota_cooldown_active(self) -> bool:
        self.cooldown_checks += 1
        return self.cooldown

    def note_request_succeeded(self, started_epoch=None) -> None:
        self.successes += 1

    def set_quota_cooldown(self, code) -> None:
        self.cooldown_set.append(code)


class FakeClientError(Exception):
    """Stands in for aiohttp.ClientError, a MagicMock in the stubbed environment."""


@pytest.fixture(autouse=True)
def _real_client_error(monkeypatch):
    monkeypatch.setattr(cb.aiohttp, "ClientError", FakeClientError)


@pytest.fixture(autouse=True)
def _no_sleep():
    sleeps: list[float] = []

    async def fake_sleep(s):
        sleeps.append(s)

    with patch.object(cb.asyncio, "sleep", fake_sleep):
        yield sleeps


def make(*responses: FakeResp, cooldown: bool = False):
    mgr = FakeManager(cooldown)
    session = FakeSession(*responses)
    backend = cb.ChatGPTBackend({}, mgr, hass=object())
    return backend, mgr, session


def run_stream(backend, session) -> tuple[list[dict], Exception | None]:
    events: list[dict] = []

    async def go():
        async for e in backend.async_stream(system_prompt="sys", user_message="usr", model="gpt-x"):
            events.append(e)

    with patch.object(cb, "_get_session", return_value=session):
        try:
            asyncio.run(go())
        except AIBackendError as exc:
            return events, exc
    return events, None


def run_request(backend, session):
    with patch.object(cb, "_get_session", return_value=session):
        return asyncio.run(backend.async_request(system_prompt="sys", user_message="usr", model="gpt-x"))


def assert_failed_before_stop(events, exc, category):
    assert exc is not None and exc.category == category
    assert all(e["type"] != "stop" for e in events)


class TestSuccess:
    def test_normal_stream(self):
        lines = sse(delta("Hel"), delta("lo"), completed({"input_tokens": 11, "output_tokens": 5}))
        backend, _, session = make(FakeResp(lines=lines))
        events, exc = run_stream(backend, session)
        assert exc is None
        assert [e["type"] for e in events] == ["text", "text", "stop"]
        assert events[-1]["input_tokens"] == 11 and events[-1]["output_tokens"] == 5
        assert events[-1]["estimated_cost"] == 0.0

    def test_request_aggregates(self):
        lines = sse(delta("Hel"), delta("lo"), completed({"input_tokens": 3, "output_tokens": 2}))
        backend, _, session = make(FakeResp(lines=lines))
        r = run_request(backend, session)
        assert r.success and r.content == "Hello" and r.input_tokens == 3 and r.output_tokens == 2
        assert r.estimated_cost == 0.0 and r.resolved_model == "gpt-x"

    def test_thinking_deltas(self):
        lines = sse(
            {"type": "response.reasoning_summary_text.delta", "delta": "pondering"},
            delta("ans"),
            completed({"input_tokens": 1, "output_tokens": 1}),
        )
        backend, _, session = make(FakeResp(lines=lines))
        events, _ = run_stream(backend, session)
        assert events[0] == {"type": "thinking", "text": "pondering"}
        assert events[1]["type"] == "text"

    def test_missing_usage_reports_zero(self):
        backend, _, session = make(FakeResp(lines=sse(delta("x"), completed())))
        events, exc = run_stream(backend, session)
        assert exc is None
        assert events[-1]["input_tokens"] == 0 and events[-1]["output_tokens"] == 0

    def test_final_text_used_when_no_deltas(self):
        done = {
            "type": "response.completed",
            "response": {"output": [{"content": [{"type": "output_text", "text": "whole"}]}]},
        }
        backend, _, session = make(FakeResp(lines=sse(done)))
        assert run_request(backend, session).content == "whole"

    def test_request_body_and_headers(self):
        backend, _, session = make(FakeResp(lines=sse(delta("x"), completed())))
        run_stream(backend, session)
        call = session.calls[0]
        assert call["url"] == "https://api.openai.com/v1/responses"
        body = call["json"]
        assert body["store"] is False and body["stream"] is True
        assert body["instructions"] == "sys"
        assert len(body["input"]) == 1 and body["input"][0]["role"] == "user"
        assert body["input"][0]["content"] == "usr"
        assert set(call["headers"]) == {"Authorization", "Content-Type"}
        for banned in ("temperature", "reasoning", "max_output_tokens", "max_tokens"):
            assert banned not in body
        assert call["headers"]["Authorization"] == f"Bearer {TOKEN}"
        assert "chatgpt.com" not in json.dumps(call, default=str)


class TestStreamFailures:
    def test_response_failed_midstream(self):
        lines = sse(
            delta("part"),
            {"type": "response.failed", "response": {"error": {"code": "subscription_sharing_usage_unavailable"}}},
        )
        backend, mgr, session = make(FakeResp(lines=lines))
        events, exc = run_stream(backend, session)
        assert_failed_before_stop(events, exc, "transient")
        assert exc.error_code == "subscription_sharing_usage_unavailable" and mgr.cooldown_set == []
        assert events == [{"type": "text", "text": "part"}]

    def test_failed_unknown_code_is_permanent_no_reauth(self):
        lines = sse({"type": "response.failed", "response": {"error": {"code": "mystery"}}})
        backend, _, session = make(FakeResp(lines=lines))
        events, exc = run_stream(backend, session)
        assert_failed_before_stop(events, exc, "permanent")
        assert exc.reauth_required is False

    def test_failed_request_raises_not_partial(self):
        lines = sse(delta("part"), {"type": "response.failed", "response": {"error": {"code": "bad"}}})
        backend, _, session = make(FakeResp(lines=lines))
        with patch.object(cb, "_get_session", return_value=session), pytest.raises(AIBackendError):
            asyncio.run(backend.async_request(system_prompt="s", user_message="u", model="m"))

    def test_failed_usage_limit_is_quota_and_cooldown(self):
        lines = sse({"type": "response.failed", "response": {"error": {"code": cb.USAGE_LIMIT_CODE}}})
        backend, mgr, session = make(FakeResp(lines=lines))
        events, exc = run_stream(backend, session)
        assert_failed_before_stop(events, exc, "quota")
        assert mgr.cooldown_set == [cb.USAGE_LIMIT_CODE]

    def test_response_incomplete(self):
        lines = sse(
            delta("cut"),
            {"type": "response.incomplete", "response": {"incomplete_details": {"reason": "max_output_tokens"}}},
        )
        backend, _, session = make(FakeResp(lines=lines))
        events, exc = run_stream(backend, session)
        assert_failed_before_stop(events, exc, "permanent")
        assert exc.error_code == "max_output_tokens"

    def test_interrupted_no_terminal_event(self):
        backend, _, session = make(FakeResp(lines=sse(delta("a"), delta("b"))))
        events, exc = run_stream(backend, session)
        assert_failed_before_stop(events, exc, "transient")

    def test_connection_drop_midstream(self):
        resp = FakeResp(lines=sse(delta("a")), raise_after=ConnectionResetError("dropped"))
        backend, _, session = make(resp)
        events, exc = run_stream(backend, session)
        assert_failed_before_stop(events, exc, "transient")

    def test_truncated_block_dropped(self):
        lines = sse(delta("a")) + [b'data: {"type": "response.completed"}\n']  # no blank line
        backend, _, session = make(FakeResp(lines=lines))
        events, exc = run_stream(backend, session)
        assert_failed_before_stop(events, exc, "transient")


class TestManagerIntegration:
    def test_success_notes_manager_stream_and_request(self):
        backend, mgr, session = make(FakeResp(lines=sse(delta("x"), completed())))
        run_stream(backend, session)
        assert mgr.successes == 1
        session2 = FakeSession(FakeResp(lines=sse(delta("x"), completed())))
        run_request(backend, session2)
        assert mgr.successes == 2

    @pytest.mark.parametrize(
        "terminal",
        [
            {"type": "response.failed", "response": {"error": {"code": "x"}}},
            {"type": "response.incomplete", "response": {}},
            None,
        ],
    )
    def test_failed_streams_do_not_note_success(self, terminal):
        lines = sse(delta("a"), *([terminal] if terminal else []))
        backend, mgr, session = make(FakeResp(lines=lines))
        run_stream(backend, session)
        assert mgr.successes == 0

    def test_http_error_does_not_note_success(self):
        backend, mgr, session = make(FakeResp(403, body={}))
        run_stream(backend, session)
        assert mgr.successes == 0

    def test_cooldown_checked_once_per_request_and_not_for_models(self):
        backend, mgr, session = make(
            FakeResp(401, body={}),
            FakeResp(lines=sse(delta("x"), completed())),
            FakeResp(200, body={"models": []}),
            FakeResp(200, body={"models": []}),
        )
        run_stream(backend, session)
        assert mgr.cooldown_checks == 1 and mgr.refreshes == 1
        with patch.object(cb, "_get_session", return_value=session):
            asyncio.run(backend.async_list_models())
            asyncio.run(backend.async_test_connection())
        assert mgr.cooldown_checks == 1

    def test_terminal_aibackenderror_sets_reauth_without_leaking(self):
        class Terminal(AIBackendError):
            terminal = True

        backend, mgr, session = make()
        mgr.fail_token = Terminal(f"needs sign-in {TOKEN}", "auth")
        _, exc = run_stream(backend, session)
        assert exc.category == "auth" and exc.reauth_required is True
        assert TOKEN not in str(exc) and session.calls == []

    def test_terminal_on_forced_refresh_sets_reauth(self):
        from custom_components.climate_advisor.chatgpt_account import AccountReauthRequired

        backend, mgr, session = make(FakeResp(401, body={}))
        mgr.fail_refresh = AccountReauthRequired()
        _, exc = run_stream(backend, session)
        assert exc.reauth_required is True and exc.category == "auth"

    def test_real_manager_probe_lifts_cooldown(self):
        from custom_components.climate_advisor.chatgpt_account import ChatGPTAccountManager

        mgr = ChatGPTAccountManager.__new__(ChatGPTAccountManager)
        mgr._quota_until = 1.0  # cooldown already elapsed
        mgr._quota_probe_until = None
        mgr._quota_epoch = 0
        mgr._refresh_generation = 0
        mgr._schedule_save = lambda: None

        async def tok(force_refresh=False):
            return TOKEN

        mgr.async_get_access_token = tok
        backend = cb.ChatGPTBackend({}, mgr, hass=object())
        session = FakeSession(FakeResp(lines=sse(delta("x"), completed())))
        _, exc = run_stream(backend, session)  # first caller is the probe
        assert exc is None
        assert mgr._quota_until is None and mgr._quota_probe_until is None

    @staticmethod
    def _real_manager_past_its_cooldown():
        from custom_components.climate_advisor.chatgpt_account import ChatGPTAccountManager

        mgr = ChatGPTAccountManager.__new__(ChatGPTAccountManager)
        mgr._quota_until = 1.0  # cooldown already elapsed
        mgr._quota_probe_until = None
        mgr._quota_epoch = 0
        mgr._refresh_generation = 0
        mgr._schedule_save = lambda: None

        async def tok(force_refresh=False):
            return TOKEN

        mgr.async_get_access_token = tok
        return mgr

    def test_real_manager_probe_that_fails_for_a_non_quota_reason_keeps_the_gate(self):
        mgr = self._real_manager_past_its_cooldown()
        backend = cb.ChatGPTBackend({}, mgr, hass=object())
        _, exc = run_stream(backend, FakeSession(FakeResp(500, body={})))
        assert exc.category == "transient"
        assert mgr._quota_until == 1.0
        assert mgr.quota_cooldown_active() is True  # the lease is held by the failed probe

    def test_real_manager_ignores_success_of_a_request_older_than_the_cooldown(self):
        """Passed the gate, then another zone's 429, the wait and its probe, then it completes."""
        mgr = self._real_manager_past_its_cooldown()
        mgr._quota_until = None

        async def tok(force_refresh=False):
            mgr.set_quota_cooldown("limit")
            mgr._quota_until = 1.0
            assert mgr.quota_cooldown_active() is False
            return TOKEN

        mgr.async_get_access_token = tok
        backend = cb.ChatGPTBackend({}, mgr, hass=object())
        session = FakeSession(FakeResp(lines=sse(delta("x"), completed())))
        _, exc = run_stream(backend, session)
        assert exc is None
        assert mgr._quota_until == 1.0
        assert mgr.quota_cooldown_active() is True


class TestHttpErrors:
    def test_429_quota_sets_cooldown_no_retry(self):
        resp = FakeResp(429, body={"error": {"code": cb.USAGE_LIMIT_CODE}})
        backend, mgr, session = make(resp, FakeResp(lines=sse(delta("x"), completed())))
        events, exc = run_stream(backend, session)
        assert_failed_before_stop(events, exc, "quota")
        assert exc.status == 429 and exc.request_id == "req_123"
        assert mgr.cooldown_set == [cb.USAGE_LIMIT_CODE]
        assert len(session.calls) == 1

    def test_active_cooldown_blocks_without_http(self):
        backend, _, session = make(cooldown=True)
        events, exc = run_stream(backend, session)
        assert_failed_before_stop(events, exc, "quota")
        assert session.calls == []

    def test_401_refresh_once_then_success(self):
        backend, mgr, session = make(
            FakeResp(401, body={"error": {"code": "token_expired"}}),
            FakeResp(lines=sse(delta("ok"), completed())),
        )
        events, exc = run_stream(backend, session)
        assert exc is None and mgr.refreshes == 1
        assert session.calls[1]["headers"]["Authorization"] == f"Bearer {TOKEN2}"

    def test_401_second_response_invalid_user_surfaced_no_reauth(self):
        backend, mgr, session = make(
            FakeResp(401, body={"detail": "x"}),
            FakeResp(401, body={"error": {"code": cb.INVALID_USER_CODE}}),
        )
        events, exc = run_stream(backend, session)
        assert_failed_before_stop(events, exc, "auth")
        assert exc.error_code == cb.INVALID_USER_CODE and exc.reauth_required is False
        assert mgr.refreshes == 1 and len(session.calls) == 2

    def test_401_invalid_user_first_does_not_refresh(self):
        backend, mgr, session = make(FakeResp(401, body={"error": {"code": cb.INVALID_USER_CODE}}))
        _, exc = run_stream(backend, session)
        assert exc.category == "auth" and mgr.refreshes == 0

    def test_401_second_response_revoked_code_does_not_force_reauth(self):
        backend, mgr, session = make(
            FakeResp(401, body={"error": {"code": "token_expired"}}),
            FakeResp(401, body={"error": {"code": "token_revoked"}}),
        )
        _, exc = run_stream(backend, session)
        # Inference codes never force re-sign-in; only a terminal refresh failure does.
        assert exc.reauth_required is False and exc.category == "auth" and mgr.refreshes == 1

    def test_401_second_response_unknown_code_no_reauth(self):
        backend, mgr, session = make(FakeResp(401, body={"detail": "x"}), FakeResp(401, body={"detail": "x"}))
        _, exc = run_stream(backend, session)
        assert exc.category == "auth" and exc.reauth_required is False and mgr.refreshes == 1

    def test_401_second_response_429_is_quota(self):
        backend, mgr, session = make(
            FakeResp(401, body={}), FakeResp(429, body={"error": {"code": cb.USAGE_LIMIT_CODE}})
        )
        _, exc = run_stream(backend, session)
        assert exc.category == "quota" and mgr.cooldown_set

    def test_terminal_refresh_failure_requires_reauth(self):
        backend, mgr, session = make(FakeResp(401, body={}))
        err = RuntimeError("refresh_token_reused")
        err.terminal = True  # type: ignore[attr-defined]
        mgr.fail_refresh = err
        _, exc = run_stream(backend, session)
        assert exc.category == "auth" and exc.reauth_required is True

    def test_unexpected_manager_error_is_not_retried(self):
        """It may have come after a grant went out, so the wrapper must not run the request again."""
        backend, mgr, session = make(FakeResp(lines=[]))
        mgr.fail_token = RuntimeError("unexpected")
        _, exc = run_stream(backend, session)
        assert exc.category == "transient" and exc.no_retry is True and session.calls == []

    def test_403_surfaced_not_retried(self):
        code = "subscription_sharing_user_not_eligible"
        backend, _, session = make(FakeResp(403, body={"error": {"code": code}}), FakeResp(lines=[]))
        events, exc = run_stream(backend, session)
        assert_failed_before_stop(events, exc, "permanent")
        assert exc.status == 403 and exc.error_code == code and len(session.calls) == 1

    def test_503_bounded_backoff_then_success(self, _no_sleep):
        backend, _, session = make(
            FakeResp(503, body={}), FakeResp(503, body={}), FakeResp(lines=sse(delta("x"), completed()))
        )
        events, exc = run_stream(backend, session)
        assert exc is None and len(session.calls) == 3 and len(_no_sleep) == 2

    def test_503_exhausted_is_transient(self, _no_sleep):
        backend, _, session = make(*[FakeResp(503, body={}) for _ in range(10)])
        _, exc = run_stream(backend, session)
        assert exc.category == "transient" and exc.status == 503
        assert len(session.calls) == cb.MAX_503_RETRIES + 1

    def test_404_model_not_found(self):
        backend, _, session = make(FakeResp(404, body={"error": {"code": "model_not_found"}}))
        _, exc = run_stream(backend, session)
        assert exc.model_not_found is True


class TestRateLimit429:
    """Only the documented plan-limit code is ``quota``; any other 429 stops this request alone."""

    @pytest.mark.parametrize(
        "body",
        [
            {"error": {"type": "rate_limit_error", "code": "slow_down"}},
            {"detail": "too many requests"},
            None,
        ],
    )
    def test_other_429_is_rate_limit_not_a_plan_limit(self, body):
        backend, mgr, session = make(FakeResp(429, body=body), FakeResp(lines=sse(delta("x"), completed())))
        events, exc = run_stream(backend, session)
        assert_failed_before_stop(events, exc, "rate_limit")
        assert exc.no_retry is True  # the wrapper must not repeat it: the Terms require stopping on 429
        assert exc.status == 429 and exc.request_id == "req_123"
        assert mgr.cooldown_set == [] and len(session.calls) == 1
        assert "usage limit" not in str(exc).lower() and "plan" not in str(exc).lower()

    def test_retry_after_seconds_is_in_the_message(self):
        resp = FakeResp(429, body={"error": {"code": "slow_down"}}, headers={"Retry-After": "56"})
        backend, _, session = make(resp)
        _, exc = run_stream(backend, session)
        assert exc.category == "rate_limit" and "56 seconds" in str(exc)

    @pytest.mark.parametrize("value", ["Wed, 21 Oct 2026 07:28:00 GMT", "0", "<b>"])
    def test_retry_after_that_is_not_positive_seconds_is_not_shown(self, value):
        resp = FakeResp(429, body={}, headers={"Retry-After": value})
        backend, _, session = make(resp)
        _, exc = run_stream(backend, session)
        assert exc.category == "rate_limit" and "seconds" not in str(exc) and value not in str(exc)

    def test_plan_limit_message_never_infers_a_reset_from_retry_after(self):
        resp = FakeResp(429, body={"error": {"code": cb.USAGE_LIMIT_CODE}}, headers={"Retry-After": "56"})
        backend, mgr, session = make(resp)
        _, exc = run_stream(backend, session)
        assert exc.category == "quota" and "56" not in str(exc) and mgr.cooldown_set

    @pytest.mark.parametrize(
        ("code", "event"),
        [
            ("rate_limit_exceeded", lambda c: {"type": "response.failed", "response": {"error": {"code": c}}}),
            ("slow_down", lambda c: {"type": "error", "code": c, "message": "m"}),
        ],
    )
    def test_stream_rate_limit_is_not_retried_and_pauses_nothing(self, code, event):
        backend, mgr, session = make(FakeResp(lines=sse(delta("part"), event(code))))
        events, exc = run_stream(backend, session)
        assert_failed_before_stop(events, exc, "rate_limit")
        assert exc.no_retry is True and exc.error_code == code and mgr.cooldown_set == []

    def test_stream_error_event_with_plan_limit_code_is_quota(self):
        lines = sse({"type": "error", "code": cb.USAGE_LIMIT_CODE, "message": "m"})
        backend, mgr, session = make(FakeResp(lines=lines))
        events, exc = run_stream(backend, session)
        assert_failed_before_stop(events, exc, "quota")
        assert exc.no_retry is True and mgr.cooldown_set == [cb.USAGE_LIMIT_CODE]

    def test_token_refresh_429_is_not_retried(self):
        backend, mgr, session = make()
        mgr.fail_token = AIBackendError("ChatGPT token refresh failed", "rate_limit", no_retry=True)
        _, exc = run_stream(backend, session)
        assert exc.category == "rate_limit" and exc.no_retry is True and session.calls == []

    def test_probe_ended_by_other_429_keeps_the_cooldown_gate(self):
        mgr = TestTwoZones.manager()
        mgr._quota_until = 1.0  # cooldown already elapsed: this request is the probe
        _, exc = run_stream(cb.ChatGPTBackend({}, mgr, hass=object()), FakeSession(FakeResp(429, body={})))
        assert exc.category == "rate_limit"
        assert mgr._quota_until == 1.0 and mgr.quota_cooldown_active() is True

    def test_wrapper_makes_one_attempt_and_leaves_the_breaker_alone(self):
        from custom_components.climate_advisor.claude_api import ClaudeAPIClient

        failed = sse({"type": "response.failed", "response": {"error": {"code": "slow_down"}}})
        backend, _, session = make(*[FakeResp(lines=failed) for _ in range(5)])
        client = ClaudeAPIClient({"ai_enabled": True, "ai_provider": "chatgpt", "ai_chatgpt_model": "gpt-x"})
        client._backend = backend
        with patch.object(cb, "_get_session", return_value=session):
            response = asyncio.run(client.async_request("sys", "usr"))
        assert response.success is False and response.no_retry is True
        assert len(session.calls) == 1
        assert client._circuit_breaker.consecutive_failures == 0


class TestTwoZones:
    """Two zones share one real account manager, as they do in production."""

    @staticmethod
    def manager():
        from custom_components.climate_advisor.chatgpt_account import ChatGPTAccountManager

        mgr = ChatGPTAccountManager.__new__(ChatGPTAccountManager)
        mgr._quota_until = None
        mgr._quota_probe_until = None
        mgr._quota_epoch = 0
        mgr._refresh_generation = 0
        mgr._schedule_save = lambda: None

        async def tok(force_refresh=False):
            return TOKEN

        mgr.async_get_access_token = tok
        return mgr

    @staticmethod
    def zones(mgr):
        return cb.ChatGPTBackend({"zone": "a"}, mgr, hass=object()), cb.ChatGPTBackend(
            {"zone": "b"}, mgr, hass=object()
        )

    def test_other_429_on_one_zone_does_not_stop_the_other(self):
        mgr = self.manager()
        zone_a, zone_b = self.zones(mgr)
        _, exc = run_stream(zone_a, FakeSession(FakeResp(429, body={"error": {"code": "slow_down"}})))
        assert exc.category == "rate_limit"
        session_b = FakeSession(FakeResp(lines=sse(delta("ok"), completed())))
        events, exc_b = run_stream(zone_b, session_b)
        assert exc_b is None and events[-1]["type"] == "stop" and len(session_b.calls) == 1

    def test_plan_limit_429_on_one_zone_stops_the_other(self):
        mgr = self.manager()
        zone_a, zone_b = self.zones(mgr)
        _, exc = run_stream(zone_a, FakeSession(FakeResp(429, body={"error": {"code": cb.USAGE_LIMIT_CODE}})))
        assert exc.category == "quota"
        session_b = FakeSession(FakeResp(lines=sse(delta("ok"), completed())))
        _, exc_b = run_stream(zone_b, session_b)
        assert exc_b.category == "quota" and exc_b.no_retry is True and session_b.calls == []


class TestModels:
    MODELS = {
        "models": [
            {"slug": "gpt-a", "visibility": "list"},
            {"slug": "hidden", "visibility": "hide"},
            {"slug": "gpt-b", "visibility": "list"},
        ]
    }

    def _run(self, backend, session, coro_name, *a):
        with patch.object(cb, "_get_session", return_value=session):
            return asyncio.run(getattr(backend, coro_name)(*a))

    def test_list_models_uses_get_models(self):
        backend, _, session = make(FakeResp(200, body=self.MODELS))
        assert self._run(backend, session, "async_list_models") == ["gpt-a", "gpt-b"]
        assert session.calls[0]["method"] == "get" and session.calls[0]["url"].endswith("/v1/models")

    def test_list_models_failure_returns_empty(self):
        backend, _, session = make(FakeResp(500, body={}))
        assert self._run(backend, session, "async_list_models") == []

    @pytest.mark.parametrize("status", [429, 503])
    def test_test_connection_temporary_is_not_ready(self, status):
        backend, mgr, session = make(*[FakeResp(status, body={}) for _ in range(4)])
        ok, msg = self._run(backend, session, "async_test_connection")
        assert ok is False and "not ready" in msg
        assert mgr.cooldown_set == [] and all(c["method"] == "get" for c in session.calls)

    def test_test_connection_reports_a_manager_error_instead_of_raising(self):
        backend, mgr, session = make()
        mgr.fail_token = AIBackendError("ChatGPT token refresh failed", "transient", no_retry=True)
        ok, msg = self._run(backend, session, "async_test_connection")
        assert ok is False and "not ready" in msg

    def test_test_connection_says_when_the_account_is_not_connected(self):
        backend, mgr, session = make()
        mgr.fail_token = AIBackendError("ChatGPT account is not connected", "auth")
        ok, msg = self._run(backend, session, "async_test_connection")
        assert ok is False and "not connected" in msg

    def test_test_connection_ok_never_posts(self):
        backend, _, session = make(FakeResp(200, body=self.MODELS))
        ok, _ = self._run(backend, session, "async_test_connection")
        assert ok and [c["method"] for c in session.calls] == ["get"]

    def test_test_connection_401_codes_do_not_ask_signin(self):
        backend, _, session = make(FakeResp(401, body={}), FakeResp(401, body={"error": {"code": "token_revoked"}}))
        ok, msg = self._run(backend, session, "async_test_connection")
        assert ok is False and "Sign in" not in msg


class TestDiagnosticLogging:
    def test_stream_end_logs_terminal_status_and_request_id(self, caplog):
        caplog.set_level(logging.DEBUG)
        backend, mgr, session = make(FakeResp(200, lines=sse(delta("SECRETTEXT"), completed())))
        run_stream(backend, session)
        msgs = [r.getMessage() for r in caplog.records if r.levelno == logging.DEBUG]
        assert any("response.completed" in m and "200" in m and "req_123" in m for m in msgs)
        assert "SECRETTEXT" not in caplog.text

    def test_failed_stream_logs_category_and_code(self, caplog):
        caplog.set_level(logging.DEBUG)
        failed = {"type": "response.failed", "response": {"error": {"code": "server_error", "message": "PLANTED"}}}
        backend, mgr, session = make(FakeResp(200, lines=sse(failed)))
        _, exc = run_stream(backend, session)
        assert exc is not None
        msgs = [r.getMessage() for r in caplog.records if r.levelno == logging.DEBUG]
        assert any("terminal=response.failed" in m for m in msgs)
        assert any("category=" in m and "code=server_error" in m for m in msgs)
        assert "PLANTED" not in caplog.text

    def test_http_failure_logs_category_status_and_code(self, caplog):
        caplog.set_level(logging.DEBUG)
        backend, mgr, session = make(FakeResp(403, body={"error": {"code": "some_code", "message": "PLANTED"}}))
        run_stream(backend, session)
        msgs = [r.getMessage() for r in caplog.records if r.levelno == logging.DEBUG]
        assert any("category=permanent" in m and "status=403" in m and "some_code" in m for m in msgs)
        assert "PLANTED" not in caplog.text


class TestSecrecy:
    def test_tokens_absent_from_logs_and_errors(self, caplog):
        caplog.set_level(logging.DEBUG)
        backend, mgr, session = make(
            FakeResp(401, body={"detail": f"bad {TOKEN}"}),
            FakeResp(401, body={"error": {"code": "x", "message": f"Bearer {TOKEN2}"}, "detail": TOKEN2}),
        )
        _, exc = run_stream(backend, session)
        text = str(exc) + repr(exc) + caplog.text
        assert TOKEN not in text and TOKEN2 not in text

    def test_manager_error_text_not_leaked(self):
        backend, mgr, session = make(FakeResp(401, body={}))
        mgr.fail_refresh = RuntimeError(f"failed with {TOKEN}")
        _, exc = run_stream(backend, session)
        assert TOKEN not in str(exc)


class TestSpec:
    def test_registered_spec(self):
        spec = PROVIDERS["chatgpt"]
        assert spec is cb.CHATGPT_SPEC
        assert spec.credential_kind == "account" and spec.plan_restricted is True
        assert not spec.tracks_cost and not spec.supports_tuning and not spec.supports_tier_fallback
        assert spec.usage_url

    def test_resolve_drops_claude_overrides(self):
        out = cb.CHATGPT_SPEC.resolve_request_options(
            {"ai_chatgpt_model": "gpt-z", "ai_model": "claude-x"},
            {"model": "claude-y", "max_tokens": 9, "temperature": 1.0, "reasoning_effort": "high"},
        )
        assert out["model"] == "gpt-z" and out["max_tokens"] == 0

    def test_inactive_reason(self):
        assert cb.ChatGPTBackend({}, None).inactive_reason
        assert cb.ChatGPTBackend({}, FakeManager()).inactive_reason is None


class TestMalformedFrames:
    def test_unparseable_frame_fails_the_stream(self):
        lines = [*sse(delta("partial")), b"data: {not json\n", b"\n", *sse(delta("rest"), completed())]
        backend, mgr, session = make(FakeResp(lines=lines))
        events, exc = run_stream(backend, session)
        assert_failed_before_stop(events, exc, "transient")
        assert mgr.successes == 0

    def test_non_object_frame_fails_the_stream(self):
        backend, _, session = make(FakeResp(lines=[b"data: 42\n", b"\n", *sse(delta("x"), completed())]))
        events, exc = run_stream(backend, session)
        assert_failed_before_stop(events, exc, "transient")

    def test_request_path_fails_too(self):
        backend, _, session = make(FakeResp(lines=[b"data: {not json\n", b"\n", *sse(delta("x"), completed())]))
        with pytest.raises(AIBackendError):
            run_request(backend, session)

    def test_comments_keepalives_and_done_marker_are_ignored(self):
        lines = [
            b": keepalive\n",
            b"\n",
            b"event: ping\n",
            b"\n",
            b"data:\n",
            b"\n",
            b'event: ping\ndata: {"type": "ping"}\n',
            b"\n",
            *sse(delta("x"), completed()),
            b"data: [DONE]\n",
            b"\n",
        ]
        backend, _, session = make(FakeResp(lines=lines))
        events, exc = run_stream(backend, session)
        assert exc is None and [e["type"] for e in events] == ["text", "stop"]


class TestNoAnswer:
    def test_refusal_delta_is_an_error_and_is_not_echoed(self):
        refusal = {"type": "response.refusal.delta", "delta": "I can't help with SECRET-REFUSAL"}
        backend, _, session = make(FakeResp(lines=sse(refusal, completed())))
        events, exc = run_stream(backend, session)
        assert_failed_before_stop(events, exc, "permanent")
        assert exc.no_retry and exc.no_answer
        assert "declined" in str(exc) and "SECRET-REFUSAL" not in str(exc)

    def test_refusal_only_in_the_completed_output_is_an_error(self):
        done = {
            "type": "response.completed",
            "response": {"output": [{"content": [{"type": "refusal", "refusal": "no"}]}]},
        }
        backend, _, session = make(FakeResp(lines=sse(done)))
        events, exc = run_stream(backend, session)
        assert_failed_before_stop(events, exc, "permanent")
        assert exc.no_answer and "declined" in str(exc)

    def test_completed_with_no_text_at_all_is_an_error(self):
        backend, _, session = make(FakeResp(lines=sse(completed())))
        events, exc = run_stream(backend, session)
        assert_failed_before_stop(events, exc, "permanent")
        assert exc.no_retry and exc.no_answer

    def test_whitespace_only_delta_is_an_error(self):
        backend, _, session = make(FakeResp(lines=sse(delta("  \n"), completed())))
        events, exc = run_stream(backend, session)
        assert exc is not None and exc.category == "permanent"
        assert exc.no_retry and exc.no_answer
        assert all(e["type"] != "stop" for e in events)

    def test_whitespace_only_delta_is_an_error_when_aggregated(self):
        backend, _, session = make(FakeResp(lines=sse(delta(" "), completed())))
        with pytest.raises(AIBackendError) as info:
            run_request(backend, session)
        assert info.value.no_answer

    def test_text_then_refusal_in_the_completed_output_is_an_error(self):
        done = {
            "type": "response.completed",
            "response": {"output": [{"content": [{"type": "refusal", "refusal": "no"}]}]},
        }
        backend, _, session = make(FakeResp(lines=sse(delta("partial"), done)))
        events, exc = run_stream(backend, session)
        assert exc is not None and exc.no_answer and "declined" in str(exc)
        assert all(e["type"] != "stop" for e in events)
        backend, _, session = make(FakeResp(lines=sse(delta("partial"), done)))
        with pytest.raises(AIBackendError) as info:
            run_request(backend, session)
        assert info.value.no_answer

    def test_short_answers_are_kept(self):
        backend, _, session = make(FakeResp(lines=sse(delta("4"), completed())))
        events, exc = run_stream(backend, session)
        assert exc is None and [e["type"] for e in events] == ["text", "stop"]

    def test_whitespace_delta_with_text_in_the_completed_output_uses_that_text(self):
        done = {
            "type": "response.completed",
            "response": {"output": [{"content": [{"type": "output_text", "text": "Yes"}]}]},
        }
        backend, _, session = make(FakeResp(lines=sse(delta(" "), done)))
        assert run_request(backend, session).content.strip() == "Yes"

    @pytest.mark.parametrize("kind", ["delta_refusal", "output_refusal", "empty"])
    def test_every_no_answer_outcome_notes_the_request_as_succeeded(self, kind):
        refusal_item = {"type": "response.completed", "response": {"output": [{"content": [{"type": "refusal"}]}]}}
        lines = {
            "delta_refusal": sse({"type": "response.refusal.delta", "delta": "x"}, completed()),
            "output_refusal": sse(refusal_item),
            "empty": sse(completed()),
        }[kind]
        backend, mgr, session = make(FakeResp(lines=lines))
        _, exc = run_stream(backend, session)
        assert exc is not None and exc.no_answer
        assert mgr.successes == 1

    def test_client_reports_it_without_retrying_or_counting_the_breaker(self):
        from custom_components.climate_advisor.claude_api import ClaudeAPIClient

        mgr = FakeManager()
        client = ClaudeAPIClient({"ai_enabled": True, "ai_provider": "chatgpt", "ai_chatgpt_model": "m"}, mgr)
        session = FakeSession(FakeResp(lines=sse(completed())), FakeResp(lines=sse(completed())))
        with patch.object(cb, "_get_session", return_value=session):
            response = asyncio.run(client.async_request("S", "U"))
        assert not response.success and response.no_answer and response.error
        assert len(session.calls) == 1
        assert client._circuit_breaker.consecutive_failures == 0

    def test_client_logs_name_the_provider_not_claude(self, caplog):
        from custom_components.climate_advisor.claude_api import ClaudeAPIClient

        client = ClaudeAPIClient({"ai_enabled": True, "ai_provider": "chatgpt", "ai_chatgpt_model": "m"}, FakeManager())
        session = FakeSession(FakeResp(lines=sse(completed())))
        with caplog.at_level(logging.WARNING), patch.object(cb, "_get_session", return_value=session):
            asyncio.run(client.async_request("S", "U"))
        assert "ChatGPT request not retried" in caplog.text
        assert "Claude API" not in caplog.text


class _RotatingManager(FakeManager):
    """Hands out the current generation's token; a forced read rotates it, as the real manager does."""

    async def async_get_access_token(self, force_refresh: bool = False) -> str:
        if force_refresh:
            self.refreshes += 1
            self.refresh_generation += 1
        return f"tok-{self.refresh_generation}"


class _RotatedBeforeAnswer(FakeResp):
    """A rejection that arrives after another zone has refreshed the shared account."""

    def __init__(self, mgr: FakeManager, status: int = 401, **kw: Any) -> None:
        super().__init__(status, **kw)
        self.mgr = mgr

    async def __aenter__(self):
        self.mgr.refresh_generation += 1
        return self


class _TokenServer:
    """Refuses the expired tok-0, accepts its replacement, and answers the first request last."""

    class _Exchange:
        def __init__(self, resp: FakeResp, hold: asyncio.Event | None, on_entered: asyncio.Event) -> None:
            self.resp, self.hold, self.on_entered = resp, hold, on_entered

        async def __aenter__(self):
            if self.hold is not None:
                self.on_entered.set()
                await self.hold.wait()
            return self.resp

        async def __aexit__(self, *a):
            return False

    def __init__(self) -> None:
        self.sent: list[str] = []
        self.first_sent, self.answer_first = asyncio.Event(), asyncio.Event()

    def post(self, url, **kw):
        token = kw["headers"]["Authorization"].removeprefix("Bearer ")
        self.sent.append(token)
        resp = (
            FakeResp(401, body={"detail": "x"}) if token == "tok-0" else FakeResp(lines=sse(delta("ok"), completed()))
        )
        return self._Exchange(resp, self.answer_first if len(self.sent) == 1 else None, self.first_sent)


class TestTokenReplacedByAnotherZone:
    """A 401 for a token another zone has since replaced says nothing about the current token."""

    @staticmethod
    def _bound() -> tuple[cb.ChatGPTBackend, _RotatingManager]:
        mgr = _RotatingManager()
        return cb.ChatGPTBackend({}, mgr, hass=object()).for_request(), mgr

    def test_two_zones_racing_on_one_token_both_succeed_with_one_grant(self):
        mgr = _RotatingManager()
        zone_a = cb.ChatGPTBackend({}, mgr, hass=object()).for_request()
        zone_b = cb.ChatGPTBackend({}, mgr, hass=object()).for_request()

        async def collect(zone):
            return [e async for e in zone.async_stream(system_prompt="s", user_message="u", model="gpt-x")]

        async def go():
            server = _TokenServer()
            with patch.object(cb, "_get_session", return_value=server):
                pending_a = asyncio.ensure_future(collect(zone_a))
                await server.first_sent.wait()  # zone A has sent tok-0 and awaits its answer
                events_b = await collect(zone_b)  # zone B is refused tok-0 and refreshes to tok-1
                server.answer_first.set()  # only now does zone A hear its 401
                return await pending_a, events_b, server.sent

        events_a, events_b, sent = asyncio.run(go())
        assert events_a[-1]["type"] == "stop" and events_b[-1]["type"] == "stop"
        assert mgr.refreshes == 1
        assert sent == ["tok-0", "tok-0", "tok-1", "tok-1"]

    def test_replacement_token_rejected_is_terminal_with_no_grant(self):
        backend, mgr = self._bound()
        session = FakeSession(_RotatedBeforeAnswer(mgr), FakeResp(401))
        events, exc = run_stream(backend, session)
        assert_failed_before_stop(events, exc, "auth")
        assert exc.no_retry and mgr.refreshes == 0 and len(session.calls) == 2
        assert session.calls[1]["headers"]["Authorization"] == "Bearer tok-1"

    def test_token_replaced_on_every_answer_cannot_loop(self):
        backend, mgr = self._bound()
        session = FakeSession(*(_RotatedBeforeAnswer(mgr) for _ in range(5)))
        events, exc = run_stream(backend, session)
        assert_failed_before_stop(events, exc, "auth")
        assert exc.no_retry and mgr.refreshes == 0 and len(session.calls) == 2

    def test_token_replaced_after_this_requests_own_grant_sends_no_second_grant(self):
        backend, mgr = self._bound()
        session = FakeSession(FakeResp(401), _RotatedBeforeAnswer(mgr), FakeResp(lines=sse(delta("ok"), completed())))
        events, exc = run_stream(backend, session)
        assert exc is None and events[-1]["type"] == "stop"
        assert mgr.refreshes == 1 and len(session.calls) == 3

    def test_non_credential_401_is_not_retried_on_the_replacement(self):
        backend, mgr = self._bound()
        session = FakeSession(_RotatedBeforeAnswer(mgr, body={"error": {"code": cb.INVALID_USER_CODE}}))
        events, exc = run_stream(backend, session)
        assert_failed_before_stop(events, exc, "auth")
        assert len(session.calls) == 1 and mgr.refreshes == 0

    def test_rejection_of_a_token_refreshed_before_sending_stays_terminal(self):
        """The first read may have spent this request's grant (expiry), so no second one."""
        backend, mgr = self._bound()
        mgr.refresh_generation += 1
        session = FakeSession(FakeResp(401))
        events, exc = run_stream(backend, session)
        assert_failed_before_stop(events, exc, "auth")
        assert exc.no_retry and mgr.refreshes == 0 and len(session.calls) == 1


class _SwappingSession(FakeSession):
    """After the first send, the shared account changes hands."""

    def __init__(self, mgr: FakeManager, *responses: FakeResp) -> None:
        super().__init__(*responses)
        self.mgr = mgr

    def _do(self, method, url, **kw):
        result = super()._do(method, url, **kw)
        self.mgr.account_identity = ("new-owner", "new-subject")
        return result


class TestRequestBoundToItsAccount:
    def _bound(self, *responses: FakeResp):
        mgr = FakeManager()
        mgr.account_identity = ("owner-1", "subject-1")
        backend = cb.ChatGPTBackend({}, mgr, hass=object()).for_request()
        return backend, mgr, _SwappingSession(mgr, *responses)

    def test_503_backoff_does_not_resend_after_the_account_changed(self):
        backend, _mgr, session = self._bound(FakeResp(503), FakeResp(lines=sse(delta("x"), completed())))
        events, exc = run_stream(backend, session)
        assert_failed_before_stop(events, exc, "auth")
        assert exc.no_retry and len(session.calls) == 1

    def test_401_does_not_refresh_or_resend_after_the_account_changed(self):
        backend, mgr, session = self._bound(FakeResp(401), FakeResp(lines=sse(delta("x"), completed())))
        events, exc = run_stream(backend, session)
        assert_failed_before_stop(events, exc, "auth")
        assert mgr.refreshes == 0 and len(session.calls) == 1

    def test_account_changing_during_the_token_read_sends_nothing(self):
        backend, mgr, session = self._bound(FakeResp(lines=sse(delta("x"), completed())))
        real = mgr.async_get_access_token

        async def swap(force_refresh: bool = False) -> str:
            mgr.account_identity = ("new-owner", "new-subject")
            return await real(force_refresh)

        mgr.async_get_access_token = swap
        _events, exc = run_stream(backend, session)
        assert exc is not None and session.calls == []

    def test_same_account_after_a_token_refresh_is_still_served(self):
        mgr = FakeManager()
        mgr.account_identity = ("owner-1", "subject-1")
        backend = cb.ChatGPTBackend({}, mgr, hass=object()).for_request()
        session = FakeSession(FakeResp(401), FakeResp(lines=sse(delta("x"), completed())))
        events, exc = run_stream(backend, session)
        assert exc is None and events[-1]["type"] == "stop"
        assert session.calls[1]["headers"]["Authorization"] == f"Bearer {TOKEN2}"

    def test_unbound_backend_is_unaffected_by_a_change(self):
        """The model list and readiness probe are not a user's request."""
        mgr = FakeManager()
        mgr.account_identity = ("owner-1", "subject-1")
        backend = cb.ChatGPTBackend({}, mgr, hass=object())
        session = _SwappingSession(mgr, FakeResp(503), FakeResp(lines=sse(delta("x"), completed())))
        _events, exc = run_stream(backend, session)
        assert exc is None

    def test_client_retry_does_not_reach_a_newly_connected_account(self):
        from custom_components.climate_advisor.claude_api import ClaudeAPIClient

        mgr = FakeManager()
        mgr.account_identity = ("owner-1", "subject-1")
        client = ClaudeAPIClient({"ai_enabled": True, "ai_provider": "chatgpt", "ai_chatgpt_model": "m"}, mgr)
        session = _SwappingSession(mgr, FakeResp(500), FakeResp(lines=sse(delta("x"), completed())))
        with patch.object(cb, "_get_session", return_value=session):
            response = asyncio.run(client.async_request("S", "U"))
        assert not response.success and response.no_retry
        assert len(session.calls) == 1
        assert client._circuit_breaker.consecutive_failures == 0

    def test_client_stream_binds_too(self):
        from custom_components.climate_advisor.claude_api import ClaudeAPIClient

        mgr = FakeManager()
        mgr.account_identity = ("owner-1", "subject-1")
        client = ClaudeAPIClient({"ai_enabled": True, "ai_provider": "chatgpt", "ai_chatgpt_model": "m"}, mgr)
        session = _SwappingSession(mgr, FakeResp(503), FakeResp(lines=sse(delta("x"), completed())))

        async def go():
            return [e async for e in client.async_request_streaming("S", "U")]

        with patch.object(cb, "_get_session", return_value=session), pytest.raises(AIBackendError):
            asyncio.run(go())
        assert len(session.calls) == 1

    def test_client_retries_with_no_switch(self):
        """The request holds a copy of the zone's backend; that alone must not look like a switch."""
        from custom_components.climate_advisor.claude_api import ClaudeAPIClient

        client = ClaudeAPIClient({"ai_enabled": True, "ai_provider": "chatgpt", "ai_chatgpt_model": "m"}, FakeManager())
        session = FakeSession(FakeResp(500), FakeResp(lines=sse(delta("x"), completed())))
        with patch.object(cb, "_get_session", return_value=session):
            assert asyncio.run(client.async_request("S", "U")).success
        assert len(session.calls) == 2

    def test_client_retry_ends_when_the_zone_moves_to_another_provider(self):
        from custom_components.climate_advisor.claude_api import ClaudeAPIClient

        client = ClaudeAPIClient({"ai_enabled": True, "ai_provider": "chatgpt", "ai_chatgpt_model": "m"}, FakeManager())
        session = FakeSession(FakeResp(500), FakeResp(lines=sse(delta("x"), completed())))
        new_backend = MagicMock()

        async def switch_while_waiting(_delay):
            client.update_config({"ai_enabled": True, "ai_provider": "anthropic"}, backend=new_backend)

        with (
            patch.object(cb, "_get_session", return_value=session),
            patch.object(cb.asyncio, "sleep", switch_while_waiting),
        ):
            response = asyncio.run(client.async_request("S", "U"))
        assert not response.success and response.no_retry
        assert len(session.calls) == 1 and not new_backend.async_request.called
        assert client._circuit_breaker.consecutive_failures == 0
