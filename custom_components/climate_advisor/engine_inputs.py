"""The coordinator-owned facts the automation engine's decisions read (Issue #1443).

Occupant view: the decisions Climate Advisor makes for the house (may a window-open fan session start, the
nat-vent soft start, the time-of-use Away bank, the remote-sensor swing) act on the forecast, the learned
thermal model, today's observed outdoor peak and the active TOU window the coordinator holds RIGHT NOW, never
on a copy that one path forgot to refresh.

Ownership rule: the coordinator is the single owner of these facts and the engine holds no copy. The engine
reads them through ONE read-only provider handed to its constructor (``AutomationEngine(..., inputs=...)``):

* ``CoordinatorEngineInputs`` -- production. Reads the coordinator's own attributes at call time; it has no
  setters, so the engine cannot write them back.
* ``StaticEngineInputs`` -- a bare engine with no coordinator (the headless engine-only harness, unit tests).
  Its fields are owned by whoever built the engine (the scenario, the test).

Single facts still reach the decision functions as arguments: the engine never sees the forecast list, only
``forecast_peak_next_2h(now)``, which it passes to ``nat_vent_gate.decide_nat_vent_start_veto`` as
``forecast_peak_2h`` (Issue #1138), and the soft-start peak/sample count, passed to the soft-start decision
(Issue #540). A provider (rather than an argument from the coordinator) is used because the same decisions also
run from engine-internal timers that have no coordinator call frame.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol

from homeassistant.util import dt as dt_util


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


class EngineInputs(Protocol):
    """What the engine may read of the coordinator's state. Read-only by construction."""

    def thermal_model(self) -> dict:
        """The learned thermal model (``learning.get_thermal_model()``'s last answer); ``{}`` when none."""

    def forecast_peak_next_2h(self, now: datetime) -> float | None:
        """The hourly forecast's peak over (now, now + 2 h], or None."""

    def outdoor_today_peak(self) -> float | None:
        """Today's observed outdoor peak so far, or None before the first recorded reading."""

    def outdoor_today_sample_count(self) -> int:
        """How many outdoor readings today's peak is taken over."""

    def tou_av_window(self) -> TouAvWindow:
        """The active Away/Vacation TOU window, ``TOU_AV_INACTIVE`` outside one."""


@dataclass
class StaticEngineInputs:
    """Inputs for a bare engine (no coordinator): the caller that built the engine owns these fields."""

    thermal: dict = field(default_factory=dict)
    hourly_forecast: list[dict] = field(default_factory=list)
    today_peak: float | None = None
    today_sample_count: int = 0
    tou_av: TouAvWindow = TOU_AV_INACTIVE

    def thermal_model(self) -> dict:
        return self.thermal or {}

    def forecast_peak_next_2h(self, now: datetime) -> float | None:
        return forecast_peak_next_2h(self.hourly_forecast, now)

    def outdoor_today_peak(self) -> float | None:
        return self.today_peak

    def outdoor_today_sample_count(self) -> int:
        return self.today_sample_count

    def tou_av_window(self) -> TouAvWindow:
        return self.tou_av


class _EmptyEngineInputs:
    """The immutable 'nothing known' provider (an engine built via ``object.__new__`` in tests)."""

    __slots__ = ()

    def thermal_model(self) -> dict:
        return {}

    def forecast_peak_next_2h(self, now: datetime) -> float | None:
        return None

    def outdoor_today_peak(self) -> float | None:
        return None

    def outdoor_today_sample_count(self) -> int:
        return 0

    def tou_av_window(self) -> TouAvWindow:
        return TOU_AV_INACTIVE


EMPTY_ENGINE_INPUTS = _EmptyEngineInputs()


class CoordinatorEngineInputs:
    """Production provider: reads the coordinator's own state at call time (no copy, no setters).

    Owners: ``coordinator._thermal_model`` (30-min cycle / forecast cycle), ``coordinator._hourly_forecast_temps``
    (``_refresh_hourly_forecast`` and the kept-forecast restore, #1337), ``coordinator._outdoor_temp_history``
    (each recorded outdoor reading, the same-day restore, the midnight reset) and ``coordinator._tou_av_window``
    (``_sync_tou_away_vacation_state`` each cycle). ``getattr`` defaults cover a coordinator still inside its
    ``__init__`` (the engine is built before these attributes exist).
    """

    __slots__ = ("_coordinator",)

    def __init__(self, coordinator: Any) -> None:
        self._coordinator = coordinator

    def thermal_model(self) -> dict:
        return getattr(self._coordinator, "_thermal_model", None) or {}

    def forecast_peak_next_2h(self, now: datetime) -> float | None:
        return forecast_peak_next_2h(getattr(self._coordinator, "_hourly_forecast_temps", None), now)

    def _today_outdoor(self) -> list[float]:
        return [t for _, t in (getattr(self._coordinator, "_outdoor_temp_history", None) or [])]

    def outdoor_today_peak(self) -> float | None:
        temps = self._today_outdoor()
        return max(temps) if temps else None

    def outdoor_today_sample_count(self) -> int:
        return len(self._today_outdoor())

    def tou_av_window(self) -> TouAvWindow:
        window = getattr(self._coordinator, "_tou_av_window", None)
        return window if isinstance(window, TouAvWindow) else TOU_AV_INACTIVE
