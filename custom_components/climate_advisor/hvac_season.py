"""Which season a local day belongs to, by what the HVAC actually did that day (Issue #1230 R1, DP-8).

Occupant view: the prediction scorecard is split by season because a house that is being heated behaves differently
from one that is being cooled. The season of a day is not the calendar month: it is what the heating, the air
conditioning and the whole-house fan did that day.

* a day with any heating is ``"heating"``;
* else a day with cooling, or the whole-house fan running (``fan_running`` with ``nat_vent_active``, or an hvac
  ``fan`` action), is ``"summer"`` (the cooling / fan season);
* else ``"shoulder"`` (a day with neither: reported apart, never scored).

Moved here from ``tools/predict_backtest.py`` in Issue #1492. The one rule shared by the live "How each model has
scored" table (``prediction_accuracy.live_scorecard``) and the walk-forward backtest (``tools/predict_backtest.py`` imports this file by path). Pure: standard library only, no
package-relative import, so the tool can load it without Home Assistant. The label only groups scores; no model and no
decision reads it.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from datetime import date, datetime, tzinfo
from typing import Any

HEATING = "heating"
SUMMER = "summer"
SHOULDER = "shoulder"
SCORED_SEASONS: tuple[str, ...] = (HEATING, SUMMER)


def season_of_day(heating: bool, cooling_or_fan: bool) -> str:
    """The season of one day from whether it had any heating, and any cooling or whole-house fan running."""
    if heating:
        return HEATING
    return SUMMER if cooling_or_fan else SHOULDER


def _local_date(value: Any, tz: tzinfo) -> date | None:
    try:
        dt = datetime.fromisoformat(str(value))
    except TypeError, ValueError:
        return None
    dt = dt.replace(tzinfo=tz) if dt.tzinfo is None else dt
    return dt.astimezone(tz).date()


def day_seasons(rows: Iterable[Mapping[str, Any]], tz: tzinfo) -> dict[date, str]:
    """``{local date: season}`` for every day the chart-log ``rows`` show heating, cooling or the fan season.

    A date absent from the result had none of them (``"shoulder"``). A naive ``ts`` is read in ``tz``.
    """
    heat: set[date] = set()
    cool: set[date] = set()
    for r in rows:
        if not isinstance(r, Mapping):
            continue
        d = _local_date(r.get("ts"), tz)
        if d is None:
            continue
        hv = str(r.get("hvac") or "").lower()
        fan_season = r.get("fan_running", r.get("fan")) is True and (
            r.get("nat_vent_active") is True or hv.startswith("fan")
        )
        if hv == "heating":
            heat.add(d)
        elif hv == "cooling" or fan_season:
            cool.add(d)
    return {d: season_of_day(d in heat, d in cool) for d in heat | cool}
