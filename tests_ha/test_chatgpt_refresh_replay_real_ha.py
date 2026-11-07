"""A lost token refresh must not be replayed within a request.

The real account manager, ChatGPT backend and request wrapper run together against real Home
Assistant; only HTTP is mocked. The server may have rotated the refresh token when its answer was
lost, so the saved token may already be consumed and sending it again can end the session for
every zone. All tokens are fake.
"""

from __future__ import annotations

import json
import time
from unittest.mock import AsyncMock

import pytest
from aiohttp import ClientConnectionError
from homeassistant.exceptions import HomeAssistantError
from pytest_homeassistant_custom_component.test_util.aiohttp import AiohttpClientMockResponse

from custom_components.climate_advisor import chatgpt_account, chatgpt_backend, claude_api
from custom_components.climate_advisor.ai_providers import async_ensure_providers_registered
from custom_components.climate_advisor.chatgpt_account import STATE_CONNECTED, STATE_REAUTH_REQUIRED
from tests_ha.test_chatgpt_account_real_ha import HOUR, REFRESH, _connected, _token_ok

CONFIG = {"ai_enabled": True, "ai_provider": "chatgpt", "ai_chatgpt_model": "gpt-test"}
COMPLETED_STREAM = (
    'data: {"type": "response.output_text.delta", "delta": "hello"}\n\n'
    'data: {"type": "response.completed", "response": {"output": []}}\n\n'
)
_LOST_RESPONSES = {
    "timeout": {"exc": TimeoutError()},
    "connection": {"exc": ClientConnectionError()},
    "gateway_5xx": {"status": 502, "text": "bad gateway"},
    "unreadable_200": {"status": 200, "text": "not json"},
}


@pytest.fixture
def lost(request) -> dict:
    return _LOST_RESPONSES[request.param]


lost_response = pytest.mark.parametrize("lost", _LOST_RESPONSES, indirect=True)


@pytest.fixture(autouse=True)
def no_retry_wait(monkeypatch) -> None:
    monkeypatch.setattr(claude_api, "AI_RETRY_BASE_DELAY_SECONDS", 0)


async def _client(hass, aioclient_mock, monkeypatch=None):
    """Signed in, with a wrapper on top. With ``monkeypatch`` the clock moves on, so each request refreshes."""
    await async_ensure_providers_registered(hass)
    manager = await _connected(hass, aioclient_mock)
    if monkeypatch is not None:
        later = time.time() + 2 * HOUR
        monkeypatch.setattr(chatgpt_account, "_now", lambda: later)
    return manager, claude_api.ClaudeAPIClient(dict(CONFIG), manager)


def _refresh_tokens_sent(aioclient_mock) -> list[str]:
    return [call[2]["refresh_token"] for call in aioclient_mock.mock_calls if str(call[1]) == chatgpt_account.TOKEN_URL]


def _posts_sent(aioclient_mock) -> int:
    return sum(str(call[1]) == chatgpt_backend.RESPONSES_URL for call in aioclient_mock.mock_calls)


async def _request(client):
    return await client.async_request("sys", "hi")


async def _fails_without_retry(client, *, streaming: bool) -> None:
    """One failed request through the wrapper: not retried, and outside the circuit breaker."""
    if streaming:
        with pytest.raises(chatgpt_account.AIBackendError) as exc:
            async for _event in client.async_request_streaming("sys", "hi"):
                pass
        assert exc.value.no_retry is True
    else:
        response = await _request(client)
        assert response.success is False and response.no_retry is True
    assert client._circuit_breaker.consecutive_failures == 0


either_path = pytest.mark.parametrize("streaming", [False, True], ids=["non_streaming", "streaming"])


class TestSameRequestDoesNotReplay:
    @lost_response
    async def test_non_streaming(self, hass, aioclient_mock, monkeypatch, lost) -> None:
        manager, client = await _client(hass, aioclient_mock, monkeypatch)
        aioclient_mock.post(chatgpt_account.TOKEN_URL, **lost)

        response = await _request(client)

        assert _refresh_tokens_sent(aioclient_mock) == [REFRESH]
        assert response.success is False and response.no_retry is True
        assert manager.state == STATE_CONNECTED

    @lost_response
    async def test_streaming(self, hass, aioclient_mock, monkeypatch, lost) -> None:
        manager, client = await _client(hass, aioclient_mock, monkeypatch)
        aioclient_mock.post(chatgpt_account.TOKEN_URL, **lost)

        with pytest.raises(chatgpt_account.AIBackendError) as exc:
            async for _event in client.async_request_streaming("sys", "hi"):
                pass

        assert exc.value.no_retry is True
        assert _refresh_tokens_sent(aioclient_mock) == [REFRESH]
        assert manager.state == STATE_CONNECTED

    @lost_response
    async def test_model_list_used_by_the_readiness_check(self, hass, aioclient_mock, monkeypatch, lost) -> None:
        manager, _ = await _client(hass, aioclient_mock, monkeypatch)
        aioclient_mock.post(chatgpt_account.TOKEN_URL, **lost)

        with pytest.raises(chatgpt_account.AIBackendError):
            await chatgpt_backend.ChatGPTBackend({}, manager).async_fetch_models()

        assert _refresh_tokens_sent(aioclient_mock) == [REFRESH]
        assert manager.state == STATE_CONNECTED

    async def test_force_refresh_after_a_401_does_not_replay(self, hass, aioclient_mock) -> None:
        """A valid-looking token gets a 401, and the refresh that follows loses its answer."""
        manager, client = await _client(hass, aioclient_mock)
        aioclient_mock.post(chatgpt_backend.RESPONSES_URL, status=401, text="{}")
        aioclient_mock.post(chatgpt_account.TOKEN_URL, exc=TimeoutError())

        response = await _request(client)

        assert _refresh_tokens_sent(aioclient_mock) == [REFRESH]
        assert response.no_retry is True and manager.state == STATE_CONNECTED


class TestNextRequest:
    """It can only send the saved token again; what that means depends on what the server did."""

    async def test_refresh_never_reached_the_server_so_the_saved_token_still_works(
        self, hass, aioclient_mock, monkeypatch
    ) -> None:
        manager, client = await _client(hass, aioclient_mock, monkeypatch)
        aioclient_mock.post(chatgpt_account.TOKEN_URL, exc=TimeoutError())
        assert (await _request(client)).success is False

        aioclient_mock.clear_requests()
        aioclient_mock.post(chatgpt_account.TOKEN_URL, json=_token_ok())
        aioclient_mock.post(chatgpt_backend.RESPONSES_URL, text=COMPLETED_STREAM)
        response = await _request(client)

        assert response.success is True and response.content == "hello"
        assert _refresh_tokens_sent(aioclient_mock) == [REFRESH]
        assert manager.state == STATE_CONNECTED

    async def test_server_had_rotated_so_the_saved_token_is_refused_and_reauth_follows(
        self, hass, aioclient_mock, monkeypatch
    ) -> None:
        """The known limit: the rotated token died with the lost answer, and only a new sign-in recovers."""
        manager, client = await _client(hass, aioclient_mock, monkeypatch)
        aioclient_mock.post(chatgpt_account.TOKEN_URL, exc=TimeoutError())
        assert (await _request(client)).success is False
        assert manager.state == STATE_CONNECTED

        aioclient_mock.clear_requests()
        aioclient_mock.post(chatgpt_account.TOKEN_URL, status=400, text=json.dumps({"error": "invalid_grant"}))
        response = await _request(client)

        assert response.success is False and response.no_retry is True
        assert _refresh_tokens_sent(aioclient_mock) == [REFRESH]
        assert manager.state == STATE_REAUTH_REQUIRED


class TestEveryRefreshFailureExitIsNoRetry:
    @either_path
    async def test_rotated_refresh_token_without_an_access_token(
        self, hass, aioclient_mock, monkeypatch, streaming
    ) -> None:
        manager, client = await _client(hass, aioclient_mock, monkeypatch)
        aioclient_mock.post(chatgpt_account.TOKEN_URL, json={"refresh_token": "rt_ROTATED"})

        await _fails_without_retry(client, streaming=streaming)

        assert _refresh_tokens_sent(aioclient_mock) == [REFRESH]
        assert manager.state == STATE_CONNECTED

        # The rotated token was kept: the next request sends that one, not the spent one.
        aioclient_mock.clear_requests()
        aioclient_mock.post(chatgpt_account.TOKEN_URL, json=_token_ok())
        aioclient_mock.post(chatgpt_backend.RESPONSES_URL, text=COMPLETED_STREAM)
        assert (await _request(client)).success is True
        assert _refresh_tokens_sent(aioclient_mock) == ["rt_ROTATED"]

    @either_path
    async def test_rotated_token_that_could_not_be_saved(self, hass, aioclient_mock, monkeypatch, streaming) -> None:
        manager, client = await _client(hass, aioclient_mock, monkeypatch)
        aioclient_mock.post(chatgpt_account.TOKEN_URL, json=_token_ok())
        save = AsyncMock(side_effect=HomeAssistantError)
        real_save = manager._store.async_save
        monkeypatch.setattr(manager._store, "async_save", save)

        await _fails_without_retry(client, streaming=streaming)
        assert _refresh_tokens_sent(aioclient_mock) == [REFRESH]

        # Still unsaved: the next request stops at the save, in one attempt, with no new grant.
        await _fails_without_retry(client, streaming=streaming)
        assert _refresh_tokens_sent(aioclient_mock) == [REFRESH]

        # Saving works again: the rotated tokens were kept, so no grant is needed.
        monkeypatch.setattr(manager._store, "async_save", real_save)
        aioclient_mock.post(chatgpt_backend.RESPONSES_URL, text=COMPLETED_STREAM)
        assert (await _request(client)).success is True
        assert _refresh_tokens_sent(aioclient_mock) == [REFRESH]

    @either_path
    async def test_refresh_not_allowed_yet(self, hass, aioclient_mock, monkeypatch, streaming) -> None:
        manager, client = await _client(hass, aioclient_mock, monkeypatch)
        manager._credentials.earliest_refresh_at = time.time() + 3 * HOUR

        await _fails_without_retry(client, streaming=streaming)

        assert _refresh_tokens_sent(aioclient_mock) == []
        assert _posts_sent(aioclient_mock) == 0


class TestOneGrantPerRequest:
    """A 401 on a token that was just refreshed is not cured by another refresh."""

    @either_path
    async def test_401_after_the_refresh_it_caused(self, hass, aioclient_mock, streaming) -> None:
        _, client = await _client(hass, aioclient_mock)
        aioclient_mock.post(chatgpt_backend.RESPONSES_URL, status=401, text="{}")
        aioclient_mock.post(chatgpt_account.TOKEN_URL, json=_token_ok())

        await _fails_without_retry(client, streaming=streaming)

        assert _refresh_tokens_sent(aioclient_mock) == [REFRESH]
        assert _posts_sent(aioclient_mock) == 2

    @either_path
    async def test_401_on_a_token_refreshed_because_it_had_expired(
        self, hass, aioclient_mock, monkeypatch, streaming
    ) -> None:
        _, client = await _client(hass, aioclient_mock, monkeypatch)
        aioclient_mock.post(chatgpt_backend.RESPONSES_URL, status=401, text="{}")
        aioclient_mock.post(chatgpt_account.TOKEN_URL, json=_token_ok())

        await _fails_without_retry(client, streaming=streaming)

        assert _refresh_tokens_sent(aioclient_mock) == [REFRESH]
        assert _posts_sent(aioclient_mock) == 1

    async def test_401_on_the_wrappers_retry_after_a_refresh(self, hass, aioclient_mock) -> None:
        """401, refresh, 500 (retried by the wrapper), 401: the retry belongs to the same request."""
        _, client = await _client(hass, aioclient_mock)
        answers = iter((401, 500, 401))

        async def answer(method, url, _data):
            return AiohttpClientMockResponse(method, url, status=next(answers), text="{}")

        aioclient_mock.post(chatgpt_backend.RESPONSES_URL, side_effect=answer)
        aioclient_mock.post(chatgpt_account.TOKEN_URL, json=_token_ok())

        response = await _request(client)

        assert response.success is False and response.no_retry is True
        assert _refresh_tokens_sent(aioclient_mock) == [REFRESH]
        assert _posts_sent(aioclient_mock) == 3
