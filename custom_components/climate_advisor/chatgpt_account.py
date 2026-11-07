"""The ChatGPT account shared by every zone of one Home Assistant instance.

A refresh token rotates and has exactly one holder, while the integration runs one
config entry and one AI client per zone. So the tokens live here, once, and each zone's
client reads them on each request.

Setup calls ``await async_get_account_manager(hass)`` on the event loop before the
coordinator restores state, then passes the manager by reference to the client built in
the executor. Unload calls ``async_cancel_runtime_tasks()`` (only when the last loaded
entry goes) and final removal calls ``await async_remove_account(hass)``.

Sign-in runs from inside Home Assistant: ``async_begin_login`` builds the authorize URL, the
browser ends on a dead 127.0.0.1 page, and the admin pastes that address into
``async_complete_login``. The HA box exchanges the code itself, so tokens never reach the
browser. The pasted URL is parsed only and never fetched.

Only ``auth.openai.com`` is contacted from here. Tokens, the code and the pasted URL never reach
logs, exception text, ``repr`` or ``status()``: errors carry a fixed message and a vetted code.
"""

from __future__ import annotations

import asyncio
import base64
import functools
import hashlib
import json
import logging
import re
import secrets
import urllib.parse
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

import voluptuous as vol
from aiohttp import ClientError, ClientTimeout
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.event import async_track_time_interval
from homeassistant.util import dt as dt_util

from .ai_backend import AIBackendError
from .const import CHATGPT_ACCOUNT_HASS_KEY, DOMAIN

_LOGGER = logging.getLogger(__name__)

STORAGE_VERSION = 1
STORAGE_KEY = "climate_advisor.chatgpt_account"
MAX_TOKEN_LENGTH = 16384

STATE_DISCONNECTED = "disconnected"
STATE_CONNECTED = "connected"
STATE_REAUTH_REQUIRED = "reauth_required"
_STATES = (STATE_DISCONNECTED, STATE_CONNECTED, STATE_REAUTH_REQUIRED)
# Only ever reported by status(), never stored: a sign-in's save could not be proved either way.
STATE_SAVE_UNCERTAIN = "save_uncertain"

REVOCATION_CONFIRMED = "confirmed"
REVOCATION_UNCONFIRMED = "unconfirmed"  # tell the user to disconnect the app in ChatGPT Settings
REVOCATION_NOT_NEEDED = "not_needed"

# Also the translation_key: strings.json needs issues.chatgpt_account_reauth_required.
REAUTH_ISSUE_ID = "chatgpt_account_reauth_required"

ISSUER = "https://auth.openai.com"
TOKEN_URL = f"{ISSUER}/api/accounts/oauth/token"
DISCOVERY_URL = f"{ISSUER}/.well-known/openid-configuration"
RESOURCE = "https://api.openai.com/v1"
DIRECT_SCOPE = "chatgpt.tokens.use.direct"
AUTHORIZE_URL = f"{ISSUER}/api/accounts/authorize"
APP_NAME = "Climate Advisor"
LOGIN_SCOPES = f"openid profile email offline_access resource.invoke {DIRECT_SCOPE}"
# The only port other than 1455, which the Codex CLI holds, that the sign-in docs name. Nothing
# listens here: the browser lands on a dead page and the admin copies its address.
LOGIN_PORT = 54321
LOGIN_CALLBACK_PATH = "/auth/callback"
LOGIN_REDIRECT_URI = f"http://127.0.0.1:{LOGIN_PORT}{LOGIN_CALLBACK_PATH}"
LOGIN_TTL_SECONDS = 900
MAX_PASTED_URL_LENGTH = 8192
_BOOTSTRAP_CLIENT_ID = "dynamic_agent_client"
# The docs name no algorithm. Asymmetric only: never "none" or HS*.
_ID_TOKEN_ALGS = ["RS256", "RS384", "RS512", "PS256", "PS384", "PS512", "ES256", "ES384", "ES512", "EdDSA"]
_ID_TOKEN_REQUIRED_CLAIMS = ["exp", "iat", "iss", "aud", "sub"]
_CLOCK_LEEWAY_SECONDS = 5  # as in the Sign in with ChatGPT website sample
_HTTP_TIMEOUT_SECONDS = 30

# errors-and-recovery: "Clear unusable tokens and repeat OAuth".
_TERMINAL_REFRESH_ERRORS = frozenset(
    {
        "invalid_grant",
        "invalid_refresh_token",
        "token_expired",
        "refresh_token_expired",
        "refresh_token_invalidated",
        "refresh_token_reused",
    }
)
# Real codes are short lower-case identifiers; anything else (an echoed token) is not logged.
_SAFE_CODE = re.compile(r"^[a-z][a-z0-9_.\-]{0,63}$")

_DEFAULT_EXPIRES_IN = 3600  # token-reference: access tokens last one hour
_REFRESH_SKEW_SECONDS = 300
_REVOKE_BACKOFF_SECONDS = (1, 2)
# Cleanup of a refused sign-in must not hold the flow step (and the lock) for the full retry budget.
_LOGIN_REVOKE_TIMEOUT_SECONDS = 20

# Weekly because refresh tokens last 30 days and the investigator may go unused for longer.
# The age is persisted and a tick costs nothing unless due, so restarts cannot reset the week.
KEEPALIVE_AFTER_SECONDS = 7 * 86400
KEEPALIVE_TICK = timedelta(hours=1)

# Not a reset time, which the docs say must not be inferred: only how long to wait before
# one request may look. Only a successful response ends the wait; a probe that proves nothing
# just lets another request look when its lease ends.
QUOTA_COOLDOWN_SECONDS = 3600
QUOTA_PROBE_LEASE_SECONDS = 900


class AccountReauthRequired(AIBackendError):
    """The account needs a new sign-in: confirmed by the auth server, never a guess."""

    terminal = True

    def __init__(self, message: str = "ChatGPT account needs a new sign-in") -> None:
        super().__init__(message, "auth")


# Fixed text: nothing pasted or received ever reaches an error message.
_LOGIN_ERRORS = {
    "already_connected": ("A ChatGPT account is already connected; disconnect first", "permanent"),
    "owner_required": ("Connecting needs the Home Assistant user who owns the plan", "permanent"),
    "no_pending_login": ("No sign-in is in progress; start again", "permanent"),
    "login_expired": ("That sign-in expired; start again", "permanent"),
    "bad_url": ("That is not the 127.0.0.1 address the browser ended on", "permanent"),
    "state_mismatch": ("That address belongs to a different sign-in attempt", "permanent"),
    "consent_denied": ("Access was declined in the browser", "permanent"),
    "exchange_failed": ("OpenAI did not complete the sign-in; start again", "transient"),
    "login_checks_failed": ("The sign-in failed its security checks; start again", "permanent"),
    "missing_scope": ("ChatGPT plan usage was not granted; start again and allow it", "permanent"),
    "bad_identity": ("The ChatGPT account could not be verified; start again", "permanent"),
    "different_account": ("That is not the ChatGPT account connected here; disconnect first to change it", "permanent"),
    "save_failed": ("The sign-in could not be saved; start again", "transient"),
}


class AccountLoginError(AIBackendError):
    """A sign-in was refused. ``reason`` is a stable code for the flow or view to show."""

    def __init__(self, reason: str, *, tokens_may_be_saved: bool = False) -> None:
        message, category = _LOGIN_ERRORS[reason]
        super().__init__(message, category)
        self.reason = reason
        # save_failed only: the write may have landed, so the new tokens must not be revoked yet.
        self.tokens_may_be_saved = tokens_may_be_saved


class _PersistError(Exception):
    """The store does not hold what was just saved.

    ``unsaved`` is True only when a read-back succeeded and proves the saved refresh token is not
    in the store. A failed write or failed read-back leaves it False: the write may have landed.
    """

    def __init__(self, unsaved: bool = False) -> None:
        super().__init__()
        self.unsaved = unsaved


class _RetryableHttp(Exception):
    """A 5xx where the docs ask for a retry with backoff."""


def _now() -> float:
    return dt_util.utcnow().timestamp()


def _iso(timestamp: float | None) -> str | None:
    if timestamp is None:
        return None
    return datetime.fromtimestamp(timestamp, UTC).isoformat(timespec="seconds")


def _check_auth_url(url: str) -> str:
    """Refuse anything but the documented auth host."""
    parts = urllib.parse.urlsplit(url)
    if parts.scheme != "https" or parts.hostname != "auth.openai.com":
        raise AIBackendError(f"Refusing out-of-bounds auth URL host={parts.hostname!r}", "permanent")
    return url


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float | str):
        return None
    try:
        return float(value)
    except ValueError:
        return None


def _parse_earliest(raw: Any) -> float | None:
    """Read ``earliest_refresh_at``, which the docs name but do not define.

    The live value has been integer epoch seconds. Accepted: absolute epoch seconds or
    milliseconds, as number or string, and ISO 8601 with a zone. Anything else imposes no
    floor, because blocking on an unreadable value would let the session die. A small number
    could be a duration and is never read as a date in 1970.
    """
    if raw is None:
        return None
    value = _number(raw)
    if value is None and isinstance(raw, str):
        try:
            parsed = datetime.fromisoformat(raw.strip().replace("Z", "+00:00"))
        except ValueError:
            parsed = None
        if parsed is not None and parsed.tzinfo is not None:
            return parsed.timestamp()
    if value is not None and value >= 1e9:
        return value / 1000 if value > 1e11 else value
    _LOGGER.warning("Ignoring unreadable earliest_refresh_at of type %s", type(raw).__name__)
    return None


def _error_code(payload: Any) -> str | None:
    """OAuth uses ``error``, Responses uses ``error.code``, some answers ``code``."""
    if not isinstance(payload, dict):
        return None
    err = payload.get("error")
    candidates = (
        (err.get("code"), err.get("type"))
        if isinstance(err, dict)
        else (err, payload.get("code"), payload.get("error_code"))
    )
    for code in candidates:
        if isinstance(code, str) and _SAFE_CODE.match(code):
            return code
    return None


def _parse_json(raw: str) -> Any:
    try:
        return json.loads(raw)
    except ValueError:
        return None


def _read_claims(token: str) -> dict[str, Any] | None:
    """Claims of a JWT, or None for an opaque token. Does not verify: the login tool did.

    PyJWT ships with Home Assistant core. It is imported at call time so this module still
    imports in the plain-Python test environment, which does not have it.
    """
    import jwt  # noqa: PLC0415

    try:
        claims = jwt.decode(token, options={"verify_signature": False})
    except jwt.PyJWTError:
        return None
    return claims if isinstance(claims, dict) else None


def _bearer(value: str) -> str:
    if value.lower() != "bearer":
        raise vol.Invalid("unsupported token type")
    return value


_TOKEN = vol.All(str, vol.Length(min=1, max=MAX_TOKEN_LENGTH))
_TOKEN_RESPONSE_SCHEMA = vol.Schema(
    {
        vol.Required("id_token"): _TOKEN,
        vol.Required("access_token"): _TOKEN,
        vol.Required("refresh_token"): _TOKEN,
        vol.Optional("token_type"): vol.Any(None, vol.All(str, _bearer)),
    },
    extra=vol.ALLOW_EXTRA,
)


@dataclass(repr=False)
class _PendingLogin:
    """One sign-in attempt. In memory only: never persisted, logged or returned."""

    verifier: str
    state: str
    nonce: str
    client_id: str
    created_at: float
    # Public handle for this attempt (a flow keeps it to cancel only its own); never the OAuth state.
    login_id: str = field(default_factory=lambda: secrets.token_urlsafe(8))

    def __repr__(self) -> str:
        return "<_PendingLogin [redacted]>"


@dataclass(repr=False)
class _Credentials:
    client_id: str
    subject: str
    issuer: str = ISSUER
    email: str | None = None
    access_token: str | None = None
    refresh_token: str | None = None
    token_type: str | None = None
    scopes: list[str] = field(default_factory=list)
    expires_at: float = 0.0
    earliest_refresh_at: Any = None
    last_refreshed_at: float = 0.0
    connected_at: float = 0.0
    rotation_count: int = 0

    def __repr__(self) -> str:
        return "<_Credentials [redacted]>"

    @classmethod
    def from_stored(cls, stored: Any) -> _Credentials | None:
        if not isinstance(stored, dict) or not stored.get("client_id"):
            return None
        known = {k: stored[k] for k in cls.__dataclass_fields__ if k in stored}
        known.setdefault("subject", "")
        return cls(**known)


@dataclass(repr=False)
class _UnprovedLogin:
    """A sign-in whose save may or may not have landed. In memory only: never persisted or logged."""

    refresh_token: str
    client_id: str
    quota_epoch: int  # as the sign-in began saving: a change means a newer 429 than the store knows

    def __repr__(self) -> str:
        return "<_UnprovedLogin [redacted]>"


def _refresh_token_of(stored: Any) -> Any:
    creds = stored.get("credentials") if isinstance(stored, dict) else None
    return creds.get("refresh_token") if isinstance(creds, dict) else None


def _s256(verifier: str) -> str:
    return base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()


def _parse_pasted_url(pasted_url: Any) -> dict[str, str]:
    """The query of a pasted callback address, or ``bad_url``. Parses only: never fetches it."""
    if not isinstance(pasted_url, str) or len(pasted_url) > MAX_PASTED_URL_LENGTH:
        raise AccountLoginError("bad_url")
    try:
        parts = urllib.parse.urlsplit(pasted_url.strip())
        port = parts.port
        pairs = urllib.parse.parse_qsl(parts.query, max_num_fields=32)
    except ValueError:
        raise AccountLoginError("bad_url") from None
    params = dict(pairs)
    if (
        parts.scheme != "http"
        or parts.hostname != "127.0.0.1"
        or parts.username is not None
        or port != LOGIN_PORT
        or parts.path != LOGIN_CALLBACK_PATH
        or len(params) != len(pairs)  # a repeated parameter is not something OpenAI sends
        or not params.get("state")
        or not (params.get("code") or params.get("error"))  # cut off while copying
    ):
        raise AccountLoginError("bad_url")
    return params


def _verify_id_token(id_token: str, jwks: dict[str, Any], audience: str, nonce: str) -> dict[str, Any]:
    """Signature against JWKS, then issuer, audience, expiry and nonce. Runs in the executor.

    Only exception class names are logged, because PyJWT's messages can quote what it was given.
    """
    import jwt  # noqa: PLC0415 - ships with HA core; imported late so the module loads without it

    try:
        key = jwt.PyJWKSet.from_dict(jwks)[jwt.get_unverified_header(id_token).get("kid")]
        claims = jwt.decode(
            id_token,
            key.key,
            algorithms=_ID_TOKEN_ALGS,
            audience=audience,
            issuer=ISSUER,
            leeway=_CLOCK_LEEWAY_SECONDS,
            options={"require": _ID_TOKEN_REQUIRED_CLAIMS},
        )
    except jwt.MissingRequiredClaimError as err:
        _LOGGER.warning(
            "ChatGPT ID token has no %s claim", err.claim if err.claim in _ID_TOKEN_REQUIRED_CLAIMS else "?"
        )
        raise AccountLoginError("bad_identity" if err.claim == "sub" else "login_checks_failed") from None
    except (jwt.PyJWTError, KeyError, TypeError, ValueError) as err:
        # A header algorithm that does not fit the key's type surfaces as TypeError, not PyJWTError.
        _LOGGER.warning("ChatGPT ID token rejected (%s)", type(err).__name__)
        raise AccountLoginError("login_checks_failed") from None
    if not secrets.compare_digest(str(claims.get("nonce", "")).encode(), nonce.encode()):
        _LOGGER.warning("ChatGPT ID token rejected (nonce)")
        raise AccountLoginError("login_checks_failed")
    return claims


def _create_store(hass: HomeAssistant) -> Any:
    # Imported at call time so the module loads without Home Assistant's storage helper installed.
    from homeassistant.helpers.storage import Store  # noqa: PLC0415

    return Store(hass, STORAGE_VERSION, STORAGE_KEY, private=True, atomic_writes=True)


def _get_session(hass: HomeAssistant) -> Any:
    from homeassistant.helpers.aiohttp_client import async_get_clientsession  # noqa: PLC0415

    return async_get_clientsession(hass)


def _consume_task_result(task: asyncio.Task[Any]) -> None:
    """A caller may have gone away while the critical section finished."""
    if not task.cancelled():
        task.exception()


class ChatGPTAccountManager:
    """Tokens, host ID, owner, account state and quota cooldown for one HA instance."""

    def __init__(self, hass: HomeAssistant) -> None:
        self.hass = hass
        self._store = _create_store(hass)
        self._lock = asyncio.Lock()
        self._loaded = False
        self._closed = False
        self._dirty = False
        self._host_id: str | None = None
        self._state = STATE_DISCONNECTED
        self._owner_user_id: str | None = None
        self._credentials: _Credentials | None = None
        self._issued_client_id: str | None = None
        self._pending_login: _PendingLogin | None = None
        self._uncertain_login: _UnprovedLogin | None = None
        self._reconsent_next_login = False
        self._quota_until: float | None = None
        self._quota_probe_until: float | None = None
        self._quota_epoch = 0
        self._refresh_generation = 0
        self._runtime_active = False
        self._keepalive_unsub: Callable[[], None] | None = None
        self._keepalive_task: asyncio.Task[None] | None = None
        self._critical_tasks: set[asyncio.Task[Any]] = set()

    def __repr__(self) -> str:
        return f"<ChatGPTAccountManager state={self._state}>"

    # ---------------------------------------------------------------- read-only

    @property
    def state(self) -> str:
        """What the account can do now. ``status()`` also says when a sign-in's save is unproved."""
        return self._state

    @property
    def owner_user_id(self) -> str | None:
        return self._owner_user_id

    @property
    def account_identity(self) -> tuple[str | None, str] | None:
        """Who holds the account: (owner, subject), unchanged by a token rotation. None when no one."""
        creds = self._credentials
        return (self._owner_user_id, creds.subject) if creds is not None else None

    @property
    def host_id(self) -> str | None:
        """This HA box's stable ``ext_agent_host_id``. Not for status or logs."""
        return self._host_id

    @property
    def pending_login_id(self) -> str | None:
        """Identifier of the attempt in progress (not a secret, not the OAuth state), or None."""
        pending = self._pending_login
        return pending.login_id if pending is not None else None

    @property
    def has_pending_login(self) -> bool:
        pending = self._pending_login
        return pending is not None and _now() < pending.created_at + LOGIN_TTL_SECONDS

    def status(self) -> dict[str, Any]:
        """Non-secret account summary for the status view. Has no side effects."""
        creds = self._credentials
        connected = self._state == STATE_CONNECTED
        return {
            "state": STATE_SAVE_UNCERTAIN if self._uncertain_login else self._state,
            "owner_user_id": self._owner_user_id,
            "email": creds.email if creds else None,
            "plan_usage_enabled": bool(creds and connected and DIRECT_SCOPE in creds.scopes),
            "connected_at": _iso(creds.connected_at) if creds else None,
            "last_refreshed_at": _iso(creds.last_refreshed_at) if creds else None,
            "quota_cooldown_active": self._quota_blocked(_now()),
            "quota_next_probe_at": _iso(self._quota_next_probe()),
            "persist_pending": self._dirty,
            "login_pending": self.has_pending_login,
        }

    # ---------------------------------------------------------------- exclusivity

    async def _run_exclusive[T](self, func: Callable[[], Awaitable[T]], *, load: bool = True) -> T:
        """Run ``func`` under the one lock, in a task its caller cannot cancel.

        The server rotates as soon as it answers, so a refresh abandoned mid-flight would lose
        the only valid token, and releasing the lock with it still running would let another
        zone replay the old one. The task therefore owns the lock and always finishes. It is a
        tracked task, not a background one, so HA shutdown waits for the save.
        """
        task = self.hass.async_create_task(self._locked(func, load), name="climate_advisor_chatgpt_account")
        self._critical_tasks.add(task)
        task.add_done_callback(self._critical_tasks.discard)
        task.add_done_callback(_consume_task_result)
        return await asyncio.shield(task)

    async def _locked[T](self, func: Callable[[], Awaitable[T]], load: bool) -> T:
        async with self._lock:
            if self._closed:
                raise AIBackendError("ChatGPT account was removed", "auth")
            if load:
                await self._ensure_loaded()
            return await func()

    # ---------------------------------------------------------------- load and persist

    async def async_load(self) -> None:
        """Load once and start the keepalive. Failure is logged, never raised: AI stays optional."""

        async def nothing() -> None:
            return None

        try:
            await self._run_exclusive(nothing)
        except AIBackendError as err:
            _LOGGER.error("ChatGPT account not loaded: %s", err)
        self._runtime_active = True
        self._sync_keepalive()

    async def _ensure_loaded(self) -> None:
        if self._loaded:
            return
        # Stays unloaded on failure and retries later. Saving now would overwrite what could not be read.
        stored = await self._read_store()
        self._apply_stored(stored if isinstance(stored, dict) else {})
        self._loaded = True
        if self._state == STATE_REAUTH_REQUIRED:
            self._create_reauth_issue()

    async def _read_store(self) -> Any:
        try:
            return await self._store.async_load()
        except (HomeAssistantError, OSError, ValueError) as err:
            _LOGGER.error("Could not read ChatGPT account storage (%s)", type(err).__name__)
            raise AIBackendError("ChatGPT account storage could not be read", "transient") from None

    def _apply_stored(self, stored: dict[str, Any]) -> None:
        self._host_id = stored.get("host_id")
        self._owner_user_id = stored.get("owner_user_id")
        self._credentials = _Credentials.from_stored(stored.get("credentials"))
        self._issued_client_id = stored.get("issued_client_id") or (
            self._credentials.client_id if self._credentials else None
        )
        self._quota_until = _number(stored.get("quota_cooldown_until"))
        self._quota_probe_until = _number(stored.get("quota_probe_until"))
        state = stored.get("state")
        self._state = state if state in _STATES else STATE_DISCONNECTED
        if self._state == STATE_CONNECTED and not (self._credentials and self._credentials.refresh_token):
            self._state = STATE_REAUTH_REQUIRED

    def _to_stored(self) -> dict[str, Any]:
        return {
            "host_id": self._host_id,
            "state": self._state,
            "owner_user_id": self._owner_user_id,
            "credentials": asdict(self._credentials) if self._credentials else None,
            "issued_client_id": self._issued_client_id,
            "quota_cooldown_until": self._quota_until,
            "quota_probe_until": self._quota_probe_until,
        }

    async def _persist(self) -> None:
        """Save immediately and prove it.

        ``Store.async_save`` logs and swallows write errors, so returning proves nothing.
        Reading back is what makes "persisted before release" true. While HA is stopping the
        store holds the data for its final write and the read-back sees that pending copy.

        Refused while a sign-in's save is unproved: memory holds the state from before it, and
        writing that could replace live tokens nobody would then revoke.
        """
        if self._uncertain_login is not None:
            self._dirty = True
            raise _PersistError
        data = self._to_stored()
        try:
            await self._store.async_save(data)
            stored = await self._store.async_load()
        except (HomeAssistantError, OSError, ValueError, TypeError) as err:
            self._dirty = True
            _LOGGER.error("Could not save ChatGPT account (%s)", type(err).__name__)
            raise _PersistError from None
        if stored != data:
            self._dirty = True
            _LOGGER.error("ChatGPT account did not reach storage")
            raise _PersistError(unsaved=_refresh_token_of(stored) != _refresh_token_of(data))
        self._dirty = False

    async def _persist_or_mark_dirty(self) -> bool:
        try:
            await self._persist()
        except _PersistError:
            return False
        return True

    async def _flush_dirty(self) -> None:
        if self._dirty and not await self._persist_or_mark_dirty():
            raise AIBackendError("ChatGPT account could not be saved", "transient", no_retry=True)

    # ---------------------------------------------------------------- tokens

    @property
    def refresh_generation(self) -> int:
        """Counts refresh grants that succeeded, so a request can tell it has already had one."""
        return self._refresh_generation

    async def async_get_access_token(self, force_refresh: bool = False) -> str:
        """Return a usable access token, already persisted.

        ``force_refresh`` is for a 401 on a token that looked valid. Zones that ask at the same
        time share one refresh: only a caller whose token is still the current one causes it.

        Raises ``AIBackendError``: ``auth`` when not connected, ``AccountReauthRequired``
        (``terminal = True``) when a new sign-in is needed, ``transient`` or ``rate_limit`` when
        retrying later may work. Credentials are kept in all but the terminal case. Every refresh
        failure is ``no_retry``: a grant must not be sent again within the request that made it.
        """
        generation = self._refresh_generation if force_refresh else None
        return await self._run_exclusive(lambda: self._token_locked(generation))

    async def _token_locked(self, generation: int | None) -> str:
        await self._reconcile_uncertain_login()
        await self._flush_dirty()
        creds = self._credentials
        if self._state == STATE_REAUTH_REQUIRED:
            raise AccountReauthRequired
        if self._state != STATE_CONNECTED or creds is None or not creds.access_token:
            raise AIBackendError("ChatGPT account is not connected", "auth")

        now = _now()
        force = generation == self._refresh_generation
        if not force and now < creds.expires_at - _REFRESH_SKEW_SECONDS:
            return creds.access_token

        earliest = _parse_earliest(creds.earliest_refresh_at)
        if earliest is not None and earliest > now:
            if not force and now < creds.expires_at:
                return creds.access_token
            raise AIBackendError("ChatGPT token refresh is not allowed yet", "transient", no_retry=True)

        await self._refresh_locked()
        return self._credentials.access_token

    async def _refresh_locked(self) -> None:
        """One refresh grant, never retried: a replay after an unclear failure can end the session.

        Every failure below is ``no_retry`` so the request wrapper stops at once. Only a later request
        or keepalive tick sends the saved token again. It has no other: if the server did rotate and
        the answer was lost, that token is already spent and the answer is ``AccountReauthRequired``.
        """
        creds = self._credentials
        try:
            status, raw = await self._http(
                "post",
                TOKEN_URL,
                {
                    "grant_type": "refresh_token",
                    "client_id": creds.client_id,
                    "refresh_token": creds.refresh_token,
                    "resource": RESOURCE,
                },
            )
        except TimeoutError:
            _LOGGER.warning("ChatGPT token refresh timed out; credentials kept")
            raise AIBackendError("ChatGPT token refresh timed out", "transient", timeout=True, no_retry=True) from None
        except (ClientError, OSError) as err:
            # Unknown whether the server rotated. Credentials stay; the docs forbid erasing them here.
            _LOGGER.warning("ChatGPT token refresh failed (%s); credentials kept", type(err).__name__)
            raise AIBackendError(
                f"ChatGPT token refresh failed ({type(err).__name__})", "transient", no_retry=True
            ) from None

        payload = _parse_json(raw)
        if status != 200:
            await self._handle_refresh_failure(status, _error_code(payload))
            return
        await self._apply_refresh(payload)

    async def _handle_refresh_failure(self, status: int, code: str | None) -> None:
        detail = f"http {status}, code {code or 'none'}"
        _LOGGER.warning("ChatGPT token refresh rejected: %s", detail)
        if code in _TERMINAL_REFRESH_ERRORS:
            creds = self._credentials
            creds.access_token = None
            creds.refresh_token = None
            creds.expires_at = 0.0
            self._enter_reauth()
            await self._persist_or_mark_dirty()
            raise AccountReauthRequired
        message = f"ChatGPT token refresh failed ({detail})"
        if status >= 500:
            category = "transient"
        elif status == 429:
            category = "rate_limit"
        elif status in (401, 403) or code == "invalid_client":
            category = "auth"
        else:
            category = "permanent"
        raise AIBackendError(message, category, no_retry=True)

    async def _apply_refresh(self, payload: Any) -> None:
        payload = payload if isinstance(payload, dict) else {}
        new_access, new_refresh = payload.get("access_token"), payload.get("refresh_token")
        new_access = new_access if isinstance(new_access, str) and new_access else None
        new_refresh = new_refresh if isinstance(new_refresh, str) and new_refresh else None
        if not new_access and not new_refresh:
            _LOGGER.warning("ChatGPT token refresh returned no tokens; credentials kept")
            raise AIBackendError("ChatGPT token refresh returned no tokens", "transient", no_retry=True)

        now = _now()
        creds = self._credentials
        if new_refresh:
            creds.refresh_token = new_refresh
        if new_access:
            creds.access_token = new_access
            expires_in = _number(payload.get("expires_in"))
            creds.expires_at = now + (expires_in if expires_in else _DEFAULT_EXPIRES_IN)
        else:
            creds.expires_at = 0.0
        earliest = payload.get("earliest_refresh_at")
        creds.earliest_refresh_at = earliest if isinstance(earliest, int | float | str) else None
        creds.last_refreshed_at = now
        creds.rotation_count += 1
        if payload.get("token_type"):
            creds.token_type = payload["token_type"]
        if isinstance(payload.get("scope"), str) and payload["scope"]:
            creds.scopes = payload["scope"].split()
        self._refresh_generation += 1

        lost_scope = DIRECT_SCOPE not in creds.scopes
        if lost_scope:
            # Tokens stay, per the docs and so that a disconnect can still revoke them.
            self._enter_reauth()
        if not await self._persist_or_mark_dirty():
            # The rotated token is only in memory now: keep it, withhold it, and retry the save.
            self._sync_keepalive()
            raise AIBackendError("ChatGPT account could not be saved", "transient", no_retry=True)
        if lost_scope:
            raise AccountReauthRequired("ChatGPT plan usage was not granted; sign in again")
        if not new_access:
            raise AIBackendError("ChatGPT token refresh returned no access token", "transient", no_retry=True)
        _LOGGER.debug("ChatGPT token refresh succeeded")

    def _enter_reauth(self) -> None:
        self._state = STATE_REAUTH_REQUIRED
        self._create_reauth_issue()
        self._sync_keepalive()

    def _create_reauth_issue(self) -> None:
        ir.async_create_issue(
            self.hass,
            DOMAIN,
            REAUTH_ISSUE_ID,
            is_fixable=False,
            is_persistent=True,
            severity=ir.IssueSeverity.WARNING,
            translation_key=REAUTH_ISSUE_ID,
        )

    async def _http(self, method: str, url: str, form: dict[str, str] | None = None) -> tuple[int, str]:
        _check_auth_url(url)
        session = _get_session(self.hass)
        kwargs: dict[str, Any] = {"timeout": ClientTimeout(total=_HTTP_TIMEOUT_SECONDS), "allow_redirects": False}
        if form is not None:
            kwargs["data"] = form
        async with getattr(session, method)(url, **kwargs) as response:
            return response.status, await response.text(errors="replace")

    # ---------------------------------------------------------------- quota

    def _quota_blocked(self, now: float) -> bool:
        if self._quota_until is None:
            return False
        if now < self._quota_until:
            return True
        return self._quota_probe_until is not None and now < self._quota_probe_until

    def _quota_next_probe(self) -> float | None:
        if self._quota_until is None:
            return None
        return max(self._quota_until, self._quota_probe_until or self._quota_until)

    @property
    def quota_epoch(self) -> int:
        """Changes with each cooldown. Read it after the gate and pass it to ``note_request_succeeded``."""
        return self._quota_epoch

    def quota_cooldown_active(self) -> bool:
        """Should this request stop? Event loop only.

        After the cooldown this returns False to one caller per ``QUOTA_PROBE_LEASE_SECONDS``, which
        is the probe, and True to the rest. Only ``note_request_succeeded`` opens the gate. Not a
        pure read: use ``status()`` for that. The lease is saved so a restart cannot grant a second.
        """
        now = _now()
        if self._quota_blocked(now):
            return True
        if self._quota_until is None:
            return False
        self._quota_probe_until = now + QUOTA_PROBE_LEASE_SECONDS
        self._schedule_save()
        return False

    def set_quota_cooldown(self, error_code: str | None = None) -> None:
        """A plan usage-limit error from any zone stops every zone, from now. Event loop only."""
        _LOGGER.warning(
            "ChatGPT usage limit reached (code %s); pausing plan requests for all zones",
            error_code if isinstance(error_code, str) and _SAFE_CODE.match(error_code) else "none",
        )
        self._quota_until = _now() + QUOTA_COOLDOWN_SECONDS
        self._quota_probe_until = None
        self._quota_epoch += 1
        self._schedule_save()

    def note_request_succeeded(self, started_epoch: int | None = None) -> None:
        """A request worked, which is the only thing that lifts the cooldown.

        ``started_epoch`` is ``quota_epoch`` from when it passed the gate. A request that started
        before the latest cooldown, or any request before a probe is out, proves nothing.
        """
        if started_epoch is not None and started_epoch != self._quota_epoch:
            return
        if self._quota_until is not None and self._quota_probe_until is not None:
            self._clear_quota()

    def _clear_quota(self) -> None:
        self._quota_until = None
        self._quota_probe_until = None
        self._schedule_save()

    def _schedule_save(self) -> None:
        async def save() -> None:
            try:
                await self._run_exclusive(self._persist_or_mark_dirty)
            except AIBackendError:
                self._dirty = True

        self.hass.async_create_task(save(), name="climate_advisor_chatgpt_account_save")

    # ---------------------------------------------------------------- keepalive and unload

    @callback
    def _sync_keepalive(self) -> None:
        """A timer exists only while connected, runtime is active and the manager is open."""
        wanted = self._runtime_active and not self._closed and self._state == STATE_CONNECTED
        if wanted and self._keepalive_unsub is None:
            self._keepalive_unsub = async_track_time_interval(
                self.hass,
                self._on_keepalive_tick,
                KEEPALIVE_TICK,
                name="climate_advisor_chatgpt_account_keepalive",
                cancel_on_shutdown=True,
            )
        elif not wanted and self._keepalive_unsub is not None:
            self._keepalive_unsub()
            self._keepalive_unsub = None

    @callback
    def _on_keepalive_tick(self, _now_utc: datetime) -> None:
        if self._keepalive_task is None or self._keepalive_task.done():
            self._keepalive_task = self.hass.async_create_background_task(
                self._async_keepalive(), name="climate_advisor_chatgpt_account_keepalive"
            )

    async def _async_keepalive(self) -> None:
        try:
            await self._run_exclusive(self._keepalive_locked)
        except AIBackendError as err:
            _LOGGER.warning("ChatGPT keepalive did not complete: %s", err)

    async def _keepalive_locked(self) -> None:
        await self._flush_dirty()
        creds = self._credentials
        if self._state != STATE_CONNECTED or creds is None:
            return
        now = _now()
        if now < creds.last_refreshed_at + KEEPALIVE_AFTER_SECONDS:
            return
        earliest = _parse_earliest(creds.earliest_refresh_at)
        if earliest is not None and earliest > now:
            return
        await self._refresh_locked()

    @callback
    def async_cancel_runtime_tasks(self) -> None:
        """For unload, which also runs on reload. Never touches credentials.

        Call when the last loaded entry unloads, since the timer is shared by all zones. A
        refresh already in flight still finishes and saves. The manager stays in ``hass.data``,
        because a second instance could replay a token this one rotated;
        ``async_get_account_manager`` re-arms the timer on the next setup.
        """
        self._runtime_active = False
        self._sync_keepalive()
        if self._keepalive_task is not None and not self._keepalive_task.done():
            self._keepalive_task.cancel()
        self._keepalive_task = None

    # ---------------------------------------------------------------- sign-in

    async def async_begin_login(self) -> str:
        """Start a sign-in and return the authorize URL. A new call replaces any earlier attempt.

        Allowed from ``reauth_required``. A returning account reuses its issued client ID, as the
        docs require; a first registration uses ``dynamic_agent_client``.
        """

        async def begin() -> str:
            await self._reconcile_uncertain_login()
            if self._state == STATE_CONNECTED:
                raise AccountLoginError("already_connected")
            if not self._host_id:  # the first use of an account: nothing is written before this
                self._host_id = f"urn:uuid:{uuid.uuid4()}"
                await self._persist_or_mark_dirty()
            verifier, state, nonce = secrets.token_urlsafe(64), secrets.token_urlsafe(32), secrets.token_urlsafe(32)
            client_id = self._issued_client_id or _BOOTSTRAP_CLIENT_ID
            params = {"client_id": client_id}
            if client_id == _BOOTSTRAP_CLIENT_ID:
                params["agent_name_hint"] = APP_NAME
            params.update(
                ext_agent_host_id=self._host_id,
                response_type="code",
                redirect_uri=LOGIN_REDIRECT_URI,
                scope=LOGIN_SCOPES,
                resource=RESOURCE,
                state=state,
                nonce=nonce,
                code_challenge_method="S256",
                code_challenge=_s256(verifier),
            )
            if self._reconsent_next_login:
                params["prompt"] = "consent"  # errors-and-recovery: repeat OAuth for a later opt-in
            self._pending_login = _PendingLogin(verifier, state, nonce, client_id, _now())
            return f"{_check_auth_url(AUTHORIZE_URL)}?{urllib.parse.urlencode(params)}"

        return await self._run_login(begin)

    async def async_complete_login(self, pasted_url: str, owner_user_id: str) -> dict[str, Any]:
        """Finish a sign-in from the pasted callback address. Returns ``status()``.

        Raises ``AccountLoginError``. A mistyped paste (``bad_url``, ``state_mismatch``) leaves the
        attempt in place for a retry; anything that passes the state check uses it up, because
        an authorization code works once.
        """

        async def complete() -> dict[str, Any]:
            await self._reconcile_uncertain_login()
            if self._state == STATE_CONNECTED:
                raise AccountLoginError("already_connected")
            if not owner_user_id:
                raise AccountLoginError("owner_required")
            pending = self._pending_login
            if pending is None:
                raise AccountLoginError("no_pending_login")
            if not self.has_pending_login:
                self._pending_login = None
                raise AccountLoginError("login_expired")
            params = _parse_pasted_url(pasted_url)
            if not secrets.compare_digest(params["state"].encode(), pending.state.encode()):
                raise AccountLoginError("state_mismatch")
            self._pending_login = None
            await self._finish_login(pending, params, owner_user_id)
            _LOGGER.info("ChatGPT sign-in completed")
            return self.status()

        return await self._run_login(complete)

    @callback
    def async_cancel_login(self, login_id: str | None = None) -> None:
        """Forget the attempt in progress. With ``login_id``, only if it is still that attempt."""
        pending = self._pending_login
        if login_id is None or (pending is not None and pending.login_id == login_id):
            self._pending_login = None

    async def _run_login[T](self, func: Callable[[], Awaitable[T]]) -> T:
        try:
            return await self._run_exclusive(func)
        except AccountLoginError:
            raise
        except AIBackendError:  # storage could not be read: the contract has one storage reason
            raise AccountLoginError("save_failed") from None

    async def _finish_login(self, pending: _PendingLogin, params: dict[str, str], owner_user_id: str) -> None:
        if params.get("error"):
            code = params["error"] if _SAFE_CODE.match(params["error"]) else "other"
            _LOGGER.warning("ChatGPT sign-in returned an error (%s)", code)
            raise AccountLoginError("consent_denied" if code == "access_denied" else "exchange_failed")
        if not params.get("code"):
            raise AccountLoginError("login_checks_failed")

        client_id = await self._resolve_client_id(pending, params.get("client_id"))
        tokens = await self._exchange_code(client_id, params["code"], pending.verifier)
        # From here the server holds a live refresh token. Any refusal below must revoke it.
        new_refresh = tokens.get("refresh_token") if isinstance(tokens, dict) else None
        try:
            await self._accept_exchange(tokens, client_id, pending, owner_user_id)
        except AccountLoginError as err:
            if isinstance(new_refresh, str) and new_refresh and not err.tokens_may_be_saved:
                await self._revoke_refused(new_refresh, client_id)
            raise

    async def _reconcile_uncertain_login(self) -> None:
        """Settle an unproved save from the store. Call under the lock. Raises if it cannot be read.

        ``_persist`` has returned, so no write of those tokens is still pending and this read is
        final: tokens that are not in the store are held by nobody, and only revoking ends them.
        """
        uncertain = self._uncertain_login
        if uncertain is None:
            return
        stored = await self._read_store()
        self._uncertain_login = None
        if _refresh_token_of(stored) == uncertain.refresh_token:
            quota = self._quota_until, self._quota_probe_until
            self._apply_stored(stored)
            self._dirty = False
            if self._quota_epoch != uncertain.quota_epoch:
                self._quota_until, self._quota_probe_until = quota
                await self._persist_or_mark_dirty()  # stays dirty on failure, with the cooldown kept
            self._mark_signed_in()
            return
        await self._revoke_refused(uncertain.refresh_token, uncertain.client_id)

    async def _revoke_refused(self, refresh_token: str, client_id: str) -> None:
        """Best effort, bounded, never raises and never touches saved credentials or state.

        Runs inside the login's lock-owning task like ``_revoke_and_forget`` does; the revoke
        calls do not take the lock, so it cannot deadlock. The caller's cancellation cannot
        reach this task (``_run_exclusive`` shields it), and nothing is mutated here.
        """
        try:
            async with asyncio.timeout(_LOGIN_REVOKE_TIMEOUT_SECONDS):
                outcome = await self._async_revoke_token(refresh_token, client_id)
        except Exception as err:  # noqa: BLE001 - the caller must still see the original refusal
            _LOGGER.warning("Could not revoke the tokens of a refused ChatGPT sign-in (%s)", type(err).__name__)
            return
        if outcome != REVOCATION_CONFIRMED:
            _LOGGER.warning("Tokens of a refused ChatGPT sign-in may still be live; revocation unconfirmed")

    async def _accept_exchange(self, tokens: Any, client_id: str, pending: _PendingLogin, owner_user_id: str) -> None:
        try:
            tokens = _TOKEN_RESPONSE_SCHEMA(tokens)
        except vol.Invalid:
            _LOGGER.warning("ChatGPT sign-in response had missing or malformed tokens")
            raise AccountLoginError("login_checks_failed") from None

        jwks = await self._fetch_jwks()
        claims = await self.hass.async_add_executor_job(
            functools.partial(_verify_id_token, tokens["id_token"], jwks, client_id, pending.nonce)
        )
        subject = claims.get("sub")
        returning = self._credentials.subject if self._credentials else None
        if not isinstance(subject, str) or not subject:
            _LOGGER.warning("ChatGPT sign-in was for a missing account")
            raise AccountLoginError("bad_identity")
        if returning and subject != returning:
            _LOGGER.warning("ChatGPT sign-in was for a different account than the one connected")
            raise AccountLoginError("different_account")
        if tokens["access_token"] == tokens["refresh_token"] or not self._access_token_matches(tokens, client_id):
            _LOGGER.warning("ChatGPT sign-in returned tokens that do not belong together")
            raise AccountLoginError("login_checks_failed")

        scope = tokens.get("scope")
        scopes = scope.split() if isinstance(scope, str) else []
        if DIRECT_SCOPE not in scopes:
            # A valid ID token alone does not authorize plan usage. The next attempt asks again.
            self._reconsent_next_login = True
            _LOGGER.warning("ChatGPT plan usage was not granted")
            raise AccountLoginError("missing_scope")

        await self._store_login(tokens, claims, scopes, client_id, subject, owner_user_id)

    async def _resolve_client_id(self, pending: _PendingLogin, returned: str | None) -> str:
        if pending.client_id != _BOOTSTRAP_CLIENT_ID:
            # Reauthorization may omit it, but a different one must not replace the registration.
            if returned and returned != pending.client_id:
                _LOGGER.warning("ChatGPT sign-in returned a different client ID")
                raise AccountLoginError("login_checks_failed")
            return pending.client_id
        if not re.fullmatch(r"oaiapp_\w+", returned or ""):
            _LOGGER.warning("ChatGPT registration is incomplete: no issued client ID")
            raise AccountLoginError("login_checks_failed")
        # Saved before the exchange so a retry does not register a second client.
        self._issued_client_id = returned
        await self._persist_or_mark_dirty()
        return returned

    async def _exchange_code(self, client_id: str, code: str, verifier: str) -> Any:
        """One attempt. A code works once, so a network failure is never retried."""
        try:
            status, raw = await self._http(
                "post",
                TOKEN_URL,
                {
                    "grant_type": "authorization_code",
                    "client_id": client_id,
                    "code": code,
                    "code_verifier": verifier,
                    "redirect_uri": LOGIN_REDIRECT_URI,
                    "resource": RESOURCE,
                },
            )
        except (ClientError, OSError, TimeoutError) as err:
            _LOGGER.warning("ChatGPT sign-in exchange failed (%s)", type(err).__name__)
            raise AccountLoginError("exchange_failed") from None
        payload = _parse_json(raw)
        if status != 200:
            _LOGGER.warning(
                "ChatGPT sign-in exchange rejected: http %s, code %s", status, _error_code(payload) or "none"
            )
            raise AccountLoginError("exchange_failed")
        return payload

    async def _fetch_jwks(self) -> dict[str, Any]:
        """Over HA's session rather than PyJWKClient, which blocks. Fresh per sign-in, so a rotated key is seen."""
        try:
            status, raw = await self._http("get", DISCOVERY_URL)
            doc = _parse_json(raw) if status == 200 else None
            if not isinstance(doc, dict):
                raise AccountLoginError("exchange_failed")
            if doc.get("issuer") != ISSUER or not isinstance(doc.get("jwks_uri"), str):
                raise AccountLoginError("login_checks_failed")
            status, raw = await self._http("get", doc["jwks_uri"])
        except AccountLoginError:
            raise
        except AIBackendError:  # discovery pointed off the documented host
            _LOGGER.warning("OpenAI discovery has an out-of-bounds JWKS address")
            raise AccountLoginError("login_checks_failed") from None
        except (ClientError, OSError, TimeoutError) as err:
            _LOGGER.warning("Could not fetch OpenAI signing keys (%s)", type(err).__name__)
            raise AccountLoginError("exchange_failed") from None
        jwks = _parse_json(raw) if status == 200 else None
        if not isinstance(jwks, dict):
            raise AccountLoginError("exchange_failed")
        return jwks

    @staticmethod
    def _access_token_matches(tokens: dict[str, Any], client_id: str) -> bool:
        """An access token may be opaque; only one that is a JWT and names a client can disagree."""
        claims = _read_claims(tokens["access_token"])
        return not claims or claims.get("client_id") in (None, client_id)

    async def _store_login(
        self,
        tokens: dict[str, Any],
        claims: dict[str, Any],
        scopes: list[str],
        client_id: str,
        subject: str,
        owner_user_id: str,
    ) -> None:
        now = _now()
        expires_in = _number(tokens.get("expires_in"))
        earliest = tokens.get("earliest_refresh_at")
        email = claims.get("email")
        credentials = _Credentials(
            client_id=client_id,
            subject=subject,
            email=email if isinstance(email, str) else None,
            access_token=tokens["access_token"],
            refresh_token=tokens["refresh_token"],
            token_type=tokens.get("token_type"),
            scopes=scopes,
            expires_at=now + (expires_in if expires_in else _DEFAULT_EXPIRES_IN),
            earliest_refresh_at=earliest if isinstance(earliest, int | float | str) else None,
            last_refreshed_at=now,
            connected_at=now,
        )
        previous = self._state, self._owner_user_id, self._credentials
        previous_quota = self._quota_until, self._quota_probe_until
        quota_epoch = self._quota_epoch
        self._state, self._owner_user_id, self._credentials = STATE_CONNECTED, owner_user_id, credentials
        self._issued_client_id = client_id
        self._quota_until = self._quota_probe_until = None
        try:
            await self._persist()
        except _PersistError as err:
            self._state, self._owner_user_id, self._credentials = previous
            if self._quota_epoch == quota_epoch:  # otherwise a 429 arrived during the save: keep it
                self._quota_until, self._quota_probe_until = previous_quota
            if not err.unsaved:
                self._uncertain_login = _UnprovedLogin(tokens["refresh_token"], client_id, quota_epoch)
            raise AccountLoginError("save_failed", tokens_may_be_saved=not err.unsaved) from None
        self._mark_signed_in()

    def _mark_signed_in(self) -> None:
        self._reconsent_next_login = False
        ir.async_delete_issue(self.hass, DOMAIN, REAUTH_ISSUE_ID)
        self._sync_keepalive()

    # ---------------------------------------------------------------- disconnect and removal

    async def async_disconnect(self) -> str:
        """Revoke, then clear. Returns a ``REVOCATION_*`` value.

        Clears even when revocation is unconfirmed, so a departed owner cannot strand the
        install: on ``REVOCATION_UNCONFIRMED`` the docs require telling the user to disconnect
        the app in ChatGPT Settings. The host ID is kept. An unproved sign-in needs no read of the
        store: both possible holders are revoked and the save replaces whichever it holds.
        """

        async def disconnect() -> str:
            outcome = await self._revoke_and_forget()
            if not await self._persist_or_mark_dirty():
                raise AIBackendError("ChatGPT account could not be saved", "transient")
            _LOGGER.info("ChatGPT account disconnected (revocation: %s)", outcome)
            return outcome

        return await self._run_exclusive(disconnect)

    async def async_remove(self) -> str:
        """Final removal: revoke and delete the store. Use ``async_remove_account`` from setup code.

        Stale client references are refused from here on, and a later setup builds a new manager
        with a new host ID. Returns a ``REVOCATION_*`` value and never raises for storage trouble.
        What is in memory is revoked regardless; tokens only in an unreadable file cannot be, so
        that case is ``REVOCATION_UNCONFIRMED``.
        """

        async def remove() -> str:
            unread = False
            try:
                await self._ensure_loaded()
            except AIBackendError:
                unread = True
                _LOGGER.warning(
                    "Removing the ChatGPT account without reading it. Any sign-in it held could not be "
                    "revoked: disconnect the app in ChatGPT Settings"
                )
            outcome = await self._revoke_and_forget()
            await self._erase_store()
            self._closed = True
            if self.hass.data.get(CHATGPT_ACCOUNT_HASS_KEY) is self:
                self.hass.data.pop(CHATGPT_ACCOUNT_HASS_KEY)
            final = REVOCATION_UNCONFIRMED if unread else outcome
            _LOGGER.info("ChatGPT account removed (revocation: %s)", final)
            return final

        self.async_cancel_runtime_tasks()
        return await self._run_exclusive(remove, load=False)

    async def _erase_store(self) -> None:
        """Delete the store or, failing that, blank it: an empty record loads as a fresh install."""
        try:
            await self._store.async_remove()
            return
        except OSError as err:
            _LOGGER.error("Could not delete ChatGPT account storage (%s); blanking it", type(err).__name__)
        try:
            await self._store.async_save({})
            if not await self._store.async_load():
                return
        except (HomeAssistantError, OSError, ValueError, TypeError) as err:
            _LOGGER.error("Could not blank ChatGPT account storage (%s)", type(err).__name__)
        _LOGGER.error(
            "ChatGPT account storage may still hold the removed account and would load again if the "
            "integration is re-added: delete .storage/%s by hand",
            STORAGE_KEY,
        )

    async def _revoke_and_forget(self) -> str:
        creds = self._credentials
        uncertain = self._uncertain_login
        outcome = await self._async_revoke(creds) if creds and creds.refresh_token else REVOCATION_NOT_NEEDED
        if uncertain is not None:
            other = await self._async_revoke_token(uncertain.refresh_token, uncertain.client_id)
            outcome = other if outcome == REVOCATION_NOT_NEEDED or other != REVOCATION_CONFIRMED else outcome
        self._uncertain_login = None
        self._state = STATE_DISCONNECTED
        self._owner_user_id = None
        self._credentials = None
        # A later sign-in may be a different account, which needs its own registration.
        self._issued_client_id = None
        self._pending_login = None
        self._reconsent_next_login = False
        self._quota_until = self._quota_probe_until = None
        ir.async_delete_issue(self.hass, DOMAIN, REAUTH_ISSUE_ID)
        self._sync_keepalive()
        return outcome

    async def _async_revoke(self, creds: _Credentials) -> str:
        return await self._async_revoke_token(creds.refresh_token, creds.client_id)

    async def _async_revoke_token(self, refresh_token: str, client_id: str) -> str:
        """profiles-and-sessions: endpoint from discovery; empty 200 is success; retry network and 5xx."""
        form = {"token": refresh_token, "token_type_hint": "refresh_token", "client_id": client_id}
        endpoint: str | None = None
        for delay in (None, *_REVOKE_BACKOFF_SECONDS):
            if delay is not None:
                await asyncio.sleep(delay)
            try:
                endpoint = endpoint or await self._revocation_endpoint()
                status, _ = await self._http("post", endpoint, form)
            except (ClientError, OSError, TimeoutError, _RetryableHttp) as err:
                _LOGGER.warning("ChatGPT revocation attempt failed (%s)", type(err).__name__)
                continue
            except AIBackendError as err:
                _LOGGER.warning("ChatGPT revocation not attempted: %s", err)
                return REVOCATION_UNCONFIRMED
            if status == 200:
                return REVOCATION_CONFIRMED
            _LOGGER.warning("ChatGPT revocation returned http %s", status)
            if status < 500:
                return REVOCATION_UNCONFIRMED
        return REVOCATION_UNCONFIRMED

    async def _revocation_endpoint(self) -> str:
        status, raw = await self._http("get", DISCOVERY_URL)
        if status >= 500:
            raise _RetryableHttp
        doc = _parse_json(raw)
        endpoint = doc.get("revocation_endpoint") if isinstance(doc, dict) and status == 200 else None
        if not isinstance(endpoint, str):
            raise AIBackendError("OpenAI discovery has no revocation endpoint", "permanent")
        return _check_auth_url(endpoint)


def get_account_manager(hass: HomeAssistant) -> ChatGPTAccountManager:
    """The instance's manager, created if needed. Not loaded yet: each operation loads on demand."""
    manager = hass.data.get(CHATGPT_ACCOUNT_HASS_KEY)
    if manager is None:
        manager = ChatGPTAccountManager(hass)
        hass.data[CHATGPT_ACCOUNT_HASS_KEY] = manager
    return manager


async def async_get_account_manager(hass: HomeAssistant) -> ChatGPTAccountManager:
    """Get or create, await the load and arm the keepalive. For ``async_setup_entry``."""
    manager = get_account_manager(hass)
    await manager.async_load()
    return manager


async def async_remove_account(hass: HomeAssistant) -> str:
    """Final removal, for ``async_remove_entry`` on the last entry. Works if setup never ran."""
    return await get_account_manager(hass).async_remove()
