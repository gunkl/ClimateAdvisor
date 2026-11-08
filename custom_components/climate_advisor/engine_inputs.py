"""The coordinator-owned facts the automation engine's decisions read, passed in at the decision (Issue #1443).

Occupant view: the decisions Climate Advisor makes for the house (may a window-open fan session start, the
nat-vent soft start, the time-of-use Away bank) act on the forecast, today's observed outdoor peak and the active
TOU window the coordinator holds at the moment it asks, never on a copy that one path forgot to refresh.

Ownership rule (owner's choice, "number 2"): the coordinator is the single owner and the engine holds no copy.
Single facts are VALUES PASSED IN at the decision call:

* The coordinator computes ``EngineFacts`` (a frozen snapshot: the 2-hour forecast peak, today's outdoor peak and
  sample count, the Away/Vacation TOU window) with ``coordinator.engine_facts(now)`` and hands it to the engine call
  it makes (``decide(coordinator, ...)`` -> ``engine.decision_facts(facts)``). The engine reads it for that call
  only; nothing is stored on the engine and a snapshot is never reused across calls (it expires after
  ``FACTS_MAX_AGE``).
* The pure decisions get the single values as arguments, as before: ``decide_nat_vent_start_veto(forecast_peak_2h=)``
  (Issue #1138) and the soft-start peak/sample count (Issue #540). The engine never sees the forecast list.

Accessors that stay, each because an argument is not practical (listed in the plan's "Owner decisions"):

* ``facts source`` fallback: an engine-internal timer (grace expiry re-check, override confirmation, the fan
  min-runtime cycle, ...) or a read-only reader (API, sensors) has no coordinator call frame to pass facts in, so
  the engine asks its source to BUILD a fresh frozen snapshot at that moment (``build_facts(now)``).
* the learned thermal model (``thermal_model()``, then ``_control_model_provider``): a whole model, read by many
  decision helpers.

``StaticEngineInputs`` is the source for a bare engine (the headless engine-only harness, unit tests): the caller
that built the engine owns its fields.
"""

from __future__ import annotations

import contextlib
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol

from homeassistant.util import dt as dt_util

# A passed-in snapshot is for ONE decision: older than this (a timer that fires later inside the context that
# captured it, a long await) it is not used, and a fresh one is built instead.
FACTS_MAX_AGE = timedelta(seconds=60)


@dataclass(frozen=True)
class TouAvWindow:
    """The active Away/Vacation time-of-use window (Issue #899): band expansion, schedule id, precondition target.

    ``TOU_AV_INACTIVE`` outside a window (0.0 / None / None), exactly the engine's former defaults.
    """

    expansion_f: float = 0.0
    schedule_id: str | None = None
    precondition_target: float | None = None


TOU_AV_INACTIVE = TouAvWindow()


def _parse_forecast_dt(dt_str: str | None) -> datetime | None:
    """Parse an ISO 8601 forecast datetime string; return None on failure."""
    if not dt_str:
        return None
    try:
        return dt_util.parse_datetime(dt_str)
    except Exception:  # noqa: BLE001
        return None


def forecast_peak_next_2h(hourly: list[dict] | None, now: datetime) -> float | None:
    """Highest hourly-forecast temperature in the next 2 hours, or None (Issue #1138).

    The shell half of the forecast-peak start guard: clock and datetime parsing live here, the comparison lives in
    ``nat_vent_gate.decide_nat_vent_start_veto``. Moved verbatim from ``AutomationEngine._forecast_peak_next_2h``.
    """
    hourly = hourly or []
    if not hourly:
        return None
    now_dt = now if now.tzinfo is not None else now.replace(tzinfo=UTC)
    temps = [
        h["temperature"]
        for h in hourly
        if h.get("temperature") is not None
        and (parsed := _parse_forecast_dt(h.get("datetime"))) is not None
        and now_dt
        < (parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC))
        <= now_dt + timedelta(hours=2)
    ]
    return max(temps) if temps else None


@dataclass(frozen=True)
class EngineFacts:
    """The single coordinator-owned facts one decision reads, frozen at ``as_of`` (Issue #1443)."""

    as_of: datetime
    forecast_peak_2h: float | None = None  # hourly-forecast peak over (as_of, as_of + 2 h], #1138
    outdoor_today_peak: float | None = None  # today's observed outdoor peak so far, #540
    outdoor_today_sample_count: int = 0
    tou_av: TouAvWindow = TOU_AV_INACTIVE  # the active Away/Vacation TOU window, #899

    def fresh_for(self, now: datetime) -> bool:
        """True when this snapshot was built for a decision at ``now`` (within ``FACTS_MAX_AGE``)."""
        try:
            return abs(now - self.as_of) <= FACTS_MAX_AGE
        except TypeError:  # naive vs aware: not the same clock
            return False


def build_engine_facts(
    now: datetime,
    *,
    hourly_forecast: list[dict] | None,
    outdoor_history: list[tuple[str, float]] | None,
    tou_av: TouAvWindow | None,
) -> EngineFacts:
    """The one place a snapshot is computed (the coordinator's ``engine_facts`` and a bare engine's source)."""
    temps = [t for _, t in (outdoor_history or [])]
    return EngineFacts(
        as_of=now,
        forecast_peak_2h=forecast_peak_next_2h(hourly_forecast, now),
        outdoor_today_peak=max(temps) if temps else None,
        outdoor_today_sample_count=len(temps),
        tou_av=tou_av if isinstance(tou_av, TouAvWindow) else TOU_AV_INACTIVE,
    )


class EngineInputs(Protocol):
    """The engine's facts source: builds a frozen snapshot on request, plus the thermal-model accessor."""

    def build_facts(self, now: datetime) -> EngineFacts:
        """A fresh frozen snapshot for a decision at ``now`` (only where no snapshot was passed in)."""

    def thermal_model(self) -> dict:
        """The learned thermal model (``learning.get_thermal_model()``'s last answer); ``{}`` when none."""


@dataclass
class StaticEngineInputs:
    """Source for a bare engine (no coordinator): the caller that built the engine owns these fields."""

    thermal: dict = field(default_factory=dict)
    hourly_forecast: list[dict] = field(default_factory=list)
    today_peak: float | None = None
    today_sample_count: int = 0
    tou_av: TouAvWindow = TOU_AV_INACTIVE

    def build_facts(self, now: datetime) -> EngineFacts:
        return EngineFacts(
            as_of=now,
            forecast_peak_2h=forecast_peak_next_2h(self.hourly_forecast, now),
            outdoor_today_peak=self.today_peak,
            outdoor_today_sample_count=self.today_sample_count,
            tou_av=self.tou_av,
        )

    def thermal_model(self) -> dict:
        return self.thermal or {}


class _EmptyEngineInputs:
    """The immutable 'nothing known' source (an engine built via ``object.__new__`` in tests)."""

    __slots__ = ()

    def build_facts(self, now: datetime) -> EngineFacts:
        return EngineFacts(as_of=now)

    def thermal_model(self) -> dict:
        return {}


EMPTY_ENGINE_INPUTS = _EmptyEngineInputs()


class CoordinatorEngineInputs:
    """Production source: asks the coordinator to compute a snapshot (no copy, no setters, no live per-fact reads).

    ``build_facts`` delegates to ``coordinator.engine_facts(now)``, the same builder the coordinator uses for the
    snapshots it passes in itself; ``thermal_model`` is the model accessor (the coordinator's ``_thermal_model``).
    """

    __slots__ = ("_coordinator",)

    def __init__(self, coordinator: Any) -> None:
        self._coordinator = coordinator

    def build_facts(self, now: datetime) -> EngineFacts:
        builder = getattr(self._coordinator, "engine_facts", None)
        return builder(now) if callable(builder) else EngineFacts(as_of=now)

    def thermal_model(self) -> dict:
        return getattr(self._coordinator, "_thermal_model", None) or {}


# --------------------------------------------------------------------------- passing the facts in (the call sites)
def _facts_scope(coordinator: Any, method: Any, at: datetime) -> contextlib.AbstractContextManager[Any]:
    """The engine's ``decision_facts`` scope holding the coordinator's snapshot for ``at`` (a no-op scope when either
    side is not a real one, e.g. a test stub: the engine then builds its facts from its own source, as before)."""
    engine = getattr(method, "__self__", None)
    if engine is None:
        engine = getattr(coordinator, "automation_engine", None)
    builder = getattr(coordinator, "engine_facts", None)
    scope = getattr(engine, "decision_facts", None)
    facts = builder(at) if callable(builder) else None
    if not isinstance(facts, EngineFacts) or not callable(scope):
        return contextlib.nullcontext()
    return scope(facts)


async def decide(coordinator: Any, method: Callable[..., Awaitable[Any]], *args: Any, **kwargs: Any) -> Any:
    """Await an engine decision method with ``coordinator``'s facts passed in, computed when the call starts.

    Every call from outside automation.py into an engine method that reaches a coordinator-owned fact goes through
    this (or ``decide_sync``); ``tests/test_engine_state_ownership_static.py`` enforces it.
    """
    with _facts_scope(coordinator, method, dt_util.now()):
        return await method(*args, **kwargs)


def decide_sync(
    coordinator: Any, method: Callable[..., Any], *args: Any, facts_at: datetime | None = None, **kwargs: Any
) -> Any:
    """Call a synchronous engine decision method with ``coordinator``'s facts passed in (as of ``facts_at``)."""
    with _facts_scope(coordinator, method, facts_at if facts_at is not None else dt_util.now()):
        return method(*args, **kwargs)
