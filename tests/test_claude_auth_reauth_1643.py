"""Issue #1643: a rejected Claude API key asks the owner for a new one, on both request paths.

Occupant view: when the built-in Claude key is revoked, the AI features stop; instead of failing quietly (and
retrying the same dead key three times), the client calls its ``on_auth_failed`` hook once per refused request,
which the coordinator points at Home Assistant's reauthentication flow. Any other API error asks for nothing.

The anthropic package is not installed for the unit tier, so the SDK's error classes are stood in for on the
already-imported module (CLAUDE.md "Module-Level sys.modules Mocking"). ``claude_api`` is imported lazily, at test
time, so this file never binds it before ``test_claude_api.py`` injects its mock ``anthropic`` at collection.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import sys
from unittest.mock import MagicMock

if "homeassistant" not in sys.modules:
    from conftest import _install_ha_stubs

    _install_ha_stubs()

import importlib  # noqa: E402

import pytest  # noqa: E402

_mod = None  # custom_components.climate_advisor.claude_api, bound by the ``sdk`` fixture


class _APIError(Exception):
    pass


class _AuthError(_APIError):
    pass


class _RateLimit(Exception):
    pass


class _Timeout(Exception):
    pass


@pytest.fixture
def sdk(monkeypatch):
    global _mod
    _mod = importlib.import_module("custom_components.climate_advisor.claude_api")
    monkeypatch.setattr(_mod, "APIError", _APIError)
    monkeypatch.setattr(_mod, "AuthenticationError", _AuthError)
    monkeypatch.setattr(_mod, "RateLimitError", _RateLimit)
    monkeypatch.setattr(_mod, "APITimeoutError", _Timeout)
    monkeypatch.setattr(_mod, "NotFoundError", None)
    monkeypatch.setattr(_mod, "AI_RETRY_BASE_DELAY_SECONDS", 0)


def _client(raises: BaseException) -> tuple:
    api = MagicMock()
    client = _mod.ClaudeAPIClient(
        {"ai_enabled": True, "ai_provider": "builtin_claude", "ai_api_key": "k", "ai_model": "claude-sonnet-4-6"},
        client=api,
    )
    hook = MagicMock()
    client.on_auth_failed = hook
    calls = MagicMock(side_effect=raises)

    async def _single(**kwargs):
        return calls(**kwargs)

    client._single_api_call = _single

    class _FailingStream:
        async def __aenter__(self):
            raise raises

        async def __aexit__(self, *exc):
            return False

    api.messages.stream = MagicMock(return_value=_FailingStream())
    return client, hook, calls


def _stream(client) -> None:
    async def _collect():
        return [e async for e in client.async_request_streaming("System.", "User.")]

    asyncio.run(_collect())


def test_rejected_key_asks_once_and_is_not_retried(sdk) -> None:
    client, hook, calls = _client(_AuthError("401 invalid x-api-key"))
    response = asyncio.run(client.async_request("System.", "User."))
    assert response.success is False
    assert response.error.startswith("Authentication failed")
    assert calls.call_count == 1
    hook.assert_called_once_with()


def test_rejected_key_on_the_streaming_path_asks_too(sdk, caplog) -> None:
    client, hook, _calls = _client(_AuthError("401 invalid x-api-key"))
    with caplog.at_level(logging.WARNING), pytest.raises(_AuthError):
        _stream(client)
    hook.assert_called_once_with()
    assert not any("Unexpected API error" in r.message for r in caplog.records)


@pytest.mark.parametrize("error", [_APIError("400 bad request"), _RateLimit("429"), RuntimeError("reset")])
def test_other_errors_ask_for_nothing(sdk, error) -> None:
    client, hook, _calls = _client(error)
    asyncio.run(client.async_request("System.", "User."))
    with contextlib.suppress(Exception):
        _stream(client)
    hook.assert_not_called()


def test_a_failing_hook_never_breaks_the_request(sdk) -> None:
    client, hook, _calls = _client(_AuthError("401"))
    hook.side_effect = RuntimeError("no flow manager")
    response = asyncio.run(client.async_request("System.", "User."))
    assert response.success is False and response.error.startswith("Authentication failed")


def test_sdk_without_authentication_error_degrades_to_the_old_retry(sdk, monkeypatch) -> None:
    monkeypatch.setattr(_mod, "AuthenticationError", None)
    client, hook, calls = _client(_AuthError("401"))
    asyncio.run(client.async_request("System.", "User."))
    hook.assert_not_called()
    assert calls.call_count == _mod.AI_MAX_RETRIES
