"""ChatGPT-plan (Sign in with ChatGPT) AIBackend for the Responses route.

Talks to ``https://api.openai.com/v1`` only (never ``chatgpt.com/backend-api``) over aiohttp.
Only a ``response.completed`` event with text is success: failed, incomplete, unreadable,
refused, empty or interrupted streams raise before the final ``stop`` event so no partial or
blank report is ever stored. Tokens come from
the shared account manager (injected) and are never logged or placed in exception text.
Retry/breaker/rate-limit policy lives in ``ClaudeAPIClient``; the only retry here is a
bounded backoff on HTTP 503. A 429 is never retried: only the documented plan-limit code
(``QUOTA_CODES``) pauses every zone, any other 429 fails this one request as ``rate_limit``.
"""

from __future__ import annotations

import asyncio
import copy
import json
import logging
import time
from collections.abc import AsyncIterator
from typing import Any

import aiohttp

from .ai_backend import CREDENTIAL_ACCOUNT, PROVIDERS, AIBackendError, ProviderSpec
from .chatgpt_account import _error_code, _get_session, get_account_manager
from .const import CONF_AI_CHATGPT_MODEL

_LOGGER = logging.getLogger(__name__)

API_BASE = "https://api.openai.com/v1"
RESPONSES_URL = f"{API_BASE}/responses"
MODELS_URL = f"{API_BASE}/models"
USAGE_URL = "https://chatgpt.com/settings/usage"  # "Manage usage"

USAGE_LIMIT_CODE = "subscription_sharing_usage_limit_exceeded"
INVALID_USER_CODE = "subscription_sharing_invalid_user"
# 401 codes that are not a stale access token: refreshing would not help.
NON_CREDENTIAL_401 = frozenset({INVALID_USER_CODE})
# Documented structured codes (errors-and-recovery). The re-sign-in codes there (invalid_grant,
# refresh_token_*, ...) are REFRESH errors owned by the account manager, never inference codes.
QUOTA_CODES = frozenset({USAGE_LIMIT_CODE})
# Stream failures have no HTTP status, so a rate limit is told by code. slow_down/rate_limit_error:
# API error-codes guide. rate_limit_exceeded: ResponseError.code in the Responses API reference.
RATE_LIMIT_CODES = frozenset({"rate_limit_exceeded", "slow_down", "rate_limit_error"})
TRANSIENT_CODES = frozenset({"subscription_sharing_usage_unavailable", "subscription_sharing_user_unavailable"})

_NOT_BOUND: Any = object()  # a backend not taken for one request (the model list, the readiness probe)

MAX_503_RETRIES = 2
BACKOFF_SECONDS = (1.0, 3.0)

REQUEST_TIMEOUT = aiohttp.ClientTimeout(total=900, connect=30, sock_read=300)
MODELS_TIMEOUT = aiohttp.ClientTimeout(total=30)


class ChatGPTBackendError(AIBackendError):
    """AIBackendError carrying diagnosis fields. Never contains tokens or response bodies."""

    def __init__(
        self,
        message: str,
        category: str = "permanent",
        *,
        status: int | None = None,
        error_code: str | None = None,
        request_id: str | None = None,
        reauth_required: bool = False,
        **kwargs: Any,
    ) -> None:
        super().__init__(message, category, **kwargs)
        self.status = status
        self.error_code = error_code
        self.request_id = request_id
        self.reauth_required = reauth_required


def _safe(value: Any) -> str | None:
    """Short identifier-like string, else None (never free text that could echo secrets)."""
    if isinstance(value, str) and 0 < len(value) <= 80 and all(c.isalnum() or c in "_-.:" for c in value):
        return value
    return None


def _shape(payload: Any) -> str:
    return ",".join(sorted(payload)) if isinstance(payload, dict) else type(payload).__name__


def _describe(what: str, status: int | None, code: str | None, rid: str | None, shape: str | None = None) -> str:
    parts = [what]
    if status is not None:
        parts.append(f"status={status}")
    if code:
        parts.append(f"code={code}")
    if rid:
        parts.append(f"request_id={rid}")
    if shape:
        parts.append(f"shape={shape}")
    return " ".join(parts)


def _rate_limit_text(retry_after: Any = None) -> str:
    """Retry-After is documented as seconds; any other form is not shown."""
    seconds = _safe(retry_after)
    wait = f"in {int(seconds)} seconds" if seconds and seconds.isdecimal() and int(seconds) > 0 else "later"
    return f"ChatGPT is rate limiting requests; this one was not retried, try again {wait}"


def _classify_stream_error(code: str | None) -> str:
    if code in QUOTA_CODES:
        return "quota"
    if code in RATE_LIMIT_CODES:
        return "rate_limit"
    if code in TRANSIENT_CODES:
        return "transient"
    return "permanent"


async def _iter_sse(content: Any) -> AsyncIterator[dict[str, Any]]:
    """Yield parsed SSE events; a frame that is not a JSON object is yielded as ``_malformed``.

    Comments, event-only blocks, empty data and ``[DONE]`` are keepalive/framing and yield nothing.
    A block without its terminating blank line is dropped (may be truncated).
    """
    event_name: str | None = None
    data: list[str] = []
    async for raw in content:
        line = (raw.decode("utf-8", "replace") if isinstance(raw, bytes) else raw).rstrip("\r\n")
        if not line:
            if data:
                payload = "\n".join(data)
                if payload.strip() and payload != "[DONE]":
                    try:
                        parsed = json.loads(payload)
                    except ValueError:
                        parsed = None
                    if isinstance(parsed, dict):
                        if "type" not in parsed and event_name:
                            parsed["type"] = event_name
                        yield parsed
                    else:
                        yield {"type": "_malformed"}
            event_name, data = None, []
        elif line.startswith(":"):
            continue
        elif line.startswith("event:"):
            event_name = line[6:].strip()
        elif line.startswith("data:"):
            value = line[5:]
            data.append(value[1:] if value.startswith(" ") else value)


def _declined(request_id: str | None) -> ChatGPTBackendError:
    """A refusal. The model's own text is never placed in an exception, which reaches the logs."""
    return ChatGPTBackendError(
        "ChatGPT declined to answer this request", "permanent", no_retry=True, no_answer=True, request_id=request_id
    )


_REFUSAL_MARKER = "refusal"


def _completed_text(response: dict[str, Any], rid: str | None) -> str:
    """Text of a completed response's output; a refusal anywhere in it is an error."""
    content = [
        c
        for item in response.get("output") or []
        if isinstance(item, dict)
        for c in item.get("content") or []
        if isinstance(c, dict)
    ]
    if any(c.get("type") == "refusal" for c in content):
        raise _declined(rid)
    return "".join(c.get("text", "") for c in content if c.get("type") == "output_text")


class ChatGPTBackend:
    """AIBackend for the ChatGPT plan (Responses API, Sign in with ChatGPT tokens).

    ``client`` is the injected shared account manager (``chatgpt_account``). Manager
    API as implemented there (the ``_manager_*`` helpers are the only places that touch it):
    ``await async_get_access_token(force_refresh=False) -> str`` (raises on terminal
    refresh failure; the exception's ``terminal`` attribute marks re-sign-in),
    ``quota_cooldown_active() -> bool``, ``quota_epoch``, ``note_request_succeeded(started_epoch)``
    and ``set_quota_cooldown(error_code)``.
    """

    def __init__(self, config: dict[str, Any], client: Any | None = None, hass: Any | None = None) -> None:
        self._config = config
        self.client: Any = client
        self._hass = hass if hass is not None else getattr(client, "hass", None)
        self._bound_account: Any = _NOT_BOUND
        self._bound_generation: int | None = None

    def for_request(self) -> ChatGPTBackend:
        """A copy for one request, bound to the account that holds the manager right now."""
        bound = copy.copy(self)
        bound._bound_account = self._manager_account()
        bound._bound_generation = self._manager_refresh_generation()
        return bound

    def _check_bound_account(self) -> None:
        """A request never goes out on credentials other than those it started with."""
        if self._bound_account is not _NOT_BOUND and self._manager_account() != self._bound_account:
            raise ChatGPTBackendError(
                "The ChatGPT account changed while this request was running; it was stopped",
                "auth",
                no_retry=True,
            )

    # --- manager seam -------------------------------------------------------------
    def _manager_account(self) -> Any:
        return self.client.account_identity

    async def _manager_token(self, force_refresh: bool = False) -> str:
        self._check_bound_account()
        try:
            return await self.client.async_get_access_token(force_refresh=force_refresh)
        except AIBackendError as exc:
            if getattr(exc, "terminal", False) and not getattr(exc, "reauth_required", False):
                # Terminal refresh failure: fixed text, never the manager's own message.
                raise ChatGPTBackendError(
                    "ChatGPT account needs a new sign-in", "auth", reauth_required=True, no_retry=True
                ) from None
            raise
        except Exception as exc:  # noqa: BLE001 - manager errors are mapped, never re-raised raw
            terminal = bool(getattr(exc, "terminal", False))
            raise ChatGPTBackendError(
                f"ChatGPT account token unavailable ({type(exc).__name__})",
                "auth" if terminal else "transient",
                reauth_required=terminal,
                no_retry=True,
            ) from None

    def _manager_refresh_generation(self) -> int:
        return self.client.refresh_generation

    def _manager_cooldown_active(self) -> bool:
        return bool(self.client.quota_cooldown_active())

    def _manager_quota_epoch(self) -> int:
        return self.client.quota_epoch

    def _manager_note_success(self, started_epoch: int | None) -> None:
        self.client.note_request_succeeded(started_epoch)

    def _manager_set_cooldown(self, error_code: str | None) -> None:
        self.client.set_quota_cooldown(error_code)

    # --- AIBackend ----------------------------------------------------------------
    @property
    def inactive_reason(self) -> str | None:
        if self.client is None:
            return "ChatGPT account is not connected"
        return None

    def update_config(self, config: dict[str, Any]) -> None:
        self._config = config

    @staticmethod
    def build_body(system_prompt: str, user_message: str, model: str) -> dict[str, Any]:
        """Request body. No temperature, output cap or reasoning.effort (unsupported/unconfirmed)."""
        return {
            "model": model,
            "instructions": system_prompt,
            "input": [{"role": "user", "content": user_message}],
            "store": False,
            "stream": True,
        }

    @staticmethod
    def _headers(token: str, *, stream: bool) -> dict[str, str]:
        headers = {"Authorization": f"Bearer {token}"}
        if stream:
            headers["Content-Type"] = "application/json"
        return headers

    async def _read_error(self, resp: Any, *, stage: str) -> ChatGPTBackendError:
        """Build the error for a non-2xx response without echoing the body."""
        status = resp.status
        rid = _safe(resp.headers.get("x-request-id"))
        if 300 <= status < 400:  # never followed: the prompt and token go to the API host only
            return ChatGPTBackendError(
                _describe(f"ChatGPT {stage} was redirected", status, None, rid),
                "permanent",
                no_retry=True,
                status=status,
                request_id=rid,
            )
        try:
            payload = json.loads(await resp.read())
        except ValueError, aiohttp.ClientError, OSError:
            payload = None
        code = _error_code(payload)
        shape = _shape(payload)
        msg = _describe(f"ChatGPT {stage} failed", status, code, rid, shape)
        kw = {"status": status, "error_code": code, "request_id": rid}
        if status == 429:
            if code in QUOTA_CODES:
                return ChatGPTBackendError(msg, "quota", no_retry=True, **kw)
            text = _rate_limit_text(resp.headers.get("Retry-After"))
            return ChatGPTBackendError(_describe(text, status, code, rid, shape), "rate_limit", no_retry=True, **kw)
        if status == 401:
            # Re-sign-in only on a terminal refresh failure (manager), never from an inference code.
            return ChatGPTBackendError(msg, "auth", **kw)
        if status == 403:
            return ChatGPTBackendError(msg, "permanent", **kw)
        if status >= 500:
            return ChatGPTBackendError(msg, "transient", **kw)
        not_found = status == 404 or (code is not None and "model_not_found" in code)
        return ChatGPTBackendError(msg, "permanent", model_not_found=not_found, **kw)

    async def _open(self, session: Any, method: str, url: str, body: dict[str, Any] | None, timeout: Any) -> Any:  # noqa: ASYNC109
        """Return an entered response context for a 2xx; raises ChatGPTBackendError otherwise.

        401: if another zone has replaced the token this request sent, retry once with the saved token and
        no forced grant. Otherwise refresh only if the code is not a known non-credential code and no token
        has been refreshed since the request began (the expiry check or a retry may already have), then
        classify the next answer by its own code. A 401 after a refresh is ``no_retry``: one request sends
        one refresh grant.
        503: bounded backoff. A redirect is an error, never followed. Everything else is not retried.
        Callers must ``await ctx.__aexit__`` via ``_release``.
        """
        start_generation = self._bound_generation
        if start_generation is None:
            start_generation = self._manager_refresh_generation()
        refreshed = False
        retries_503 = 0
        retried_replaced = False
        token = await self._manager_token()
        sent_generation = self._manager_refresh_generation()
        while True:
            self._check_bound_account()
            headers = self._headers(token, stream=body is not None)
            kwargs: dict[str, Any] = {"headers": headers, "timeout": timeout, "allow_redirects": False}
            if body is not None:
                kwargs["json"] = body
            ctx = getattr(session, method)(url, **kwargs)
            try:
                resp = await ctx.__aenter__()
            except TimeoutError as exc:
                raise ChatGPTBackendError("ChatGPT request timed out", "transient", timeout=True) from exc
            except (aiohttp.ClientError, OSError) as exc:
                raise ChatGPTBackendError(f"ChatGPT connection failed ({type(exc).__name__})", "transient") from None
            if resp.status < 300:
                return ctx, resp
            try:
                err = await self._read_error(resp, stage="request")
            finally:
                await ctx.__aexit__(None, None, None)
            _LOGGER.debug(
                "ChatGPT request failed: category=%s status=%s code=%s", err.category, err.status, err.error_code
            )
            generation = self._manager_refresh_generation()
            if (
                err.status == 401
                and generation != sent_generation
                and not retried_replaced
                and err.error_code not in NON_CREDENTIAL_401
            ):
                retried_replaced = True
                token = await self._manager_token()
                sent_generation = self._manager_refresh_generation()
                continue
            refreshed = refreshed or generation != start_generation
            if err.status == 401 and not refreshed and err.error_code not in NON_CREDENTIAL_401:
                refreshed = True
                token = await self._manager_token(force_refresh=True)
                sent_generation = self._manager_refresh_generation()
                continue
            if err.status == 503 and retries_503 < MAX_503_RETRIES:
                await asyncio.sleep(BACKOFF_SECONDS[min(retries_503, len(BACKOFF_SECONDS) - 1)])
                retries_503 += 1
                continue
            if err.status == 401 and refreshed:
                _LOGGER.warning("ChatGPT 401 after token refresh: %s", err)
                err.no_retry = True
            raise err

    async def _events(self, system_prompt: str, user_message: str, model: str) -> AsyncIterator[dict[str, Any]]:
        if self.client is None:
            raise ChatGPTBackendError("ChatGPT account is not connected", "auth", reauth_required=True, no_retry=True)
        if self._manager_cooldown_active():
            raise ChatGPTBackendError("ChatGPT plan usage limit reached; requests are paused", "quota", no_retry=True)
        quota_epoch = self._manager_quota_epoch()
        body = self.build_body(system_prompt, user_message, model)
        session = _get_session(self._hass)
        ctx, resp = None, None
        try:
            try:
                ctx, resp = await self._open(session, "post", RESPONSES_URL, body, REQUEST_TIMEOUT)
            except ChatGPTBackendError as exc:
                if exc.category == "quota" and exc.status == 429:
                    self._manager_set_cooldown(exc.error_code)
                raise
            rid = _safe(resp.headers.get("x-request-id"))
            streamed = ""
            terminal: dict[str, Any] | None = None
            async for ev in self._read_stream(resp, rid):
                if ev["type"] == "text":
                    streamed += ev["text"]
                    yield ev
                elif ev["type"] == "thinking":
                    yield ev
                elif ev["type"] == _REFUSAL_MARKER:
                    self._manager_note_success(quota_epoch)  # the request completed, the model declined it
                    raise _declined(rid)
                else:
                    terminal = ev
            if terminal is None:
                raise ChatGPTBackendError("ChatGPT stream ended without a terminal event", "transient", request_id=rid)
            etype = str(terminal.get("type"))
            _LOGGER.debug("ChatGPT stream ended: terminal=%s status=%s request_id=%s", etype, resp.status, rid)
            response = terminal.get("response") if isinstance(terminal.get("response"), dict) else {}
            if etype != "response.completed":
                raise self._terminal_failure(terminal, response, rid)
            self._manager_note_success(quota_epoch)  # only a success lifts the cooldown
            final = _completed_text(response, rid)
            if not streamed.strip():
                if not final.strip():
                    raise ChatGPTBackendError(
                        "ChatGPT returned no text for this request",
                        "permanent",
                        no_retry=True,
                        no_answer=True,
                        request_id=rid,
                    )
                yield {"type": "text", "text": final}
            usage = response.get("usage") if isinstance(response.get("usage"), dict) else {}
            yield {
                "type": "stop",
                "stop_reason": "end_turn",
                "truncated_empty": False,
                "input_tokens": int(usage.get("input_tokens") or 0),
                "output_tokens": int(usage.get("output_tokens") or 0),
                "estimated_cost": 0.0,
            }
        finally:
            if ctx is not None:
                await ctx.__aexit__(None, None, None)

    async def _read_stream(self, resp: Any, rid: str | None) -> AsyncIterator[dict[str, Any]]:
        """Yield text and thinking events, then the terminal event (or a refusal marker) and stop."""
        try:
            async for ev in _iter_sse(resp.content):
                etype = str(ev.get("type", ""))
                delta = ev.get("delta")
                if etype == "response.output_text.delta":
                    if isinstance(delta, str) and delta:
                        yield {"type": "text", "text": delta}
                elif etype == "response.reasoning_summary_text.delta":
                    if isinstance(delta, str) and delta:
                        yield {"type": "thinking", "text": delta}
                elif etype == "_malformed":
                    raise ChatGPTBackendError("ChatGPT stream had an unreadable frame", "transient", request_id=rid)
                elif etype.startswith("response.refusal."):
                    yield {"type": _REFUSAL_MARKER}
                    return
                elif etype in ("response.completed", "response.failed", "response.incomplete", "error"):
                    yield ev
                    return
        except (aiohttp.ClientError, OSError) as exc:
            raise ChatGPTBackendError(
                f"ChatGPT stream interrupted ({type(exc).__name__})",
                "transient",
                timeout=isinstance(exc, asyncio.TimeoutError),
                request_id=rid,
            ) from None

    def _terminal_failure(
        self, terminal: dict[str, Any], response: dict[str, Any], rid: str | None
    ) -> ChatGPTBackendError:
        """The error for a failed or incomplete terminal event; starts the cooldown on a quota failure."""
        etype = str(terminal.get("type"))
        err = terminal.get("error") or response.get("error") or None
        code = _error_code({"error": err}) if isinstance(err, dict) else _error_code(terminal)
        if etype == "response.incomplete":
            details = response.get("incomplete_details")
            reason = _safe(details.get("reason")) if isinstance(details, dict) else None
            _LOGGER.debug("ChatGPT stream failed: category=permanent code=%s", reason)
            return ChatGPTBackendError(
                _describe(f"ChatGPT response incomplete reason={reason}", None, None, rid),
                "permanent",
                error_code=reason,
                request_id=rid,
            )
        category = _classify_stream_error(code)
        _LOGGER.debug("ChatGPT stream failed: category=%s code=%s", category, code)
        what = _rate_limit_text() if category == "rate_limit" else f"ChatGPT stream {etype}"
        if category == "quota":
            self._manager_set_cooldown(code)
        return ChatGPTBackendError(
            _describe(what, None, code, rid),
            category,
            error_code=code,
            request_id=rid,
            no_retry=category in ("quota", "rate_limit"),
        )

    async def async_stream(
        self,
        *,
        system_prompt: str,
        user_message: str,
        model: str,
        max_tokens: int = 0,
        temperature: float = 0.0,
        reasoning_effort: str = "",
    ) -> AsyncIterator[dict[str, Any]]:
        """Yield text/thinking events then ``stop``; tuning arguments are ignored by design."""
        async for event in self._events(system_prompt, user_message, model):
            yield event

    async def async_request(
        self,
        *,
        system_prompt: str,
        user_message: str,
        model: str,
        max_tokens: int = 0,
        temperature: float = 0.0,
        reasoning_effort: str = "",
    ) -> Any:
        """Aggregate the stream into a ``ClaudeResponse``; raises before any partial success."""
        from .claude_api import ClaudeResponse

        start = time.monotonic()
        parts: list[str] = []
        stop: dict[str, Any] = {}
        async for event in self._events(system_prompt, user_message, model):
            if event["type"] == "text":
                parts.append(event["text"])
            elif event["type"] == "stop":
                stop = event
        return ClaudeResponse(
            success=True,
            content="".join(parts),
            input_tokens=stop.get("input_tokens", 0),
            output_tokens=stop.get("output_tokens", 0),
            estimated_cost=0.0,
            latency_ms=(time.monotonic() - start) * 1000.0,
            stop_reason=stop.get("stop_reason"),
            resolved_model=model,
        )

    async def async_fetch_models(self) -> list[str]:
        """GET /v1/models (never an inference request). Raises ChatGPTBackendError."""
        if self.client is None:
            raise ChatGPTBackendError("ChatGPT account is not connected", "auth", reauth_required=True, no_retry=True)
        ctx, resp = await self._open(_get_session(self._hass), "get", MODELS_URL, None, MODELS_TIMEOUT)
        try:
            try:
                payload = json.loads(await resp.read())
            except ValueError, aiohttp.ClientError, OSError:
                payload = None
        finally:
            await ctx.__aexit__(None, None, None)
        models = payload.get("models") if isinstance(payload, dict) else None
        if not isinstance(models, list):
            raise ChatGPTBackendError("ChatGPT model list had an unexpected shape", "transient")
        return [m["slug"] for m in models if isinstance(m, dict) and m.get("visibility") == "list" and m.get("slug")]

    async def async_list_models(self) -> list[str]:
        """Visible model slugs; empty list on any failure (never raises)."""
        try:
            return await self.async_fetch_models()
        except Exception as exc:  # noqa: BLE001 - spec contract: must not raise
            _LOGGER.debug("ChatGPT model list unavailable: %s", exc)
            return []

    async def async_test_connection(self, hass: Any | None = None) -> tuple[bool, str]:
        """Probe with GET /v1/models. Temporary 429/5xx is 'not ready', never a credential verdict."""
        if hass is not None:
            self._hass = hass
        try:
            models = await self.async_fetch_models()
        except AIBackendError as exc:  # _manager_token re-raises the account manager's own errors
            if exc.category in ("transient", "quota", "rate_limit"):
                return False, f"ChatGPT is not ready right now; try again later ({exc})"
            if getattr(exc, "reauth_required", False):
                return False, f"Sign in to ChatGPT again ({exc})"
            return False, str(exc)
        return True, f"Connected to ChatGPT plan ({len(models)} models available)"


def _resolve_request_options(config: dict[str, Any], overrides: dict[str, Any]) -> dict[str, Any]:
    """Drop Claude's model/token/reasoning overrides; use ``ai_chatgpt_model``."""
    return {
        "model": config.get(CONF_AI_CHATGPT_MODEL, ""),
        "max_tokens": 0,
        "temperature": 0.0,
        "reasoning_effort": "",
    }


def _backend_for(config: dict[str, Any], hass: Any | None) -> ChatGPTBackend:
    """Single lookup point for the shared account manager (``chatgpt_account.get_account_manager``)."""
    return ChatGPTBackend(config, get_account_manager(hass), hass)


async def _list_models(config: dict[str, Any], hass: Any | None = None) -> list[str]:
    """The account's models, plus the saved one: it stays selectable if the account stops listing it."""
    models = await _backend_for(config, hass).async_list_models()
    saved = config.get(CONF_AI_CHATGPT_MODEL)
    return [saved, *models] if saved and saved not in models else models


async def _validate_credentials(config: dict[str, Any], hass: Any | None = None) -> tuple[bool, str]:
    backend = _backend_for(config, hass)
    return await backend.async_test_connection(hass=hass)


CHATGPT_SPEC = ProviderSpec(
    id="chatgpt",
    label="ChatGPT subscription",
    display_name="ChatGPT",
    api_name="ChatGPT",
    credential_kind=CREDENTIAL_ACCOUNT,
    build_backend=ChatGPTBackend,
    usage_url=USAGE_URL,
    tracks_cost=False,
    supports_tuning=False,
    supports_tier_fallback=False,
    plan_restricted=True,
    resolve_request_options=_resolve_request_options,
    list_models=_list_models,
    validate_credentials=_validate_credentials,
    credential_setting=None,
    model_setting=CONF_AI_CHATGPT_MODEL,
)
PROVIDERS.setdefault(CHATGPT_SPEC.id, CHATGPT_SPEC)
